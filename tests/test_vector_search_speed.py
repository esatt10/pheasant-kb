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


def _until(predicate, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "it never happened"
        time.sleep(0.05)


def test_the_first_search_does_not_wait_for_the_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first query after a restart used to load the whole table inside the
    request: 37.9 s on the lab. It is answered from disk now, exactly, while
    the copy loads behind it."""

    load = ResidentVectors._load

    def slow_load(self, table, version):  # a load slower than any query
        time.sleep(1.0)
        load(self, table, version)

    monkeypatch.setattr(ResidentVectors, "_load", slow_load)
    store = _store(tmp_path, resident=True)
    query = QUERIES[3]
    assert store._resident._snapshot is None
    started = time.monotonic()
    first = store.search(query, 10)
    assert time.monotonic() - started < 0.8, "the first search waited for the load"
    assert [cid for cid, _, _ in first] == [cid for cid, _ in _exact(store, query, 10)]
    _until(lambda: store._resident._snapshot is not None)
    assert [cid for cid, _, _ in store.search(query, 10)] == [cid for cid, _, _ in first]


def test_a_background_reload_serves_the_held_copy_until_it_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pheasant.search import vector_resident

    monkeypatch.setattr(vector_resident, "RELOAD_MIN_SECONDS", 0.0)
    store = _store(tmp_path, resident=True)
    query = QUERIES[1]
    store.search(query, 5)
    _until(lambda: store._resident._snapshot is not None)
    first = store.search(query, 5)  # from the held copy
    store.upsert(["chunk:new"], [query], [{"source_id": "s0", "artifact_id": "file:new"}])
    _until(lambda: store.search(query, 5)[0][0] == "chunk:new")
    assert first[0][0] != "chunk:new"


def test_reloads_are_throttled_while_the_table_keeps_changing(tmp_path: Path) -> None:
    """An indexing source writes a version every few seconds; one reload per
    window, serving the held copy in between, rather than one per write."""

    store = _store(tmp_path, resident=True)
    query = QUERIES[4]
    store.search(query, 5)
    _until(lambda: store._resident._snapshot is not None)
    held = store._resident._snapshot.version
    for n in range(3):
        store.upsert([f"chunk:burst{n}"], [query], [{"source_id": "s0", "artifact_id": "f"}])
        store.search(query, 5)
    time.sleep(0.3)
    assert store._resident._snapshot.version == held, "a write inside the window reloaded"
    assert store._resident._loading is None


def test_warm_loads_without_a_query_and_keeps_the_copy_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pheasant.search import vector_resident

    monkeypatch.setattr(vector_resident, "RELOAD_MIN_SECONDS", 0.0)
    monkeypatch.setattr(vector_resident, "REFRESH_SECONDS", 0.05)
    store = _store(tmp_path, resident=True)
    store.warm()
    _until(lambda: store._resident._snapshot is not None)
    before = store._resident._snapshot.version
    store.upsert(["chunk:later"], [QUERIES[5]], [{"source_id": "s0", "artifact_id": "f"}])
    _until(lambda: store._resident._snapshot.version > before)  # no search needed


def _rows_by_id(snapshot) -> dict[str, list[float]]:
    """What a query can see: each live id's vector (dead rows are not live)."""

    return {
        chunk_id: snapshot.matrix[i].round(6).tolist() for chunk_id, i in snapshot.position.items()
    }


def _payloads_by_id(snapshot) -> dict[str, str]:
    return {chunk_id: snapshot.payloads[i] for chunk_id, i in snapshot.position.items()}


def test_a_new_version_is_applied_as_a_delta_equal_to_a_full_read(tmp_path: Path) -> None:
    """Content-addressed ids make an id in both versions the same vector, so
    added and removed ids are the whole change -- and the result has to be
    exactly what reading the table again would give."""

    from pheasant.search import vector_resident

    store = _store(tmp_path, n=300)
    table = store._table()
    held = vector_resident._full(table, int(table.version))
    fresh = _vectors(5, seed=11).tolist()
    store.upsert(
        [f"chunk:added{n}" for n in range(5)],
        fresh,
        [{"source_id": "s0", "artifact_id": f"file:a{n}"} for n in range(5)],
    )
    store.delete([held.ids[0], held.ids[7]])
    table = store._table()
    delta = vector_resident._delta(table, int(table.version), held)
    full = vector_resident._full(table, int(table.version))
    assert delta is not None, "a small change fell back to a full read"
    assert set(delta.position) == set(full.position)
    assert _rows_by_id(delta) == _rows_by_id(full)
    assert _payloads_by_id(delta) == _payloads_by_id(full)
    # Appended, not copied: the kept rows are the very buffer `held` holds.
    assert delta.matrix is held.matrix
    assert delta.n == held.n + 5 and delta.live == held.live - 2 + 5


def test_a_removed_row_is_never_returned_while_it_still_has_its_slot(tmp_path: Path) -> None:
    """A delta marks a removed row dead rather than compacting it away, so the
    search has to skip it -- even when it is the best match there is."""

    from pheasant.search import vector_resident

    store = _store(tmp_path, n=200)
    table = store._table()
    held = vector_resident._full(table, int(table.version))
    target = held.ids[3]
    query = held.matrix[held.position[target]].tolist()
    store.delete([target])
    table = store._table()
    resident = vector_resident.ResidentVectors(10**9, background=False)
    resident._snapshot = held
    resident._last_load = None
    hits = resident.search(table, query, 5)
    assert resident._snapshot.matrix is held.matrix, "the removal was not applied as a delta"
    assert target not in [chunk_id for chunk_id, _score, _payload in hits]
    assert len(hits) == 5


def test_a_delta_that_would_overrun_the_spare_capacity_reads_in_full(tmp_path: Path) -> None:
    from pheasant.search import vector_resident

    store = _store(tmp_path, n=50)
    table = store._table()
    held = vector_resident._full(table, int(table.version))
    tight = vector_resident._Snapshot(
        held.version, held.ids, held.payloads, held.matrix[: held.n], held.n
    )  # no spare rows at all
    store.upsert(["chunk:one-more"], [QUERIES[0]], [{"source_id": "s0", "artifact_id": "f"}])
    table = store._table()
    assert vector_resident._delta(table, int(table.version), tight) is None


def test_a_change_too_large_for_a_delta_is_read_in_full(tmp_path: Path) -> None:
    from pheasant.search import vector_resident

    store = _store(tmp_path, n=40)
    table = store._table()
    held = vector_resident._full(table, int(table.version))
    store.upsert(
        [f"chunk:many{n}" for n in range(30)],
        _vectors(30, seed=5).tolist(),
        [{"source_id": "s0", "artifact_id": "f"} for _ in range(30)],
    )
    table = store._table()
    assert vector_resident._delta(table, int(table.version), held) is None


def test_a_loading_thread_lowers_its_own_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from pheasant.search import vector_resident

    asked: list[tuple] = []
    monkeypatch.setattr(os, "PRIO_PROCESS", 0, raising=False)
    monkeypatch.setattr(os, "setpriority", lambda *a: asked.append(a), raising=False)
    store = _store(tmp_path, resident=True)
    store.search(QUERIES[0], 5)
    _until(lambda: store._resident._snapshot is not None)
    assert asked and asked[0][2] == vector_resident.LOAD_NICENESS


def test_the_fast_decode_matches_the_old_one() -> None:
    import pyarrow as pa

    from pheasant.search.vector_resident import _unit_rows

    raw = np.vstack([_vectors(5, seed=3), np.zeros((1, 16), dtype=np.float32)])
    flat = pa.FixedSizeListArray.from_arrays(pa.array(raw.ravel(), pa.float32()), 16)
    old = raw.astype(np.float32).copy()
    norms = np.linalg.norm(old, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    old /= norms
    new = _unit_rows(flat, len(raw))
    assert new.flags.writeable and new.dtype == np.float32
    assert np.allclose(new, old, atol=1e-6) and not new[-1].any()


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
