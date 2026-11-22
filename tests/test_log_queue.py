"""The log tier: its own queue, its own worker, its own storage tiers.

The tier exists to keep observation off two hot paths -- the request, and the
indexer's ``sync_lock``. These tests pin the parts of that which are easy to
regress silently:

* a batch published by one process is drained by another;
* a redelivered batch is a no-op, so at-least-once delivery is safe;
* a roll is bounded, and cold storage is readable by something that is not
  pheasant.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.persistence.state_store import StateStore
from pheasant.sync.log_queue import (
    DEFAULT_LOG_MAX_ATTEMPTS,
    LogTask,
    NatsLogQueue,
    drop_expired_partitions,
    handle_batch,
    hot_row_count,
    ingest_spool,
    log_queue_from_config,
    roll,
    run_log_maintenance,
    write_events,
)
from pheasant.sync.queue import DONE, PENDING, drain
from pheasant.telemetry.interactions import InteractionEvent, QueueSink, SpoolSink


def _log_queue(index: dict[str, Any], logs: dict[str, Any]) -> NatsLogQueue:
    return NatsLogQueue(
        servers=list(index["nats_servers"]),
        stream=logs["nats_stream"],
        subject=logs["nats_subject"],
        durable=logs["nats_durable"],
    )


@pytest.fixture
def log_queue(nats_queue_settings: dict, nats_log_queue_settings: dict) -> Any:
    queue = _log_queue(nats_queue_settings, nats_log_queue_settings)
    try:
        yield queue
    finally:
        queue.close()


@pytest.fixture
def state(tmp_path: Path) -> StateStore:
    store = StateStore()
    store.migrate()
    return store


def _event(index: int, *, when: str = "2026-01-01T00:00:00.000000Z") -> InteractionEvent:
    return InteractionEvent(
        kb_id="kb",
        operation="search_context",
        trace_id=f"{index:032x}",
        span_id=f"{index:016x}",
        started_at=when,
        session_id=f"s{index % 3}",
        query_text=f"query {index}",
        answer_text=f"answer {index}",
        result_ids=[f"file:docs:{index}.md:branch=none"],
        result_paths=[f"{index}.md"],
        result_count=1,
        top_score=0.5,
    )


# --------------------------------------------------------------------------
# The parameterization did not move the index queue
# --------------------------------------------------------------------------


def test_a_batch_is_best_effort_and_gives_up_sooner_than_a_sync() -> None:
    """Retrying a poisoned batch three times costs more than the data."""

    assert DEFAULT_LOG_MAX_ATTEMPTS < 3


# --------------------------------------------------------------------------
# Publish -> claim -> persist
# --------------------------------------------------------------------------


def test_a_batch_survives_the_process_that_produced_it(state: StateStore, log_queue: Any) -> None:
    queue = log_queue
    QueueSink(queue, kb_id="kb").write([_event(1), _event(2)])

    written = drain(queue, lambda task: handle_batch(state, task), owner="logger-1")

    assert written == [2]
    assert hot_row_count(state) == 2
    assert queue.depth()[DONE] == 1


def test_redelivering_a_batch_writes_no_new_rows(state: StateStore, log_queue: Any) -> None:
    """At-least-once is the queue's normal mode. A double-count here would
    inflate a formation threshold -- a wrong memory, not a wrong number."""

    QueueSink(log_queue, kb_id="kb").write([_event(1), _event(2)])
    claimed = log_queue.claim("logger")
    assert claimed is not None
    payload = claimed.payload

    handle_batch(state, LogTask(id="x", payload=payload))
    handle_batch(state, LogTask(id="x", payload=payload))

    assert hot_row_count(state) == 2


def test_one_malformed_event_does_not_cost_the_whole_batch(state: StateStore) -> None:
    """A batch is inserted in one transaction, so an event with a null
    ``trace_id`` -- a truncated spool line, a garbled payload -- used to raise
    IntegrityError and roll back every good event beside it. The batch then
    nacked, retried, failed identically and dead-lettered: one bad line for
    hundreds of good observations."""

    good = [_event(1).as_json(), _event(2).as_json()]
    payload = {
        "events": [
            good[0],
            "not-an-event",
            {"trace_id": None, "span_id": None},
            {"kb_id": "kb", "operation": "x"},  # no trace/span/started_at
            good[1],
        ]
    }

    written = handle_batch(state, LogTask(id="x", payload=payload))

    assert written == 2
    assert hot_row_count(state) == 2


def test_an_unwritable_event_is_recognised_before_it_reaches_sql() -> None:
    assert _event(1).is_writable
    assert not InteractionEvent(kb_id="kb", operation="x").is_writable
    assert not InteractionEvent(
        kb_id="", operation="x", trace_id="a", span_id="b", started_at="t"
    ).is_writable


# --------------------------------------------------------------------------
# Hot -> cold
# --------------------------------------------------------------------------


def _age_everything(state: StateStore) -> None:
    state.execute("UPDATE interaction_events SET started_at=?", ("2020-03-04T05:06:07.000000Z",))


def test_a_roll_is_bounded_so_it_cannot_stall_the_beat_that_runs_it(
    state: StateStore, tmp_path: Path
) -> None:
    """In one container this runs on the scheduler beat. An unbounded roll
    there is an unbounded stall for every source in the region."""

    settings = PheasantConfig().observability.interactions
    settings.hot_retention_days = 0
    settings.max_rows_per_pass = 3
    write_events(state, [_event(index) for index in range(10)])
    _age_everything(state)

    first = roll(state, settings, exports_path=tmp_path)

    assert first["rolled"] == 3
    assert hot_row_count(state) == 7


def test_cold_storage_is_parquet_a_stranger_can_read(state: StateStore, tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    settings = PheasantConfig().observability.interactions
    settings.hot_retention_days = 0
    settings.cold_enabled = True
    write_events(state, [_event(index) for index in range(5)])
    _age_everything(state)

    report = roll(state, settings, exports_path=tmp_path)

    assert report["rolled"] == 5
    assert report["disposition"] == "cold"
    assert hot_row_count(state) == 0
    # Partitioned by day, so retention can drop whole directories.
    partitions = list((tmp_path / "interactions").glob("dt=*"))
    assert [p.name for p in partitions] == ["dt=2020-03-04"]

    rows = (
        duckdb.connect(":memory:")
        .execute(
            "SELECT count(*), count(DISTINCT session_id) FROM "
            f"read_parquet('{tmp_path}/interactions/**/*.parquet')"
        )
        .fetchone()
    )
    assert rows == (5, 3)


def test_without_cold_storage_expired_rows_are_simply_dropped(
    state: StateStore, tmp_path: Path
) -> None:
    settings = PheasantConfig().observability.interactions
    settings.hot_retention_days = 0
    settings.cold_enabled = False
    write_events(state, [_event(index) for index in range(4)])
    _age_everything(state)

    report = roll(state, settings, exports_path=tmp_path)

    assert report["disposition"] == "dropped"
    assert hot_row_count(state) == 0
    assert not (tmp_path / "interactions").exists()


def test_retention_keeps_rows_inside_the_window(state: StateStore, tmp_path: Path) -> None:
    settings = PheasantConfig().observability.interactions
    settings.hot_retention_days = 7
    fresh = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    write_events(state, [_event(1, when=fresh), _event(2)])

    roll(state, settings, exports_path=tmp_path)

    assert hot_row_count(state) == 1


def test_expired_partitions_are_dropped_whole(tmp_path: Path) -> None:
    """Whole days, never individual rows: a partition is the unit cold
    storage is written in."""

    settings = PheasantConfig().observability.interactions
    settings.cold_retention_days = 30
    root = tmp_path / "interactions"
    old = (datetime.now(UTC) - timedelta(days=90)).strftime("%Y-%m-%d")
    new = datetime.now(UTC).strftime("%Y-%m-%d")
    for day in (old, new):
        (root / f"dt={day}").mkdir(parents=True)
        (root / f"dt={day}" / "part.parquet").write_bytes(b"")

    dropped = drop_expired_partitions(tmp_path, settings)

    assert dropped == [old]
    assert not (root / f"dt={old}").exists()
    assert (root / f"dt={new}").exists()


def test_null_cold_retention_keeps_everything_forever(tmp_path: Path) -> None:
    settings = PheasantConfig().observability.interactions
    assert settings.cold_retention_days is None
    (tmp_path / "interactions" / "dt=1999-01-01").mkdir(parents=True)

    assert drop_expired_partitions(tmp_path, settings) == []
    assert (tmp_path / "interactions" / "dt=1999-01-01").exists()


# --------------------------------------------------------------------------
# The spool
# --------------------------------------------------------------------------


def test_a_spool_is_ingested_once_and_then_removed(state: StateStore, tmp_path: Path) -> None:
    SpoolSink(tmp_path / "spool", owner="api-1").write([_event(1), _event(2)])

    assert ingest_spool(state, tmp_path / "spool") == 2
    assert hot_row_count(state) == 2
    assert not list((tmp_path / "spool").rglob("*.ndjson"))
    assert ingest_spool(state, tmp_path / "spool") == 0


def test_a_truncated_spool_line_does_not_strand_the_batch_behind_it(
    state: StateStore, tmp_path: Path
) -> None:
    """A spool is written by a process that may have been killed mid-line."""

    spool = tmp_path / "spool" / "api-1"
    spool.mkdir(parents=True)
    good = json.dumps(_event(1).as_json())
    (spool / "2026-01-01.ndjson").write_text(f'{good}\n{{"trace_id": "trunc\n')

    assert ingest_spool(state, tmp_path / "spool") == 1


# --------------------------------------------------------------------------
# Rule 7 — nothing happens when nothing is enabled
# --------------------------------------------------------------------------


def test_no_log_queue_by_default(state: StateStore) -> None:
    assert log_queue_from_config(PheasantConfig(), state) is None


def test_a_queue_needs_observation_on_not_just_the_queue_flag(state: StateStore) -> None:
    """Otherwise a region publishes batches nothing will ever produce."""

    config = PheasantConfig.model_validate(
        {"observability": {"interactions": {"enabled": False, "queue": {"enabled": True}}}}
    )
    assert log_queue_from_config(config, state) is None


def test_maintenance_no_ops_fast_when_observation_is_off(state: StateStore) -> None:
    assert run_log_maintenance(state, PheasantConfig()) is None


@pytest.mark.parametrize("backend", ["local", "kafka"])
def test_a_backend_other_than_nats_is_refused(state: StateStore, backend: str) -> None:
    config = PheasantConfig.model_validate(
        {
            "observability": {
                "interactions": {"enabled": True, "queue": {"enabled": True, "backend": backend}}
            }
        }
    )
    with pytest.raises(ValueError, match="NATS JetStream"):
        log_queue_from_config(config, state)


# --------------------------------------------------------------------------
# Postgres: where the parameterized claim is actually contended
# --------------------------------------------------------------------------

POSTGRES_DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()

postgres = pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="set PHEASANT_TEST_POSTGRES_DSN to a throwaway database to run the log-queue race test",
)


def _pg_stores() -> tuple[Any, list[Any]]:
    from pheasant.persistence.backends import PostgresBackend

    stores: list[Any] = []

    def new_store() -> StateStore:
        store = StateStore(
            backend=PostgresBackend(os.environ["PHEASANT_DATABASE_URL"], pool_size=6)
        )
        stores.append(store)
        return store

    return new_store, stores


@postgres
def test_the_ledger_round_trips_on_postgres() -> None:
    """Three shapes this repo has already been bitten by, all on this path:
    a declared FK a maintenance path violates, a discarded `cursor.rowcount`,
    and `INSERT OR IGNORE` where `ON CONFLICT DO NOTHING` is required. The
    ledger's idempotent insert and its bounded chunked delete exercise the
    last two directly."""

    pytest.importorskip("pg8000", reason="the [postgres] extra is optional")
    new_store, stores = _pg_stores()
    try:
        store = new_store()
        store.migrate()
        store.execute("DELETE FROM interaction_events", ())

        events = [_event(index) for index in range(1200)]
        assert write_events(store, events) == 1200
        # Redelivery: the portable ON CONFLICT form, not SQLite's INSERT OR
        # IGNORE, which Postgres does not have at all.
        assert write_events(store, events) == 1200
        assert hot_row_count(store) == 1200

        settings = PheasantConfig().observability.interactions
        settings.hot_retention_days = 0
        store.execute(
            "UPDATE interaction_events SET started_at=?", ("2020-03-04T05:06:07.000000Z",)
        )
        # Crosses the 500-row delete chunk boundary several times.
        report = roll(store, settings, exports_path=Path("/tmp"))
        assert report["rolled"] == 1200
        assert hot_row_count(store) == 0
    finally:
        for store in stores:
            store.close()


@postgres
def test_an_older_postgres_ledger_gains_the_new_columns() -> None:
    """Postgres returns early from `migrate()` and takes its own path, so an
    additive column has to be added in *both* places or it exists on exactly
    one backend -- and `CREATE TABLE IF NOT EXISTS` cannot widen a table that
    is already there. Without this, a Postgres /state written by an earlier
    build would fail every ledger insert on a missing column.
    """

    pytest.importorskip("pg8000", reason="the [postgres] extra is optional")
    new_store, stores = _pg_stores()
    try:
        store = new_store()
        store.execute("DROP TABLE IF EXISTS interaction_events", ())
        # The shape before answer_text and result_paths_json existed.
        store.execute(
            """CREATE TABLE interaction_events (
                 id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, trace_id TEXT NOT NULL,
                 span_id TEXT NOT NULL, parent_span_id TEXT, modality TEXT NOT NULL,
                 operation TEXT NOT NULL, principal TEXT, session_id TEXT, client_id TEXT,
                 started_at TEXT NOT NULL, duration_ms REAL, status TEXT NOT NULL,
                 query_text TEXT, criteria_json TEXT, result_ids_json TEXT,
                 result_count INTEGER, top_score REAL, attributes_json TEXT,
                 schema_version INTEGER NOT NULL DEFAULT 1)""",
            (),
        )
        # A stale marker must not let the upgrade be skipped, which is why the
        # probe names the newest column rather than just the table.
        store.execute("DELETE FROM pheasant_schema_meta WHERE key=?", ("core",))
        store.migrate()

        columns = store.backend.table_columns("interaction_events")
        assert {"answer_text", "result_paths_json"} <= columns
        assert write_events(store, [_event(1)]) == 1
        row = dict(
            store.rows("SELECT answer_text, result_paths_json FROM interaction_events", ())[0]
        )
        assert row["answer_text"] == "answer 1"
        assert json.loads(row["result_paths_json"]) == ["1.md"]
    finally:
        for store in stores:
            store.close()


def test_a_batch_published_by_one_process_is_drained_by_another(
    tmp_path: Path, nats_queue_settings: dict, nats_log_queue_settings: dict
) -> None:
    """The property the whole tier rests on, and one an in-process fake cannot
    show: a `--role logger` is a *different process* from the API replica that
    observed the call. Cross-process visibility is also where this codebase has
    been bitten before (an in-memory registry that looked fine in one process),
    so it is asserted rather than assumed.
    """

    import subprocess
    import sys
    import textwrap

    store = StateStore()
    store.migrate()
    store.close()
    producer = _log_queue(nats_queue_settings, nats_log_queue_settings)
    QueueSink(producer, kb_id="kb").write([_event(1), _event(2), _event(3)])
    producer.close()
    stream = (
        nats_queue_settings["nats_servers"][0],
        nats_log_queue_settings["nats_stream"],
        nats_log_queue_settings["nats_subject"],
        nats_log_queue_settings["nats_durable"],
    )

    # A genuinely separate interpreter, holding its own connection.
    drained = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""
                from pheasant.persistence.state_store import StateStore
                from pheasant.sync.log_queue import NatsLogQueue, handle_batch, hot_row_count
                from pheasant.sync.queue import drain

                server, stream, subject, durable = {stream!r}
                store = StateStore()
                store.migrate()
                queue = NatsLogQueue(
                    servers=[server], stream=stream, subject=subject, durable=durable
                )
                written = drain(
                    queue,
                    lambda task: handle_batch(store, task),
                    owner="logger-process",
                )
                print(sum(written), hot_row_count(store))
                queue.close()
                store.close()
            """),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert drained.returncode == 0, drained.stderr
    assert drained.stdout.split() == ["3", "3"]

    # And the first process sees the result, not a stale view of its own.
    store = StateStore()
    queue = _log_queue(nats_queue_settings, nats_log_queue_settings)
    try:
        assert hot_row_count(store) == 3
        assert queue.depth()[PENDING] == 0
    finally:
        queue.close()
        store.close()
