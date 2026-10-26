"""Graph history: the interval-gated snapshot a sync leaves behind.

Synapse 21.6A: compressed timestamped graph snapshots beside the published
graph, throttled by ``storage.graph_snapshot_interval_seconds`` and bounded by
``max_state_size_gb``. Additive history, never load-bearing for a sync, so
every step here is fail-soft.

Out of ``engine.py`` because it is a self-contained pass over the engine's
state, the same shape as :mod:`pheasant.sync.vanished` and
:mod:`pheasant.sync.archived`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pheasant.ingestion.pipeline import utc_now
from pheasant.persistence.graph_store import SNAPSHOT_PATTERN

logger = logging.getLogger(__name__)


def restore_iso_ts(filename_ts: str) -> str:
    """Restore an ISO-8601 timestamp from its filesystem-safe snapshot form.

    ``write_snapshot`` builds filenames as ``utc_ts.replace(":", "-")`` over a
    ``YYYY-MM-DDTHH:MM:SSZ`` timestamp. The date hyphens must stay; only the
    time portion's ``-`` separators become ``:`` again. The trailing ``Z`` is
    dropped so ``datetime.fromisoformat`` parses it on Python 3.11.
    """

    ts = filename_ts.rstrip("Z")
    if "T" in ts:
        date_part, _, time_part = ts.partition("T")
        return f"{date_part}T{time_part.replace('-', ':')}"
    return ts


def snapshot_after_sync(engine: Any) -> None:
    """Write a compressed graph snapshot (interval-throttled) + prune.

    Gated by ``storage.graph_snapshots`` (default on). At most one snapshot
    per ``graph_snapshot_interval_seconds``: if the newest existing snapshot
    is younger than the interval, no new snapshot is written. Retention then
    evicts oldest snapshots beyond ``max_state_size_gb``. Every step is
    fail-soft — the snapshot is additive history, never load-bearing for a
    sync.
    """

    storage = engine.config.storage
    if not storage.graph_snapshots:
        return
    kb_id = engine.config.knowledge_base_id
    try:
        now = utc_now()
        if snapshot_due(engine.graph_store, kb_id, now, storage.graph_snapshot_interval_seconds):
            # Only a working set read from the store can stand in for it; an
            # engine that never loads the persisted graph holds a graph that
            # was never the published one.
            resident = (
                engine.graph_builder.graph
                if engine._loads_persisted_graph and engine._graph_loaded
                else None
            )
            engine.graph_store.snapshot_current(kb_id, now, resident=resident)
        max_bytes = int(float(storage.max_state_size_gb) * 1024**3)
        engine.graph_store.enforce_retention(kb_id, max_bytes)
    except Exception as exc:  # noqa: BLE001 - snapshots must not fail sync
        logger.warning("Graph snapshot/retention failed for %s: %s", kb_id, exc)


def snapshot_due(graph_store: Any, kb_id: str, now: str, interval_seconds: int) -> bool:
    """True when no snapshot exists yet or the newest is older than interval."""

    if interval_seconds <= 0:
        return True
    snapshots = graph_store.list_snapshots(kb_id)
    if not snapshots:
        return True
    match = SNAPSHOT_PATTERN.match(snapshots[-1].name)
    if not match:
        return True
    # Filename ts is the ISO-8601 form with ':' -> '-'; restore the time
    # separators (positions of the two ':' in HH:MM:SS) for parsing.
    iso = restore_iso_ts(match.group("ts"))
    try:
        last = datetime.fromisoformat(iso)
        current = datetime.fromisoformat(now.rstrip("Z"))
    except ValueError:
        return True
    return (current - last).total_seconds() >= interval_seconds
