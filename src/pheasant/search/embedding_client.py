"""The OpenAI-spec embeddings HTTP client, and what one request observes of it.

Split from ``vector_store``, which holds vectors and searches them: this module
only talks to a provider. ``OpenAISpecEmbedder`` speaks the standard
``POST {base_url}/embeddings`` shape (OpenAI, Azure-compatible gateways, vLLM,
Ollama's compatibility endpoint) with stdlib HTTP and no SDK: a kept-alive
connection per thread and origin, bounded retry on transient failures, a
shared adaptive gate under rate limiting, and a request deadline that every
wait honours. ``QueryEmbeddingMetrics`` is the per-request accounting the
assistant reads back (cache hits, provider requests and retries) for a query
being answered.
"""

from __future__ import annotations

import base64
import email.utils
import http.client
import io
import json
import logging
import os
import random
import re
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class QueryEmbeddingMetrics:
    """Request-local query vector cache and provider observations."""

    cache_hits: int = 0
    fresh_misses: int = 0
    singleflight_waits: int = 0
    provider_requests: int = 0
    provider_retries: int = 0
    elapsed_seconds: float = 0.0
    failed: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _failed_vectors: dict[tuple, BaseException] = field(default_factory=dict, repr=False)

    def add(self, field_name: str, value: int | float) -> None:
        with self._lock:
            setattr(self, field_name, getattr(self, field_name) + value)

    def remember_failure(self, keys: list[tuple], error: BaseException) -> None:
        with self._lock:
            for key in keys:
                self._failed_vectors[key] = error

    def prior_failure(self, key: tuple) -> BaseException | None:
        with self._lock:
            return self._failed_vectors.get(key)

    def as_dict(self) -> dict[str, int | float]:
        with self._lock:
            return {
                "cache_hits": self.cache_hits,
                "fresh_misses": self.fresh_misses,
                "singleflight_waits": self.singleflight_waits,
                "provider_requests": self.provider_requests,
                "provider_retries": self.provider_retries,
                "elapsed_seconds": self.elapsed_seconds,
                "failed": self.failed,
            }


_ACTIVE_QUERY_EMBEDDING_METRICS: ContextVar[QueryEmbeddingMetrics | None] = ContextVar(
    "pheasant_query_embedding_metrics", default=None
)


@contextmanager
def collect_query_embedding_metrics():
    """Collect cache and provider outcomes for one assistant request."""
    metrics = QueryEmbeddingMetrics()
    token = _ACTIVE_QUERY_EMBEDDING_METRICS.set(metrics)
    try:
        yield metrics
    finally:
        _ACTIVE_QUERY_EMBEDDING_METRICS.reset(token)


def _record_embedding_provider_request() -> None:
    metrics = _ACTIVE_QUERY_EMBEDDING_METRICS.get()
    if metrics is not None:
        metrics.add("provider_requests", 1)


def _record_embedding_provider_retry() -> None:
    metrics = _ACTIVE_QUERY_EMBEDDING_METRICS.get()
    if metrics is not None:
        metrics.add("provider_retries", 1)


#: HTTP statuses worth trying again: overload, rate limiting, and the gateway
#: family. Explicitly *not* 400/401/403/404 — a malformed request or a wrong
#: key fails identically on every retry, so retrying only spends time.
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: Transport-level failures. `ssl.SSLError` is first because it is the one a
#: real 12k-file index actually died on.
_TRANSIENT_ERRORS = (
    ssl.SSLError,
    URLError,
    TimeoutError,
    ConnectionError,
    socket.timeout,
    http.client.HTTPException,
)

#: Ceiling on locally invented backoff. A provider's explicit Retry-After is
#: intentionally not capped: waking before its quota resets causes another 429.
_MAX_BACKOFF_SECONDS = 30.0

#: Kept-alive provider connections, one per thread per origin.
_CONNECTIONS = threading.local()


def _transport(request: Request, timeout: float) -> bytes:
    """Send ``request`` on a kept-alive connection; the response body.

    ``urlopen`` opened a new TCP and TLS session for every call, and a query
    embedding is one call per question: measured from the lab api tier to
    api.openai.com, 325 ms p50 per query against 263 ms on a connection kept
    open between calls. One connection per thread, because an
    ``HTTPConnection`` serves one request at a time and indexing embeds from
    several threads. A status of 400 or above raises ``HTTPError`` exactly as
    ``urlopen`` does, so the retry policy reads the same either way. A proxied
    origin keeps ``urlopen``, which is what honours the proxy settings.
    """

    from urllib.request import getproxies, proxy_bypass

    parts = urlsplit(request.full_url)
    host = parts.hostname or ""
    if parts.scheme in getproxies() and not proxy_bypass(host):
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    key = (parts.scheme, host, parts.port)
    pool: dict[tuple, http.client.HTTPConnection] = _CONNECTIONS.__dict__.setdefault("pool", {})
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    headers = dict(request.header_items())
    for attempt in range(2):
        connection = pool.get(key)
        reused = connection is not None
        if connection is None:
            https = parts.scheme == "https"
            factory = http.client.HTTPSConnection if https else http.client.HTTPConnection
            connection = factory(host, parts.port, timeout=timeout)
            pool[key] = connection
        else:
            connection.timeout = timeout
            if connection.sock is not None:
                connection.sock.settimeout(timeout)
        try:
            connection.request(request.get_method(), target, body=request.data, headers=headers)
            response = connection.getresponse()
            body = response.read()
        except (http.client.RemoteDisconnected, ConnectionError):
            # Reset, aborted (Windows) or a broken pipe: what writing to a
            # socket the server already closed looks like on each platform.
            connection.close()
            pool.pop(key, None)
            if reused and attempt == 0:
                continue  # the server closed an idle connection: once more, on a new one
            raise
        except BaseException:
            connection.close()
            pool.pop(key, None)
            raise
        if response.will_close:
            connection.close()
            pool.pop(key, None)
        if response.status >= 400:
            fp = io.BytesIO(body)
            url, status, reason = request.full_url, response.status, response.reason
            raise HTTPError(url, status, reason, response.headers, fp)
        return body
    raise AssertionError("unreachable")  # pragma: no cover


def _embedding_values(item: dict[str, Any]) -> list[float]:
    """One embedding, sent as a float list or as base64 little-endian float32."""

    value = item["embedding"]
    if isinstance(value, str):
        return np.frombuffer(base64.b64decode(value), dtype="<f4").astype(float).tolist()
    return [float(v) for v in value]


def _retry_after_seconds(header: str | None, fallback: float) -> float:
    """Parse a `Retry-After` value, falling back to our own backoff."""
    if not header:
        return fallback
    try:
        return max(0.0, float(header))
    except (TypeError, ValueError):
        try:
            retry_at = email.utils.parsedate_to_datetime(header)
            return max(0.0, retry_at.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return fallback


_RESET_PART_RE = re.compile(r"([0-9]*\.?[0-9]+)\s*(ms|s|m|h)", re.IGNORECASE)


def _reset_after_seconds(header: str | None) -> float | None:
    """Parse OpenAI-style ``x-ratelimit-reset-*`` duration headers."""

    if not header:
        return None
    multipliers = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    parts = _RESET_PART_RE.findall(str(header))
    if not parts:
        try:
            return max(0.0, float(header))
        except (TypeError, ValueError):
            return None
    return sum(float(value) * multipliers[unit.lower()] for value, unit in parts)


class _AdaptiveRateGate:
    """One shared cooldown and AIMD concurrency gate per embedder/model."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._ceiling = 1
        self._limit = 1
        self._active = 0
        self._successes = 0
        self._blocked_until = 0.0
        self.rate_limit_events = 0
        self.throttle_seconds = 0.0

    def configure(self, ceiling: int) -> None:
        with self._condition:
            self._ceiling = max(1, int(ceiling or 1))
            self._limit = self._ceiling
            self._condition.notify_all()

    def acquire(self, timeout: float | None = None) -> None:
        request_deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            with self._condition:
                now = time.monotonic()
                request_remaining = None if request_deadline is None else request_deadline - now
                if request_remaining is not None and request_remaining <= 0:
                    raise TimeoutError("embedding rate gate exceeded the request deadline")
                cooldown = self._blocked_until - now
                if cooldown <= 0 and self._active < self._limit:
                    self._active += 1
                    return
                wait = cooldown if cooldown > 0 else 0.1
                if request_remaining is not None:
                    wait = min(wait, request_remaining)
                self._condition.wait(timeout=max(0.001, wait))

    def succeeded(self) -> None:
        with self._condition:
            self._active = max(0, self._active - 1)
            self._successes += 1
            if self._limit < self._ceiling and self._successes >= self._limit * 4:
                self._limit += 1
                self._successes = 0
            self._condition.notify_all()

    def failed(self) -> None:
        with self._condition:
            self._active = max(0, self._active - 1)
            self._condition.notify_all()

    def throttled(self, wait: float) -> float:
        with self._condition:
            self._active = max(0, self._active - 1)
            self._limit = max(1, self._limit // 2)
            self._successes = 0
            self.rate_limit_events += 1
            self.throttle_seconds += max(0.0, wait)
            deadline = time.monotonic() + max(0.0, wait)
            self._blocked_until = max(self._blocked_until, deadline)
            self._condition.notify_all()
            return deadline

    def finish_cooldown(self, deadline: float) -> None:
        with self._condition:
            if self._blocked_until <= deadline:
                self._blocked_until = 0.0
            self._condition.notify_all()

    @property
    def concurrency(self) -> int:
        with self._condition:
            return self._limit


class OpenAISpecEmbedder:
    """OpenAI-spec HTTP embedding client (stdlib urllib, no SDK).

    Wire format: ``POST {base_url}/embeddings`` with body
    ``{"model": ..., "input": [...]}`` (+ optional ``dimensions``);
    response embeddings are read from ``data[i].embedding`` ordered by
    ``data[i].index``. The API key is read from the environment variable
    named by ``api_key_env`` at call time and never persisted.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key_env: str = "OPENAI_API_KEY",
        dimensions: int | None = None,
        batch_size: int = 64,
        timeout: int = 30,
        max_retries: int = 4,
        retry_backoff_seconds: float = 1.0,
        rate_limit_max_wait_seconds: float = 300.0,
    ):
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._base64 = urlsplit(self.base_url).hostname == "api.openai.com"
        self.model = model
        self.api_key_env = api_key_env
        self.dimensions = int(dimensions) if dimensions else None
        self.batch_size = max(1, int(batch_size or 64))
        self.timeout = timeout
        # Bounded retry on transient transport failures — see _post_with_retry.
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = max(0.1, float(retry_backoff_seconds))
        self.rate_limit_max_wait_seconds = max(0.0, float(rate_limit_max_wait_seconds))
        self.calls = 0
        self.texts_embedded = 0
        self._counter_lock = threading.Lock()
        self._rate_gate = _AdaptiveRateGate()

    def configure_parallelism(self, ceiling: int) -> None:
        """Share the indexer's requested concurrency across all HTTP threads."""

        self._rate_gate.configure(ceiling)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(texts[start : start + self.batch_size]))
        return vectors

    def _post_with_retry(self, request: Request) -> dict[str, Any]:
        """POST with bounded exponential backoff on *transient* failures.

        Indexing a large corpus is hundreds of HTTPS calls — 12,667 files of
        microsoft/vscode took over 200 — and without this a single flaky one
        aborts the whole sync. That is not hypothetical: a real run died ~45
        minutes in on

            ssl.SSLError: [SSL: SSLV3_ALERT_BAD_RECORD_MAC]

        which is a corrupted TLS record, i.e. precisely the sort of thing that
        succeeds on the next attempt. Resuming is cheap (the sha256 pre-read
        skip means unchanged files are not re-embedded), but a multi-hour index
        should not need a human to notice and restart it.

        Only *transient* conditions are retried. A 401 is a wrong key and a 400
        is a malformed request; retrying either burns time and money to fail
        the same way, so both surface immediately.
        """
        delay = self.retry_backoff_seconds
        transient_attempt = 0
        rate_limit_attempt = 0
        rate_limit_waited = 0.0
        last: Exception | None = None
        from pheasant.request_budget import DeadlineExceeded, remaining_seconds

        def sleep_with_budget(seconds: float) -> None:
            remaining = remaining_seconds()
            if remaining is None:
                time.sleep(seconds)
                return
            if remaining <= 0:
                raise DeadlineExceeded("assistant request deadline exceeded")
            if seconds >= remaining:
                time.sleep(remaining)
                raise DeadlineExceeded("assistant request deadline exceeded")
            time.sleep(seconds)

        while True:
            remaining = remaining_seconds()
            if remaining is not None and remaining <= 0:
                raise DeadlineExceeded("assistant request deadline exceeded")
            try:
                self._rate_gate.acquire(timeout=remaining)
            except TimeoutError as exc:
                if remaining_seconds() is not None and (remaining_seconds() or 0) <= 0:
                    raise DeadlineExceeded("assistant request deadline exceeded") from exc
                raise
            try:
                remaining = remaining_seconds()
                if remaining is not None and remaining <= 0:
                    raise DeadlineExceeded("assistant request deadline exceeded")
                timeout = self.timeout if remaining is None else min(self.timeout, remaining)
                _record_embedding_provider_request()
                payload = json.loads(_transport(request, timeout).decode("utf-8"))
                if remaining_seconds() is not None and (remaining_seconds() or 0) <= 0:
                    raise DeadlineExceeded("assistant request deadline exceeded")
            except HTTPError as exc:  # noqa: PERF203 - retry loop
                if remaining_seconds() is not None and (remaining_seconds() or 0) <= 0:
                    self._rate_gate.failed()
                    raise DeadlineExceeded("assistant request deadline exceeded") from exc
                headers = exc.headers
                if exc.code == 429:
                    retry_after = headers.get("Retry-After") if headers else None
                    resets = [
                        value
                        for value in (
                            _reset_after_seconds(
                                headers.get("x-ratelimit-reset-requests") if headers else None
                            ),
                            _reset_after_seconds(
                                headers.get("x-ratelimit-reset-tokens") if headers else None
                            ),
                        )
                        if value is not None
                    ]
                    fallback = random.uniform(delay * 0.75, delay * 1.25)
                    wait = max([_retry_after_seconds(retry_after, fallback), *resets])
                    rate_limit_attempt += 1
                    budget_exhausted = (
                        not self.rate_limit_max_wait_seconds
                        or rate_limit_waited + wait > self.rate_limit_max_wait_seconds
                        or rate_limit_attempt > 100
                    )
                    if not budget_exhausted:
                        _record_embedding_provider_retry()
                    deadline = self._rate_gate.throttled(0.0 if budget_exhausted else wait)
                    if budget_exhausted:
                        raise
                    rate_limit_waited += wait
                    last = exc
                    request_id = headers.get("x-request-id") if headers else None
                    logger.warning(
                        "embedding provider throttled model=%s request_id=%s; shared cooldown "
                        "%.1fs, concurrency=%d [rate-limit %d, %.1fs cumulative]",
                        self.model,
                        request_id or "unknown",
                        wait,
                        self._rate_gate.concurrency,
                        rate_limit_attempt,
                        rate_limit_waited,
                    )
                    sleep_with_budget(wait)
                    self._rate_gate.finish_cooldown(deadline)
                    delay = min(delay * 2, _MAX_BACKOFF_SECONDS)
                    continue
                self._rate_gate.failed()
                if exc.code not in _RETRYABLE_STATUS or transient_attempt >= self.max_retries:
                    raise
                _record_embedding_provider_retry()
                last = exc
                header = exc.headers.get("Retry-After") if exc.headers else None
                wait = _retry_after_seconds(header, delay)
            except _TRANSIENT_ERRORS as exc:
                self._rate_gate.failed()
                if remaining_seconds() is not None and (remaining_seconds() or 0) <= 0:
                    raise DeadlineExceeded("assistant request deadline exceeded") from exc
                if transient_attempt >= self.max_retries:
                    raise
                _record_embedding_provider_retry()
                last = exc
                wait = random.uniform(delay * 0.75, delay * 1.25)
            except BaseException:
                self._rate_gate.failed()
                raise
            else:
                self._rate_gate.succeeded()
                return payload
            transient_attempt += 1
            logger.warning(
                "embedding request failed (%s), retrying in %.1fs [%d/%d]",
                type(last).__name__,
                wait,
                transient_attempt,
                self.max_retries,
            )
            sleep_with_budget(wait)
            delay = min(delay * 2, _MAX_BACKOFF_SECONDS)

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        body: dict[str, Any] = {"model": self.model, "input": list(batch)}
        if self.dimensions:
            body["dimensions"] = self.dimensions
        if self._base64:
            # A quarter of the bytes and no float parsing: 263 -> 208 ms p50 for
            # one query from the lab. Only where the provider is known to take it;
            # a compatible server that rejects an unknown field would 400 forever.
            body["encoding_format"] = "base64"
        headers = {"Content-Type": "application/json", "User-Agent": "pheasant/0.1"}
        api_key = os.environ.get(self.api_key_env or "", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = Request(
            f"{self.base_url}/embeddings",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        payload = self._post_with_retry(request)
        with self._counter_lock:
            self.calls += 1
            self.texts_embedded += len(batch)
        data = sorted(payload.get("data", []), key=lambda item: int(item.get("index", 0)))
        if len(data) != len(batch):
            raise ValueError(
                f"Embedding endpoint returned {len(data)} embeddings for {len(batch)} inputs"
            )
        return [_embedding_values(item) for item in data]
