"""Defects found by stressing the evaluation and memory planes, pinned.

Each was invisible to the offline suite and surfaced only by running the real
thing -- a real Postgres server, a real SIGKILL, a real ``/exports`` volume,
two writes a second apart -- which is CLAUDE.md rule 10 doing its job. Each is
pinned here at the level the offline suite *can* reach, with the
backend-specific half marked and skipped when there is no server.

They share a shape worth naming. Four are failures of a **scaling** path (a
connection pool, a fleet's run identity, a retention tier, a liveness clock)
that a single-container SQLite region never exercises. And all but one are
*silent*: a timeout, a lying progress row, a report claiming rows were archived
that were not, a correction that stopped applying. Two are the same underlying
mistake in different modules -- a wall-clock second treated as a semantic
boundary -- which is why they are filed together.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest

import pheasant.evaluation as evaluation
from pheasant.evaluation import store as evaluation_store
from pheasant.evaluation.replay import ReplayEngine
from pheasant.evaluation.runner import EVALUATION_LEASE, reclaim_interrupted_runs
from pheasant.sync import log_queue
from pheasant.sync.log_queue import hot_row_count, roll, write_events
from pheasant.telemetry.interactions import InteractionEvent
from tests.test_evaluation_batch import _engine, _seed

DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()

postgres = pytest.mark.skipif(
    not DSN,
    reason="set PHEASANT_TEST_POSTGRES_DSN to a throwaway database to run the pool tests",
)


@pytest.fixture()
def seeded(tmp_path: Path):
    engine = _engine(tmp_path)
    _seed(engine)
    try:
        yield engine
    finally:
        engine.close()


# --------------------------------------------------------------------------
# 1. A read on the request path must not pin a pooled Postgres connection
# --------------------------------------------------------------------------


@postgres
def test_acl_reads_do_not_exhaust_the_connection_pool() -> None:
    """`artifact_acls` runs on every search under `security.acl_enforced`.

    It reached the database through `self.conn.execute`, which under Postgres
    is the *write* path: it marks the thread dirty and deliberately holds the
    connection so a `with conn:` block stays one transaction. A read has
    nothing to commit, so the connection was pinned for the life of the calling
    thread -- and Starlette serves sync endpoints from a 40-slot threadpool
    against a pool of `storage.pool_size` (10). The eleventh thread to serve a
    search blocked for 30s and raised `PoolTimeout`; so did every one after it.

    More threads than the pool is the whole point of the arrangement, so this
    runs eight against a pool of two. It is not a load test: one leaked
    connection per thread is enough to deadlock it.
    """

    from pheasant.persistence.backends import PostgresBackend
    from pheasant.persistence.state_store import StateStore

    backend = PostgresBackend(DSN, pool_size=2)
    store = StateStore(backend=backend)
    store.migrate()

    failures: list[str] = []
    done: list[int] = []

    def search_like_read(n: int) -> None:
        try:
            # Exactly what the ACL post-filter does per search.
            store.artifact_acls([f"file:docs:a{n}.md", f"file:docs:b{n}.md"])
            store.idp_groups_for({f"user:p{n}", f"p{n}"})
            done.append(n)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=search_like_read, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    try:
        assert not failures, f"the pool was exhausted by reads: {failures[:2]}"
        assert len(done) == 8
    finally:
        store.close()


# --------------------------------------------------------------------------
# 2. A settled run is not re-run on top of itself
# --------------------------------------------------------------------------


def test_a_settled_batch_is_skipped_rather_than_replayed_over(
    seeded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fleet's normal scheduled case, and it used to corrupt the run row.

    `open_run` declined to *rewrite* a completed row but returned no signal the
    runner acted on, so the batch replayed the whole cohort-by-variant matrix
    anyway. Three things broke at once, and this pins all three: no replay
    happens, the row keeps saying `completed` with its finished phase, and the
    caller still gets the report.
    """

    first = evaluation.run(seeded)
    assert first.status == "completed"
    before = evaluation_store.run_status(seeded.state, first.run_id)

    replayed: list[str] = []
    real = ReplayEngine.replay_variant

    def counted(self: Any, cohort: Any, variant: Any) -> Any:
        replayed.append(f"{cohort.name}/{variant.variant_id}")
        return real(self, cohort, variant)

    monkeypatch.setattr(ReplayEngine, "replay_variant", counted)
    second = evaluation.run(seeded)

    assert not replayed, f"a settled batch was replayed again: {replayed[:3]}"
    assert second.status == "skipped"
    assert second.run_id == first.run_id
    assert "already completed" in second.skipped_reason

    # The published report comes back with it: the numbers exist, re-deriving
    # them is what was redundant.
    assert second.report["health_vector"] == first.report["health_vector"]
    assert second.gates and second.gates_passed == first.gates_passed

    # And the row a watcher reads is untouched -- not a live batch reporting
    # itself finished with a fraction that fell back to zero.
    after = evaluation_store.run_status(seeded.state, first.run_id)
    for field in ("status", "phase", "completed_units", "total_units", "finished_at"):
        assert after[field] == before[field], field


def test_a_failed_or_interrupted_batch_is_still_resumable(seeded: Any) -> None:
    """The skip must not swallow the recovery path it sits next to.

    `failed` and `interrupted` are terminal but *not* settled: both mean the
    batch did not finish, and picking one up again is exactly what the replay
    checkpoints are for.
    """

    for status in ("interrupted", "failed"):
        seeded.state.execute("DELETE FROM evaluation_runs")
        seeded.state.execute(
            "INSERT INTO evaluation_runs(run_id, kb_id, snapshot_id, started_at, status, "
            "mode, config_digest, attempts) VALUES(?,?,?,?,?,?,?,?)",
            ("run-x", "kb", "kb-x", "2026-01-01T00:00:00Z", status, "current_state", "c", 1),
        )
        claim = evaluation_store.open_run(
            seeded.state,
            run_id="run-x",
            kb_id="kb",
            snapshot_id="kb-x",
            started_at="2026-01-02T00:00:00Z",
            mode="current_state",
            config_digest="c",
        )
        assert claim["claimed"] is True, status
        assert claim["resumed"] is True, status
        assert claim["attempts"] == 2, status


# --------------------------------------------------------------------------
# 3. The hot -> cold roll is honest about what it archived
# --------------------------------------------------------------------------


class _ColdSettings:
    hot_retention_days = 0
    max_rows_per_pass = 50_000
    cold_enabled = True
    cold_retention_days = None


def _seed_ledger(state: Any, kb: str, *, days: tuple[str, ...], per_day: int = 4) -> None:
    write_events(
        state,
        [
            InteractionEvent(
                kb_id=kb,
                operation="/search",
                modality="ui",
                principal="user:ada",
                session_id="cold",
                trace_id=f"{d}{i:016x}".replace("-", "")[:32].ljust(32, "0"),
                span_id=f"{i:016x}",
                started_at=f"{day}T00:00:{i:02d}.000000Z",
                status="ok",
                duration_ms=1.0,
                query_text=f"cold {day} {i}",
                result_count=1,
            )
            for d, day in enumerate(days)
            for i in range(per_day)
        ],
    )


def test_a_roll_that_archived_nothing_does_not_report_rows_as_cold(
    seeded: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cold_enabled` without the analytics extra dropped every expired row and
    reported `disposition: "cold"` -- which an operator reads as "they are in
    /exports". They were gone."""

    from pheasant import analytics

    seeded.state.execute("DELETE FROM interaction_events")
    _seed_ledger(seeded.state, "kb", days=("2026-02-01", "2026-02-02"))

    def unavailable() -> Any:
        raise analytics.AnalyticsUnavailable("no duckdb")

    monkeypatch.setattr(analytics, "duckdb_module", unavailable)
    report = roll(seeded.state, _ColdSettings(), exports_path=tmp_path)

    assert report["partitions"] == []
    assert report["disposition"] == "dropped", "rows that reached no partition are not cold"
    assert report["cold_unavailable"] is True


def test_a_partition_that_could_not_be_written_is_retried_not_duplicated(
    seeded: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full or read-only `/exports` volume, mid-pass.

    The pass used to write day one, raise on day two, and delete nothing -- so
    the retry wrote day one *again*. Cold storage is the one place an outside
    reader reads (`docs/reference/export-schema.md`), and it held the same
    observation twice.
    """

    pytest.importorskip("duckdb")
    seeded.state.execute("DELETE FROM interaction_events")
    _seed_ledger(seeded.state, "kb", days=("2026-02-01", "2026-02-02", "2026-02-03"))
    total = hot_row_count(seeded.state)

    calls = {"n": 0}
    real = log_queue._write_partition

    def flaky(exports_path: Any, day: str, rows: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        return real(exports_path, day, rows)

    monkeypatch.setattr(log_queue, "_write_partition", flaky)
    report = roll(seeded.state, _ColdSettings(), exports_path=tmp_path)
    monkeypatch.setattr(log_queue, "_write_partition", real)

    # The days that were written left the hot store; the one that failed did not.
    kept = hot_row_count(seeded.state)
    assert 0 < kept < total, "a failed day must survive, and a written day must not"
    assert report["deferred_partitions"], report
    assert report["disposition"] == "cold_partial"

    # The retry finishes the job without writing anything twice.
    roll(seeded.state, _ColdSettings(), exports_path=tmp_path)
    assert hot_row_count(seeded.state) == 0

    import duckdb

    connection = duckdb.connect(":memory:")
    try:
        glob = f"{tmp_path}/interactions/dt=*/*.parquet"
        rows = connection.execute(f"SELECT COUNT(*) FROM read_parquet('{glob}')").fetchone()[0]
        unique = connection.execute(
            f"SELECT COUNT(DISTINCT id) FROM read_parquet('{glob}')"
        ).fetchone()[0]
    finally:
        connection.close()
    assert rows == unique == total, f"{rows} rows for {unique} distinct ids (expected {total})"


# --------------------------------------------------------------------------
# 4. A live batch is never declared dead
# --------------------------------------------------------------------------


def test_the_heartbeat_margin_survives_a_narrowed_stale_window() -> None:
    """`evaluation.run_stale_seconds` is operator-configurable; the beat was not.

    Three beats of margin is what makes "the heartbeat expired" mean "the
    process is gone". With the beat fixed at 15s, any window at or below that
    inverted it -- and CLAUDE.md records a CI region running 20s.
    """

    margin = evaluation_store.RUN_HEARTBEAT_MARGIN
    for window in (90.0, 45.0, 30.0, 20.0, 10.0, 5.0, 1.0):
        effective = max(window, evaluation_store.MINIMUM_RUN_STALE_SECONDS)
        beat = evaluation_store.heartbeat_interval_for(window)
        assert beat > 0
        assert effective / beat >= margin, f"window {window}s beats every {beat}s"

    # The default is byte-identical to the constant it always used.
    assert (
        evaluation_store.heartbeat_interval_for(evaluation_store.RUN_STALE_SECONDS)
        == evaluation_store.RUN_HEARTBEAT_SECONDS
    )
    assert evaluation_store.heartbeat_interval_for(None) == evaluation_store.RUN_HEARTBEAT_SECONDS


def test_reclamation_refuses_a_window_no_beat_can_satisfy(seeded: Any) -> None:
    """Reclaiming on a window narrower than the fastest beat is not recovery.

    It frees the `__evaluation__` lease under a batch that never stopped, which
    is the one thing keeping N replicas to one run.
    """

    seeded.state.execute("DELETE FROM evaluation_runs")
    seeded.state.execute(
        "INSERT INTO evaluation_runs(run_id, kb_id, snapshot_id, started_at, status, "
        "mode, config_digest, heartbeat_at, attempts) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "run-live",
            "kb",
            "kb-x",
            evaluation_store.__dict__.get("_now", lambda: None)() or "2026-01-01T00:00:00Z",
            "running",
            "current_state",
            "c",
            # Beating right now.
            _utc_now(),
            1,
        ),
    )
    # Even asked for a window of zero, reclamation uses the floor -- so a run
    # that beat a moment ago is left alone.
    assert reclaim_interrupted_runs(seeded.state, "kb", stale_after_seconds=0.0) == []
    assert evaluation_store.run_status(seeded.state, "run-live")["status"] == "running"


def test_a_live_batch_keeps_its_lease_under_a_narrow_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a slow replay under a narrow window stays alive and keeps
    the lease, so no second replica can start a duplicate."""

    import time

    engine = _engine(tmp_path, run_stale_seconds=5.0)
    _seed(engine)
    try:
        # One replay pair that outlasts the whole window several times over.
        real = ReplayEngine.replay_variant
        slowed = {"done": False}

        def slow(self: Any, cohort: Any, variant: Any) -> Any:
            if not slowed["done"]:
                slowed["done"] = True
                time.sleep(12.0)
            return real(self, cohort, variant)

        monkeypatch.setattr(ReplayEngine, "replay_variant", slow)

        seen: list[dict[str, Any]] = []

        def reclaimer() -> None:
            for _ in range(24):
                time.sleep(0.5)
                got = reclaim_interrupted_runs(engine.state, "kb", stale_after_seconds=5.0)
                if got:
                    held = engine.state.rows(
                        "SELECT source_id FROM source_leases WHERE source_id=?",
                        (EVALUATION_LEASE,),
                    )
                    seen.append({"reclaimed": got, "lease_held": bool(held)})
                    return

        watcher = threading.Thread(target=reclaimer, daemon=True)
        watcher.start()
        outcome = evaluation.run(engine)
        watcher.join(timeout=5)

        assert not seen, f"a live batch was reclaimed mid-flight: {seen}"
        assert outcome.status in ("completed", "truncated")
    finally:
        engine.close()


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------
# 5. Re-asserting an archived claim is decided by content, not by the clock
# --------------------------------------------------------------------------


def test_reviving_an_archived_record_keeps_its_identity(tmp_path: Path) -> None:
    """A correction must not be defeated by a second boundary.

    Consolidation archives a superseded record on the very next pass under the
    default `supersede_retention_days: 0`. Re-asserting that same text later --
    a person, an agent, a re-import; `memory_write` is an open API and the old
    wording is still what people say -- minted a *new* id, because the id
    carries a wall-clock prefix and only the digest part is content-addressed.
    The live correction names the *old* id, so it no longer applied: the
    corrected claim came back **current, beside its own correction**, and
    `current_only` returned both.

    Re-asserted inside the same second it reused the id by luck and stayed
    corrected. That is the tell -- identical operations, different memory,
    decided by a boundary nobody means as a semantic one.
    """

    from pheasant.artifacts.store import MemoryStore

    fact = "The gateway restarts nightly at 0300 UTC."
    fix = "Correction: the gateway restarts nightly at 0400 UTC."

    def sequence(gap_seconds: float) -> dict[str, Any]:
        root = tmp_path / f"mem-{gap_seconds}"
        root.mkdir(parents=True, exist_ok=True)
        store = MemoryStore(root)
        original, _ = store.append(fact, scope="org")
        store.append(fix, scope="org", supersedes=original.record_id)
        archived = store.consolidate(supersede_retention_days=0).archived_superseded
        assert archived == (original.record_id,)
        if gap_seconds:
            import time

            time.sleep(gap_seconds)
        # A fresh reader, as another process or a later beat would be.
        revived, _ = MemoryStore(root).append(fact, scope="org")
        current = MemoryStore(root).list_records(current_only=True)
        return {
            "original_id": original.record_id,
            "revived_id": revived.record_id,
            "current": sorted(record.text for record in current),
        }

    same_second = sequence(0.0)
    next_second = sequence(1.2)

    # The revived record is the same assertion, so it keeps the same id --
    # which is also what keeps it one `memory_record` node and one
    # `supersedes` edge rather than two.
    for outcome in (same_second, next_second):
        assert outcome["revived_id"] == outcome["original_id"]
        assert outcome["current"] == [fix], outcome["current"]

    # And the whole point: the clock does not decide.
    assert same_second["current"] == next_second["current"]


def test_reviving_an_archived_record_nothing_corrected_still_makes_it_current(
    tmp_path: Path,
) -> None:
    """The other half, so the fix does not quietly bury a legitimate revival.

    A TTL-expired record is archived without being superseded. Re-asserting it
    brings it back *and* it is current, because nothing corrected it -- which
    is the behaviour `append` documents.
    """

    from pheasant.artifacts.store import MemoryStore

    root = tmp_path / "ttl"
    root.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(root)
    text = "The planner packs whole sources per region."
    original, _ = store.append(text, scope="org")
    MemoryStore.archive(original)
    assert original.path.with_suffix(".md.archived").exists()
    assert MemoryStore(root).list_records() == []

    import time

    time.sleep(1.2)
    revived, created = MemoryStore(root).append(text, scope="org")
    assert created is True
    assert revived.record_id == original.record_id
    assert [record.text for record in MemoryStore(root).list_records(current_only=True)] == [text]


# --------------------------------------------------------------------------
# 6. A backend migration carries the whole region, or says what it left
# --------------------------------------------------------------------------


def test_every_core_table_is_either_migrated_or_declared_unmigrated() -> None:
    """The guard that would have caught the evaluation plane going missing.

    `pheasant migrate --to postgres` is the step a region takes on its way to a
    fleet, and it copies the tables named in `TABLE_ORDER`. The evaluation
    plane added six tables and none of them reached that tuple, so a migration
    silently dropped every snapshot, cohort, run, metric and *proof* the region
    had -- and reported success, because a table in neither `copied` nor
    `skipped` appears in the output not at all.

    Two of those are unreproducible. Proof comes from a surface where somebody
    said so and nothing can say it again on their behalf; and the cohorts carry
    the frozen anchor, without which the next run builds a new one and every
    trend point after the migration is measured against different questions
    than every point before.

    So this is mechanical rather than a list someone remembers to update, the
    same posture `test_config_surface_freshness.py` takes for the config
    surface.
    """

    import re

    from pheasant.persistence import schema
    from pheasant.persistence.migrate import NOT_MIGRATED, TABLE_ORDER

    core = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", schema.CORE_SCHEMA))
    assert core, "could not read the core schema"

    migrated = set(TABLE_ORDER)
    declared = set(NOT_MIGRATED)

    unaccounted = sorted(core - migrated - declared)
    assert not unaccounted, (
        f"{unaccounted} are in the schema but neither copied by `migrate` nor listed in "
        "NOT_MIGRATED. A migration would drop them silently. Add each to TABLE_ORDER, or "
        "to NOT_MIGRATED with the reason it is safe to leave behind."
    )
    assert not (migrated & declared), sorted(migrated & declared)
    assert not (migrated - core), (
        f"TABLE_ORDER names tables that are not in the schema: {sorted(migrated - core)}"
    )
    assert not (declared - core), (
        f"NOT_MIGRATED names tables that are not in the schema: {sorted(declared - core)}"
    )
    assert all(NOT_MIGRATED.values()), "every excluded table needs a stated reason"


def test_the_evaluation_plane_is_carried_across_a_migration() -> None:
    """The tables themselves, named, so a future reorder cannot quietly drop one."""

    from pheasant.persistence.migrate import TABLE_ORDER

    order = list(TABLE_ORDER)
    for table in (
        "evaluation_snapshots",
        "evaluation_cohorts",
        "evaluation_proofs",
        "evaluation_runs",
        "evaluation_metrics",
    ):
        assert table in order, f"{table} would be dropped by a backend migration"

    # Dependency order: a run names its snapshot, a metric names run, snapshot
    # and cohort. Copied out of order, the rows arrive before what they refer
    # to -- which SQLite tolerates and Postgres does not.
    assert order.index("evaluation_snapshots") < order.index("evaluation_runs")
    assert order.index("evaluation_runs") < order.index("evaluation_metrics")
    assert order.index("evaluation_cohorts") < order.index("evaluation_metrics")


def test_a_roll_where_every_day_deferred_reports_neither_cold_nor_dropped(
    seeded: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `/exports` volume that is entirely unavailable for one pass.

    Nothing was archived *and* nothing was dropped, so the report must say
    neither. Claiming `dropped` here would be the same lie in the other
    direction: an operator reading it would go looking for rows that are still
    sitting in the hot store waiting to be retried.
    """

    seeded.state.execute("DELETE FROM interaction_events")
    _seed_ledger(seeded.state, "kb", days=("2026-02-01", "2026-02-02"))
    before = hot_row_count(seeded.state)

    def always_fails(exports_path: Any, day: str, rows: Any) -> Any:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(log_queue, "_write_partition", always_fails)
    report = roll(seeded.state, _ColdSettings(), exports_path=tmp_path)

    assert report["rolled"] == 0
    assert report["partitions"] == []
    assert sorted(report["deferred_partitions"]) == ["2026-02-01", "2026-02-02"]
    assert report["disposition"] == "cold"
    assert "cold_unavailable" not in report
    assert hot_row_count(seeded.state) == before, "a deferred day must not be deleted"
