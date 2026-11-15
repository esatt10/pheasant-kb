"""Where a citation points, in the terms a viewer can open.

A passage reaches an answer by one of two roads. The text and vector arms hit
a *chunk row*, and their result names the file's artifact as ``node_id``, the
chunk as ``chunk_id`` and the file as ``relative_path``. The graph arm, and a
walk out of another hit, reach the *chunk node*: its ``node_id`` is the chunk
itself, and it carries neither ``chunk_id`` nor ``relative_path``.

Both are the same passage of the same file, but only the first could be opened:
the web UI's citation chip opens ``source_id`` / ``relative_path`` at
``chunk_id``, so a graph-arm citation did nothing but switch the panel away
from the document the reader was looking at.

:func:`locate` fills the two fields from the chunk's own stable id
(``chunk:{source}:{relpath}:sha256=...:chunk=NNNN``, ``docs/graph_model.md``).
It is applied to the citations of a finished answer and nowhere earlier, so
what was retrieved, ranked and read is exactly what it was: this names a place,
it does not move one.
"""

from __future__ import annotations

from typing import Any


def locate(citation: dict[str, Any]) -> dict[str, Any]:
    """``citation`` with ``chunk_id`` and ``relative_path`` filled where its
    ``node_id`` is a chunk of ``source_id``. Returns the same dict."""

    source = citation.get("source_id")
    if not source or citation.get("memory"):
        return citation
    prefix = f"chunk:{source}:"
    node = str(citation.get("node_id") or "")
    if not citation.get("chunk_id") and node.startswith(prefix) and ":sha256=" in node:
        citation["chunk_id"] = node
    chunk = str(citation.get("chunk_id") or "")
    if not citation.get("relative_path") and chunk.startswith(prefix) and ":sha256=" in chunk:
        # The path may itself contain ':'; the digest marker is the last one.
        citation["relative_path"] = chunk[len(prefix) :].rsplit(":sha256=", 1)[0]
    return citation


def locate_all(payload: dict[str, Any]) -> dict[str, Any]:
    """:func:`locate` over an answer's (or a visual's) ``citations``."""

    for citation in payload.get("citations") or []:
        if isinstance(citation, dict):
            locate(citation)
    return payload
