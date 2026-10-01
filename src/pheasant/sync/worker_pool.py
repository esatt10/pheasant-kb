"""Durable dispatch to remote preparation workers (Phase 35.5).

Before this module, ``SyncEngine`` picked a worker with ``position %
len(urls)`` and called :func:`~pheasant.sync.remote_worker.prepare_remote`,
which is one ``urlopen`` per file. Every failure mode that a distributed
system actually has was unhandled: **one dead worker failed its share of
every sync**, a restart or a rolling deploy did the same, a slow worker had
no deadline, and a new TCP+TLS handshake was paid per file.

What this adds, and why each piece earns its place:

* **Connection reuse.** One keep-alive :mod:`http.client` connection per
  endpoint per concurrent caller, checked out of a pool. On a 50k-file source
  that is 50k handshakes avoided; over TLS the handshake alone can exceed the
  parse it is carrying.
* **Batching.** Several tasks per request, so the per-request overhead is
  amortized. The coordinator still reads and commits one file at a time — a
  batch is a transport grouping, not a semantic one.
* **Bounded retry with full jitter**, honouring ``Retry-After``. Retrying in
  lockstep is how a briefly-overloaded worker gets held down; jitter is what
  spreads a thundering herd back out.
* **A circuit breaker per endpoint.** Retrying a worker that is *down* (as
  opposed to busy) spends the whole sync's time budget discovering the same
  thing once per file. After a threshold of consecutive failures the endpoint
  is skipped outright until a cooldown lets one probe through.
* **Failover, then local preparation.** Endpoints are tried in order of
  health; if every one fails, the caller falls back to preparing locally.
  Remote preparation is a *throughput* optimization, so it must never be able
  to fail a sync — the same policy Phase 34.5 applied to WASM acceleration,
  and the opposite of the sandboxed extractor, which fails loudly because it
  is a security property.
* **Deadline propagation.** The remaining budget travels with the request, so
  a worker that picks up a task whose caller has already given up declines it
  instead of burning CPU on an answer nobody will read.
* **Content-addressed idempotency keys.** The payload sha256 is already
  computed by the engine, so a retry after a timeout carries the same key and
  the worker can answer from its bounded cache rather than re-parsing.

Nothing here is reachable unless ``sync.concurrency.file_executor`` is
``remote`` and ``remote_worker_urls`` is non-empty, so a standalone region is
byte-identical (rule 7).
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pheasant.sync.remote_worker import (
    ParsedArtifact,
    RemoteWorkerError,
    parsed_from_wire,
)

logger = logging.getLogger(__name__)

#: Consecutive failures before an endpoint is taken out of rotation. Three is
#: enough to distinguish "down" from "unlucky" without spending a long time
#: proving it: two transient errors in a row happen, three in a row on the
#: same endpoint while others succeed does not.
FAILURE_THRESHOLD = 3

#: How long a tripped breaker stays open before one probe is allowed through.
#: Matched to a rolling-deploy pod replacement rather than to a human: the
#: common cause of an endpoint going away and coming back is a restart.
BREAKER_RESET_SECONDS = 30.0

#: Attempts per task across *all* endpoints, including the first. Beyond this
#: the caller prepares locally, which always works.
MAX_ATTEMPTS = 4

#: Backoff base and ceiling for the jittered sleep between attempts.
BACKOFF_BASE_SECONDS = 0.25
BACKOFF_MAX_SECONDS = 8.0

#: Cap on a server-supplied ``Retry-After``. A worker that asks for ten
#: minutes is telling us to use a different worker, not to sleep.
RETRY_AFTER_MAX_SECONDS = 30.0

#: Idle pooled connections kept per endpoint. Above the file-worker count
#: they would only sit idle until the server closed them.
POOL_SIZE = 16


class AllWorkersFailed(RemoteWorkerError):
    """No endpoint could prepare the batch; the caller should do it locally.

    Carries the per-endpoint reasons so the log line says *why* the fleet was
    unusable rather than only that it was.
    """

    def __init__(self, reasons: dict[str, str]) -> None:
        detail = "; ".join(f"{url}: {reason}" for url, reason in reasons.items()) or "no endpoints"
        super().__init__(f"every remote preparation worker failed ({detail})")
        self.reasons = dict(reasons)


class TaskRejected(RemoteWorkerError):
    """The worker understood the task and refused it (HTTP 4xx below 429).

    Distinct from a transport failure on purpose: another worker running the
    same code will refuse it identically, so failing over is pure latency.
    The caller prepares locally, which accepts everything the remote path
    deliberately does not (taxonomy, binary formats, repair mode).
    """


def redact_endpoint(url: str) -> str:
    """Drop any userinfo before a URL becomes a metric label or a log line.

    ``remote_worker_urls`` is ordinary config, but nothing stops an operator
    writing ``https://user:pass@worker/`` into it, and both destinations here
    are read by people who are not that operator.
    """

    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.netloc or "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


@dataclass
class _Breaker:
    """Per-endpoint health, guarded by the pool's lock."""

    failures: int = 0
    opened_at: float = 0.0
    #: Set while a half-open probe is in flight so a hundred concurrent
    #: callers do not all probe a dead endpoint at once.
    probing: bool = False

    def available(self, now: float) -> bool:
        if self.failures < FAILURE_THRESHOLD:
            return True
        if self.probing:
            return False
        return now - self.opened_at >= BREAKER_RESET_SECONDS

    @property
    def tripped(self) -> bool:
        return self.failures >= FAILURE_THRESHOLD


@dataclass
class _Endpoint:
    url: str
    breaker: _Breaker = field(default_factory=_Breaker)
    idle: deque[Any] = field(default_factory=deque)
    successes: int = 0
    failures: int = 0

    @property
    def label(self) -> str:
        return redact_endpoint(self.url)


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds form only. The HTTP-date form is legal and never used here."""

    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, RETRY_AFTER_MAX_SECONDS)


def _backoff(attempt: int, retry_after: float | None) -> float:
    """Full-jitter exponential backoff, or the server's own number.

    Full jitter (``uniform(0, window)``) rather than "exponential plus a
    little noise": the point is to *decorrelate* retries from many files in
    flight, and a narrow jitter band around a common delay barely does that.
    """

    if retry_after is not None:
        return retry_after
    window = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2**attempt))
    return random.uniform(0.0, window)


def build_transport(name: str) -> Any:
    """Select a wire format. Retry, failover and breakers are unaffected.

    An unknown name falls back to HTTP with a warning — it is a typo, and a
    typo should not stop a sync. A *known* name whose dependency is missing
    raises instead: an operator who asked for gRPC and silently got JSON
    would keep paying the base64 inflation they chose gRPC to avoid, with
    nothing anywhere saying so.
    """

    from pheasant.sync.worker_transport import HttpTransport

    requested = (name or "http").strip().lower()
    if requested == "grpc":
        from pheasant.sync.grpc_worker import GrpcTransport

        return GrpcTransport()
    if requested != "http":
        logger.warning(
            "Unknown sync.concurrency.worker_transport=%r; using http",
            name,
        )
    return HttpTransport()


class WorkerPool:
    """Dispatch preparation batches across endpoints, durably.

    Thread-safe: the engine calls this from its file-worker pool. One instance
    per sync run is the intended lifetime, so breaker state and pooled
    connections are shared by every file in the source.
    """

    def __init__(
        self,
        endpoints: list[str],
        token: str,
        *,
        timeout: float = 120.0,
        transport: Any | None = None,
        transport_name: str = "http",
        sleep: Any = time.sleep,
        clock: Any = time.monotonic,
    ) -> None:
        self._endpoints = [_Endpoint(url=url.rstrip("/")) for url in endpoints if url]
        self._token = token
        self._timeout = float(timeout)
        self._lock = threading.Lock()
        self._sleep = sleep
        self._clock = clock
        self._cursor = 0
        self._transport = transport if transport is not None else build_transport(transport_name)

    # -- health -----------------------------------------------------------

    def endpoint_health(self) -> dict[str, bool]:
        """``{endpoint: up}`` for the ``pheasant_worker_up`` gauge."""

        now = self._clock()
        with self._lock:
            return {
                endpoint.label: not endpoint.breaker.tripped or endpoint.breaker.available(now)
                for endpoint in self._endpoints
            }

    def stats(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "endpoint": endpoint.label,
                    "successes": endpoint.successes,
                    "failures": endpoint.failures,
                    "circuit_open": endpoint.breaker.tripped,
                }
                for endpoint in self._endpoints
            ]

    def publish_health(self) -> None:
        """Push endpoint health into the metrics registry, best effort."""

        try:
            from pheasant.telemetry import metrics

            for label, up in self.endpoint_health().items():
                metrics.REGISTRY.set("pheasant_worker_up", 1.0 if up else 0.0, endpoint=label)
        except Exception:  # pragma: no cover - telemetry must never fail a sync
            logger.debug("could not publish worker health", exc_info=True)

    def close(self) -> None:
        with self._lock:
            endpoints = list(self._endpoints)
        for endpoint in endpoints:
            while True:
                try:
                    connection = endpoint.idle.popleft()
                except IndexError:
                    break
                self._transport.discard(connection)
        close_transport = getattr(self._transport, "close", None)
        if callable(close_transport):
            close_transport()

    def __enter__(self) -> WorkerPool:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # -- dispatch ---------------------------------------------------------

    def _order(self) -> list[_Endpoint]:
        """Available endpoints first, rotating so load spreads.

        Rotation is round-robin like the code this replaces, but over the
        *available* set rather than over the configured list — which is the
        whole difference between "one dead worker costs its share of every
        sync" and "one dead worker costs three requests".
        """

        now = self._clock()
        with self._lock:
            if not self._endpoints:
                return []
            self._cursor = (self._cursor + 1) % len(self._endpoints)
            rotated = self._endpoints[self._cursor :] + self._endpoints[: self._cursor]
            available = [item for item in rotated if item.breaker.available(now)]
            # When every endpoint is open the honest answer is none, not "try
            # the least-bad one anyway": probing a fleet that is down costs
            # MAX_ATTEMPTS round trips *per batch*, which is the exact expense
            # the breaker exists to stop. The caller prepares locally in the
            # meantime and the cooldown lets a probe through on its own.
            # Deliberately does NOT set `probing` here. Marking every
            # cooled-down candidate leaked the flag: only the endpoint
            # actually dispatched to ever cleared it, so if an earlier one in
            # the rotation answered, the rest kept `probing=True` forever —
            # and `available()` returns False while it is set, so a recovered
            # worker was excluded for the pool's lifetime and reported down by
            # `pheasant_worker_up`. The flag is now set and cleared around the
            # dispatch itself (`_probe`), which cannot leak.
            return available

    @contextmanager
    def _probe(self, endpoint: _Endpoint):
        """Hold the half-open slot for exactly one dispatch.

        A tripped endpoint admits one probe at a time, so a hundred concurrent
        callers do not all rediscover that it is down. Paired in a
        ``finally``, because every way out of the dispatch loop — success,
        failure, the attempt budget, a deadline — has to release it.
        """

        with self._lock:
            probing = endpoint.breaker.tripped
            if probing:
                endpoint.breaker.probing = True
        try:
            yield
        finally:
            if probing:
                with self._lock:
                    endpoint.breaker.probing = False

    def _record_success(self, endpoint: _Endpoint) -> None:
        with self._lock:
            endpoint.breaker.failures = 0
            endpoint.breaker.probing = False
            endpoint.successes += 1

    def _record_failure(self, endpoint: _Endpoint) -> None:
        with self._lock:
            endpoint.breaker.failures += 1
            endpoint.breaker.probing = False
            endpoint.failures += 1
            if endpoint.breaker.failures == FAILURE_THRESHOLD:
                endpoint.breaker.opened_at = self._clock()
                logger.warning(
                    "Remote preparation worker %s failed %d times in a row; skipping it for %.0fs",
                    endpoint.label,
                    FAILURE_THRESHOLD,
                    BREAKER_RESET_SECONDS,
                )
            elif endpoint.breaker.failures > FAILURE_THRESHOLD:
                endpoint.breaker.opened_at = self._clock()

    def prepare_batch(
        self,
        tasks: list[dict[str, Any]],
        *,
        deadline: float | None = None,
    ) -> list[ParsedArtifact | None]:
        """Prepare ``tasks`` remotely, returning one result per task in order.

        Raises :class:`AllWorkersFailed` when no endpoint could answer, and
        :class:`TaskRejected` when a worker refused the batch on its merits.
        Both mean "prepare locally"; they are distinct so the caller can log
        the difference between a fleet problem and a task problem.
        """

        if not tasks:
            return []
        keys = [_idempotency_key(task) for task in tasks]
        return self._with_failover(
            lambda endpoint, remaining: self._dispatch(endpoint, tasks, keys, remaining),
            deadline,
        )

    def extract_pages(
        self,
        content: bytes,
        first: int,
        stop: int,
        *,
        deadline: float | None = None,
    ) -> list[str]:
        """The text of PDF pages ``[first, stop)``, read by one worker.

        Same retry, failover, breakers and deadline as :meth:`prepare_batch`,
        and the same two exceptions meaning "do it locally". A transport that
        cannot carry the request (HTTP has no page route) is a
        :class:`TaskRejected`: every worker behind it would refuse alike.
        """

        extract = getattr(self._transport, "extract_pages", None)
        if extract is None:
            raise TaskRejected("this worker transport cannot extract PDF page ranges")

        def call(endpoint: _Endpoint, remaining: float | None) -> list[str]:
            timeout = (
                self._timeout if remaining is None else max(0.001, min(self._timeout, remaining))
            )
            pages = extract(endpoint.url, content, first, stop, token=self._token, timeout=timeout)
            if len(pages) != stop - first:
                raise _Retryable(f"worker returned {len(pages)} pages for {stop - first}")
            return pages

        return self._with_failover(call, deadline)

    def _with_failover(self, call: Any, deadline: float | None) -> Any:
        """Run ``call(endpoint, remaining)`` against the fleet until one answers."""

        if not self._endpoints:
            raise AllWorkersFailed({})
        reasons: dict[str, str] = {}
        attempt = 0
        while attempt < MAX_ATTEMPTS:
            candidates = self._order()
            if not candidates:
                for endpoint in self._endpoints:
                    reasons.setdefault(endpoint.label, "circuit open")
                break
            progressed = False
            for endpoint in candidates:
                if attempt >= MAX_ATTEMPTS:
                    break
                remaining = self._remaining(deadline)
                if remaining is not None and remaining <= 0:
                    reasons.setdefault(endpoint.label, "deadline exceeded before dispatch")
                    raise AllWorkersFailed(reasons)
                attempt += 1
                progressed = True
                try:
                    with self._probe(endpoint):
                        results = call(endpoint, remaining)
                except TaskRejected:
                    self._record_success(endpoint)  # the worker is fine; the task is not
                    raise
                except _Retryable as exc:
                    self._record_failure(endpoint)
                    reasons[endpoint.label] = str(exc)
                    if attempt < MAX_ATTEMPTS:
                        self._sleep(_backoff(attempt, exc.retry_after))
                    continue
                except RemoteWorkerError as exc:
                    self._record_failure(endpoint)
                    reasons[endpoint.label] = str(exc)
                    continue
                except Exception as exc:  # noqa: BLE001 - see below
                    # A transport can fail in ways that are not
                    # `RemoteWorkerError`: `binascii.Error` from a corrupt
                    # base64 payload, `json.JSONDecodeError` from a truncated
                    # body, `grpc.RpcError` from the gRPC channel. Letting any
                    # of those out aborts the whole sync over one sick worker —
                    # the exact single-point-of-failure this pool exists to
                    # remove. A malformed answer is a failed endpoint, so it
                    # counts against the breaker and the fleet moves on; if
                    # every endpoint does it the caller still gets
                    # `AllWorkersFailed` and prepares locally.
                    self._record_failure(endpoint)
                    reasons[endpoint.label] = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "Remote preparation worker %s raised %s; treating it as a worker failure",
                        endpoint.label,
                        type(exc).__name__,
                        exc_info=True,
                    )
                    continue
                self._record_success(endpoint)
                return results
            if not progressed:
                break
        raise AllWorkersFailed(reasons)

    def _remaining(self, deadline: float | None) -> float | None:
        if deadline is None:
            return None
        return deadline - self._clock()

    def _dispatch(
        self,
        endpoint: _Endpoint,
        tasks: list[dict[str, Any]],
        keys: list[str],
        remaining: float | None,
    ) -> list[ParsedArtifact | None]:
        timeout = self._timeout if remaining is None else max(0.001, min(self._timeout, remaining))
        try:
            connection = endpoint.idle.popleft()
        except IndexError:
            connection = None
        body, reuse = self._transport.post(
            connection,
            endpoint.url,
            tasks,
            keys,
            token=self._token,
            timeout=timeout,
            deadline_seconds=remaining,
        )
        if reuse is not None:
            if len(endpoint.idle) < POOL_SIZE:
                endpoint.idle.append(reuse)
            else:
                self._transport.discard(reuse)
        results = body.get("results")
        if not isinstance(results, list) or len(results) != len(tasks):
            raise _Retryable(
                f"worker returned {len(results) if isinstance(results, list) else '?'} "
                f"results for {len(tasks)} tasks"
            )
        prepared: list[ParsedArtifact | None] = []
        for entry in results:
            if not isinstance(entry, dict):
                raise _Retryable("worker returned a malformed result entry")
            if entry.get("error"):
                raise TaskRejected(str(entry["error"]))
            prepared.append(parsed_from_wire(entry.get("parsed")))
        return prepared


class _Retryable(RemoteWorkerError):
    """A failure that another attempt could plausibly survive."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _idempotency_key(task: dict[str, Any]) -> str:
    """Content-addressed: same bytes, same source, same key.

    The engine has already hashed the payload (``engine.py`` computes
    ``content_hash`` before dispatch), so this costs nothing and lets a worker
    answer a retried request from cache instead of re-parsing. The relative
    path is included because two identical files in one source are two
    artifacts with two stable IDs.
    """

    payload = task.get("payload") or {}
    item = task.get("item") or {}
    source = task.get("source") or {}
    digest = payload.get("sha256") or item.get("sha256") or ""
    if not digest:
        import hashlib

        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
    return f"{source.get('name', '')}:{item.get('relative_path', '')}:{digest}"
