"""Serving an indexed image's bytes, once, for both surfaces.

``GET /media`` and the MCP ``get_image`` tool call :func:`get_media`. It
answers only for ``image`` artifacts, under the same read check as every
other content operation (``graph.require_readable``): search already filters
images a principal may not see, and serving their bytes by id would undo it.

Bytes come from the media store (``ingestion.media``), which the indexer fills
as it prepares each image. An image indexed before the store existed has no
stored copy; if its indexed path is still readable here — a standalone
container, whose sources are mounted — the file is read directly. That path
comes from the region's own artifact row, never from the caller, so there is
nothing for a caller to traverse. A full re-sync fills the store for good.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pheasant.ingestion.media import MAX_MEDIA_BYTES, media_store_for_config, mime_for
from pheasant.services import ServiceContext
from pheasant.services.errors import MediaNotFound
from pheasant.services.graph import require_readable


@dataclass(frozen=True)
class MediaRequest:
    node_id: str
    knowledge_base: str | None = None
    principal: str | None = None
    principal_groups: list[str] | None = None


def get_media(context: ServiceContext, request: MediaRequest) -> dict[str, Any]:
    """``{node_id, mime_type, sha256, relative_path, caption, content}``.

    ``content`` is bytes; each adapter encodes it for its transport.
    """

    context.knowledge_base(request.knowledge_base)
    rows = context.state.rows(
        "SELECT id, type, path, relative_path, sha256, size_bytes, source_id "
        "FROM artifacts WHERE id=? LIMIT 1",
        (request.node_id,),
    )
    if not rows or rows[0]["type"] != "image":
        raise MediaNotFound(request.node_id)
    row = dict(rows[0])
    # Before the bytes are even looked up: a principal that may not read the
    # artifact must not learn from a 404-versus-403 whether it was stored.
    require_readable(context, row["id"], request.principal, request.principal_groups)
    relative = str(row.get("relative_path") or "")
    mime = mime_for(relative)
    if mime is None:
        raise MediaNotFound(request.node_id)
    suffix = Path(relative).suffix
    content = media_store_for_config(context.config).get(str(row.get("sha256") or ""), suffix)
    if content is None:
        content = _from_indexed_path(row)
    if content is None:
        raise MediaNotFound(request.node_id)
    caption_rows = context.state.rows(
        "SELECT text FROM chunks WHERE artifact_id=? ORDER BY chunk_index LIMIT 1",
        (row["id"],),
    )
    return {
        "node_id": row["id"],
        "mime_type": mime,
        "sha256": row.get("sha256"),
        "relative_path": relative,
        "source_id": row.get("source_id"),
        "size_bytes": len(content),
        "caption": str(caption_rows[0]["text"]) if caption_rows else "",
        "content": content,
    }


def _from_indexed_path(row: dict[str, Any]) -> bytes | None:
    path = Path(str(row.get("path") or ""))
    try:
        if not path.is_file() or path.stat().st_size > MAX_MEDIA_BYTES:
            return None
        content = path.read_bytes()
    except OSError:
        return None
    # The file may have changed since it was indexed; serving different bytes
    # under the indexed id would show a picture nobody's caption describes.
    from pheasant.ingestion.pipeline import sha256_bytes

    return content if sha256_bytes(content) == row.get("sha256") else None
