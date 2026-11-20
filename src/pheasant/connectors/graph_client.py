"""A read-only Microsoft Graph v1.0 client for OneDrive and SharePoint.

What the SharePoint connector reads, one function per resource: a sharing link
(the shares API), a site by its path, its document libraries (drives), a
folder's children (walked, shared folders from another drive included), its
lists and their items with fields, its modern pages with their canvas, and a
file's bytes. List item *attachments* are SharePoint REST, not Graph: Graph
v1.0 has no attachment API, so those two calls take a token whose audience is
the SharePoint host (``connections.microsoft.sharepoint_token``).

Every request goes through :func:`request`, module-level so tests replace it.
It retries throttling (429 and 503) after ``Retry-After``, else with an
exponential backoff from 2 s, five times; any other error is a
:class:`GraphError` carrying Graph's ``error.code``. A download's 302 to a
pre-authenticated URL is followed *without* the bearer token, which that URL
neither needs nor should see.

First drafted by the region's own compose (``compose_artifact`` over the
Microsoft Graph sources), then corrected and tested here.
"""

from __future__ import annotations

import base64
import html
import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

GRAPH = "https://graph.microsoft.com/v1.0"
TIMEOUT_SECONDS = 60
MAX_RETRIES = 5
#: The fields a listing needs from a drive item, so a large folder is read
#: without its thumbnails, permissions and activity.
ITEM_SELECT = (
    "id,name,size,eTag,cTag,webUrl,lastModifiedDateTime,file,folder,package,"
    "remoteItem,parentReference,deleted"
)


class GraphError(RuntimeError):
    """Graph (or SharePoint REST) refused a request."""

    def __init__(self, status: int, code: str, message: str, url: str = "") -> None:
        self.status = status
        self.code = code
        self.message = message
        self.url = url
        super().__init__(f"{status} {code or 'error'}: {message or 'no message'}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    retry_after = error.headers.get("Retry-After") if error.headers else None
    try:
        return max(0.0, float(retry_after)) if retry_after else float(2 ** (attempt + 1))
    except ValueError:
        return float(2 ** (attempt + 1))


def _error_of(error: urllib.error.HTTPError, url: str) -> GraphError:
    try:
        body = json.loads(error.read().decode("utf-8") or "{}")
    except (ValueError, UnicodeError, OSError):
        body = {}
    detail = body.get("error") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        message = detail.get("message")
        # SharePoint REST nests its message: {"error": {"message": {"value": ...}}}.
        if isinstance(message, dict):
            message = message.get("value")
        return GraphError(error.code, str(detail.get("code") or ""), str(message or ""), url)
    return GraphError(error.code, "", str(error.reason or ""), url)


def request(
    method: str,
    url: str,
    token: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    accept_json: bool = True,
) -> Any:
    """The one network call: JSON, bytes, or ``{"Location": ...}`` for a redirect."""

    outgoing_headers = {"User-Agent": "pheasant", **(headers or {})}
    if token:
        outgoing_headers["Authorization"] = f"Bearer {token}"
    if accept_json:
        outgoing_headers.setdefault("Accept", "application/json")
    opener = urllib.request.build_opener(_NoRedirect)
    for attempt in range(MAX_RETRIES + 1):
        outgoing = urllib.request.Request(url, data=data, headers=outgoing_headers, method=method)
        try:
            with opener.open(outgoing, timeout=TIMEOUT_SECONDS) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308):
                return {"Location": error.headers.get("Location", "")}
            if error.code in (429, 503) and attempt < MAX_RETRIES:
                time.sleep(_retry_delay(error, attempt))
                continue
            raise _error_of(error, url) from error
        if not accept_json:
            return body
        return json.loads(body.decode("utf-8")) if body else {}
    raise GraphError(429, "tooManyRetries", f"still throttled after {MAX_RETRIES} retries", url)


def encode_sharing_url(url: str) -> str:
    """The shares API's id for a link: unpadded base64url of it, prefixed ``u!``."""

    encoded = base64.b64encode(url.encode("utf-8")).decode("ascii")
    return "u!" + encoded.rstrip("=").replace("/", "_").replace("+", "-")


def resolve_share(token: str, url: str) -> dict[str, Any]:
    """The drive item a sharing (or plain) OneDrive/SharePoint link names."""

    return request(
        "GET",
        f"{GRAPH}/shares/{encode_sharing_url(url)}/driveItem?$select={ITEM_SELECT}",
        token,
        headers={"Prefer": "redeemSharingLinkIfNecessary"},
    )


def site_by_path(token: str, hostname: str, server_relative_path: str) -> dict[str, Any]:
    """A site by host and server-relative path (``""`` is the tenant's root site)."""

    path = server_relative_path.strip("/")
    url = f"{GRAPH}/sites/{hostname}"
    if path:
        url += f":/{quote(path)}"
    return request("GET", url, token)


def paged(token: str, url: str) -> Iterator[dict[str, Any]]:
    """Every element of ``value`` across ``@odata.nextLink`` pages."""

    next_url: str | None = url
    while next_url:
        page = request("GET", next_url, token)
        yield from page.get("value") or []
        next_url = page.get("@odata.nextLink")


def drive_item(token: str, drive_id: str, item_id: str) -> dict[str, Any]:
    return request("GET", f"{GRAPH}/drives/{drive_id}/items/{item_id}?$select={ITEM_SELECT}", token)


def item_by_path(token: str, drive_id: str, path: str) -> dict[str, Any]:
    """A drive item by its path from the drive's root (``""`` is the root)."""

    path = path.strip("/")
    if not path:
        return request("GET", f"{GRAPH}/drives/{drive_id}/root?$select={ITEM_SELECT}", token)
    return request(
        "GET", f"{GRAPH}/drives/{drive_id}/root:/{quote(path)}:?$select={ITEM_SELECT}", token
    )


def children(token: str, drive_id: str, item_id: str) -> Iterator[dict[str, Any]]:
    yield from paged(
        token, f"{GRAPH}/drives/{drive_id}/items/{item_id}/children?$top=200&$select={ITEM_SELECT}"
    )


def walk(
    token: str, drive_id: str, item_id: str, prefix: str, *, _seen: set[str] | None = None
) -> Iterator[tuple[str, dict[str, Any], str]]:
    """``(relative path, drive item, its drive id)`` for every file beneath a folder.

    A shared folder from another drive (a ``remoteItem``) is walked in its own
    drive, and each folder is walked once, so a share that links back to an
    ancestor cannot loop.
    """

    seen = _seen if _seen is not None else set()
    if f"{drive_id}/{item_id}" in seen:
        return
    seen.add(f"{drive_id}/{item_id}")
    for item in children(token, drive_id, item_id):
        name = str(item.get("name") or "")
        path = f"{prefix.rstrip('/')}/{name}" if prefix else name
        remote = item.get("remoteItem")
        if remote:
            remote_drive = str((remote.get("parentReference") or {}).get("driveId") or "")
            remote_id = str(remote.get("id") or "")
            if not (remote_drive and remote_id):
                continue
            if remote.get("folder") is not None:
                yield from walk(token, remote_drive, remote_id, path, _seen=seen)
            elif remote.get("file") is not None:
                yield path, {**remote, "name": name}, remote_drive
        elif item.get("folder") is not None:
            yield from walk(token, drive_id, str(item["id"]), path, _seen=seen)
        elif item.get("file") is not None:
            yield path, item, drive_id


def site_drives(token: str, site_id: str) -> list[dict[str, Any]]:
    """The site's document libraries."""

    return list(paged(token, f"{GRAPH}/sites/{site_id}/drives?$select=id,name,webUrl,driveType"))


def site_lists(token: str, site_id: str, *, include_hidden: bool = False) -> list[dict[str, Any]]:
    """The site's lists that are lists: no document library, no hidden or system list."""

    url = (
        f"{GRAPH}/sites/{site_id}/lists"
        "?$select=id,name,displayName,webUrl,list,system,lastModifiedDateTime"
    )
    found: list[dict[str, Any]] = []
    for entry in paged(token, url):
        facet = entry.get("list") or {}
        if facet.get("template") == "documentLibrary" or entry.get("system") is not None:
            continue
        if facet.get("hidden") and not include_hidden:
            continue
        found.append(entry)
    return found


def list_columns(token: str, site_id: str, list_id: str) -> list[dict[str, Any]]:
    return list(
        paged(
            token,
            f"{GRAPH}/sites/{site_id}/lists/{list_id}/columns"
            "?$select=name,displayName,hidden,readOnly,columnGroup",
        )
    )


def list_items(token: str, site_id: str, list_id: str) -> Iterator[dict[str, Any]]:
    yield from paged(
        token, f"{GRAPH}/sites/{site_id}/lists/{list_id}/items?$expand=fields&$top=200"
    )


def site_pages(token: str, site_id: str) -> Iterator[dict[str, Any]]:
    yield from paged(token, f"{GRAPH}/sites/{site_id}/pages/microsoft.graph.sitePage")


def page_with_canvas(token: str, site_id: str, page_id: str) -> dict[str, Any]:
    return request(
        "GET",
        f"{GRAPH}/sites/{site_id}/pages/{page_id}/microsoft.graph.sitePage?$expand=canvasLayout",
        token,
    )


def _web_part(part: dict[str, Any]) -> str:
    kind = str(part.get("@odata.type") or "").rsplit(".", 1)[-1]
    if kind == "textWebPart" or "innerHtml" in part:
        return str(part.get("innerHtml") or "")
    data = part.get("data") or {}
    words = [
        str(value)
        for value in (data.get("title"), data.get("description"))
        if isinstance(value, str) and value.strip()
    ]
    # A standard web part's own text (a hero's tiles, a quick link's labels)
    # is in data.serverProcessedContent.searchablePlainTexts.
    processed = data.get("serverProcessedContent") or {}
    for entry in processed.get("searchablePlainTexts") or []:
        if isinstance(entry, dict) and str(entry.get("value") or "").strip():
            words.append(str(entry["value"]))
    label = html.escape(str(part.get("webPartType") or kind or "webPart"), quote=True)
    body = "".join(f"<p>{html.escape(word)}</p>" for word in words)
    return f'<div data-webpart="{label}">{body}</div>'


def render_canvas(page: dict[str, Any]) -> str:
    """A modern page as one HTML document: its title, then every web part in order.

    Horizontal sections are read column by column, the vertical section last,
    as the page lays them out. A text web part is its own HTML; any other web
    part is a ``<div data-webpart>`` holding the words it shows.
    """

    title = html.escape(str(page.get("title") or page.get("name") or "Untitled"))
    canvas = page.get("canvasLayout") or {}
    parts: list[str] = []
    for section in canvas.get("horizontalSections") or []:
        for column in section.get("columns") or []:
            parts.extend(_web_part(part) for part in column.get("webparts") or [])
    vertical = canvas.get("verticalSection") or {}
    parts.extend(_web_part(part) for part in vertical.get("webparts") or [])
    description = str(page.get("description") or "").strip()
    lead = f"<p>{html.escape(description)}</p>" if description else ""
    source = html.escape(str(page.get("webUrl") or ""), quote=True)
    link = f'<p><a href="{source}">{source}</a></p>' if source else ""
    return (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
        f"<title>{title}</title></head>\n<body>\n<h1>{title}</h1>\n{lead}"
        + "\n".join(parts)
        + f"\n{link}\n</body></html>\n"
    )


def download(token: str, drive_id: str, item_id: str) -> bytes:
    """A file's bytes, following the redirect without the bearer token."""

    result = request(
        "GET", f"{GRAPH}/drives/{drive_id}/items/{item_id}/content", token, accept_json=False
    )
    if isinstance(result, bytes):
        return result
    location = str(result.get("Location") or "")
    if not location:
        raise GraphError(302, "missingLocation", "the download redirect named no URL")
    content = request("GET", location, "", accept_json=False)
    if not isinstance(content, bytes):
        raise GraphError(302, "unexpectedRedirect", "the download redirected twice")
    return content


def list_item_attachments(
    sp_token: str, site_web_url: str, list_id: str, item_id: str
) -> list[dict[str, str]]:
    """``[{FileName, ServerRelativeUrl}]`` for one list item (SharePoint REST)."""

    result = request(
        "GET",
        f"{site_web_url.rstrip('/')}/_api/web/lists(guid'{list_id}')/items({int(item_id)})"
        "/AttachmentFiles",
        sp_token,
        headers={"Accept": "application/json;odata=nometadata"},
    )
    return [
        {"FileName": str(entry["FileName"]), "ServerRelativeUrl": str(entry["ServerRelativeUrl"])}
        for entry in result.get("value") or []
        if entry.get("FileName") and entry.get("ServerRelativeUrl")
    ]


def attachment_content(sp_token: str, site_web_url: str, server_relative_url: str) -> bytes:
    """One attachment's bytes (SharePoint REST)."""

    escaped = quote(server_relative_url.replace("'", "''"), safe="/")
    result = request(
        "GET",
        f"{site_web_url.rstrip('/')}/_api/web/GetFileByServerRelativeUrl('{escaped}')/$value",
        sp_token,
        accept_json=False,
    )
    if not isinstance(result, bytes):
        raise GraphError(302, "unexpectedRedirect", "the attachment redirected")
    return result
