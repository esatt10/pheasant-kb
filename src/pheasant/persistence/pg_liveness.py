"""A pooled Postgres connection that died while idle is replaced, not handed out.

An idle connection can be closed by something between pheasant and its
server, and nothing notices until the next statement fails on it. On Azure
Container Apps that was every connection idle a few minutes: five MCP calls in
a row raised "server closed the connection unexpectedly", one per dead
connection, while Postgres itself logged nothing.

Two defences, both at the pool. TCP keepalives keep an idle flow alive and let
the kernel notice a dead peer. And :class:`IdleCheck` is the pool's ``check``:
a connection that sat in the pool long enough to have been dropped is pinged
on checkout, and one that fails is discarded and another handed over. A
connection back within :data:`IDLE_CHECK_SECONDS` is not pinged, so a busy
thread pays no extra round trip.
"""

from __future__ import annotations

import time
import weakref
from typing import Any

#: A pooled connection idle at least this long is pinged before it is handed
#: out. Shorter than any idle timeout seen between pheasant and its server.
IDLE_CHECK_SECONDS = 30.0

#: libpq TCP keepalives, so an idle connection keeps its network flow alive
#: (Azure expires idle flows after four minutes by default) and a dead peer is
#: detected rather than found by the next statement.
TCP_KEEPALIVES = {
    "keepalives": 1,
    "keepalives_idle": 60,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}


class IdleCheck:
    """The pool's ``check`` callback, and the record of when each connection came back."""

    def __init__(self) -> None:
        self._returned_at: weakref.WeakKeyDictionary[Any, float] = weakref.WeakKeyDictionary()

    def returned(self, conn: Any) -> None:
        """Note that ``conn`` is going back to the pool now."""

        self._returned_at[conn] = time.monotonic()

    def __call__(self, conn: Any) -> None:
        returned = self._returned_at.get(conn)
        if returned is not None and time.monotonic() - returned < IDLE_CHECK_SECONDS:
            return
        from psycopg_pool import ConnectionPool

        ConnectionPool.check_connection(conn)
