"""Publish MCP mutations to the fleet's durable writer queue."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pheasant.registry.source_registry import SourceRegistry


def publish(
    config: Any,
    state: Any,
    source_name: str,
    mode: str,
    max_depth: int | None = None,
    full_scan: bool = False,
    operation: str | None = None,
) -> dict:
    from pheasant.sync.queue import IndexTask, queue_from_config

    payload = {
        "max_depth": max_depth,
        "full_scan": bool(full_scan),
        "source_config": SourceRegistry(config, state).config_digest(source_name),
    }
    if operation:
        payload["operation"] = operation
    digest = hashlib.sha256(
        (
            f"{config.knowledge_base_id}\0{source_name}\0{mode}\0"
            + json.dumps(payload, sort_keys=True, separators=(",", ":"))
        ).encode()
    ).hexdigest()[:24]
    task = IndexTask(
        id=f"idx-{digest}",
        source_id=source_name,
        mode=mode,
        payload=payload,
        max_attempts=max(1, int(config.sync.queue.max_attempts or 1)),
    )
    queue = queue_from_config(config, state)
    if queue is None:
        raise RuntimeError("remote graph fleet needs an enabled sync queue")
    try:
        queue.publish(task)
    finally:
        queue.close()
    return {"source_id": source_name, "status": "queued", "task_id": task.id}
