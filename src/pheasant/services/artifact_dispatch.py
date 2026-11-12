"""Durable artifact dispatch; no request waits for the writing tier."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any

from pheasant.services import ServiceContext
from pheasant.services.artifact_files import source_files
from pheasant.services.artifact_scope import ARTIFACTS_SOURCE

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _pacing(settings: Any) -> dict[str, Any]:
    return {
        "min_interval_seconds": int(settings.index_interval_seconds or 0) or None,
        "yield_to_content": bool(settings.yield_to_content),
        # A boot is not a reason to index generated work ahead of content.
        "on_startup": False,
    }


def _deferred(context: ServiceContext) -> bool:
    return context.indexing is not None and not context.indexing.local


def _enqueue(context: ServiceContext, record: dict, operation: str) -> None:
    from pheasant.persistence import artifact_outbox
    from pheasant.services import artifact_settings

    settings = artifact_settings.current(context)
    artifact_outbox.enqueue(
        context.state,
        context.config.knowledge_base_id,
        record["payload"]["folder"],
        operation,
        {
            "artifact_id": record["id"],
            "files": list(source_files(record)),
            "pacing": _pacing(settings),
            "index": settings.index_on_save,
        },
    )


def _defer_removal(
    context: ServiceContext,
    folder: str,
    names: list[str],
    actor: str | None,
    *,
    artifact_id: str | None = None,
) -> None:
    from pheasant.persistence import artifact_outbox

    with context.state.conn:
        artifact_outbox.enqueue(
            context.state, context.config.knowledge_base_id, folder, "remove", {"names": names}
        )
        context.state.conn.executemany(
            "INSERT INTO archived_documents("
            "source_id,relative_path,artifact_id,kind,archived_at,archived_by) "
            "VALUES(?,?,NULL,?,?,?) ON CONFLICT(source_id,relative_path) "
            "DO UPDATE SET kind=excluded.kind",
            [(ARTIFACTS_SOURCE, f"{folder}/{name}", "removed", _now(), actor) for name in names],
        )
        if artifact_id:
            context.state.conn.execute(
                "DELETE FROM saved_artifacts WHERE id=? AND kb_id=?",
                (artifact_id, context.config.knowledge_base_id),
            )


_wake_lock = threading.Lock()
_waking: set[int] = set()


def _wake_projection(context: ServiceContext) -> None:
    """Best-effort notification, outside the request; the database owns intent."""
    import threading

    key = id(context.state)
    with _wake_lock:
        if key in _waking:
            return
        _waking.add(key)

    def wake() -> None:
        try:
            # This GET wakes the writing tier on traffic-scaled deployments;
            # it does not need a graph or land any bytes before acknowledgment.
            context.landing_zone().directory(ARTIFACTS_SOURCE)
            _notify_projection(context)
        except Exception:
            logger.warning(
                "artifact projection notification failed; durable intent remains", exc_info=True
            )
        finally:
            with _wake_lock:
                _waking.discard(key)

    threading.Thread(target=wake, name="pheasant-artifact-wake", daemon=True).start()


def _notify_projection(context: ServiceContext) -> None:
    """Interrupt an idle broker claim without scheduling a full source sync."""
    import uuid

    from pheasant.sync.queue import IndexTask, queue_from_config

    queue = queue_from_config(context.config, context.state)
    if queue is None:
        return
    try:
        queue.publish(
            IndexTask(
                id=f"artifact-projection-{uuid.uuid4().hex}",
                source_id=ARTIFACTS_SOURCE,
                payload={"operation": "project_artifacts"},
            )
        )
    finally:
        queue.close()
