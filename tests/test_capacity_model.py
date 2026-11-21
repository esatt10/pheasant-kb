"""Phase 35.7 — the measured capacity model, and the O(N²) it uncovered.

Half of this file guards a *performance* fix, which is unusual and deliberate.
`replace_artifact_chunks(fresh=True)` skips four DELETEs on the full-sync path
because the source was just emptied. If that assumption is ever wrong the
symptom is not a crash — it is duplicated chunks and duplicated FTS rows,
which show up as a search returning the same document twice and nothing else
noticing. So the tests here assert the *absence* of duplicates rather than
the presence of speed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pheasant.capacity import (
    NODES_PER_FILE,
    Projection,
    fleet_for,
    project,
    recommend_memory,
)
from pheasant.config.schema import PheasantConfig
from pheasant.sync.engine import SyncEngine


def _config(
    tmp_path: Path,
    *,
    state_name: str = "state",
    files: int = 4,
    graph_format: str = "rows",
) -> PheasantConfig:
    workspace = tmp_path / f"workspace-{state_name}"
    workspace.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        (workspace / f"note-{index}.md").write_text(
            f"# Note {index}\n\n"
            f"The deployment gateway rotates credentials nightly.\n"
            f"Marker{index} identifies this document uniquely.\n",
            encoding="utf-8",
        )
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "capacity",
                "state_path": str(tmp_path / state_name),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / f"{state_name}-exports"),
            },
            "storage": {"graph_snapshots": False, "graph_format": graph_format},
            "sources": [
                {
                    "name": "docs",
                    "type": "markdown_folder",
                    "path": str(workspace),
                    "include": ["**/*.md"],
                }
            ],
        }
    )


def _counts(engine: SyncEngine) -> dict[str, int]:
    def one(sql: str) -> int:
        rows = engine.state.rows(sql, ())
        return int(rows[0]["c"]) if rows else 0

    return {
        "artifacts": one("SELECT COUNT(*) AS c FROM artifacts"),
        "chunks": one("SELECT COUNT(*) AS c FROM chunks"),
        "fts": one("SELECT COUNT(*) AS c FROM chunks_fts"),
        "symbols": one("SELECT COUNT(*) AS c FROM symbols"),
        "terms": one("SELECT COUNT(*) AS c FROM artifact_terms"),
    }


# --------------------------------------------------------------------------
# The fresh-write fast path must not lose or duplicate anything
# --------------------------------------------------------------------------


def test_a_second_full_sync_does_not_duplicate_rows(tmp_path: Path) -> None:
    """The exact failure mode of a wrong `fresh=True`.

    A full sync clears the source and then writes every artifact as new. If
    the clear ever stopped happening, skipping the per-artifact DELETEs would
    double every chunk — silently, since nothing counts them.
    """

    engine = SyncEngine(_config(tmp_path, state_name="twice", files=6))
    try:
        engine.sync_source("docs", "full")
        first = _counts(engine)
        engine.sync_source("docs", "full")
        second = _counts(engine)
    finally:
        engine.close()

    assert first["artifacts"] == 6
    assert first["chunks"] > 0
    assert second == first, "a second full sync changed the row counts"


def test_search_returns_each_document_once_after_a_resync(tmp_path: Path) -> None:
    """A duplicated FTS row is invisible in `chunks` but not in results.

    This is the assertion that a row-count check alone would miss: `chunks`
    and `chunks_fts` are separate tables, and only one of them is what search
    reads.
    """

    from pheasant.search.hybrid import HybridSearch
    from pheasant.search.text_store import SearchStore

    engine = SyncEngine(_config(tmp_path, state_name="dedupe", files=5))
    try:
        engine.sync_source("docs", "full")
        engine.sync_source("docs", "full")
        searcher = HybridSearch(SearchStore(engine.state))
        results = searcher.search_context(engine.config.knowledge_base_id, "Marker3", "text", 10)[
            "results"
        ]
    finally:
        engine.close()

    paths = [result.get("relative_path") for result in results]
    assert paths.count("note-3.md") == 1, f"duplicate hits after a re-sync: {paths}"


def test_an_incremental_sync_still_replaces_changed_content(tmp_path: Path) -> None:
    """`fresh` must be False on the path that has something to replace.

    An incremental sync does *not* clear the source, so the DELETEs are
    load-bearing: without them an edited file's old chunks stay searchable
    forever.
    """

    from pheasant.search.hybrid import HybridSearch
    from pheasant.search.text_store import SearchStore

    config = _config(tmp_path, state_name="incremental", files=3)
    engine = SyncEngine(config)
    try:
        engine.sync_source("docs", "full")
        before = _counts(engine)

        target = Path(config.sources[0].path) / "note-1.md"
        target.write_text(
            "# Note 1\n\nCompletelyDifferentContent about failover drills.\n", encoding="utf-8"
        )
        engine.sync_source("docs", "incremental")
        after = _counts(engine)

        searcher = HybridSearch(SearchStore(engine.state))
        kb = engine.config.knowledge_base_id
        stale = searcher.search_context(kb, "Marker1", "text", 10)["results"]
        fresh = searcher.search_context(kb, "CompletelyDifferentContent", "text", 10)["results"]
    finally:
        engine.close()

    assert after["artifacts"] == before["artifacts"]
    assert not [hit for hit in stale if hit.get("relative_path") == "note-1.md"], (
        "the edited file's old content is still searchable"
    )
    assert [hit for hit in fresh if hit.get("relative_path") == "note-1.md"], (
        "the edited file's new content was not indexed"
    )


def test_repair_mode_does_not_take_the_fresh_path(tmp_path: Path) -> None:
    """Repair rewrites in place without clearing, so it must still delete."""

    engine = SyncEngine(_config(tmp_path, state_name="repair", files=4))
    try:
        engine.sync_source("docs", "full")
        before = _counts(engine)
        engine.sync_source("docs", "repair")
        after = _counts(engine)
    finally:
        engine.close()

    assert after == before, "repair duplicated rows"


def test_a_full_sync_takes_the_fresh_path_and_an_incremental_one_does_not(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Guards the *optimization*, which correctness tests cannot.

    Mutation testing found this gap: forcing `cleared = False` leaves every
    other test in this file green, because the result is correct — merely
    O(N²) again. So the mechanism is asserted directly, which is cheap and
    not flaky, rather than by timing a sync.
    """

    from pheasant.persistence.state_store import StateStore

    seen: list[bool] = []
    original = StateStore.replace_artifact_chunks

    def spy(self, artifact, chunks, *, fresh=False):  # type: ignore[no-untyped-def]
        seen.append(fresh)
        return original(self, artifact, chunks, fresh=fresh)

    monkeypatch.setattr(StateStore, "replace_artifact_chunks", spy)

    config = _config(tmp_path, state_name="fastpath", files=3)
    engine = SyncEngine(config)
    try:
        engine.sync_source("docs", "full")
        full_calls = list(seen)
        seen.clear()

        target = Path(config.sources[0].path) / "note-0.md"
        target.write_text("# Note 0\n\nEditedContent here.\n", encoding="utf-8")
        engine.sync_source("docs", "incremental")
        incremental_calls = list(seen)
    finally:
        engine.close()

    assert full_calls and all(full_calls), "a full sync did not use the fresh fast path"
    assert incremental_calls, "the incremental sync wrote nothing"
    assert not any(incremental_calls), "an incremental sync skipped the deletes it needs"


def test_the_fresh_flag_is_off_by_default(tmp_path: Path) -> None:
    """Callers that do not know must get the safe behavior.

    The fast path is an assertion about the caller's state; a writer that
    assumed it by default would corrupt any caller that had not cleared.
    """

    import inspect

    from pheasant.persistence.state_store import StateStore

    signature = inspect.signature(StateStore.replace_artifact_chunks)
    assert signature.parameters["fresh"].default is False
    assert signature.parameters["fresh"].kind is inspect.Parameter.KEYWORD_ONLY


# --------------------------------------------------------------------------
# The projection
# --------------------------------------------------------------------------


def test_a_projection_reports_every_axis_a_sizing_decision_needs(tmp_path: Path) -> None:
    projection = project(10_000, corpus_bytes=50 * 1024 * 1024)

    assert isinstance(projection, Projection)
    assert projection.nodes == int(10_000 * NODES_PER_FILE)
    assert projection.rss_bytes > 0
    assert projection.state_bytes > 0
    assert projection.index_seconds > 0
    assert projection.recommended_memory.endswith("Gi")
    assert projection.shards == 1


def test_a_projection_without_a_byte_count_reports_only_what_files_can_tell_it() -> None:
    """`scan` always has bytes; an API caller might not.

    The content half of state tracks bytes and is omitted rather than guessed.
    The *graph* half tracks file count, so once the graph moved into the
    database (35.10) it became knowable from files alone — and reporting zero
    there would understate a real cost by exactly the amount that decides
    whether a volume is big enough.
    """

    from pheasant.capacity import GRAPH_ROW_BYTES_PER_NODE

    projection = project(1_000)
    assert projection.state_bytes == projection.nodes * GRAPH_ROW_BYTES_PER_NODE
    assert projection.rss_bytes > 0


def test_chunks_are_projected_from_bytes_not_from_file_count() -> None:
    """A hundred large files and a hundred small ones are not the same corpus.

    Chunk count drives the vector-store projection, and it tracks content.
    Deriving it from file count would size the vectors for a corpus whose
    documents happen to be a convenient length — which is exactly what the
    benchmark fixture is, and exactly why its chunks-per-file figure was not
    adopted as a constant.
    """

    files = 1_000
    small = project(files, corpus_bytes=1_000 * 3_000, embedding_dimensions=768)
    large = project(files, corpus_bytes=1_000 * 30_000, embedding_dimensions=768)

    assert large.chunks > small.chunks * 5
    assert large.state_bytes > small.state_bytes * 5

    # And the floor holds: even a corpus of empty files has one chunk each.
    assert project(500, corpus_bytes=10).chunks == 500


def test_embeddings_add_vector_bytes_and_a_stated_unknown() -> None:
    """Index time under a real embedder is the provider's rate limit, not ours.

    Recorded as a warning rather than a fabricated multiplier — the number
    would be about someone else's service.
    """

    without = project(5_000, corpus_bytes=10 * 1024 * 1024)
    with_vectors = project(5_000, corpus_bytes=10 * 1024 * 1024, embedding_dimensions=1536)

    assert with_vectors.state_bytes > without.state_bytes
    assert any("embedding" in warning for warning in with_vectors.warnings)
    assert not without.warnings


def test_an_oversized_corpus_is_told_to_shard() -> None:
    projection = project(500_000)

    assert projection.shards > 1
    assert any("shard plan" in warning for warning in projection.warnings)


def test_recommended_memory_has_headroom_and_lands_on_a_real_size() -> None:
    """50% over on a request is cheap; 10% under is an OOM kill mid-sync."""

    ladder = {"0.5Gi", "1Gi", "2Gi", "4Gi", "6Gi", "8Gi", "12Gi", "16Gi", "24Gi", "32Gi", "48Gi"}
    for files in (100, 1_000, 10_000, 50_000, 150_000):
        projection = project(files)
        assert projection.recommended_memory in ladder
        headroom = float(projection.recommended_memory.rstrip("Gi")) * (1024**3)
        assert headroom >= projection.rss_bytes


def test_the_fleet_recommendation_matches_the_shard_count() -> None:
    small = fleet_for(5_000)
    assert small["shards"] == 1
    assert small["indexers"] == 1
    assert small["single_container_is_enough"] is True
    # No workers for a small corpus: a worker tier that indexes nothing is
    # pure cost, and 8 file workers were measured at 1.113x anyway.
    assert small["workers"] == 0

    large = fleet_for(600_000)
    assert large["shards"] > 1
    assert large["indexers"] == large["shards"]
    assert large["workers"] > 0
    assert large["single_container_is_enough"] is False


def test_recommend_memory_is_monotonic() -> None:
    """A bigger corpus must never recommend a smaller container."""

    sizes = [recommend_memory(project(files).rss_bytes) for files in (1_000, 10_000, 100_000)]
    values = [float(size.rstrip("Gi")) for size in sizes]
    assert values == sorted(values)


#: Points measured by `python -m pheasant.sync.benchmark --mode capacity`,
#: re-run on `storage.graph_format: rows` (35.10) because the graph now lives
#: in the database and the earlier points did not include it: at 4,000 files
#: state went from 86.6MB to 106.0MB, and 19.4MB of that is 12,003 graph nodes
#: at the measured `GRAPH_ROW_BYTES_PER_NODE` — which is the coefficient
#: predicting the difference to within 2%, on a run it was not fitted to.
#:
#: `graph_nodes` is carried because the synthetic fixture produces **3.0**
#: nodes per file where a real corpus produces 6.3 (see `NODES_PER_FILE`).
#: Projecting from `files` here would compare the model's estimate of this
#: fixture's structure against the fixture, which measures the estimate and
#: not the coefficient under test.
#: (files, corpus_bytes, seconds, state_bytes, graph_nodes)
MEASURED = [
    (4000, 21_826_890, 10.37, 106_004_480, 12_003),
    (8000, 43_654_890, 21.23, 213_090_304, 24_003),
]


@pytest.mark.parametrize(
    ("files", "corpus_bytes", "seconds", "state_bytes", "graph_nodes"), MEASURED
)
def test_the_projection_matches_what_was_actually_measured(
    files: int, corpus_bytes: int, seconds: float, state_bytes: int, graph_nodes: int
) -> None:
    """The constants are measurements, so drift in either direction is a bug.

    Tolerances are wide enough to survive a different machine's clock but
    narrow enough that a changed constant fails here rather than quietly
    producing worse advice. Measured error at the largest point was 0.4% on
    disk.
    """

    projection = project(files, corpus_bytes, nodes=graph_nodes)

    disk_error = abs(projection.state_bytes - state_bytes) / state_bytes
    assert disk_error < 0.15, f"state projection off by {disk_error:.0%}"

    # Time is hardware-dependent in a way disk is not, so this only asserts
    # the right order of magnitude — enough to catch a constant that moved by
    # a factor, which is the failure that matters.
    assert 0.25 < projection.index_seconds / seconds < 4.0


def test_index_time_is_projected_as_linear_in_file_count() -> None:
    """It was not, until 35.7 removed an O(N²) — so the shape is pinned.

    Before the fix, doubling the corpus roughly tripled the time (measured
    3.85 → 20.58 seconds per 1k files over 500 → 8,000 files). Anyone quoting
    a per-file figure from a small corpus was extrapolating a curve as though
    it were a line. If a superlinear cost is ever reintroduced, this
    assertion is still true and the *measurement* test above is what fails —
    which is why both exist.
    """

    small = project(1_000).index_seconds
    large = project(100_000).index_seconds
    assert large == pytest.approx(small * 100, rel=1e-6)


def test_scan_reports_a_projection(tmp_path: Path) -> None:
    """`scan --json` and the UI read this key, so it is part of the contract.

    Found missing by mutation testing after a harness bug was fixed: renaming
    the key changed nothing, because the CLI falls back to computing the
    projection itself. That fallback is right — but it meant the *reported*
    projection had no test at all.
    """

    engine = SyncEngine(_config(tmp_path, state_name="scan", files=5))
    try:
        report = engine.scan_source("docs")
    finally:
        engine.close()

    projection = report["projection"]
    assert projection["files"] == 5
    assert projection["graph_nodes"] > 0
    assert projection["projected_state_gb"] >= 0
    assert projection["recommended_memory"].endswith("Gi")
    # Bytes come from the walk, so the disk projection is real here rather
    # than the zero a file-count-only caller would get.
    assert projection["corpus_bytes"] > 0
    assert projection["projected_state_bytes"] > 0


def test_scan_projects_vectors_only_when_embeddings_are_on(tmp_path: Path) -> None:
    """The projection reflects *this* config, not a generic one."""

    config = _config(tmp_path, state_name="scan-vec", files=5)
    engine = SyncEngine(config)
    try:
        without = engine.scan_source("docs")["projection"]
    finally:
        engine.close()

    config.search.embeddings.enabled = True
    config.search.embeddings.dimensions = 1536
    engine = SyncEngine(config)
    try:
        with_vectors = engine.scan_source("docs")["projection"]
    finally:
        engine.close()

    assert with_vectors["projected_state_bytes"] > without["projected_state_bytes"]


# --------------------------------------------------------------------------
# The benchmark that produced the constants
# --------------------------------------------------------------------------


def test_the_capacity_sweep_runs_and_reports_every_axis() -> None:
    """Small sizes: this is a smoke test of the harness, not a measurement."""

    from pheasant.sync.benchmark import run_capacity

    report = run_capacity(sizes=(20, 40), lines=10)

    assert [point["files"] for point in report["points"]] == [20, 40]
    for point in report["points"]:
        for key in (
            "seconds_per_1k_files",
            "nodes_per_file",
            "chunks_per_file",
            "state_bytes",
            "state_breakdown_bytes",
        ):
            assert point[key] is not None, f"{key} missing"
    assert report["caveats"], "a sweep with a synthetic fixture must state its caveats"
    assert report["model"]["nodes_per_file"] == NODES_PER_FILE
