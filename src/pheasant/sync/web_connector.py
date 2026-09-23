"""The web-page connector: listed URLs, fetched and kept fresh per page.

Split out of ``connectors.py`` because it had grown its own concerns — which
listed URLs to take, how a served content type decides extraction, and a
per-URL revalidation schedule — and the module budget is right that they read
better as one unit of their own. ``connectors.py`` re-exports the public names.
"""

from __future__ import annotations

import hashlib
import mimetypes
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pheasant.config.schema import (
    DEFAULT_INCLUDES,
    PLACEHOLDER_SOURCE_PATH,
    SourceConfig,
    SourceConnectorSettings,
    SourceType,
)
from pheasant.ingestion.pipeline import _match_any, utc_now, within_max_depth
from pheasant.persistence.state_store import StateStore
from pheasant.security.url_policy import require_public_urls
from pheasant.sync.connectors import (
    FETCHABLE_SCHEMES,
    ConnectorItem,
    ConnectorPayload,
    ConnectorUnavailable,
    ItemNotModified,
    SourceConnector,
    _relative_url_path,
    _urlopen,
    is_fetchable_url,
    logger,
    require_fetchable_url,
)


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


def web_source_for_agent(
    *,
    name: str,
    urls: list[str],
    path: str,
    allow_private: bool,
) -> SourceConfig:
    """A web collection an agent asked for, or a ``ValueError`` saying why not.

    Each refusal is a ``ValueError`` so the MCP boundary forwards its text; a
    ``ConnectorUnavailable`` there reads as a bare "Error executing tool".
    """

    if not urls:
        raise ValueError("a web_collection source needs at least one URL in `urls`")
    for url in urls:
        try:
            require_fetchable_url(url)
        except ConnectorUnavailable as exc:
            raise ValueError(str(exc)) from exc
    if not allow_private:
        require_public_urls(urls)
    return SourceConfig(
        name=name,
        type=SourceType.web_collection,
        path=Path(path.strip() or PLACEHOLDER_SOURCE_PATH),
        urls=urls,
        connector=SourceConnectorSettings(**registration_connector("web_collection", None)),
    )
