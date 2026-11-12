"""Ordered mutation operations handled by the persistent writer."""

from __future__ import annotations

from typing import Any


def apply(engine: Any, task: Any, on_progress: Any = None) -> Any:
    from pheasant.sync import archived
    from pheasant.sync.artifact_projection import acknowledge
    from pheasant.sync.artifact_projection import apply as project
    from pheasant.sync.engine import SyncResult

    operation = task.payload["operation"]
    if operation == "prune_documents":
        removed = archived.apply(engine, str(task.source_id))
        nodes, edges = engine._graph_counts()
        return SyncResult(str(task.source_id), removed, 0, nodes, edges, "removed")
    completed, index, tasks = project(engine)
    if index:
        result = engine.sync_source("artifacts", "incremental", on_progress=on_progress)
        if result.status in {"healthy", "degraded", "unchanged"}:
            acknowledge(engine, tasks)
        return result
    acknowledge(engine, tasks)
    nodes, edges = engine._graph_counts()
    return SyncResult("artifacts", 0, 0, nodes, edges, "healthy", {"projected": completed})


def drain_pending(worker: Any, engine: Any, cfg: Any, stop: Any, log: Any) -> None:
    from pheasant.job_snapshots import FAILED_SYNC_STATUSES
    from pheasant.persistence import artifact_outbox
    from pheasant.sync.queue import IndexTask

    if artifact_outbox.pending(engine.state, cfg.knowledge_base_id, limit=1):
        result = worker.apply_index_task(
            IndexTask(
                id="artifact-projection",
                source_id="artifacts",
                mode="incremental",
                payload={"operation": "project_artifacts"},
            )
        )
        if result.status in FAILED_SYNC_STATUSES:
            log.warning("artifact projection failed: %s", result.details)
            stop.wait(1)


def pending_projection_depth(cfg: Any, state: Any) -> int:
    from pheasant.persistence import artifact_outbox

    return len(artifact_outbox.pending(state, cfg.knowledge_base_id, limit=1))


def backlog_depth(cfg: Any, state: Any) -> int:
    """Durable intents outrank opportunistic source scans after a cold boot."""
    import logging

    from pheasant.sync.queue import QueueUnavailable, queue_from_config

    queue = queue_from_config(cfg, state)
    try:
        depth = queue.depth() if queue is not None else {}
    except QueueUnavailable as exc:
        logging.getLogger("pheasant.cli").warning(
            "Durable queue depth unavailable (%s); not deferring startup reconciliation", exc
        )
        depth = {}
    finally:
        if queue is not None:
            queue.close()
    return (
        int(depth.get("pending", 0))
        + int(depth.get("inflight", 0))
        + pending_projection_depth(cfg, state)
    )
