"""Pages whose server sends a script instead of the page, read from the site's API.

A web collection indexes what a URL's server returns. For a single-page
application that is a shell -- a ``<div>``, a bundle of JavaScript and a
``<noscript>`` asking for JavaScript -- and the documentation a browser shows
is fetched afterwards, by that script, from a JSON API. pheasant runs no page
scripts (an indexer that executes somebody else's JavaScript is a browser, with
a browser's attack surface and none of its determinism), so such a page indexed
as "Please enable Javascript to use this application" and nothing else. Found
on the Terraform Registry: every provider doc page is one of these.

A *site adapter* answers for one site whose API serves the same document the
page renders. It is chosen by URL alone, before anything is fetched, so the
artifact's path can carry the format the adapter returns (``.md`` -- chunked
by heading, shown in the Markdown view) while its URL, and the "web page" link
the viewer draws, stay the page a person would open. An adapter that cannot
find the document raises, which the engine records as that item's read error
and keeps whatever was indexed before: falling back to the shell would index
the one thing this module exists to stop indexing.

Sites with no adapter are unchanged, except that a page that looks like a
script shell is now *said* to be one (``html_text.looks_like_script_shell``)
instead of indexing silently as a page with something in it.

Adding a site is one class satisfying ``SiteAdapter`` and one entry in
``SITE_ADAPTERS``; ``docs/how-to/site-adapters.md`` walks through it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import quote, urlparse

from pheasant.sync.connectors import logger

#: Bump when an adapter changes what text a page produces. Part of the
#: fingerprint of a collection that lists an adapted URL (and of no other),
#: so a bump re-reads exactly those collections once.
WEB_ADAPTERS_VERSION = 1

#: ``fetch(url) -> response`` in ``connectors._urlopen``'s shape. The
#: connector supplies it with the source's headers and timeout bound.
Fetch = Callable[[str], dict[str, Any]]


class SiteAdapter(Protocol):
    """What the web connector asks of an adapter. ``TerraformRegistryAdapter`` is the example.

    ``matches`` decides by URL alone, before any request, and must be cheap
    and pure: it runs at listing time, and the artifact path is built from
    ``suffix`` then. ``fetch`` returns a response in ``connectors._urlopen``'s
    shape with ``content`` holding the document itself, plus ``adapter`` and
    ``api_url``; it makes its requests only through ``fetch`` (the source's
    headers and timeout, and the seam the offline tests stub), may keep
    anything shared between pages in ``cache`` (emptied every sync), and
    raises ``SiteAdapterError`` when the URL names nothing the site has.
    """

    name: str
    suffix: str
    mime_type: str

    def matches(self, url: str) -> bool: ...

    def fetch(self, url: str, fetch: Fetch, cache: dict[str, Any]) -> dict[str, Any]: ...


class SiteAdapterError(RuntimeError):
    """An adapter could not find the document a URL names.

    Deliberately not ``ConnectorUnavailable``: that fails the whole source,
    and one mistyped page is a fact about that page.
    """


class TerraformRegistryAdapter:
    """``registry.terraform.io`` provider docs, from the registry's public API.

    ``/providers/{ns}/{name}/{version}/docs/{category}/{slug}`` is an Ember
    route. ``/v1/providers/{ns}/{name}[/{version}]`` lists that version's doc
    pages (id, category, slug), and ``/v2/provider-docs/{id}`` returns one
    page's Markdown source -- the file the provider publishes, which is what
    the page renders. ``latest`` resolves to the version the registry calls
    latest *now*, so a page follows new provider releases the way its URL
    does in a browser. ``/docs`` alone is the provider's overview.
    """

    name = "terraform-registry"
    suffix = ".md"
    mime_type = "text/markdown"
    hosts = frozenset({"registry.terraform.io"})
    api_base = "https://registry.terraform.io"
    _route = re.compile(
        r"^/providers/(?P<ns>[^/]+)/(?P<name>[^/]+)/(?P<version>[^/]+)/docs"
        r"(?:/(?P<category>[^/]+)/(?P<slug>[^/]+))?/?$"
    )

    def _parts(self, url: str) -> dict[str, str] | None:
        parsed = urlparse(url)
        if (parsed.hostname or "").lower() not in self.hosts:
            return None
        found = self._route.match(parsed.path)
        return found.groupdict() if found else None

    def matches(self, url: str) -> bool:
        return self._parts(url) is not None

    def fetch(self, url: str, fetch: Fetch, cache: dict[str, Any]) -> dict[str, Any]:
        parts = self._parts(url)
        if parts is None:  # pragma: no cover - callers ask `matches` first
            raise SiteAdapterError(f"not a Terraform Registry docs URL: {url}")
        provider = f"{quote(parts['ns'], safe='')}/{quote(parts['name'], safe='')}"
        version = parts["version"]
        listing_url = f"{self.api_base}/v1/providers/{provider}"
        if version != "latest":
            listing_url += f"/{quote(version, safe='')}"
        # One provider's listing is every doc page it has (~400 KB for
        # azurerm), and a collection usually lists several pages of one
        # provider: read it once per sync.
        listing = cache.get(listing_url)
        if listing is None:
            listing = _json(fetch(listing_url), listing_url)
            cache[listing_url] = listing
        category = parts["category"] or "overview"
        slug = parts["slug"] or "index"
        candidates = [
            doc
            for doc in listing.get("docs") or []
            if doc.get("category") == category and doc.get("slug") == slug
        ]
        if not candidates:
            raise SiteAdapterError(
                f"the Terraform Registry lists no {category}/{slug} page for "
                f"{parts['ns']}/{parts['name']} {listing.get('version') or version}: {url}"
            )
        # A page published in several languages (CDKTF) has one entry per
        # language; HCL is the one the URL shows by default.
        doc = next((d for d in candidates if d.get("language") == "hcl"), candidates[0])
        doc_url = f"{self.api_base}/v2/provider-docs/{quote(str(doc['id']), safe='')}"
        response = fetch(doc_url)
        attributes = (_json(response, doc_url).get("data") or {}).get("attributes") or {}
        content = attributes.get("content")
        if not isinstance(content, str) or not content.strip():
            raise SiteAdapterError(f"the Terraform Registry returned no content for {url}")
        if attributes.get("truncated"):
            logger.warning("Terraform Registry: %s is truncated in the registry's API", url)
        return {
            "not_modified": False,
            "content": content.encode("utf-8"),
            "mime_type": self.mime_type,
            # The API sends no validators; the sha256 decides "changed".
            "etag": None,
            "last_modified": None,
            "headers": response.get("headers") or {},
            "adapter": self.name,
            "api_url": doc_url,
            "version": listing.get("version"),
        }


def _json(response: dict[str, Any], url: str) -> dict[str, Any]:
    try:
        parsed = json.loads(response.get("content") or b"")
    except ValueError as exc:
        raise SiteAdapterError(f"{url} did not return JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SiteAdapterError(f"{url} returned JSON that is not an object")
    return parsed


#: Every adapter, tried in order; the first that matches a URL answers for it.
SITE_ADAPTERS: tuple[SiteAdapter, ...] = (TerraformRegistryAdapter(),)


def adapter_for(url: str) -> SiteAdapter | None:
    return next((adapter for adapter in SITE_ADAPTERS if adapter.matches(url)), None)


def adapted_names(urls: list[str]) -> list[str]:
    """The adapters a URL list uses, sorted: what a collection's fingerprint carries."""

    return sorted({adapter.name for url in urls if (adapter := adapter_for(url)) is not None})
