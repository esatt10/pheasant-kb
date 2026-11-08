"""A heartbeat that does not share what it reports on.

A long-running process -- a sync, a lease, an evaluation batch -- proves it is
alive by updating a row every few seconds, and every reader that decides
"running", "stale" or "take it over" reads that row. So a heartbeat that
stops while the work goes on is worse than no heartbeat: it tells every
reader the work has died.

That happened on the Azure fleet. A 23,691-page web sync committed for
another hour after its ``sync_runs`` heartbeat stopped at 00:41, so
``wait_for_sync`` said ``queued`` about a sync that was visibly indexing, and
a cancel would have been refused as "not running". The beat went through the
same pooled connections as the sync's own work, failed, and said so at debug
level inside a child process whose output is read when it ends.

Two rules, then. :class:`HeartbeatWriter` writes over a connection of its own
-- on Postgres a dedicated autocommit connection outside the pool, with TCP
keepalives and a statement timeout, replaced after any failure -- so the work
cannot starve its own proof of life. And :class:`Heartbeat` says when a beat
fails: a warning on the first failure and every sixth after it, and a line
when it recovers.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from pheasant.persistence.pg_liveness import TCP_KEEPALIVES

logger = logging.getLogger(__name__)

#: How long one heartbeat statement may run before it is a failed beat.
BEAT_TIMEOUT_MS = 10_000

#: After the first failure, every this-many consecutive failures is logged.
WARN_EVERY = 6


class HeartbeatWriter:
    """Heartbeat writes on a connection the work does not use."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self._conn: Any = None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Run one write and commit it; the rows of a ``RETURNING`` clause."""

        backend = self.state.backend
        if getattr(backend, "dsn", None) is None:
            # SQLite: every thread already has a connection of its own.
            if "RETURNING" in sql.upper():
                return [dict(row) for row in self.state.execute_returning(sql, params)]
            self.state.execute(sql, params)
            return []
        conn = self._connect(backend)
        try:
            with conn.cursor() as cursor:
                cursor.execute(backend.dialect.translate(sql), params or None)
                if cursor.description is None:
                    return []
                names = [column.name for column in cursor.description]
                return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
        except Exception:
            self.reset()
            raise

    def _connect(self, backend: Any) -> Any:
        if self._conn is None or self._conn.closed:
            import psycopg

            self._conn = psycopg.connect(
                backend.dsn,
                autocommit=True,
                connect_timeout=10,
                application_name="pheasant-heartbeat",
                options=f"-c statement_timeout={BEAT_TIMEOUT_MS}",
                **TCP_KEEPALIVES,
            )
        return self._conn

    def reset(self) -> None:
        """Drop the connection; the next write opens a fresh one."""

        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - already broken is fine
                pass

    close = reset


class Heartbeat:
    """``beat(writer)`` every ``interval`` seconds on a thread of its own.

    ``beat`` returns ``False`` to stop (the row it updates is no longer this
    process's); anything it raises is a failed beat, logged and retried on
    the next interval over a fresh connection. ``on_failure(count)`` is told
    each consecutive failure, for a caller whose claim lapses after a few.
    """

    def __init__(
        self,
        state: Any,
        name: str,
        beat: Callable[[HeartbeatWriter], bool | None],
        interval_s: float,
        *,
        on_failure: Callable[[int], bool | None] | None = None,
    ) -> None:
        self.name = name
        self.beat = beat
        self.interval_s = interval_s
        self.on_failure = on_failure
        self.writer = HeartbeatWriter(state)
        self.failures = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> Heartbeat:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=f"pheasant-{self.name}", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, timeout_s: float | None = None) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.interval_s * 2) if timeout_s is None else timeout_s)
        self.writer.close()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                keep = self.beat(self.writer)
            except Exception as exc:  # noqa: BLE001 - a failed beat is retried, not fatal
                self.failures += 1
                if self.failures == 1 or self.failures % WARN_EVERY == 0:
                    logger.warning(
                        "%s heartbeat failed (%d in a row): %s", self.name, self.failures, exc
                    )
                if self.on_failure is not None and self.on_failure(self.failures) is False:
                    return
                continue
            if self.failures:
                logger.warning(
                    "%s heartbeat recovered after %d failed beats", self.name, self.failures
                )
                self.failures = 0
            if keep is False:
                return
