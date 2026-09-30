"""Embed-on-sync: which chunks reach the embedder, and when.

Split out of `vector_store.py` (which keeps the embedders, the stores and the
query side) when this class grew a background path and the module passed its
size ceiling (`tests/test_module_budget.py`). It depends on the two protocols
only, so it imports nothing from there at run time; `vector_store` re-exports
it, and every existing ``from pheasant.search.vector_store import
VectorIndexer`` keeps working.
"""

from __future__ import annotations

import concurrent.futures
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pheasant.search.vector_store import Embedder, VectorStore


class VectorIndexer:
    """Embed-on-sync helper: embeds only chunk ids missing from the store.

    New/changed chunks are queued across files rather than embedded one file
    at a time. The sync loop calls `index_artifact` once per file, and most
    files in a real corpus carry far fewer chunks than an embedder's own
    `batch_size` (64) — embedding immediately, per file, turned a sync into
    one HTTP round-trip to the embedding provider *per file* instead of
    packing many files' chunks into one request. On a few-hundred-file repo
    that was the difference between a handful of embedding calls and
    hundreds, entirely serial on the sync's only thread. Queued chunks are
    flushed automatically once `queue_size` accumulates (one provider batch
    per allowed concurrent request by default, so memory stays bounded) and
    explicitly by `flush()`, which the caller
    (`SyncEngine`) already calls at the end of every sync.

    **``background=True``** (what `SyncEngine` asks for) embeds a full queue
    on a helper thread instead of the caller's. The caller is the commit loop
    — the sole commit authority — and a synchronous flush made it wait out a
    provider round trip every `queue_size` chunks, with nothing committed in
    the meantime: the provider's latency added straight onto the sync's wall
    time. In the background the loop keeps committing while one batch is in
    flight and the next fills. At most **one** batch is in flight, so pending
    text stays bounded at two queues and provider concurrency is exactly what
    `max_parallel_embeddings` already allowed. Nothing about *what* is stored
    changes: the same batches, the same ordered upsert, the same
    content-addressed ids.

    The guarantee `flush()` gave still holds: when it returns, every chunk
    queued before it was called is in the store. That is bookkept exactly --
    each queued chunk carries a sequence number until its upsert lands --
    rather than inferred from "nothing is in flight right now", because with
    several sources syncing at once another source's background batch can
    pick up this source's last chunks *after* that check. The source would
    then record its manifest while its vectors were still out, and a crash in
    that window leaves chunks no incremental sync ever re-embeds. A failed
    background batch is put back at the front of the queue exactly as a
    failed synchronous one is, and its error is raised by the next
    `index_artifact` or `flush` -- so a provider failure still fails the sync,
    one queue later than it used to.

    Thread-safe either way: the queue and the in-flight count are guarded by
    one condition, and every store call by one lock, so concurrent sources
    (``max_parallel_sources``) no longer need the engine's commit mutex to
    serialize them.
    """

    def __init__(
        self,
        embedder: Embedder,
        store: VectorStore,
        queue_size: int | None = None,
        max_parallel_embeddings: int = 1,
        *,
        background: bool = False,
    ):
        self.embedder = embedder
        self.store = store
        self.max_parallel_embeddings = max(1, int(max_parallel_embeddings or 1))
        configure_parallelism = getattr(embedder, "configure_parallelism", None)
        if callable(configure_parallelism):
            configure_parallelism(self.max_parallel_embeddings)
        # Queue one provider-sized group per concurrency slot. This fills each
        # request without allowing pending text to grow with source size.
        provider_batch_size = int(getattr(embedder, "batch_size", 64) or 64)
        self.queue_size = int(queue_size or provider_batch_size * self.max_parallel_embeddings)
        self.background = bool(background)
        self._pending: list[dict[str, Any]] = []
        self._state = threading.Condition()
        self._store_lock = threading.RLock()
        self._inflight = 0
        #: Sequence numbers of chunks queued and not yet stored.
        self._seq = 0
        self._outstanding: set[int] = set()
        self._error: BaseException | None = None
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None

    def index_artifact(
        self,
        source_id: str,
        artifact_id: str,
        chunk_rows: list[dict[str, Any]],
        on_progress: Callable[[int], None] | None = None,
    ) -> int:
        """Queue new/changed chunks for embedding; returns how many were queued.

        Chunk ids are content-addressed (``sha256={text_hash}`` is part of
        the id), so store membership doubles as the text_hash bookkeeping:
        an unchanged chunk keeps its id and is skipped without ever
        reaching the embedder. Queued chunks are not yet in the store —
        callers that need durability before returning (as opposed to by the
        end of the sync) should call `flush()`.
        """

        self._raise_background_error()
        ids = [str(chunk["id"]) for chunk in chunk_rows]
        with self._store_lock:
            existing = self.store.existing_ids(ids)
        pending = [chunk for chunk in chunk_rows if str(chunk["id"]) not in existing]
        if not pending:
            return 0
        with self._state:
            for chunk in pending:
                self._seq += 1
                self._outstanding.add(self._seq)
                self._pending.append(
                    {
                        "seq": self._seq,
                        "id": str(chunk["id"]),
                        "text": str(chunk["text"]),
                        "source_id": source_id,
                        "artifact_id": artifact_id,
                        "text_hash": chunk.get("text_hash"),
                    }
                )
            full = len(self._pending) >= self.queue_size
        if full:
            if self.background:
                self._submit_background(on_progress)
            else:
                self.flush_pending(on_progress=on_progress)
        return len(pending)

    def flush_pending(self, on_progress: Callable[[int], None] | None = None) -> None:
        """Embed and upsert everything queued so far, across every file
        `index_artifact` has touched since the last flush.

        Provider-sized batches may be in flight concurrently, but results are
        reassembled in input order and the vector store receives one ordered
        upsert. A failed batch therefore commits no partial flush and the
        content-addressed ids make a retry safe.

        Returns only once every chunk outstanding when it was called is
        stored: it embeds what is still queued and waits for whatever another
        thread has in flight, and raises a background batch's error.
        """
        with self._state:
            mine = set(self._outstanding)
        while True:
            with self._state:
                while True:
                    if self._error is not None:
                        error, self._error = self._error, None
                        raise error
                    if not mine & self._outstanding:
                        return
                    if self._pending:
                        batch, self._pending = self._pending, []
                        break
                    # The rest of ours is in another thread's batch.
                    self._state.wait()
            try:
                self._embed_and_store(batch, on_progress)
            except BaseException:
                # Nothing reached the store. Put the exact ordered work back so
                # a caller that catches the provider error may retry this
                # indexer.
                with self._state:
                    self._pending = batch + self._pending
                raise

    def _embed_and_store(
        self,
        batch: list[dict[str, Any]],
        on_progress: Callable[[int], None] | None,
    ) -> None:
        batch_size = max(1, int(getattr(self.embedder, "batch_size", len(batch)) or len(batch)))
        groups = [batch[start : start + batch_size] for start in range(0, len(batch), batch_size)]
        grouped_vectors: list[list[list[float]] | None] = [None] * len(groups)

        def embed_group(group: list[dict[str, Any]]) -> list[list[float]]:
            return self.embedder.embed([str(item["text"]) for item in group])

        if self.max_parallel_embeddings <= 1 or len(groups) <= 1:
            for index, group in enumerate(groups):
                grouped_vectors[index] = embed_group(group)
                if on_progress is not None:
                    on_progress(len(group))
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(self.max_parallel_embeddings, len(groups)),
                thread_name_prefix="pheasant-embed",
            ) as executor:
                futures = {
                    executor.submit(embed_group, group): (index, len(group))
                    for index, group in enumerate(groups)
                }
                for future in concurrent.futures.as_completed(futures):
                    index, size = futures[future]
                    grouped_vectors[index] = future.result()
                    if on_progress is not None:
                        on_progress(size)

        vectors = [vector for group in grouped_vectors for vector in (group or [])]
        with self._store_lock:
            self.store.upsert(
                [item["id"] for item in batch],
                vectors,
                [
                    {
                        "source_id": item["source_id"],
                        "artifact_id": item["artifact_id"],
                        "text_hash": item["text_hash"],
                    }
                    for item in batch
                ],
            )
        with self._state:
            self._outstanding.difference_update(item["seq"] for item in batch)
            self._state.notify_all()

    # -- background embedding -------------------------------------------

    def _submit_background(self, on_progress: Callable[[int], None] | None) -> None:
        with self._state:
            # Backpressure: one batch in flight while the next fills. Waiting
            # here is what bounds pending text when the provider is slower
            # than the commit loop.
            while self._inflight and self._error is None:
                self._state.wait()
            if self._error is not None or not self._pending:
                # The error is raised by the next index_artifact/flush; the
                # batch that failed is already back in `_pending`.
                return
            batch, self._pending = self._pending, []
            self._inflight += 1
            if self._executor is None:
                self._executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="pheasant-embed-bg"
                )
            executor = self._executor
        executor.submit(self._run_background, batch, on_progress)

    def _run_background(
        self,
        batch: list[dict[str, Any]],
        on_progress: Callable[[int], None] | None,
    ) -> None:
        try:
            self._embed_and_store(batch, on_progress)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            with self._state:
                self._pending = batch + self._pending
                if self._error is None:
                    self._error = exc
        finally:
            with self._state:
                self._inflight -= 1
                self._state.notify_all()

    def _wait_background(self, *, raise_error: bool = True) -> None:
        with self._state:
            while self._inflight:
                self._state.wait()
            error, self._error = self._error, None
        if error is not None and raise_error:
            raise error

    def _raise_background_error(self) -> None:
        with self._state:
            error, self._error = self._error, None
        if error is not None:
            raise error

    def prune_source(self, source_id: str, live_chunk_ids: set[str]) -> int:
        """Delete vectors for chunks (or whole artifacts) no longer indexed.

        Only considers chunks already *in the store* — a chunk still sitting
        in the pending queue (not yet embedded) is never mistaken for stale,
        since it cannot be a member of `store.source_chunk_ids` yet.
        """

        with self._store_lock:
            stale = sorted(self.store.source_chunk_ids(source_id) - set(live_chunk_ids))
            if not stale:
                return 0
            return self.store.delete(chunk_ids=stale)

    def reset(self) -> int:
        """Discard pending work and reset the store's vector-space schema.

        Waits out a background batch first: one landing *after* the reset
        would write vectors from the space being discarded into the new one.
        """

        self._wait_background(raise_error=False)
        with self._state:
            self._pending = []
            self._outstanding.clear()
        with self._store_lock:
            return self.store.reset()

    def flush(self, on_progress: Callable[[int], None] | None = None) -> None:
        """Embed anything still queued, then force the store's writes to
        disk now. The caller (`SyncEngine`) MUST call this at the end of a
        sync, alongside its own final graph save — see `NumpyVectorStore`'s
        docstring for why the disk-flush half exists."""
        self.flush_pending(on_progress=on_progress)
        with self._store_lock:
            self.store.flush()
