"""Phase 3 — deterministic near-duplicate clustering and medoid promotion.

Acceptance: bucketing never crosses scope, kind, or ACL partition; medoid
selection is deterministic regardless of write/iteration order; a subsumed
record's `valid_until` is never touched (distinct from `supersedes`) and
stays reachable via an explicit tier filter or `current_only=False`; the
compaction ledger is idempotent; and the whole pass is off by default.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pheasant.artifacts.compaction import find_clusters, params_hash
from pheasant.artifacts.policy import MemoryPolicy, admits
from pheasant.artifacts.store import MemoryRecord
from pheasant.config.loader import load_config
from pheasant.mcp_server.tools import PheasantTools

NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _record(
    record_id: str,
    text: str,
    *,
    scope: str = "org",
    subject: str | None = None,
    kind: str = "fact",
    written_by: str | None = None,
) -> MemoryRecord:
    return MemoryRecord(
        record_id=record_id,
        scope=scope,
        subject=subject,
        text=text,
        asserted_at="2026-07-16T12:00:00Z",
        supersedes=None,
        tags=(),
        path=Path(f"/tmp/{record_id}.md"),
        kind=kind,
        written_by=written_by,
    )


# ---------------------------------------------------------------------------
# find_clusters — the pure algorithm
# ---------------------------------------------------------------------------


def test_near_duplicates_cluster_and_pick_a_medoid():
    records = [
        _record("a", "The staging cluster runs in us-east-2, owned by platform."),
        _record("b", "The staging cluster lives in us-east-2, owned by the platform team."),
        _record("c", "Staging cluster: us-east-2, owned by platform."),
        _record("d", "Completely unrelated fact about the coffee machine."),
    ]
    plans = find_clusters(records, {}, {}, similarity_threshold=0.4, min_cluster_size=2)
    assert len(plans) == 1
    plan = plans[0]
    assert set(plan.member_ids) | {plan.canonical_id} == {"a", "b", "c"}
    assert "d" not in plan.member_ids and plan.canonical_id != "d"


def test_medoid_selection_is_order_independent():
    """The same cluster, built from records in every permutation of write
    order, must always pick the same canonical record."""
    base = [
        _record("a", "The staging cluster runs in us-east-2, owned by platform."),
        _record("b", "The staging cluster lives in us-east-2, owned by the platform team."),
        _record("c", "Staging cluster: us-east-2, owned by platform."),
    ]
    canonicals = set()
    import itertools

    for permutation in itertools.permutations(base):
        plans = find_clusters(
            list(permutation), {}, {}, similarity_threshold=0.4, min_cluster_size=2
        )
        assert len(plans) == 1
        canonicals.add(plans[0].canonical_id)
    assert len(canonicals) == 1, f"medoid changed across orderings: {canonicals}"


def test_bucketing_never_crosses_scope_subject_kind_or_acl():
    text = "The rollout password is the same sentence, asserted many ways here."
    records = [
        _record("a", text, scope="org"),
        _record("b", text, scope="user", written_by="alice"),  # different scope
        _record("c", text, scope="org", subject="infra"),  # different subject
        _record("d", text, scope="org", kind="alias"),  # steering kind
        _record("e", text, scope="user", written_by="bob"),  # different writer
        _record("f", text, scope="user", written_by="alice"),  # matches b
    ]
    plans = find_clusters(records, {}, {}, similarity_threshold=0.1, min_cluster_size=2)
    # Only (b, f) share scope=user + written_by=alice + subject=None + kind=fact.
    assert len(plans) == 1
    assert set(plans[0].member_ids) | {plans[0].canonical_id} == {"b", "f"}


def test_steering_kinds_are_never_clustered():
    text = "when: deploy, docker -> prefer: docs/, deploy/"
    records = [
        _record("a", text, kind="alias"),
        _record("b", text, kind="alias"),
    ]
    assert find_clusters(records, {}, {}, similarity_threshold=0.1, min_cluster_size=2) == []


def test_already_cold_records_are_never_reclustered():
    text = "The same fact, restated a few different ways for this test."
    records = [
        _record("a", text),
        _record("b", text + " (a slightly different restatement)"),
    ]
    tiers = {"a": "cold"}  # already subsumed by something else
    plans = find_clusters(records, {}, tiers, similarity_threshold=0.3, min_cluster_size=2)
    assert plans == []  # only one hot record left; nothing to cluster


def test_absorbed_observations_sum_plus_one_per_member():
    records = [
        _record("a", "The staging cluster runs in us-east-2, owned by platform."),
        _record("b", "The staging cluster lives in us-east-2, owned by the platform team."),
    ]
    observations = {"a": 3, "b": 5}
    plans = find_clusters(records, observations, {}, similarity_threshold=0.3, min_cluster_size=2)
    assert len(plans) == 1
    plan = plans[0]
    other = plan.member_ids[0]
    # absorbed = other's own observations + 1 (for existing as a duplicate)
    assert plan.absorbed_observations == observations[other] + 1


def test_params_hash_changes_with_threshold():
    a = params_hash(similarity_threshold=0.6, min_cluster_size=2)
    b = params_hash(similarity_threshold=0.7, min_cluster_size=2)
    assert a != b


# ---------------------------------------------------------------------------
# subsumed_by is distinct from supersedes — the whole point
# ---------------------------------------------------------------------------


def test_subsumed_record_valid_until_is_never_touched():
    """A subsumed record is redundant but still TRUE: unlike a superseded
    one, its validity window must stay open. This is what tiers=("cold",)
    exists to reach without pretending the record expired."""
    record = {
        "record_id": "b",
        "scope": "org",
        "subject": None,
        "kind": "fact",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_until": None,  # never set by subsumption
        "tier": "cold",
    }
    now = "2030-01-01T00:00:00Z"  # arbitrarily far in the future
    assert admits(MemoryPolicy(tiers=("cold",)), record, now=now) is True
    assert admits(MemoryPolicy(), record, now=now) is False  # not in default results
    assert admits(MemoryPolicy(current_only=False), record, now=now) is True


# ---------------------------------------------------------------------------
# End to end, via PheasantTools
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, *, compaction: bool = True, threshold: float = 0.4) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory").mkdir(exist_ok=True)
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: compaction-test
  state_path: {tmp_path / ".pheasant" / "state"}
  exports_path: {tmp_path / ".pheasant" / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
memory:
  compaction_enabled: {"true" if compaction else "false"}
  compaction_similarity_threshold: {threshold}
sources:
  - name: agent-memory
    type: memory
    path: memory
""",
        encoding="utf-8",
    )
    return config_path


def test_end_to_end_subsumed_records_hidden_by_default_reachable_via_tier(
    tmp_path: Path,
) -> None:
    from pheasant.artifacts.maintenance import run_memory_maintenance

    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        tools.memory_write(
            "compaction-test",
            "The staging cluster runs in us-east-2, owned by platform.",
            scope="org",
        )
        tools.memory_write(
            "compaction-test",
            "The staging cluster lives in us-east-2, owned by the platform team.",
            scope="org",
        )
        tools.memory_write(
            "compaction-test", "Completely unrelated fact about coffee.", scope="org"
        )

        result = run_memory_maintenance(tools.engine)
        assert result is not None
        assert result["compaction"]["clusters"] == 1
        assert len(result["compaction"]["subsumed"]) == 1

        default_hits = tools.search_context(
            "compaction-test", "staging cluster us-east-2", mode="text", max_results=10
        )
        default_ids = {
            item["memory"]["record_id"]
            for item in default_hits.get("results") or []
            if item.get("memory")
        }
        assert len(default_ids) == 1  # the duplicate is hidden

        cold_hits = tools.search_context(
            "compaction-test",
            "staging cluster us-east-2",
            mode="text",
            max_results=10,
            artifact_policy={"tiers": ["cold"]},
        )
        cold_ids = {
            item["memory"]["record_id"]
            for item in cold_hits.get("results") or []
            if item.get("memory")
        }
        assert len(cold_ids) == 1
        assert cold_ids != default_ids

        # The ledger explains the decision.
        ledger = tools.state.memory_compaction_ledger()
        assert len(ledger) == 1
        assert ledger[0]["op"] == "subsume"
        assert ledger[0]["canonical_id"] in default_ids
        assert ledger[0]["member_id"] in cold_ids
    finally:
        tools.engine.close()


def test_compaction_disabled_by_default_leaves_near_duplicates_alone(tmp_path: Path) -> None:
    from pheasant.artifacts.maintenance import run_memory_maintenance

    config = load_config(_write_config(tmp_path, compaction=False))
    tools = PheasantTools(config)
    try:
        tools.memory_write(
            "compaction-test",
            "The staging cluster runs in us-east-2, owned by platform.",
            scope="org",
        )
        tools.memory_write(
            "compaction-test",
            "The staging cluster lives in us-east-2, owned by the platform team.",
            scope="org",
        )
        result = run_memory_maintenance(tools.engine)
        assert result is not None
        assert "compaction" not in result
        rows = tools.state.memory_compaction_rows()
        assert all(row["tier"] == "hot" for row in rows)
    finally:
        tools.engine.close()


def test_compaction_pass_is_idempotent(tmp_path: Path) -> None:
    from pheasant.artifacts.maintenance import run_memory_maintenance

    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        tools.memory_write(
            "compaction-test",
            "The staging cluster runs in us-east-2, owned by platform.",
            scope="org",
        )
        tools.memory_write(
            "compaction-test",
            "The staging cluster lives in us-east-2, owned by the platform team.",
            scope="org",
        )
        first = run_memory_maintenance(tools.engine)
        assert first is not None
        ledger_after_first = tools.state.memory_compaction_ledger()

        second = run_memory_maintenance(tools.engine)
        assert second is not None
        assert "compaction" not in second  # the pair is already cold; nothing new
        ledger_after_second = tools.state.memory_compaction_ledger()
        assert ledger_after_second == ledger_after_first  # no duplicate rows
    finally:
        tools.engine.close()


def test_subsumes_edge_is_drawn_and_distinct_from_supersedes(tmp_path: Path) -> None:
    from pheasant.artifacts.maintenance import run_memory_maintenance

    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        tools.memory_write(
            "compaction-test",
            "The staging cluster runs in us-east-2, owned by platform.",
            scope="org",
        )
        tools.memory_write(
            "compaction-test",
            "The staging cluster lives in us-east-2, owned by the platform team.",
            scope="org",
        )
        run_memory_maintenance(tools.engine)

        edge_types = set()
        for _endpoints, edge_map in tools.engine.graph_builder.graph.edges():
            for _key, data in edge_map.items():
                if data.get("type") in ("subsumes", "supersedes"):
                    edge_types.add(data["type"])
        assert edge_types == {"subsumes"}  # no correction happened, only a subsumption
    finally:
        tools.engine.close()


def test_compaction_never_crosses_principal_at_user_scope(tmp_path: Path) -> None:
    """The same ACL constraint reinforcement (Phase 1) enforces at the write
    path must hold for clustering too: alice and bob's identical assertions
    at user scope must never merge into one record."""
    from pheasant.artifacts.maintenance import run_memory_maintenance

    config = load_config(_write_config(tmp_path, threshold=0.1))
    tools = PheasantTools(config)
    try:
        alice = tools.memory_write(
            "compaction-test",
            "My favorite color is blue, definitely, for sure, always.",
            scope="user",
            principal="alice",
        )
        bob = tools.memory_write(
            "compaction-test",
            "My favorite colour is blue too, definitely, for sure, always.",
            scope="user",
            principal="bob",
        )
        run_memory_maintenance(tools.engine)
        rows = {row["record_id"]: row for row in tools.state.memory_compaction_rows()}
        assert rows[alice["record"]["record_id"]]["tier"] == "hot"
        assert rows[bob["record"]["record_id"]]["tier"] == "hot"
        assert tools.state.memory_compaction_ledger() == []
    finally:
        tools.engine.close()
