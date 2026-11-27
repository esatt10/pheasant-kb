"""The background runner: fair scheduling across tenants, and the reaper.

Runs are picked round-robin by tenant within each workload class, so one
tenant's queue of long builds cannot starve another tenant's short lookups,
and interactive work keeps a lane of its own. Each pass also runs the sandbox
reaper, which is what disposes of a generation whose worker died.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pheasant_harness.common import now
from pheasant_harness.coordinator import CLASS_OF, Coordinator

logger = logging.getLogger(__name__)

LANES = {"interactive": 3, "build": 2}
PER_TENANT = 1


def runnable(harness: Any, limit: int = 50) -> list[dict]:
    """Runs a worker can advance now.

    A run reconciling an *uncertain* effect is excluded: it waits on a person who
    checks the target, and leaving it in the queue would let it take its
    tenant's slot on every pass while never changing.
    """
    rows = harness.state.rows(
        "SELECT id, tenant, workload, updated_at FROM harness_runs r WHERE kb_id=? AND state IN "
        "('accepted','running','verifying','delivering','reconciling') "
        "AND (lease_until IS NULL OR lease_until<?) "
        "AND NOT EXISTS (SELECT 1 FROM harness_effects e WHERE e.run_id=r.id AND "
        "e.state='uncertain') "
        "ORDER BY updated_at LIMIT ?",
        (harness.kb_id, now(), limit * 4),
    )
    return [dict(r) for r in rows]


def select(
    candidates: list[dict], lanes: dict[str, int] | None = None, per_tenant: int = PER_TENANT
) -> list[dict]:
    """Round-robin by tenant inside each class lane, oldest first within a tenant."""
    lanes = lanes or LANES
    picked: list[dict] = []
    for lane, capacity in lanes.items():
        queues: OrderedDict[str, list[dict]] = OrderedDict()
        for row in candidates:
            if CLASS_OF.get(row["workload"]) == lane:
                queues.setdefault(row["tenant"], []).append(row)
        taken: dict[str, int] = {}
        while len([p for p in picked if CLASS_OF.get(p["workload"]) == lane]) < capacity and queues:
            for tenant in list(queues):
                if taken.get(tenant, 0) >= per_tenant or not queues[tenant]:
                    queues.pop(tenant)
                    continue
                picked.append(queues[tenant].pop(0))
                taken[tenant] = taken.get(tenant, 0) + 1
                if len([p for p in picked if CLASS_OF.get(p["workload"]) == lane]) >= capacity:
                    break
    return picked


class Runner:
    def __init__(self, harness: Any, *, interval: float = 0.5, llm: Any = None):
        self.harness = harness
        self.interval = interval
        self.coordinator = Coordinator(harness, llm=llm)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(
            max_workers=sum(LANES.values()), thread_name_prefix="harness"
        )
        self._busy: set[str] = set()
        self._lock = threading.Lock()
        self._last_reap = 0.0

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="harness-runner", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.pass_once()
            except Exception:  # noqa: BLE001 - the runner must outlive one bad pass
                logger.exception("harness runner pass failed")

    def pass_once(self) -> list[str]:
        if now() - self._last_reap > 10:
            self._last_reap = now()
            self.harness.sandboxes.reap()
        with self._lock:
            chosen = [r for r in select(runnable(self.harness)) if r["id"] not in self._busy]
            for row in chosen:
                self._busy.add(row["id"])
        for row in chosen:
            self._pool.submit(self._tick, row["id"])
        return [r["id"] for r in chosen]

    def _tick(self, run_id: str) -> None:
        try:
            self.coordinator.tick(run_id)
        except Exception:  # noqa: BLE001
            logger.exception("harness run %s failed a tick", run_id)
        finally:
            with self._lock:
                self._busy.discard(run_id)


_RUNNERS: dict[int, Runner] = {}


def ensure_runner(harness: Any, llm: Any = None) -> Runner | None:
    if os.environ.get("PHEASANT_HARNESS_RUNNER", "on") == "off":
        return None
    runner = _RUNNERS.get(id(harness))
    if runner is None:
        runner = _RUNNERS[id(harness)] = Runner(harness, llm=llm)
        runner.start()
    return runner
