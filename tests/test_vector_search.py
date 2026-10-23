"""Acceptance tests for per-region vector self-search (Synapse step 21.4).

Fully offline: embeddings come from the deterministic ``StubEmbedder``
(planted synonym ``automobile -> car``) and the OpenAI-spec wire format is
exercised against a monkeypatched ``urlopen``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import make_vector_engine, run_sync


def _search(engine: Any, query: str, mode: str, source_name: str | None = None) -> dict:
    from pheasant.search.hybrid import HybridSearch
    from pheasant.search.sqlite_store import SearchStore

    searcher = HybridSearch(SearchStore(engine.state), vector=engine.vector_searcher())
    return searcher.search_context(
        engine.config.knowledge_base_id,
        query,
        mode=mode,
        max_results=5,
        source_name=source_name,
    )


def _result_paths(payload: dict) -> list[str]:
    return [str(item.get("relative_path") or "") for item in payload["results"]]


def test_vector_and_hybrid_surface_lexically_absent_synonym(tmp_path: Path) -> None:
    """Query "car" finds the automobile chunk via vectors; text mode misses it."""

    engine = make_vector_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")

    text = _search(engine, "car", "text")
    assert all("vehicles.md" not in path for path in _result_paths(text))

    vector = _search(engine, "car", "vector")
    assert vector["counts"]["vector"] >= 1
    assert _result_paths(vector), "Expected vector candidates for the synonym query."
    assert "vehicles.md" in _result_paths(vector)[0]
    top = vector["results"][0]
    # Same result shape as text hits: path/preview/score/node ids.
    assert top["type"] == "chunk"
    assert top["reason"] == "Vector similarity"
    assert top["node_id"] and top["chunk_id"]
    assert 0.0 <= top["score"] <= 1.0
    assert top["chunks"][0]["text_preview"]
    assert top["provenance"]["source_id"] == "notes"

    hybrid = _search(engine, "car", "hybrid")
    assert any("vehicles.md" in path for path in _result_paths(hybrid))


def test_hybrid_degrades_when_the_vector_arm_raises(tmp_path: Path) -> None:
    """A broken embedding provider (bad/missing key, network) must not take
    down text and graph results that already succeeded.

    Regression: ``HybridSearch.search_context`` ran its three arms in a
    ``ThreadPoolExecutor`` and called ``future.result()`` unguarded, so one
    arm's exception (e.g. ``urllib.error.HTTPError: 401`` from an
    OpenAI-spec embedder with no valid API key) propagated out of the whole
    hybrid search — and from there out of the assistant chat workflow,
    surfacing to the UI as a raw "401 Unauthorized" instead of an answer
    built from the arms that were actually healthy.
    """
    from pheasant.search.hybrid import HybridSearch
    from pheasant.search.sqlite_store import SearchStore

    engine = make_vector_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")

    class ExplodingVector:
        def search(self, *args: Any, **kwargs: Any) -> list[dict]:
            raise RuntimeError("simulated embedding provider failure (e.g. 401)")

    searcher = HybridSearch(SearchStore(engine.state), vector=ExplodingVector())
    # "automobile" (not the planted synonym "car") is literal text in
    # vehicles.md, so the text arm has a real hit to degrade to.
    payload = searcher.search_context(
        engine.config.knowledge_base_id, "automobile", mode="hybrid", max_results=5
    )
    # The vector arm degraded to empty rather than raising; text/graph still
    # answered, so hybrid mode returns a real (if reduced) result set.
    assert payload["counts"]["vector"] == 0
    assert payload["results"]
    assert "vehicles.md" in _result_paths(payload)[0]


def test_vector_mode_respects_source_filter(tmp_path: Path) -> None:
    engine = make_vector_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")
    payload = _search(engine, "car", "vector", source_name="no-such-source")
    assert payload["results"] == []


def test_index_artifact_batches_across_files_instead_of_one_call_per_file(
    tmp_path: Path,
) -> None:
    """The slow-indexing complaint this fixes: most files carry far fewer
    chunks than an embedder's batch size, so embedding immediately inside
    the per-file sync loop made one HTTP round-trip per file. Chunks must
    now queue across files and flush in `queue_size` groups instead.
    """
    from pheasant.search.vector_store import NumpyVectorStore, StubEmbedder, VectorIndexer

    embedder = StubEmbedder(dim=16)
    indexer = VectorIndexer(embedder, NumpyVectorStore(tmp_path / "vectors"), queue_size=10)

    # 25 "files" of 1 chunk each -- the old per-file-call design would have
    # made 25 embedder calls; batched at queue_size=10 this is 2 full
    # flushes during the loop (20 chunks) plus a final explicit flush for
    # the 5-chunk remainder.
    for i in range(25):
        queued = indexer.index_artifact(
            "notes",
            f"artifact-{i}",
            [
                {
                    "id": f"chunk:notes:file{i}.md:sha256=hash{i}:chunk=0000",
                    "text": f"file {i} says something about topic {i}",
                    "text_hash": f"hash{i}",
                }
            ],
        )
        assert queued == 1

    assert embedder.calls == 2, "two full queue_size=10 batches should have auto-flushed"
    assert embedder.texts_embedded == 20, "the 5-chunk remainder is still only queued"
    assert indexer.store.count() == 20

    indexer.flush()  # SyncEngine always calls this at the end of a sync
    assert embedder.calls == 3
    assert embedder.texts_embedded == 25
    assert indexer.store.count() == 25

    # A second call with the identical chunk ids is a no-op: nothing queued,
    # nothing embedded, nothing to flush.
    requeued = indexer.index_artifact(
        "notes",
        "artifact-0",
        [
            {
                "id": "chunk:notes:file0.md:sha256=hash0:chunk=0000",
                "text": "file 0 says something about topic 0",
                "text_hash": "hash0",
            }
        ],
    )
    assert requeued == 0
    assert embedder.calls == 3


def test_changed_chunk_reembeds_only_that_chunk(tmp_path: Path) -> None:
    engine = make_vector_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")
    embedder = engine.vectors.embedder
    store = engine.vectors.store
    baseline_texts = embedder.texts_embedded
    baseline_count = store.count()

    notes_dir = Path(engine.config.sources[0].path)
    kitchen = notes_dir / "kitchen.md"
    kitchen.write_text(
        kitchen.read_text(encoding="utf-8") + "\nPolish the silverware weekly.\n",
        encoding="utf-8",
    )
    run_sync(engine, source_name="notes", mode="incremental")

    # Only the changed file's single chunk was re-embedded; the stale vector
    # for its previous text_hash was pruned, so the store count is unchanged.
    assert embedder.texts_embedded - baseline_texts == 1
    assert store.count() == baseline_count


def test_removed_artifact_vectors_are_deleted(tmp_path: Path) -> None:
    engine = make_vector_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")
    store = engine.vectors.store
    before = store.count()
    assert before >= 2

    notes_dir = Path(engine.config.sources[0].path)
    (notes_dir / "kitchen.md").unlink()
    result = run_sync(engine, source_name="notes", mode="full")

    assert store.count() == before - 1
    assert all("kitchen.md" not in chunk_id for chunk_id in store.source_chunk_ids("notes"))
    assert result.details["pruned_vectors"] >= 1


def test_disabled_embeddings_change_nothing(
    loaded_config: Any, sync_engine: Any, state_path: Path
) -> None:
    """Default config: no vector dir, no embedder, search modes as before."""

    run_sync(sync_engine, source_name="pheasant-repo", mode="full")
    assert sync_engine.vectors is None
    assert sync_engine.vector_searcher() is None
    assert not (state_path / "vectors").exists()

    text = _search(sync_engine, "sync engine", "text")
    assert text["results"]
    assert "vector" not in {item["reason"] for item in text["results"]}
    vector = _search(sync_engine, "sync engine", "vector")
    assert vector["results"] == []
    assert vector["counts"]["vector"] == 0
    hybrid = _search(sync_engine, "sync engine", "hybrid")
    assert hybrid["results"]


def test_stub_embedder_is_deterministic_and_synonym_aligned() -> None:
    from pheasant.search.vector_store import StubEmbedder

    first = StubEmbedder(dim=32)
    second = StubEmbedder(dim=32)
    assert first.embed(["the automobile fleet"]) == second.embed(["the automobile fleet"])

    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
        return dot / norm

    car = first.embed(["car"])[0]
    automobile = first.embed(["automobile"])[0]
    pantry = first.embed(["pantry"])[0]
    assert _cosine(car, automobile) > 0.99
    assert _cosine(car, automobile) > _cosine(car, pantry) + 0.5


def test_openai_spec_embedder_wire_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline conformance with the OpenAI embeddings HTTP shape (x-repo pin)."""

    from pheasant.search.vector_store import OpenAISpecEmbedder

    captured: dict[str, Any] = {}

    def fake_transport(request: Any, timeout: float = 0) -> bytes:
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["auth"] = request.get_header("Authorization")
        count = len(captured["body"]["input"])
        # Deliberately out of order: the client must sort by data[i].index.
        return json.dumps(
            {"data": [{"index": i, "embedding": [float(i), 1.0]} for i in reversed(range(count))]}
        ).encode("utf-8")

    monkeypatch.setattr("pheasant.search.embedding_client._transport", fake_transport)
    monkeypatch.setenv("PHEASANT_TEST_EMBED_KEY", "sekrit")
    embedder = OpenAISpecEmbedder(
        "http://localhost:9/v1/",
        "fleet-pinned-model",
        api_key_env="PHEASANT_TEST_EMBED_KEY",
        dimensions=2,
        batch_size=8,
    )
    vectors = embedder.embed(["alpha", "beta"])

    assert captured["url"] == "http://localhost:9/v1/embeddings"
    assert captured["body"]["model"] == "fleet-pinned-model"
    assert captured["body"]["input"] == ["alpha", "beta"]
    assert captured["body"]["dimensions"] == 2
    assert captured["auth"] == "Bearer sekrit"
    assert vectors == [[0.0, 1.0], [1.0, 1.0]]
    assert embedder.calls == 1
    assert embedder.texts_embedded == 2


@pytest.fixture(params=["numpy", "lancedb"])
def store_backend(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "lancedb":
        pytest.importorskip(
            "lancedb", reason="lancedb not installed (pip install 'pheasant-kb[vector]')"
        )
        from pheasant.search.vector_store import LanceDBVectorStore

        return LanceDBVectorStore(tmp_path / "vectors")
    from pheasant.search.vector_store import NumpyVectorStore

    return NumpyVectorStore(tmp_path / "vectors")


def test_vector_store_roundtrip(store_backend: Any) -> None:
    payloads = [
        {"source_id": "s1", "artifact_id": "a1", "text_hash": "h1"},
        {"source_id": "s1", "artifact_id": "a1", "text_hash": "h2"},
        {"source_id": "s2", "artifact_id": "a2", "text_hash": "h3"},
    ]
    store_backend.upsert(
        ["c1", "c2", "c3"],
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.9, 0.1, 0.0]],
        payloads,
    )
    assert store_backend.count() == 3
    assert store_backend.dimensions() == 3
    assert store_backend.existing_ids(["c1", "missing"]) == {"c1"}
    assert store_backend.source_chunk_ids("s1") == {"c1", "c2"}

    hits = store_backend.search([1.0, 0.0, 0.0], k=2)
    assert [hit[0] for hit in hits] == ["c1", "c3"]
    assert hits[0][1] == pytest.approx(1.0, abs=1e-3)
    assert hits[0][1] >= hits[1][1]
    assert hits[0][2]["artifact_id"] == "a1"

    # Idempotent upsert: re-upserting an id replaces rather than duplicates.
    store_backend.upsert(["c1"], [[0.5, 0.5, 0.0]], [payloads[0]])
    assert store_backend.count() == 3

    assert store_backend.delete(chunk_ids=["c2"]) == 1
    assert store_backend.count() == 2
    assert store_backend.delete(artifact_id="a2") == 1
    assert store_backend.count() == 1

    # Reset must remove the backend's vector-width schema, not just its rows.
    # LanceDB otherwise keeps a FixedSizeList(3) table and rejects this new
    # five-dimensional embedding space even though the table is empty.
    assert store_backend.reset() == 1
    assert store_backend.count() == 0
    assert store_backend.dimensions() is None
    store_backend.upsert(
        ["c4"],
        [[1.0, 0.0, 0.0, 0.0, 0.0]],
        [{"source_id": "s3", "artifact_id": "a3", "text_hash": "h4"}],
    )
    assert store_backend.dimensions() == 5
    assert len(store_backend.all_vectors()[0][1]) == 5


def test_engine_embeds_with_lancedb_backend(tmp_path: Path) -> None:
    pytest.importorskip(
        "lancedb", reason="lancedb not installed (pip install 'pheasant-kb[vector]')"
    )
    engine = make_vector_engine(tmp_path, vector_provider="lancedb")
    run_sync(engine, source_name="notes", mode="full")
    assert engine.vectors.store.count() >= 2
    payload = _search(engine, "car", "vector")
    assert "vehicles.md" in _result_paths(payload)[0]


def test_lancedb_membership_is_cached_and_new_ids_skip_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-artifact membership checks must not rescan a growing Lance table.

    New content-addressed ids also must not create pointless Lance delete
    versions before their first insert.
    """
    pytest.importorskip(
        "lancedb", reason="lancedb not installed (pip install 'pheasant-kb[vector]')"
    )
    from pheasant.search.vector_store import LanceDBVectorStore

    store = LanceDBVectorStore(tmp_path / "vectors")
    store.upsert(["c1"], [[1.0, 0.0]], [{"source_id": "s1", "artifact_id": "a1"}])

    row_reads = 0
    original_rows = store._rows

    def counted_rows(columns: list[str]) -> list[dict[str, Any]]:
        nonlocal row_reads
        row_reads += 1
        return original_rows(columns)

    monkeypatch.setattr(store, "_rows", counted_rows)
    assert store.existing_ids(["c1", "missing"]) == {"c1"}
    assert store.existing_ids(["c1"]) == {"c1"}
    assert row_reads == 0, "the successful initial write populated membership"

    deleted: list[list[str] | None] = []
    original_delete = store.delete

    def counted_delete(chunk_ids: list[str] | None = None, artifact_id: str | None = None) -> int:
        deleted.append(chunk_ids)
        return original_delete(chunk_ids=chunk_ids, artifact_id=artifact_id)

    monkeypatch.setattr(store, "delete", counted_delete)
    store.upsert(["c2"], [[0.0, 1.0]], [{"source_id": "s1", "artifact_id": "a2"}])
    assert deleted == [], "a never-seen id does not need a Lance delete version"
    store.upsert(["c1"], [[0.5, 0.5]], [{"source_id": "s1", "artifact_id": "a1"}])
    assert deleted == [["c1"]]


def test_lancedb_opens_at_the_last_complete_version_after_a_torn_commit(
    tmp_path: Path,
) -> None:
    """An empty newest manifest must not make every sync fail.

    A host stopped mid-commit leaves the newest version manifest journaled but
    unwritten: 0 bytes, which Lance reads as the latest version and cannot
    open. The store parks it (never deletes it) and opens the version before.
    """
    pytest.importorskip(
        "lancedb", reason="lancedb not installed (pip install 'pheasant-kb[vector]')"
    )
    from pheasant.search.vector_store import LanceDBVectorStore

    store = LanceDBVectorStore(tmp_path / "vectors")
    store.upsert(["c1"], [[1.0, 0.0]], [{"source_id": "s1", "artifact_id": "a1"}])
    store.upsert(["c2"], [[0.0, 1.0]], [{"source_id": "s1", "artifact_id": "a2"}])
    versions = tmp_path / "vectors" / f"{store.TABLE}.lance" / "_versions"
    manifests = sorted(versions.glob("*.manifest"), key=lambda path: path.stat().st_mtime_ns)
    newest = manifests[-1]
    newest.write_bytes(b"")

    fresh = LanceDBVectorStore(tmp_path / "vectors")
    assert fresh.count() == 1, "the version before the torn commit holds c1 only"
    assert newest.with_name(newest.name + ".torn").exists(), "parked, not deleted"

    fresh.upsert(["c3"], [[0.5, 0.5]], [{"source_id": "s1", "artifact_id": "a3"}])
    assert LanceDBVectorStore(tmp_path / "vectors").existing_ids(["c1", "c3"]) == {"c1", "c3"}


def test_numpy_store_caches_the_decoded_matrix(tmp_path: Path) -> None:
    """Regression test: search() must not re-decode the whole index every call.

    Every vector is stored base64-encoded, and decoding all of them is a
    pure-Python loop (struct.unpack per item) that does not release the GIL
    between iterations -- so redoing it on every search() call meant N
    concurrent searches cost roughly N times one search's worth of decode
    work, GIL-serialized, regardless of thread count. Measured live: an
    agentic retrieve step fanning out 4 concurrent vector searches took
    21-29s against a 7,463-chunk index. The fix caches the decoded
    (ids, matrix, norms) keyed off the same file signature `_items()`
    already uses, so it is correctness-equivalent to the uncached version
    and only rebuilds when the underlying file actually changes.
    """
    from pheasant.search.vector_store import NumpyVectorStore

    store = NumpyVectorStore(tmp_path / "vectors")
    store.upsert(
        ["c1", "c2", "c3"],
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.9, 0.1, 0.0]],
        [
            {"source_id": "s1", "artifact_id": "a1", "text_hash": "h1"},
            {"source_id": "s1", "artifact_id": "a1", "text_hash": "h2"},
            {"source_id": "s2", "artifact_id": "a2", "text_hash": "h3"},
        ],
    )

    first = store.search([1.0, 0.0, 0.0], k=2)
    cache_after_first = store._matrix_cache

    # Repeated searches against unchanged data must be identical...
    second = store.search([1.0, 0.0, 0.0], k=2)
    assert first == second
    # ...and must not have rebuilt the decoded matrix (same object, not an
    # equal-but-freshly-built one) -- this is the actual property being
    # fixed, not just "results are still correct".
    assert store._matrix_cache is cache_after_first

    # A write must invalidate the cache. Invalidation is lazy -- the matrix
    # is rebuilt on the *next search*, not eagerly on write -- so the write
    # alone changes nothing yet; only after a search does identity change,
    # and the new data is what it returns.
    store.upsert(["c4"], [[0.0, 0.0, 1.0]], [{"source_id": "s3", "artifact_id": "a3"}])
    after_write = store.search([0.0, 0.0, 1.0], k=1)
    assert after_write[0][0] == "c4"
    assert store._matrix_cache is not cache_after_first

    # Deleting must invalidate it too, not just adding.
    cache_before_delete = store._matrix_cache
    store.delete(chunk_ids=["c4"])
    remaining = store.search([0.0, 0.0, 1.0], k=5)
    assert all(hit[0] != "c4" for hit in remaining)
    assert store._matrix_cache is not cache_before_delete


def test_numpy_store_concurrent_search_is_thread_safe(tmp_path: Path) -> None:
    """Many threads racing to build the cache must not corrupt or crash."""
    import threading

    from pheasant.search.vector_store import NumpyVectorStore

    store = NumpyVectorStore(tmp_path / "vectors")
    # One-hot vectors: orthogonal, so cosine similarity to the query is
    # exactly 1.0 for its own match and exactly 0.0 for every other id --
    # unlike collinear vectors (e.g. [i, 0, 0]), which all tie at cosine
    # similarity 1.0 and make "the top hit" undefined.
    n = 200
    store.upsert(
        [f"c{i}" for i in range(n)],
        [[1.0 if j == i else 0.0 for j in range(n)] for i in range(n)],
        [{"source_id": "s", "artifact_id": f"a{i}"} for i in range(n)],
    )
    query = [1.0 if j == n - 1 else 0.0 for j in range(n)]

    results: list[list] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            hits = store.search(query, k=3)
            with lock:
                results.append(hits)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"concurrent search failed: {errors[0]!r}"
    assert len(results) == 16


def test_numpy_store_defers_disk_writes_until_flush(tmp_path: Path) -> None:
    """Regression: `upsert` used to write the *entire* index to disk on
    every call — one full rewrite per artifact, since `VectorIndexer.
    index_artifact` calls it once per file. Measured live indexing a large
    second source into an already-large shared index: over 100GB written
    for a ~150MB final file. Writes must now stay in memory until an
    explicit `flush()` (or the self-throttled interval elapses)."""
    from pheasant.search.vector_store import NumpyVectorStore

    store = NumpyVectorStore(tmp_path / "vectors", flush_interval_seconds=999)
    path = store.path

    store.upsert(["c1"], [[1.0, 0.0]], [{"source_id": "s1", "artifact_id": "a1"}])
    assert not path.exists(), "upsert must not write to disk before a flush"

    store.upsert(["c2"], [[0.0, 1.0]], [{"source_id": "s1", "artifact_id": "a2"}])
    assert not path.exists(), "a second buffered upsert must still not touch disk"

    # Reads against this same instance must see the buffered data regardless.
    assert store.count() == 2
    assert store.existing_ids(["c1", "c2", "missing"]) == {"c1", "c2"}

    store.flush()
    assert path.exists(), "flush() must write the buffered index to disk"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert set(on_disk["items"]) == {"c1", "c2"}

    # A second flush with nothing new pending must not rewrite the file.
    mtime_after_first_flush = path.stat().st_mtime_ns
    store.flush()
    assert path.stat().st_mtime_ns == mtime_after_first_flush


def test_numpy_store_flush_is_durable_for_a_fresh_instance(tmp_path: Path) -> None:
    """A second `NumpyVectorStore` pointed at the same directory — the
    shape of a later process reading what an earlier sync wrote — must see
    exactly what was flushed, nothing more and nothing less."""
    from pheasant.search.vector_store import NumpyVectorStore

    directory = tmp_path / "vectors"
    writer = NumpyVectorStore(directory, flush_interval_seconds=999)
    writer.upsert(
        ["c1", "c2"], [[1.0, 0.0], [0.0, 1.0]], [{"source_id": "s1"}, {"source_id": "s1"}]
    )
    writer.upsert(["c3"], [[0.5, 0.5]], [{"source_id": "s2"}])  # still unflushed
    writer.flush()
    writer.upsert(["c4"], [[0.2, 0.8]], [{"source_id": "s2"}])  # buffered, never flushed

    reader = NumpyVectorStore(directory)
    assert reader.existing_ids(["c1", "c2", "c3", "c4"]) == {"c1", "c2", "c3"}
    assert reader.count() == 3


def test_numpy_store_flush_throttle_matches_checkpoint_pattern(tmp_path: Path) -> None:
    """The interval self-scales with the last save's duration, the same
    shape as `SyncEngine._maybe_checkpoint` — a short configured interval
    is still respected as a *minimum*, not bypassed, once at least one
    save has actually happened."""
    from pheasant.search.vector_store import NumpyVectorStore

    store = NumpyVectorStore(tmp_path / "vectors", flush_interval_seconds=999)
    store.upsert(["c1"], [[1.0]], [{"source_id": "s1"}])
    assert not store.path.exists()

    # An explicit flush always honors `force=True` regardless of interval.
    store.flush()
    assert store.path.exists()
    mtime = store.path.stat().st_mtime_ns

    # A subsequent non-forced upsert, immediately after, must not flush —
    # the interval (999s) has obviously not elapsed.
    store.upsert(["c2"], [[0.0, 1.0]], [{"source_id": "s1"}])
    assert store.path.stat().st_mtime_ns == mtime
    assert store.existing_ids(["c1", "c2"]) == {"c1", "c2"}, "still readable from the buffer"


def test_sync_engine_flushes_the_vector_store_at_the_end_of_a_sync(tmp_path: Path) -> None:
    """End-to-end: a real sync must leave the vector index durable on disk
    even though the store's own flush interval (well above one test's
    runtime) would not have elapsed on its own — `SyncEngine.sync_source`
    must force a flush, the same way it unconditionally saves the graph."""
    engine = make_vector_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")

    index_path = engine.vectors.store.path
    assert index_path.exists()
    on_disk = json.loads(index_path.read_text(encoding="utf-8"))
    assert len(on_disk["items"]) == engine.vectors.store.count() > 0


# ---------------------------------------------------------------------------
# Transient-failure retry (found by a live 12,667-file index)
# ---------------------------------------------------------------------------


def _embedder(**kwargs):
    from pheasant.search.vector_store import OpenAISpecEmbedder

    return OpenAISpecEmbedder(
        base_url="https://example.invalid/v1",
        model="text-embedding-3-small",
        retry_backoff_seconds=0.01,
        **kwargs,
    )


def _ok_response(count: int) -> bytes:
    import json as _json

    return _json.dumps(
        {"data": [{"index": i, "embedding": [0.1, 0.2]} for i in range(count)]}
    ).encode()


def test_a_transient_tls_error_is_retried(monkeypatch) -> None:
    """The exact failure a real vscode index died on, 45 minutes in.

    `ssl.SSLError: SSLV3_ALERT_BAD_RECORD_MAC` is a corrupted TLS record — it
    succeeds on the next attempt. Without a retry, one flaky call out of the
    ~200 a large corpus needs aborts the entire sync.
    """
    import ssl

    from pheasant.search import embedding_client

    calls = {"n": 0}

    def flaky(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ssl.SSLError("SSLV3_ALERT_BAD_RECORD_MAC")
        return _ok_response(1)

    monkeypatch.setattr(embedding_client, "_transport", flaky)
    vectors = _embedder().embed(["hello"])
    assert calls["n"] == 2
    assert vectors == [[0.1, 0.2]]


def test_a_rate_limit_is_retried_and_honours_retry_after(monkeypatch) -> None:
    from urllib.error import HTTPError

    from pheasant.search import embedding_client, vector_store

    calls = {"n": 0}
    slept: list[float] = []

    def limited(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise HTTPError("u", 429, "Too Many Requests", {"Retry-After": "2"}, None)
        return _ok_response(1)

    monkeypatch.setattr(embedding_client, "_transport", limited)
    monkeypatch.setattr(vector_store.time, "sleep", lambda s: slept.append(s))
    _embedder().embed(["hello"])
    assert calls["n"] == 2
    # Guessing shorter than a stated Retry-After is how a 429 becomes a ban.
    assert slept == [2.0]


def test_a_bad_key_is_not_retried(monkeypatch) -> None:
    """401 fails identically every time; retrying only spends time and money."""
    from urllib.error import HTTPError

    import pytest as _pytest

    from pheasant.search import embedding_client

    calls = {"n": 0}

    def unauthorized(request, timeout=None):
        calls["n"] += 1
        raise HTTPError("u", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(embedding_client, "_transport", unauthorized)
    with _pytest.raises(HTTPError):
        _embedder().embed(["hello"])
    assert calls["n"] == 1, "a wrong key must surface immediately"


def test_retries_are_bounded(monkeypatch) -> None:
    import ssl

    import pytest as _pytest

    from pheasant.search import embedding_client, vector_store

    calls = {"n": 0}

    def always_broken(request, timeout=None):
        calls["n"] += 1
        raise ssl.SSLError("still broken")

    monkeypatch.setattr(embedding_client, "_transport", always_broken)
    monkeypatch.setattr(vector_store.time, "sleep", lambda s: None)
    with _pytest.raises(ssl.SSLError):
        _embedder(max_retries=3).embed(["hello"])
    assert calls["n"] == 4, "initial attempt plus exactly max_retries"


# ---------------------------------------------------------------------------
# Query latency: the provider connection and the held table
# ---------------------------------------------------------------------------


def _embedding_server(close_after: int | None = None):
    """A local OpenAI-spec endpoint that counts the connections it accepts."""

    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = {"connections": 0, "requests": 0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            seen["connections"] += 1

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["requests"] += 1
            data = [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(body["input"]))]
            payload = json.dumps({"data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            if close_after is not None and seen["requests"] % close_after == 0:
                self.close_connection = True  # an idle timeout, from the client's side

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def test_query_embeddings_reuse_one_connection() -> None:
    """A new TCP+TLS session per query was most of a vector search's time."""

    from pheasant.search.vector_store import OpenAISpecEmbedder

    server, seen = _embedding_server()
    try:
        embedder = OpenAISpecEmbedder(f"http://127.0.0.1:{server.server_port}/v1", "m")
        for word in ("alpha", "beta", "gamma", "delta"):
            assert embedder.embed([word]) == [[1.0, 0.0]]
    finally:
        server.shutdown()
    assert seen["requests"] == 4
    assert seen["connections"] == 1


def test_a_connection_the_server_closed_is_replaced_without_a_retry() -> None:
    from pheasant.search.vector_store import OpenAISpecEmbedder

    server, seen = _embedding_server(close_after=1)
    try:
        embedder = OpenAISpecEmbedder(
            f"http://127.0.0.1:{server.server_port}/v1", "m", max_retries=0
        )
        for word in ("alpha", "beta", "gamma"):
            assert embedder.embed([word]) == [[1.0, 0.0]]
    finally:
        server.shutdown()
    assert seen["requests"] == 3


def test_openai_itself_is_asked_for_base64_and_others_are_not(monkeypatch) -> None:
    import base64

    import numpy as np

    from pheasant.search import embedding_client, vector_store

    sent: list[dict[str, Any]] = []
    vector = np.array([0.25, -0.5, 1.0], dtype="<f4")

    def fake(request: Any, timeout: float = 0) -> bytes:
        body = json.loads(request.data)
        sent.append(body)
        value: Any = [0.25, -0.5, 1.0]
        if body.get("encoding_format") == "base64":
            value = base64.b64encode(vector.tobytes()).decode()
        return json.dumps({"data": [{"index": 0, "embedding": value}]}).encode()

    monkeypatch.setattr(embedding_client, "_transport", fake)
    openai = vector_store.OpenAISpecEmbedder("https://api.openai.com/v1", "m")
    other = vector_store.OpenAISpecEmbedder("http://localhost:11434/v1", "m")

    assert openai.embed(["q"]) == [[0.25, -0.5, 1.0]]
    assert other.embed(["q"]) == [[0.25, -0.5, 1.0]]
    assert sent[0]["encoding_format"] == "base64"
    assert "encoding_format" not in sent[1]


def test_lancedb_holds_its_table_and_still_sees_other_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-opening per query cost a tenth of the vector arm; holding the handle
    must not cost freshness -- a serving replica reads what the indexer wrote,
    including a table the indexer dropped and re-created."""

    pytest.importorskip(
        "lancedb", reason="lancedb not installed (pip install 'pheasant-kb[vector]')"
    )
    from pheasant.search.vector_store import LanceDBVectorStore

    writer = LanceDBVectorStore(tmp_path / "vectors")
    writer.upsert(["c1"], [[1.0, 0.0]], [{"source_id": "s1", "artifact_id": "a1"}])
    reader = LanceDBVectorStore(tmp_path / "vectors")
    assert [hit[0] for hit in reader.search([1.0, 0.0], 5)] == ["c1"]

    opened = {"n": 0}
    database = reader._database()
    original = database.open_table

    def counted(name: str, *args: Any, **kwargs: Any) -> Any:
        opened["n"] += 1
        return original(name, *args, **kwargs)

    monkeypatch.setattr(database, "open_table", counted)
    writer.upsert(["c2"], [[0.0, 1.0]], [{"source_id": "s1", "artifact_id": "a2"}])
    assert {hit[0] for hit in reader.search([0.0, 1.0], 5)} == {"c1", "c2"}
    assert opened["n"] == 0, "the held handle answered, and saw the other writer's commit"

    writer.reset()
    writer.upsert(["c3"], [[0.0, 1.0]], [{"source_id": "s1", "artifact_id": "a3"}])
    assert [hit[0] for hit in reader.search([0.0, 1.0], 5)] == ["c3"]


# ---------------------------------------------------------------------------
# A warm first query: one shared connection pool, and a quiet keep-warm beat
# ---------------------------------------------------------------------------


class _CountingServer:
    """An HTTP/1.1 server that remembers each request's client port."""

    def __init__(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        seen: list[tuple[str, str, int, str | None]] = []
        self.seen = seen

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                seen.append(
                    (
                        self.command,
                        self.path,
                        self.client_address[1],
                        self.headers.get("Authorization"),
                    )
                )
                body = b'{"data": []}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer  # noqa: N815 - http.server's spelling

            def log_message(self, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def counting_server():
    from pheasant.search import embedding_client

    embedding_client._POOL.clear()
    server = _CountingServer()
    try:
        yield server
    finally:
        server.close()
        embedding_client._POOL.clear()


def _post_from_a_new_thread(url: str) -> None:
    import threading
    from urllib.request import Request

    from pheasant.search.embedding_client import _transport

    worker = threading.Thread(
        target=lambda: _transport(Request(url, data=b"{}", method="POST"), 5.0)
    )
    worker.start()
    worker.join()


def test_a_connection_kept_alive_on_one_thread_serves_the_next(counting_server) -> None:
    """Per-thread connections meant a serving replica's request threads -- one
    of dozens -- rarely held a live one, so the first question after a quiet
    spell paid a TLS handshake past the query-embedding timeout."""

    _post_from_a_new_thread(counting_server.base + "/embeddings")
    _post_from_a_new_thread(counting_server.base + "/embeddings")
    ports = {port for _method, _path, port, _auth in counting_server.seen}
    assert len(counting_server.seen) == 2
    assert len(ports) == 1, "the second thread opened its own connection"


def test_a_connection_idle_too_long_is_replaced_not_reused(
    counting_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pheasant.search import embedding_client

    monkeypatch.setattr(embedding_client, "IDLE_REUSE_SECONDS", -1.0)
    _post_from_a_new_thread(counting_server.base + "/embeddings")
    _post_from_a_new_thread(counting_server.base + "/embeddings")
    assert len({port for *_rest, port, _auth in counting_server.seen}) == 2


def test_ping_asks_for_the_model_list_with_the_key(
    counting_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pheasant.search.embedding_client import OpenAISpecEmbedder

    monkeypatch.setenv("PING_TEST_KEY", "sk-test")
    embedder = OpenAISpecEmbedder(counting_server.base, "m", api_key_env="PING_TEST_KEY")
    assert embedder.ping() is True
    method, path, _port, auth = counting_server.seen[-1]
    assert (method, path, auth) == ("GET", "/models", "Bearer sk-test")


def test_a_keep_warm_search_moves_no_metric_and_no_salience(tmp_path: Path) -> None:
    from pheasant.config.schema import PheasantConfig
    from pheasant.search.hybrid import HybridSearch
    from pheasant.search.keep_warm import quiet_copy
    from pheasant.search.sqlite_store import SearchStore
    from pheasant.sync.engine import SyncEngine
    from pheasant.telemetry import metrics

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "note.md").write_text("# Note\n\nThe overview word appears here.\n", encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "warm",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [{"name": "docs", "type": "document_folder", "path": str(corpus)}],
        }
    )
    engine = SyncEngine(config)
    try:
        engine.sync_source("docs", "full")
        metrics.register_default_metrics(
            "test"
        )  # what create_app does; unregistered, nothing counts
        search = HybridSearch(SearchStore(engine.state), usage_tracking=True, stage_sample_rate=1.0)
        quiet = quiet_copy(search)

        def counted() -> float:
            return sum(
                metrics.REGISTRY.value("pheasant_retrieval_arm_total", arm=arm, outcome=outcome)
                or 0.0
                for arm in ("text", "vector", "graph")
                for outcome in ("ok", "empty", "failed")
            )

        before = counted()
        quiet.search_context(config.knowledge_base_id, "overview", mode="hybrid")
        assert counted() == before, "a keep-warm search moved the retrieval metrics"
        assert quiet.usage_tracking is False and quiet.stage_sample_rate == 0.0
        search.search_context(config.knowledge_base_id, "overview", mode="hybrid")
        assert counted() > before, "a real search stopped counting"
        assert search.usage_tracking is True, "the copy changed the original"
    finally:
        engine.close()
