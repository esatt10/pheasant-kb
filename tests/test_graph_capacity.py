"""Acceptance tests for Phase 35.3 — measured graph capacity and its guardrail.

The point of this step is that the recommendation is a *number*. Phase 34.7
asked the same question, measured two scales, and concluded "not a memory
problem" — true at 10x the demo corpus and false at 100x. These tests pin the
properties the recommendation rests on, not the constants themselves, which
are hardware-dependent and belong in the doc.
"""

from __future__ import annotations

from pathlib import Path

from pheasant.config.schema import PheasantConfig
from pheasant.graph import capacity
from pheasant.sync.engine import SyncEngine


def test_the_generated_graph_has_the_shape_a_real_corpus_produces() -> None:
    """A fixture that is not representative measures nothing useful."""

    graph = capacity.build_graph(1_000)
    assert graph.number_of_nodes() > 0
    # ~6.3 nodes per file, the live demo-corpus ratio, within rounding.
    assert 6.0 <= graph.number_of_nodes() / 1_000 <= 6.5
    types = {attrs.get("type") for _id, attrs in graph.nodes(data=True)}
    assert {"chunk", "symbol", "file"} <= types
    # Attribute payloads dominate the persisted bytes; a fixture with bare ids
    # would under-report the graph's real size by an order of magnitude.
    _node_id, attrs = next(iter(graph.nodes(data=True)))
    assert attrs.get("relative_path") and attrs.get("summary")


def test_cost_per_node_is_flat_which_is_what_makes_the_table_interpolatable(
    tmp_path: Path,
) -> None:
    """The doc tells operators to interpolate between measured rows. That is
    only honest if the per-node cost really is flat."""

    small = capacity.measure(500, tmp_path)
    large = capacity.measure(5_000, tmp_path)

    small_per_node = small["graph_bytes"] / small["nodes"]
    large_per_node = large["graph_bytes"] / large["nodes"]
    assert 0.7 <= large_per_node / small_per_node <= 1.4, (
        f"per-node cost is not flat: {small_per_node:.0f} -> {large_per_node:.0f} bytes"
    )
    # And the absolute numbers are sane rather than accidentally near-zero.
    assert small["nodes"] > 0 and small["edges"] > 0
    assert large["compressed_bytes"] < large["json_bytes"], "compression did nothing"
    assert large["load_seconds"] >= 0


def _config(tmp_path: Path, workspace: Path, max_nodes: int | None) -> PheasantConfig:
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "capacity",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "graph": {"max_nodes": max_nodes},
            "sync": {"watcher": {"enabled": False}, "scheduler": {"enabled": False}},
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


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "docs"
    workspace.mkdir(parents=True)
    for index in range(4):
        (workspace / f"note-{index}.md").write_text(
            f"# Note {index}\n\nBody about gateways and retries.\n", encoding="utf-8"
        )
    return workspace


def test_a_graph_past_max_nodes_reports_capacity_advice(tmp_path: Path) -> None:
    """A threshold of 1 forces the notice on a tiny corpus."""

    workspace = _workspace(tmp_path)
    engine = SyncEngine(_config(tmp_path, workspace, max_nodes=1))
    try:
        result = engine.sync_source("docs", "full")
    finally:
        engine.close()

    notice = result.details.get("capacity")
    assert notice is not None, "no capacity notice past the threshold"
    assert notice["nodes"] == result.graph_nodes
    assert notice["max_nodes"] == 1
    # Exact bytes, because the rounded GB figure is 0.0 for a tiny graph.
    assert notice["projected_rss_bytes"] > 0
    assert notice["projected_rss_gb"] >= 0
    assert "region" in notice["advice"]
    # A notice, never a refusal: the index still happened.
    assert result.status == "healthy"
    assert result.indexed_artifacts == 4


def test_a_graph_within_budget_says_nothing(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    engine = SyncEngine(_config(tmp_path, workspace, max_nodes=1_000_000))
    try:
        result = engine.sync_source("docs", "full")
    finally:
        engine.close()
    assert "capacity" not in result.details


def test_the_guardrail_can_be_switched_off(tmp_path: Path) -> None:
    """`null` disables it — an operator who has read the doc and accepted the
    cost should not be nagged every sync."""

    workspace = _workspace(tmp_path)
    engine = SyncEngine(_config(tmp_path, workspace, max_nodes=None))
    try:
        result = engine.sync_source("docs", "full")
    finally:
        engine.close()
    assert "capacity" not in result.details


def test_the_default_threshold_matches_the_published_table() -> None:
    """The doc and the default must not drift apart.

    1.5M nodes is ~240,000 files at the measured 6.3 nodes/file, which is the
    row `capacity-planning.md` marks as a full 6 Gi container — the limit the
    shipped manifests set. If any of the three moves without the others, the
    warning fires at a size the doc calls fine, or never fires before the pod
    is OOM-killed.
    """

    default = PheasantConfig().graph.max_nodes
    assert default == 1_500_000
    files_at_threshold = default / capacity.NODES_PER_FILE
    assert 230_000 <= files_at_threshold <= 250_000

    doc = Path("docs/how-to/capacity-planning.md").read_text(encoding="utf-8")
    assert "1.5M nodes" in doc
    assert "240,000 files" in doc


# ---------------------------------------------------------------------------
# Checkpoint scheduling (Phase 35.4)
# ---------------------------------------------------------------------------


def test_checkpoint_interval_follows_youngs_formula() -> None:
    """`T = sqrt(2 * C * MTBF)` — the standard HPC/ML-training result.

    The rule it replaced was `max(60s, last_save * 10)`, which held overhead at
    a flat **10%** however large the graph got. Young's formula minimizes the
    *sum* of overhead and post-crash rework instead of either alone, and lands
    near 1% on pheasant's measured save costs.
    """

    import math

    from pheasant.sync.engine import optimal_checkpoint_interval

    for save in (0.13, 1.32, 6.71, 19.91):
        interval = optimal_checkpoint_interval(
            save, mtbf_seconds=86_400, floor_seconds=60, ceiling_seconds=100_000
        )
        assert abs(interval - math.sqrt(2 * save * 86_400)) < 1.0
        # The property that matters: overhead is small at every scale, where
        # the old rule pinned it at 10%.
        assert save / interval < 0.05


def test_a_shorter_mtbf_checkpoints_more_often() -> None:
    """Spot/preemptible instances are interrupted more, so they should
    checkpoint more — the formula must respond to that, not ignore it."""

    from pheasant.sync.engine import optimal_checkpoint_interval

    stable = optimal_checkpoint_interval(
        10.0, mtbf_seconds=86_400, floor_seconds=60, ceiling_seconds=100_000
    )
    spot = optimal_checkpoint_interval(
        10.0, mtbf_seconds=3_600, floor_seconds=60, ceiling_seconds=100_000
    )
    assert spot < stable


def test_the_interval_is_bounded_at_both_ends() -> None:
    """The floor keeps a trivially cheap save from checkpointing constantly;
    the ceiling bounds how much indexing a crash can throw away."""

    from pheasant.sync.engine import optimal_checkpoint_interval

    assert (
        optimal_checkpoint_interval(
            0.001, mtbf_seconds=86_400, floor_seconds=60, ceiling_seconds=1_800
        )
        == 60
    )
    assert (
        optimal_checkpoint_interval(
            600.0, mtbf_seconds=86_400, floor_seconds=60, ceiling_seconds=1_800
        )
        == 1_800
    )


def test_it_degrades_to_the_pre_35_4_behaviour_when_it_cannot_compute() -> None:
    """No measured save yet (the first checkpoint of a fresh graph), or MTBF
    switched off, must fall back to the plain floor rather than to zero —
    returning 0 would checkpoint on every artifact."""

    from pheasant.sync.engine import optimal_checkpoint_interval

    assert (
        optimal_checkpoint_interval(
            0.0, mtbf_seconds=86_400, floor_seconds=60, ceiling_seconds=1_800
        )
        == 60
    )
    assert (
        optimal_checkpoint_interval(10.0, mtbf_seconds=0, floor_seconds=60, ceiling_seconds=1_800)
        == 60
    )
