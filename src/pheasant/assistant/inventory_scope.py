"""``@current`` / ``@dir`` / ``@exclude`` applied to a question about the index.

A scope keyword narrows whatever else the message asks for. For an answer or
``@search`` that is the retriever's business (``PheasantRetriever`` holds the
scope). For the index lookups it is this module's: it fills in what the
lookup was not told, and never overrides what it was —

* ``@dir @taxonomy`` browses the folder; ``@current @taxonomy`` the open
  document's folder.
* ``@current @doc`` describes the open document; ``@dir @doc`` (a folder is
  not a document) lists the folder's documents.
* ``@dir @docs``, ``@dir recent documents``, ``@dir how many documents`` list
  or count only the folder, less anything ``@exclude`` named.
* ``@current @links`` pages the open document's links; under ``@dir`` links
  are summarised for the folder's source, and the answer says so.
* ``@dir @source`` describes the source the folder is in.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

from pheasant.assistant.inventory import InventoryQuestion

#: Index lookups a folder or exclusion narrows, through `DocumentsRequest`.
LISTINGS = frozenset({"documents", "recent", "counts"})


def apply(asked: InventoryQuestion, scope: Any, word: str | None) -> InventoryQuestion:
    """``asked`` narrowed to ``scope``. ``word`` is the shorthand typed (``document`` …)."""

    if scope is None or not (scope.path_prefix or scope.exclusion or scope.keywords):
        return asked
    source = scope.source_name
    if not source:
        return replace(asked, notes=(*asked.notes, *scope.notes))
    file = scope.path_prefix if scope.kind == "file" else None
    folder = scope.path_prefix if scope.kind == "folder" else None
    if file:
        folder = str(PurePosixPath(file).parent).strip(".") or None
    notes = list(scope.notes)
    action = asked.action
    if action == "taxonomy":
        if not asked.source_name:
            asked = replace(asked, source_name=source, path=asked.path or folder)
    elif action == "documents" and word == "document" and file and not asked.path_contains:
        asked = replace(asked, action="document", path=f"{source}/{file}", source_name=None)
    elif action in {"source", "sources"} and word in {"source", None} and not asked.source_name:
        asked = replace(asked, action="source", source_name=source)
    elif action == "links" and not (asked.path or asked.source_name):
        if file:
            asked = replace(asked, path=f"{source}/{file}")
        else:
            asked = replace(asked, source_name=source)
            if folder:
                notes.append(f"links are summarised for all of `{source}`, not one folder")
    if asked.action in LISTINGS:
        asked = replace(
            asked,
            source_name=asked.source_name or source,
            path_prefix=scope.path_prefix,
            exclude=tuple(scope.excluded),
        )
    return replace(asked, notes=(*asked.notes, *notes))
