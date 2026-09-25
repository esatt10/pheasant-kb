from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

from pheasant.config.schema import SourceConfig
from pheasant.ingestion.pipeline import _match_any, utc_now, within_max_depth
from pheasant.ingestion.walk import WalkBudget, walk_source
from pheasant.persistence.state_store import StateStore

logger = logging.getLogger(__name__)


class ConnectorUnavailable(RuntimeError):
    """Raised when a connector is configured but cannot run in this environment."""


class ItemNotModified(RuntimeError):
    """Raised by ``read_item`` when validators report an unchanged remote item.

    The sync engine treats this as a skip: no body was transferred, the
    persisted artifact stays untouched (Synapse step 21.3).
    """


@dataclass(frozen=True)
class ConnectorItem:
    identity: str
    relative_path: str
    uri: str
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    mtime: str | None = None
    etag: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorPayload:
    item: ConnectorItem
    content: bytes
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    mtime: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorHealth:
    ok: bool
    status: str
    item_count: int
    checked_items: int
    errors: list[str] = field(default_factory=list)
    checkpoint: dict[str, Any] | None = None


class SourceConnector(ABC):
    connector_type = "base"
    experimental = False

    def __init__(self, source: SourceConfig, state: StateStore):
        self.source = source
        self.state = state
        self.sync_mode: str | None = None
        self._previous_cursor: dict[str, Any] | None = None
        self._previous_watermark: dict[str, Any] | None = None

    def begin_sync(self, mode: str = "incremental") -> None:
        """Prepare one sync pass: load the previous checkpoint (Synapse 21.3).

        Connectors only consult their checkpoint in ``incremental`` mode —
        ``full`` and ``repair`` always re-fetch. Checkpoints written before
        21.3 simply lack the newer cursor fields and degrade gracefully to
        "no validator cached", i.e. a normal fetch.
        """
        self.sync_mode = mode
        self._previous_cursor = None
        self._previous_watermark = None
        if mode != "incremental":
            return
        checkpoint = self.get_checkpoint()
        if checkpoint:
            self._previous_cursor = checkpoint.get("cursor") or {}
            self._previous_watermark = checkpoint.get("high_watermark") or {}

    @abstractmethod
    def list_items(self) -> list[ConnectorItem]:
        """Return source items that may be indexed."""

    @abstractmethod
    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        """Read one item payload."""

    def get_checkpoint(self) -> dict[str, Any] | None:
        return self.state.get_source_checkpoint(self.source.name)

    def set_checkpoint(
        self,
        cursor: dict[str, Any],
        high_watermark: dict[str, Any],
        status: str = "healthy",
    ) -> None:
        self.state.set_source_checkpoint(
            self.source.name,
            self.connector_type,
            cursor,
            high_watermark,
            utc_now(),
            status,
        )

    def resolve_identity(self, item: ConnectorItem) -> str:
        return item.identity

    def validate(self) -> ConnectorHealth:
        errors: list[str] = []
        try:
            items = self.list_items()
        except Exception as exc:
            return ConnectorHealth(
                ok=False,
                status="unhealthy",
                item_count=0,
                checked_items=0,
                errors=[str(exc)],
                checkpoint=self.get_checkpoint(),
            )
        checked = 0
        for item in items[:20]:
            try:
                self.read_item(item)
                checked += 1
            except Exception as exc:
                errors.append(f"{item.relative_path}: {exc}")
        return ConnectorHealth(
            ok=not errors,
            status="healthy" if not errors else "unhealthy",
            item_count=len(items),
            checked_items=checked,
            errors=errors,
            checkpoint=self.get_checkpoint(),
        )

    def checkpoint_from_items(
        self,
        items: list[ConnectorItem],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        identities = [item.identity for item in items]
        mtimes = [item.mtime for item in items if item.mtime]
        cursor = {
            "item_count": len(items),
            "last_identity": identities[-1] if identities else None,
        }
        high_watermark = {
            "item_count": len(items),
            "max_mtime": max(mtimes) if mtimes else None,
            "listed_at": utc_now(),
        }
        return cursor, high_watermark

    def _require_experimental_enabled(self) -> None:
        if self.experimental and not self.source.connector.allow_experimental:
            raise ConnectorUnavailable(
                f"{self.connector_type} connector for source {self.source.name} is experimental. "
                "Set sources[].connector.allow_experimental=true to enable it."
            )

    def _allows_relative_path(self, relative_path: str) -> bool:
        if not within_max_depth(relative_path, self.source.max_depth):
            return False
        if _match_any(relative_path, self.source.exclude):
            return False
        return not self.source.include or _match_any(relative_path, self.source.include)


class FilesystemConnector(SourceConnector):
    connector_type = "filesystem"

    def list_items(self) -> list[ConnectorItem]:
        root = self.source.path
        if not root.exists():
            return []
        # Pruning, budgeted walk (see ingestion/walk.py). The previous
        # `rglob("*")` materialized the whole tree before applying excludes,
        # so excluding node_modules cost more than not excluding it — and a
        # source pointed at a home directory had no upper bound at all.
        from pheasant.ingestion.walk import SyncBudgetExceeded, budget_message

        budget = WalkBudget.from_settings(self.source.limits)
        # The include globs select members, not just the ZIP container. Let
        # the walk discover archives even when only inner extensions match.
        include = [*self.source.include, "**/*.zip", "**/*.ZIP"]
        report = walk_source(
            root,
            include=include,
            exclude=self.source.exclude,
            max_depth=self.source.max_depth,
            budget=budget,
            follow_symlinks=bool(getattr(self.source.limits, "follow_symlinks", False)),
        )
        if report.limit_hit:
            raise SyncBudgetExceeded(budget_message(self.source.name, report, budget), report)
        items: list[ConnectorItem] = []
        expanded_total = report.total_bytes
        for path in report.files:
            relative = path.relative_to(root if root.is_dir() else root.parent).as_posix()
            stat = path.stat()
            if path.suffix.lower() == ".zip":
                from pheasant.sync.zip_archive import MAX_ARCHIVE_MEMBER_BYTES, members

                archive_selected = _match_any(
                    relative.lower(), [pattern.lower() for pattern in self.source.include]
                )
                for member_name, info in members(path):
                    virtual = f"{relative}/{member_name}"
                    if not within_max_depth(virtual, self.source.max_depth):
                        continue
                    if _match_any(virtual, self.source.exclude) or _match_any(
                        member_name, self.source.exclude
                    ):
                        continue
                    if self.source.include and not (
                        archive_selected
                        or _match_any(virtual, self.source.include)
                        or _match_any(member_name, self.source.include)
                    ):
                        continue
                    max_bytes = min(
                        budget.max_file_size_bytes or MAX_ARCHIVE_MEMBER_BYTES,
                        MAX_ARCHIVE_MEMBER_BYTES,
                    )
                    if info.file_size > max_bytes:
                        report.oversized.append((virtual, info.file_size))
                        continue
                    if budget.max_files is not None and len(items) >= budget.max_files:
                        report.limit_hit = "max_files"
                        raise SyncBudgetExceeded(
                            budget_message(self.source.name, report, budget), report
                        )
                    if (
                        budget.max_total_bytes is not None
                        and expanded_total + info.file_size > budget.max_total_bytes
                    ):
                        report.limit_hit = "max_total_mb"
                        report.total_bytes = expanded_total
                        raise SyncBudgetExceeded(
                            budget_message(self.source.name, report, budget), report
                        )
                    expanded_total += info.file_size
                    items.append(
                        ConnectorItem(
                            identity=f"filesystem:{self.source.name}:{virtual}",
                            relative_path=virtual,
                            uri=f"{_path_uri(path)}#{quote(member_name, safe='/')}",
                            mime_type=mimetypes.guess_type(member_name)[0],
                            size_bytes=info.file_size,
                            mtime=_timestamp(stat.st_mtime),
                            metadata={
                                "archive_path": str(path),
                                "archive_member": member_name,
                                "archive_entry_name": info.filename,
                            },
                        )
                    )
                continue
            if not self._allows_relative_path(relative):
                continue
            if budget.max_files is not None and len(items) >= budget.max_files:
                report.limit_hit = "max_files"
                raise SyncBudgetExceeded(budget_message(self.source.name, report, budget), report)
            items.append(
                ConnectorItem(
                    identity=f"filesystem:{self.source.name}:{relative}",
                    relative_path=relative,
                    uri=_path_uri(path),
                    mime_type=mimetypes.guess_type(path.name)[0],
                    size_bytes=stat.st_size,
                    mtime=_timestamp(stat.st_mtime),
                    metadata={"path": str(path)},
                )
            )
        return items

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        if "archive_member" in item.metadata:
            from pheasant.sync.zip_archive import MAX_ARCHIVE_MEMBER_BYTES, read_member

            limit = WalkBudget.from_settings(self.source.limits).max_file_size_bytes
            content = read_member(
                Path(item.metadata["archive_path"]),
                item.metadata.get("archive_entry_name", item.metadata["archive_member"]),
                min(limit or MAX_ARCHIVE_MEMBER_BYTES, MAX_ARCHIVE_MEMBER_BYTES),
            )
            return ConnectorPayload(
                item=item,
                content=content,
                mime_type=item.mime_type,
                size_bytes=len(content),
                mtime=item.mtime,
            )
        path = Path(item.metadata["path"])
        content = path.read_bytes()
        return ConnectorPayload(
            item=item,
            content=content,
            mime_type=item.mime_type,
            size_bytes=item.size_bytes,
            sha256=item.sha256,
            mtime=item.mtime,
            metadata={"path": str(path)},
        )

    def validate(self) -> ConnectorHealth:
        if not self.source.path.exists():
            return ConnectorHealth(
                ok=False,
                status="unhealthy",
                item_count=0,
                checked_items=0,
                errors=[f"path does not exist: {self.source.path}"],
                checkpoint=self.get_checkpoint(),
            )
        return super().validate()


class APIConnector(SourceConnector):
    connector_type = "api"
    experimental = True

    def __init__(self, source: SourceConfig, state: StateStore):
        super().__init__(source, state)
        self._read_hashes: dict[str, str] = {}

    def begin_sync(self, mode: str = "incremental") -> None:
        super().begin_sync(mode)
        self._read_hashes = {}

    def list_items(self) -> list[ConnectorItem]:
        self._require_experimental_enabled()
        endpoint = self.source.connector.api_endpoint or (
            self.source.urls[0] if self.source.urls else None
        )
        if not endpoint:
            raise ConnectorUnavailable(
                f"api connector for source {self.source.name} requires "
                "connector.api_endpoint or urls[0]"
            )
        response = _urlopen(
            endpoint,
            headers=self.source.connector.headers,
            timeout=self.source.connector.request_timeout_seconds,
        )
        payload = json.loads(response["content"].decode("utf-8"))
        raw_items = payload.get(self.source.connector.api_items_field, payload)
        if not isinstance(raw_items, list):
            raise ConnectorUnavailable(
                "API item listing must be a JSON list or contain a list field"
            )
        items: list[ConnectorItem] = []
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                continue
            item_id = str(raw.get("id") or raw.get("path") or raw.get("url") or index)
            item_url = raw.get("url") or raw.get("href")
            relative = str(raw.get("path") or raw.get("name") or item_id)
            relative = _ensure_text_suffix(_safe_path(relative))
            if not self._allows_relative_path(relative):
                continue
            uri = str(item_url or f"api://{self.source.name}/{item_id}")
            identity = f"api:{self.source.name}:{item_id}"
            mtime = raw.get("updated_at") or raw.get("mtime")
            sha256 = raw.get("sha256")
            content_value = raw.get(self.source.connector.api_content_field)
            if sha256 is None and content_value is not None:
                # Inline content already arrived with the listing — hashing it
                # here is free and lets the engine skip before "reading".
                sha256 = hashlib.sha256(str(content_value).encode("utf-8")).hexdigest()
            if sha256 is None:
                sha256 = self._cached_sha256(identity, mtime)
            items.append(
                ConnectorItem(
                    identity=identity,
                    relative_path=relative,
                    uri=uri,
                    mime_type=raw.get("mime_type"),
                    sha256=sha256,
                    mtime=mtime,
                    metadata=raw,
                )
            )
        return items

    def _cached_sha256(self, identity: str, mtime: str | None) -> str | None:
        """Reuse the content hash from the checkpoint cursor when the item's
        last-modified marker is unchanged (Synapse 21.3 cursor consultation)."""
        if not mtime:
            return None
        cached = ((self._previous_cursor or {}).get("items") or {}).get(identity)
        if isinstance(cached, dict) and cached.get("mtime") == mtime:
            return cached.get("sha256")
        return None

    def checkpoint_from_items(
        self,
        items: list[ConnectorItem],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        cursor, high_watermark = super().checkpoint_from_items(items)
        item_state: dict[str, dict[str, Any]] = {}
        for item in items:
            sha256 = item.sha256 or self._read_hashes.get(item.identity)
            if sha256:
                item_state[item.identity] = {"sha256": sha256, "mtime": item.mtime}
        cursor["items"] = item_state
        return cursor, high_watermark

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        self._require_experimental_enabled()
        content_value = item.metadata.get(self.source.connector.api_content_field)
        if content_value is not None:
            content = str(content_value).encode("utf-8")
            digest = hashlib.sha256(content).hexdigest()
            self._read_hashes[item.identity] = digest
            return ConnectorPayload(
                item=item,
                content=content,
                mime_type=item.mime_type or "text/plain",
                size_bytes=len(content),
                sha256=digest,
                mtime=item.mtime,
                metadata=item.metadata,
            )
        if not item.uri.startswith(("http://", "https://")):
            raise ConnectorUnavailable(
                f"API item {item.identity} has no readable URL or content field"
            )
        response = _urlopen(
            item.uri,
            headers=self.source.connector.headers,
            timeout=self.source.connector.request_timeout_seconds,
        )
        content = response["content"]
        digest = hashlib.sha256(content).hexdigest()
        self._read_hashes[item.identity] = digest
        return ConnectorPayload(
            item=item,
            content=content,
            mime_type=response["mime_type"] or item.mime_type,
            size_bytes=len(content),
            sha256=digest,
            mtime=response["last_modified"] or item.mtime,
            metadata=item.metadata,
        )


class S3Connector(SourceConnector):
    connector_type = "s3"
    experimental = True

    def __init__(self, source: SourceConfig, state: StateStore):
        super().__init__(source, state)
        self._read_hashes: dict[str, str] = {}

    def begin_sync(self, mode: str = "incremental") -> None:
        super().begin_sync(mode)
        self._read_hashes = {}

    def list_items(self) -> list[ConnectorItem]:
        self._require_experimental_enabled()
        client = _boto3_client()
        bucket = self.source.connector.s3_bucket
        prefix = self.source.connector.s3_prefix or ""
        if not bucket:
            raise ConnectorUnavailable(
                f"s3 connector for source {self.source.name} requires connector.s3_bucket"
            )
        items: list[ConnectorItem] = []
        continuation: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if continuation:
                kwargs["ContinuationToken"] = continuation
            response = client.list_objects_v2(**kwargs)
            for obj in response.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                relative = key[len(prefix) :].lstrip("/") if key.startswith(prefix) else key
                if _match_any(relative, self.source.exclude):
                    continue
                if self.source.include and not _match_any(relative, self.source.include):
                    continue
                last_modified = obj.get("LastModified")
                item = ConnectorItem(
                    identity=f"s3:{bucket}:{key}",
                    relative_path=relative,
                    uri=f"s3://{bucket}/{key}",
                    mime_type=mimetypes.guess_type(key)[0],
                    size_bytes=obj.get("Size"),
                    sha256=None,
                    mtime=last_modified.isoformat().replace("+00:00", "Z")
                    if last_modified
                    else None,
                    etag=(obj.get("ETag") or "").strip('"') or None,
                    metadata={"bucket": bucket, "key": key},
                )
                cached_sha256 = self._cached_object_sha256(item)
                if cached_sha256:
                    item = replace(item, sha256=cached_sha256)
                items.append(item)
            if not response.get("IsTruncated"):
                break
            continuation = response.get("NextContinuationToken")
        return items

    def _cached_object_sha256(self, item: ConnectorItem) -> str | None:
        """Objects at-or-before the checkpoint high-watermark (LastModified)
        with unchanged ETag/size reuse the cached content hash, letting the
        engine skip them without ``get_object`` (Synapse 21.3)."""
        watermark = (self._previous_watermark or {}).get("max_mtime")
        if not watermark or not item.mtime or item.mtime > watermark:
            return None
        cached = ((self._previous_cursor or {}).get("objects") or {}).get(item.identity)
        if not isinstance(cached, dict):
            return None
        if cached.get("etag") != item.etag or cached.get("size_bytes") != item.size_bytes:
            return None
        return cached.get("sha256")

    def checkpoint_from_items(
        self,
        items: list[ConnectorItem],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        cursor, high_watermark = super().checkpoint_from_items(items)
        objects: dict[str, dict[str, Any]] = {}
        for item in items:
            sha256 = item.sha256 or self._read_hashes.get(item.identity)
            if sha256:
                objects[item.identity] = {
                    "etag": item.etag,
                    "size_bytes": item.size_bytes,
                    "mtime": item.mtime,
                    "sha256": sha256,
                }
        cursor["objects"] = objects
        return cursor, high_watermark

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        self._require_experimental_enabled()
        client = _boto3_client()
        response = client.get_object(Bucket=item.metadata["bucket"], Key=item.metadata["key"])
        content = response["Body"].read()
        digest = hashlib.sha256(content).hexdigest()
        self._read_hashes[item.identity] = digest
        return ConnectorPayload(
            item=item,
            content=content,
            mime_type=response.get("ContentType") or item.mime_type,
            size_bytes=len(content),
            sha256=digest,
            mtime=item.mtime,
            metadata=item.metadata,
        )


def connector_for_source(source: SourceConfig, state: StateStore) -> SourceConnector:
    if source.connector.runtime == "sandboxed":
        # Synapse Step 34.1+: opt-in per source, checked before the
        # source.type dispatch below so it takes precedence. Default
        # "native" leaves every existing source byte-identical to pre-34.1.
        from pheasant.sandbox.connector import SandboxedConnector

        return SandboxedConnector(source, state)
    if source.type.value in {
        "repository",
        "markdown_folder",
        "obsidian_vault",
        "document_folder",
        "single_file",
        "memory",
    }:
        return FilesystemConnector(source, state)
    if source.type.value == "web_collection":
        from pheasant.sync.web_connector import WebCollectionConnector

        return WebCollectionConnector(source, state)
    if source.type.value == "api":
        return APIConnector(source, state)
    if source.type.value == "s3":
        return S3Connector(source, state)
    from pheasant.sync.connector_registry import get_connector_class, list_connector_types

    plugin_class = get_connector_class(source.type.value)
    if plugin_class is not None:
        return plugin_class(source, state)
    installed = ", ".join(list_connector_types()) or "none"
    raise ConnectorUnavailable(
        f"No connector registered for source type: {source.type.value} "
        f"(installed connector plugins: {installed})"
    )


def _timestamp(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _path_uri(path: Path) -> str:
    try:
        return path.resolve().as_uri()
    except ValueError:
        return str(path)


#: Schemes the web/API connectors may fetch. ``urlopen`` also speaks
#: ``file://`` and ``ftp://``; leaving those reachable turns a "web
#: collection" source into an arbitrary local-file reader that indexes
#: whatever it finds (``file:///proc/self/environ``, private keys) and
#: serves it straight back out of ``/search``. Fetching remote documents is
#: the feature; reading the host filesystem through the same door is not —
#: local content has its own connector, with its own path policy.
FETCHABLE_SCHEMES = frozenset({"http", "https"})


def is_fetchable_url(url: str) -> bool:
    """Whether the connectors may fetch ``url`` at all."""
    return urlparse(url).scheme.lower() in FETCHABLE_SCHEMES


def require_fetchable_url(url: str) -> str:
    """Reject any URL whose scheme the connectors must not fetch."""
    scheme = urlparse(url).scheme.lower()
    if not is_fetchable_url(url):
        raise ConnectorUnavailable(
            f"refusing to fetch {url!r}: only "
            f"{'/'.join(sorted(FETCHABLE_SCHEMES))} URLs may be fetched "
            f"(got scheme {scheme or 'none'!r}). Index local content with a "
            f"filesystem source instead."
        )
    return url


def _urlopen(
    url: str,
    headers: dict[str, str],
    timeout: int,
    etag: str | None = None,
    last_modified: str | None = None,
) -> dict[str, Any]:
    require_fetchable_url(url)
    request_headers = {"User-Agent": "pheasant/0.1"}
    request_headers.update(headers)
    if etag:
        request_headers["If-None-Match"] = etag
    if last_modified:
        request_headers["If-Modified-Since"] = last_modified
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            content = response.read()
            info = response.info()
            mime_type = info.get_content_type() if hasattr(info, "get_content_type") else None
            return {
                "not_modified": False,
                "content": content,
                "mime_type": mime_type,
                "etag": info.get("ETag"),
                "last_modified": info.get("Last-Modified"),
                "headers": dict(info.items()),
            }
    except HTTPError as exc:
        if exc.code == 304 and (etag or last_modified):
            exc.close()
            return {
                "not_modified": True,
                "content": b"",
                "mime_type": None,
                "etag": etag,
                "last_modified": last_modified,
                "headers": {},
            }
        raise


def _relative_url_path(url: str, index: int) -> str:
    parsed = urlparse(url)
    host = _safe_segment(parsed.netloc or parsed.scheme or "url")
    path = unquote(parsed.path or "").strip("/")
    if not path:
        path = f"index-{index}.html"
    relative = f"{host}/{_safe_path(path)}"
    return _ensure_text_suffix(relative)


def _safe_segment(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return sanitized.strip("._") or "item"


def _safe_path(value: str) -> str:
    return "/".join(_safe_segment(part) for part in value.replace("\\", "/").split("/") if part)


def _ensure_text_suffix(relative: str) -> str:
    if Path(relative).suffix:
        return relative
    return f"{relative}.txt"


def _boto3_client() -> Any:
    try:
        import boto3
    except ModuleNotFoundError as exc:
        raise ConnectorUnavailable("S3 connector requires boto3 to be installed") from exc
    return boto3.client("s3")
