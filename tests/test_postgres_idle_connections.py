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
