"""OneDrive and SharePoint connector -- SDK plugin (``type: sharepoint``).

A source is one site (a SharePoint site or a person's OneDrive), and its
``urls`` are what to read in it: any link someone can paste. Each is expanded
at listing time (``connectors/sharepoint_urls.py`` says what it names), and the
site comes out as folders, the way a web collection's pages do:

* ``<library>/<folder>/.../<file>`` -- every file in a document library, a
  folder, or one file; shared folders from another drive are walked too.
* ``Lists/<list>/<id>-<title>.md`` -- one Markdown document per list item, its
  fields under their display names, and beside it
  ``Lists/<list>/<id>-<title>/<attachment>`` for each attachment, which the
  item's document links to (so the graph has the edge, and the taxonomy shows
  the attachments under their item).
* ``Pages/<page>.html`` -- each modern page rendered from its canvas: kept as
  the page's HTML, so the viewer shows it beside its text.

A link that cannot be read (removed, or not shared with this connection) is
named unreadable in the sync's health instead of failing the source, and that
pass then removes nothing, because a listing missing one link's content is not
a complete listing. Auth failures (401) fail the source: nothing can be read.

Incremental: each item's ``sha256`` is a version proxy (the drive item's
``cTag``/``eTag``, a list item's ``eTag``, a page's ``lastModifiedDateTime``),
so the engine skips what has not changed before anything is downloaded, and
the per-item versions in the checkpoint let ``read_item`` skip too.

Credentials: the source's ``connector.connection`` (a Microsoft 365 connection:
a sign-in, an app registration, or a pasted token), else a token in
``connector.api_key_env`` (default ``MICROSOFT_GRAPH_TOKEN``), else the region's
one Microsoft 365 connection.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from pheasant.connectors import graph_client as graph
from pheasant.connectors.sharepoint_urls import Target, parse
from pheasant.sync.connectors import (
    ConnectorItem,
    ConnectorPayload,
    ConnectorUnavailable,
    ItemNotModified,
    SourceConnector,
)

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_ENV = "MICROSOFT_GRAPH_TOKEN"
BROAD_INCLUDES = {"*", "**", "**/*", "*.*", "**/*.*"}
#: List fields that are SharePoint's bookkeeping, not the item's content.
SYSTEM_FIELDS = {
    "ContentType", "Edit", "LinkTitle", "LinkTitleNoMenu", "DocIcon", "ItemChildCount",
    "FolderChildCount", "AppAuthor", "AppEditor", "Attachments", "id", "ID",
    "AuthorLookupId", "EditorLookupId", "ComplianceAssetId",
}  # fmt: skip
UNREACHABLE_FOLDER = "_unreadable"


def _segment(name: str) -> str:
    """One path segment: no separator, no control character."""

    cleaned = re.sub(r"[\x00-\x1f/\\]+", "-", str(name or "")).strip().strip(".")
    return cleaned or "untitled"


def _slug(text: str, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return slug[:limit].strip("-") or "item"


def _version(*parts: Any) -> str:
    return ":".join(str(part or "") for part in parts)


def _plain(value: Any) -> str:
    """A list field's value as text: lookups by their label, people by name."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(text for text in (_plain(entry) for entry in value) if text)
    if isinstance(value, dict):
        for key in ("LookupValue", "Label", "displayName", "Email", "email", "Url", "url"):
            if value.get(key):
                return str(value[key])
        return json.dumps(value, sort_keys=True)
    text = str(value)
    if "<" in text and re.search(r"</?(div|p|br|span|ul|li|strong|em|a)\b", text, re.I):
        # A rich-text column holds HTML; its words are what an item says.
        text = re.sub(r"<\s*(br|/p|/div|/li)\s*/?>", "\n", text, flags=re.I)
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


class SharePointConnector(SourceConnector):
    connector_type = "sharepoint"
    complete_listing = True

    def begin_sync(self, mode: str = "incremental") -> None:
        super().begin_sync(mode)
        # Per pass: a link that failed last time and reads now completes it.
        self.complete_listing = True
        self._sites: dict[tuple[str, str], dict[str, Any]] = {}
        self._drives: dict[str, list[dict[str, Any]]] = {}
        self._drive_names: dict[str, str] = {}

    # -- credentials ---------------------------------------------------------

    def _secret(self) -> dict[str, Any] | None:
        """The connection's sealed fields, or ``None`` for a token in a variable."""

        import os

        from pheasant import connections
        from pheasant.connections import resolve

        try:
            name = connections.source_connection(self.source)
            if name:
                _, secret = connections.named(name, providers_allowed=("microsoft_365",))
                return secret
            env_name = self.source.connector.api_key_env or DEFAULT_TOKEN_ENV
            token = os.environ.get(env_name) or connections.secret(env_name)
            if token:
                return {"token": token}
            row = connections.ambient("microsoft_365")
            if row is not None:
                return resolve.opened(row)
        except connections.ConnectionUnavailable as exc:
            raise ConnectorUnavailable(f"sharepoint source {self.source.name}: {exc}") from exc
        raise ConnectorUnavailable(
            f"sharepoint source {self.source.name} needs a Microsoft 365 connection "
            f"(Sources > Connect > Microsoft 365) or a Graph access token in ${env_name}"
        )

    def _graph(self) -> str:
        from pheasant.connections import microsoft

        try:
            return microsoft.graph_token(self._secret() or {})
        except microsoft.MicrosoftAuthError as exc:
            raise ConnectorUnavailable(f"sharepoint source {self.source.name}: {exc}") from exc

    def _sharepoint(self, host: str) -> str:
        from pheasant.connections import microsoft

        return microsoft.sharepoint_token(self._secret() or {}, host)

    # -- listing ---------------------------------------------------------------

    def list_items(self) -> list[ConnectorItem]:
        if not getattr(self, "_sites", None):
            self.begin_sync(self.sync_mode or "incremental")
        if not self.source.urls:
            raise ConnectorUnavailable(
                f"sharepoint source {self.source.name} lists no links; add a site, library, "
                "folder, list or page URL"
            )
        token = self._graph()
        found: dict[str, ConnectorItem] = {}
        for url in self.source.urls:
            try:
                for item in self._expand(token, url):
                    if item.relative_path not in found and self._admits(item):
                        found[item.relative_path] = item
            except graph.GraphError as exc:
                if exc.status == 401:
                    raise ConnectorUnavailable(
                        f"sharepoint source {self.source.name}: Microsoft refused the "
                        f"credential ({exc}); sign in again"
                    ) from exc
                self._unreachable(found, url, exc)
            except (ValueError, OSError) as exc:
                self._unreachable(found, url, exc)
        return [found[path] for path in sorted(found)]

    def _unreachable(self, found: dict[str, ConnectorItem], url: str, error: Exception) -> None:
        """Name a link that could not be read, and keep this pass from pruning."""

        self.complete_listing = False
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        relative = f"{UNREACHABLE_FOLDER}/{_slug(url, 70)}-{digest}.txt"
        logger.warning("sharepoint source %s: could not read %s: %s", self.source.name, url, error)
        found[relative] = ConnectorItem(
            identity=f"sharepoint:{self.source.name}:link:{digest}",
            relative_path=relative,
            uri=url,
            mime_type="text/plain",
            sha256=hashlib.sha256(f"{url}:{error}".encode()).hexdigest(),
            metadata={"kind": "unreachable", "error": f"{url}: {error}"},
        )

    def _admits(self, item: ConnectorItem) -> bool:
        """Depth and excludes always; ``include`` only when chosen, else every
        format the pipeline reads (a library is mostly Office files and PDFs,
        which the stock code-and-Markdown globs would drop without a word)."""

        from pheasant.config.schema import DEFAULT_INCLUDES
        from pheasant.ingestion.content_types import (
            AUDIO_EXTENSIONS,
            DOCUMENT_EXTENSIONS,
            IMAGE_EXTENSIONS,
            is_text_file,
        )
        from pheasant.sync.connectors import _match_any, within_max_depth

        relative = item.relative_path
        if item.metadata.get("kind") == "unreachable":
            return True
        include = list(self.source.include)
        chosen = include != list(DEFAULT_INCLUDES) and not (
            set(pattern.strip("/") for pattern in include) <= BROAD_INCLUDES and include
        )
        if chosen:
            return self._allows_relative_path(relative)
        if not within_max_depth(relative, self.source.max_depth):
            return False
        if _match_any(relative, self.source.exclude):
            return False
        suffix = PurePosixPath(relative).suffix.lower()
        return (
            is_text_file(relative)
            or suffix in DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS
            or suffix == ".html"
        )

    def _site(self, token: str, host: str, site: str) -> dict[str, Any]:
        key = (host, site.lower())
        if key not in self._sites:
            self._sites[key] = graph.site_by_path(token, host, site)
        return self._sites[key]

    def _site_drives(self, token: str, site_id: str) -> list[dict[str, Any]]:
        if site_id not in self._drives:
            self._drives[site_id] = graph.site_drives(token, site_id)
            for drive in self._drives[site_id]:
                self._drive_names[str(drive.get("id"))] = str(drive.get("name") or "Documents")
        return self._drives[site_id]

    def _expand(self, token: str, url: str):
        target = parse(url)
        if target.kind == "share":
            yield from self._share(token, target)
            return
        site = self._site(token, target.host, target.site)
        site_id = str(site["id"])
        if target.kind == "site":
            for drive in self._site_drives(token, site_id):
                if self._is_pages_library(drive):
                    continue
                yield from self._drive_files(token, drive, "", _segment(drive.get("name")))
            for entry in graph.site_lists(token, site_id):
                yield from self._list(token, site, entry)
            yield from self._pages(token, site, None)
        elif target.kind == "drive_path":
            yield from self._drive_path(token, site, target)
        elif target.kind == "list":
            yield from self._named_list(token, site, target.name)
        elif target.kind == "pages":
            yield from self._pages(token, site, None)
        elif target.kind == "page":
            yield from self._pages(token, site, target.name)

    @staticmethod
    def _is_pages_library(drive: dict[str, Any]) -> bool:
        path = unquote(urlparse(str(drive.get("webUrl") or "")).path).rstrip("/").lower()
        return path.endswith("/sitepages")

    # -- drives ------------------------------------------------------------------

    def _drive_path(self, token: str, site: dict[str, Any], target: Target):
        wanted = target.path.rstrip("/").lower()
        best: tuple[int, dict[str, Any]] | None = None
        for drive in self._site_drives(token, str(site["id"])):
            root = unquote(urlparse(str(drive.get("webUrl") or "")).path).rstrip("/").lower()
            if root and (wanted == root or wanted.startswith(root + "/")):
                if best is None or len(root) > best[0]:
                    best = (len(root), drive)
        if best is None:
            # Not under a library this site lists (a link into another site, or
            # a page of the old UI): the shares API can name it.
            yield from self._share(token, target)
            return
        root_length, drive = best
        within = target.path.rstrip("/")[root_length:].strip("/")
        yield from self._drive_files(token, drive, within, _segment(drive.get("name")))

    def _drive_files(self, token: str, drive: dict[str, Any], within: str, library: str):
        drive_id = str(drive["id"])
        item = graph.item_by_path(token, drive_id, within)
        prefix = "/".join([library, *(_segment(part) for part in within.split("/") if part)])
        if item.get("folder") is not None:
            for path, entry, entry_drive in graph.walk(token, drive_id, str(item["id"]), prefix):
                yield self._file_item(path, entry, entry_drive)
        elif item.get("file") is not None:
            yield self._file_item(prefix, item, drive_id)

    def _share(self, token: str, target: Target):
        item = graph.resolve_share(token, target.url)
        parent = item.get("parentReference") or {}
        drive_id = str(parent.get("driveId") or "")
        if not drive_id:
            raise ValueError("the link resolved to no drive item")
        library = self._drive_names.get(drive_id)
        if library is None:
            try:
                library = str(
                    graph.request(
                        "GET", f"{graph.GRAPH}/drives/{drive_id}?$select=name", token
                    ).get("name")
                    or "Shared"
                )
            except graph.GraphError:
                library = "Shared"
            self._drive_names[drive_id] = library
        # parentReference.path is "/drives/<id>/root:/A/B" ("" when not shared).
        within = str(parent.get("path") or "").partition("root:")[2].strip("/")
        parts = [_segment(library), *(_segment(p) for p in unquote(within).split("/") if p)]
        prefix = "/".join([*parts, _segment(item.get("name"))])
        if item.get("folder") is not None:
            for path, entry, entry_drive in graph.walk(token, drive_id, str(item["id"]), prefix):
                yield self._file_item(path, entry, entry_drive)
        elif item.get("file") is not None:
            yield self._file_item(prefix, item, drive_id)

    def _file_item(self, relative: str, entry: dict[str, Any], drive_id: str) -> ConnectorItem:
        item_id = str(entry.get("id"))
        version = _version(
            entry.get("cTag") or entry.get("eTag"),
            entry.get("size"),
            entry.get("lastModifiedDateTime"),
        )
        mime = str((entry.get("file") or {}).get("mimeType") or "") or None
        return ConnectorItem(
            identity=f"sharepoint:{self.source.name}:file:{drive_id}:{item_id}",
            relative_path=relative,
            uri=str(entry.get("webUrl") or f"msgraph://drives/{drive_id}/items/{item_id}"),
            mime_type=mime,
            size_bytes=int(entry["size"]) if isinstance(entry.get("size"), int) else None,
            sha256=hashlib.sha256(f"{drive_id}:{item_id}:{version}".encode()).hexdigest(),
            mtime=entry.get("lastModifiedDateTime"),
            etag=entry.get("eTag"),
            metadata={
                "kind": "file",
                "drive_id": drive_id,
                "item_id": item_id,
                "version": version,
                "title": str(entry.get("name") or ""),
            },
        )

    # -- lists ---------------------------------------------------------------------

    def _named_list(self, token: str, site: dict[str, Any], name: str):
        wanted = name.strip().lower()
        for entry in graph.site_lists(token, str(site["id"]), include_hidden=True):
            web = unquote(urlparse(str(entry.get("webUrl") or "")).path).rstrip("/").lower()
            names = {
                str(entry.get("name") or "").lower(),
                str(entry.get("displayName") or "").lower(),
            }
            if wanted in names or web.endswith(f"/lists/{wanted}"):
                yield from self._list(token, site, entry)
                return
        raise ValueError(f"no list named {name!r} in {site.get('webUrl')}")

    def _list(self, token: str, site: dict[str, Any], entry: dict[str, Any]):
        site_id = str(site["id"])
        list_id = str(entry["id"])
        title = str(entry.get("displayName") or entry.get("name") or "List")
        folder = f"Lists/{_segment(title)}"
        columns = {
            str(column.get("name")): str(column.get("displayName") or column.get("name"))
            for column in graph.list_columns(token, site_id, list_id)
            if not column.get("hidden")
            and (not column.get("readOnly") or column.get("name") in ("Created", "Modified"))
        }
        attachments_failed: str | None = None
        for row in graph.list_items(token, site_id, list_id):
            fields = dict(row.get("fields") or {})
            item_id = str(row.get("id"))
            name = f"{item_id}-{_slug(fields.get('Title') or fields.get('FileLeafRef') or '')}"
            version = _version(row.get("eTag"), row.get("lastModifiedDateTime"))
            files: list[dict[str, str]] = []
            if fields.get("Attachments") and attachments_failed is None:
                try:
                    files = graph.list_item_attachments(
                        self._sharepoint(urlparse(str(site.get("webUrl"))).hostname or ""),
                        str(site.get("webUrl")),
                        list_id,
                        item_id,
                    )
                except Exception as exc:  # noqa: BLE001 - attachments are best-effort
                    attachments_failed = str(exc)
                    logger.warning(
                        "sharepoint source %s: list %s attachments unreadable: %s",
                        self.source.name,
                        title,
                        exc,
                    )
            attached = [(f"{folder}/{name}/{_segment(file['FileName'])}", file) for file in files]
            yield ConnectorItem(
                identity=f"sharepoint:{self.source.name}:listitem:{list_id}:{item_id}",
                relative_path=f"{folder}/{name}.md",
                uri=str(row.get("webUrl") or ""),
                mime_type="text/markdown",
                sha256=hashlib.sha256(f"{list_id}:{item_id}:{version}".encode()).hexdigest(),
                mtime=row.get("lastModifiedDateTime"),
                etag=row.get("eTag"),
                metadata={
                    "kind": "list_item",
                    "version": version,
                    "list": title,
                    "list_url": entry.get("webUrl"),
                    "item_id": item_id,
                    "fields": fields,
                    "columns": columns,
                    "created": row.get("createdDateTime"),
                    "modified": row.get("lastModifiedDateTime"),
                    "attachments": [relative for relative, _file in attached],
                    "attachments_error": (
                        attachments_failed if fields.get("Attachments") and not files else None
                    ),
                    "title": str(fields.get("Title") or name),
                },
            )
            for relative, file in attached:
                yield ConnectorItem(
                    identity=(
                        f"sharepoint:{self.source.name}:attachment:{list_id}:{item_id}:"
                        f"{file['FileName']}"
                    ),
                    relative_path=relative,
                    uri=f"https://{urlparse(str(site.get('webUrl'))).hostname}{file['ServerRelativeUrl']}",
                    sha256=hashlib.sha256(
                        f"{list_id}:{item_id}:{version}:{file['FileName']}".encode()
                    ).hexdigest(),
                    mtime=row.get("lastModifiedDateTime"),
                    metadata={
                        "kind": "attachment",
                        "version": version,
                        "site_url": str(site.get("webUrl")),
                        "server_relative_url": file["ServerRelativeUrl"],
                        "title": file["FileName"],
                    },
                )

    # -- pages -----------------------------------------------------------------------

    def _pages(self, token: str, site: dict[str, Any], only: str | None):
        site_id = str(site["id"])
        wanted = only.strip().lower() if only else None
        matched = False
        try:
            pages = list(graph.site_pages(token, site_id))
        except graph.GraphError as exc:
            if wanted is None and exc.status in (400, 404):
                return  # a site with no modern pages (a OneDrive) has no pages list
            raise
        for page in pages:
            name = str(page.get("name") or page.get("title") or page.get("id"))
            if wanted is not None and name.lower() != wanted:
                continue
            matched = True
            version = _version(page.get("eTag"), page.get("lastModifiedDateTime"))
            stem = _segment(name[: -len(".aspx")] if name.lower().endswith(".aspx") else name)
            yield ConnectorItem(
                identity=f"sharepoint:{self.source.name}:page:{site_id}:{page.get('id')}",
                relative_path=f"Pages/{stem}.html",
                uri=str(page.get("webUrl") or f"msgraph://sites/{site_id}/pages/{page.get('id')}"),
                mime_type="text/html",
                sha256=hashlib.sha256(f"{site_id}:{page.get('id')}:{version}".encode()).hexdigest(),
                mtime=page.get("lastModifiedDateTime"),
                etag=page.get("eTag"),
                metadata={
                    "kind": "page",
                    "version": version,
                    "site_id": site_id,
                    "page_id": str(page.get("id")),
                    "title": str(page.get("title") or stem),
                },
            )
        if wanted is not None and not matched:
            raise ValueError(f"no page named {only!r} in {site.get('webUrl')}")

    # -- reading -----------------------------------------------------------------------

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        kind = item.metadata.get("kind")
        if kind == "unreachable":
            raise RuntimeError(str(item.metadata.get("error") or "this link could not be read"))
        if self.sync_mode == "incremental" and self._previous_cursor:
            previous = (self._previous_cursor.get("versions") or {}).get(item.identity)
            if previous and previous == item.metadata.get("version"):
                raise ItemNotModified(f"{item.relative_path} unchanged since {previous}")
        if kind == "file":
            content = graph.download(
                self._graph(), str(item.metadata["drive_id"]), str(item.metadata["item_id"])
            )
        elif kind == "list_item":
            content = self._render_list_item(item).encode("utf-8")
        elif kind == "attachment":
            site_url = str(item.metadata["site_url"])
            content = graph.attachment_content(
                self._sharepoint(urlparse(site_url).hostname or ""),
                site_url,
                str(item.metadata["server_relative_url"]),
            )
        elif kind == "page":
            page = graph.page_with_canvas(
                self._graph(), str(item.metadata["site_id"]), str(item.metadata["page_id"])
            )
            content = graph.render_canvas(page).encode("utf-8")
        else:
            raise RuntimeError(f"unknown sharepoint item kind {kind!r}")
        return ConnectorPayload(
            item=item,
            content=content,
            mime_type=item.mime_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            mtime=item.mtime,
            metadata=dict(item.metadata),
        )

    @staticmethod
    def _render_list_item(item: ConnectorItem) -> str:
        meta = item.metadata
        fields: dict[str, Any] = meta.get("fields") or {}
        columns: dict[str, str] = meta.get("columns") or {}
        title = str(fields.get("Title") or meta.get("title") or "Untitled")
        lines = [f"# {title}", ""]
        lines.append(f"- **List:** {meta.get('list')}")
        lines.append(f"- **Item:** {meta.get('item_id')}")
        if meta.get("created"):
            lines.append(f"- **Created:** {meta['created']}")
        if meta.get("modified"):
            lines.append(f"- **Modified:** {meta['modified']}")
        if item.uri:
            lines.append(f"- **Link:** <{item.uri}>")
        long_fields: list[tuple[str, str]] = []
        for name in sorted(fields, key=lambda key: (columns.get(key) or key).lower()):
            if name in SYSTEM_FIELDS or name == "Title" or name.startswith(("_", "@", "OData")):
                continue
            if columns and name not in columns:
                continue
            value = _plain(fields[name])
            if not value:
                continue
            label = columns.get(name) or name
            if "\n" in value or len(value) > 160:
                long_fields.append((label, value))
            else:
                lines.append(f"- **{label}:** {value}")
        for label, value in long_fields:
            lines.extend(["", f"## {label}", "", value])
        attachments = meta.get("attachments") or []
        if attachments or meta.get("attachments_error"):
            lines.extend(["", "## Attachments", ""])
            # A wiki link, by the attachment's whole path in this source: the
            # resolver reads it as written (a Markdown link's %20 would not
            # match "toner order.txt"), and no other list's item can claim it.
            for path in attachments:
                lines.append(f"- [[{path}]]")
            if not attachments:
                lines.append(f"Attachments could not be read: {meta['attachments_error']}")
        return "\n".join(lines).rstrip() + "\n"

    def checkpoint_from_items(
        self, items: list[ConnectorItem]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        versions = {
            item.identity: str(item.metadata.get("version") or "")
            for item in items
            if item.metadata.get("kind") != "unreachable"
        }
        modified = [str(item.mtime) for item in items if item.mtime]
        return {"versions": versions}, {
            "max_modified": max(modified, default=""),
            "item_count": len(versions),
        }
