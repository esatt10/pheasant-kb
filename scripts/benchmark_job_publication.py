"""Measure 50 progress updates on the actual share, using disposable job files.

Run inside an indexer: python benchmark_job_publication.py /state
Works on both the previous synchronous registry and the coalesced publisher.
"""

from __future__ import annotations

import inspect
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from pheasant.job_snapshots import JobSnapshotStore
from pheasant.jobs import JobRegistry

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir())
probe = Path(tempfile.mkdtemp(prefix="job-publication-probe-", dir=root))
writes = []
original = JobSnapshotStore.write


def counted(store, payload):
    writes.append(payload["id"])
    original(store, payload)


registry = None
try:
    for index in range(15):
        (probe / f"seed-{index}.json").write_text(
            json.dumps({"id": f"seed-{index}", "active": False, "status": "succeeded"})
        )
    JobSnapshotStore.write = counted
    asynchronous = "async_snapshots" in inspect.signature(JobRegistry).parameters
    registry = JobRegistry(shared_path=probe, **({"async_snapshots": True} if asynchronous else {}))
    started = time.monotonic()
    job = registry.create("sync", "Publication probe", ["probe"])
    for index in range(50):
        registry.progress(job.id, source="probe", current=index, total=50)
    registry.finish(job.id, "succeeded")
    submitted = time.monotonic() - started
    publisher = getattr(registry, "_publisher", None)
    if publisher is not None:
        assert publisher.flush(timeout=30), "snapshot writer did not finish"
    completed = time.monotonic() - started
    assert json.loads((probe / f"{job.id}.json").read_text())["status"] == "succeeded"
    print(
        json.dumps(
            {
                "async": asynchronous,
                "updates": 50,
                "seed_jobs": 15,
                "task_completion_s": submitted,
                "publication_complete_s": completed,
                "snapshot_writes": len(writes),
            }
        ),
        flush=True,
    )
finally:
    if registry is not None and hasattr(registry, "close"):
        registry.close()
    assert probe.parent.resolve() == root.resolve() and probe.name.startswith(
        "job-publication-probe-"
    )
    shutil.rmtree(probe)
