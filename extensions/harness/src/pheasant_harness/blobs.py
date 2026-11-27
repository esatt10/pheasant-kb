"""Immutable, content-addressed storage for everything bulky.

A blob is written before anything names it, and re-read on every get against
its own digest: a restore that lost an object, or a row whose bytes rotted,
reads as *missing* rather than as whatever is there (fixture A14).
"""

from __future__ import annotations

import base64
from typing import Any

from pheasant_harness.common import digest_bytes, now


class MissingBlob(LookupError):
    def __init__(self, digest: str, reason: str = "missing"):
        super().__init__(f"blob {digest} is {reason}")
        self.digest = digest
        self.reason = reason


class BlobStore:
    def __init__(self, state: Any):
        self.state = state

    def put(
        self, data: bytes, media_type: str = "application/octet-stream", classification="internal"
    ) -> str:
        digest = digest_bytes(data)
        self.state.execute(
            "INSERT INTO harness_blobs(digest,size_bytes,media_type,content_b64,classification,"
            "created_at) VALUES(?,?,?,?,?,?) ON CONFLICT (digest) DO NOTHING",
            (
                digest,
                len(data),
                media_type,
                base64.b64encode(data).decode("ascii"),
                classification,
                now(),
            ),
        )
        # Durable means readable back and intact, not "the INSERT returned".
        if self.get(digest) != data:  # pragma: no cover - storage corruption
            raise MissingBlob(digest, "not durable")
        return digest

    def put_text(self, text: str, media_type: str = "text/plain; charset=utf-8") -> str:
        return self.put(text.encode("utf-8"), media_type)

    def get(self, digest: str) -> bytes:
        rows = self.state.rows("SELECT content_b64 FROM harness_blobs WHERE digest=?", (digest,))
        if not rows:
            raise MissingBlob(digest)
        data = base64.b64decode(rows[0]["content_b64"])
        if digest_bytes(data) != digest:
            raise MissingBlob(digest, "corrupt")
        return data

    def get_text(self, digest: str) -> str:
        return self.get(digest).decode("utf-8")

    def exists(self, digest: str) -> bool:
        try:
            self.get(digest)
        except MissingBlob:
            return False
        return True

    def missing(self, digests) -> list[str]:
        return sorted({d for d in digests if d and not self.exists(d)})
