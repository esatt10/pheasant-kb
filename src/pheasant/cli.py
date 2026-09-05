from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pheasant.sync.worker import PROGRESS_MARKER as worker_progress_marker


def _print_scan(report: dict) -> None:
    """Human-readable pre-flight for one source.

    The point of the depth table is that a depth cap should be picked from
    evidence — "depth 2 gets me 900 files, depth 3 gets me 40,000" — rather
    than guessed and then discovered by an OOM.
    """
    name = report.get("source_id")
    if not report.get("scannable"):
        print(f"{name}: not scannable — {report.get('reason')}")
        return
    print(f"{name} ({report.get('path')})")
    print(
        f"  would index {report['file_count']} files, {report['total_mb']} MB "
        f"(scanned {report['entries_scanned']} entries, "
        f"pruned {report['directories_pruned']} directories)"
    )
    subtree = report.get("bytes_by_subtree") or {}
    if subtree:
        top = ", ".join(
            f"{key} ({value // (1024 * 1024)} MB)" for key, value in list(subtree.items())[:5]
        )
        print(f"  largest subtrees: {top}")
    options = report.get("depth_options") or []
    if len(options) > 1:
        table = "  ".join(f"depth {o['max_depth']}={o['files']}" for o in options[:8])
        print(f"  files by depth cap: {table}")
    if report.get("oversized_count"):
        print(f"  {report['oversized_count']} file(s) skipped as oversized")
    if report.get("would_exceed"):
        print(f"  WOULD BE REFUSED: exceeds {', '.join(report['would_exceed'])}")
        print("  narrow with --depth / include / exclude, raise sync.limits, or use --full-scan")
    else:
        print("  within configured limits — sync would proceed")
    _print_projection(report)


def _print_projection(report: dict) -> None:
    """Turn the scan's counts into a sizing answer (Phase 35.7).

    `scan` already walks without reading and reports files and bytes. Those
    are exactly the two inputs the capacity model needs, so the projection is
    free here — and this is the moment it is useful, before anyone has
    committed to a first index and discovered the answer by OOM.
    """

    from pheasant.capacity import project

    projection = report.get("projection")
    if not projection:
        if not report.get("scannable"):
            return
        projection = project(
            int(report.get("file_count") or 0),
            int(report.get("total_bytes") or 0),
        ).as_dict()
    minutes = projection["projected_index_minutes"]
    duration = f"{minutes:.1f} min" if minutes >= 1 else f"{projection['projected_index_seconds']}s"
    print(
        f"  projected: ~{projection['graph_nodes']:,} nodes, "
        f"~{projection['projected_rss_gb']:.1f} GB RAM, "
        f"~{projection['projected_state_gb']:.1f} GB in /state, ~{duration} to index"
    )
    print(f"  suggested container memory: {projection['recommended_memory']}")
    for warning in projection.get("warnings") or []:
        print(f"  ! {warning}")


def _print_evaluation_projection(report: dict) -> None:
    """What an evaluation batch would cost on a corpus this size.

    Printed here because this is the moment an operator is deciding what to
    provision, and because the evaluation plane scales on a *different axis*
    from the corpus: it grows with cohort size times the ablation matrix, not
    with file count. A region with a million files and forty recorded queries
    has a large index and a trivial evaluation, and the reverse is equally
    possible — so a single "how big should this be" number would describe
    neither.

    Only shown when evaluation is switched on: an operator who has not enabled
    it does not need a volume estimate for it.
    """

    evaluation = report.get("evaluation_projection")
    if not evaluation:
        return
    print("evaluation plane (region-wide)")
    print(
        f"  at full cohorts ({evaluation['queries_per_cohort']} queries x "
        f"{evaluation['variants']} variants x {evaluation['cohorts']} cohorts): "
        f"{evaluation['replays_per_run']:,} replays/run, "
        f"~{evaluation['projected_run_minutes']:.1f} min"
    )
    print(
        f"  storage: ~{evaluation['state_mb_per_run']:.0f} MB per run "
        f"(~{evaluation['state_gb_per_year']:.1f} GB/yr at the configured cadence), "
        f"{evaluation['peak_checkpoint_mb']:.1f} MB of replay checkpoints in flight"
    )
    print(f"  suggested container memory for a batch: {evaluation['recommended_memory']}")
    for warning in evaluation.get("warnings") or []:
        print(f"  ! {warning}")


def _sync_services(engine, cfg, config_path=None, policy=None):
    """Build the watcher + scheduler pair sharing one sync serialization lock.

    SyncEngine is not safe for concurrent syncs within a process, so both
    background services funnel their sync calls through the same lock.
    """
    import threading

    from pheasant.sync.scheduler import SchedulerService
    from pheasant.sync.watcher import WatcherService

    sync_lock = threading.Lock()
    if config_path is not None:
        # Serving: background syncs run in a child process, so a scheduled
        # re-index never competes with queries for the GIL.
        from pheasant.sync.worker import WorkerBackedEngine

        engine = WorkerBackedEngine(engine, config_path)
    return (
        WatcherService(engine, cfg, sync_lock=sync_lock),
        SchedulerService(
            engine, cfg, sync_lock=sync_lock, log_upkeep=_owns_log_upkeep(cfg, policy)
        ),
        sync_lock,
    )


def _owns_log_upkeep(cfg, policy) -> bool:
    """Does this process persist and roll the observation ledger?

    With no log queue, yes: whoever observed a call already wrote it, and the
    roll needs some timer. With a queue under ``--role all``, also yes -- a
    single container drains what it publishes, exactly as ``sync_all`` does
    with the index queue. With a queue in a fleet, no: a ``--role logger``
    tier owns it, and an indexer doing the same work would put a
    multi-million-row Parquet roll back on the process holding ``sync_lock``,
    which is the thing the tier exists to prevent.
    """

    from pheasant.deployment.roles import Role

    queue = getattr(
        getattr(getattr(cfg, "observability", None), "interactions", None), "queue", None
    )
    if not getattr(queue, "enabled", False):
        return True
    return policy is None or policy.role is Role.ALL


def _report_ui(app_obj, cfg) -> None:
    """Say whether the web UI is being served, and how to get it if not.

    Going from "the CLI works" to "I can see the graph" is the step people get
    stuck on: the API answers on the port either way, so a missing bundle is
    otherwise silent.
    """
    host = "localhost" if cfg.server.host in ("0.0.0.0", "::") else cfg.server.host
    if getattr(app_obj.state, "ui_dist", None):
        print(f"Web UI:      http://{host}:{cfg.server.port}")
    elif cfg.server.ui.enabled:
        print(
            "Web UI:      not served — no built bundle found.\n"
            "             Build it with `npm --prefix ui ci && npm --prefix ui run build`,\n"
            "             set PHEASANT_UI_DIST to an existing one, or run the container\n"
            "             sidecar (`pheasant host`). See docs/how-to/run-the-ui.md."
        )


def _serve_app(cfg, config_path: str, *, report_ui: bool = True, role: str | None = None) -> None:
    import uvicorn

    from pheasant.api.app import create_app
    from pheasant.deployment.roles import resolve_role

    policy = resolve_role(cfg, role)
    app_obj = create_app(cfg, config_path=config_path, role=policy.name)
    if report_ui and policy.serves_ui:
        _report_ui(app_obj, cfg)
    watcher, scheduler, sync_lock = _sync_services(
        app_obj.state.engine, cfg, config_path=config_path, policy=policy
    )
    # Standalone startup syncs and every background producer share this gate.
    # Postgres indexers additionally hand it to the leader supervisor below.
    app_obj.state.sync_lock = sync_lock
    drainer = _queue_drainer(cfg, config_path, policy, sync_lock=sync_lock)
    log_drainer = _log_drainer(cfg, app_obj.state.engine, policy)
    refresher = _graph_refresher(cfg, app_obj.state.engine, policy)
    orchestration = _orchestration_supervisor(
        app_obj,
        cfg,
        policy,
        watcher=watcher,
        scheduler=scheduler,
        drainer=drainer,
        config_path=config_path,
        sync_lock=sync_lock,
    )
    if orchestration is not None:
        app_obj.state.orchestration = orchestration
        orchestration.start()
    else:
        if policy.runs_watcher:
            watcher.start()
        if policy.runs_scheduler:
            scheduler.start()
        if drainer is not None:
            drainer.start()
    if log_drainer is not None:
        log_drainer.start()
    if refresher is not None:
        refresher.start()
    if not policy.is_default:
        print(f"role: {policy.name}")
    try:
        uvicorn.run(app_obj, host=cfg.server.host, port=cfg.server.port)
    finally:
        if log_drainer is not None:
            log_drainer.stop()
        if refresher is not None:
            refresher.stop()
        if orchestration is not None:
            orchestration.stop()
        else:
            if drainer is not None:
                drainer.stop()
            scheduler.stop()
            watcher.stop()
        app_obj.state.engine.close()


class _OrchestrationSupervisor:
    """Elect one active indexer per knowledge-base shard, with failover.

    Preparation workers are the scalable tier. The graph, manifests, vectors
    and graph-node FTS are one coordinated commit stream, so starting the
    watcher, scheduler and queue drainer on every indexer creates duplicate
    scans and stale whole-graph overwrites. A durable database lease leaves
    extra indexers as hot standbys and promotes one automatically when the
    leader dies or loses its database session.
    """

    def __init__(
        self,
        state,
        knowledge_base: str,
        *,
        watcher,
        scheduler,
        drainer,
        on_promote=None,
        promotion_lock=None,
        poll_interval: float = 1.0,
    ) -> None:
        from pheasant.sync.queue import owner_id

        self.state = state
        self.lease_name = f"__pheasant_orchestrator__:{knowledge_base}"
        self.owner = owner_id()
        self.watcher = watcher
        self.scheduler = scheduler
        self.drainer = drainer
        self.on_promote = on_promote
        self.promotion_lock = promotion_lock
        self.poll_interval = max(0.1, float(poll_interval))
        self._leader = False
        self._stop = None
        self._thread = None
        self._lease = None
        self._promotion_thread = None

    @property
    def leader(self) -> bool:
        return self._leader

    def start(self) -> None:
        import threading

        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="pheasant-orchestrator-election", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=35)
            self._thread = None
        if self._promotion_thread is not None:
            self._promotion_thread.join(timeout=10)

    def _set_metric(self, value: float) -> None:
        try:
            from pheasant.telemetry import metrics

            metrics.REGISTRY.set("pheasant_indexer_leader", value)
        except Exception:  # pragma: no cover - telemetry cannot own leadership
            pass

    def _promote(self) -> None:
        import logging
        import threading

        self._leader = True
        self._set_metric(1.0)
        if self.on_promote is not None and (
            self._promotion_thread is None or not self._promotion_thread.is_alive()
        ):
            # Reserve the global sync gate before any producer starts. The
            # promotion worker releases it after startup reconciliation, while
            # watcher/scheduler/drainer can start immediately and wait behind
            # it without claiming queue work.
            if self.promotion_lock is not None:
                self.promotion_lock.acquire()

            def reconcile() -> None:
                try:
                    self.on_promote()
                except Exception:
                    logging.getLogger("pheasant.cli").exception(
                        "leader startup reconciliation failed"
                    )
                finally:
                    if self.promotion_lock is not None:
                        self.promotion_lock.release()

            self._promotion_thread = threading.Thread(
                target=reconcile,
                name="pheasant-leader-startup",
                daemon=True,
            )
            self._promotion_thread.start()
        if self.watcher is not None:
            self.watcher.start()
        if self.scheduler is not None:
            self.scheduler.start()
        if self.drainer is not None:
            self.drainer.start()
        logging.getLogger("pheasant.cli").info(
            "indexer elected orchestrator for %s", self.lease_name
        )

    def _demote(self) -> None:
        import logging

        if not self._leader:
            return
        # Stop claims first, then producers. This prevents a demoted leader
        # from beginning another queued source while its successor promotes.
        if self.drainer is not None:
            self.drainer.stop()
        if self.scheduler is not None:
            self.scheduler.stop()
        if self.watcher is not None:
            self.watcher.stop()
        self._leader = False
        self._set_metric(0.0)
        logging.getLogger("pheasant.cli").warning(
            "indexer relinquished orchestrator leadership for %s", self.lease_name
        )

    def _run(self) -> None:
        import logging

        from pheasant.sync.locks import SourceLease

        log = logging.getLogger("pheasant.cli")
        self._set_metric(0.0)
        try:
            while not self._stop.is_set():
                if self._lease is None:
                    self._lease = SourceLease(
                        self.state,
                        self.lease_name,
                        owner=self.owner,
                        heartbeat_interval_s=5.0,
                        stale_after_s=20.0,
                    )
                if not self._lease.held:
                    self._demote()
                    try:
                        won = self._lease.try_acquire()
                    except Exception:
                        log.warning("orchestrator election failed; retrying", exc_info=True)
                        won = False
                    if won:
                        self._promote()
                self._stop.wait(self.poll_interval)
        finally:
            self._demote()
            if self._lease is not None:
                self._lease.release()


def _durable_backlog_depth(cfg, state) -> int:
    """Pending/in-flight durable work that should outrank startup scans."""

    from pheasant.sync.queue import queue_from_config

    queue = queue_from_config(cfg, state)
    try:
        depth = queue.depth() if queue is not None else {}
    finally:
        if queue is not None:
            queue.close()
    return int(depth.get("pending", 0)) + int(depth.get("inflight", 0))


def _orchestration_supervisor(
    app_obj,
    cfg,
    policy,
    *,
    watcher,
    scheduler,
    drainer,
    config_path=None,
    sync_lock=None,
):
    """Elect Postgres indexers; standalone/SQLite keeps its direct services."""

    from pheasant.deployment.roles import Role

    if policy.role is not Role.INDEXER or not app_obj.state.state.dialect.is_postgres:
        return None

    def reconcile_on_promotion() -> None:
        import logging

        from pheasant.sync.worker import WorkerBackedEngine

        # Explicit durable work outranks opportunistic on-startup freshness.
        # After a restart, scanning every repository before reclaiming a task
        # made recovery take minutes and could repeat the very stress workload
        # an operator was trying to resume. Watcher/scheduler beats still
        # reconcile these sources once the backlog is gone.
        queued = _durable_backlog_depth(cfg, app_obj.state.state)
        if queued:
            logging.getLogger("pheasant.cli").info(
                "Deferring startup source reconciliation until %d durable task(s) drain",
                queued,
            )
            return

        sources = [
            source.name
            for source in app_obj.state.engine.enabled_sources()
            if source.sync.on_startup
        ]
        if not sources:
            return
        log = logging.getLogger("pheasant.cli")
        log.info("Leader startup sync for sources: %s", ", ".join(sources))
        results = WorkerBackedEngine(app_obj.state.engine, config_path).startup()
        app_obj.state.startup_sync_results = results
        log.info(
            "Leader startup sync complete: sources=%s indexed=%s skipped=%s",
            len(results),
            sum(result.indexed_artifacts for result in results),
            sum(result.skipped_artifacts for result in results),
        )

    return _OrchestrationSupervisor(
        app_obj.state.state,
        cfg.knowledge_base_id,
        watcher=watcher if policy.runs_watcher else None,
        scheduler=scheduler if policy.runs_scheduler else None,
        drainer=drainer,
        on_promote=reconcile_on_promotion,
        promotion_lock=sync_lock,
    )


class _QueueDrainer:
    """Claim and run index tasks for as long as this process serves.

    A background thread rather than a second process: the sync it runs already
    goes to a child through ``WorkerBackedEngine``, so the drain loop itself is
    only waiting, and giving it its own process would buy nothing but a second
    thing to supervise.

    The loop lives here rather than in ``SyncEngine`` because it is a
    *deployment* behavior — it exists only for the indexer role, and an engine
    that drained a queue on its own would do it inside the CLI, the tests, and
    every embedded caller too.
    """

    def __init__(self, cfg, config_path: str, *, sync_lock=None) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.sync_lock = sync_lock
        self._stop = None
        self._thread = None

    def start(self) -> None:
        import threading

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pheasant-drainer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            # Bounded: a task in flight finishes, but a shutdown must not wait
            # out a multi-hour index. The visibility timeout hands an
            # unfinished task to the next indexer, which is exactly the
            # mechanism that makes an abrupt stop safe.
            self._thread.join(timeout=10)

    def _run(self) -> None:
        import logging

        from pheasant.jobs import JobRegistry
        from pheasant.sync.queue import drain, owner_id, queue_from_config

        log = logging.getLogger("pheasant.cli")
        # This long-lived engine coordinates queue/state only. Each claimed
        # task runs in an isolated child that owns graph and vector commits;
        # eagerly loading the same graph here silently doubles peak RSS.
        engine = _engine(
            Path(self.config_path),
            load_persisted_graph=False,
            initialize_indexing_components=False,
        )
        queue = queue_from_config(self.cfg, engine.state)
        if queue is None:
            log.warning("role 'indexer' has no queue configured; nothing will be drained")
            engine.close()
            return
        from pheasant.sync.worker import WorkerBackedEngine

        worker = WorkerBackedEngine(engine, self.config_path)
        jobs = JobRegistry(shared_path=Path(self.cfg.pheasant.state_path) / "jobs")
        owner = owner_id()
        visibility = float(self.cfg.sync.queue.visibility_seconds or 300)
        log.info("draining index queue as %s", owner)
        try:
            while not self._stop.is_set():
                # A short idle timeout rather than a long block: `drain`
                # returns when the backlog clears, and the outer loop is what
                # notices the stop event. Blocking inside `drain` for minutes
                # would make SIGTERM take minutes.
                def handle(task):
                    job = jobs.create(
                        "sync",
                        f"Indexing {task.source_id}",
                        [task.source_id],
                        job_id=task.id,
                    )
                    jobs.progress(
                        job.id,
                        phase="claimed",
                        detail=f"Claimed by {owner}",
                        source=task.source_id,
                    )

                    def forward(event: dict) -> None:
                        meta = event.get("meta") or {}
                        jobs.progress(
                            job.id,
                            phase=event.get("phase"),
                            current=event.get("current"),
                            total=event.get("total"),
                            detail=event.get("detail"),
                            source=meta.get("source") or task.source_id,
                            stats=meta,
                        )

                    try:
                        result = worker.apply_index_task(task, on_progress=forward)
                    except Exception as exc:
                        jobs.finish(job.id, "failed", error=str(exc))
                        raise
                    failed = result.status in {"failed", "timeout", "limit_exceeded"}
                    jobs.finish(
                        job.id,
                        "failed" if failed else "succeeded",
                        error=(result.details.get("error") or result.status) if failed else None,
                        result={
                            "source_id": result.source_id,
                            "indexed_artifacts": result.indexed_artifacts,
                            "skipped_artifacts": result.skipped_artifacts,
                            "status": result.status,
                        },
                    )
                    if failed:
                        raise RuntimeError(result.details.get("error") or result.status)
                    return result

                def drain_once() -> None:
                    drain(
                        queue,
                        handle,
                        owner=owner,
                        idle_timeout=2.0,
                        visibility_seconds=visibility,
                    )

                if self.sync_lock is None:
                    drain_once()
                else:
                    # Acquire before claiming, not inside ``handle``. Otherwise
                    # this drainer can reserve one of a sync-all child's tasks,
                    # block on the lock, and split one shared graph commit over
                    # two child processes after the visibility timeout.
                    with self.sync_lock:
                        drain_once()
        except Exception:  # noqa: BLE001 - a drainer that dies silently is worse
            log.exception("index queue drainer stopped")
        finally:
            queue.close()
            engine.close()


def _queue_drainer(cfg, config_path: str, policy, *, sync_lock=None):
    """The drain loop, or ``None`` when this role does not claim tasks."""

    if not policy.drains_queue:
        return None
    return _QueueDrainer(cfg, config_path, sync_lock=sync_lock)


class _GraphRefresher:
    """Pick up a graph written by another process.

    On the whole-file backend a process loads the graph once at startup, and
    the only other reload path runs after a sync **that process** performed.
    Split the roles and that stops being enough: an api replica never indexes,
    so without this it answers graph queries from whatever the graph was when
    the pod started — indefinitely, and silently, while text and vector search
    stay current from the shared database. Shipping fleet manifests without
    closing that would be shipping a fleet that quietly disagrees with itself.

    On the ``rows`` backend a serving replica reads the graph out of the same
    database, so it *cannot* be stale and there is nothing to reload. This
    still runs: it keeps the process's reported generation in step with the
    published one, and it is what makes the two backends answer ``/ready``
    with the same shape. A refresh that finds nothing to do is the correct
    outcome there, not a missed one.

    Two triggers, and the cheap one is not the primary one (Phase 35.8).
    An indexer announces each committed generation on the fleet's broker, so
    a replica reloads at commit latency rather than up to `interval_seconds`
    later. The poll is **kept** underneath it as a backstop: a dropped
    message, a broker restart, a region with no broker at all, and a legacy
    state directory with no publication record all resolve to "notice on the
    next stat", which is exactly the behavior this had before. That is what
    lets the event path be at-most-once and stateless.

    The subscription handler only sets an event. Reloading a large graph takes
    seconds and holds two generations at once; doing that on the broker
    client's own event loop would stall its keepalive, which is a
    disconnection with extra steps.
    """

    def __init__(self, engine, interval_seconds: float, notifier=None) -> None:
        import threading

        self.engine = engine
        self.interval = max(1.0, float(interval_seconds))
        self.notifier = notifier
        self._stop = None
        self._thread = None
        self._wake = threading.Event()
        self._seen = self._stamp()
        self._subscribed = False

    def _stamp(self):
        return self.engine.graph_store.publication_stamp(self.engine.config.knowledge_base_id)

    def start(self) -> None:
        import threading

        self._stop = threading.Event()
        if self.notifier is not None and self.notifier.enabled:
            self._subscribed = self.notifier.subscribe(
                self.engine.config.knowledge_base_id, self._wake.set
            )
        self._thread = threading.Thread(
            target=self._run, name="pheasant-graph-refresh", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self.notifier is not None:
            self.notifier.close()

    def check_once(self, trigger: str = "poll") -> bool:
        """Reload if the published graph changed. Returns whether it did.

        Still keyed on the store's own publication stamp even when an event
        woke it: a notification is a hint to look, never a fact to act on, so
        a message that arrives twice, arrives for a generation already loaded,
        or is forged costs one cheap read and nothing else.
        """

        stamp = self._stamp()
        if stamp is None or stamp == self._seen:
            return False
        self._seen = stamp
        self.engine.reload_graph()
        from pheasant.telemetry import metrics

        metrics.REGISTRY.inc("pheasant_graph_reloads_total", trigger=trigger)
        # The generation swap drops the old graph, but CPython's allocator can
        # keep hundreds of MiB of its now-empty arenas mapped forever. Repeated
        # refreshes then make steady RSS look like a leak and eventually erase
        # the headroom reserved for the next atomic swap. Collection removes
        # cycles; glibc's optional trim returns fully free pages to the OS.
        import gc

        gc.collect()
        try:
            import ctypes

            trim = getattr(ctypes.CDLL(None), "malloc_trim", None)
            if trim is not None:
                trim(0)
        except (AttributeError, OSError, TypeError):  # non-glibc platforms
            pass
        return True

    def _run(self) -> None:
        import logging

        log = logging.getLogger("pheasant.cli")
        while not self._stop.is_set():
            # The interval is the ceiling on how long a miss can go unnoticed,
            # not the cadence: an announcement returns from this immediately.
            woken = self._wake.wait(self.interval)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                if self.check_once(trigger="event" if woken else "poll"):
                    generation = self.engine.graph_store.published_generation(
                        self.engine.config.knowledge_base_id
                    )
                    log.info(
                        "Reloaded graph generation %s (%s)",
                        (generation or {}).get("generation_id", "unknown"),
                        "announced" if woken else "found by poll",
                    )
            except Exception:  # noqa: BLE001 - a stale graph beats a dead thread
                log.warning("graph refresh failed; will retry", exc_info=True)


class _LogDrainer:
    """The ``--role logger`` loop: claim batches, persist, roll to cold.

    Deliberately its own thread in its own process. Everything it does --
    a multi-row insert per batch, a Parquet write per day rolled, a bounded
    delete after it -- is work that would otherwise land on a process that is
    either answering requests or holding the indexer's ``sync_lock``. Neither
    can afford it at request rates, and that is the entire argument for the
    tier.

    The roll runs on its own, longer cadence than the drain: a batch should be
    persisted within seconds, while rolling a day out of the hot store is
    hourly work at most.
    """

    #: How long a claim loop waits for more work before rolling.
    DRAIN_IDLE_SECONDS = 5.0

    def __init__(self, engine, queue, *, roll_interval_seconds: float = 300.0) -> None:
        import threading

        self._engine = engine
        self._queue = queue
        self._roll_interval = float(roll_interval_seconds)
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        import threading

        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pheasant-logger", daemon=True)
        self._thread.start()
        print("log tier draining (role: logger)")

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=10)
        try:
            self._queue.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass

    def _run(self) -> None:
        import logging
        import time

        from pheasant.sync.log_queue import handle_batch, publish_depth, run_log_maintenance
        from pheasant.sync.queue import drain

        log = logging.getLogger("pheasant.cli")
        state, config = self._engine.state, self._engine.config
        last_roll = 0.0
        while not self._stop.is_set():
            try:
                drain(
                    self._queue,
                    lambda task: handle_batch(state, task),
                    idle_timeout=self.DRAIN_IDLE_SECONDS,
                )
                publish_depth(self._queue)
                if time.monotonic() - last_roll >= self._roll_interval:
                    last_roll = time.monotonic()
                    report = run_log_maintenance(state, config)
                    if report and report.get("rolled"):
                        log.info(
                            "Rolled %s interaction row(s) out of the hot store (%s)",
                            report["rolled"],
                            report.get("disposition"),
                        )
            except Exception:  # noqa: BLE001 - one bad pass must not end the tier
                log.warning("Log tier pass failed", exc_info=True)
                self._stop.wait(5.0)


def _log_drainer(cfg, engine, policy):
    """The log tier's loop, or ``None`` when this role is not it."""

    if not policy.drains_log_queue:
        return None
    from pheasant.sync.log_queue import log_queue_from_config

    queue = log_queue_from_config(cfg, engine.state)
    if queue is None:
        # `validate_role` already refuses this combination at startup, so
        # reaching here means the config changed underneath us.
        return None
    return _LogDrainer(engine, queue)


def _graph_refresher(cfg, engine, policy):
    """The refresh loop, or ``None`` when this role indexes its own graph."""

    from pheasant.sync.graph_events import notifier_from_config

    interval = float(getattr(cfg.server.api, "graph_refresh_seconds", 0) or 0)
    # A remote API has no local snapshot by design. Starting its legacy file
    # refresher would quietly re-materialize the full graph and erase the
    # memory isolation this deployment mode exists to provide.
    if policy.name == "api" and getattr(cfg.graph, "query_service_url", None):
        return None
    if not policy.refreshes_graph or interval <= 0:
        return None
    # Its own client rather than the engine's: the engine's exists to publish
    # and belongs to processes that write graphs, this one exists to listen
    # and belongs to processes that do not. No process does both — an `all`
    # container reloads its own graph in-process and has no refresher at all.
    return _GraphRefresher(engine, interval, notifier=notifier_from_config(cfg))


def _print_evaluation_status(payload: dict) -> None:
    """One line of live progress, read from the row rather than from a process.

    This is the surface an operator uses when the batch is running somewhere
    else -- another container, another terminal, or a process that has since
    been restarted. It says `interrupted` for a run whose heartbeat expired
    rather than printing a bar that will never move.
    """

    status = str(payload.get("status") or "unknown")
    if status == "none":
        print(payload.get("detail") or "No evaluation batch has run for this knowledge base.")
        return
    phase = payload.get("phase") or status
    detail = payload.get("phase_detail")
    done = payload.get("completed_units") or 0
    total = payload.get("total_units") or 0
    fraction = payload.get("fraction")
    bar = ""
    if fraction is not None:
        filled = int(round(fraction * 24))
        bar = f"[{'#' * filled}{'.' * (24 - filled)}] {fraction:.0%} "
    line = f"{status:<12} {bar}{phase}"
    if detail:
        line += f" — {detail}"
    if total:
        line += f"  ({done}/{total} replays)"
    print(line)
    attempts = int(payload.get("attempts") or 1)
    if attempts > 1:
        print(f"  attempt {attempts}: a previous attempt was interrupted and this one resumed it")
    if payload.get("error"):
        print(f"  error: {payload['error']}")
    if payload.get("owner"):
        print(f"  run {payload.get('run_id')} on {payload['owner']}")


def _print_stage_histogram(histogram: dict, *, summary: str = "") -> None:
    """The diagnosis, as a person reads it.

    Ordered by count and labelled with whether a parameter can reach the stage,
    because that is the only thing a reader has to decide: tune, or go fix
    indexing. A bare histogram invites the first answer regardless.
    """

    from pheasant.tuning.stages import ACTIONABLE_STAGES

    evaluated = int(histogram.get("evaluated") or 0)
    misses = int(histogram.get("misses") or 0)
    print(f"  {evaluated} evidenced query/target pairs, {histogram.get('served', 0)} served")
    if not misses:
        print("  no misses to attribute")
    for entry in histogram.get("ranked") or []:
        stage = str(entry["stage"])
        count = int(entry["count"])
        reach = "tunable" if stage in ACTIONABLE_STAGES else "NOT reachable by any parameter"
        bar = "#" * min(30, count)
        print(f"    {stage:<22} {count:>4}  {bar:<30} ({reach})")
    if summary:
        print(f"\n  {summary}")


def _print_tuning_summary(report: dict) -> None:
    decision = report.get("decision") or {}
    diagnosis = report.get("diagnosis") or {}
    experiment = report.get("experiment") or {}
    print(f"Experiment {experiment.get('experiment_id', '?')}")
    print(
        f"  snapshot {experiment.get('snapshot_id', '?')}  "
        f"cohort {experiment.get('cohort_id', '?')}"
    )
    print("\nDiagnosis")
    _print_stage_histogram(diagnosis.get("histogram") or {}, summary=diagnosis.get("summary", ""))
    trials = report.get("trials") or []
    if trials:
        print(
            f"\nTop trials ({report.get('trial_count', len(trials))} run, "
            f"{report.get('searches', 0)} searches)"
        )
        metric = report.get("primary_metric", "")
        for trial in trials[:5]:
            value = (trial.get("metrics") or {}).get(metric)
            shown = "—" if value is None else f"{value:.4f}"
            proposal = trial.get("proposal") or {}
            point = proposal.get("point") or {}
            print(
                f"    {shown:>8}  {point.get('delta_description', ''):<44}"
                f"  [{proposal.get('motivating_stage', '')}, {proposal.get('cost_class', '')}]"
            )
    print(f"\nDecision: {decision.get('outcome', '?')}")
    print(f"  {decision.get('reason', '')}")
    for gate in decision.get("gates") or []:
        mark = "PASS" if gate.get("passed") else ("FAIL" if gate.get("blocking") else "warn")
        print(f"    [{mark}] {gate['gate_id']}: {gate['summary']}")
    bundle = report.get("bundle")
    if bundle:
        print(f"\nBundle {bundle['bundle_id']}  (proposed, not applied)")
        for name, value in sorted((bundle.get("parameters") or {}).items()):
            print(f"    {name} = {value:g}")
        print("\n  Apply it with: pheasant tune apply " + bundle["bundle_id"])


def _print_tuning_status(payload: dict) -> None:
    status = payload.get("status", "none")
    if status == "none":
        print("No tuning batch has run for this knowledge base.")
        return
    fraction = payload.get("progress")
    shown = "—" if fraction is None else f"{fraction:.0%}"
    print(
        f"{payload.get('experiment_id', '?')}  {status}  "
        f"{payload.get('phase', '')}  {shown}  "
        f"({payload.get('completed_units', 0)}/{payload.get('total_units', 0)} units, "
        f"{payload.get('searches', 0)} searches)"
    )
    if payload.get("phase_detail"):
        print(f"  {payload['phase_detail']}")
    if payload.get("error"):
        print(f"  error: {payload['error']}")


def _tune_command(args) -> int:
    """Every `pheasant tune` subcommand.

    Only `run` and `diagnose` load the persisted graph. The rest read rows,
    and materializing a whole corpus's graph to print a table would cost the
    region's memory for nothing -- the same call `pheasant eval status` makes.
    """

    import json as _json
    import time as _time
    from pathlib import Path as _Path

    import pheasant.tuning as tuning
    from pheasant.config.loader import load_config
    from pheasant.tuning import store as tuning_store

    command = args.tune_command
    cfg = load_config(_Path(args.config))
    kb = cfg.knowledge_base_id
    needs_graph = command in {"run", "diagnose"}
    engine = _engine(_Path(args.config), load_persisted_graph=needs_graph)
    try:
        if command in {"run", "diagnose"}:
            if command == "diagnose":
                # A diagnosis is a run with no trial budget: it replays the
                # cohort, attributes the misses and stops. Spelled as a budget
                # rather than a flag so there is one code path, and so what
                # `diagnose` does is exactly the first movement of `run`.
                from pheasant.tuning.strategy import Budget

                outcome = tuning.run(
                    engine,
                    force=True,
                    budget=Budget(refusion_trials=0, requery_trials=0, max_searches=10_000),
                    on_progress=lambda phase, detail: print(
                        f"  {phase}{(': ' + detail) if detail else ''}"
                    ),
                )
            else:
                if args.apply:
                    cfg.tuning.auto.apply = True
                    engine.config.tuning.auto.apply = True
                outcome = tuning.run(
                    engine,
                    force=bool(args.force),
                    on_progress=lambda phase, detail: print(
                        f"  {phase}{(': ' + detail) if detail else ''}"
                    ),
                )
            if outcome.status == "skipped":
                print(f"Skipped: {outcome.skipped_reason}")
                return 0
            if outcome.status not in {"completed", "interrupted"}:
                print(f"{outcome.status}: {outcome.skipped_reason}")
                return 1
            if command == "diagnose":
                if args.json:
                    print(
                        _json.dumps(
                            outcome.diagnosis.as_dict() if outcome.diagnosis else {},
                            indent=2,
                            sort_keys=True,
                            default=str,
                        )
                    )
                    return 0
                print(f"Retrieval diagnosis for {kb}\n")
                _print_stage_histogram(
                    outcome.diagnosis.histogram if outcome.diagnosis else {},
                    summary=outcome.diagnosis.summary if outcome.diagnosis else "",
                )
                return 0
            if args.json:
                print(_json.dumps(outcome.report, indent=2, sort_keys=True, default=str))
            else:
                _print_tuning_summary(outcome.report)
                if outcome.applied:
                    print(f"\nApplied bundle {outcome.bundle_id} — every replica picks it up.")
            # A batch that proposed nothing, or whose winner failed a gate, is
            # a *result*, not an error: exiting non-zero would make a CI job
            # fail for correctly declining to change anything.
            return 0

        if command == "status":
            terminal = {"completed", "failed", "interrupted", "none"}
            while True:
                payload = tuning.progress(engine.state, kb, args.experiment)
                _print_tuning_status(payload)
                if not args.watch or payload.get("status") in terminal:
                    break
                _time.sleep(max(0.5, float(args.interval)))
            return 0

        if command == "report":
            row = (
                tuning_store.experiment_status(engine.state, args.experiment)
                if args.experiment
                else tuning_store.latest_experiment(engine.state, kb)
            )
            report = (row or {}).get("report")
            if not report:
                print("No tuning batch has completed yet. Run `pheasant tune run`.")
                return 1
            if args.json:
                print(_json.dumps(report, indent=2, sort_keys=True, default=str))
            else:
                _print_tuning_summary(report)
            return 0

        if command == "bundles":
            bundles = tuning_store.list_bundles(engine.state, kb)
            if not bundles:
                print("No configuration bundles. Run `pheasant tune run`.")
                return 0
            for item in bundles:
                mark = "* LIVE" if item.get("active") else "      "
                params = ", ".join(
                    f"{k}={v:g}" for k, v in sorted((item.get("parameters") or {}).items())
                )
                print(f"{mark} {item['bundle_id']}  {item.get('created_at', '')}")
                print(f"         {params}")
                if item.get("rationale"):
                    print(f"         {item['rationale'][:150]}")
            return 0

        if command == "show":
            active = tuning.active_parameters(engine.state, kb, cfg)
            if args.yaml:
                import yaml as _yaml

                from pheasant.tuning.bundle import as_config_fragment

                print(_yaml.safe_dump(as_config_fragment(active), sort_keys=True).rstrip())
                return 0
            from pheasant.tuning import objective as objective_module

            objective = objective_module.resolve(cfg.tuning.objective)
            print(f"Tuning for: {objective.label} ({objective.objective_id})")
            print(f"  trades away: {objective.trades_away}\n")
            print(f"{kb} is ranking with parameters from: {active['provenance']}")
            if active["bundle_id"]:
                bundle = active.get("bundle") or {}
                print(f"  bundle {active['bundle_id']}")
                print(f"  applied {bundle.get('applied_at', '')} by {bundle.get('applied_by', '')}")
                print(f"  from experiment {bundle.get('experiment_id', '')}")
            from pheasant.search.ranking import PARAMETER_STAGES

            changes = {change["parameter"]: change for change in active.get("changes") or []}
            for name, value in sorted(active["values"].items()):
                stage = PARAMETER_STAGES.get(name, "?")
                change = changes.get(name)
                # Base shown beside anything the overlay moved, so "what would
                # a rollback give me" is answerable without a second command.
                suffix = f"  (was {change['base']:g} in the base)" if change else ""
                print(f"    {name:<20} {value:<10g} ({stage}){suffix}")
            if changes:
                print("\n  Roll back with: pheasant tune rollback")
            return 0

        if command == "apply":
            try:
                payload = tuning_store.apply_bundle(engine.state, kb, args.bundle, applied_by="cli")
            except KeyError as exc:
                print(str(exc))
                return 1
            print(f"Applied {payload['bundle_id']} to {kb}.")
            print("Every replica reading this /state picks it up within its refresh window.")
            for name, value in sorted((payload.get("parameters") or {}).items()):
                print(f"    {name} = {value:g}")
            print("\n  Undo with: pheasant tune rollback")
            return 0

        if command == "rollback":
            target = getattr(args, "to", "base") or "base"
            if target != "base" and not tuning_store.load_bundle_row(engine.state, kb, target):
                print(f"Unknown bundle: {target}")
                return 1
            reverted = tuning_store.revert_bundle(engine.state, kb, applied_by="cli", to=target)
            if reverted is None:
                print(f"{kb} has no bundle applied; it is already on its configured parameters.")
                return 0
            if target == "base":
                print(f"Stood down {reverted['bundle_id']}. {kb} is back on its configured values.")
            else:
                print(f"Stood down {reverted['bundle_id']}; {kb} is now serving {target}.")
            return 0

        if command == "explain":
            from pheasant.tuning import glossary

            if args.term:
                entry = glossary.lookup(args.term)
                if entry is None:
                    print(f"No explanation for {args.term!r}. Run `pheasant tune explain`.")
                    return 1
                print(f"{entry['label']}  ({entry['kind']}, better = {entry['direction']})\n")
                print(f"  {entry['means']}\n")
                print(f"  If it moves: {entry['impact']}\n")
                print(f"  It does NOT mean: {entry['does_not_mean']}")
                return 0
            catalog = glossary.catalog()
            for group in ("metrics", "health", "stages", "gates", "parameters"):
                print(f"\n{group.upper()}")
                for entry in catalog[group]:
                    print(f"  {entry['term']:<32} {entry['means'][:70]}")
            print("\nReading notes")
            for note in catalog["reading_notes"]:
                print(f"  - {note}")
            print("\n  `pheasant tune explain <term>` for the full entry.")
            return 0

        if command == "lineage":
            history = tuning_store.lineage(engine.state, kb)
            if not history:
                print(f"{kb} has only ever served its configured base parameters.")
                return 0
            for entry in history:
                mark = "* SERVING" if entry["active"] else "         "
                params = ", ".join(f"{k}={v:g}" for k, v in sorted(entry["parameters"].items()))
                print(
                    f"{mark} {entry['bundle_id']}  applied {entry['applied_at']} "
                    f"by {entry['applied_by']}"
                )
                print(f"           {params}")
                if entry["replaced"]:
                    replaced = ", ".join(f"{k}={v:g}" for k, v in sorted(entry["replaced"].items()))
                    print(f"           replaced: {replaced}")
                else:
                    print("           replaced: the configured base")
            print("\n  Roll back with: pheasant tune rollback --to <bundle-id|base>")
            return 0
    finally:
        engine.close()
    return 1


def _print_evaluation_summary(report: dict, *, kb: str) -> None:
    """The terminal view of a run: the vector, the gates, and the caveat.

    The end-user paragraph is printed verbatim rather than re-summarized here.
    It is generated from the same numbers this prints, and writing a second
    prose summary in the CLI is how the two start disagreeing -- with the
    terminal one, which is what people actually read, being the one nobody
    checks against the metrics.
    """

    from pheasant.evaluation.report import HEALTH_VECTOR

    identity = report.get("run_identity", {})
    print(f"\nEvaluation report for {kb}")
    print(f"  run       {identity.get('run_id')}")
    print(f"  snapshot  {identity.get('snapshot_id')}  ({identity.get('mode')})")
    print(
        f"  compared  {identity.get('treatment_variant', identity.get('primary_variant'))} "
        f"against {identity.get('baseline_variant')}"
    )

    print("\nHealth vector (a vector, deliberately — there is no single accuracy score):")
    # Rendered in the registry's own order, not the payload's. A stored report
    # is JSON with sorted keys, so iterating the mapping would put
    # `control_regression` first here and `evidence_coverage` first after a
    # fresh run — two orders for one vector that is meant to be read top to
    # bottom, with its coverage caveat before its scores.
    vector = report.get("health_vector") or {}
    ordered = [label for _metric, label in HEALTH_VECTOR if label in vector]
    ordered += [label for label in vector if label not in set(ordered)]
    for name in ordered:
        entry = vector[name]
        value = entry.get("value")
        rendered = (
            "—"
            if value is None
            else f"{value:+.4f}"
            if "gain" in name or "gap" in name
            else f"{value:.4f}"
        )
        denominator = entry.get("denominator")
        suffix = f" (n={denominator})" if denominator else ""
        print(f"  {name:<34} {rendered:>9}  [{entry.get('status')}]{suffix}")

    gates = report.get("gates") or []
    failed = [gate for gate in gates if not gate.get("passed")]
    print(f"\nHard gates: {len(gates) - len(failed)}/{len(gates)} passed")
    for gate in gates:
        mark = "ok  " if gate.get("passed") else "FAIL"
        print(f"  [{mark}] {gate.get('gate_id'):<28} {gate.get('detail')}")

    decisions = report.get("candidate_decisions") or []
    if decisions:
        print("\nCandidate decisions:")
        for decision in decisions:
            applied = " (applied)" if decision.get("applied") else ""
            print(f"  {decision.get('candidate_id')}: {decision.get('decision')}{applied}")
            for reason in decision.get("reasons", [])[:3]:
                print(f"      · {reason}")

    print("\n" + (report.get("explanations", {}).get("end_user") or ""))
    limitations = report.get("limitations") or {}
    if limitations.get("truncated_replays"):
        print(
            f"\nTruncated: {len(limitations['truncated_replays'])} cohort/variant pairs were not "
            "replayed within the run budget."
        )


def _engine(
    config_path: Path,
    *,
    load_persisted_graph: bool = True,
    defer_persisted_graph_load: bool = False,
    initialize_indexing_components: bool = True,
):
    from pheasant.config.loader import load_config
    from pheasant.persistence.paths import StatePaths
    from pheasant.persistence.state_store import StateStore
    from pheasant.sync.engine import SyncEngine

    cfg = load_config(config_path)
    paths = StatePaths.from_config(cfg)
    paths.ensure()
    state = StateStore.from_config(cfg, paths.sqlite)
    return SyncEngine(
        cfg,
        paths,
        state,
        load_persisted_graph=load_persisted_graph,
        defer_persisted_graph_load=defer_persisted_graph_load,
        initialize_indexing_components=initialize_indexing_components,
    )


#: Re-exported from the worker that parses it. The CLI emits these lines and
#: `sync.worker` reads them, so the constant belongs on the reading side; it
#: keeps its name here because callers and tests import it from the CLI.
PROGRESS_MARKER = worker_progress_marker


def _progress_emitter():
    """A progress hook that writes one NDJSON line per update to stdout.

    This is the wire between the indexing child process and the server's job
    registry (:mod:`pheasant.jobs`). Flushed per line: the whole point is that
    the parent sees movement *while* the sync runs, and Python buffers stdout
    when it is a pipe, which is exactly the case here.

    Throttled by design — a progress line per file over 50,000 files is 50,000
    writes the sync pays for and nobody reads. Updates are emitted on phase
    changes, then at most every 25 items or once a second.
    """
    import time as _time

    # Throttle per source, not globally: with `max_parallel_sources > 1` a
    # single counter meant a fast source could suppress every update from a
    # slow one, which is precisely the source a watcher cares about.
    last: dict[str, dict[str, float | int | str]] = {}

    def emit(
        phase: str,
        current: int,
        total: int | None,
        detail: str,
        meta: dict | None = None,
    ) -> None:
        source = str((meta or {}).get("source") or "")
        now = _time.monotonic()
        seen = last.setdefault(source, {"emitted": 0.0, "current": 0, "phase": ""})
        changed_phase = phase != seen["phase"]
        if (
            not changed_phase
            and current - int(seen["current"]) < 25
            and now - float(seen["emitted"]) < 1.0
        ):
            return
        seen.update({"emitted": now, "current": current, "phase": phase})
        line = json.dumps(
            {
                "marker": PROGRESS_MARKER,
                "phase": phase,
                "current": current,
                "total": total,
                "detail": detail,
                "meta": meta or {},
            }
        )
        print(line, flush=True)

    return emit


def _run_setup(args) -> int:
    """Drive :mod:`pheasant.setup_wizard` and write everything it produced.

    Kept out of ``main`` because it is the only subcommand with real
    control flow, and because the tests drive it through here rather than
    through ``input()``.
    """
    from pheasant.setup_wizard import (
        PROGRESS_FILENAME,
        Prompter,
        SetupSaveAndExit,
        Wizard,
        ensure_gitignored,
        load_progress,
        render_config_yaml,
        save_progress,
        startup_commands,
        write_env_file,
    )

    output = Path(args.output)
    if output.exists() and not args.force:
        print(f"ERROR: {output} already exists. Use --force to overwrite.", file=sys.stderr)
        return 1

    preset: dict = {}
    if args.answers:
        preset = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    progress_path = output.parent / PROGRESS_FILENAME
    resumed_answers, resumed_sources = load_progress(progress_path)
    # An explicit --answers file beats a stale resume file: the user just said
    # what they want, and silently preferring yesterday's half-run would be a
    # surprising way to ignore them.
    merged = {**resumed_answers, **preset}

    wizard = Wizard(
        prompter=Prompter(plain=getattr(args, "plain", False)),
        advanced=args.advanced,
        accept_defaults=args.accept_defaults,
        preset=merged,
    )
    wizard.sources = resumed_sources
    wizard.output_path = str(output)
    wizard.env_output_path = str(args.env_output)
    try:
        wizard.run()
    except SetupSaveAndExit:
        save_progress(progress_path, wizard)
        print(f"\nProgress saved to {progress_path}.")
        return 0
    except KeyboardInterrupt:
        save_progress(progress_path, wizard)
        print(f"\nStopped. Answers so far saved to {progress_path} — re-run to resume.")
        return 130

    try:
        data = wizard.config_dict()
    except (ValueError, TypeError) as exc:
        save_progress(progress_path, wizard)
        print(f"ERROR: those answers do not make a valid config: {exc}", file=sys.stderr)
        return 1

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_config_yaml(data), encoding="utf-8")
    print(f"\nWrote {output}")

    secrets = wizard.env_values()
    if secrets:
        env_path = Path(args.env_output)
        report = write_env_file(env_path, secrets)
        changed = report["added"] + report["updated"]
        print(f"Wrote {env_path} (mode 0600): {', '.join(changed)}")
        note = ensure_gitignored(env_path)
        if note:
            print(note)
    else:
        print("No secrets entered — nothing written to .env.")

    progress_path.unlink(missing_ok=True)
    port = int(data.get("server", {}).get("port") or 8765)
    print("\nNext:")
    for line in startup_commands(output, args.target, port):
        print(f"  {line}")
    if not data.get("sources"):
        print("\nNo sources yet. Add one with:")
        print(f"  pheasant up <folder-or-url> -c {output}")
    return 0


def _run_mount(args) -> int:
    """Write a bind mount for a host directory, and allow-list it in config.

    Both halves matter: a mount alone makes the path visible but still
    unregisterable, and an allow-list entry alone points at nothing.
    """
    import yaml

    from pheasant.config.loader import dump_config_yaml
    from pheasant.deployment.mounts import render_compose_override, suggested_container_path

    host = str(Path(args.path).expanduser())
    container = args.at or suggested_container_path(host)
    override = Path(args.output)
    existing = override.read_text(encoding="utf-8") if override.exists() else None
    try:
        rendered = render_compose_override(
            {host: container}, service=args.service, existing=existing
        )
    except (ValueError, yaml.YAMLError) as exc:
        print(f"ERROR: cannot update {override}: {exc}", file=sys.stderr)
        return 1

    config_path = Path(args.config)
    config_note = None
    config_text = None
    if config_path.exists():
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            print(f"ERROR: cannot read {config_path}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(data, dict):
            print(f"ERROR: {config_path} is not a pheasant config mapping", file=sys.stderr)
            return 1
        security = data.setdefault("security", {})
        roots = list(security.get("allow_workspace_roots") or [])
        if container not in roots:
            # Keep the defaults: replacing the field wholesale with one entry
            # would lock the user out of /workspace.
            if not roots:
                from pheasant.config.schema import SecuritySettings

                roots = [str(p) for p in SecuritySettings().allow_workspace_roots]
            roots.append(container)
            security["allow_workspace_roots"] = roots
            config_text = dump_config_yaml(data)
            config_note = f"allow_workspace_roots += {container}"
        else:
            config_note = f"{container} is already allow-listed"

    if args.print_only:
        print(f"# {override}")
        print(rendered, end="")
        if config_text:
            print(f"\n# {config_path} ({config_note})")
            print(config_text, end="")
        return 0

    override.write_text(rendered, encoding="utf-8")
    print(f"Wrote {override}: {host} -> {container} (read-only)")
    if config_text:
        config_path.write_text(config_text, encoding="utf-8")
        print(f"Updated {config_path}: {config_note}")
    elif config_note:
        print(config_note)
    else:
        print(f"No {config_path} found — add {container} to security.allow_workspace_roots.")
    print("\nApply it with:  docker compose up -d")
    print(f"Then index it:  pheasant up {container} -c {config_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pheasant",
        description="pheasant knowledge graph indexing server",
    )
    sub = parser.add_subparsers(dest="command")
    up_p = sub.add_parser(
        "up",
        help="zero-config quickstart: detect, generate config, index, serve",
    )
    up_p.add_argument(
        "path",
        nargs="*",
        default=["."],
        help=(
            "one or more targets: a folder, file, glob (~/clients/*), git URL, "
            "web URL, s3:// bucket, or connector (notion:workspace)"
        ),
    )
    up_p.add_argument("--config", "-c", default="pheasant.yaml")
    up_p.add_argument("--name", help="knowledge-base name [default: slug of the directory]")
    up_p.add_argument("--port", type=int, default=8765)
    up_p.add_argument("--profile", default="quickstart")
    up_p.add_argument("--mode", default="incremental")
    up_p.add_argument(
        "--split",
        action="store_true",
        help="index each immediate subdirectory of a target as its own source",
    )
    up_p.add_argument(
        "--no-serve",
        action="store_true",
        help="generate + index only; do not start the server",
    )
    host_p = sub.add_parser(
        "host",
        help="one-line container hosting: generate config + compose file, then run it",
    )
    host_p.add_argument("path", nargs="*", default=["."])
    host_p.add_argument("--config", "-c", default="pheasant.yaml")
    host_p.add_argument("--name")
    host_p.add_argument("--port", type=int, default=8765)
    host_p.add_argument("--ui-port", type=int, default=8080)
    host_p.add_argument("--profile", default="quickstart")
    host_p.add_argument("--split", action="store_true")
    host_p.add_argument("--output", "-o", default="docker-compose.pheasant.yml")
    host_p.add_argument("--image")
    host_p.add_argument("--ui-image")
    host_p.add_argument("--no-ui", action="store_true", help="omit the web UI sidecar")
    host_p.add_argument(
        "--print-only",
        action="store_true",
        help="write the compose file but do not run docker compose",
    )
    setup_p = sub.add_parser(
        "setup",
        help="interactive, sectioned wizard: explains every option, writes "
        "pheasant.yaml + a 0600 .env, prints the startup commands",
    )
    setup_p.add_argument("--output", "-o", default="pheasant.yaml")
    setup_p.add_argument("--env-output", default=".env")
    setup_p.add_argument(
        "--advanced",
        action="store_true",
        help="ask about every option, not just the ones that usually matter",
    )
    setup_p.add_argument(
        "--accept-defaults",
        action="store_true",
        help="ask nothing; write a config of defaults (scriptable)",
    )
    setup_p.add_argument(
        "--plain",
        action="store_true",
        help="use wrapped plain text output without color or terminal control sequences",
    )
    setup_p.add_argument(
        "--answers",
        help="JSON file of {dotted.config.key: value} to answer non-interactively",
    )
    setup_p.add_argument(
        "--target",
        choices=("local", "docker", "compose"),
        default="local",
        help="which startup commands to print at the end",
    )
    setup_p.add_argument("--force", action="store_true", help="overwrite an existing config")
    mount_p = sub.add_parser(
        "mount",
        help="make a host directory visible inside the container: write the "
        "bind mount into docker-compose.override.yml and allow-list it",
    )
    mount_p.add_argument("path", help="host directory to mount")
    mount_p.add_argument(
        "--at",
        help=f"container path to mount it at [default: {'/data'}/<dirname>]",
    )
    mount_p.add_argument("--config", "-c", default="pheasant.yaml")
    mount_p.add_argument("--service", default="pheasant")
    mount_p.add_argument("--output", "-o", default="docker-compose.override.yml")
    mount_p.add_argument(
        "--print-only",
        action="store_true",
        help="show what would be written, change nothing",
    )
    start_p = sub.add_parser("start")
    start_p.add_argument("--config", "-c", default="pheasant.yaml")
    start_p.add_argument("--profile", default="quickstart")
    start_p.add_argument("--set", dest="overrides", action="append", default=[])
    validate_p = sub.add_parser("validate")
    validate_p.add_argument("config", nargs="?", default="pheasant.example.yaml")
    validate_p.add_argument("--no-require-paths", action="store_true")
    init_p = sub.add_parser("init")
    init_p.add_argument("--profile", default="quickstart")
    init_p.add_argument("--output", "-o", default="pheasant.yaml")
    init_p.add_argument("--force", action="store_true")
    config_p = sub.add_parser("config")
    config_sub = config_p.add_subparsers(dest="config_command")
    config_show_p = config_sub.add_parser("show")
    config_show_p.add_argument("--effective", action="store_true")
    config_show_p.add_argument("--config", "-c", default="pheasant.yaml")
    config_show_p.add_argument("--profile", default="quickstart")
    config_show_p.add_argument("--set", dest="overrides", action="append", default=[])
    doctor_p = sub.add_parser("doctor")
    doctor_p.add_argument("--config", "-c", default="pheasant.yaml")
    doctor_p.add_argument("--profile", default="quickstart")
    doctor_p.add_argument("--no-require-paths", action="store_true")
    sync_p = sub.add_parser("sync")
    sync_p.add_argument("--config", "-c", default="pheasant.example.yaml")
    sync_p.add_argument("--source", "-s")
    sync_p.add_argument("--all", action="store_true")
    sync_p.add_argument("--mode", default="incremental")
    sync_p.add_argument(
        "--depth",
        type=int,
        default=None,
        help="cap directory depth for this run (0 = only files directly in the source root)",
    )
    sync_p.add_argument(
        "--full-scan",
        action="store_true",
        help="index everything: no depth cap and no size budget (sync.limits is ignored)",
    )
    sync_p.add_argument(
        "--json",
        action="store_true",
        help="emit the result as one JSON object (how the server's sync worker reports back)",
    )
    sync_p.add_argument(
        "--progress",
        action="store_true",
        help="stream NDJSON progress lines on stdout as the sync advances "
        "(how the server's sync worker drives the jobs tray)",
    )
    sync_p.add_argument(
        "--wait-for-lease",
        type=float,
        default=None,
        help=argparse.SUPPRESS,
    )
    sync_p.add_argument("--task-payload", default=None, help=argparse.SUPPRESS)
    sync_p.add_argument("--worker-child", action="store_true", help=argparse.SUPPRESS)
    scan_p = sub.add_parser(
        "scan",
        help="estimate what a source would index — file count, size, depth options — "
        "without indexing anything",
    )
    scan_p.add_argument("--config", "-c", default="pheasant.yaml")
    scan_p.add_argument("--source", "-s", help="source to scan (default: every enabled source)")
    scan_p.add_argument("--depth", type=int, default=None, help="cap directory depth for the scan")
    scan_p.add_argument("--json", action="store_true", help="emit the raw report as JSON")
    # `PHEASANT_CONFIG` is the container's documented way to relocate the
    # config (Dockerfile sets it, docker-entrypoint.sh reads it,
    # troubleshooting.md tells you to check it) — and pheasant's *own*
    # generated MCP client configs put it in the agent's environment
    # (`deployment/host.py`, `mcp_client/vscode.py`). These two servers
    # hardcoded the default instead, so an agent pointed at a non-default
    # config silently got `/config/pheasant.yaml`: the wrong knowledge base,
    # reported as if it were the right one. Explicit `--config` still wins.
    server_config_default = os.environ.get("PHEASANT_CONFIG", "/config/pheasant.yaml")
    serve_p = sub.add_parser("serve")
    serve_p.add_argument("--config", "-c", default=server_config_default)
    serve_p.add_argument(
        "--role",
        choices=("all", "api", "indexer", "graph", "worker", "logger"),
        default=None,
        help="Which jobs this process takes on. Default: server.role, or 'all'.",
    )
    worker_p = sub.add_parser(
        "worker",
        help="Run a stateless preparation worker for a remote coordinator.",
    )
    worker_p.add_argument("--config", "-c", default=server_config_default)
    worker_p.add_argument(
        "--transport",
        choices=("grpc",),
        default="grpc",
        help="HTTP workers are served by `pheasant serve`; this runs the gRPC one.",
    )
    worker_p.add_argument("--host", default="0.0.0.0")  # noqa: S104 - addressed from other pods
    worker_p.add_argument("--port", type=int, default=8766)
    worker_p.add_argument("--max-workers", type=int, default=8)
    mcp_p = sub.add_parser("mcp")
    mcp_p.add_argument("--config", "-c", default=server_config_default)
    mcp_p.add_argument("--transport", choices=("stdio", "streamable-http", "sse"), default="stdio")
    client_p = sub.add_parser("client-config")
    client_sub = client_p.add_subparsers(dest="client")
    vscode_p = client_sub.add_parser("vscode")
    vscode_p.add_argument("--mode", choices=("docker-exec", "docker-run"), default="docker-exec")
    vscode_p.add_argument("--server-name", default="pheasant")
    vscode_p.add_argument("--container-name", default="pheasant")
    vscode_p.add_argument("--image")
    vscode_p.add_argument("--output", "-o")
    for agent_name in ("claude-code", "cursor"):
        agent_p = client_sub.add_parser(
            agent_name, help=f"emit an mcpServers config for {agent_name}"
        )
        agent_p.add_argument(
            "--mode", choices=("local", "docker-exec", "docker-run"), default="local"
        )
        agent_p.add_argument("--server-name", default="pheasant")
        agent_p.add_argument("--config", "-c", default="pheasant.yaml")
        agent_p.add_argument("--container-name", default="pheasant")
        agent_p.add_argument("--image")
        agent_p.add_argument("--output", "-o")
    compose_env_p = sub.add_parser("compose-env")
    compose_env_p.add_argument("config", nargs="?", default="pheasant.yaml")
    compose_env_p.add_argument("--output", "-o")
    repair_p = sub.add_parser("repair")
    repair_p.add_argument("--config", "-c", default="pheasant.example.yaml")
    queue_p = sub.add_parser("queue", help="Inspect the durable index work queue.")
    queue_sub = queue_p.add_subparsers(dest="queue_command")
    queue_status_p = queue_sub.add_parser("status", help="Backlog, in-flight and dead letters.")
    queue_status_p.add_argument("--config", "-c", default="pheasant.yaml")
    queue_drain_p = queue_sub.add_parser("drain", help="Index everything currently queued.")
    queue_drain_p.add_argument("--config", "-c", default="pheasant.yaml")
    queue_drain_p.add_argument(
        "--idle-timeout",
        type=float,
        default=0.0,
        help="Keep waiting this many seconds for new work before exiting (0 = drain and stop).",
    )
    queue_requeue_p = queue_sub.add_parser(
        "requeue-dead", help="Replay dead-lettered tasks after fixing the cause."
    )
    queue_requeue_p.add_argument("--config", "-c", default="pheasant.yaml")
    shard_p = sub.add_parser("shard", help="Plan a multi-region split of this corpus.")
    shard_sub = shard_p.add_subparsers(dest="shard_command", required=True)
    shard_plan_p = shard_sub.add_parser("plan", help="Propose which sources go to which region.")
    shard_plan_p.add_argument("--config", "-c", default="pheasant.yaml")
    shard_plan_p.add_argument("--shards", type=int, help="Split into exactly this many regions.")
    shard_plan_p.add_argument("--json", action="store_true")
    shard_plan_p.add_argument(
        "--emit",
        metavar="DIR",
        help=(
            "Write each region's config, compose file and .env stub into DIR, "
            "so acting on the plan is a reviewed pull request rather than a "
            "weekend of copying. Nothing is applied and no data moves."
        ),
    )
    migrate_p = sub.add_parser(
        "migrate", help="Copy SQLite state into the configured Postgres backend."
    )
    migrate_p.add_argument("--config", "-c", default="pheasant.yaml")
    migrate_p.add_argument(
        "--to", choices=("postgres",), default="postgres", help="Target backend."
    )
    migrate_p.add_argument(
        "--sqlite-path",
        help="Source database. Defaults to the state path in the config.",
    )
    migrate_p.add_argument(
        "--keep-original",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Rename the SQLite file to *.migrated instead of leaving it in place.",
    )
    migrate_p.add_argument("--json", action="store_true")
    # Imported at parser-build time so `--help` can enumerate the tables from
    # the one place they are declared. Safe on every other command too:
    # `pheasant.analytics` pulls in stdlib and the schema module only —
    # DuckDB itself is imported lazily, inside the functions that need it.
    from pheasant.analytics import EXPORTABLE

    readiness_p = sub.add_parser(
        "readiness",
        help="Whether an outside harness can trust this region's answers.",
    )
    readiness_sub = readiness_p.add_subparsers(dest="readiness_command", required=True)
    readiness_contract_p = readiness_sub.add_parser(
        "contract", help="What this build supports, and what it declares unsupported."
    )
    readiness_contract_p.add_argument("--config", "-c", default="pheasant.yaml")
    readiness_contract_p.add_argument("--json", action="store_true")
    readiness_check_p = readiness_sub.add_parser(
        "check",
        help="Probe this region and print the go/no-go verdict per gate set.",
    )
    readiness_check_p.add_argument("--config", "-c", default="pheasant.yaml")
    readiness_check_p.add_argument(
        "--gate-set",
        action="append",
        dest="gate_sets",
        choices=["core", "swarm", "memory", "tuning"],
        help="Evaluate only this gate set. Repeatable; default is all four.",
    )
    readiness_check_p.add_argument("--json", action="store_true")
    readiness_check_p.add_argument(
        "--out",
        help="Write the Markdown report here as well as printing the verdict.",
    )

    eval_p = sub.add_parser(
        "eval", help="Build evaluation sets from what this region was really asked."
    )
    eval_sub = eval_p.add_subparsers(dest="eval_command", required=True)
    eval_boot_p = eval_sub.add_parser(
        "bootstrap",
        help="Turn the interaction ledger into a de-identified evaluation case set.",
    )
    eval_boot_p.add_argument(
        "--config", "-c", default="pheasant.yaml", help="Path to pheasant.yaml"
    )
    eval_boot_p.add_argument(
        "--out",
        default=None,
        help="Where to write it. Default: <exports_path>/eval/cases.json",
    )
    eval_boot_p.add_argument(
        "--limit", type=int, default=500, help="Maximum distinct questions (default: 500)"
    )
    eval_run_p = eval_sub.add_parser(
        "run",
        help="Replay cohorts against baselines and write an evidence-bearing report.",
    )
    eval_run_p.add_argument("--config", "-c", default="pheasant.yaml", help="Path to pheasant.yaml")
    eval_run_p.add_argument(
        "--mode",
        choices=("current_state", "historical"),
        default="current_state",
        help=(
            "current_state: how the region answers those questions now. "
            "historical: what it could have known at --as-of."
        ),
    )
    eval_run_p.add_argument(
        "--as-of",
        default=None,
        help="Instant a historical reconstruction describes (ISO-8601).",
    )
    eval_run_p.add_argument(
        "--force",
        action="store_true",
        help="Run even when evaluation.enabled is false.",
    )
    eval_run_p.add_argument(
        "--json", action="store_true", help="Print the whole report instead of a summary."
    )
    eval_report_p = eval_sub.add_parser("report", help="Print the most recent evaluation report.")
    eval_report_p.add_argument(
        "--config", "-c", default="pheasant.yaml", help="Path to pheasant.yaml"
    )
    eval_report_p.add_argument("--run", default=None, help="A specific run id.")
    eval_report_p.add_argument(
        "--json", action="store_true", help="Print the whole report instead of a summary."
    )
    eval_proof_p = eval_sub.add_parser(
        "proof", help="Record one typed piece of interaction evidence."
    )
    eval_proof_p.add_argument(
        "--config", "-c", default="pheasant.yaml", help="Path to pheasant.yaml"
    )
    eval_proof_p.add_argument("--query", required=True, help="The question that was asked.")
    eval_proof_p.add_argument("--target", required=True, help="Artifact or memory record id.")
    eval_proof_p.add_argument(
        "--event",
        required=True,
        help="Event type from the taxonomy (see `pheasant eval taxonomy`).",
    )
    eval_proof_p.add_argument(
        "--target-type",
        default="artifact",
        choices=("artifact", "memory", "fact", "response", "action", "query"),
    )
    eval_proof_p.add_argument("--principal", default=None)
    eval_proof_p.add_argument("--session", default=None)
    eval_proof_p.add_argument("--position", type=int, default=None)
    eval_tax_p = eval_sub.add_parser(
        "taxonomy", help="Print the evidence taxonomy: every event type and what it licenses."
    )
    # Takes a config like every sibling, and for a reason beyond uniformity:
    # the closing line about exposure and non-selection is a claim about
    # `evaluation.proof`, which a deployment can re-polarize. Printed without
    # reading it, that claim can be false for the very region the command was
    # pointed at -- while `GET /evaluation/taxonomy` reports the real values.
    eval_tax_p.add_argument("--config", "-c", default="pheasant.yaml", help="Path to pheasant.yaml")
    eval_status_p = eval_sub.add_parser(
        "status", help="What an evaluation batch is doing right now."
    )
    eval_status_p.add_argument(
        "--config", "-c", default="pheasant.yaml", help="Path to pheasant.yaml"
    )
    eval_status_p.add_argument("--run", default=None, help="A specific run id.")
    eval_status_p.add_argument(
        "--watch",
        action="store_true",
        help="Poll until the batch reaches a terminal state.",
    )
    eval_status_p.add_argument(
        "--interval", type=float, default=2.0, help="Seconds between polls with --watch."
    )
    eval_trend_p = eval_sub.add_parser("trend", help="One metric's history across snapshots.")
    eval_trend_p.add_argument(
        "--config", "-c", default="pheasant.yaml", help="Path to pheasant.yaml"
    )
    eval_trend_p.add_argument(
        "--metric", default="known_positive_reciprocal_rank", help="Metric id."
    )
    eval_trend_p.add_argument("--cohort", default="anchor", help="Cohort name (default: anchor).")
    eval_trend_p.add_argument("--variant", default="B5", help="Variant id (default: B5).")

    tune_p = sub.add_parser(
        "tune",
        help="Find which retrieval stage is failing, and tune the parameters that reach it.",
    )
    tune_sub = tune_p.add_subparsers(dest="tune_command", required=True)

    def _tune_parser(name: str, help_text: str):
        parser = tune_sub.add_parser(name, help=help_text)
        parser.add_argument("--config", "-c", default="pheasant.yaml", help="Path to pheasant.yaml")
        return parser

    tune_diag_p = _tune_parser(
        "diagnose",
        "Attribute every retrieval miss to the stage that lost it. Changes nothing.",
    )
    tune_diag_p.add_argument(
        "--json", action="store_true", help="Print the whole diagnosis instead of a summary."
    )
    tune_run_p = _tune_parser(
        "run", "Diagnose, search the parameters that reach the blamed stages, and gate a winner."
    )
    tune_run_p.add_argument(
        "--force", action="store_true", help="Run even when tuning.enabled is false."
    )
    tune_run_p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Apply the winning bundle if every gate passes. Off by default: producing a "
            "bundle changes nothing, applying one re-ranks the whole fleet."
        ),
    )
    tune_run_p.add_argument("--json", action="store_true", help="Print the whole report.")
    tune_status_p = _tune_parser("status", "What a tuning batch is doing right now.")
    tune_status_p.add_argument("--experiment", default=None, help="A specific experiment id.")
    tune_status_p.add_argument(
        "--watch", action="store_true", help="Poll until the batch reaches a terminal state."
    )
    tune_status_p.add_argument(
        "--interval", type=float, default=2.0, help="Seconds between polls with --watch."
    )
    tune_report_p = _tune_parser("report", "The most recent tuning report.")
    tune_report_p.add_argument("--experiment", default=None, help="A specific experiment id.")
    tune_report_p.add_argument("--json", action="store_true", help="Print the whole report.")
    _tune_parser("bundles", "Configuration bundles this region has produced, and which is live.")
    tune_show_p = _tune_parser(
        "show", "The parameters this region is ranking with, and where they came from."
    )
    tune_show_p.add_argument(
        "--yaml",
        action="store_true",
        help="Print the equivalent `search.ranking` block, to paste into pheasant.yaml.",
    )
    tune_apply_p = _tune_parser("apply", "Make a bundle the region's live retrieval overlay.")
    tune_apply_p.add_argument("bundle", help="Bundle id (see `pheasant tune bundles`).")
    tune_rollback_p = _tune_parser(
        "rollback", "Stand the active overlay down; the region returns to its configured values."
    )
    tune_rollback_p.add_argument(
        "--to",
        default="base",
        help=(
            "'base' (default: the values in pheasant.yaml) or an earlier bundle id, "
            "which is recorded as a rollback rather than a fresh apply."
        ),
    )
    tune_explain_p = _tune_parser(
        "explain", "What a tuning measure means, and the misreading it invites."
    )
    tune_explain_p.add_argument(
        "term", nargs="?", default=None, help="A metric, stage, gate or parameter name."
    )
    _tune_parser("lineage", "Every retrieval configuration this region has served.")

    export_p = sub.add_parser(
        "export", help="Write Parquet exports of indexed state, and query them."
    )
    export_sub = export_p.add_subparsers(dest="export_command", required=True)
    export_tables_p = export_sub.add_parser(
        "tables", help="List what can be exported, and what each table holds."
    )
    export_tables_p.add_argument(
        "--schema",
        action="store_true",
        help="Print every column, its type and what it joins to.",
    )
    export_tables_p.add_argument(
        "--config",
        "-c",
        help=(
            "With --schema, read the live tables so migration-added columns are "
            "included. Without it the declared schema is printed."
        ),
    )
    export_tables_p.add_argument("--json", action="store_true")
    export_parquet_p = export_sub.add_parser(
        "parquet", help="Export state tables and the knowledge graph as Parquet files."
    )
    export_parquet_p.add_argument("--config", "-c", default="pheasant.yaml")
    export_parquet_p.add_argument(
        "--out", help="Output directory. Defaults to <exports_path>/parquet/<kb_id>."
    )
    export_parquet_p.add_argument(
        "--table",
        action="append",
        dest="tables",
        metavar="NAME",
        help=(
            "Export only this table; repeat for several. "
            f"One of: {', '.join(sorted(EXPORTABLE))}. "
            "Defaults to everything but artifact_terms."
        ),
    )
    export_parquet_p.add_argument(
        "--compression",
        choices=("zstd", "snappy", "gzip", "uncompressed"),
        default="zstd",
        help="Parquet codec. zstd is the smallest and what every reader supports.",
    )
    export_parquet_p.add_argument("--json", action="store_true")
    export_query_p = export_sub.add_parser(
        "query", help="Run SQL over an export directory (one view per Parquet file)."
    )
    export_query_p.add_argument("sql", help="A SQL statement, e.g. 'SELECT * FROM artifacts'.")
    export_query_p.add_argument("--config", "-c", default="pheasant.yaml")
    export_query_p.add_argument(
        "--dir", help="Export directory. Defaults to <exports_path>/parquet/<kb_id>."
    )
    export_query_p.add_argument(
        "--format", choices=("table", "json", "csv"), default="table", help="Output rendering."
    )
    export_query_p.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Cap the result set (0 removes the cap). Applied around your statement.",
    )
    backup_p = sub.add_parser("backup")
    backup_p.add_argument("output")
    backup_p.add_argument("--config", "-c", default="pheasant.example.yaml")
    restore_p = sub.add_parser("restore")
    restore_p.add_argument("input")
    restore_p.add_argument("--config", "-c", default="pheasant.example.yaml")
    restore_p.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.command in {None, "--help"}:
        parser.print_help()
        return 0
    if args.command == "up":
        from pheasant.quickstart import ensure_up_config, state_root
        from pheasant.sync.locks import EngineLeaseError
        from pheasant.targets import TargetError, fetch_target, resolve_targets

        config_path = Path(args.config)
        specs = args.path if isinstance(args.path, list) else [args.path]
        if not specs:
            specs = ["."]
        local_state = state_root(config_path)
        try:
            targets = resolve_targets(
                specs,
                clone_root=local_state / "sources",
                workspace=local_state / "external",
                split=args.split,
                name=args.name,
            )
            for target in targets:
                status = fetch_target(target)
                if status:
                    print(status)
        except TargetError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        created = ensure_up_config(
            targets, config_path, name=args.name, port=args.port, profile=args.profile
        )
        if created:
            print(f"Wrote {config_path}")
            for target in targets:
                print(f"  + {target.name} ({target.type}) <- {target.path}")
        else:
            from pheasant.quickstart import added_sources

            added = added_sources()
            if added:
                print(f"Added to existing {config_path} (your settings are untouched)")
                for target in targets:
                    if target.name in added:
                        print(f"  + {target.name} ({target.type}) <- {target.path}")
            else:
                print(f"Reusing existing {config_path} — every target is already configured")
        engine = _engine(config_path)
        try:
            results = engine.sync_all(args.mode)
        except EngineLeaseError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        finally:
            engine.close()
        for r in results:
            print(
                f"{r.source_id}: indexed={r.indexed_artifacts} "
                f"skipped={r.skipped_artifacts} nodes={r.graph_nodes} edges={r.graph_edges}"
            )
        if args.no_serve:
            print(f"Ready. Start the server with: pheasant start -c {config_path}")
            print(
                "Attach to a coding agent: "
                f"pheasant client-config claude-code -c {config_path} -o .mcp.json"
            )
            return 0
        from pheasant.config.loader import load_config

        cfg = load_config(config_path)
        print(f"API + MCP:   http://{cfg.server.host}:{cfg.server.port}")
        _serve_app(cfg, str(config_path))
        return 0
    if args.command == "setup":
        return _run_setup(args)
    if args.command == "mount":
        return _run_mount(args)
    if args.command == "host":
        from pheasant.deployment.host import host_stack
        from pheasant.targets import TargetError

        try:
            return host_stack(args)
        except TargetError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    if args.command == "start":
        from pheasant.config.loader import load_layered_config, parse_override_pairs

        cfg = load_layered_config(
            Path(args.config),
            args.profile,
            parse_override_pairs(args.overrides),
        )
        _serve_app(cfg, args.config)
        return 0
    if args.command == "validate":
        from pheasant.config.loader import load_config, validate_source_paths

        cfg = load_config(Path(args.config))
        errors = validate_source_paths(cfg, require_exists=not args.no_require_paths)
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        print(f"Config valid: {args.config} ({len(cfg.sources)} sources)")
        return 0
    if args.command == "init":
        from pheasant.config.loader import render_init_config

        output = Path(args.output)
        if output.exists() and not args.force:
            print(f"ERROR: {output} already exists. Use --force to overwrite.")
            return 1
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_init_config(args.profile), encoding="utf-8")
        print(f"Wrote {output}")
        return 0
    if args.command == "config":
        import yaml

        from pheasant.config.loader import (
            effective_config_dict,
            parse_override_pairs,
        )

        if args.config_command != "show":
            config_p.print_help()
            return 1
        payload = effective_config_dict(
            Path(args.config),
            args.profile,
            parse_override_pairs(args.overrides),
        )
        print(yaml.safe_dump(payload, sort_keys=False), end="")
        return 0
    if args.command == "doctor":
        from pheasant.config.loader import (
            load_layered_config,
            parse_override_pairs,
            validate_source_paths,
        )

        cfg = load_layered_config(Path(args.config), args.profile, parse_override_pairs([]))
        errors = validate_source_paths(cfg, require_exists=not args.no_require_paths)
        for path in [
            cfg.pheasant.state_path,
            cfg.pheasant.exports_path,
        ]:
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                errors.append(f"cannot create {path}: {exc}")
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        print(
            f"Doctor ok: profile={args.profile} "
            f"sources={len(cfg.sources)} transports={cfg.server.mcp.transports}"
        )
        return 0
    if args.command == "sync":
        from pheasant.sync.locks import EngineLeaseError

        on_progress = _progress_emitter() if getattr(args, "progress", False) else None
        engine = _engine(Path(args.config), defer_persisted_graph_load=True)
        if args.wait_for_lease is not None:
            engine.lease.wait_timeout_s = max(0.0, args.wait_for_lease)
        try:
            # This process owns the CPU cost of indexing, so it also owns
            # building the graph search index when it is missing (after an
            # upgrade, or a wiped cache). Doing it here keeps that work out of
            # the server, which is the whole point of running sync out of
            # process.
            engine.ensure_node_index()
            if args.task_payload:
                import base64
                import json as _json

                from pheasant.sync.queue import IndexTask

                task_payload = _json.loads(
                    base64.urlsafe_b64decode(args.task_payload.encode("ascii")).decode("utf-8")
                )
                delivery_attempt = int(task_payload.pop("_delivery_attempt", 0) or 0)
                task = IndexTask(
                    id="worker-control-task",
                    source_id=str(args.source or ""),
                    mode=str(args.mode),
                    payload=task_payload,
                    attempts=delivery_attempt,
                )
                results = [engine.apply_index_task(task, on_progress=on_progress)]
            else:
                results = (
                    engine.sync_all(
                        args.mode,
                        max_depth=args.depth,
                        full_scan=args.full_scan,
                        on_progress=on_progress,
                    )
                    if args.all or not args.source
                    else [
                        engine.sync_source(
                            args.source,
                            args.mode,
                            max_depth=args.depth,
                            full_scan=args.full_scan,
                            on_progress=on_progress,
                        )
                    ]
                )
        except EngineLeaseError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        finally:
            engine.close()
        if getattr(args, "json", False):
            # One line, last: the server runs this command as a subprocess and
            # reads the report off stdout.
            import json as _json

            payload = {
                "status": "ok",
                "results": [
                    {
                        "source_id": r.source_id,
                        "indexed_artifacts": r.indexed_artifacts,
                        "skipped_artifacts": r.skipped_artifacts,
                        "graph_nodes": r.graph_nodes,
                        "graph_edges": r.graph_edges,
                        "status": r.status,
                        "details": r.details,
                    }
                    for r in results
                ],
            }
            print(_json.dumps(payload))
            exit_code = 0 if all(r.status != "limit_exceeded" for r in results) else 1
            if args.worker_child:
                # Every store, lease and vector writer was explicitly closed
                # above. Letting CPython recursively destroy a million-node
                # graph after reporting success adds tens of seconds to the
                # queue ack and looks like a save hang. This flag is hidden
                # and only supplied by the supervised subprocess boundary, so
                # an embedded/public ``main([...])`` call is never terminated.
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(exit_code)
            return exit_code
        exit_code = 0
        for r in results:
            if r.status == "limit_exceeded":
                # A refused sync is a failure the caller must see — both in the
                # message and in the exit code, so a scripted sync does not
                # sail past it reporting "0 indexed" as if that were success.
                print(f"ERROR: {r.details.get('error', 'sync limit exceeded')}", file=sys.stderr)
                exit_code = 1
                continue
            print(
                f"{r.source_id}: indexed={r.indexed_artifacts} "
                f"skipped={r.skipped_artifacts} nodes={r.graph_nodes} edges={r.graph_edges}"
            )
        return exit_code
    if args.command == "scan":
        engine = _engine(Path(args.config))
        try:
            names = (
                [args.source]
                if args.source
                else [s.name for s in engine.config.sources if s.enabled]
            )
            reports = [engine.scan_source(name, max_depth=args.depth) for name in names]
        except KeyError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        finally:
            engine.close()
        if args.json:
            print(json.dumps(reports, indent=2))
            return 0
        for report in reports:
            _print_scan(report)
        # Once, after the sources. The evaluation plane is a property of the
        # *region*, not of any one source, and printing it per source would
        # repeat one number as though it were several.
        _print_evaluation_projection(
            next((r for r in reports if r.get("evaluation_projection")), {})
        )
        return 0
    if args.command == "queue":
        from pheasant.sync.queue import (
            DEAD,
            DONE,
            INFLIGHT,
            PENDING,
            LocalQueue,
            drain,
            queue_from_config,
        )

        engine = _engine(Path(args.config))
        queue = None
        try:
            # Built from config, not hardcoded to LocalQueue: with
            # `sync.queue.backend: nats` the tasks are in JetStream, and
            # reaching for the SQLite table reported an empty queue on a
            # backlog of thousands and drained nothing — while `requeue-dead`
            # silently did nothing to the queue that actually held the dead
            # letters. `queue_from_config` returns None when queueing is off.
            queue = queue_from_config(engine.config, engine.state) or LocalQueue(engine.state)
            if args.queue_command == "status":
                depth = queue.depth()
                print(
                    f"queued: {depth[PENDING]}  in-flight: {depth[INFLIGHT]}  "
                    f"done: {depth[DONE]}  dead: {depth[DEAD]}"
                )
                if depth[DEAD] and isinstance(queue, LocalQueue):
                    # Per-task detail is a property of the local table; a
                    # JetStream dead letter is terminated on the broker and
                    # has no row here to read.
                    for row in engine.state.rows(
                        "SELECT source_id, attempts, last_error FROM index_tasks "
                        "WHERE status=? ORDER BY updated_at",
                        (DEAD,),
                    ):
                        print(
                            f"  ! {row['source_id']} failed {row['attempts']}x: {row['last_error']}"
                        )
                    print("\nFix the cause, then: pheasant queue requeue-dead")
                return 0
            if args.queue_command == "requeue-dead":
                requeue = getattr(queue, "requeue_dead", None)
                if requeue is None:
                    print(
                        f"{type(queue).__name__} has no dead-letter replay; "
                        "terminated messages are managed on the broker.",
                        file=sys.stderr,
                    )
                    return 2
                print(f"requeued {requeue()} dead-lettered task(s)")
                return 0
            if args.queue_command == "drain":
                results = drain(
                    queue,
                    lambda task: engine.apply_index_task(task),
                    idle_timeout=float(args.idle_timeout or 0.0),
                )
                for result in results:
                    print(
                        f"{result.source_id}: {result.status} "
                        f"({result.indexed_artifacts} indexed, "
                        f"{result.skipped_artifacts} skipped)"
                    )
                print(f"drained {len(results)} task(s)")
                return 0
        finally:
            if queue is not None:
                try:
                    queue.close()
                except Exception:  # pragma: no cover - shutdown must not raise
                    pass
            engine.close()
        queue_p.print_help()
        return 2
    if args.command == "shard":
        from pheasant.config.loader import load_config
        from pheasant.sharding import SourceSize, plan_shards, render_plan

        cfg = load_config(Path(args.config))
        engine = _engine(Path(args.config))
        sizes = []
        try:
            # UI/API registrations live in the durable source registry and do
            # not have to be written back into the read-only fleet YAML.  Plan
            # from the engine's hydrated view so the command sees the same
            # corpus the scheduler and queue drain actually index.
            for source in engine.enabled_sources():
                if not source.enabled:
                    continue
                # `scan` walks without reading, so planning a split of a corpus
                # you have not indexed yet costs a directory walk, not an index.
                try:
                    report = engine.scan_source(source.name)
                except Exception as exc:  # an unwalkable source is not fatal
                    print(f"WARNING: could not scan {source.name}: {exc}", file=sys.stderr)
                    continue
                if not report.get("scannable"):
                    print(
                        f"WARNING: skipping {source.name}: {report.get('reason')}",
                        file=sys.stderr,
                    )
                    continue
                sizes.append(
                    SourceSize(
                        name=source.name,
                        files=int(report.get("file_count") or 0),
                        bytes_=int(report.get("total_bytes") or 0),
                    )
                )
        finally:
            engine.close()
        plan = plan_shards(
            sizes,
            shards=args.shards,
            max_nodes_per_shard=int(getattr(cfg.graph, "max_nodes", None) or 1_500_000),
        )
        if getattr(args, "emit", None):
            from pheasant.sharding import render_artifacts

            target = Path(args.emit)
            artifacts = render_artifacts(plan, cfg)
            if not artifacts:
                print("Nothing to emit: the plan proposes no regions.", file=sys.stderr)
                return 1
            # Refuse rather than overwrite. These are files an operator edits
            # — a filled-in .env, a hand-tuned mem_limit — and a re-run that
            # silently replaced them would be the second-worst outcome after
            # not having them at all.
            existing = [name for name in artifacts if (target / name).exists()]
            if existing:
                print(
                    f"Refusing to overwrite {len(existing)} existing file(s) in {target}: "
                    f"{', '.join(sorted(existing)[:3])}"
                    f"{'...' if len(existing) > 3 else ''}\n"
                    "Emit into an empty directory and diff, so an edited region config "
                    "is never replaced by a regenerated one.",
                    file=sys.stderr,
                )
                return 1
            for name, content in sorted(artifacts.items()):
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            print(render_plan(plan))
            print(f"Wrote {len(artifacts)} file(s) to {target}/ — review, then apply per region.")
            return 0
        print(json.dumps(plan, indent=2, sort_keys=True) if args.json else render_plan(plan))
        return 0
    if args.command == "migrate":
        from pheasant.config.loader import load_config
        from pheasant.persistence.migrate import MigrationError, migrate_sqlite_to_postgres
        from pheasant.persistence.paths import StatePaths
        from pheasant.persistence.secrets import DsnUnavailable, resolve_dsn

        cfg = load_config(Path(args.config))
        sqlite_path = args.sqlite_path or StatePaths.from_config(cfg).sqlite
        try:
            dsn = resolve_dsn(cfg.storage)
        except DsnUnavailable as exc:
            # The DSN comes from the environment by design, so the most likely
            # mistake is a config that still says `backend: sqlite`.
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        try:
            report = migrate_sqlite_to_postgres(
                sqlite_path,
                dsn,
                pool_size=int(getattr(cfg.storage, "pool_size", 10) or 10),
                keep_original=bool(args.keep_original),
            )
        except MigrationError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"Migrated {report['source']} -> {report['target']}")
            for table, count in sorted(report["tables"].items()):
                print(f"  {table}: {count} row(s)")
            if report["skipped"]:
                print(f"  already populated, left alone: {', '.join(report['skipped'])}")
            print(f"  chunks_fts: rebuilt {report['chunks_fts']} row(s) for this dialect")
            if report.get("original_renamed_to"):
                print(f"  original kept at {report['original_renamed_to']}")
            print("Set storage.backend: postgres in your config and restart.")
        return 0
    if args.command == "repair":
        from pheasant.sync.locks import EngineLeaseError

        engine = _engine(Path(args.config))
        try:
            engine.sync_all("repair")
        except EngineLeaseError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        finally:
            engine.close()
        print("Repair complete")
        return 0
    if args.command == "readiness" and args.readiness_command == "contract":
        from pheasant.config.loader import load_config
        from pheasant.readiness.contract import build_contract

        contract = build_contract(load_config(Path(args.config)))
        if args.json:
            print(json.dumps(contract, indent=2, sort_keys=True, default=str))
            return 0
        _print_readiness_contract(contract)
        return 0
    if args.command == "readiness" and args.readiness_command == "check":
        # `PheasantTools` is the same facade the MCP surface drives, and that is
        # the reason `cli.py` is a composition root at all: a third assembly of
        # searcher, graph and engine is a third thing that can be assembled
        # differently, and a readiness check has to exercise what this region
        # actually serves.
        from pheasant.config.loader import load_config
        from pheasant.mcp_server.tools import PheasantTools
        from pheasant.readiness.runner import render_report

        tools = PheasantTools(load_config(Path(args.config)))
        try:
            report = tools.run_readiness_check(gate_sets=args.gate_sets)
        finally:
            tools.engine.close()
        if args.out:
            Path(args.out).write_text(render_report(report), encoding="utf-8")
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True, default=str))
        else:
            print(render_report(report))
        # A non-zero exit for anything short of "go", `core-only` included: a
        # green CI signal for a run whose swarm gates failed is the shape the
        # evaluation plane's skipped-run incident already cost this codebase
        # once.
        return 0 if report["verdict"] == "go" else 1
    if args.command == "eval" and args.eval_command == "taxonomy":
        from pheasant.evaluation.proof import DEFAULT_TAXONOMY

        print(f"{'event type':<32} {'polarity':<9} {'strength':<11} what it licenses")
        for kind in DEFAULT_TAXONOMY.values():
            print(f"{kind.event_type:<32} {kind.polarity:<9} {kind.strength:<11} {kind.note}")
        print()
        # The taxonomy above is the shipped one; these two are the knobs a
        # deployment can turn, so they are read from the region rather than
        # asserted about it. An unreadable config is not an error here -- the
        # table is still worth printing -- but the line about it says so
        # instead of stating a default the region may not be running.
        proof_settings = None
        try:
            from pheasant.config.loader import load_config

            proof_settings = load_config(Path(args.config)).evaluation.proof
        except Exception:  # noqa: BLE001 - printing a table must not need a region
            pass
        if proof_settings is None:
            print(
                f"Could not read {args.config}, so this is the shipped taxonomy rather than "
                "this region's: pass --config to see whether it re-polarizes anything."
            )
        elif (
            not proof_settings.unknown_is_negative and not proof_settings.non_selection_is_negative
        ):
            print(
                "Exposure is not success and non-selection is not a negative: both stay unknown "
                "unless a deployment deliberately re-polarizes them. This one does not."
            )
        else:
            print("This deployment has re-polarized the two defaults that are normally unknown:")
            print(f"  unknown_is_negative:       {proof_settings.unknown_is_negative}")
            print(f"  non_selection_is_negative: {proof_settings.non_selection_is_negative}")
            print(
                "Treating silence as a negative manufactures negatives at the rate the region "
                "serves results; every metric below inherits that."
            )
        return 0
    if args.command == "eval" and args.eval_command == "proof":
        import pheasant.evaluation as evaluation
        from pheasant.config.loader import load_config

        cfg = load_config(Path(args.config))
        engine = _engine(Path(args.config), load_persisted_graph=False)
        try:
            recorded = evaluation.record_evidence(
                engine.state,
                cfg,
                query=args.query,
                target_id=args.target,
                target_type=args.target_type,
                event_type=args.event,
                principal=args.principal,
                session_id=args.session,
                position=args.position,
            )
        except ValueError as exc:
            print(str(exc))
            return 2
        finally:
            engine.close()
        print(
            f"Recorded {args.event} ({recorded['polarity']}, {recorded['strength']}) "
            f"weight {recorded['weight']} as {recorded['proof_id']} for {recorded['query_id']}."
        )
        print(f"Multipliers: {recorded['multipliers']}")
        return 0
    if args.command == "tune":
        return _tune_command(args)
    if args.command == "eval" and args.eval_command == "run":
        import pheasant.evaluation as evaluation
        from pheasant.config.loader import load_config

        cfg = load_config(Path(args.config))
        # The graph *is* loaded here: replay goes through the real hybrid path,
        # and a graph-less run silently measures a two-arm region.
        engine = _engine(Path(args.config))
        try:
            outcome = evaluation.run(
                engine,
                mode=args.mode,
                effective_as_of=args.as_of,
                force=bool(args.force),
                on_progress=lambda phase, detail: print(
                    f"  {phase}{(': ' + detail) if detail else ''}"
                ),
            )
        finally:
            engine.close()
        if outcome.status == "skipped":
            print(f"Skipped: {outcome.skipped_reason}")
            return 0
        if args.json:
            print(json.dumps(outcome.report, indent=2, sort_keys=True, default=str))
            return 0 if outcome.gates_passed else 1
        _print_evaluation_summary(outcome.report, kb=cfg.knowledge_base_id)
        return 0 if outcome.gates_passed else 1
    if args.command == "eval" and args.eval_command == "report":
        import pheasant.evaluation as evaluation
        from pheasant.config.loader import load_config
        from pheasant.evaluation import store as evaluation_store

        cfg = load_config(Path(args.config))
        engine = _engine(Path(args.config), load_persisted_graph=False)
        try:
            report = (
                evaluation_store.load_report(engine.state, args.run)
                if args.run
                else evaluation.latest_report(engine.state, cfg.knowledge_base_id)
            )
        finally:
            engine.close()
        if report is None:
            print("No evaluation run has completed yet. Run `pheasant eval run`.")
            return 1
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True, default=str))
            return 0
        _print_evaluation_summary(report, kb=cfg.knowledge_base_id)
        return 0
    if args.command == "eval" and args.eval_command == "status":
        import time as _time

        import pheasant.evaluation as evaluation
        from pheasant.config.loader import load_config
        from pheasant.evaluation.store import TERMINAL_RUN_STATUSES

        cfg = load_config(Path(args.config))
        # No graph: this reads one row. Materializing a graph to print a
        # progress line would cost the whole corpus's memory for nothing.
        engine = _engine(Path(args.config), load_persisted_graph=False)
        terminal = (*TERMINAL_RUN_STATUSES, "none", "unknown")
        try:
            while True:
                payload = evaluation.progress(engine.state, cfg.knowledge_base_id, args.run)
                _print_evaluation_status(payload)
                if not args.watch or payload.get("status") in terminal:
                    break
                _time.sleep(max(0.5, float(args.interval)))
        finally:
            engine.close()
        return 0
    if args.command == "eval" and args.eval_command == "trend":
        import pheasant.evaluation as evaluation
        from pheasant.config.loader import load_config

        cfg = load_config(Path(args.config))
        engine = _engine(Path(args.config), load_persisted_graph=False)
        try:
            points = evaluation.trend(
                engine.state,
                cfg.knowledge_base_id,
                args.metric,
                cohort_name=args.cohort,
                variant_id=args.variant,
            )
        finally:
            engine.close()
        if not points:
            print(f"No trend points for {args.metric} on cohort {args.cohort}.")
            return 1
        print(f"{args.metric} — cohort {args.cohort}, variant {args.variant}")
        for point in points:
            value = "—" if point["value"] is None else f"{point['value']:.4f}"
            print(
                f"  {point['started_at']}  {value:>8}  "
                f"({point['numerator']}/{point['denominator']}, {point['status']})"
            )
        return 0
    if args.command == "eval":
        from pheasant.config.loader import load_config
        from pheasant.evalset import bootstrap

        cfg = load_config(Path(args.config))
        # No graph: this reads the ledger and writes a JSON file. Materializing
        # a graph for it would cost the whole corpus's memory for nothing.
        engine = _engine(Path(args.config), load_persisted_graph=False)
        try:
            target = (
                Path(args.out)
                if args.out
                else Path(cfg.pheasant.exports_path) / "eval" / "cases.json"
            )
            report = bootstrap(engine.state, target, limit=args.limit)
        finally:
            engine.close()
        print(
            f"Wrote {report['cases']} case(s) to {report['path']} "
            f"({report['answered']} with a promoted answer, "
            f"{report['unanswered']} that found nothing)."
        )
        print(
            "Principals and sessions are per-export pseudonyms; two exports "
            "cannot be joined to re-identify anyone."
        )
        return 0
    if args.command == "export":
        from pheasant.analytics import (
            EXPORTABLE,
            GRAPH_TABLES,
            AnalyticsUnavailable,
            QueryError,
            StateUnavailable,
            export_dir_for,
            export_parquet,
            export_schema,
            format_rows,
            open_state,
            query,
            render_schema,
            resolve_tables,
        )

        if args.export_command == "tables":
            if not args.schema:
                if args.json:
                    print(json.dumps(EXPORTABLE, indent=2, sort_keys=True))
                    return 0
                width = max(len(name) for name in EXPORTABLE)
                for name in sorted(EXPORTABLE):
                    print(f"{name.ljust(width)}  {EXPORTABLE[name]}")
                return 0
            # The live tables when a config points at real state, so
            # migration-added columns show up; the declared schema otherwise.
            state = None
            if args.config:
                from pheasant.config.loader import load_config
                from pheasant.persistence.paths import StatePaths

                cfg = load_config(Path(args.config))
                paths = StatePaths.from_config(cfg)
                try:
                    state = open_state(cfg, paths.sqlite)
                except StateUnavailable as exc:
                    # Printing the declared schema is still useful — the point
                    # of --schema is usually "what will I get", asked before
                    # there is any state to read.
                    print(f"WARNING: {exc} Showing the declared schema.", file=sys.stderr)
            try:
                report = export_schema(state)
            finally:
                if state is not None:
                    state.close()
            print(json.dumps(report, indent=2) if args.json else render_schema(report))
            return 0

        from pheasant.config.loader import load_config
        from pheasant.persistence.paths import StatePaths

        cfg = load_config(Path(args.config))
        paths = StatePaths.from_config(cfg)
        kb_id = cfg.knowledge_base_id

        if args.export_command == "parquet":
            from pheasant.persistence.graph_store import GraphStore

            try:
                selected = resolve_tables(args.tables)
            except ValueError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            # The export reads state; it never creates it. `open_state` turns
            # every way that can fail — missing file, absent DSN, unreachable
            # or never-synced database — into one actionable line instead of a
            # traceback out of sqlite3 or psycopg.
            try:
                state = open_state(cfg, paths.sqlite)
            except StateUnavailable as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            out = Path(args.out) if args.out else export_dir_for(paths.exports, kb_id)
            try:
                # Only pay for loading the graph when a graph table was asked
                # for. Inside the try so a failure here still returns the
                # state store's connection — on Postgres that is a pooled
                # server-side process, not just a file handle.
                graph = (
                    GraphStore(paths.graphs).load(kb_id)
                    if any(name in GRAPH_TABLES for name in selected)
                    else None
                )
                if graph is not None and not graph.number_of_nodes():
                    # An empty graph exports as an empty file, which reads as
                    # "this corpus has no structure" rather than "the graph was
                    # not where I looked". Say which it is.
                    print(
                        f"WARNING: no graph found under {paths.graphs / kb_id}; "
                        "graph_nodes/graph_edges will be empty.",
                        file=sys.stderr,
                    )
                report = export_parquet(
                    state,
                    out_dir=out,
                    kb_id=kb_id,
                    graph=graph,
                    tables=selected,
                    compression=args.compression,
                    backend=state.dialect.name,
                )
            except AnalyticsUnavailable as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            finally:
                state.close()
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(f"Exported {kb_id} -> {report['directory']}")
                for entry in report["tables"]:
                    print(f"  {entry['file']}: {entry['rows']} row(s), {entry['bytes']} bytes")
                print("  export.json: manifest")
                print('Query it with: pheasant export query "SELECT * FROM artifacts"')
            return 0

        if args.export_command == "query":
            directory = Path(args.dir) if args.dir else export_dir_for(paths.exports, kb_id)
            try:
                columns, rows = query(directory, args.sql, limit=args.limit or None)
            except AnalyticsUnavailable as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            except FileNotFoundError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            except QueryError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            print(format_rows(columns, rows, args.format))
            return 0
    if args.command == "backup":
        from pheasant.config.loader import load_config
        from pheasant.persistence.backup import create_backup
        from pheasant.persistence.paths import StatePaths

        cfg = load_config(Path(args.config))
        paths = StatePaths.from_config(cfg)
        out = create_backup(paths.state, Path(args.output), sqlite_path=paths.sqlite)
        size = out.stat().st_size
        print(f"Backup written: {out} ({size} bytes)")
        return 0
    if args.command == "restore":
        from pheasant.config.loader import load_config
        from pheasant.persistence.backup import restore_backup
        from pheasant.persistence.paths import StatePaths

        cfg = load_config(Path(args.config))
        paths = StatePaths.from_config(cfg)
        try:
            target = restore_backup(Path(args.input), paths.state, force=args.force)
        except FileExistsError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        except (FileNotFoundError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"Restored state into: {target}")
        return 0
    if args.command == "serve":
        from pheasant.config.loader import load_config
        from pheasant.deployment.roles import RoleConfigurationError

        cfg = load_config(Path(args.config))
        # `serve` is the container entrypoint, where the UI is a separate
        # sidecar workload — an npm hint in the image's logs would be noise.
        try:
            _serve_app(cfg, args.config, report_ui=False, role=args.role)
        except RoleConfigurationError as exc:
            print(str(exc))
            return 1
        return 0
    if args.command == "worker":
        import logging

        from pheasant.config.loader import load_config
        from pheasant.deployment.roles import POLICIES, Role, RoleConfigurationError
        from pheasant.deployment.roles import validate_role as validate_worker_role
        from pheasant.sync.grpc_worker import GrpcUnavailable
        from pheasant.sync.grpc_worker import serve as serve_grpc_worker
        from pheasant.version import __version__

        cfg = load_config(Path(args.config))
        level = getattr(logging, str(cfg.pheasant.log_level).upper(), logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        # The same allow-list `serve --role worker` runs. This command is the
        # one Compose actually uses for the tier, so it was the one path where
        # a worker could hold the database URL and never be told.
        try:
            # `serves_http=False`: this command binds a gRPC port and never
            # starts the HTTP app, so there is no knowledge-base API here to
            # demand a token for. What it may *hold* is checked either way.
            validate_worker_role(POLICIES[Role.WORKER], cfg, serves_http=False)
        except RoleConfigurationError as exc:
            print(f"Refusing to start: {exc}")
            return 1
        if not cfg.sync.concurrency.remote_worker_enabled:
            print(
                "Refusing to start: sync.concurrency.remote_worker_enabled is false.\n"
                "A worker accepts parse work from a coordinator, so it is opt-in.",
            )
            return 1
        try:
            server = serve_grpc_worker(
                cfg,
                host=args.host,
                port=args.port,
                max_workers=args.max_workers,
                version=__version__,
            )
        except GrpcUnavailable as exc:
            print(str(exc))
            return 1
        print(f"pheasant preparation worker (grpc) on {args.host}:{args.port}")
        try:
            server.wait_for_termination()
        except KeyboardInterrupt:
            server.stop(grace=5).wait()
        return 0
    if args.command == "mcp":
        from pheasant.config.loader import load_config
        from pheasant.mcp_server.server import run_mcp_server

        cfg = load_config(Path(args.config))
        run_mcp_server(cfg, args.transport)
        return 0
    if args.command == "client-config":
        from pheasant.mcp_client.vscode import (
            docker_exec_stdio_config,
            docker_run_stdio_config,
            render_vscode_mcp_json,
        )

        if args.client in ("claude-code", "cursor"):
            from pheasant.mcp_client.agents import (
                AGENT_CONFIG_FILES,
                agent_mcp_config,
                render_agent_mcp_json,
            )

            payload = agent_mcp_config(
                args.mode,
                server_name=args.server_name,
                config_path=args.config,
                container_name=args.container_name,
                image=args.image,
            )
            rendered = render_agent_mcp_json(payload)
            if args.output:
                output_path = Path(args.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(rendered, encoding="utf-8")
            else:
                print(rendered, end="")
                print(
                    f"# save as {AGENT_CONFIG_FILES[args.client]} in your project root",
                    file=sys.stderr,
                )
            return 0
        if args.client != "vscode":
            client_p.print_help()
            return 1
        payload = (
            docker_exec_stdio_config(args.server_name, args.container_name)
            if args.mode == "docker-exec"
            else docker_run_stdio_config(args.server_name, args.image)
        )
        rendered = render_vscode_mcp_json(payload)
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 0
    if args.command == "compose-env":
        from pheasant.deployment.compose_env import load_compose_environment, render_env_file

        rendered = render_env_file(load_compose_environment(args.config))
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 0
    return 1


def app() -> None:
    raise SystemExit(main())


def _print_readiness_contract(contract: dict) -> None:
    """The contract as a table, failures and refusals first.

    A reader running this is deciding whether to start an experiment, so what
    they need at the top is what this region *cannot* do — the supported rows
    are the ones they can skip.
    """

    print(f"pheasant {contract['server_version']} — {contract['knowledge_base']}")
    print(f"contract {contract['digest']}")
    enabled = "on" if contract["readiness_enabled"] else "off (readiness.enabled)"
    print(f"readiness: {enabled}")
    print()
    unsupported = [row for row in contract["capabilities"] if row["status"] == "unsupported"]
    if unsupported:
        print("Declared unsupported:")
        for row in unsupported:
            print(f"  {row['logical']} ({row['gap']})")
            print(f"    {row.get('detail') or row['summary']}")
        print()
    print(f"{'capability':<32} {'gap':<12} {'gate':<7} status")
    for row in contract["capabilities"]:
        if row["status"] == "unsupported":
            continue
        print(f"  {row['logical']:<30} {row['gap']:<12} {row['gate']:<7} {row['status']}")
    print()
    print("Refusal codes (an MCP client maps a refusal's text onto these):")
    for row in contract["refusal_codes"]:
        retry = "retryable" if row["retryable"] else "permanent"
        print(f"  {row['code']:<26} {row['status']}  {retry}")
