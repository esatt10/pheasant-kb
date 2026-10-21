"""Add pages to a web collection, or take them out, without restating the rest.

``POST /sources`` and MCP ``register_source`` set a source's whole
configuration, its URL list included: right for creating a collection or
rewriting it, and a trap for the commonest edit there is. A caller adding one
page has to send every page already listed, and one that sends only the new
page has replaced the collection with it - the same loss quick add used to
cause by naming a page after its host. This is the edit itself: ``add`` joins
URLs on (existing first, each once, so every page keeps its indexed path) and
``remove`` takes them off.

The change is applied by an ordinary incremental sync. A web collection lists
every URL it holds (``complete_listing``), so a page taken off the list is a
page the sync removes - chunks, vectors and graph nodes - and a page added is
fetched; the pages that stayed are revalidated on their own schedule, not
re-read. On an api replica the sync is published, and the indexer syncs the
collection as registered now (``SourceRegistry.adopt``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest, SourceNotFound

#: The source types whose pages are a URL list.
URL_SOURCE_TYPES = frozenset({"web_collection", "api"})


@dataclass(frozen=True)
class UrlEditRequest:
    """Pages to add to and remove from one source's URL list."""

    source_name: str
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    knowledge_base: str | None = None
    #: Accept loopback and private addresses. An operator over HTTP may index
    #: an intranet page; an agent may not unless the region allows it, because
    #: what it asks to fetch can come from something it read.
    allow_private: bool = True
    sync: bool = True


def edit_urls(context: ServiceContext, request: UrlEditRequest) -> dict[str, Any]:
    """Apply ``add`` and ``remove`` to a source's URLs, register it, and sync it."""

    from pheasant.registry.source_registry import SourceRegistry
    from pheasant.targets import merged_urls

    kb_id = context.knowledge_base(request.knowledge_base)
    source = _source(context, request.source_name)
    kind = getattr(source.type, "value", source.type)
    if kind not in URL_SOURCE_TYPES:
        raise InvalidRequest(
            f"{source.name} is a {kind} source; only a web_collection or api source "
            "holds a URL list"
        )
    add = _urls(request.add)
    remove = _urls(request.remove)
    if not add and not remove:
        raise InvalidRequest("name at least one URL to add or remove")
    _check_fetchable(add, allow_private=request.allow_private)

    current = list(source.urls)
    kept = [url for url in current if url not in remove]
    urls = merged_urls(kept, add)
    if not urls:
        raise InvalidRequest(
            f"that would leave {source.name} with no URLs; remove the source instead"
        )
    result: dict[str, Any] = {
        "knowledge_base": kb_id,
        "source_name": source.name,
        "urls": urls,
        "added": [url for url in add if url not in current],
        "already_listed": [url for url in add if url in current],
        "removed": [url for url in remove if url in current],
        "not_listed": [url for url in remove if url not in current],
        "status": "unchanged",
        "sync": None,
    }
    if urls == current:
        return result
    payload = source.model_dump(mode="json")
    payload["urls"] = urls
    SourceRegistry(context.config, context.state).install(payload)
    result["status"] = "updated"
    indexing = context.indexing
    if request.sync and indexing is not None:
        result["sync"] = indexing.start_sync(source.name)
    return result


def _source(context: ServiceContext, name: str) -> Any:
    """The source as registered now: another replica may have edited it since
    this process last read it, and an edit computed from a stale copy would
    undo that one."""

    from pheasant.registry.source_registry import SourceRegistry

    if not name or context.state.source_removed(name):
        raise SourceNotFound(name or "(no source named)")
    SourceRegistry(context.config, context.state).adopt(name)
    for source in context.config.sources:
        if source.name == name:
            return source
    raise SourceNotFound(name)


def _urls(values: list[str] | None) -> list[str]:
    urls: list[str] = []
    for value in values or []:
        url = str(value or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def _check_fetchable(urls: list[str], *, allow_private: bool) -> None:
    from pheasant.security.url_policy import require_public_urls
    from pheasant.sync.connectors import ConnectorUnavailable, require_fetchable_url

    try:
        for url in urls:
            require_fetchable_url(url)
        if urls and not allow_private:
            require_public_urls(urls)
    except (ConnectorUnavailable, ValueError) as exc:
        raise InvalidRequest(str(exc)) from exc
