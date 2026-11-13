"""Interval-based catch-all incremental sync loop.

The scheduler is the fallback freshness mechanism for sources the
filesystem watcher cannot observe (web collections, APIs, S3): a daemon
thread calls ``SyncEngine.sync_all(mode="incremental")`` every
``sync.scheduler.interval_seconds``. It is a no-op when disabled in config.

Scheduler- and watcher-triggered syncs are serialized through a shared
``sync_lock`` because ``SyncEngine`` is not safe for concurrent syncs within
one process (full crash/concurrency safety lands in Step 21.2).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from pheasant.config.schema import PheasantConfig

if TYPE_CHECKING:
    from pheasant.sync.engine import SyncEngine

logger = logging.getLogger(__name__)


class SchedulerService:
    """Daemon-thread interval loop running ``sync_all("incremental")``."""

    def __init__(
        self,
        engine: SyncEngine,
        config: PheasantConfig | None = None,
        *,
        interval_seconds: float | None = None,
        enabled: bool | None = None,
        sync_lock: threading.Lock | None = None,
        log_upkeep: bool = True,
        jobs: Any = None,
    ) -> None:
        self.engine = engine
        #: Where each beat is reported as a job (`sync/background_jobs.py`).
        #: Only with a worker-backed engine, whose progress hook takes the
        #: worker's event dict; ``None`` runs the beat unreported, as before.
        self.jobs = jobs
        resolved = config if config is not None else getattr(engine, "config", None)
        settings = getattr(getattr(resolved, "sync", None), "scheduler", None)
        if interval_seconds is None:
            interval_seconds = getattr(settings, "interval_seconds", 900)
        if enabled is None:
            enabled = getattr(settings, "enabled", True)
        self.interval_seconds = float(interval_seconds)
        self.enabled = bool(enabled)
        self._sync_lock = sync_lock or threading.Lock()
        #: Whether this process is the one that persists and rolls
        #: observations. False on an ``indexer`` that has a ``--role logger``
        #: tier behind it -- see :meth:`_log_upkeep`.
        self.log_upkeep = bool(log_upkeep)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Work a composition root adds to every beat, outside ``sync_lock``
        #: (artifact retention): best-effort, so one failing never stops the beat.
        self.upkeep: list[Any] = []
        #: Monotonic clock of the last evaluation run this process started, so
        #: ``evaluation.minimum_interval_seconds`` is enforced here rather than
        #: leaving a corpus under active indexing -- which changes materially
        #: on every beat -- to fire a run every beat.
        self._last_evaluation: float = 0.0

    def _log_upkeep(self) -> None:
        """Drain and roll the observation ledger, when nobody else will.

        Three cases, and this method is the seam between them:

        * **No log queue.** Whoever observed a call already wrote it, so all
          that is left is the roll -- and this beat is the only timer in the
          process.
        * **A queue, under ``--role all``.** A single container publishes to
          a queue for crash resumption, not to become a fleet, so it drains
          what it publishes here, bounded, exactly as ``sync_all`` does with
          the index queue.
        * **A queue, in a fleet.** A ``--role logger`` tier owns both, and
          this process must keep its hands off: that separation is the whole
          reason the tier exists. ``log_upkeep`` is False and this returns.

        Never raises. It runs inside the beat's own ``try``, but a telemetry
        failure should not even reach that -- the beat's next line is a sync.
        """

        if not self.log_upkeep:
            return
        try:
            from pheasant.sync.log_queue import (
                handle_batch,
                log_queue_from_config,
                publish_depth,
                run_log_maintenance,
            )
            from pheasant.sync.queue import drain

            config, state = self.engine.config, self.engine.state
            queue = log_queue_from_config(config, state)
            if queue is not None:
                # Bounded: a beat must not become a log worker. What it does
                # not reach waits for the next one.
                drain(queue, lambda task: handle_batch(state, task), max_tasks=200)
                publish_depth(queue)
            run_log_maintenance(state, config)
        except Exception:  # noqa: BLE001 - telemetry must never break the beat
            logger.debug("Observation-ledger upkeep failed", exc_info=True)

    def _evaluation_upkeep(self) -> None:
        """Run an effectiveness batch when the region has materially changed.

        **Deliberately outside ``sync_lock``**, like :meth:`_log_upkeep` and for
        the same reason: the beat holds that lock across all its work, and a
        replay of several hundred queries inside it would stall incremental
        sync for every source in the region. A batch is read-only search
        traffic and has no business blocking writes.

        Cheap when there is nothing to do. The manifest is built from digests
        over already-indexed rows, and :func:`material_change` compares it to
        the previous snapshot: counts drifting is not material, only content,
        graph, retrieval-configuration, memory or ACL-policy digests moving.
        A corpus under active indexing does change materially every beat, which
        is what ``minimum_interval_seconds`` is for.

        The run itself claims the evaluation lease, so in a fleet the fact that
        several processes reach this line produces one run rather than several.

        Never raises: this is measurement, and the beat's next line is a sync.
        """

        config = self.engine.config
        settings = getattr(config, "evaluation", None)
        if settings is None or not settings.enabled:
            return
        # Reclamation runs whenever evaluation is on at all, even when the
        # automatic trigger is off: a batch someone started by hand from the UI
        # or the CLI is exactly as capable of being killed with its container,
        # and leaving that row saying `running` forever would show a spinner
        # nobody will ever stop.
        try:
            from pheasant.evaluation.runner import reclaim_interrupted_runs

            reclaim_interrupted_runs(
                self.engine.state,
                config.knowledge_base_id,
                stale_after_seconds=settings.run_stale_seconds,
            )
        except Exception:  # noqa: BLE001 - recovery must never break the beat
            logger.debug("Evaluation reclamation failed", exc_info=True)
        if not settings.on_material_snapshot:
            return
        now = time.monotonic()
        if self._last_evaluation and (now - self._last_evaluation) < float(
            settings.minimum_interval_seconds
        ):
            return
        try:
            from pheasant.evaluation import runner as evaluation_runner
            from pheasant.evaluation import snapshots as evaluation_snapshots
            from pheasant.evaluation import store as evaluation_store

            state = self.engine.state
            manifest = evaluation_snapshots.build_snapshot(
                state,
                config,
                graph=getattr(self.engine.graph_builder, "graph", None),
            )
            # A snapshot id is a digest of the state, so its mere *presence*
            # answers the question: this exact region has already been
            # evaluated and there is nothing new to say. Asking
            # `previous_snapshot` here instead would exclude the current id and
            # compare against the state before it -- reporting a change on every
            # beat after a run, and firing a batch as often as the interval
            # allowed.
            if evaluation_store.load_snapshot(state, manifest.snapshot_id) is not None:
                return
            previous = evaluation_store.previous_snapshot(
                state,
                config.knowledge_base_id,
                before=manifest.created_at,
                exclude=manifest.snapshot_id,
            )
            changed = evaluation_snapshots.material_change(previous, manifest)
            if not changed:
                return
            self._last_evaluation = now
            outcome = evaluation_runner.run_evaluation(self.engine)
            if outcome.status == "skipped":
                logger.debug("Evaluation skipped: %s", outcome.skipped_reason)
            else:
                logger.info(
                    "Evaluation run %s finished (%s); gates %s",
                    outcome.run_id,
                    outcome.status,
                    "passed" if outcome.gates_passed else "FAILED",
                )
        except Exception:  # noqa: BLE001 - measurement must never break the beat
            logger.debug("Evaluation upkeep failed", exc_info=True)

    def _scheduled_sync(self) -> list[Any]:
        """One beat's ``sync_all``, as a job over the pipelines that run by themselves."""

        if self.jobs is None:
            return self.engine.sync_all("incremental", scheduled=True)
        from pheasant.sync.background_jobs import run_sync_job
        from pheasant.sync.pipeline import beat_sources, default_interval

        state = getattr(self.engine, "state", None)
        default = default_interval(getattr(self.engine, "config", None), state)
        sources = self.engine.enabled_sources()
        names = [s.name for s in beat_sources(sources, state, default_interval=default)]
        return run_sync_job(
            self.jobs,
            "Scheduled sync",
            names,
            lambda progress: self.engine.sync_all(
                "incremental", scheduled=True, on_progress=progress
            ),
        )

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running or not self.enabled or self.interval_seconds <= 0:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="pheasant-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("Scheduler started (interval=%ss)", self.interval_seconds)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10)
            self._thread = None
            logger.info("Scheduler stopped")

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                with self._sync_lock:
                    results = self._scheduled_sync()
                    # Step 33.2: memory consolidation rides the same beat —
                    # archive superseded/expired records, then a full re-sync
                    # of the (small) memory source drops them from the index.
                    # No-ops fast when no memory source is configured.
                    from pheasant.artifacts.maintenance import run_memory_maintenance

                    maintenance = run_memory_maintenance(self.engine)
                    # Step 32.4: the IdP group-sync refresh rides the same
                    # beat. No-ops when disabled or not yet due; a fetch
                    # failure is reported (never raised) so the beat
                    # survives and the mapping just ages toward the SLA.
                    from pheasant.security.idp import run_idp_maintenance

                    idp = run_idp_maintenance(self.engine.config, self.engine.state)
            except Exception:
                logger.exception("Scheduled incremental sync failed")
                continue
            if idp and not idp.get("error"):
                logger.debug("IdP group sync refreshed (%s principal(s))", idp["principals"])
            if maintenance and maintenance["report"]["archived"]:
                logger.info(
                    "Memory consolidation archived %s record(s)",
                    maintenance["report"]["archived"],
                )
            logger.debug("Scheduled sync completed for %s source(s)", len(results))
            # Deliberately **outside** `sync_lock`. Rolling a few million
            # observation rows to Parquet inside it would stall incremental
            # sync for every source in the region -- and it is bounded by
            # `max_rows_per_pass` on top, so a backlog costs several beats
            # rather than one long one. No-ops fast when observation is off.
            self._log_upkeep()
            for task in list(self.upkeep):
                try:
                    task()
                except Exception:  # noqa: BLE001 - upkeep must never break the beat
                    logger.warning("Scheduler upkeep failed", exc_info=True)
            # Same posture, same reason: outside `sync_lock`, bounded by its
            # own interval, and a no-op unless the region has materially
            # changed. See `_evaluation_upkeep`.
            self._evaluation_upkeep()
