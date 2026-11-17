"""A bounded pool of PostgreSQL connections, with a deadline per caller.

Part of :mod:`pheasant.persistence.pg_driver` (re-exported there).
"""

from __future__ import annotations

import os
import threading
import time
import weakref
from typing import Any

from pheasant.persistence.pg_dsn import DEFAULT_CONNECT_TIMEOUT, parse_dsn
from pheasant.persistence.pg_errors import Error, InterfaceError, PoolTimeout, translate_error

#: A pooled connection idle at least this long is pinged before it is handed
#: out. Shorter than any idle timeout seen between pheasant and its server.
IDLE_CHECK_SECONDS = 30.0

#: How long ``getconn()`` waits when the caller names no deadline (psycopg_pool's).
DEFAULT_POOL_TIMEOUT = 30.0

_POOLS: weakref.WeakSet[ConnectionPool] = weakref.WeakSet()


def _after_fork_in_child() -> None:
    # A child must not use (or close, which would end the parent's session)
    # a connection it inherited. Start each pool afresh.
    for pool in list(_POOLS):
        pool._reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


class ConnectionPool:
    """A bounded pool of :class:`Connection`, for one process.

    SQLAlchemy's ``QueuePool`` holds the idle connections (most recently used
    first, so an idle-dropped one is the one left at the back), replaces one
    that fails its check, and rolls back on return. In front of it a
    semaphore of ``max_size`` permits is what a caller waits on, with its own
    deadline: ``getconn(timeout=remaining)`` gives up when *this* request has
    to, and no request changes a timeout another one sees. The pool itself
    never waits (``max_overflow=0`` and a permit per connection), so creating
    a connection is the only other wait, bounded by the connect timeout and
    the deadline.

    A connection back within :data:`IDLE_CHECK_SECONDS` is handed out as is;
    one idle longer is pinged first (one round trip) and replaced if dead --
    what pheasant's psycopg pool check did.
    """

    def __init__(
        self,
        dsn: str,
        *,
        max_size: int = 10,
        application_name: str = "pheasant",
        autocommit: bool = False,
        timeout: float = DEFAULT_POOL_TIMEOUT,
    ):
        from sqlalchemy import event
        from sqlalchemy.pool import QueuePool

        self.params = parse_dsn(dsn)
        self.max_size = max(1, int(max_size))
        self.timeout = float(timeout)
        self._application_name = application_name
        self._autocommit = autocommit
        self._local = threading.local()
        self._permits = threading.BoundedSemaphore(self.max_size)
        self._fairies: dict[int, Any] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._pool = QueuePool(
            self._create,
            pool_size=self.max_size,
            max_overflow=0,
            timeout=max(self.timeout, 3600.0),
            use_lifo=True,
            reset_on_return="rollback",
            pre_ping=False,
        )
        event.listen(self._pool, "checkout", self._on_checkout)
        event.listen(self._pool, "checkin", self._on_checkin)
        _POOLS.add(self)

    def _create(self) -> Any:
        from pheasant.persistence.pg_driver import connect

        deadline = getattr(self._local, "deadline", None)
        timeout = self.params.connect_timeout or DEFAULT_CONNECT_TIMEOUT
        if deadline is not None:
            timeout = max(0.05, min(timeout, deadline - time.monotonic()))
        return connect(
            self.params,
            autocommit=self._autocommit,
            application_name=self._application_name,
            connect_timeout=timeout,
        )

    @staticmethod
    def _on_checkout(dbapi_connection: Any, record: Any, proxy: Any) -> None:
        from sqlalchemy.exc import DisconnectionError

        returned = dbapi_connection.returned_at
        if returned is not None and time.monotonic() - returned < IDLE_CHECK_SECONDS:
            return
        if returned is None:
            return  # just created: it has answered its own startup
        try:
            dbapi_connection.ping()
        except Error as exc:
            raise DisconnectionError(str(exc)) from exc

    @staticmethod
    def _on_checkin(dbapi_connection: Any, record: Any) -> None:
        if dbapi_connection is not None:
            dbapi_connection.returned_at = time.monotonic()

    def getconn(self, timeout: float | None = None) -> Any:
        """A connection for this thread's unit of work; :class:`PoolTimeout` past ``timeout``."""

        if self._closed:
            raise InterfaceError("the connection pool is closed")
        wait = self.timeout if timeout is None else max(0.0, float(timeout))
        if not self._permits.acquire(timeout=wait):
            raise PoolTimeout(f"couldn't get a connection after {wait:.2f} sec")
        self._local.deadline = time.monotonic() + wait if timeout is not None else None
        try:
            fairy = self._pool.connect()
        except BaseException as exc:
            self._permits.release()
            error = translate_error(exc)
            if error is exc:
                inner = getattr(exc, "__cause__", None)
                if isinstance(inner, Error):
                    raise inner from exc
                raise
            raise error from exc
        finally:
            self._local.deadline = None
        conn = fairy.dbapi_connection
        with self._lock:
            self._fairies[id(conn)] = fairy
        return conn

    def putconn(self, conn: Any) -> None:
        """Return ``conn``; a closed one is replaced rather than reused."""

        with self._lock:
            fairy = self._fairies.pop(id(conn), None)
        if fairy is None:
            return
        try:
            if conn.closed:
                fairy.invalidate()
            else:
                fairy.close()
        finally:
            self._permits.release()

    def discard(self, conn: Any) -> None:
        """Close ``conn`` and return its slot; the pool opens another when asked."""

        conn.close()
        self.putconn(conn)

    def check(self) -> None:
        """Drop every idle connection, after a disconnect suggests they are all dead."""

        self._pool.dispose()

    def close(self) -> None:
        self._closed = True
        try:
            self._pool.dispose()
        except Exception:  # noqa: BLE001 - shutting down
            pass

    def _reset_after_fork(self) -> None:
        self._pool = self._pool.recreate()
        from sqlalchemy import event

        event.listen(self._pool, "checkout", self._on_checkout)
        event.listen(self._pool, "checkin", self._on_checkin)
        self._permits = threading.BoundedSemaphore(self.max_size)
        self._fairies = {}
        self._lock = threading.Lock()
        self._local = threading.local()

    @property
    def stats(self) -> dict[str, int]:
        return {
            "max_size": self.max_size,
            "checked_out": len(self._fairies),
            "idle": int(self._pool.checkedin()),
        }
