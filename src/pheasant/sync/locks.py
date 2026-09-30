from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
    # cross-process writers are excluded by ``EngineLease`` (heartbeat-based),
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


class EngineLeaseError(RuntimeError):
    """A different live engine process already holds the writer lease."""


class EngineLease:
    """Single-writer lease for one state directory (Synapse step 21.2).

    The lease is a JSON file ``<state>/engine.lease`` recording the owning
    PID, hostname, and an ISO heartbeat timestamp that a daemon thread
    refreshes every ``heartbeat_interval_s`` seconds while held. A second
    *process* attempting to acquire against the same state directory fails
    fast with :class:`EngineLeaseError`; a stale lease (heartbeat older than
    ``stale_after_s``, or the owning PID provably dead on this host) is taken
    over with a logged warning. Engines within the same process share the
    lease (PID match), so in-process concurrency is serialized elsewhere
    (``SyncEngine._sync_mutex``), not here.
    """

    def __init__(
        self,
        state_dir: str | Path,
        *,
        heartbeat_interval_s: float = 5.0,
        stale_after_s: float = 30.0,
        wait_timeout_s: float = 0.0,
        poll_interval_s: float = 0.25,
    ):
        self.path = Path(state_dir) / "engine.lease"
        self.heartbeat_interval_s = heartbeat_interval_s
        self.stale_after_s = stale_after_s
        self.wait_timeout_s = wait_timeout_s
        self.poll_interval_s = poll_interval_s
        self._held = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self) -> None:
        """Take the lease, optionally waiting for the current writer.

        Interactive/embedded engines remain fail-fast by default. Server sync
        workers set ``wait_timeout_s`` because two legitimate background
        triggers (most commonly startup indexing and a source just added in
        the UI) should queue, not make the first user-visible sync fail.
        """
        if self._held:
            return
        deadline = time.monotonic() + self.wait_timeout_s
        logged_wait = False
        while True:
            existing = self._read()
            if existing is None:
                break
            pid = existing.get("pid")
            same_host = existing.get("hostname") in (None, socket.gethostname())
            if isinstance(pid, int) and pid == os.getpid() and same_host:
                break  # another engine in this process holds it; share ownership
            elif self._is_stale(existing, same_host):
                logger.warning(
                    "Taking over stale engine lease %s (pid=%s, heartbeat_at=%s)",
                    self.path,
                    pid,
                    existing.get("heartbeat_at"),
                )
                break
            elif time.monotonic() >= deadline:
                raise EngineLeaseError(
                    f"Another pheasant engine (pid={pid}, "
                    f"host={existing.get('hostname')}) holds the writer lease at "
                    f"{self.path} (heartbeat_at={existing.get('heartbeat_at')}). "
                    f"Stop that process before syncing this state directory, or "
                    f"wait ~{self.stale_after_s:.0f}s for the lease to go stale "
                    f"if the process crashed."
                )
            else:
                if not logged_wait:
                    logger.info(
                        "Waiting for pheasant writer lease %s (pid=%s, host=%s)",
                        self.path,
                        pid,
                        existing.get("hostname"),
                    )
                    logged_wait = True
                time.sleep(self.poll_interval_s)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write()
        self._held = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="pheasant-engine-lease",
            daemon=True,
        )
        self._thread.start()

    def release(self) -> None:
        """Stop heartbeating and remove the lease if this process owns it."""
        if not self._held:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.heartbeat_interval_s * 2))
            self._thread = None
        self._held = False
        existing = self._read()
        if existing is not None and existing.get("pid") == os.getpid():
            self.path.unlink(missing_ok=True)

    def _is_stale(self, payload: dict[str, Any], same_host: bool) -> bool:
        heartbeat_raw = payload.get("heartbeat_at")
        try:
            heartbeat = datetime.fromisoformat(str(heartbeat_raw))
        except (TypeError, ValueError):
            return True
        age_s = (datetime.now(UTC) - heartbeat).total_seconds()
        if age_s > self.stale_after_s:
            return True
        pid = payload.get("pid")
        return same_host and isinstance(pid, int) and not _pid_is_running(pid)

    def _read(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write(self) -> None:
        payload = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "heartbeat_at": datetime.now(UTC).isoformat(),
        }
        # Unique per writer. Engines in one process share this lease and each
        # heartbeats it, so a fixed `engine.lease.tmp` let one engine's rename
        # take another's temp file mid-write -- a FileNotFoundError logged from
        # a daemon thread, which on Python 3.12 aborts the interpreter if it
        # lands during shutdown. A failed write removes its own temp file, or
        # unique names would leave one orphan per failure.
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval_s):
            try:
                self._write()
            except OSError as exc:  # pragma: no cover - disk-failure path
                logger.warning("Failed to refresh engine lease %s: %s", self.path, exc)


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

    :class:`EngineLease` permits exactly one writer process per ``/state``
    directory. That is correct for SQLite — which serializes writers anyway —
    and it is the ceiling Phase 35 exists to lift: with several indexers
    available, two different *sources* have no reason to wait for each other.

    So on a shared backend the unit of exclusion becomes the source. Two
    indexers can commit ``docs`` and ``code`` concurrently; two indexers can
    never both commit ``docs``, because a source's artifacts, chunks, graph
    nodes and manifest are one consistent set.

    Exclusion is a single conditional ``UPDATE``: a row is claimed only if it
    is unheld, already ours, or provably stale. The database decides, so two
    processes racing cannot both win — a read-then-write would let them.

    **This does not replace ``EngineLease`` on SQLite.** There, one writer per
    file is the honest model and the whole-state lease still enforces it.
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
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

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
        if not was_held and self._thread is None:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.heartbeat_interval_s * 2))
            self._thread = None
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
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"pheasant-source-lease-{self.source_id}",
            daemon=True,
        )
        self._thread.start()

    def _heartbeat_loop(self) -> None:
        failures = 0
        while not self._stop.wait(self.heartbeat_interval_s):
            try:
                result = self.state.conn.execute(
                    "UPDATE source_leases SET heartbeat_at=? "
                    "WHERE source_id=? AND owner=? RETURNING owner",
                    (self._now(), self.source_id, self.owner),
                )
                rows = list(result)
                self.state.conn.commit()
                failures = 0
                if not rows:
                    self._held = False
                    logger.error(
                        "Lease ownership was lost for %s; stopping its heartbeat",
                        self.source_id,
                    )
                    return
            except Exception as exc:  # pragma: no cover - disk/network blip
                failures += 1
                logger.warning("Lease heartbeat failed for %s: %s", self.source_id, exc)
                if failures >= 3:
                    # Continuing to orchestrate after three missed heartbeats
                    # risks overlapping a successor that correctly took the
                    # stale lease. The supervisor observes this and stops all
                    # leader-only services before trying to reacquire.
                    self._held = False
                    logger.error(
                        "Lease heartbeat failed three times for %s; relinquishing leadership",
                        self.source_id,
                    )
                    return

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
