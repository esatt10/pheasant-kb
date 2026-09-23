from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from pheasant.config.schema import DEFAULT_INCLUDES, SourceConfig
from pheasant.ingestion.pipeline import _match_any, utc_now, within_max_depth
from pheasant.ingestion.walk import walk_source
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
        from pheasant.ingestion.walk import SyncBudgetExceeded, WalkBudget, budget_message

        budget = WalkBudget.from_settings(self.source.limits)
        report = walk_source(
            root,
            include=self.source.include,
            exclude=self.source.exclude,
            max_depth=self.source.max_depth,
            budget=budget,
            follow_symlinks=bool(getattr(self.source.limits, "follow_symlinks", False)),
        )
        if report.limit_hit:
            raise SyncBudgetExceeded(budget_message(self.source.name, report, budget), report)
        items: list[ConnectorItem] = []
        for path in report.files:
            relative = path.relative_to(root if root.is_dir() else root.parent).as_posix()
            stat = path.stat()
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


def registration_connector(source_type: str, connector: dict[str, Any] | None) -> dict[str, Any]:
    """The connector settings a source registered at runtime starts with.

    Registering a *web collection* — from the UI's "Web pages" form or an
    agent's ``register_source`` — is itself the request to fetch those URLs,
    so it carries the experimental opt-in unless the caller set the flag
    either way. Without this the form saved a source whose first sync was
    refused, and the only fix was hand-writing connector JSON. Every other
    type is returned unchanged: its opt-in stays an explicit act.
    """

    settings = dict(connector or {})
    if source_type == "web_collection":
        settings.setdefault("allow_experimental", True)
    return settings


def _wall_clock() -> float:
    """Wall time, because a page's schedule has to survive a restart.

    A module function so a test can move time instead of sleeping.
    """

    return time.time()


#: A web page's first revalidation interval when its source sets no
#: ``sync.interval_seconds``. One hour: a page being actively edited shows up
#: within the hour, and one that is not backs off from there.
DEFAULT_WEB_REFRESH_SECONDS = 3600


class WebCollectionConnector(SourceConnector):
    connector_type = "web_collection"
    experimental = True

    def __init__(self, source: SourceConfig, state: StateStore):
        super().__init__(source, state)
        self._seen_validators: dict[str, dict[str, Any]] = {}
        self.revalidated = 0
        self.deferred = 0

    def begin_sync(self, mode: str = "incremental") -> None:
        super().begin_sync(mode)
        self._seen_validators = {}
        self.revalidated = 0
        self.deferred = 0

    def list_items(self) -> list[ConnectorItem]:
        github_tree_urls = [
            url
            for url in self.source.urls
            if (urlparse(url).hostname or "").lower().removeprefix("www.") == "github.com"
            and "/tree/" in urlparse(url).path
        ]
        if github_tree_urls:
            raise ConnectorUnavailable(
                f"source {self.source.name} was registered as a web collection, but its URL is "
                "a GitHub repository path. Remove this source and add the GitHub /tree/ URL "
                "again so pheasant can clone the repository and index its subfolder."
            )
        self._require_experimental_enabled()
        items: list[ConnectorItem] = []
        for index, url in enumerate(self.source.urls):
            if not is_fetchable_url(url):
                # Drop it here rather than at read time, so one unfetchable
                # URL (a `file://` that would have read the host filesystem)
                # is a skipped item and not a failed sync for every other URL
                # in the collection.
                logger.warning(
                    "source %s: skipping non-fetchable URL %r (only %s are fetched)",
                    self.source.name,
                    url,
                    "/".join(sorted(FETCHABLE_SCHEMES)),
                )
                continue
            relative = _relative_url_path(url, index)
            if not self._allows_url(relative):
                # Say so: the operator named this URL, and a page dropped
                # without a word reads as a page that was indexed and has
                # nothing in it.
                logger.warning(
                    "source %s: skipping %s - %s does not match the source's include patterns %s",
                    self.source.name,
                    url,
                    relative,
                    self.source.include,
                )
                continue
            items.append(
                ConnectorItem(
                    identity=f"web:{url}",
                    relative_path=relative,
                    uri=url,
                    mime_type=mimetypes.guess_type(urlparse(url).path)[0],
                    metadata={"url": url},
                )
            )
        return items

    def _allows_url(self, relative: str) -> bool:
        """Depth and excludes always apply; ``include`` only when it was chosen.

        The stock ``include`` is code, Markdown and config globs - written for
        a folder walk, where it decides which files to take. A web collection
        has no walk: every URL was listed on purpose, so filtering the list
        through those globs silently dropped every ``.html`` (and ``.pdf``)
        URL while an extensionless one slipped through as ``.txt``. An
        ``include`` the operator actually set still applies.
        """

        if list(self.source.include) != list(DEFAULT_INCLUDES):
            return self._allows_relative_path(relative)
        if not within_max_depth(relative, self.source.max_depth):
            return False
        return not _match_any(relative, self.source.exclude)

    # -- freshness ---------------------------------------------------------
    #
    # The scheduler beat (`sync.scheduler.interval_seconds`, 15 minutes by
    # default) is the same for every source, and a web page is somebody
    # else's server: asking every listed URL on every beat is 96 requests a
    # day per page, most of them for pages that change monthly. So each URL
    # carries its own schedule in the checkpoint. It is checked when it is
    # due and not before; each check that finds it unchanged doubles the wait,
    # up to `connector.max_refresh_seconds`; a change resets it to the minimum,
    # so a page that is being edited is watched closely and one that is not
    # costs a request every few days. A `max-age` the server sends is honoured
    # as a floor. `full` ignores the schedule — it is the "check everything
    # now" switch — and a URL with no history is fetched immediately.

    def _min_refresh(self) -> float:
        configured = self.source.sync.interval_seconds
        return float(DEFAULT_WEB_REFRESH_SECONDS if configured is None else configured)

    def _max_refresh(self) -> float:
        return max(float(self.source.connector.max_refresh_seconds), self._min_refresh())

    def _due(self, cached: dict[str, Any]) -> bool:
        if self.sync_mode != "incremental" or self._min_refresh() <= 0:
            return True
        checked_at = cached.get("checked_at")
        if not isinstance(checked_at, int | float):
            # Written before schedules existed: no evidence of when it was
            # last looked at, so look now and start its schedule.
            return True
        interval = cached.get("interval") or self._min_refresh()
        return _wall_clock() >= float(checked_at) + float(interval)

    def _schedule(
        self, cached: dict[str, Any], *, changed: bool, max_age: float | None
    ) -> dict[str, Any]:
        low, high = self._min_refresh(), self._max_refresh()
        previous = float(cached.get("interval") or low)
        interval = low if changed else min(previous * 2, high)
        if max_age is not None:
            interval = max(interval, min(max_age, high))
        return {"checked_at": _wall_clock(), "interval": max(low, interval)}

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        self._require_experimental_enabled()
        cached = self._cached_validators(item.uri)
        if not self._due(cached):
            # Not due: no request at all. The engine keeps the artifact it
            # already holds, exactly as it does for a 304.
            self._seen_validators[item.uri] = cached
            self.deferred += 1
            raise ItemNotModified(f"not due for revalidation: {item.uri}")
        self.revalidated += 1
        response = _urlopen(
            item.uri,
            headers=self.source.connector.headers,
            timeout=self.source.connector.request_timeout_seconds,
            etag=cached.get("etag"),
            last_modified=cached.get("last_modified"),
        )
        if response.get("not_modified"):
            # Carry validators forward so the next sync stays conditional.
            self._seen_validators[item.uri] = {
                **cached,
                **self._schedule(cached, changed=False, max_age=None),
            }
            raise ItemNotModified(f"not modified (HTTP 304): {item.uri}")
        content = response["content"]
        digest = hashlib.sha256(content).hexdigest()
        changed = cached.get("sha256") != digest
        self._seen_validators[item.uri] = {
            "etag": response.get("etag"),
            "last_modified": response["last_modified"],
            "sha256": digest,
            **self._schedule(
                cached, changed=changed, max_age=_max_age(response.get("headers") or {})
            ),
        }
        return ConnectorPayload(
            item=item,
            content=content,
            mime_type=response["mime_type"] or item.mime_type,
            size_bytes=len(content),
            sha256=digest,
            mtime=response["last_modified"],
            metadata={"url": item.uri, "headers": response["headers"]},
        )

    def checkpoint_from_items(
        self,
        items: list[ConnectorItem],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        urls = [item.uri for item in items]
        previous = (self._previous_cursor or {}).get("validators") or {}
        validators: dict[str, dict[str, Any]] = {}
        for item in items:
            entry = self._seen_validators.get(item.uri) or previous.get(item.uri)
            if entry:
                validators[item.uri] = entry
        return (
            {
                "item_count": len(items),
                "last_url": urls[-1] if urls else None,
                "validators": validators,
            },
            {
                "item_count": len(items),
                "urls": urls,
                "listed_at": utc_now(),
                # What this pass cost the sites it lists: requests made, and
                # URLs left alone because they were not due.
                "revalidated": self.revalidated,
                "deferred": self.deferred,
            },
        )

    def _cached_validators(self, url: str) -> dict[str, Any]:
        validators = (self._previous_cursor or {}).get("validators") or {}
        cached = validators.get(url)
        return cached if isinstance(cached, dict) else {}


def _max_age(headers: dict[str, Any]) -> float | None:
    """``Cache-Control: max-age`` in seconds, or ``None``; ``no-cache`` means 0."""

    value = next((str(v) for k, v in headers.items() if str(k).lower() == "cache-control"), "")
    directives = [part.strip().lower() for part in value.split(",")]
    if any(d in {"no-cache", "no-store"} for d in directives):
        return 0.0
    for directive in directives:
        if directive.startswith("max-age="):
            try:
                return max(0.0, float(directive.split("=", 1)[1]))
            except ValueError:
                return None
    return None


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
