"""Graph history: the interval-gated snapshot a sync leaves behind.

Synapse 21.6A: compressed timestamped graph snapshots beside the published
graph, throttled by ``storage.graph_snapshot_interval_seconds`` and bounded by
``max_state_size_gb``. Additive history, never load-bearing for a sync, so
every step here is fail-soft.

Out of ``engine.py`` because it is a self-contained pass over the engine's
state, the same shape as :mod:`pheasant.sync.vanished` and
:mod:`pheasant.sync.archived`.

**Off the sync's path where a server runs the sync.** A sync process on the row
backend holds no graph (`graph.store_backed`), so its snapshot is read back
from the rows: 18-34 s on a 123k-node region, which the first changed sync
of every interval used to pay before reporting. History is not what that sync
was for, so such a process defers it and its supervisor takes it in a
process of its own (:func:`snapshot_published`, run as
``python -m pheasant.sync.graph_history`` by ``WorkerBackedEngine``) -- never
in the server, whose CPU answers queries. The supervisor says so to the child
it starts (:data:`SNAPSHOT_BY_SUPERVISOR`); a sync run in-process, or a
``pheasant sync`` somebody ran by hand, still snapshots inline, because nobody
else would.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from pheasant.ingestion.pipeline import utc_now
from pheasant.persistence.graph_store import SNAPSHOT_PATTERN

logger = logging.getLogger(__name__)

#: Set by ``WorkerBackedEngine`` on the sync child it starts: "I will take the
#: snapshot once you report." Honoured only by a store-backed working set, the
#: one whose snapshot has to read the rows back.
SNAPSHOT_BY_SUPERVISOR = "PHEASANT_SNAPSHOT_BY_SUPERVISOR"


def _deferred(engine: Any) -> bool:
    explicit = getattr(engine, "defer_snapshots", None)
    if explicit is not None:
        return bool(explicit)
    return bool(getattr(engine, "_partial_graph", False)) and (
        os.environ.get(SNAPSHOT_BY_SUPERVISOR) == "1"
    )


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
    if not storage.graph_snapshots or _deferred(engine):
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
                if engine._loads_persisted_graph
                and engine._graph_loaded
                and not engine._partial_graph
                else None
            )
            engine.graph_store.snapshot_current(kb_id, now, resident=resident)
        max_bytes = int(float(storage.max_state_size_gb) * 1024**3)
        engine.graph_store.enforce_retention(kb_id, max_bytes)
    except Exception as exc:  # noqa: BLE001 - snapshots must not fail sync
        logger.warning("Graph snapshot/retention failed for %s: %s", kb_id, exc)


def snapshot_published(engine: Any) -> Path | None:
    """Take the interval's snapshot of the published generation, if one is due.

    Runs beside syncs rather than inside one, so a commit can land while the
    rows are being read -- and a snapshot spanning two generations would be a
    graph that never existed. The generation id is content-addressed, so it
    is read before and after: if it moved, the file is discarded and the next
    sync's supervisor tries again. Returns the snapshot written, or ``None``.
    """

    storage = engine.config.storage
    if not storage.graph_snapshots:
        return None
    kb_id = engine.config.knowledge_base_id
    now = utc_now()
    if not snapshot_due(engine.graph_store, kb_id, now, storage.graph_snapshot_interval_seconds):
        return None
    before = engine.graph_store.published_generation(kb_id)
    path = engine.graph_store.snapshot_current(kb_id, now)
    after = engine.graph_store.published_generation(kb_id)
    if (before or {}).get("generation_id") != (after or {}).get("generation_id"):
        path.unlink(missing_ok=True)
        logger.info("Graph snapshot for %s discarded: a commit landed while it was read", kb_id)
        return None
    max_bytes = int(float(storage.max_state_size_gb) * 1024**3)
    engine.graph_store.enforce_retention(kb_id, max_bytes)
    return path


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


def main(argv: list[str] | None = None) -> int:
    """``python -m pheasant.sync.graph_history --config F``: the snapshot job.

    Reads only and takes no lease, so it never holds up a sync; prints one
    JSON line, the way a sync child reports.
    """

    parser = argparse.ArgumentParser(prog="python -m pheasant.sync.graph_history")
    parser.add_argument("--config", "-c", required=True)
    args = parser.parse_args(argv)

    from pheasant.config.loader import load_config
    from pheasant.persistence.paths import StatePaths
    from pheasant.persistence.state_store import StateStore
    from pheasant.sync.engine import SyncEngine

    config = load_config(Path(args.config))
    paths = StatePaths.from_config(config)
    engine = SyncEngine(
        config,
        paths,
        StateStore.from_config(config, paths.sqlite),
        defer_persisted_graph_load=True,
        initialize_indexing_components=False,
    )
    try:
        written = snapshot_published(engine)
    finally:
        engine.close()
    print(json.dumps({"status": "ok", "snapshot": str(written) if written else None}))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
