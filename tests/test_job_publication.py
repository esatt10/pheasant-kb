"""Slow/shared snapshot storage cannot hold up mutation completion."""

from __future__ import annotations

import threading

from pheasant.job_publication import SnapshotPublisher
from pheasant.job_snapshots import JobSnapshotStore
from pheasant.jobs import JobRegistry


def test_blocked_snapshot_writer_coalesces_progress_and_preserves_terminal_state(
    tmp_path, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    written = []
    original = JobSnapshotStore.write

    def slow(store, payload):
        entered.set()
        assert release.wait(10)
        written.append(payload)
        original(store, payload)

    monkeypatch.setattr(JobSnapshotStore, "write", slow)
    jobs = JobRegistry(shared_path=tmp_path / "jobs", async_snapshots=True)
    try:
        job = jobs.create("sync", "Small change", ["docs"])
        assert entered.wait(5)
        for index in range(100):
            jobs.progress(job.id, current=index, total=100, source="docs")
        jobs.finish(job.id, "succeeded")
        assert jobs.get(job.id)["status"] == "succeeded"
        assert not jobs._publisher.flush(timeout=0)
        release.set()
        assert jobs._publisher.flush(timeout=5)
        assert len(written) <= 2
        assert written[-1]["status"] == "succeeded"
        assert JobSnapshotStore(tmp_path / "jobs", 200).get(job.id)["active"] is False
    finally:
        release.set()
        jobs.close()


def test_failed_snapshot_write_retries_latest_terminal_payload():
    written = []

    class Store:
        def write(self, payload):
            written.append(payload)
            if len(written) == 1:
                raise OSError("share unavailable")

    publisher = SnapshotPublisher(Store(), interval=0)
    try:
        publisher.submit({"id": "job", "active": False, "status": "succeeded"})
        assert publisher.flush(timeout=5)
        assert len(written) == 2
        assert written[-1]["status"] == "succeeded"
    finally:
        publisher.close()


def test_clearing_finished_job_cannot_be_undone_by_an_inflight_snapshot(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = JobSnapshotStore.write

    def blocked(store, payload):
        entered.set()
        assert release.wait(10)
        original(store, payload)

    monkeypatch.setattr(JobSnapshotStore, "write", blocked)
    jobs = JobRegistry(shared_path=tmp_path / "jobs", async_snapshots=True)
    try:
        job = jobs.create("sync", "Small change", ["docs"])
        assert entered.wait(5)
        jobs.finish(job.id, "succeeded")
        assert jobs.clear(job.id) == 1
        release.set()
        assert jobs._publisher.flush(timeout=5)
        assert not list((tmp_path / "jobs").glob("*.json"))
    finally:
        release.set()
        jobs.close()


def test_snapshot_writes_below_retention_limit_do_not_reread_other_jobs(tmp_path, monkeypatch):
    store = JobSnapshotStore(tmp_path / "jobs", 200)
    monkeypatch.setattr(
        store, "_read", lambda *_: (_ for _ in ()).throw(AssertionError("snapshot reread"))
    )
    store.write({"id": "first", "active": False})
    store.write({"id": "second", "active": True})
