"""A pooled Postgres connection closed while idle is replaced, not handed out.

On Azure Container Apps every connection left idle for a few minutes was
closed between the api tier and the server, and the first statement on each
one raised "server closed the connection unexpectedly": five MCP write tools
failed in a row, one per dead connection, while Postgres logged nothing.
Terminating the backend from the server side is the same event, seen from
the client. Needs a real server (``PHEASANT_TEST_POSTGRES_DSN``).
"""

from __future__ import annotations

import os

import pytest

from pheasant.persistence import pg_liveness
from pheasant.persistence.backends import PostgresBackend

DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()

postgres = pytest.mark.skipif(not DSN, reason="set PHEASANT_TEST_POSTGRES_DSN to run")


def _terminate(backend_pid: int) -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (backend_pid,))


def _pid(backend: PostgresBackend) -> int:
    return int(backend.rows("SELECT pg_backend_pid() AS pid")[0]["pid"])


@postgres
def test_a_connection_killed_while_idle_is_not_handed_out(monkeypatch) -> None:
    monkeypatch.setattr(pg_liveness, "IDLE_CHECK_SECONDS", 0.0)
    backend = PostgresBackend(DSN, pool_size=1)
    try:
        dead = _pid(backend)
        _terminate(dead)

        assert backend.rows("SELECT 1 AS one")[0]["one"] == 1
        assert _pid(backend) != dead
    finally:
        backend.close()


@postgres
def test_a_recently_returned_connection_is_not_pinged(monkeypatch) -> None:
    monkeypatch.setattr(pg_liveness, "IDLE_CHECK_SECONDS", 3600.0)
    backend = PostgresBackend(DSN, pool_size=1)
    pinged: list[object] = []
    from psycopg_pool import ConnectionPool

    real = ConnectionPool.check_connection
    monkeypatch.setattr(
        ConnectionPool,
        "check_connection",
        staticmethod(lambda conn: (pinged.append(conn), real(conn))),
    )
    try:
        backend.rows("SELECT 1")
        first = len(pinged)
        for _ in range(5):
            backend.rows("SELECT 1")
        assert len(pinged) == first
    finally:
        backend.close()


@postgres
def test_connections_ask_for_tcp_keepalives() -> None:
    backend = PostgresBackend(DSN, pool_size=1)
    try:
        dsn = backend._conn().info.dsn
        assert "keepalives=1" in dsn
        assert "keepalives_idle=60" in dsn
    finally:
        backend.release()
        backend.close()


@postgres
def test_a_read_whose_connection_dropped_is_run_again(monkeypatch) -> None:
    """The check runs on checkout; a connection that dies after it, or within
    its window, fails the statement itself. A standalone read was autocommit,
    so it is repeated once on another connection rather than failing a call."""

    monkeypatch.setattr(pg_liveness, "IDLE_CHECK_SECONDS", 3600.0)
    # One connection: with two, the pool can hand the read the live one and
    # the test passes with the retry deleted.
    backend = PostgresBackend(DSN, pool_size=1)
    try:
        dead = _pid(backend)
        _terminate(dead)

        assert backend.rows("SELECT 1 AS one")[0]["one"] == 1
    finally:
        backend.close()


@postgres
def test_sync_events_read_back_on_postgres(tmp_path) -> None:
    """`list_sync_events` ordered by SQLite's `rowid` and had no caller, so it
    had never run on Postgres. `describe_source` and `wait_for_sync` read it."""

    from pheasant.config.schema import PheasantConfig
    from pheasant.persistence.state_store import StateStore

    config = PheasantConfig.model_validate(
        {
            "pheasant": {"name": "events", "defaults": "standalone", "state_path": str(tmp_path)},
            "storage": {"backend": "postgres", "dsn_env": "PHEASANT_TEST_POSTGRES_DSN"},
        }
    )
    state = StateStore.from_config(config, tmp_path / "unused.db")
    try:
        state.migrate()
        state.execute("DELETE FROM sync_events WHERE source_id=?", ("events-src",))
        for n, finished in enumerate(["2026-11-06T15:00:00Z", "2026-11-06T15:10:00Z"]):
            state.append_sync_event(
                f"evt-{n}", "events-src", "sync.completed", "healthy", finished, finished, {"n": n}
            )
        events = state.list_sync_events("events-src", limit=1)
        assert [event["details"]["n"] for event in events] == [1]
    finally:
        state.close()
