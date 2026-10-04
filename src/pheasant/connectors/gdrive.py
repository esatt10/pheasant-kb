"""Google Drive connector (Product Framework Step 31.3) — SDK plugin.

Indexes the text-bearing files a Drive OAuth token can see (Drive API v3):
listing via ``GET /files`` with ``pageToken`` pagination, content via
``files/{id}/export`` for Google-native docs (exported as plain text) or
``files/{id}?alt=media`` for stored text files. Deterministic: content is
returned byte-for-byte from the API; no transformation beyond a stable
title header. Incremental: ``item.sha256`` is a ``(file_id, modifiedTime,
md5Checksum)`` version proxy for the engine's pre-read skip, and the
per-file cursor lets ``read_item`` raise :class:`ItemNotModified` for
unchanged files. Token from ``connector.api_key_env`` (default
``GDRIVE_TOKEN``). ACL capture (Phase 32 reserved): ``metadata["acl"]``
carries owner emails + the ``shared`` flag.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from pheasant.sync.connectors import (
    ConnectorItem,
    ConnectorPayload,
    ConnectorUnavailable,
    ItemNotModified,
    SourceConnector,
)

DEFAULT_BASE_URL = "https://www.googleapis.com/drive/v3"
DEFAULT_TOKEN_ENV = "GDRIVE_TOKEN"
PAGE_SIZE = 100
LIST_FIELDS = (
    "nextPageToken,files(id,name,mimeType,modifiedTime,md5Checksum,shared,"
    "owners(emailAddress),webViewLink)"
)
EXPORTABLE = {"application/vnd.google-apps.document": "text/plain"}
TEXT_MIMES = {"text/plain", "text/markdown", "text/csv", "text/html", "application/json"}


def _gdrive_request(url: str, token: str, timeout: int, *, raw: bool = False) -> Any:
    """One Drive API GET; module-level so tests monkeypatch it."""
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
    except HTTPError as exc:  # pragma: no cover - exercised via fakes
        raise ConnectorUnavailable(f"Drive API {exc.code} for {url}: {exc.reason}") from exc
    return body if raw else json.loads(body.decode("utf-8"))


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "untitled"


class GDriveConnector(SourceConnector):
    connector_type = "gdrive"
    complete_listing = True  # pages through every non-trashed file

    def _token(self) -> str:
        import os

        env_name = self.source.connector.api_key_env or DEFAULT_TOKEN_ENV
        token = os.environ.get(env_name)
        if not token:
            raise ConnectorUnavailable(
                f"gdrive connector for source {self.source.name} needs an OAuth "
                f"access token in ${env_name}"
            )
        return token

    def _base_url(self) -> str:
        return (self.source.connector.api_endpoint or DEFAULT_BASE_URL).rstrip("/")

    def list_items(self) -> list[ConnectorItem]:
        files: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            params = {"pageSize": PAGE_SIZE, "fields": LIST_FIELDS, "q": "trashed = false"}
            if token:
                params["pageToken"] = token
            result = _gdrive_request(
                f"{self._base_url()}/files?{urlencode(params)}",
                self._token(),
                self.source.connector.request_timeout_seconds,
            )
            files.extend(result.get("files", []))
            token = result.get("nextPageToken")
            if not token:
                break

        items: list[ConnectorItem] = []
        for file in sorted(files, key=lambda f: str(f.get("id"))):
            mime = str(file.get("mimeType") or "")
            if mime not in EXPORTABLE and mime not in TEXT_MIMES:
                continue
            file_id = str(file.get("id"))
            modified = str(file.get("modifiedTime") or "")
            checksum = str(file.get("md5Checksum") or "")
            name = str(file.get("name") or "untitled")
            suffix = ".md" if mime in EXPORTABLE else ".txt"
            relative = f"{_slug(name)}-{file_id[:8]}{suffix}"
            if not self._allows_relative_path(relative):
                continue
            items.append(
                ConnectorItem(
                    identity=f"gdrive:{self.source.name}:{file_id}",
                    relative_path=relative,
                    uri=str(file.get("webViewLink") or f"gdrive://{file_id}"),
                    mime_type="text/markdown" if mime in EXPORTABLE else mime,
                    sha256=hashlib.sha256(f"{file_id}:{modified}:{checksum}".encode()).hexdigest(),
                    mtime=modified or None,
                    etag=modified or None,
                    metadata={
                        "file_id": file_id,
                        "title": name,
                        "source_mime": mime,
                        "modified_time": modified,
                        # Phase 32 ACL capture (reserved).
                        "acl": {
                            "owners": [
                                str(o.get("emailAddress"))
                                for o in file.get("owners") or []
                                if o.get("emailAddress")
                            ],
                            "shared": bool(file.get("shared")),
                        },
                    },
                )
            )
        return items

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        file_id = str(item.metadata["file_id"])
        if self.sync_mode == "incremental" and self._previous_cursor:
            previous = (self._previous_cursor.get("file_versions") or {}).get(file_id)
            if previous and previous == item.metadata.get("modified_time"):
                raise ItemNotModified(f"drive file {file_id} unchanged since {previous}")
        source_mime = str(item.metadata.get("source_mime") or "")
        base = self._base_url()
        if source_mime in EXPORTABLE:
            url = f"{base}/files/{quote(file_id)}/export?mimeType={quote(EXPORTABLE[source_mime])}"
        else:
            url = f"{base}/files/{quote(file_id)}?alt=media"
        body = _gdrive_request(
            url, self._token(), self.source.connector.request_timeout_seconds, raw=True
        )
        header = f"# {item.metadata.get('title', 'untitled')}\n\n".encode()
        content = header + bytes(body)
        return ConnectorPayload(
            item=item,
            content=content,
            mime_type=item.mime_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            mtime=item.mtime,
            metadata=dict(item.metadata),
        )

    def checkpoint_from_items(
        self, items: list[ConnectorItem]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        versions = {
            str(item.metadata["file_id"]): str(item.metadata.get("modified_time") or "")
            for item in items
        }
        newest = max(versions.values(), default="")
        return {"file_versions": versions}, {"max_modified": newest, "file_count": len(items)}
