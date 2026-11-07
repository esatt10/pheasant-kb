"""Acceptance tests for Phase 35.4 — per-source leases and shard planning.

Two things this step exists to make true:

1. **Two indexers can write two different sources at once**, which is what the
   35.2 Postgres seam was for. One knowledge base, one writer process, was the
   ceiling.
2. **A split is a proposal you can read**, not a manual arithmetic exercise.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.persistence.backends import PostgresBackend
from pheasant.persistence.state_store import StateStore
from pheasant.registry.source_registry import SourceRegistry
from pheasant.sharding import SourceSize, plan_shards, render_plan
from pheasant.sync.locks import SourceLease, SourceLeaseError

DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()
postgres = pytest.mark.skipif(not DSN, reason="set PHEASANT_TEST_POSTGRES_DSN to run")


# ---------------------------------------------------------------------------
# Shard planning — offline, no database
# ---------------------------------------------------------------------------


def test_a_split_balances_by_packing_whole_sources() -> None:
    """Whole sources, never split, because cross-source edges live inside one.

    Hashing paths across shards balances perfectly and severs every
    `references`/`imports` edge — even balance is the wrong objective when the
    thing being balanced is a graph.
    """

    # Named so that alphabetical order is *ascending* size. A fixture whose
    # names happen to sort by descending size makes "largest-first" and "in
    # whatever order they arrived" indistinguishable — mutation testing caught
    # exactly that, and the first version of this test passed with the sort
    # replaced by name order.
    plan = plan_shards(
        [
            SourceSize("a", 3_000),
            SourceSize("b", 3_000),
            SourceSize("c", 3_000),
            SourceSize("d", 4_000),
            SourceSize("e", 5_000),
        ],
        shards=2,
    )
    assert plan["shard_count"] == 2
    placed = [name for shard in plan["shards"] for name in shard["sources"]]
    assert sorted(placed) == ["a", "b", "c", "d", "e"], "a source was dropped or duplicated"

    # The property LPT buys is balance. Ascending order gives 11k/7k here;
    # largest-first gives 10k/8k.
    sizes = sorted(shard["files"] for shard in plan["shards"])
    assert sizes[-1] - sizes[0] <= 2_000, f"poorly balanced split: {sizes}"
    assert sum(sizes) == 18_000

    # And whole sources, never split across regions.
    for shard in plan["shards"]:
        assert shard["files"] == sum(
            size
            for name, size in {"a": 3_000, "b": 3_000, "c": 3_000, "d": 4_000, "e": 5_000}.items()
            if name in shard["sources"]
        )


def test_the_shard_count_is_derived_from_the_node_budget() -> None:
    """Without an explicit count, pick the fewest regions that fit."""

    plan = plan_shards(
        [SourceSize(f"repo-{index}", 100_000) for index in range(6)],
        max_nodes_per_shard=1_500_000,
    )
    # 600k files -> 3.78M nodes; at 1.5M per shard that needs 3.
    assert plan["total_nodes"] == int(600_000 * 6.3)
    assert plan["shard_count"] == 3
    assert all(shard["nodes"] <= 1_500_000 for shard in plan["shards"])


def test_one_oversized_source_is_reported_not_silently_split() -> None:
    """No arrangement of whole sources fixes a single source over budget. The
    planner has to say so rather than propose a split it cannot honour."""

    plan = plan_shards([SourceSize("monorepo", 500_000)], max_nodes_per_shard=1_500_000)
    assert len(plan["shards"]) == 1
    assert plan["warnings"], "an over-budget source produced no warning"
    warning = plan["warnings"][0]
    assert "monorepo" in warning
    assert "include/exclude" in warning or "depth" in warning


def test_asking_for_more_shards_than_sources_says_so() -> None:
    plan = plan_shards([SourceSize("only", 1_000)], shards=4)
    assert len(plan["shards"]) == 1
    assert plan["shard_count"] == 1


def test_memory_recommendations_carry_headroom_and_round_to_real_sizes() -> None:
    """A 10% underestimate is an OOM kill mid-sync; a 50% overestimate is
    cheap. The recommendation is deliberately generous."""

    plan = plan_shards([SourceSize("repo", 240_000)], shards=1)
    shard = plan["shards"][0]
    assert shard["projected_rss_bytes"] > shard["nodes"] * 2400
    assert shard["recommended_memory"].endswith("Gi")
    # Comfortably above the raw projection, and a size a human would type.
    recommended_gib = float(shard["recommended_memory"].removesuffix("Gi"))
    assert recommended_gib >= shard["projected_rss_bytes"] / 1024**3


def test_the_rendered_plan_names_regions_sources_and_memory() -> None:
    text = render_plan(plan_shards([SourceSize("a", 9_000), SourceSize("b", 3_000)], shards=2))
    assert "shard-1" in text and "shard-2" in text
    assert "- a" in text and "- b" in text
    assert "Gi" in text
    assert "router" in text


def test_planning_an_empty_corpus_is_not_an_error() -> None:
    plan = plan_shards([SourceSize("empty", 0)])
    assert plan["shards"] == []
    assert plan["warnings"]
    assert "Nothing to plan" in render_plan(plan)


def test_cli_plans_sources_registered_at_runtime(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fleet YAML is read-only; UI-added sources exist only in state."""

    from pheasant.cli import main
    from pheasant.sync.engine import SyncEngine

    workspace = tmp_path / "runtime-source"
    workspace.mkdir()
    (workspace / "a.md").write_text("# Runtime source\n", encoding="utf-8")
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        "\n".join(
            [
                "pheasant:",
                "  name: runtime-planner",
                f"  state_path: {tmp_path / 'state'}",
                f"  workspace_root: {tmp_path}",
                f"  exports_path: {tmp_path / 'exports'}",
                "storage:",
                "  graph_snapshots: false",
                "sync:",
                "  watcher:",
                "    enabled: false",
                "  scheduler:",
                "    enabled: false",
                "sources: []",
            ]
        ),
        encoding="utf-8",
    )
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "runtime-planner",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "sync": {"watcher": {"enabled": False}, "scheduler": {"enabled": False}},
            "sources": [],
        }
    )
    engine = SyncEngine(config)
    try:
        source = PheasantConfig.model_validate(
            {
                "sources": [
                    {
                        "name": "runtime-source",
                        "type": "markdown_folder",
                        "path": str(workspace),
                        "include": ["**/*.md"],
                    }
                ]
            }
        ).sources[0]
        SourceRegistry(config, engine.state).register_source(source)
    finally:
        engine.close()

    assert main(["shard", "plan", "--config", str(config_path), "--shards", "1"]) == 0
    output = capsys.readouterr().out
    assert "runtime-source" in output
    assert "Nothing to plan" not in output


# ---------------------------------------------------------------------------
# Per-source leases — needs a real database
# ---------------------------------------------------------------------------


def _store() -> StateStore:
    store = StateStore(backend=PostgresBackend(DSN))
    store.migrate()
    return store


@postgres
def test_two_writers_can_hold_two_different_sources_at_once() -> None:
    """The ceiling this step lifts. One writer per knowledge base was the
    limit; two sources have no reason to wait for each other."""

    store = _store()
    try:
        docs = SourceLease(store, "docs", owner="writer-a")
        code = SourceLease(store, "code", owner="writer-b")
        assert docs.try_acquire() is True
        assert code.try_acquire() is True
        assert docs.held and code.held
    finally:
        docs.release()
        code.release()
        store.close()


@postgres
def test_two_writers_cannot_hold_the_same_source() -> None:
    """A source's artifacts, chunks, graph nodes and manifest are one
    consistent set; two writers would interleave them."""

    store = _store()
    try:
        first = SourceLease(store, "shared", owner="writer-a")
        second = SourceLease(store, "shared", owner="writer-b")
        assert first.try_acquire() is True
        assert second.try_acquire() is False
        assert second.held is False
        with pytest.raises(SourceLeaseError, match="shared"):
            second.acquire(wait_timeout_s=0)
    finally:
        first.release()
        store.close()


@postgres
def test_a_released_lease_is_immediately_available() -> None:
    store = _store()
    try:
        first = SourceLease(store, "handover", owner="writer-a")
        assert first.try_acquire() is True
        first.release()
        second = SourceLease(store, "handover", owner="writer-b")
        assert second.try_acquire() is True
        second.release()
    finally:
        store.close()


@postgres
def test_a_dead_writers_lease_is_taken_over_once_it_goes_stale() -> None:
    """Otherwise an OOM-killed indexer locks its source out forever."""

    store = _store()
    try:
        dead = SourceLease(store, "stale", owner="dead-writer", stale_after_s=0.0)
        assert dead.try_acquire() is True
        dead._heartbeat.stop()  # stop heartbeating, as a killed process would

        # stale_after_s=0 makes every heartbeat instantly ancient.
        taker = SourceLease(store, "stale", owner="live-writer", stale_after_s=0.0)
        assert taker.try_acquire() is True
        rows = store.rows("SELECT owner FROM source_leases WHERE source_id=?", ("stale",))
        assert rows[0]["owner"] == "live-writer"
        taker.release()
    finally:
        store.close()


@postgres
def test_releasing_a_lease_we_already_lost_does_not_steal_it_back() -> None:
    """A takeover means someone else owns the row. Deleting it on our way out
    would hand a third writer a source that is actively in use."""

    store = _store()
    try:
        loser = SourceLease(store, "contested", owner="loser", stale_after_s=0.0)
        assert loser.try_acquire() is True
        loser._heartbeat.stop()
        winner = SourceLease(store, "contested", owner="winner", stale_after_s=0.0)
        assert winner.try_acquire() is True

        loser.release()  # the loser tidying up after itself

        rows = store.rows("SELECT owner FROM source_leases WHERE source_id=?", ("contested",))
        assert rows and rows[0]["owner"] == "winner", "the loser deleted the winner's lease"
        winner.release()
    finally:
        store.close()


@postgres
def test_concurrent_claims_produce_exactly_one_winner() -> None:
    """The exclusion is a single conditional UPDATE precisely so the database
    arbitrates; a read-then-write in Python would let both threads win."""

    store = _store()
    winners: list[str] = []
    held: list[SourceLease] = []
    lock = threading.Lock()
    # Every thread claims at the same instant. Without this they queue up, and
    # since a winner that releases frees the row, several would legitimately
    # "win" in turn — which is not the property under test. (That is exactly
    # what the first version of this test measured, and it looked like a
    # concurrency bug in the lease rather than a bug in the test.)
    start = threading.Barrier(6)

    def claim(name: str) -> None:
        lease = SourceLease(StateStore(backend=PostgresBackend(DSN)), "race", owner=name)
        start.wait(timeout=10)
        won = lease.try_acquire()
        with lock:
            if won:
                winners.append(name)
                # Hold it. Releasing here would let the next thread win too.
                held.append(lease)
        if not won:
            lease.state.close()

    try:
        threads = [threading.Thread(target=claim, args=(f"w{i}",)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert len(winners) == 1, f"expected exactly one winner, got {winners}"
        rows = store.rows("SELECT owner FROM source_leases WHERE source_id=?", ("race",))
        assert rows and rows[0]["owner"] == winners[0]
    finally:
        for lease in held:
            lease.release()
            lease.state.close()
        store.close()


def test_sqlite_keeps_the_whole_state_lease(tmp_path: Path) -> None:
    """Rule 7. SQLite genuinely permits one writer per file, so the
    whole-state lease there is an accurate model, not a limitation to route
    around. Runs everywhere, because that is the default deployment."""

    from pheasant.sync.engine import SyncEngine

    workspace = tmp_path / "docs"
    workspace.mkdir()
    (workspace / "a.md").write_text("# A\n\nGateway notes.\n", encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "kb",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
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
    engine = SyncEngine(config)
    try:
        assert engine.state.dialect.name == "sqlite"
        assert engine.sync_source("docs", "full").indexed_artifacts == 1
        # The whole-state lease was taken, and no per-source rows were written.
        assert engine.lease.held
        assert engine.state.rows("SELECT source_id FROM source_leases") == []
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# Phase 35.8 — the ceiling, published; the split, emitted
#
# The single-writer ceiling is a defensible consequence of a globally
# consistent graph. What was not defensible is that it was undocumented as a
# cliff and the escape from it was unautomated: a team scales workers, watches
# ingest stop improving, and has nothing telling them they have reached the
# commit-authority limit rather than a tuning problem.
# ---------------------------------------------------------------------------


class _Clock:
    """A hand-wound monotonic clock, so a five-minute window costs no seconds."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _meter(clock: _Clock, **kwargs: object):
    from pheasant.sync.saturation import CommitAuthorityMeter

    return CommitAuthorityMeter(clock=clock, **kwargs)


def test_saturation_is_the_busy_fraction_of_the_window() -> None:
    clock = _Clock()
    meter = _meter(clock, window_seconds=100.0, minimum_seconds=10.0)

    with meter.busy():
        clock.advance(40.0)
    clock.advance(60.0)

    assert meter.saturation() == 0.4


def test_it_publishes_nothing_before_it_has_seen_enough() -> None:
    """Two busy seconds in a pod's first four are not 50% saturation.

    A gauge that said so would invite sharding a region that is doing nothing,
    which is the most expensive possible response to a misread number.
    """

    clock = _Clock()
    meter = _meter(clock, window_seconds=100.0, minimum_seconds=60.0)
    with meter.busy():
        clock.advance(2.0)
    clock.advance(2.0)

    assert meter.saturation() is None


def test_concurrent_sources_cannot_report_more_than_full() -> None:
    """An indexer runs several sources through a pool; the spans overlap.

    Summing durations would report 300% busy on a three-source pass — a
    number that cannot be true of wall time and would trip every threshold.
    """

    clock = _Clock()
    meter = _meter(clock, window_seconds=100.0, minimum_seconds=10.0)
    meter._spans.extend([(1000.0, 1040.0), (1010.0, 1050.0), (1020.0, 1030.0)])
    clock.advance(100.0)

    assert meter.saturation() == 0.5


def test_a_source_still_indexing_counts_while_it_runs() -> None:
    """Otherwise one multi-hour source reports zero saturation for its whole
    duration — which is exactly the run somebody would be looking at."""

    clock = _Clock()
    meter = _meter(clock, window_seconds=100.0, minimum_seconds=10.0)
    clock.advance(20.0)  # idle first, so the window has something to divide by
    with meter.busy():
        clock.advance(80.0)
        assert meter.saturation() == 0.8


def test_the_window_rolls() -> None:
    """A region that indexed hard this morning is not saturated this afternoon."""

    clock = _Clock()
    meter = _meter(clock, window_seconds=100.0, minimum_seconds=10.0)
    with meter.busy():
        clock.advance(50.0)
    clock.advance(200.0)

    assert meter.saturation() == 0.0


def test_a_nested_sync_does_not_restart_the_clock() -> None:
    """`sync_all` calls `sync_source`; the outer span is the one that counts."""

    clock = _Clock()
    meter = _meter(clock, window_seconds=100.0, minimum_seconds=10.0)
    with meter.busy():
        clock.advance(10.0)
        with meter.busy():
            clock.advance(10.0)
        clock.advance(10.0)
    clock.advance(70.0)

    assert meter.saturation() == 0.3


def test_the_capacity_model_warns_before_a_corpus_reaches_the_ceiling() -> None:
    """The ceiling stated as a number, beside the other coefficients."""

    from pheasant.capacity import COMMIT_AUTHORITY_WARN_HOURS, SHARD_ON_SATURATION, project
    from pheasant.sync.saturation import SHARD_THRESHOLD

    # One home for the threshold: a report and a live gauge that disagreed
    # about where the line is would be worse than neither.
    assert SHARD_ON_SATURATION == SHARD_THRESHOLD

    small = project(10_000, 0)
    assert not any("commit capacity" in warning for warning in small.warnings)

    # Just past the documented window, from the same measured
    # seconds-per-1k-files the projection already uses.
    from pheasant.capacity import SECONDS_PER_1K_FILES

    files = int((COMMIT_AUTHORITY_WARN_HOURS * 3600 / SECONDS_PER_1K_FILES) * 1000) + 1_000
    large = project(files, 0)
    ceiling = [w for w in large.warnings if "commit capacity" in w]
    assert ceiling, large.warnings
    assert "pheasant_commit_authority_saturation" in ceiling[0]
    assert "shard plan" in ceiling[0]


# ---------------------------------------------------------------------------
# The emitted split
# ---------------------------------------------------------------------------


def _plan_config(tmp_path: Path, names: list[str]) -> PheasantConfig:
    return PheasantConfig.model_validate(
        {
            "pheasant": {"name": "atlas", "state_path": str(tmp_path / "state")},
            "search": {"ranking": {"rrf_k": 42}},
            "sources": [
                {"name": name, "type": "markdown_folder", "path": str(tmp_path / name)}
                for name in names
            ],
        }
    )


def test_emitting_a_split_produces_one_project_per_region(tmp_path: Path) -> None:
    """`shard plan` proposed a split and left the work of making it real."""

    from pheasant.sharding import render_artifacts

    config = _plan_config(tmp_path, ["docs", "code", "tickets"])
    plan = plan_shards(
        [SourceSize("docs", 300_000), SourceSize("code", 200_000), SourceSize("tickets", 100)],
        max_nodes_per_shard=1_500_000,
    )
    artifacts = render_artifacts(plan, config)

    assert len(plan["shards"]) == 3
    for index in (1, 2, 3):
        prefix = f"atlas-shard-{index}"
        assert f"{prefix}/pheasant.yaml" in artifacts
        assert f"{prefix}/docker-compose.yml" in artifacts
        assert f"{prefix}/.env.example" in artifacts
    assert "README.md" in artifacts


def test_each_region_gets_its_own_knowledge_base_id(tmp_path: Path) -> None:
    """The one mistake that cannot be fixed by editing a file afterwards.

    Every stable ID starts with `pheasant.name`, so two regions sharing it
    would already have collided inside their persisted graphs by the time
    anyone noticed.
    """

    import yaml

    from pheasant.sharding import render_artifacts

    config = _plan_config(tmp_path, ["docs", "code"])
    plan = plan_shards([SourceSize("docs", 2_000_000), SourceSize("code", 2_000_000)])
    artifacts = render_artifacts(plan, config)

    ids = {
        yaml.safe_load(content)["pheasant"]["name"]
        for name, content in artifacts.items()
        if name.endswith("pheasant.yaml")
    }
    assert len(ids) == len(plan["shards"]) > 1
    assert all(identifier != "atlas" for identifier in ids)


def test_a_region_carries_only_its_own_sources_and_the_same_retrieval(
    tmp_path: Path,
) -> None:
    """A split must not quietly become a second product.

    Shards that ranked differently would answer differently for reasons
    invisible in the plan that produced them.
    """

    import yaml

    from pheasant.sharding import render_artifacts

    config = _plan_config(tmp_path, ["docs", "code"])
    plan = plan_shards([SourceSize("docs", 2_000_000), SourceSize("code", 2_000_000)])
    artifacts = render_artifacts(plan, config)

    seen: list[str] = []
    for name, content in artifacts.items():
        if not name.endswith("pheasant.yaml"):
            continue
        region = yaml.safe_load(content)
        names = [source["name"] for source in region["sources"]]
        assert len(names) == 1
        seen.extend(names)
        assert region["search"]["ranking"]["rrf_k"] == 42
    assert sorted(seen) == ["code", "docs"]


def test_two_regions_do_not_collide_on_volumes_or_ports(tmp_path: Path) -> None:
    """Two compose projects sharing a volume name share a state directory."""

    from pheasant.sharding import render_artifacts

    config = _plan_config(tmp_path, ["docs", "code"])
    plan = plan_shards([SourceSize("docs", 2_000_000), SourceSize("code", 2_000_000)])
    artifacts = render_artifacts(plan, config)

    composes = [c for name, c in artifacts.items() if name.endswith("docker-compose.yml")]
    assert len(composes) == 2
    assert "atlas-shard-1-state:" in composes[0]
    assert "atlas-shard-2-state:" in composes[1]
    assert "${PHEASANT_PORT:-8765}:8765" in composes[0]
    assert "${PHEASANT_PORT:-8766}:8765" in composes[1]


def test_the_emitted_secrets_are_stubs_and_say_how_many(tmp_path: Path) -> None:
    """A generated file that carried a real value would be a generated leak."""

    from pheasant.sharding import render_artifacts

    config = _plan_config(tmp_path, ["docs"])
    plan = plan_shards([SourceSize("docs", 1000)])
    artifacts = render_artifacts(plan, config)

    env = artifacts["atlas-shard-1/.env.example"]
    assert "PHEASANT_API_TOKEN=" in env
    assert "openssl rand" in env
    # Every line is commented out or empty: nothing here is a usable secret.
    assert all(not line.strip() or line.startswith("#") for line in env.splitlines())


def test_the_readme_says_what_the_emission_did_not_do(tmp_path: Path) -> None:
    """Automation that implied the data had moved would be the dangerous kind."""

    from pheasant.sharding import render_artifacts

    config = _plan_config(tmp_path, ["docs", "code"])
    plan = plan_shards([SourceSize("docs", 2_000_000), SourceSize("code", 2_000_000)])
    readme = render_artifacts(plan, config)["README.md"]

    assert "No data moves" in readme
    assert "indexes its own sources from scratch" in readme


def test_the_emitted_compose_is_a_file_docker_will_accept(tmp_path: Path) -> None:
    """A generated compose file that fails to parse is worse than none.

    `mem_limit` takes an integer with a suffix, so the ladder's `0.5Gi` first
    rung — which is what a small shard gets — is not a value it accepts.
    """

    import yaml

    from pheasant.sharding import _compose_memory, render_artifacts

    assert _compose_memory("0.5Gi") == "512m"
    assert _compose_memory("12Gi") == "12288m"

    config = _plan_config(tmp_path, ["docs"])
    plan = plan_shards([SourceSize("docs", 500)])
    compose = yaml.safe_load(render_artifacts(plan, config)["atlas-shard-1/docker-compose.yml"])

    service = compose["services"]["pheasant"]
    assert service["mem_limit"].endswith("m")
    # The container paths the emitted config names must be the ones mounted.
    mounted = {entry.split(":")[1] for entry in service["volumes"]}
    assert {"/state", "/workspace", "/exports"} <= mounted


def test_a_region_config_names_container_paths(tmp_path: Path) -> None:
    """Not wherever the planning machine happened to keep its state."""

    import yaml

    from pheasant.sharding import render_artifacts

    config = _plan_config(tmp_path, ["docs"])
    plan = plan_shards([SourceSize("docs", 500)])
    region = yaml.safe_load(render_artifacts(plan, config)["atlas-shard-1/pheasant.yaml"])

    assert region["pheasant"]["state_path"] == "/state"
    assert region["pheasant"]["workspace_root"] == "/workspace"


def test_host_source_paths_are_named_but_never_assumed(tmp_path: Path) -> None:
    """This machine's paths are not necessarily the deploying machine's.

    A compose file that silently bind-mounted a guess would either fail to
    start or index the wrong directory. Naming them is help; assuming them
    is not — so they are emitted commented out.
    """

    from pheasant.sharding import render_artifacts

    config = _plan_config(tmp_path, ["docs"])
    plan = plan_shards([SourceSize("docs", 500)])
    compose = render_artifacts(plan, config)["atlas-shard-1/docker-compose.yml"]

    hint = next(line for line in compose.splitlines() if str(tmp_path / "docs") in line)
    assert hint.strip().startswith("#")
