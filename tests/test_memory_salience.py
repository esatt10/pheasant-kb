"""Step 33.9 — salience, usage feedback, decay and bounded growth.

Acceptance: salience is a pure function of recorded inputs; usage counters are
off by default and leave every result identical when off; pruning is
deterministic and idempotent; and a store stays under its stated bound.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pheasant.artifacts.salience import (
    HALF_LIFE_DAYS,
    over_capacity,
    over_scope_capacity,
    over_subject_capacity,
    rank,
    salience,
)
from pheasant.artifacts.store import MemoryStore
from pheasant.config.loader import load_config
from pheasant.mcp_server.tools import PheasantTools

NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _record(
    record_id: str,
    *,
    scope: str = "user",
    subject: str | None = None,
    days_old: float = 0.0,
    uses: int = 0,
) -> dict:
    asserted = NOW.timestamp() - days_old * 86400
    return {
        "record_id": record_id,
        "scope": scope,
        "subject": subject,
        "asserted_at": datetime.fromtimestamp(asserted, tz=UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "uses": uses,
    }


def _config(
    tmp_path: Path,
    *,
    usage: bool = False,
    max_records: int | None = None,
    session_max_records: int | None = None,
    user_max_records: int | None = None,
    org_max_records: int | None = None,
    max_records_per_subject: int | None = None,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "readme.md").write_text("# Notes\n\nSomething.\n", encoding="utf-8")
    (tmp_path / "memory").mkdir(exist_ok=True)
    caps = {
        "max_records": max_records,
        "session_max_records": session_max_records,
        "user_max_records": user_max_records,
        "org_max_records": org_max_records,
        "max_records_per_subject": max_records_per_subject,
    }
    cap = "".join(f"\n  {name}: {value}" for name, value in caps.items() if value is not None)
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: salience-test
  state_path: {tmp_path / ".pheasant" / "state"}
  exports_path: {tmp_path / ".pheasant" / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
memory:
  usage_tracking: {"true" if usage else "false"}{cap}
sources:
  - name: docs
    type: document_folder
    path: docs
  - name: agent-memory
    type: memory
    path: memory
""",
        encoding="utf-8",
    )
    return config_path


# ---------------------------------------------------------------------------
# The formula
# ---------------------------------------------------------------------------


def test_salience_is_a_pure_function_of_its_inputs() -> None:
    record = _record("mem-1", days_old=30, uses=3)
    assert salience(record, now=NOW) == salience(record, now=NOW)


def test_recency_halves_over_the_half_life() -> None:
    fresh = salience(_record("a", days_old=0), now=NOW)
    aged = salience(_record("b", days_old=HALF_LIFE_DAYS), now=NOW)
    assert aged == round(fresh * 0.5, 6)


def test_use_helps_but_with_diminishing_returns() -> None:
    """The gap between never-used and used-once is the informative one."""
    never = salience(_record("a", uses=0), now=NOW)
    once = salience(_record("b", uses=1), now=NOW)
    forty = salience(_record("c", uses=40), now=NOW)
    forty_one = salience(_record("d", uses=41), now=NOW)
    assert once > never
    assert (once - never) > (forty_one - forty)


def test_scope_breaks_ties_between_otherwise_equal_records() -> None:
    org = salience(_record("a", scope="org"), now=NOW)
    user = salience(_record("b", scope="user"), now=NOW)
    session = salience(_record("c", scope="session"), now=NOW)
    assert org > user > session


def test_an_unparseable_timestamp_does_not_raise() -> None:
    assert salience({"record_id": "x", "scope": "user", "asserted_at": "nonsense"}, now=NOW) > 0


def test_ranking_is_total_and_stable() -> None:
    """Two records with identical salience must still order deterministically,
    or a pruning pass over an unchanged store removes different rows each time.
    """
    records = [_record("b"), _record("a"), _record("c")]
    assert [r["record_id"] for r in rank(records, now=NOW)] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------


def test_no_cap_means_nothing_is_ever_dropped() -> None:
    records = [_record(f"mem-{i}") for i in range(50)]
    assert over_capacity(records, None, now=NOW) == []
    assert over_capacity(records, 0, now=NOW) == []


def test_over_capacity_drops_the_least_salient_first() -> None:
    keep = _record("keep", scope="org", days_old=1, uses=10)
    drop = _record("drop", scope="session", days_old=400, uses=0)
    doomed = over_capacity([keep, drop], 1, now=NOW)
    assert [r["record_id"] for r in doomed] == ["drop"]


def test_pruning_is_idempotent() -> None:
    records = [_record(f"mem-{i}", days_old=i) for i in range(10)]
    first = {r["record_id"] for r in over_capacity(records, 4, now=NOW)}
    survivors = [r for r in records if r["record_id"] not in first]
    assert len(survivors) == 4
    assert over_capacity(survivors, 4, now=NOW) == []


# ---------------------------------------------------------------------------
# Per-scope / per-subject capacity (Phase 5)
# ---------------------------------------------------------------------------


def test_scope_capacity_isolates_pools() -> None:
    """A session flood must not be able to crowd out an org fact — that is
    exactly what a shared `max_records` pool cannot guarantee, since a
    session record only ever loses by `SCOPE_WEIGHT`, not by exclusion."""
    org = [_record(f"org-{i}", scope="org", days_old=1) for i in range(2)]
    session = [_record(f"session-{i}", scope="session", days_old=0) for i in range(50)]
    doomed = {
        r["record_id"]
        for r in over_scope_capacity(org + session, {"org": 2, "session": 3}, now=NOW)
    }
    assert not doomed & {r["record_id"] for r in org}
    assert len(doomed) == 47


def test_scope_capacity_with_no_caps_drops_nothing() -> None:
    records = [_record(f"mem-{i}", scope="org") for i in range(10)]
    assert over_scope_capacity(records, {}, now=NOW) == []
    assert over_scope_capacity(records, {"org": None, "user": None}, now=NOW) == []


def test_subject_capacity_groups_across_scopes() -> None:
    records = [
        _record("a", scope="org", subject="widget", days_old=5),
        _record("b", scope="user", subject="widget", days_old=1),
        _record("c", scope="session", subject="widget", days_old=0),
        _record("d", scope="org", subject="gadget", days_old=5),
    ]
    doomed = {r["record_id"] for r in over_subject_capacity(records, 2, now=NOW)}
    # Least salient of the three "widget" records loses — here that is the
    # freshest-but-`session`-scoped one, since `SCOPE_WEIGHT` dominates a
    # 5-day recency gap; "gadget" is alone, so it is never at risk regardless
    # of the cap.
    assert doomed == {"c"}


def test_subject_capacity_exempts_untagged_records() -> None:
    records = [_record(f"mem-{i}", subject=None) for i in range(10)]
    assert over_subject_capacity(records, 1, now=NOW) == []


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def _tools(tmp_path: Path, texts: list[str], **kwargs) -> PheasantTools:
    config_path = _config(tmp_path, **kwargs)
    store = MemoryStore(tmp_path / "memory")
    for index, text in enumerate(texts):
        store.append(text, scope="org", now=datetime(2026, 7, 16, 12, 0, index, tzinfo=UTC))
    tools = PheasantTools(load_config(config_path))
    tools.sync_source("salience-test", "docs", "full")
    tools.sync_source("salience-test", "agent-memory", "full")
    return tools


def _tools_writes(tmp_path: Path, writes: list[dict], **kwargs) -> PheasantTools:
    """Like `_tools`, but each write picks its own scope/subject (Phase 5)."""
    config_path = _config(tmp_path, **kwargs)
    store = MemoryStore(tmp_path / "memory")
    for index, write in enumerate(writes):
        store.append(
            write["text"],
            scope=write.get("scope", "org"),
            subject=write.get("subject"),
            now=datetime(2026, 7, 16, 12, 0, index, tzinfo=UTC),
        )
    tools = PheasantTools(load_config(config_path))
    tools.sync_source("salience-test", "docs", "full")
    tools.sync_source("salience-test", "agent-memory", "full")
    return tools


def test_usage_tracking_off_leaves_every_counter_at_zero(tmp_path: Path) -> None:
    """A write on the read path must not happen unless it was asked for."""
    tools = _tools(tmp_path, ["The rollout password is BANANAS."], usage=False)
    try:
        tools.search_context("salience-test", "rollout password", mode="text")
        rows = tools.engine.state.rows("SELECT uses, last_used_at FROM memory_records")
        assert [int(r["uses"]) for r in rows] == [0]
        assert [r["last_used_at"] for r in rows] == [None]
    finally:
        tools.engine.close()


def test_usage_tracking_on_counts_what_retrieval_returned(tmp_path: Path) -> None:
    tools = _tools(tmp_path, ["The rollout password is BANANAS."], usage=True)
    try:
        tools.search_context("salience-test", "rollout password", mode="text")
        tools.search_context("salience-test", "rollout password", mode="text")
        rows = tools.engine.state.rows("SELECT uses, last_used_at FROM memory_records")
        assert int(rows[0]["uses"]) == 2
        assert rows[0]["last_used_at"]
    finally:
        tools.engine.close()


def test_a_memory_that_was_never_returned_is_not_credited(tmp_path: Path) -> None:
    """Counting on *served*, not considered, is what makes the signal mean
    anything."""
    tools = _tools(
        tmp_path,
        ["The rollout password is BANANAS.", "Unrelated note about kitchens."],
        usage=True,
    )
    try:
        tools.search_context("salience-test", "rollout password", mode="text", max_results=1)
        rows = {
            str(r["record_id"]): int(r["uses"])
            for r in tools.engine.state.rows("SELECT record_id, uses FROM memory_records")
        }
        assert sorted(rows.values()) == [0, 1]
    finally:
        tools.engine.close()


def test_maintenance_writes_salience_back(tmp_path: Path) -> None:
    """An operator should see the score that decided a prune, not have to
    recompute the formula by hand."""
    from pheasant.artifacts.maintenance import run_memory_maintenance

    tools = _tools(tmp_path, ["A remembered fact."], usage=False)
    try:
        # A sentinel the formula can never produce. Without this the column's
        # own default of 1.0 satisfies any plausible range assertion, so the
        # test passes whether or not anything was written — which is exactly
        # what mutation testing caught.
        tools.engine.state.conn.execute("UPDATE memory_records SET salience = -1.0")
        tools.engine.state.conn.commit()

        run_memory_maintenance(tools.engine, now=NOW)

        rows = tools.engine.state.rows(
            "SELECT record_id, scope, asserted_at, uses, salience FROM memory_records"
        )
        assert float(rows[0]["salience"]) == salience(dict(rows[0]), now=NOW)
        assert float(rows[0]["salience"]) > 0.0
    finally:
        tools.engine.close()


def test_a_store_over_its_bound_is_pruned_to_the_bound(tmp_path: Path) -> None:
    from pheasant.artifacts.maintenance import run_memory_maintenance

    tools = _tools(tmp_path, [f"Remembered fact number {i}." for i in range(6)], max_records=3)
    try:
        assert len(tools.engine.state.rows("SELECT 1 FROM memory_records")) == 6

        result = run_memory_maintenance(tools.engine, now=NOW)
        assert result is not None
        assert len(result["pruned"]) == 3
        assert len(tools.engine.state.rows("SELECT 1 FROM memory_records")) == 3

        # Archived, never deleted — the bytes are still there.
        archived = list((tmp_path / "memory" / "org").glob("*.md.archived"))
        assert len(archived) == 3

        # And a second pass has nothing left to do.
        again = run_memory_maintenance(tools.engine, now=NOW)
        assert not again.get("pruned")
    finally:
        tools.engine.close()


def test_no_cap_configured_prunes_nothing(tmp_path: Path) -> None:
    from pheasant.artifacts.maintenance import run_memory_maintenance

    tools = _tools(tmp_path, [f"Remembered fact number {i}." for i in range(6)])
    try:
        result = run_memory_maintenance(tools.engine, now=NOW)
        assert not result.get("pruned")
        assert len(tools.engine.state.rows("SELECT 1 FROM memory_records")) == 6
    finally:
        tools.engine.close()


def test_scope_budget_protects_org_from_a_session_flood(tmp_path: Path) -> None:
    """The end-to-end version of `test_scope_capacity_isolates_pools`: a
    `session_max_records` cap must survive a real maintenance pass, not just
    the pure `salience` formula, without touching the org record at all."""
    from pheasant.artifacts.maintenance import run_memory_maintenance

    writes = [{"text": "An org-wide policy fact.", "scope": "org"}]
    writes += [{"text": f"Session scratch note {i}.", "scope": "session"} for i in range(5)]
    tools = _tools_writes(tmp_path, writes, session_max_records=2)
    try:
        result = run_memory_maintenance(tools.engine, now=NOW)
        assert result is not None
        assert len(result["pruned"]) == 3

        rows = tools.engine.state.rows("SELECT scope FROM memory_records")
        scopes = [str(r["scope"]) for r in rows]
        assert scopes.count("org") == 1
        assert scopes.count("session") == 2
    finally:
        tools.engine.close()


def test_subject_budget_caps_one_entity_without_touching_another(tmp_path: Path) -> None:
    from pheasant.artifacts.maintenance import run_memory_maintenance

    writes = [
        {"text": f"Widget detail {i}.", "scope": "org", "subject": "widget"} for i in range(4)
    ]
    writes += [{"text": "The only gadget fact.", "scope": "org", "subject": "gadget"}]
    tools = _tools_writes(tmp_path, writes, max_records_per_subject=2)
    try:
        result = run_memory_maintenance(tools.engine, now=NOW)
        assert result is not None
        assert len(result["pruned"]) == 2

        rows = tools.engine.state.rows("SELECT subject FROM memory_records")
        subjects = [str(r["subject"]) for r in rows]
        assert subjects.count("widget") == 2
        assert subjects.count("gadget") == 1
    finally:
        tools.engine.close()


def test_scope_and_global_caps_compose_without_double_archiving(tmp_path: Path) -> None:
    """The global `max_records` backstop must run only over what the
    per-scope cap left behind, never re-consider a record already doomed."""
    from pheasant.artifacts.maintenance import run_memory_maintenance

    writes = [{"text": f"Session note {i}.", "scope": "session"} for i in range(5)]
    writes += [{"text": f"User fact {i}.", "scope": "user"} for i in range(3)]
    tools = _tools_writes(tmp_path, writes, session_max_records=3, max_records=4)
    try:
        result = run_memory_maintenance(tools.engine, now=NOW)
        assert result is not None
        # 2 archived by the session cap (5 -> 3), then the global backstop
        # brings the remaining 6 down to `max_records`: 2 more archived, each
        # counted once even though every session survivor was eligible for
        # both rules.
        assert len(result["pruned"]) == 4
        assert len(set(result["pruned"])) == 4
        assert len(tools.engine.state.rows("SELECT 1 FROM memory_records")) == 4
    finally:
        tools.engine.close()
