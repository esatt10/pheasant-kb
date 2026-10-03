"""Per-region vector self-search (Synapse step 21.4).

Embeddings are optional and disabled by default; when ``search.embeddings``
is enabled, :class:`~pheasant.sync.engine.SyncEngine` embeds chunks at sync
time and ``HybridSearch`` gains ``mode="vector"`` candidates.

On-disk layout
--------------
Vectors live under ``<vector_store.path>/<kb_id>/`` (default
``<state>/vectors/<kb_id>/``). The directory is created only when
embeddings are enabled, so a default-configured region never grows a
vector store. Two backends implement :class:`VectorStore`:

- ``NumpyVectorStore`` — always available (numpy is a core dependency).
  A single flat JSON file ``index.json`` maps each chunk id to a
  base64-encoded little-endian float32 vector plus a small payload dict
  (``source_id`` / ``artifact_id`` / ``text_hash``). Writes are durable
  via tmp + fsync + atomic rename. This is the test backend.
- ``LanceDBVectorStore`` — the production default
  (``search.vector_store.provider: lancedb``); lazily imports ``lancedb``
  and raises an actionable hint to ``pip install 'pheasant-kb[vector]'``
  when the optional extra is missing.

Idempotency bookkeeping
-----------------------
Chunk ids are content-addressed (they embed the chunk ``text_hash``), so
"has this exact text already been embedded?" is exactly vector-store
membership of the chunk id. :class:`VectorIndexer` embeds only ids that
are missing from the store and prunes ids no longer present in the
``chunks`` table at the end of each sync — re-syncing unchanged content
(incremental *or* full) therefore performs zero embedder calls.

Embedders
---------
``OpenAISpecEmbedder`` speaks the standard OpenAI embeddings HTTP shape
(``POST {base_url}/embeddings`` with ``{"model": ..., "input": [...]}``,
response ``data[i].embedding``) — the same wire format the
pheasant-flock router's embedding provider uses, so a Synapse fleet
can pin one model for both repos. ``StubEmbedder`` is the deterministic
offline path: each lowercase token hashes (blake2b) to a fixed unit
direction and a text embeds to the normalized sum of its token
directions; a small built-in synonym table canonicalizes tokens first
(e.g. ``automobile -> car``) so tests can surface lexically-absent
matches without any model or network.
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import importlib.util
import json
import logging
import os
import random
import re
import socket
import ssl
import struct
import threading
import time
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np

from pheasant.search.ranking import DEFAULT_FILTER_OVERFETCH
from pheasant.search.sqlite_store import _row_result
from pheasant.search.vector_indexer import VectorIndexer

if TYPE_CHECKING:
    from pheasant.config.schema import EmbeddingsSettings, PheasantConfig
    from pheasant.persistence.state_store import StateStore

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


# Planted synonym groups for the deterministic stub embedder. Tokens that
# canonicalize to the same key share an embedding direction, which is how
# offline tests exercise "semantic" retrieval of lexically-absent terms.
DEFAULT_STUB_SYNONYMS: dict[str, str] = {
    "automobile": "car",
    "vehicle": "car",
    "physician": "doctor",
    "clinician": "doctor",
    "k8s": "kubernetes",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@runtime_checkable
class Embedder(Protocol):
    """Batch text -> vector provider. ``calls`` counts transport batches."""

    model: str
    calls: int
    texts_embedded: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class VectorStore(Protocol):
    """Durable chunk-id keyed vector index."""

    def upsert(
        self,
        chunk_ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
    ) -> None: ...

    def delete(
        self,
        chunk_ids: list[str] | None = None,
        artifact_id: str | None = None,
    ) -> int: ...

    def search(self, query_vec: list[float], k: int) -> list[tuple[str, float, dict[str, Any]]]:
        """Return ``(chunk_id, cosine_similarity, payload)`` best-first."""
        ...

    def count(self) -> int: ...

    def dimensions(self) -> int | None:
        """Stored vector width without materializing the vector index."""
        ...

    def existing_ids(self, chunk_ids: list[str]) -> set[str]: ...

    def source_chunk_ids(self, source_id: str) -> set[str]: ...

    def reset(self) -> int:
        """Remove every vector and any backend schema tied to its dimensions."""
        ...

    def flush(self) -> None:
        """Persist any writes a backend may have deferred. Backends that
        already write durably on every `upsert`/`delete` (e.g. LanceDB) make
        this a no-op; callers must still call it at the end of a sync to
        guarantee durability for backends that don't (e.g. `NumpyVectorStore`,
        see its docstring)."""
        ...


class StubEmbedder:
    """Deterministic, offline embedder for tests and demos.

    Each token's direction is derived per-dimension from
    ``blake2b("{token}:{i}")`` so vectors are stable across processes and
    platforms; synonyms map onto a shared canonical token before hashing.
    """

    def __init__(
        self,
        dim: int = 64,
        model: str = "stub-embed",
        synonyms: dict[str, str] | None = None,
    ):
        self.dim = max(8, int(dim or 64))
        self.model = model
        self.synonyms = dict(DEFAULT_STUB_SYNONYMS if synonyms is None else synonyms)
        self.calls = 0
        self.texts_embedded = 0
        self._counter_lock = threading.Lock()
        self._directions: dict[str, np.ndarray] = {}

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self._counter_lock:
            self.calls += 1
            self.texts_embedded += len(texts)
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = np.zeros(self.dim, dtype=np.float64)
        tokens = _TOKEN_RE.findall((text or "").lower())
        for token in tokens:
            vector += self._direction(self.synonyms.get(token, token))
        norm = float(np.linalg.norm(vector))
        if norm > 0.0:
            vector /= norm
        return [float(value) for value in vector]

    def _direction(self, token: str) -> np.ndarray:
        cached = self._directions.get(token)
        if cached is not None:
            return cached
        values = []
        for i in range(self.dim):
            digest = hashlib.blake2b(f"{token}:{i}".encode(), digest_size=8).digest()
            values.append(int.from_bytes(digest, "big") / 2**63 - 1.0)
        direction = np.array(values, dtype=np.float64)
        direction /= float(np.linalg.norm(direction)) or 1.0
        self._directions[token] = direction
        return direction


#: HTTP statuses worth trying again: overload, rate limiting, and the gateway
#: family. Explicitly *not* 400/401/403/404 — a malformed request or a wrong
#: key fails identically on every retry, so retrying only spends time.
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: Transport-level failures. `ssl.SSLError` is first because it is the one a
#: real 12k-file index actually died on.
_TRANSIENT_ERRORS = (ssl.SSLError, URLError, TimeoutError, ConnectionError, socket.timeout)

#: Ceiling on locally invented backoff. A provider's explicit Retry-After is
#: intentionally not capped: waking before its quota resets causes another 429.
_MAX_BACKOFF_SECONDS = 30.0


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
                with urlopen(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
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
        return [[float(value) for value in item["embedding"]] for item in data]


class NumpyVectorStore:
    """Always-available flat-file backend (also the offline test backend).

    ``index.json`` holds every vector in the knowledge base as one JSON
    object, so a write is always "read the whole thing, merge, write the
    whole thing back" — there is no incremental/append path, unlike a real
    embedded database (`LanceDBVectorStore`). Writing on *every*
    `upsert()` call (one per artifact, i.e. once per file — see
    `VectorIndexer.index_artifact`) is therefore O(n^2) in the number of
    artifacts once the index is non-trivially large: each of N files pays a
    full rewrite of an index that has grown to include the previous N-1.
    Measured live indexing a second large source into an already-large
    shared index (both sources share one index per knowledge base): over
    100GB written for a ~150MB final file, entirely from this pattern.
    `upsert`/`delete` now buffer in memory and flush to disk on the same
    self-throttled schedule `SyncEngine._maybe_checkpoint` already uses for
    the graph — `flush()` forces a save and callers (`SyncEngine`) MUST call
    it at the end of a sync to guarantee durability for whatever hasn't hit
    the periodic threshold yet.
    """

    def __init__(self, directory: str | Path, *, flush_interval_seconds: float = 20.0):
        self.directory = Path(directory)
        self.path = self.directory / "index.json"
        self._cache: dict[str, dict[str, Any]] | None = None
        self._cache_sig: tuple[int, int] | None = None
        # Bumped on every *content* change to `_cache` — an upsert/delete
        # (flushed or not) or a disk re-read that found different content.
        # `_cache_sig` (the on-disk file's mtime+size) does NOT change while
        # a write is buffered in memory, so it cannot be used to invalidate
        # the decoded-matrix cache below; this can.
        self._version = 0
        self._dirty = False
        self._flush_interval = max(1.0, float(flush_interval_seconds))
        self._last_flush = time.monotonic()
        self._last_flush_seconds = 0.0
        # Decoded (ids, matrix, norms) built from `_cache`. Every vector is
        # stored base64-encoded, and decoding it is a pure-Python loop
        # (base64.b64decode + struct.unpack per item) that does not release
        # the GIL between iterations -- so without caching the *decoded*
        # form, every concurrent search() call redid the full decode from
        # scratch, and the GIL serialized that CPU-bound work across
        # threads regardless of how many search() calls ran "concurrently".
        # Measured: an agentic retrieve step fanning out 4 concurrent
        # searches against a 7,463-chunk index took 21-29s (~5-7s each) --
        # almost entirely this decode, not the embedding API call or the
        # actual similarity math (a real numpy matmul over an already-
        # decoded matrix is fast). Keyed off `_version`, so it invalidates
        # on any content change, flushed to disk or still buffered.
        self._matrix_cache: tuple[list[str], Any, Any] | None = None
        self._matrix_sig: int | None = None
        self._matrix_lock = threading.Lock()

    # -- persistence -------------------------------------------------

    def _signature(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _items(self) -> dict[str, dict[str, Any]]:
        if self._dirty:
            # Buffered writes are ahead of disk (or disk has nothing yet) —
            # re-reading here would silently discard them.
            return self._cache if self._cache is not None else {}
        signature = self._signature()
        if signature is None:
            self._cache, self._cache_sig = {}, None
            return {}
        if self._cache is None or signature != self._cache_sig:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self._cache = payload.get("items", {})
            self._cache_sig = signature
            self._version += 1
        return self._cache

    def _save(self, items: dict[str, dict[str, Any]]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump({"format": "pheasant-vectors-v1", "items": items}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        self._cache = items
        self._cache_sig = self._signature()

    def _maybe_flush(self, *, force: bool = False) -> None:
        if not self._dirty:
            return
        # Self-throttling, same shape as SyncEngine._maybe_checkpoint: a
        # save's cost scales with index size (the whole thing is rewritten
        # every time), so spacing flushes at ~10x the last save's duration
        # keeps their overhead a bounded fraction of the sync no matter how
        # large the index gets, instead of a full rewrite per artifact.
        interval = max(self._flush_interval, self._last_flush_seconds * 10)
        if not force and time.monotonic() - self._last_flush < interval:
            return
        started = time.monotonic()
        self._save(self._cache or {})
        self._last_flush_seconds = time.monotonic() - started
        self._last_flush = time.monotonic()
        self._dirty = False

    def flush(self) -> None:
        self._maybe_flush(force=True)

    @staticmethod
    def _encode(vector: list[float]) -> str:
        return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")

    @staticmethod
    def _decode(blob: str) -> np.ndarray:
        raw = base64.b64decode(blob.encode("ascii"))
        return np.frombuffer(raw, dtype="<f4").astype(np.float64)

    # -- VectorStore protocol ----------------------------------------

    def upsert(
        self,
        chunk_ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
    ) -> None:
        items = dict(self._items())
        for chunk_id, vector, payload in zip(chunk_ids, vectors, payloads, strict=True):
            items[chunk_id] = {"v": self._encode(vector), "payload": payload}
        self._cache = items
        self._version += 1
        self._dirty = True
        self._maybe_flush()

    def delete(
        self,
        chunk_ids: list[str] | None = None,
        artifact_id: str | None = None,
    ) -> int:
        items = dict(self._items())
        doomed = set(chunk_ids or [])
        if artifact_id is not None:
            doomed.update(
                chunk_id
                for chunk_id, item in items.items()
                if item.get("payload", {}).get("artifact_id") == artifact_id
            )
        removed = 0
        for chunk_id in doomed:
            if items.pop(chunk_id, None) is not None:
                removed += 1
        if removed:
            self._cache = items
            self._version += 1
            self._dirty = True
            self._maybe_flush()
        return removed

    def _decoded_matrix(self) -> tuple[list[str], Any, Any] | None:
        """The whole index as ``(ids, matrix, norms)``, decoded once per file version.

        `_items()` already avoids re-reading the file when it hasn't changed;
        this avoids redoing the (much more expensive) per-vector decode and
        the full-matrix norm computation on every call. The check-then-build
        is locked so N threads racing in on a cold/stale cache build it once,
        not N times; once built, `self._matrix_cache` is only ever replaced by
        a fresh atomic assignment (never mutated in place), so reads of an
        already-built cache need no lock.
        """

        items = self._items()
        if not items:
            return None
        cached = self._matrix_cache
        if cached is not None and self._matrix_sig == self._version:
            return cached
        with self._matrix_lock:
            # Re-check: another thread may have just finished building it
            # while this one was waiting for the lock.
            if self._matrix_cache is not None and self._matrix_sig == self._version:
                return self._matrix_cache
            ids = list(items)
            matrix = np.stack([self._decode(items[chunk_id]["v"]) for chunk_id in ids])
            norms = np.linalg.norm(matrix, axis=1)
            norms[norms == 0.0] = 1.0
            built = (ids, matrix, norms)
            self._matrix_cache = built
            self._matrix_sig = self._version
            return built

    def search(self, query_vec: list[float], k: int) -> list[tuple[str, float, dict[str, Any]]]:
        decoded = self._decoded_matrix()
        if decoded is None or k < 1:
            return []
        ids, matrix, norms = decoded
        query = np.asarray(query_vec, dtype=np.float64)
        query_norm = float(np.linalg.norm(query))
        if query_norm == 0.0:
            return []
        if matrix.shape[1] != query.shape[0]:
            raise ValueError(
                f"Vector dimension mismatch: index has {matrix.shape[1]}, query has "
                f"{query.shape[0]}. Re-sync with mode=full after changing embedding settings."
            )
        similarities = (matrix @ query) / (norms * query_norm)
        order = np.argsort(-similarities)[:k]
        # A second `_items()` call: writes to this store only ever happen
        # from the separate sync-worker process (never from this one), so an
        # id vanishing between the two calls in this process is not possible
        # in practice -- `.get()` here is defensive-in-depth, not a race this
        # process can trigger on its own.
        items = self._items()
        return [
            (ids[i], float(similarities[i]), dict(items.get(ids[i], {}).get("payload", {})))
            for i in order
        ]

    def count(self) -> int:
        return len(self._items())

    def dimensions(self) -> int | None:
        items = self._items()
        if not items:
            return None
        blob = str(next(iter(items.values())).get("v") or "")
        if not blob:
            return None
        # Vectors are packed float32 values. Inspecting the encoded byte width
        # avoids decoding every vector merely to render the settings page.
        return len(base64.b64decode(blob.encode("ascii"))) // 4

    def existing_ids(self, chunk_ids: list[str]) -> set[str]:
        items = self._items()
        return {chunk_id for chunk_id in chunk_ids if chunk_id in items}

    def source_chunk_ids(self, source_id: str) -> set[str]:
        return {
            chunk_id
            for chunk_id, item in self._items().items()
            if item.get("payload", {}).get("source_id") == source_id
        }

    def reset(self) -> int:
        """Clear the regenerable index and its decoded-matrix cache."""

        removed = len(self._items())
        self._cache = {}
        self._version += 1
        self._dirty = True
        self._matrix_cache = None
        self._matrix_sig = None
        self._maybe_flush(force=True)
        return removed

    def all_vectors(self) -> list[tuple[str, list[float]]]:
        """Bulk (chunk_id, vector) reader used by the contract publisher."""

        return [
            (chunk_id, list(self._decode(item["v"]))) for chunk_id, item in self._items().items()
        ]


class LanceDBVectorStore:
    """LanceDB-backed store (optional ``[vector]`` extra)."""

    TABLE = "chunks"

    #: Small fragments at which `flush` compacts. Every ``add`` is a new
    #: fragment and nothing ever merged them, so search cost grew with the
    #: number of writes rather than the number of vectors: measured on 20,000
    #: 256-d vectors, 56.8ms p50 at 168 fragments against 32.5ms after one
    #: compaction. Compaction rewrites every fragment under Lance's target
    #: size -- the whole table, for any region under ~1M vectors -- so it is
    #: gated here rather than run per sync, where an O(one file) change would
    #: pay an O(total) rewrite. A first index of a large corpus crosses it
    #: straight away, which is where the fragment count is worst.
    COMPACT_AT_SMALL_FRAGMENTS = 64
    #: Keep the first request off cold file pages for a small index. Above
    #: this cap, eagerly scanning the whole corpus at API startup costs more
    #: than letting the first real query warm the OS page cache.
    WARMUP_MAX_VECTORS = 10_000

    #: Version manifests older than this are dropped when compacting. Nothing
    #: here reads an old version: every call re-opens the table at its latest.
    #: Data files keep Lance's own rule -- not deleted while younger than
    #: seven days, since they could belong to another writer's uncommitted
    #: transaction -- so the space a compaction frees comes back a week later
    #: rather than never.
    CLEANUP_OLDER_THAN = timedelta(hours=1)

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self._db = None
        # Membership is queried once per artifact by ``VectorIndexer``.  A
        # Lance table scan there makes a full index O(files * vectors), which
        # becomes the dominant cost long before embedding does.  One
        # coordinator owns vector writes, so keep its chunk-id membership in
        # memory and update it with every successful mutation.
        self._id_cache: set[str] | None = None
        self._id_cache_lock = threading.RLock()
        #: Set by every write, cleared when `flush` has decided whether to
        #: compact, so an unchanged sync does not even read the table stats.
        self._mutated = False

    def _database(self):
        if self._db is None:
            try:
                import lancedb
            except ModuleNotFoundError as exc:  # pragma: no cover - exercised w/o extra
                raise ModuleNotFoundError(
                    "search.vector_store.provider='lancedb' requires the optional "
                    "extra: pip install 'pheasant-kb[vector]'"
                ) from exc
            self.directory.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(self.directory))
        return self._db

    def _table(self):
        db = self._database()
        if self.TABLE not in db.table_names():
            return None
        return db.open_table(self.TABLE)

    @staticmethod
    def _quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def _rows(self, columns: list[str], where: str | None = None) -> list[dict[str, Any]]:
        """Read ``columns`` of every row (matching ``where``), projected.

        ``to_arrow()`` materializes every column -- the vectors included --
        before ``select`` discards them, so asking for chunk ids read the
        whole table. A projected scan with the predicate pushed down reads
        only what it returns: 17.5ms against 67.0ms for one source's ids out
        of 20,000 256-d vectors. The old path stays as the fallback for a
        LanceDB whose query builder lacks any of these calls.
        """

        table = self._table()
        if table is None:
            return []
        try:
            query = table.search().select(columns)
            if where is not None:
                query = query.where(where)
            return query.limit(None).to_arrow().select(columns).to_pylist()
        except (AttributeError, TypeError, NotImplementedError):
            if where is not None:
                raise  # the caller filters the unprojected rows itself
            return table.to_arrow().select(columns).to_pylist()

    # -- VectorStore protocol ----------------------------------------

    def upsert(
        self,
        chunk_ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
    ) -> None:
        if not chunk_ids:
            return
        rows = [
            {
                "chunk_id": chunk_id,
                "vector": [float(value) for value in vector],
                "source_id": str(payload.get("source_id") or ""),
                "artifact_id": str(payload.get("artifact_id") or ""),
                "payload_json": json.dumps(payload, sort_keys=True, default=str),
            }
            for chunk_id, vector, payload in zip(chunk_ids, vectors, payloads, strict=True)
        ]
        self._mutated = True
        table = self._table()
        if table is None:
            self._database().create_table(self.TABLE, data=rows)
            with self._id_cache_lock:
                self._id_cache = set(chunk_ids)
            return
        # Content-addressed ids are overwhelmingly new during a sync.  Lance
        # creates a new table version for delete(), so only pay that price for
        # ids that actually replace existing rows.
        replaced = self.existing_ids(chunk_ids)
        if replaced:
            self.delete(chunk_ids=sorted(replaced))
        table.add(rows)
        with self._id_cache_lock:
            if self._id_cache is None:
                self._id_cache = set(chunk_ids)
            else:
                self._id_cache.update(chunk_ids)

    def delete(
        self,
        chunk_ids: list[str] | None = None,
        artifact_id: str | None = None,
    ) -> int:
        table = self._table()
        if table is None:
            return 0
        self._mutated = True
        before = table.count_rows()
        ids = list(chunk_ids or [])
        for start in range(0, len(ids), 500):
            quoted = ", ".join(self._quote(chunk_id) for chunk_id in ids[start : start + 500])
            table.delete(f"chunk_id IN ({quoted})")
        if artifact_id is not None:
            table.delete(f"artifact_id = {self._quote(artifact_id)}")
        removed = before - table.count_rows()
        with self._id_cache_lock:
            if artifact_id is not None:
                # The artifact predicate can remove ids the caller did not
                # enumerate; reload membership lazily on the next lookup.
                self._id_cache = None
            elif self._id_cache is not None:
                self._id_cache.difference_update(ids)
        return removed

    def search(self, query_vec: list[float], k: int) -> list[tuple[str, float, dict[str, Any]]]:
        table = self._table()
        if table is None or k < 1:
            return []
        hits = (
            table.search([float(value) for value in query_vec])
            .distance_type("cosine")
            .limit(k)
            .to_list()
        )
        return [
            (
                hit["chunk_id"],
                1.0 - float(hit.get("_distance") or 0.0),
                json.loads(hit.get("payload_json") or "{}"),
            )
            for hit in hits
        ]

    def warm(self) -> bool:
        """Touch a small index once so its first user query avoids cold I/O."""
        table = self._table()
        if table is None:
            return False
        rows = table.count_rows()
        if rows <= 0 or rows > self.WARMUP_MAX_VECTORS:
            return False
        width = getattr(table.schema.field("vector").type, "list_size", None)
        if not width:
            return False
        probe = [0.0] * int(width)
        probe[0] = 1.0
        table.search(probe).distance_type("cosine").limit(1).to_list()
        return True

    def count(self) -> int:
        table = self._table()
        return 0 if table is None else table.count_rows()

    def dimensions(self) -> int | None:
        table = self._table()
        if table is None:
            return None
        vector_type = table.schema.field("vector").type
        width = getattr(vector_type, "list_size", None)
        return int(width) if width is not None else None

    def existing_ids(self, chunk_ids: list[str]) -> set[str]:
        with self._id_cache_lock:
            if self._id_cache is None:
                self._id_cache = {row["chunk_id"] for row in self._rows(["chunk_id"])}
            return set(chunk_ids).intersection(self._id_cache)

    def source_chunk_ids(self, source_id: str) -> set[str]:
        """One source's ids. Runs for every source at the end of every sync,
        changed or not, which is why the filter is pushed into the scan."""

        try:
            rows = self._rows(["chunk_id"], where=f"source_id = {self._quote(source_id)}")
        except (AttributeError, TypeError, NotImplementedError):
            return {
                row["chunk_id"]
                for row in self._rows(["chunk_id", "source_id"])
                if row["source_id"] == source_id
            }
        return {row["chunk_id"] for row in rows}

    def reset(self) -> int:
        """Drop the table so the next insert can establish a new vector width.

        Deleting every row is insufficient for LanceDB: an empty table keeps
        its Arrow ``FixedSizeList`` schema, so switching from a 1,536- to a
        3,072-dimensional model still fails on the first new insert.
        """

        table = self._table()
        if table is None:
            return 0
        removed = table.count_rows()
        self._database().drop_table(self.TABLE)
        with self._id_cache_lock:
            self._id_cache = set()
        return removed

    def flush(self) -> None:
        """`upsert`/`delete` already write through, so nothing is buffered.

        This is the end-of-sync hook, and the one place compaction can run
        without costing a request or a commit: see
        `COMPACT_AT_SMALL_FRAGMENTS`. Maintenance, never correctness -- a
        failure is logged and the sync carries on.
        """

        if not self._mutated:
            return
        self._mutated = False
        table = self._table()
        if table is None:
            return
        try:
            stats = table.stats()
            fragments = (stats.get("fragment_stats") or {}) if isinstance(stats, dict) else {}
            small = int(fragments.get("num_small_fragments") or 0)
            if small < self.COMPACT_AT_SMALL_FRAGMENTS:
                return
            started = time.monotonic()
            table.optimize(cleanup_older_than=self.CLEANUP_OLDER_THAN)
            logger.info(
                "Compacted the vector store: %d small fragments in %.2fs",
                small,
                time.monotonic() - started,
            )
        except Exception:  # noqa: BLE001 - maintenance must not fail a sync
            logger.warning("Vector store compaction failed; continuing", exc_info=True)

    def all_vectors(self) -> list[tuple[str, list[float]]]:
        """Bulk (chunk_id, vector) reader used by the contract publisher."""

        return [
            (row["chunk_id"], [float(value) for value in row["vector"]])
            for row in self._rows(["chunk_id", "vector"])
        ]


class VectorSearcher:
    """Query-time vector candidates in the SearchStore result shape."""

    #: Recent query embeddings, newest last. A vector query spends most of its
    #: time waiting on the embedding provider, and the same question gets asked
    #: repeatedly — re-running a search, an agent loop retrying with the same
    #: sub-query, a user refining one word. Small and per-process on purpose:
    #: this is a latency cache, not a store.
    _QUERY_CACHE_SIZE = 256
    _QUERY_INFLIGHT_MAX = 256

    def __init__(self, embedder: Embedder, store: VectorStore, state: StateStore):
        self.embedder = embedder
        self.store = store
        self.state = state
        self._query_cache: dict[tuple, list[float]] = {}
        self._query_inflight: dict[tuple, Future[list[float]]] = {}
        self._query_cache_lock = threading.RLock()

    def embed_query(self, query: str) -> list[float]:
        """Embed a query, reusing a recent identical one."""

        return self.embed_queries([query])[0]

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        """Embed several queries in one provider request when possible.

        Agentic retrieval often plans multiple queries before searching. The
        vector scans remain sequential because they contend for the GIL, but
        their network-bound embeddings can share one provider round trip.
        """
        started = time.perf_counter()
        try:
            return self._embed_query_batch(queries)
        finally:
            from pheasant.request_budget import record_active_timing

            record_active_timing(
                "query_embedding",
                time.perf_counter() - started,
                query_count=len(queries),
            )

    def _embed_query_batch(self, queries: list[str]) -> list[list[float]]:
        if not queries:
            return []

        unique = list(dict.fromkeys(queries))
        model = str(getattr(self.embedder, "model", type(self.embedder).__name__))
        provider = str(getattr(self.embedder, "provider", type(self.embedder).__name__))
        base_url = str(getattr(self.embedder, "base_url", ""))
        dimensions = getattr(self.embedder, "dimensions", getattr(self.embedder, "dim", None))
        keys = {query: (provider, base_url, model, query, dimensions) for query in unique}
        metrics = _ACTIVE_QUERY_EMBEDDING_METRICS.get()
        if metrics is not None:
            prior = next(
                (metrics.prior_failure(keys[q]) for q in unique if metrics.prior_failure(keys[q])),
                None,
            )
            if prior is not None:
                raise prior
        with self._query_cache_lock:
            vectors = {
                query: self._query_cache[keys[query]]
                for query in unique
                if keys[query] in self._query_cache
            }
            owners: list[str] = []
            waiting: dict[str, Future[list[float]]] = {}
            untracked_owners: set[str] = set()
            for query in unique:
                if query in vectors:
                    continue
                future = self._query_inflight.get(keys[query])
                if future is None:
                    if len(self._query_inflight) < self._QUERY_INFLIGHT_MAX:
                        future = Future()
                        self._query_inflight[keys[query]] = future
                    else:
                        untracked_owners.add(query)
                    owners.append(query)
                else:
                    waiting[query] = future
        if metrics is not None:
            metrics.add("cache_hits", len(vectors))
            metrics.add("fresh_misses", len(owners))
            metrics.add("singleflight_waits", len(waiting))
        if owners:
            started = time.perf_counter()
            try:
                embedded = self.embedder.embed(owners)
                if len(embedded) != len(owners):
                    raise ValueError(
                        "Embedding provider returned an unexpected number of query vectors"
                    )
                with self._query_cache_lock:
                    for query, vector in zip(owners, embedded, strict=True):
                        key = keys[query]
                        if len(self._query_cache) >= self._QUERY_CACHE_SIZE:
                            self._query_cache.pop(next(iter(self._query_cache)), None)
                        self._query_cache[key] = vector
                        vectors[query] = vector
                        future = self._query_inflight.pop(key, None)
                        if future is not None:
                            future.set_result(vector)
            except BaseException as exc:
                if metrics is not None:
                    metrics.add("failed", len(owners))
                    metrics.remember_failure([keys[query] for query in owners], exc)
                with self._query_cache_lock:
                    for query in owners:
                        future = self._query_inflight.pop(keys[query], None)
                        if future is not None:
                            future.set_exception(exc)
                raise
            finally:
                if metrics is not None:
                    metrics.add("elapsed_seconds", time.perf_counter() - started)
        wait_timeout = float(getattr(self.embedder, "timeout", 30.0) or 30.0)
        for query, future in waiting.items():
            from concurrent.futures import TimeoutError as FutureTimeoutError

            from pheasant.request_budget import DeadlineExceeded, remaining_seconds

            remaining = remaining_seconds()
            timeout = wait_timeout if remaining is None else min(wait_timeout, remaining)
            if timeout <= 0:
                raise DeadlineExceeded("assistant request deadline exceeded")
            try:
                vectors[query] = future.result(timeout=timeout)
            except FutureTimeoutError as exc:
                remaining = remaining_seconds()
                if remaining is not None and remaining <= 0:
                    raise DeadlineExceeded("assistant request deadline exceeded") from exc
                raise TimeoutError(
                    "shared query embedding did not finish before its timeout"
                ) from exc
        return [vectors[query] for query in queries]

    def search(
        self,
        query: str,
        source_name: str | None = None,
        max_results: int = 10,
        *,
        overfetch: float = DEFAULT_FILTER_OVERFETCH,
    ) -> list[dict[str, Any]]:
        """Nearest neighbours, over-fetching when the source filter will cut.

        ``overfetch`` is `search.ranking.filter_overfetch`, handed down by the
        caller. It used to be a hardcoded ``* 4`` here, which made this the
        third independent implementation of one idea and the one an operator
        raising the tunable parameter could not reach at all.
        """

        if not (query or "").strip():
            return []
        query_vec = self.embed_query(query)
        if not any(query_vec):
            return []
        # Only when a post-filter will drop rows — the same rule the fused
        # path applies, spelled by the same helper.
        fetch = max(max_results, int(max_results * overfetch)) if source_name else max_results
        hits = self.store.search(query_vec, k=fetch)
        if source_name:
            hits = [hit for hit in hits if hit[2].get("source_id") == source_name]
        hits = hits[:max_results]
        if not hits:
            return []
        placeholders = ",".join("?" for _ in hits)
        rows = self.state.rows(
            f"""SELECT chunks.id AS chunk_id, chunks.source_id, chunks.artifact_id,
                       artifacts.relative_path AS relative_path, chunks.heading_path,
                       chunks.text, chunks.start_line, chunks.end_line,
                       artifacts.path AS absolute_path
                FROM chunks JOIN artifacts ON artifacts.id = chunks.artifact_id
                WHERE chunks.id IN ({placeholders})""",
            tuple(chunk_id for chunk_id, _, _ in hits),
        )
        by_id = {row["chunk_id"]: row for row in rows}
        results: list[dict[str, Any]] = []
        for chunk_id, similarity, _payload in hits:
            row = by_id.get(chunk_id)
            if row is None:
                continue  # vector store ahead of/behind SQLite; skip orphans
            score = max(0.0, min(1.0, (1.0 + similarity) / 2.0))
            results.append(_row_result(row, len(results) + 1, score, "Vector similarity"))
        return results


def build_embedder(settings: EmbeddingsSettings) -> Embedder:
    provider = (settings.provider or "").lower()
    if provider == "stub":
        return StubEmbedder(dim=settings.dimensions, model=settings.model)
    if provider in {"openai-spec", "openai"}:
        return OpenAISpecEmbedder(
            base_url=settings.base_url,
            model=settings.model,
            api_key_env=settings.api_key_env,
            dimensions=settings.dimensions,
            batch_size=settings.batch_size,
            timeout=float(getattr(settings, "timeout_seconds", 30.0)),
            max_retries=getattr(settings, "max_retries", 4),
            retry_backoff_seconds=getattr(settings, "retry_backoff_seconds", 1.0),
            rate_limit_max_wait_seconds=getattr(settings, "rate_limit_max_wait_seconds", 300.0),
        )
    raise ValueError(
        f"Unsupported search.embeddings.provider {settings.provider!r}; "
        "expected 'openai-spec' or 'stub'"
    )


def build_vector_store(config: PheasantConfig) -> VectorStore:
    settings = config.search.vector_store
    base = settings.path or (config.pheasant.state_path / "vectors")
    directory = Path(base) / config.knowledge_base_id
    provider = (settings.provider or "lancedb").lower()
    if provider == "numpy":
        return NumpyVectorStore(directory)
    if provider == "lancedb":
        return LanceDBVectorStore(directory)
    raise ValueError(
        f"Unsupported search.vector_store.provider {settings.provider!r}; "
        "expected 'lancedb' or 'numpy'"
    )


#: Backends ``build_vector_store`` knows how to construct, with the label and
#: install hint the UI shows. ``numpy`` needs nothing beyond the core deps.
VECTOR_STORE_PROVIDERS: tuple[tuple[str, str, str | None], ...] = (
    ("numpy", "Flat file (numpy)", None),
    ("lancedb", "LanceDB", "pip install 'pheasant-kb[vector]'"),
)


def vector_store_available(provider: str) -> bool:
    """Can this backend actually run in this process?

    Backends import lazily so that a region configured for LanceDB but never
    used doesn't pay the import, which also means "constructed" is not
    "usable". Callers that need to know *before* touching the store — the
    config UI offering a choice, the API refusing to claim embeddings are
    active — ask here.
    """

    name = (provider or "").lower()
    if name == "numpy":
        return True
    if name == "lancedb":
        try:
            return importlib.util.find_spec("lancedb") is not None
        except (ImportError, ValueError):  # namespace shadowing, broken install
            return False
    return False


def vector_indexer_from_config(
    config: PheasantConfig, *, background: bool = False
) -> VectorIndexer | None:
    """Build the embed-on-sync indexer, or ``None`` when disabled.

    Unrecognized providers (e.g. pre-21.4 example configs with
    ``provider: local`` / ``engine: chroma``) log a warning and behave as
    disabled instead of breaking an existing standalone deployment. A
    missing ``lancedb`` extra, in contrast, raises with an install hint.
    """

    settings = config.search.embeddings
    if not settings.enabled:
        return None
    try:
        return VectorIndexer(
            build_embedder(settings),
            build_vector_store(config),
            max_parallel_embeddings=getattr(config.sync.concurrency, "max_parallel_embeddings", 1),
            background=background,
        )
    except ValueError as exc:
        logger.warning("Vector search disabled: %s", exc)
        return None


def vector_searcher_from_config(
    config: PheasantConfig,
    state: StateStore,
) -> VectorSearcher | None:
    indexer = vector_indexer_from_config(config)
    if indexer is None:
        return None
    settings = config.search.embeddings
    query_settings = replace(
        settings,
        timeout_seconds=(settings.query_timeout_seconds or settings.timeout_seconds),
        max_retries=(
            settings.query_max_retries
            if settings.query_max_retries is not None
            else settings.max_retries
        ),
        rate_limit_max_wait_seconds=(
            settings.query_rate_limit_max_wait_seconds
            if settings.query_rate_limit_max_wait_seconds is not None
            else settings.rate_limit_max_wait_seconds
        ),
    )
    return VectorSearcher(build_embedder(query_settings), indexer.store, state)
