"""What a pasted OneDrive or SharePoint link points at, read off its shape alone.

No request is made here: :func:`parse` says which site a URL belongs to and
what inside it the URL names, and :func:`source_name` names the source it
joins. One source is one site (a team site, a communication site, a person's
OneDrive), so a second link into the same site *joins* its source the way a
second page of a website joins its web collection, and the taxonomy view shows
the site as folders: each library, ``Lists/<list>``, ``Pages``.

Shapes, all on ``<tenant>.sharepoint.com`` or ``<tenant>-my.sharepoint.com``:

* ``/`` , ``/sites/<site>``, ``/teams/<site>``, ``/personal/<user>`` -- a whole
  site: every library, every list and every page. So is its *Site contents*
  page (``/_layouts/15/viewlsts.aspx``) and its home page.
* ``<site>/<library>[/<folder>...][/<file>]`` -- a library, a folder or a
  file; ``Forms/AllItems.aspx?id=<server-relative path>`` names the folder a
  library view is open on.
* ``<site>/Lists/<list>[/...]`` -- one list, its items and their attachments.
* ``<site>/SitePages`` -- every page; ``<site>/SitePages/<page>.aspx`` one.
* ``/:f:/s/...``, ``/:w:/g/...`` (a *sharing* link) -- resolved by Microsoft
  Graph's shares API, whatever it names. ``/:x:/r/<path>`` is a direct link and
  is read as its path.

``1drv.ms`` and ``onedrive.live.com`` (a personal Microsoft account) are
always sharing links.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

CONSUMER_HOSTS = ("1drv.ms", "onedrive.live.com", "onedrive.com")
SITE_ROOTS = ("sites", "teams", "personal")
#: ``/_layouts/15/...`` pages that show the whole site.
SITE_WIDE_LAYOUTS = ("viewlsts.aspx", "start.aspx")


@dataclass(frozen=True)
class Target:
    """One URL's meaning: the site, and what in it.

    ``kind`` is ``site`` (everything), ``drive_path`` (a library, folder or
    file: ``path`` is its server-relative path), ``list`` (``name``),
    ``pages`` (every page), ``page`` (``name``) or ``share`` (a link only the
    shares API can resolve).
    """

    url: str
    kind: str
    host: str
    #: The site's server-relative path ("" for the tenant's root site).
    site: str = ""
    path: str = ""
    name: str = ""

    @property
    def site_url(self) -> str:
        return f"https://{self.host}{self.site}"


def is_microsoft_url(url: str) -> bool:
    """Whether ``url`` is OneDrive or SharePoint."""

    host = (urlparse(url.strip()).hostname or "").lower()
    if host in CONSUMER_HOSTS or host.endswith(".onedrive.live.com"):
        return True
    return host.endswith(".sharepoint.com") or host.endswith(".sharepoint.us")


def _segments(path: str) -> list[str]:
    return [unquote(part) for part in path.split("/") if part]


def _site_of(parts: list[str]) -> tuple[str, list[str]]:
    """The site's server-relative path and what follows it."""

    if len(parts) >= 2 and parts[0].lower() in SITE_ROOTS:
        return f"/{parts[0]}/{parts[1]}", parts[2:]
    return "", parts


def parse(url: str) -> Target:
    """What ``url`` names. Refuses a URL that is not OneDrive or SharePoint."""

    url = url.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not is_microsoft_url(url):
        raise ValueError(f"not a OneDrive or SharePoint link: {url}")
    if host in CONSUMER_HOSTS or host.endswith(".onedrive.live.com"):
        return Target(url, "share", host)
    parts = _segments(parsed.path)
    # A sharing link: /:f:/s/<site>/<token>, /:w:/g/personal/<user>/<token>,
    # and the direct-path form /:x:/r/<server-relative path>.
    if parts and re.fullmatch(r":[a-z]:", parts[0]):
        if len(parts) > 2 and parts[1] == "r":
            parts = parts[2:]
        else:
            if len(parts) > 2 and parts[1] in ("s", "t"):
                site = f"/{'sites' if parts[1] == 's' else 'teams'}/{parts[2]}"
            elif len(parts) > 3 and parts[1] == "g" and parts[2].lower() in SITE_ROOTS:
                site = f"/{parts[2]}/{parts[3]}"
            else:
                site = ""
            return Target(url, "share", host, site=site)
    site, rest = _site_of(parts)
    lowered = [part.lower() for part in rest]
    if not rest:
        return Target(url, "site", host, site=site)
    if lowered[0] == "_layouts":
        # Site contents, the site's start page, or a document opened in the
        # browser (Doc.aspx?sourcedoc=...), which only the shares API can name.
        if lowered[-1] in SITE_WIDE_LAYOUTS:
            return Target(url, "site", host, site=site)
        return Target(url, "share", host, site=site)
    if lowered[0] == "lists" and len(rest) > 1:
        return Target(url, "list", host, site=site, name=rest[1])
    if lowered[0] == "sitepages":
        if len(rest) == 2 and lowered[1] == "home.aspx":
            # The site's front page: what someone pastes to mean the site.
            return Target(url, "site", host, site=site)
        if len(rest) == 1 or lowered[1] == "forms":
            return Target(url, "pages", host, site=site)
        return Target(url, "page", host, site=site, name="/".join(rest[1:]))
    # A library view open on a folder names it in ?id=<server-relative path>.
    if "forms" in lowered:
        opened = parse_qs(parsed.query).get("id") or parse_qs(parsed.query).get("RootFolder")
        if opened:
            return Target(url, "drive_path", host, site=site, path=unquote(opened[0]))
        library = rest[: lowered.index("forms")]
        return Target(url, "drive_path", host, site=site, path=f"{site}/{'/'.join(library)}")
    return Target(url, "drive_path", host, site=site, path=f"{site}/{'/'.join(rest)}")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def source_name(url: str) -> str:
    """The source a link joins: one per site.

    ``sharepoint-<tenant>[-<site>]`` for a SharePoint site,
    ``onedrive-<tenant>-<user>`` for a person's OneDrive for work or school,
    and ``onedrive`` for a personal Microsoft account's.
    """

    target = parse(url)
    if target.host in CONSUMER_HOSTS or target.host.endswith(".onedrive.live.com"):
        return "onedrive"
    tenant = target.host.split(".", 1)[0]
    personal = tenant.endswith("-my")
    tenant = tenant.removesuffix("-my")
    site = _slug(target.site.rsplit("/", 1)[-1]) if target.site else ""
    if personal or target.site.lower().startswith("/personal/"):
        return "-".join(part for part in ("onedrive", _slug(tenant), site) if part)
    return "-".join(part for part in ("sharepoint", _slug(tenant), site) if part)


def sharepoint_target(url: str, workspace: Path, *, name: str | None) -> Any:
    """The source entry for a pasted link (``pheasant.targets.ResolvedTarget``).

    Named after the site, so a second link into it joins this source and the
    taxonomy shows the site as folders whatever was pasted first; ``include``
    is every format the pipeline reads, because a library is mostly Office
    files and PDFs, which the stock code-and-Markdown globs would drop.
    """

    from pheasant.targets import ResolvedTarget

    label = name or source_name(url)
    return ResolvedTarget(
        name=label,
        type="sharepoint",
        path=str((workspace / "sharepoint" / label).resolve()),
        description=f"OneDrive / SharePoint content from {url}",
        include=["**/*"],
        urls=[url],
        local=False,
    )
