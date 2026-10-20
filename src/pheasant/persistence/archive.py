"""Documents taken out of the index on purpose, and kept out.

Free functions over a state store, following `receipts.py`: one bounded
context, a handful of operations, and `state_store.py` is the file the module
ratchet asks features to stay out of.

A row here is the truth a sync consults. Removing a document's artifact row
alone would last until the next sync re-read the file, so every sync skips a
path held here before reading it (`sync/archived.py`) — incremental, full or
repair alike. Two kinds:

* ``archived`` — the file still exists in its source. Deleting the row and
  syncing brings it back: the content, its chunks, vectors and graph nodes are
  rebuilt from the file, exactly as for a file seen for the first time.
* ``removed`` — an upload, whose bytes live in the region's own landing zone.
  There is nothing to reinstate from once they are gone, so the indexer that
  deletes them drops the row too. Until it has, the row keeps the document out
  (a fleet's api replica writes the row; only the indexer may delete bytes).

Keyed by ``(source_id, relative_path)`` rather than by artifact id, because
the path is what a listing yields and what a sync can test before it reads;
the artifact id is kept to answer "which document was this".
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

KINDS = ("archived", "removed")


def add(
    state: Any,
    *,
    source_id: str,
    relative_path: str,
    artifact_id: str | None,
    kind: str,
    now: str,
    archived_by: str | None = None,
    acl: str | None = None,
) -> None:
    """Hold one document out. Idempotent: archiving twice keeps the first time."""

    if kind not in KINDS:
        raise ValueError(f"unknown archive kind: {kind}")
    state.execute(
        """INSERT INTO archived_documents(
            source_id, relative_path, artifact_id, kind, archived_at, archived_by, acl
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, relative_path) DO UPDATE SET
            kind=excluded.kind,
            artifact_id=COALESCE(excluded.artifact_id, archived_documents.artifact_id),
            acl=COALESCE(excluded.acl, archived_documents.acl)""",
        (source_id, relative_path, artifact_id, kind, now, archived_by, acl),
    )


def remove(state: Any, source_id: str, relative_paths: Iterable[str]) -> None:
    """Let documents back in: the next sync of ``source_id`` reads them again."""

    for path in relative_paths:
        state.execute(
            "DELETE FROM archived_documents WHERE source_id=? AND relative_path=?",
            (source_id, path),
        )


def for_source(state: Any, source_id: str) -> list[dict[str, Any]]:
    """Every document held out of ``source_id``, in path order."""

    return [
        dict(row)
        for row in state.rows(
            "SELECT * FROM archived_documents WHERE source_id=? ORDER BY relative_path",
            (source_id,),
        )
    ]


def everything(state: Any) -> list[dict[str, Any]]:
    """Every document held out of any source, by source then path."""

    return [
        dict(row)
        for row in state.rows("SELECT * FROM archived_documents ORDER BY source_id, relative_path")
    ]


__all__ = ["KINDS", "add", "everything", "for_source", "remove"]
