"""Files a source no longer has: found by the listing, removed on any sync.

An incremental sync skipped what was unchanged and indexed what was new, and
did nothing at all about what was *gone*. A deleted file kept its artifact
row, its chunks (so it stayed in every text search), its vectors and its graph
nodes until somebody ran a ``full`` sync, which clears the source and
re-reads everything to find out. Nothing announced the difference: the
deleted file simply went on being an answer.

The listing already says what exists. For a connector whose listing is
complete (``SourceConnector.complete_listing``), a path the manifest records
and the listing lacks is a file that was deleted, and it is removed here, in
one batch per sync, under the writer mutex:

* its state rows (``delete_artifacts``: artifact, chunks, full-text rows,
  symbols, terms). The full-text delete is a scan, because ``chunks_fts``
  does not index ``artifact_id`` (CLAUDE.md §6), so it runs once per batch
  of deletions, never per file;
* its graph nodes, owned nodes and emptied directories
  (``graph.retraction.remove_vanished_artifacts``);
* its manifest entry. Its vectors go in the finalize step, which already
  prunes every vector whose chunk row no longer exists.

One refusal: an **empty** listing over a non-empty manifest removes nothing.
A source that suddenly lists nothing is far more often a mount that went away
than a tree somebody emptied, and the cost of guessing wrong is the whole
index. A ``full`` sync is the explicit way to say "it really is empty".
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Ids per ``delete_artifacts`` call: inside SQLite's parameter limit, and few
#: enough calls that the per-call full-text scan stays a per-sync cost.
DELETE_BATCH = 500


def prune_vanished(
    engine: Any, source_name: str, connector: Any, items: list[Any], artifacts: dict[str, Any]
) -> int:
    """Remove what the manifest holds and a complete listing lacks.

    Returns the number of artifacts removed. ``artifacts`` is the source's
    manifest map (relative path -> entry) and is updated in place. ``engine``
    is the `SyncEngine` whose writer mutex, state and graph this runs under;
    it is passed whole because this is a step of its sync, kept in its own
    module so the step can be read on its own.
    """

    if not getattr(connector, "complete_listing", False):
        return 0
    listed = {item.relative_path for item in items}
    gone = sorted(path for path in artifacts if path not in listed)
    if not gone:
        return 0
    if not listed:
        logger.warning(
            "source %s listed nothing while %d file(s) are indexed; removing none. "
            "If the source really is empty, run a full sync.",
            source_name,
            len(artifacts),
        )
        return 0
    # The state rows are the truth for ids and branches; the manifest may
    # predate the artifact id being recorded in it.
    gone_paths = set(gone)
    vanished = [
        (str(row["id"]), str(row["relative_path"]), row["git_branch"])
        for row in engine.state.rows(
            "SELECT id, relative_path, git_branch FROM artifacts WHERE source_id=?",
            (source_name,),
        )
        if row["relative_path"] in gone_paths
    ]
    ids = [artifact_id for artifact_id, _, _ in vanished]
    with engine._sync_mutex:
        engine._ensure_persisted_graph_loaded()
        for start in range(0, len(ids), DELETE_BATCH):
            engine.state.delete_artifacts(ids[start : start + DELETE_BATCH])
        engine.graph_builder.remove_vanished_artifacts(source_name, vanished)
        for path in gone:
            artifacts.pop(path, None)
    logger.info("source %s: removed %d deleted file(s)", source_name, len(ids))
    return len(ids)
