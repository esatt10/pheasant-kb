"""Phase 2 — the retention window that repairs the documented `as_of`
guarantee.

Before this: `consolidate()` archived a superseded or TTL-expired record's
file the moment it stopped being current, which drops its row out of
`memory_records` entirely (a `.md.archived` file no longer matches the
source's `**/*.md` include glob). `as_of` and `current_only=False` are
query-time filters over *indexed* rows, so "as_of deliberately brings the
old record back" held only in the window between a correction and the next
consolidation beat — effectively nothing once a region's scheduler or an
agent's own `memory_consolidate` call runs.

`memory.supersede_retention_days` (default `0`, opt-in) keeps a
newly-invalid record's file — and therefore its row — around for that many
days after it stops being current, before consolidation actually archives
it. No new column, no policy change: the *same* `valid_until` predicate that
already hides a superseded record from default (`current_only=True`) results
is what makes it reachable through the window via `as_of` /
`current_only=False`. Only *when* the file leaves the index moves; what a
query sees while it is still there is unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pheasant.config.loader import load_config
from pheasant.mcp_server.tools import PheasantTools


def _write_config(
    tmp_path: Path, *, retention_days: int | None = None, ttl_days: int | None = None
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory").mkdir(exist_ok=True)
    knobs = []
    if retention_days is not None:
        knobs.append(f"  supersede_retention_days: {retention_days}")
    if ttl_days is not None:
        knobs.append(f"  org_ttl_days: {ttl_days}")
    memory_block = "\n".join(knobs)
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: retention-test
  state_path: {tmp_path / ".pheasant" / "state"}
  exports_path: {tmp_path / ".pheasant" / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
memory:
{memory_block}
sources:
  - name: agent-memory
    type: memory
    path: memory
""",
        encoding="utf-8",
    )
    return config_path


def test_default_retention_is_zero_reproduces_pre_phase_2_archival(tmp_path: Path) -> None:
    """No configured retention window: a correction is archived on the very
    next consolidation pass, exactly as before Phase 2 existed."""
    from pheasant.artifacts.maintenance import run_memory_maintenance

    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        old = tools.memory_write("retention-test", "The password is BANANAS.", scope="org")
        tools.memory_write(
            "retention-test",
            "The password is GRAPES.",
            scope="org",
            supersedes=old["record"]["record_id"],
        )
        result = run_memory_maintenance(tools.engine)
        assert result is not None
        assert result["report"]["archived_superseded"] == [old["record"]["record_id"]]

        history = str(
            tools.search_context(
                "retention-test", "password", mode="text", artifact_policy={"current_only": False}
            )
        ).lower()
        assert "bananas" not in history  # genuinely gone, not merely filtered
    finally:
        tools.engine.close()


def test_as_of_brings_the_superseded_record_back_after_consolidation(tmp_path: Path) -> None:
    """The headline case: within the retention window, a corrected record
    stays indexed — hidden from default results, but reachable through
    `current_only=False`. Past the window, consolidation finally archives it
    and it genuinely disappears, the same way it always eventually would.

    Writes go straight through `MemoryStore.append(now=...)` rather than
    `memory_write` so every timestamp is exact and a day apart — the two
    records must not land in the same wall-clock second, or their
    second-resolution `asserted_at`/`valid_until` collide and there is no
    instant left to tell "before the correction" from "after" apart.
    """
    from pheasant.artifacts.maintenance import run_memory_maintenance
    from pheasant.artifacts.store import MemoryStore

    config_path = _write_config(tmp_path, retention_days=3)
    config = load_config(config_path)
    store_path = tmp_path / "memory"
    old_asserted = datetime(2026, 1, 1, tzinfo=UTC)
    old_record, _ = MemoryStore(store_path).append(
        "The rollout password is BANANAS.", scope="org", now=old_asserted
    )
    correction_instant = old_asserted + timedelta(days=1)
    MemoryStore(store_path).append(
        "The rollout password is GRAPES.",
        scope="org",
        supersedes=old_record.record_id,
        now=correction_instant,
    )
    old_path = old_record.path

    tools = PheasantTools(config)
    try:
        tools.sync_source("retention-test", "agent-memory", "full")

        # Default results already never show the corrected-away value —
        # that has been true since query-time policy (Step 33.6), unrelated
        # to archival.
        default_hits = str(
            tools.search_context("retention-test", "rollout password", mode="text")
        ).lower()
        assert "bananas" not in default_hits
        assert "grapes" in default_hits

        # Between the two writes: bananas was the only, valid answer, and
        # grapes did not exist yet.
        as_of_hits = str(
            tools.search_context(
                "retention-test",
                "rollout password",
                mode="text",
                artifact_policy={"as_of": old_asserted.isoformat().replace("+00:00", "Z")},
            )
        ).lower()
        assert "bananas" in as_of_hits
        assert "grapes" not in as_of_hits

        # One day after the correction: inside the 3-day retention window.
        one_day_after_correction = correction_instant + timedelta(days=1)
        result = run_memory_maintenance(tools.engine, now=one_day_after_correction)
        assert result is not None
        assert result["report"]["archived"] == 0  # not yet — still inside the window
        assert old_path.exists()  # the file was never renamed
        assert not old_path.with_suffix(".md.archived").exists()

        # Still indexed: current_only=False reaches it exactly as
        # docs/memory-system.md promises.
        history = str(
            tools.search_context(
                "retention-test",
                "rollout password",
                mode="text",
                artifact_policy={"current_only": False},
            )
        ).lower()
        assert "bananas" in history

        # Four days after the correction: past the 3-day window.
        four_days_after_correction = correction_instant + timedelta(days=4)
        result2 = run_memory_maintenance(tools.engine, now=four_days_after_correction)
        assert result2 is not None
        assert result2["report"]["archived_superseded"] == [old_record.record_id]
        assert not old_path.exists()
        assert old_path.with_suffix(".md.archived").exists()

        # Now genuinely gone, not merely filtered — the distinction the
        # pre-Phase-2 test already made, still true once the window elapses.
        after_history = str(
            tools.search_context(
                "retention-test",
                "rollout password",
                mode="text",
                artifact_policy={"current_only": False},
            )
        ).lower()
        assert "bananas" not in after_history

        # Idempotent: a third pass at the same later `now` archives nothing.
        again = run_memory_maintenance(tools.engine, now=four_days_after_correction)
        assert again is not None
        assert again["report"]["archived"] == 0
    finally:
        tools.engine.close()


def test_ttl_expiry_also_honors_the_retention_window(tmp_path: Path) -> None:
    """The same window applies to per-scope TTL expiry, not just
    supersession — both are "no longer current" and both used to be
    archived on the very next pass."""
    from pheasant.artifacts.maintenance import run_memory_maintenance

    config = load_config(_write_config(tmp_path, retention_days=2, ttl_days=5))
    tools = PheasantTools(config)
    try:
        written = tools.memory_write("retention-test", "Deploys run on Fridays.", scope="org")
        record_id = written["record"]["record_id"]

        # memory_write always stamps the real clock, so the retention/TTL
        # windows below are computed relative to the record's own
        # asserted_at rather than a fixed t0.
        asserted_at = datetime.fromisoformat(
            written["record"]["asserted_at"].replace("Z", "+00:00")
        )
        just_past_ttl = asserted_at + timedelta(days=5, hours=1)
        result = run_memory_maintenance(tools.engine, now=just_past_ttl)
        assert result is not None
        assert result["report"]["archived"] == 0  # TTL hit, but retention not yet elapsed

        well_past_retention = asserted_at + timedelta(days=8)
        result2 = run_memory_maintenance(tools.engine, now=well_past_retention)
        assert result2 is not None
        assert result2["report"]["archived_expired"] == [record_id]
    finally:
        tools.engine.close()
