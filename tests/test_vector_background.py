"""Embedding off the commit loop, and the vector store's own upkeep.

`VectorIndexer(background=True)` -- what `SyncEngine` builds -- embeds a full
queue on a helper thread so the sole commit authority keeps committing while
a provider round trip is in flight. What it must not change is what a sync
*guarantees*: after `flush()` returns every queued chunk is in the store, a
provider failure still fails the sync (and loses no work), and a `reset()`
cannot be undone by a batch landing late. Each test below pins one of those.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from pheasant.search.vector_store import NumpyVectorStore, StubEmbedder, VectorIndexer


class GatedEmbedder(StubEmbedder):
    """A provider that does not answer until the test lets it."""

    batch_size = 2

    def __init__(self) -> None:
        super().__init__(dim=8)
        self.release = threading.Event()
        self.entered = threading.Event()
        self.fail_next = False

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.entered.set()
        assert self.release.wait(10), "test never released the provider"
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("provider refused the batch")
        return super().embed(texts)


def _rows(prefix: str, count: int) -> list[dict[str, Any]]:
    return [
        {"id": f"{prefix}-{index}", "text": f"{prefix} text {index}", "text_hash": f"h{index}"}
        for index in range(count)
    ]


def test_a_full_queue_does_not_hold_the_caller_while_the_provider_works(tmp_path: Path) -> None:
    embedder = GatedEmbedder()
    store = NumpyVectorStore(tmp_path / "vectors")
    indexer = VectorIndexer(embedder, store, queue_size=2, background=True)

    # Fills the queue; a synchronous indexer would block right here.
    assert indexer.index_artifact("docs", "a1", _rows("a", 2)) == 2
    assert embedder.entered.wait(5)
    assert store.count() == 0  # still in flight

    # The caller keeps going: the next file queues while the first is out.
    assert indexer.index_artifact("docs", "a2", _rows("b", 1)) == 1

    embedder.release.set()
    indexer.flush()
    assert store.count() == 3
    assert store.existing_ids(["a-0", "a-1", "b-0"]) == {"a-0", "a-1", "b-0"}


def test_flush_waits_for_the_batch_in_flight(tmp_path: Path) -> None:
    """`flush()` returning is the promise that every queued chunk is stored."""

    embedder = GatedEmbedder()
    store = NumpyVectorStore(tmp_path / "vectors")
    indexer = VectorIndexer(embedder, store, queue_size=2, background=True)
    indexer.index_artifact("docs", "a1", _rows("a", 2))
    assert embedder.entered.wait(5)

    done = threading.Event()

    def flush() -> None:
        indexer.flush()
        done.set()

    flusher = threading.Thread(target=flush)
    flusher.start()
    assert not done.wait(0.3), "flush returned while a batch was still in flight"
    embedder.release.set()
    flusher.join(5)
    assert done.is_set()
    assert store.count() == 2


def test_a_failed_background_batch_fails_the_next_call_and_keeps_its_work(
    tmp_path: Path,
) -> None:
    embedder = GatedEmbedder()
    embedder.fail_next = True
    embedder.release.set()
    store = NumpyVectorStore(tmp_path / "vectors")
    indexer = VectorIndexer(embedder, store, queue_size=2, background=True)

    indexer.index_artifact("docs", "a1", _rows("a", 2))
    # The failure surfaces on the caller's thread, one call later.
    with pytest.raises(RuntimeError, match="provider refused"):
        indexer.flush()
    assert store.count() == 0

    # Nothing was lost: the failed batch went back to the front of the queue,
    # so a retry stores it -- in order, under the same content-addressed ids.
    indexer.flush()
    assert store.count() == 2
    assert store.existing_ids(["a-0", "a-1"]) == {"a-0", "a-1"}


def test_an_error_is_raised_once_not_on_every_later_call(tmp_path: Path) -> None:
    embedder = GatedEmbedder()
    embedder.fail_next = True
    embedder.release.set()
    indexer = VectorIndexer(
        embedder, NumpyVectorStore(tmp_path / "vectors"), queue_size=2, background=True
    )
    indexer.index_artifact("docs", "a1", _rows("a", 2))
    indexer._wait_background(raise_error=False)  # let the failing batch finish
    indexer._error = RuntimeError("provider refused the batch")  # as it left it
    # The next file reports it -- and queues nothing, because it raised...
    with pytest.raises(RuntimeError, match="provider refused"):
        indexer.index_artifact("docs", "a2", _rows("b", 1))
    # ...and the one after carries on: the error is not sticky.
    assert indexer.index_artifact("docs", "a3", _rows("c", 1)) == 1
    indexer.flush()
    assert indexer.store.existing_ids(["a-0", "a-1", "c-0"]) == {"a-0", "a-1", "c-0"}


def test_reset_waits_out_a_batch_from_the_space_it_discards(tmp_path: Path) -> None:
    """A batch landing after `reset()` would write old-space vectors into the new one."""

    embedder = GatedEmbedder()
    store = NumpyVectorStore(tmp_path / "vectors")
    indexer = VectorIndexer(embedder, store, queue_size=2, background=True)
    indexer.index_artifact("docs", "a1", _rows("a", 2))
    assert embedder.entered.wait(5)

    resetter = threading.Thread(target=indexer.reset)
    resetter.start()
    resetter.join(0.3)
    assert resetter.is_alive(), "reset() did not wait for the batch in flight"
    embedder.release.set()
    resetter.join(5)
    assert store.count() == 0


def test_the_default_indexer_is_still_synchronous(tmp_path: Path) -> None:
    """Only the sync engine opts in; every other caller keeps the old contract."""

    embedder = StubEmbedder(dim=8)
    store = NumpyVectorStore(tmp_path / "vectors")
    indexer = VectorIndexer(embedder, store, queue_size=2)
    indexer.index_artifact("docs", "a1", _rows("a", 2))
    assert store.count() == 2  # embedded before index_artifact returned
    assert indexer._executor is None


def test_the_sync_engine_embeds_in_the_background_and_stores_everything(
    tmp_path: Path,
) -> None:
    from tests.conftest import make_vector_engine

    engine = make_vector_engine(tmp_path)
    assert engine.vectors.background is True
    result = engine.sync_source("notes", "full")
    assert result.status == "healthy"
    chunk_ids = {str(row["id"]) for row in engine.state.rows("SELECT id FROM chunks")}
    assert chunk_ids and engine.vectors.store.existing_ids(list(chunk_ids)) == chunk_ids
    # An unchanged re-sync still makes no embedder call.
    calls = engine.vectors.embedder.calls
    engine.sync_source("notes", "incremental")
    assert engine.vectors.embedder.calls == calls


# -- the remote in-flight rule ---------------------------------------------


def test_remote_inflight_defaults_to_two_per_url() -> None:
    from pheasant.sync.engine import _remote_inflight_batches

    assert _remote_inflight_batches(workers=16, batches=100, urls=1, configured=0) == 2
    assert _remote_inflight_batches(workers=16, batches=100, urls=3, configured=0) == 6


def test_remote_inflight_can_see_past_a_load_balanced_url() -> None:
    """One URL fronting many pods is what the setting exists for."""

    from pheasant.sync.engine import _remote_inflight_batches

    assert _remote_inflight_batches(workers=16, batches=100, urls=1, configured=8) == 8
    # Never past max_parallel_files, nor past the work there is.
    assert _remote_inflight_batches(workers=4, batches=100, urls=1, configured=8) == 4
    assert _remote_inflight_batches(workers=16, batches=3, urls=1, configured=8) == 3
    assert _remote_inflight_batches(workers=16, batches=0, urls=1, configured=8) == 1


# -- LanceDB upkeep ----------------------------------------------------------


def _lance(tmp_path: Path) -> Any:
    pytest.importorskip("lancedb", reason="the [vector] extra is optional")
    from pheasant.search.vector_store import LanceDBVectorStore

    return LanceDBVectorStore(tmp_path / "lance")


def _write_fragments(store: Any, fragments: int, source: str = "docs") -> list[str]:
    ids: list[str] = []
    for fragment in range(fragments):
        chunk_id = f"chunk:{source}:{fragment}"
        store.upsert(
            [chunk_id],
            [[float(fragment % 7), 1.0, 0.5, 0.25]],
            [{"source_id": source, "artifact_id": f"a{fragment}"}],
        )
        ids.append(chunk_id)
    return ids


def _fragments(store: Any) -> int:
    return int(store._table().stats()["fragment_stats"]["num_fragments"])


def test_lancedb_compacts_at_the_end_of_a_sync_that_fragmented_it(tmp_path: Path) -> None:
    store = _lance(tmp_path)
    store.COMPACT_AT_SMALL_FRAGMENTS = 8
    ids = _write_fragments(store, 10)
    assert _fragments(store) == 10
    store.flush()
    assert _fragments(store) == 1
    # Compaction moves nothing a reader can see.
    assert store.count() == 10
    assert store.existing_ids(ids) == set(ids)
    assert store.search([3.0, 1.0, 0.5, 0.25], 1)[0][0] == "chunk:docs:3"


def test_lancedb_does_not_rewrite_the_table_for_a_small_change(tmp_path: Path) -> None:
    store = _lance(tmp_path)
    store.COMPACT_AT_SMALL_FRAGMENTS = 8
    _write_fragments(store, 3)
    store.flush()
    assert _fragments(store) == 3  # below the threshold: left alone


def test_lancedb_flush_after_an_unchanged_sync_does_not_touch_the_table(tmp_path: Path) -> None:
    store = _lance(tmp_path)
    _write_fragments(store, 2)
    store.flush()
    opened = []
    original = store._table
    store._table = lambda: opened.append(1) or original()  # type: ignore[method-assign]
    store.flush()
    assert opened == []


def test_lancedb_source_ids_are_filtered_in_the_scan(tmp_path: Path) -> None:
    store = _lance(tmp_path)
    ours = _write_fragments(store, 3, source="it's-mine")
    _write_fragments(store, 2, source="other")
    # A quote in the source name is escaped, not an injection or an error.
    assert store.source_chunk_ids("it's-mine") == set(ours)
    assert store.source_chunk_ids("missing") == set()


class _InterleaveOnRelease:
    """The indexer's condition, running ``between`` once, right after the
    flushing thread first releases it -- the gap another source's thread can
    land in. Forcing the interleaving is the only way to test it: a stress
    test over the same race passed against the broken logic 750 times out of
    750, because the window is a few instructions wide."""

    def __init__(self, inner: threading.Condition, flusher: int, between: Any) -> None:
        self._inner, self._flusher, self._between, self._fired = inner, flusher, between, False

    def __enter__(self) -> Any:
        return self._inner.__enter__()

    def __exit__(self, *exc: Any) -> Any:
        result = self._inner.__exit__(*exc)
        if not self._fired and threading.get_ident() == self._flusher:
            self._fired = True
            self._between()
        return result

    def wait(self, timeout: float | None = None) -> bool:
        return self._inner.wait(timeout)

    def notify_all(self) -> None:
        self._inner.notify_all()


def flush_with_another_source_in_the_gap(indexer: VectorIndexer) -> set[str]:
    """Source A flushes; source B fills the queue in the gap. What of A's is
    missing from the store when A's `flush()` returns -- the moment a sync
    records its manifest?"""

    embedder = indexer.embedder
    indexer.index_artifact("a", "a-art", _rows("a", 3))  # below the queue size

    def source_b_fills_the_queue() -> None:
        # Hands everything queued -- A's three chunks included -- to a
        # background batch that the provider has not answered yet.
        indexer.index_artifact("b", "b-art", _rows("b", 3))
        threading.Timer(0.3, embedder.release.set).start()

    indexer._state = _InterleaveOnRelease(  # type: ignore[assignment]
        indexer._state, threading.get_ident(), source_b_fills_the_queue
    )
    indexer.flush()
    ids = [f"a-{index}" for index in range(3)]
    missing = set(ids) - indexer.store.existing_ids(ids)
    embedder.release.set()
    indexer._wait_background(raise_error=False)
    return missing


def test_a_flush_waits_for_its_own_chunks_in_another_sources_batch(tmp_path: Path) -> None:
    """Several sources share one indexer. `flush()` checking "nothing in
    flight" and then finding the queue empty is not "my chunks are stored":
    another source can hand them to a background batch in between."""

    indexer = VectorIndexer(
        GatedEmbedder(), NumpyVectorStore(tmp_path / "vectors"), queue_size=4, background=True
    )
    assert flush_with_another_source_in_the_gap(indexer) == set()
