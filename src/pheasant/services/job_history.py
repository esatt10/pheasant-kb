"""Retention and deletion of operational runs; immutable outputs are untouched."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pheasant.security.authorization import require_permission
from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest


def delete_evaluation(context: ServiceContext, run_id: str) -> dict:
    require_permission("evaluation.manage")
    state = context.state
    with state.conn:
        rows = state.conn.execute(
            "DELETE FROM evaluation_runs WHERE run_id=? AND kb_id=? "
            "AND status IN ('completed','truncated','invalid','failed','interrupted','cancelled') "
            "RETURNING run_id",
            (run_id, context.config.knowledge_base_id),
        ).fetchall()
        if not rows:
            raise InvalidRequest("the evaluation run is unknown or still active; cancel it first")
        counts = {}
        for table in ("evaluation_replays", "evaluation_metrics"):
            counts[table] = len(
                state.conn.execute(
                    f"DELETE FROM {table} WHERE run_id=? RETURNING id", (run_id,)
                ).fetchall()
            )
    return {"deleted": run_id, "removed": counts, "published_outputs_unchanged": True}


def delete_tuning(context: ServiceContext, experiment_id: str) -> dict:
    require_permission("tuning.manage")
    from pheasant.tuning.store import prune_experiment

    removed = prune_experiment(
        context.state,
        context.config.knowledge_base_id,
        experiment_id,
        exports_path=context.config.pheasant.exports_path,
    )
    if not removed["experiments"]:
        raise InvalidRequest("the tuning experiment is unknown or still active; cancel it first")
    return {"deleted": experiment_id, "removed": removed, "published_outputs_unchanged": True}


def retain(context: ServiceContext, *, now: datetime | None = None) -> dict[str, Any]:
    from pheasant.persistence.durable_jobs import DurableJobStore
    from pheasant.services.artifact_settings import current

    days = current(context).job_retention_days
    if not days:
        return {"enabled": False, "pruned": 0}
    instant = now or datetime.now(UTC)
    cutoff = (instant - timedelta(days=days)).isoformat()
    count = DurableJobStore(context.state, context.config.knowledge_base_id, days).prune(
        now=instant
    )
    for table, key, operation in (
        ("evaluation_runs", "run_id", delete_evaluation),
        ("tuning_experiments", "experiment_id", delete_tuning),
    ):
        rows = context.state.rows(
            f"SELECT {key} FROM {table} WHERE kb_id=? AND finished_at<? "
            "AND status IN ('completed','truncated','invalid','failed','interrupted','cancelled') "
            "LIMIT 50",
            (context.config.knowledge_base_id, cutoff),
        )
        for row in rows:
            operation(context, row[key])
            count += 1
    return {"enabled": True, "pruned": count, "cutoff": cutoff}
