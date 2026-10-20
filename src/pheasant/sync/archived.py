"""Documents held out of the index on purpose: skipped by every sync, and removed.

`persistence/archive.py` holds the rows; this module is what a sync does with
them, kept beside `vanished.py` because it is the same step for a different
reason. A vanished file is one the listing no longer has. An archived file is
one the listing still has and the region was told to leave out, so the rule has
two halves:

* **Never read it.** :func:`admit` drops held paths from the listing before
  any executor is handed an item, exactly where the denylist would refuse one,
  so no executor can index an archived file and none needs its own check. The
  connector's checkpoint is built from the admitted listing too, which is what
  makes a later reinstatement a real read: a connector that remembers "this
  file was unchanged" (Drive's modified time, a web page's validators) has no
  memory of a file it was never handed.
* **Take out what is held.** :func:`prune` removes an archived document's
  state rows, graph nodes and manifest entry, and its vectors go in the
  finalize step that prunes every vector whose chunk row is gone — the same
  removal `vanished.py` performs, through the same calls. For an upload it
  also deletes the bytes from the landing zone and drops the row: there is
  nothing left to reinstate from.

:func:`apply` is the direct path for a process that writes its own index: one
source's removal with no listing, so the document leaves search as the request
returns. A fleet's api replica cannot do that — it publishes an incremental
sync instead, and the indexer's sync reaches :func:`prune` on its own.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pheasant.ingestion.landing import owned_upload_directory
from pheasant.persistence import archive

logger = logging.getLogger(__name__)

#: Ids per ``delete_artifacts`` call, as in `vanished.py` (the full-text delete
#: is a scan per call, so it is batched, never per file).
DELETE_BATCH = 500


def admit(state: Any, source_name: str, items: list[Any]) -> tuple[list[Any], list[dict]]:
    """``items`` less every path held out of ``source_name``, and the held rows."""

    held = archive.for_source(state, source_name)
    if not held:
        return items, held
    paths = {row["relative_path"] for row in held}
    return [item for item in items if item.relative_path not in paths], held


def prune(engine: Any, source_name: str, artifacts: dict[str, Any]) -> int:
    """Remove what is held out of ``source_name``; return how many artifacts went.

    ``artifacts`` is the source's manifest map and is updated in place. Read
    afresh rather than handed the rows :func:`admit` read, so a document
    archived while this sync was running is removed when it finishes.
    """

    held = archive.for_source(engine.state, source_name)
    if not held:
        return 0
    paths = {row["relative_path"] for row in held}
    gone = [
        (str(row["id"]), str(row["relative_path"]), row["git_branch"])
        for row in engine.state.rows(
            "SELECT id, relative_path, git_branch FROM artifacts WHERE source_id=?",
            (source_name,),
        )
        if row["relative_path"] in paths
    ]
    ids = [artifact_id for artifact_id, _, _ in gone]
    with engine._sync_mutex:
        if ids:
            engine._ensure_persisted_graph_loaded()
            for start in range(0, len(ids), DELETE_BATCH):
                engine.state.delete_artifacts(ids[start : start + DELETE_BATCH])
            engine.graph_builder.remove_vanished_artifacts(source_name, gone)
        for path in paths:
            artifacts.pop(path, None)
        _delete_uploads(engine, source_name, [r for r in held if r["kind"] == "removed"])
    if ids:
        logger.info("source %s: removed %d archived document(s)", source_name, len(ids))
    return len(ids)


def apply(engine: Any, source_name: str) -> int:
    """Remove what is held out of one source now, without a listing.

    Under the same per-source lock and commit meter as a sync, and finished by
    the sync's own finalize step — vectors pruned, graph published, manifest
    saved — so the index is in the state an incremental sync would leave it.
    Raises ``RuntimeError`` when a sync of the source holds the lock past
    ``sync.concurrency.lock_timeout_seconds``; that sync removes it as it ends.
    """

    from pheasant.sync.connectors import connector_for_source

    source = engine._source(source_name)
    with engine.commit_meter.busy(), engine._source_write_lock(source.name):
        manifest = engine.manifests.load(source.name)
        removed = prune(engine, source.name, manifest.setdefault("artifacts", {}))
        if removed:
            engine._finalize_index_state(
                source=source,
                connector=connector_for_source(source, engine.state),
                mode="incremental",
                manifest=manifest,
                indexed=removed,
                embedded_chunks=0,
                changed_ids=set(),
                total_items=0,
                report=_quiet,
                embedding_progress=_quiet,
            )
    return removed


def _delete_uploads(engine: Any, source_name: str, rows: list[dict]) -> None:
    """Delete removed uploads' bytes, and drop each row once its bytes are gone.

    Only inside the source's own landing directory (`owned_upload_directory`,
    which refuses symlinks), the rule `remove_source` already follows: an
    ordinary folder source points at user data and is never deleted from. A
    member of an uploaded ZIP has no bytes of its own, so its row stays and
    keeps holding it out while the archive is there.
    """

    if not rows:
        return
    registered = engine.state.get_source(source_name)
    root = (
        owned_upload_directory(
            engine.paths.state,
            str(registered["name"]),
            str(registered["type"]),
            str(registered["path"]),
        )
        if registered
        else None
    )
    if root is None:
        return
    base = root.resolve()
    for row in rows:
        relative = str(row["relative_path"])
        target = base / relative
        try:
            if not target.resolve().is_relative_to(base) or target.is_symlink():
                continue
            if target.is_file():
                target.unlink()
                _prune_empty_parents(target.parent, base)
            elif any((base / parent).is_file() for parent in Path(relative).parents):
                continue  # a member of an uploaded archive that is still there
        except OSError:
            logger.warning("could not delete removed upload %s", target, exc_info=True)
            continue
        archive.remove(engine.state, source_name, [relative])


def _prune_empty_parents(directory: Path, base: Path) -> None:
    while directory != base and directory.is_relative_to(base):
        try:
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent


def _quiet(*_args: Any, **_kwargs: Any) -> None:
    """A progress hook for a removal nobody is watching."""
