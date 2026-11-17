"""A pooled Postgres connection that died while idle is replaced, not handed out.

An idle connection can be closed by something between pheasant and its
server, and nothing notices until the next statement fails on it. On Azure
Container Apps that was every connection idle a few minutes: five MCP calls in
a row raised "server closed the connection unexpectedly", one per dead
connection, while Postgres itself logged nothing.

Two defences, in :mod:`pheasant.persistence.pg_dsn` and
:mod:`pheasant.persistence.pg_pool`. TCP keepalives
(idle 60 s, interval 10 s, count 3) keep an idle flow alive and let the
kernel notice a dead peer. And the pool pings a connection that sat idle long
enough to have been dropped (:data:`IDLE_CHECK_SECONDS`) before handing it
out, replacing one that fails. A connection back sooner is not pinged, so a
busy thread pays no extra round trip.

This module keeps the one question every layer above asks: was that failure
the connection, or the server answering?
"""

from __future__ import annotations

from pheasant.persistence.pg_dsn import (
    KEEPALIVE_COUNT,
    KEEPALIVE_IDLE_SECONDS,
    KEEPALIVE_INTERVAL_SECONDS,
)
from pheasant.persistence.pg_pool import IDLE_CHECK_SECONDS

__all__ = ["IDLE_CHECK_SECONDS", "TCP_KEEPALIVES", "is_disconnect", "is_pool_timeout"]

#: The keepalive settings every connection carries, for logs and docs.
TCP_KEEPALIVES = {
    "keepalives": 1,
    "keepalives_idle": KEEPALIVE_IDLE_SECONDS,
    "keepalives_interval": KEEPALIVE_INTERVAL_SECONDS,
    "keepalives_count": KEEPALIVE_COUNT,
}


def is_disconnect(exc: BaseException) -> bool:
    """Whether ``exc`` is the connection failing rather than the server refusing.

    A connection that was closed or never answered has no SQLSTATE; one the
    server ended -- terminated, shutting down, restarting -- carries class
    ``57P``, and a connection exception class ``08``. None of those says
    anything about the statement, so they are worth repeating on another
    connection. Every other SQLSTATE (a constraint, bad SQL, ``57014`` a
    cancelled statement) is the server answering, and is not. A pool that had
    no free connection is not a disconnect either (:func:`is_pool_timeout`).
    """

    from pheasant.persistence import pg_driver

    if isinstance(exc, pg_driver.Error):
        error: BaseException = exc
    elif type(exc).__module__.startswith("pg8000"):
        error = pg_driver.translate_error(exc)
    else:
        # Not the database's: an OSError from a file is not a dropped connection.
        return False
    if not isinstance(error, pg_driver.OperationalError) or isinstance(
        error, (pg_driver.PoolTimeout, pg_driver.QueryCanceled)
    ):
        return False
    sqlstate = str(error.sqlstate or "")
    return not sqlstate or sqlstate.startswith(("57P", "08"))


def is_pool_timeout(exc: BaseException) -> bool:
    """Whether ``exc`` says no pooled connection became free in time."""

    from pheasant.persistence import pg_driver

    return isinstance(exc, pg_driver.PoolTimeout)
