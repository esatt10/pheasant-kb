"""The vector arm's two speed paths: a resident exact scan, and an optional ANN index.

Both are off by default, and the default path is asserted unchanged: the
store answers what a plain LanceDB exact search answers, and builds nothing.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("lancedb", reason="lancedb not installed (pip install 'pheasant-kb[vector]')")

from pheasant.config.schema import VectorStoreSettings  # noqa: E402
from pheasant.search import vector_ann  # noqa: E402
from pheasant.search.vector_resident import ResidentVectors  # noqa: E402
from pheasant.search.vector_store import LanceDBVectorStore  # noqa: E402

DIM = 16


def _vectors(n: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    centers = rng.standard_normal((12, DIM))
    return centers[rng.integers(0, 12, n)] + 0.3 * rng.standard_normal((n, DIM))


def _store(tmp_path: Path, n: int = 600, **settings) -> LanceDBVectorStore:
    store = LanceDBVectorStore(tmp_path / "vectors", VectorStoreSettings(**settings))
    vectors = _vectors(n)
    ids = [f"chunk:{i:04d}" for i in range(n)]
    payloads = [{"source_id": f"s{i % 3}", "artifact_id": f"file:{i // 5}"} for i in range(n)]
    store.upsert(ids, vectors.tolist(), payloads)
    return store


def _exact(store: LanceDBVectorStore, query: list[float], k: int) -> list[tuple]:
    """What LanceDB itself answers with no index, no projection, no residency."""

    table = store._table()
    hits = table.search(query).distance_type("cosine").limit(k).bypass_vector_index().to_list()
    return [(h["chunk_id"], 1.0 - float(h["_distance"])) for h in hits]


QUERIES = _vectors(8, seed=7).tolist()


def test_the_default_store_answers_exactly_and_builds_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.flush()
    assert store._resident is None
    assert not [i for i in store._table().list_indices() if "vector" in i.columns]
    for query in QUERIES:
        got = store.search(query, 25)
        assert [(cid, round(sim, 5)) for cid, sim, _ in got] == [
            (cid, round(sim, 5)) for cid, sim in _exact(store, query, 25)
        ]
        # Projected, not truncated: the payload a caller reads is all there.
        assert set(got[0][2]) == {"source_id", "artifact_id"}


def test_a_resident_copy_returns_the_exact_top_k(tmp_path: Path) -> None:
    store = _store(tmp_path, resident=True)
    for query in QUERIES:
        for k in (1, 10, 50):
            got = store.search(query, k)
            want = _exact(store, query, k)
            assert [cid for cid, _, _ in got] == [cid for cid, _ in want]
            assert np.allclose([s for _, s, _ in got], [s for _, s in want], atol=1e-5)
    assert store._resident._snapshot is not None, "answered from disk, not from memory"


def test_a_resident_copy_follows_the_table_version(tmp_path: Path) -> None:
    store = _store(tmp_path, resident=True)
    store._resident.background = False
    query = QUERIES[0]
    store.search(query, 5)
    store.upsert(["chunk:new"], [query], [{"source_id": "s0", "artifact_id": "file:new"}])
    assert store.search(query, 5)[0][0] == "chunk:new"


def test_a_background_reload_serves_the_held_copy_until_it_lands(tmp_path: Path) -> None:
    store = _store(tmp_path, resident=True)
    query = QUERIES[1]
    first = store.search(query, 5)  # nothing held yet: the first load blocks
    store.upsert(["chunk:new"], [query], [{"source_id": "s0", "artifact_id": "file:new"}])
    deadline = time.monotonic() + 10
    while store.search(query, 5)[0][0] != "chunk:new":
        assert time.monotonic() < deadline, "the reload never landed"
        time.sleep(0.05)
    assert first[0][0] != "chunk:new"


def test_a_table_over_the_cap_is_searched_on_disk(tmp_path: Path) -> None:
    store = _store(tmp_path, resident=True, resident_max_mb=0)
    query = QUERIES[2]
    assert [cid for cid, _, _ in store.search(query, 10)] == [
        cid for cid, _ in _exact(store, query, 10)
    ]
    assert store._resident._snapshot is None


def test_growing_past_the_cap_drops_the_older_copy(tmp_path: Path) -> None:
    resident = ResidentVectors(10**9, background=False)
    store = _store(tmp_path, n=50)
    table = store._table()
    assert resident.search(table, QUERIES[0], 3) is not None
    resident.max_bytes = 0
    store.upsert(["chunk:more"], [QUERIES[0]], [{"source_id": "s0", "artifact_id": "f"}])
    assert resident.search(store._table(), QUERIES[0], 3) is None


@pytest.mark.parametrize("kind", ["ivf_hnsw_sq", "ivf_pq"])
def test_the_writer_builds_and_drops_the_configured_index(tmp_path: Path, kind: str) -> None:
    store = _store(tmp_path, ann_index=kind, ann_min_rows=0, ann_nprobes=64)
    store.flush()
    indexed = [i for i in store._table().list_indices() if "vector" in i.columns]
    assert len(indexed) == 1
    got = store.search(QUERIES[0], 10)
    assert len(got) == 10
    # Approximate, but on twelve tight clusters it should find most of the truth.
    truth = {cid for cid, _ in _exact(store, QUERIES[0], 10)}
    assert len(truth & {cid for cid, _, _ in got}) >= 6

    # Switching it off: the next query bypasses the index, the next flush drops it.
    off = LanceDBVectorStore(tmp_path / "vectors", VectorStoreSettings())
    assert [cid for cid, _, _ in off.search(QUERIES[0], 10)] == [
        cid for cid, _ in _exact(off, QUERIES[0], 10)
    ]
    off.flush()
    assert not [i for i in off._table().list_indices() if "vector" in i.columns]


def test_no_index_is_built_below_the_row_floor(tmp_path: Path) -> None:
    settings = VectorStoreSettings(ann_index="ivf_hnsw_sq", ann_min_rows=10_000)
    store = _store(tmp_path, ann_index="ivf_hnsw_sq", ann_min_rows=10_000)
    assert vector_ann.reconcile(store._table(), settings) == "below_min_rows"


def test_a_current_index_is_left_alone_and_a_stale_one_rebuilt(tmp_path: Path) -> None:
    settings = VectorStoreSettings(ann_index="ivf_hnsw_sq", ann_min_rows=0)
    store = _store(tmp_path, ann_index="ivf_hnsw_sq", ann_min_rows=0)
    assert vector_ann.reconcile(store._table(), settings) == "built"
    assert vector_ann.reconcile(store._table(), settings) == "current"
    more = _vectors(200, seed=3)
    store.upsert([f"chunk:x{i}" for i in range(200)], more.tolist(), [{}] * 200)
    assert vector_ann.reconcile(store._table(), settings) == "built"
    switched = replace(settings, ann_index="ivf_pq")
    assert vector_ann.reconcile(store._table(), switched) == "built"


def test_an_unknown_index_kind_is_refused_at_construction(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ann_index"):
        LanceDBVectorStore(tmp_path / "v", VectorStoreSettings(ann_index="hnsw"))


def test_hnsw_is_one_graph_until_training_it_would_not_fit() -> None:
    # Each partition is a separate graph a query walks; below the per-partition
    # ceiling one graph was both faster and as accurate (vector_ann's table).
    per = vector_ann.HNSW_ROWS_PER_PARTITION
    for rows, partitions in [(600, 1), (60_000, 1), (per, 1), (per + 1, 2), (10 * per, 10)]:
        options = vector_ann._build_options("ivf_hnsw_sq", rows, 1536)
        assert options == {"index_type": "IVF_HNSW_SQ", "num_partitions": partitions}


def test_the_index_is_built_without_the_deprecated_keywords(tmp_path: Path) -> None:
    import warnings

    store = _store(tmp_path, ann_index="ivf_hnsw_sq", ann_min_rows=0)
    table = store._table()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert vector_ann.reconcile(table, store.settings) == "built"
    assert not [w for w in caught if "create_index" in str(w.message)]
