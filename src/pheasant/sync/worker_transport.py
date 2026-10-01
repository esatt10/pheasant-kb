"""Wire transports for remote preparation (Phase 35.5).

:class:`~pheasant.sync.worker_pool.WorkerPool` owns *policy* — retry,
failover, breakers, deadlines. This module owns *bytes*: how a batch of
preparation tasks reaches a worker and how its answer comes back. Splitting
them is what lets the gRPC transport inherit every durability property
without reimplementing any of it.

The HTTP transport is the dependency-free default (rule 7) and is built on
:mod:`http.client` rather than :func:`urllib.request.urlopen`, because
``urlopen`` closes its connection after every response. Keep-alive is the
point: a source of 50,000 files was paying 50,000 TCP — and over TLS, 50,000
handshakes — for work that fits in one connection.

Error classification lives here because it is protocol knowledge, and it is
the part that decides whether a failure is worth another attempt:

===========================  ==========================================
Response                     Meaning
===========================  ==========================================
2xx                          success
408, 429, 5xx, socket error  retryable — same or another endpoint
401, 403                     endpoint misconfigured; breaker counts it
404                          no batch route: fall back to the legacy
                             one-task-per-request path (rolling upgrade)
413, 422, other 4xx          the worker refused the task on its merits
===========================  ==========================================

A 422 must **not** trigger failover: every worker runs the same refusal
logic, so trying four of them spends four round trips to learn one thing.
The caller prepares locally instead, which accepts everything the remote path
deliberately does not.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
from typing import Any
from urllib.parse import urlsplit

from pheasant.sync.remote_worker import RemoteWorkerError
from pheasant.telemetry.interactions import inject_traceparent

logger = logging.getLogger(__name__)

BATCH_PATH = "/internal/indexing/prepare-batch"
SINGLE_PATH = "/internal/indexing/prepare"
#: One page range of one long PDF (`sync/pdf_split.py`). A worker that predates
#: it answers 404, which `_decode` turns into "read it locally".
EXTRACT_PAGES_PATH = "/internal/indexing/extract-pages"

#: Header carrying the caller's remaining budget. The worker declines a task
#: whose caller has already given up rather than computing an answer nobody
#: will read — the cheapest possible form of backpressure.
DEADLINE_HEADER = "X-Pheasant-Deadline-Seconds"

#: Batch-level content address, for logs and for cache statistics. Per-task
#: keys travel in the body, because that is where the tasks are.
IDEMPOTENCY_HEADER = "X-Pheasant-Idempotency-Key"

USER_AGENT = "pheasant-index-coordinator/2"


def _apply_socket_timeout(connection: Any, timeout: float) -> None:
    """Push ``timeout`` onto the live socket, if there is one.

    Best-effort: a connection that has not dialled yet has no ``sock``, and it
    will pick the timeout up from ``connection.timeout`` when it does.
    """

    sock = getattr(connection, "sock", None)
    if sock is None:
        return
    try:
        sock.settimeout(timeout)
    except OSError:  # pragma: no cover - a socket already being torn down
        pass


class HttpTransport:
    """Keep-alive HTTP/1.1 batch transport.

    Connections are owned by the pool (one idle deque per endpoint) and
    handed in and out of :meth:`post`, so this class holds no per-endpoint
    state except which endpoints turned out to predate the batch route.
    """

    def __init__(self) -> None:
        self._legacy: set[str] = set()
        self._lock = threading.Lock()

    # -- connection lifecycle --------------------------------------------

    def _connect(self, url: str, timeout: float) -> Any:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if not host:
            raise RemoteWorkerError(f"remote worker URL has no host: {url}")
        if parts.scheme == "https":
            return http.client.HTTPSConnection(host, parts.port, timeout=timeout)
        if parts.scheme == "http":
            return http.client.HTTPConnection(host, parts.port, timeout=timeout)
        raise RemoteWorkerError(f"remote worker URL must be http or https: {url}")

    def discard(self, connection: Any) -> None:
        try:
            connection.close()
        except Exception:  # pragma: no cover - closing a dead socket
            pass

    # -- request ----------------------------------------------------------

    def post(
        self,
        connection: Any | None,
        url: str,
        tasks: list[dict[str, Any]],
        keys: list[str],
        *,
        token: str,
        timeout: float,
        deadline_seconds: float | None,
    ) -> tuple[dict[str, Any], Any | None]:
        """Send one batch; return ``(body, connection_to_reuse_or_None)``."""

        with self._lock:
            legacy = url in self._legacy
        if legacy:
            return self._post_legacy(connection, url, tasks, keys, token, timeout, deadline_seconds)

        prefix = urlsplit(url).path.rstrip("/")
        body = {
            "tasks": tasks,
            "idempotency_keys": keys,
            "deadline_seconds": deadline_seconds,
        }
        try:
            status, headers, raw, connection = self._request(
                connection, url, prefix + BATCH_PATH, body, token, timeout, deadline_seconds, keys
            )
        except OSError as exc:
            self.discard(connection) if connection is not None else None
            raise _retryable(f"transport error: {exc}") from exc
        if status == 404:
            # A worker from before 35.5. Remember it and use the one-task
            # route, so a coordinator upgraded ahead of its fleet keeps
            # working instead of failing every batch.
            with self._lock:
                self._legacy.add(url)
            logger.info(
                "Remote worker %s has no batch route; using the single-task path",
                url,
            )
            self._release(connection, headers)
            return self._post_legacy(None, url, tasks, keys, token, timeout, deadline_seconds)
        reuse = self._release(connection, headers)
        return self._decode(status, headers, raw, url), reuse

    def extract_pages(
        self,
        url: str,
        content: bytes,
        first: int,
        stop: int,
        *,
        token: str,
        timeout: float,
    ) -> list[str]:
        """The text of PDF pages ``[first, stop)`` from one worker.

        A connection of its own, closed after the answer: a long PDF is a
        handful of ranges, and holding one of the pool's keep-alive sockets for
        a multi-megabyte body would make a file's preparation wait behind it.
        """

        import base64

        from pheasant.ingestion.pdf_pages import pdf_reader_version
        from pheasant.sync.worker_pool import TaskRejected

        prefix = urlsplit(url).path.rstrip("/")
        body = {
            "content_base64": base64.b64encode(content).decode("ascii"),
            "first_page": first,
            "stop_page": stop,
        }
        connection = None
        try:
            status, headers, raw, connection = self._request(
                None, url, prefix + EXTRACT_PAGES_PATH, body, token, timeout, timeout, []
            )
        except OSError as exc:
            raise _retryable(f"transport error: {exc}") from exc
        finally:
            if connection is not None:
                self.discard(connection)
        decoded = self._decode(status, headers, raw, url)
        version = str(decoded.get("reader_version") or "")
        if version != pdf_reader_version():
            raise TaskRejected(
                f"worker reads PDFs with pymupdf {version or '(none)'}, "
                f"this process with {pdf_reader_version() or '(none)'}"
            )
        pages = decoded.get("pages")
        if not isinstance(pages, list) or not all(isinstance(page, str) for page in pages):
            raise _retryable("worker returned malformed pages")
        return pages

    def _post_legacy(
        self,
        connection: Any | None,
        url: str,
        tasks: list[dict[str, Any]],
        keys: list[str],
        token: str,
        timeout: float,
        deadline_seconds: float | None,
    ) -> tuple[dict[str, Any], Any | None]:
        """One request per task over one reused connection.

        Still better than what it replaces: the connection survives the whole
        batch, and every retry/failover/breaker property above still applies,
        because the pool never learns which route was taken.
        """

        prefix = urlsplit(url).path.rstrip("/")
        results: list[dict[str, Any]] = []
        reuse = connection
        for task, key in zip(tasks, keys, strict=True):
            try:
                status, headers, raw, reuse = self._request(
                    reuse, url, prefix + SINGLE_PATH, task, token, timeout, deadline_seconds, [key]
                )
            except OSError as exc:
                self.discard(reuse) if reuse is not None else None
                raise _retryable(f"transport error: {exc}") from exc
            body = self._decode(status, headers, raw, url)
            results.append({"parsed": body.get("parsed")})
            reuse = self._release(reuse, headers)
        return {"results": results}, reuse

    def _request(
        self,
        connection: Any | None,
        url: str,
        path: str,
        body: Any,
        token: str,
        timeout: float,
        deadline_seconds: float | None,
        keys: list[str],
    ) -> tuple[int, dict[str, str], bytes, Any]:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Content-Length": str(len(payload)),
            "Connection": "keep-alive",
        }
        if deadline_seconds is not None:
            headers[DEADLINE_HEADER] = f"{max(0.0, deadline_seconds):.3f}"
        if keys:
            headers[IDEMPOTENCY_HEADER] = keys[0] if len(keys) == 1 else _batch_key(keys)
        # Beside the deadline and the idempotency key, for the same reason both
        # of those are here: a hop the coordinator makes on someone's behalf
        # should say whose behalf. A no-op today, because preparation runs on
        # the sync path where no interaction trace is ambient -- and correct
        # the moment one is, rather than a call site somebody has to remember.
        inject_traceparent(headers)

        attempted_reuse = connection is not None
        if connection is None:
            connection = self._connect(url, timeout)
        # `HTTPConnection.timeout` is only consulted by `connect()`, so on a
        # *pooled* socket — already connected — assigning it does nothing and
        # the connection keeps whatever timeout it was opened with. A batch
        # given a short remaining deadline would then block for the original,
        # much longer timeout, and deadline propagation silently stopped
        # applying to exactly the reused connections the pool exists to
        # provide. Setting it on the live socket is what actually takes effect.
        connection.timeout = timeout
        _apply_socket_timeout(connection, timeout)
        try:
            connection.request("POST", path or "/", body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
        except OSError:
            self.discard(connection)
            if not attempted_reuse:
                raise
            # A pooled connection the server closed between requests looks
            # exactly like a dead worker. Retry once on a fresh socket before
            # blaming the endpoint, or every idle timeout would trip a breaker.
            connection = self._connect(url, timeout)
            _apply_socket_timeout(connection, timeout)
            connection.request("POST", path or "/", body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
        return (
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            raw,
            connection,
        )

    def _release(self, connection: Any | None, headers: dict[str, str]) -> Any | None:
        if connection is None:
            return None
        if "close" in headers.get("connection", "").lower():
            self.discard(connection)
            return None
        return connection

    def _decode(self, status: int, headers: dict[str, str], raw: bytes, url: str) -> dict[str, Any]:
        if 200 <= status < 300:
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _retryable(f"worker returned unreadable JSON: {exc}") from exc
            if not isinstance(decoded, dict):
                raise _retryable("worker returned a non-object body")
            return decoded
        detail = _detail(raw)
        if status in {408, 429} or status >= 500:
            raise _retryable(
                f"HTTP {status} from {url}: {detail}",
                retry_after=headers.get("retry-after"),
            )
        if status in {401, 403}:
            raise RemoteWorkerError(f"HTTP {status} from {url}: {detail}")
        from pheasant.sync.worker_pool import TaskRejected

        raise TaskRejected(f"HTTP {status} from {url}: {detail}")


def _detail(raw: bytes) -> str:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except Exception:
        return raw[:200].decode("utf-8", "replace")
    if isinstance(decoded, dict) and "detail" in decoded:
        return str(decoded["detail"])[:200]
    return str(decoded)[:200]


def _batch_key(keys: list[str]) -> str:
    import hashlib

    digest = hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()
    return f"batch:{len(keys)}:{digest}"


def _retryable(message: str, retry_after: str | None = None) -> Exception:
    from pheasant.sync.worker_pool import _parse_retry_after, _Retryable

    return _Retryable(message, _parse_retry_after(retry_after))
