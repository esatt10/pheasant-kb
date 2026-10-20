"""A fetched web page as the parser read it, for a viewer to set beside its text.

A web collection's artifact path is a URL, so once a page's text was extracted
nothing held what the extractor had read, and a reader checking an extraction
had nothing to check it against. The indexer keeps that HTML in the media
store (``ingestion.media``) under the artifact's own sha256; this serves it.

HTTP only, like ``source_browser.raw_document``: it exists for a person's
viewer, and an agent reads the same page's text through ``read_document``.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from pheasant.ingestion.media import PAGE_SUFFIX, is_fetched_page, media_store_for_config
from pheasant.services import ServiceContext
from pheasant.services.errors import DocumentNotFound, InvalidRequest


def page_snapshot(context: ServiceContext, request: Any) -> dict[str, Any]:
    """``{content, url, sha256, name}``: a fetched page's HTML as it was indexed.

    ``request`` is a ``source_browser.ReadRequest``, resolved exactly as
    ``read_document`` resolves one. Keyed by the artifact's sha256, so it is
    the page the chunks were extracted from rather than the page as it is now,
    and it is the same on a replica that cannot reach the site. A page indexed
    before snapshots were kept has none until it changes or a ``full`` sync
    reads it again.
    """

    from pheasant.services.source_browser import _resolve

    context.knowledge_base(request.knowledge_base)
    row = _resolve(context, request.path, request.source_name, request)
    relative = str(row["relative_path"] or "")
    if not is_fetched_page(row["path"], relative, row["mime_type"]):
        raise InvalidRequest(f"{relative} is not a fetched web page")
    stored = snapshot_path(context, row)
    if stored is None:
        raise DocumentNotFound(f"{relative} (no snapshot of the page is held here)")
    return {
        "content": stored.read_bytes(),
        "url": str(row["path"]),
        "sha256": row["sha256"],
        "name": PurePosixPath(relative).name,
    }


def snapshot_path(context: ServiceContext, row: Any) -> Path | None:
    """Where ``row``'s page snapshot is stored, or ``None`` when none is held here."""

    store = media_store_for_config(context.config)
    stored = store.path_for(str(row["sha256"] or ""), PAGE_SUFFIX)
    try:
        return stored if stored is not None and stored.is_file() else None
    except OSError:
        return None
