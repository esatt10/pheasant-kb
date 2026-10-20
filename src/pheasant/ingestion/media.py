"""The media store: image bytes a reader can be shown, content-addressed under `/state`.

Indexing an image turns it into text — a caption — which is what makes it
searchable. Showing it needs the bytes, and the bytes are not reliably
anywhere a serving process can read: a Notion or Drive image has no path at
all, and in the role-split fleet the api tier mounts `/state` but not the
sources. So the indexer, which holds the bytes at the moment it prepares the
artifact, writes them here, and every tier reads them back.

* **Content-addressed** (``<sha256[:2]>/<sha256><suffix>``): the artifact row
  already carries the sha256, so no index is needed, an unchanged image is
  never rewritten, and two documents embedding one logo store it once.
* **Bounded** by ``MAX_MEDIA_BYTES``. A larger image is still indexed and
  captioned; it just cannot be shown inline, which is the right failure for
  a chat surface.
* **User data**, like everything under `/state` (CLAUDE.md rule 2): nothing
  here deletes. A region that stops referencing an image leaves its bytes,
  and ``pheasant scan`` reports the volume.

Deliberately limited to raster formats pheasant already ingests. SVG is not
one of them and is not served: it is a document that can carry script.

One exception, kept apart from the images: the HTML of a *fetched* web page
(``PAGE_TYPES``). A web collection's artifact path is a URL, so once its text
is extracted there is no copy of what the parser read, and a reader checking
an extraction has nothing to check it against. The snapshot is that copy. It
is never served as an image (``mime_for`` does not know it) and is served as a
page only into a frame that runs nothing (``services.source_browser``).
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any

MEDIA_DIRNAME = "media"
MAX_MEDIA_BYTES = 8 * 1024 * 1024

MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

#: A fetched web page's HTML, as it was when it was indexed. Never an image.
PAGE_TYPES = {".html": "text/html"}
PAGE_SUFFIX = ".html"

_HTML_MIMES = frozenset({"text/html", "application/xhtml+xml"})
_HTML_SUFFIXES = frozenset({".html", ".htm", ".xhtml"})

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class MediaStore:
    """Read and write stored media under one root."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, sha256: str, suffix: str) -> Path | None:
        suffix = suffix.lower()
        known = suffix in MIME_TYPES or suffix in PAGE_TYPES
        if not _SHA_RE.match(sha256 or "") or not known:
            return None
        return self.root / sha256[:2] / f"{sha256}{suffix}"

    def put(self, sha256: str, suffix: str, content: bytes) -> Path | None:
        """Store ``content`` once. Returns the path, or ``None`` if not storable.

        The temp name is unique per writer and the move is atomic, so two
        indexers racing on one image both succeed and a reader never sees a
        partial file (the fixed-``.partial``-name collision in CLAUDE.md §6).
        """

        target = self.path_for(sha256, suffix)
        if target is None or len(content) > MAX_MEDIA_BYTES:
            return None
        if target.exists():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
        try:
            temp.write_bytes(content)
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink()
        return target

    def get(self, sha256: str, suffix: str) -> bytes | None:
        target = self.path_for(sha256, suffix)
        if target is None or not target.is_file():
            return None
        return target.read_bytes()

    def usage(self) -> dict[str, int]:
        """Files and bytes stored, for ``pheasant scan`` and diagnostics."""

        files = total = 0
        if self.root.is_dir():
            for path in self.root.rglob("*"):
                if path.is_file() and not path.name.startswith("."):
                    files += 1
                    total += path.stat().st_size
        return {"files": files, "bytes": total}


def media_store_for_config(config: Any) -> MediaStore:
    return MediaStore(Path(config.pheasant.state_path) / MEDIA_DIRNAME)


def mime_for(relative_path: str) -> str | None:
    return MIME_TYPES.get(Path(relative_path or "").suffix.lower())


def is_fetched_page(path: Any, relative_path: Any, mime_type: Any) -> bool:
    """Whether an artifact is an HTML page read from a URL rather than a file.

    The artifact path is the URL for a web collection; a local ``.html`` file
    has a file to show and needs no snapshot.
    """

    location = str(path or "").lower()
    if not location.startswith(("http://", "https://")):
        return False
    mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    return mime in _HTML_MIMES or Path(str(relative_path or "")).suffix.lower() in _HTML_SUFFIXES


def stored_suffix(artifact: Any) -> str | None:
    """The suffix ``artifact``'s bytes are stored under, or ``None`` if they are not kept.

    An image under its own suffix; a fetched page under ``.html`` whatever its
    relative path says (an extensionless URL is indexed as ``….txt``).
    """

    if artifact is None:
        return None
    if artifact.type == "image":
        suffix = Path(artifact.relative_path).suffix.lower()
        return suffix if suffix in MIME_TYPES else None
    if is_fetched_page(artifact.path, artifact.relative_path, artifact.mime_type):
        return PAGE_SUFFIX
    return None
