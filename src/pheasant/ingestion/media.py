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

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class MediaStore:
    """Read and write stored media under one root."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, sha256: str, suffix: str) -> Path | None:
        suffix = suffix.lower()
        if not _SHA_RE.match(sha256 or "") or suffix not in MIME_TYPES:
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
