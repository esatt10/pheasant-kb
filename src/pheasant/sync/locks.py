from __future__ import annotations

import json
import logging
import os
import socket
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pheasant.persistence.heartbeat import Heartbeat, HeartbeatWriter

logger = logging.getLogger(__name__)


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_is_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        # Windows reports a PID that no longer exists as ERROR_INVALID_PARAMETER,
        # rather than ESRCH/ProcessLookupError. A supervised writer killed
        # between requests must be able to reclaim that dead owner's lease.
        if getattr(exc, "winerror", None) == 87:
            return False
        raise
    return True


def _windows_pid_is_running(pid: int) -> bool:
    """Query a process handle: Windows ``os.kill(pid, 0)`` terminates it."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no such PID
            return False
        if error == 5:  # ERROR_ACCESS_DENIED: a live protected process
            return True
        raise ctypes.WinError(error)
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


def _lock_is_active(lock: Path) -> bool:
    try:
        payload = json.loads(lock.read_text(encoding="utf-8"))
    except Exception:
        return False
    # A PID only means something inside the namespace that recorded it. A lock
    # left behind by a killed container names a PID — usually 1 — that is very
    # much alive in the *next* container, so a purely PID-based check declared
    # the source locked forever and bricked every restart. A lock written by
    # anything other than this host/container is stale by definition; live
    # cross-process writers are excluded by ``SourceLease`` (heartbeat-based),
    # not by this file. Legacy locks carry no hostname and are treated the same.
    if payload.get("hostname") != socket.gethostname():
        logger.warning(
            "Clearing stale source lock %s from another host (hostname=%s, pid=%s)",
            lock,
            payload.get("hostname"),
            payload.get("pid"),
        )
        return False
    pid = payload.get("pid")
    return isinstance(pid, int) and _pid_is_running(pid)


def _unlink_lock(lock: Path, *, retry_seconds: float = 0.5) -> None:
    """Remove a lock, riding out a concurrent Windows reader briefly."""

    deadline = time.monotonic() + max(0.0, retry_seconds)
    while True:
        try:
            lock.unlink(missing_ok=True)
            return
        except PermissionError:
            # On Windows, Path.read_text() in a waiting contender can deny the
            # owner's unlink for the few microseconds its handle is open.
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


@contextmanager
def source_lock(
    lock_dir: str | Path,
    source_id: str,
    *,
    timeout_s: float = 0.0,
    poll_interval_s: float = 0.1,
):
    root = Path(lock_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / f"{source_id}.lock"
    payload = {
        "source_id": source_id,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        if lock.exists() and not _lock_is_active(lock):
            _unlink_lock(lock)
        try:
            with lock.open("x", encoding="utf-8") as fh:
                json.dump(payload, fh)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Source is already locked after waiting {max(0.0, timeout_s):.1f}s: "
                    f"{source_id}"
                ) from None
            time.sleep(max(0.01, float(poll_interval_s)))
    try:
        yield
    finally:
        _unlink_lock(lock)


class SourceLeaseError(RuntimeError):
    """Another live writer holds this source's lease."""


#: How often a holder refreshes its lease, and how stale one must be before
#: another process may take it. The gap between them is deliberate: a writer
#: that misses two heartbeats in a row has almost certainly died, while one
#: that is merely slow has three chances to prove otherwise.
SOURCE_HEARTBEAT_SECONDS = 10.0
SOURCE_STALE_SECONDS = 45.0


class SourceLease:
    """A per-**source** write lease, held in the state database (Phase 35.4).

    With several indexers available, two different *sources* have no reason
    to wait for each other, so the unit of exclusion is the source. Two
    indexers can commit ``docs`` and ``code`` concurrently; two indexers can
    never both commit ``docs``, because a source's artifacts, chunks, graph
    nodes and manifest are one consistent set.

    Exclusion is a single conditional ``UPDATE``: a row is claimed only if it
    is unheld, already ours, or provably stale. The database decides, so two
    processes racing cannot both win — a read-then-write would let them.
    """

    def __init__(
        self,
        state: Any,
        source_id: str,
        *,
        owner: str | None = None,
        heartbeat_interval_s: float = SOURCE_HEARTBEAT_SECONDS,
        stale_after_s: float = SOURCE_STALE_SECONDS,
    ):
        self.state = state
        self.source_id = source_id
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}"
        self.heartbeat_interval_s = heartbeat_interval_s
        self.stale_after_s = stale_after_s
        self._held = False
        self._heartbeat: Heartbeat | None = None

    @property
    def held(self) -> bool:
        return self._held

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()

    def _stale_before(self) -> str:
        return (datetime.now(UTC) - timedelta(seconds=self.stale_after_s)).isoformat()

    def try_acquire(self) -> bool:
        """Claim the lease if it is free, ours, or dead. One statement."""

        now = self._now()
        # One statement, and `RETURNING` is what makes it safe. An earlier
        # version ran the conditional INSERT and then a separate SELECT to see
        # who owned the row; between those two statements another writer can
        # take the lease, so both callers could observe themselves as the
        # owner. A concurrency test caught exactly that — six threads, two
        # winners.
        #
        # With RETURNING there is no window: the row comes back only if *this*
        # statement wrote it. A conflicting row that fails the WHERE produces
        # no row at all, which is the loss signal.
        #
        # The WHERE clause is the whole exclusion: an existing lease yields
        # only if it is already ours or its heartbeat has aged out. ISO-8601
        # strings compare lexicographically in chronological order, which is
        # why every timestamp in this codebase is written that way.
        result = self.state.conn.execute(
            "INSERT INTO source_leases(source_id, owner, acquired_at, heartbeat_at) "
            "VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
            "owner=excluded.owner, acquired_at=excluded.acquired_at, "
            "heartbeat_at=excluded.heartbeat_at "
            "WHERE source_leases.owner=excluded.owner "
            "OR source_leases.heartbeat_at < ? "
            "RETURNING owner",
            (self.source_id, self.owner, now, now, self._stale_before()),
        )
        rows = list(result)
        self.state.conn.commit()
        won = bool(rows) and str(rows[0]["owner"]) == self.owner
        if won and not self._held:
            self._held = True
            self._start_heartbeat()
        return won

    def acquire(self, wait_timeout_s: float = 0.0) -> None:
        deadline = time.monotonic() + max(0.0, wait_timeout_s)
        while True:
            if self.try_acquire():
                return
            if time.monotonic() >= deadline:
                holder = self.state.rows(
                    "SELECT owner, heartbeat_at FROM source_leases WHERE source_id=?",
                    (self.source_id,),
                )
                who = dict(holder[0]) if holder else {}
                raise SourceLeaseError(
                    f"another writer holds the lease for source {self.source_id!r} "
                    f"(owner={who.get('owner')}, heartbeat_at={who.get('heartbeat_at')}). "
                    f"It goes stale after {self.stale_after_s:.0f}s if that writer died."
                )
            time.sleep(0.25)

    def release(self) -> None:
        """Drop the lease so the next writer need not wait it out."""

        was_held = self._held
        if not was_held and self._heartbeat is None:
            return
        if self._heartbeat is not None:
            self._heartbeat.stop()
            self._heartbeat = None
        self._held = False
        if not was_held:
            # A conditional heartbeat discovered that ownership was lost.
            # Never delete the row now: it belongs to the successor.
            return
        try:
            # Only ever delete our own row: a lease we already lost to a
            # takeover belongs to someone else now, and removing it would
            # hand a third process a source two writers are using.
            self.state.conn.execute(
                "DELETE FROM source_leases WHERE source_id=? AND owner=?",
                (self.source_id, self.owner),
            )
            self.state.conn.commit()
        except Exception as exc:  # pragma: no cover - shutdown must not raise
            logger.warning("Could not release lease for %s: %s", self.source_id, exc)

    def _start_heartbeat(self) -> None:
        # Its own connection (`persistence.heartbeat`), so a sync that holds
        # every pooled connection cannot starve the proof that its writer is
        # alive -- and three starved beats would hand the source to a second.
        self._heartbeat = Heartbeat(
            self.state,
            f"source-lease-{self.source_id}",
            self._beat,
            self.heartbeat_interval_s,
            on_failure=self._missed,
        ).start()

    def _beat(self, writer: HeartbeatWriter) -> bool:
        rows = writer.execute(
            "UPDATE source_leases SET heartbeat_at=? WHERE source_id=? AND owner=? RETURNING owner",
            (self._now(), self.source_id, self.owner),
        )
        if not rows:
            self._held = False
            logger.error("Lease ownership was lost for %s; stopping its heartbeat", self.source_id)
            return False
        return True

    def _missed(self, failures: int) -> bool:
        if failures < 3:
            return True
        # Continuing to orchestrate after three missed heartbeats risks
        # overlapping a successor that correctly took the stale lease. The
        # supervisor observes this and stops all leader-only services before
        # trying to reacquire.
        self._held = False
        logger.error(
            "Lease heartbeat failed three times for %s; relinquishing leadership", self.source_id
        )
        return False

    def __enter__(self) -> SourceLease:
        self.acquire()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


def release_stale_lease(state: Any, source_id: str, *, stale_before: str) -> bool:
    """Drop a lease whose holder is provably gone. Returns whether one went.

    A lease is normally released by the process that took it. A process that
    was *killed* releases nothing, and the row it left behind keeps saying a
    writer is at work until the staleness window runs out.

    That window is the right answer when nobody knows whether the holder is
    alive. It is the wrong answer once something else has independently
    established that it is dead -- reclaiming its heartbeat-expired run row,
    for instance -- because then the region is refusing work it has already
    concluded nobody is doing.

    The staleness predicate stays in the ``DELETE`` rather than in a read
    before it: the caller's evidence concerns the holder it knew about, and
    between that conclusion and this statement a successor may legitimately
    have taken the lease. Only a row nobody is beating is removable, so a live
    successor survives this call whatever the caller believed.
    """

    try:
        result = state.conn.execute(
            "DELETE FROM source_leases WHERE source_id=? AND heartbeat_at < ? RETURNING owner",
            (source_id, stale_before),
        )
        rows = list(result)
        state.conn.commit()
    except Exception as exc:  # noqa: BLE001 - recovery must not raise
        logger.debug("Could not release the stale lease for %s: %s", source_id, exc)
        return False
    if rows:
        logger.info("Released the stale %s lease held by %s", source_id, str(rows[0]["owner"]))
    return bool(rows)
