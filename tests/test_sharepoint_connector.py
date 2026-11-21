"""OneDrive and SharePoint: one connection, any pasted link, the site as folders.

Acceptance:

1. Any OneDrive/SharePoint link is read off its shape: a site, its Site
   contents, a library or a folder in it (a library view's ``?id=`` too), a
   list, the pages or one page, a sharing link, a personal OneDrive. One source
   per site, so a second link into a site joins its source.
2. A site syncs as folders: ``<library>/<folder>/<file>`` (a shared folder from
   another drive included, formats nothing reads left out),
   ``Lists/<list>/<id>-<title>.md`` with its attachments under
   ``Lists/<list>/<id>-<title>/``, linked from the item, and
   ``Pages/<page>.html`` rendered from the canvas.
3. A second sync downloads nothing; a file removed upstream leaves the index;
   a link that cannot be read is named unreadable and prunes nothing; a 401
   fails the source.
4. The client retries throttling after ``Retry-After``, follows a download's
   redirect without the bearer token, and encodes a sharing link as Graph's
   documentation does.
5. A Microsoft 365 connection signs in, connects as an app, or holds a token,
   and reaches SharePoint REST with a second audience.
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pytest

from pheasant.config.schema import PluginSourceType, SourceConfig
from pheasant.connectors import graph_client as graph
from pheasant.connectors.sharepoint import SharePointConnector
from pheasant.connectors.sharepoint_urls import is_microsoft_url, parse, source_name
from pheasant.persistence.state_store import StateStore
from pheasant.sync.connectors import ConnectorUnavailable

HOST = "contoso.sharepoint.com"
SITE_URL = f"https://{HOST}/sites/Team"
G = graph.GRAPH
ATTACHMENT = "/sites/Team/Lists/Tasks/Attachments/1/toner order.txt"
WELCOME = "<p>Welcome to the <b>cedar relay</b> team.</p>"


# ------------------------------------------------------------------ the URLs


@pytest.mark.parametrize(
    ("url", "kind", "site", "extra"),
    [
        (f"https://{HOST}", "site", "", ""),
        (f"{SITE_URL}", "site", "/sites/Team", ""),
        (f"{SITE_URL}/_layouts/15/viewlsts.aspx", "site", "/sites/Team", ""),
        (f"{SITE_URL}/SitePages/Home.aspx", "site", "/sites/Team", ""),
        (
            f"{SITE_URL}/Shared%20Documents/Specs",
            "drive_path",
            "/sites/Team",
            "/sites/Team/Shared Documents/Specs",
        ),
        (
            f"{SITE_URL}/Shared%20Documents/Forms/AllItems.aspx?id=%2Fsites%2FTeam%2FShared%20Documents%2FSpecs",
            "drive_path",
            "/sites/Team",
            "/sites/Team/Shared Documents/Specs",
        ),
        (
            f"{SITE_URL}/Shared%20Documents/Forms/AllItems.aspx",
            "drive_path",
            "/sites/Team",
            "/sites/Team/Shared Documents",
        ),
        (f"{SITE_URL}/Lists/Tasks/AllItems.aspx", "list", "/sites/Team", "Tasks"),
        (f"{SITE_URL}/SitePages", "pages", "/sites/Team", ""),
        (f"{SITE_URL}/SitePages/Onboarding.aspx", "page", "/sites/Team", "Onboarding.aspx"),
        (f"https://{HOST}/:f:/s/Team/EabcDEF", "share", "/sites/Team", ""),
        (
            f"https://{HOST}/:x:/r/sites/Team/Shared%20Documents/a.xlsx",
            "drive_path",
            "/sites/Team",
            "/sites/Team/Shared Documents/a.xlsx",
        ),
        (
            "https://contoso-my.sharepoint.com/personal/jane_contoso_com/Documents/Notes",
            "drive_path",
            "/personal/jane_contoso_com",
            "/personal/jane_contoso_com/Documents/Notes",
        ),
        ("https://1drv.ms/f/s!AbCdEf", "share", "", ""),
    ],
)
def test_a_link_is_read_off_its_shape(url: str, kind: str, site: str, extra: str):
    target = parse(url)
    assert (target.kind, target.site) == (kind, site)
    if kind == "drive_path":
        assert target.path == extra
    if kind in ("list", "page"):
        assert target.name == extra


def test_one_source_per_site():
    assert source_name(f"{SITE_URL}/Lists/Tasks") == "sharepoint-contoso-team"
    assert source_name(f"{SITE_URL}/Shared%20Documents/x.docx") == "sharepoint-contoso-team"
    assert source_name(f"https://{HOST}/:f:/s/Team/Eabc") == "sharepoint-contoso-team"
    assert source_name(f"https://{HOST}") == "sharepoint-contoso"
    assert source_name("https://contoso-my.sharepoint.com/personal/jane_contoso_com") == (
        "onedrive-contoso-jane-contoso-com"
    )
    assert source_name("https://1drv.ms/f/s!Abc") == "onedrive"
    assert not is_microsoft_url("https://example.com/sites/Team")
    with pytest.raises(ValueError):
        parse("https://example.com/sites/Team")


def test_a_pasted_link_becomes_a_sharepoint_source_and_a_second_one_joins(tmp_path: Path):
    from pheasant.registry.placement import place
    from pheasant.targets import resolve_target

    first = resolve_target(f"{SITE_URL}/Lists/Tasks", clone_root=tmp_path, workspace=tmp_path)
    assert (first.type, first.name, first.urls) == (
        "sharepoint",
        "sharepoint-contoso-team",
        [f"{SITE_URL}/Lists/Tasks"],
    )
    assert first.include == ["**/*"], "a library's Office files and PDFs are admitted"
    second = resolve_target(f"{SITE_URL}/SitePages", clone_root=tmp_path, workspace=tmp_path)
    existing = SourceConfig(
        name=first.name, type=PluginSourceType("sharepoint"), path=Path("/unused"), urls=first.urls
    )
    assert place(second, existing, lambda _n: True)[0] == "join"


def test_the_sharing_link_encoding_is_graphs():
    # The example in Graph's shares-get reference.
    url = "https://onedrive.live.com/redir?resid=1231244193912!12&authKey=1201919!12921!1"
    assert graph.encode_sharing_url(url) == (
        "u!aHR0cHM6Ly9vbmVkcml2ZS5saXZlLmNvbS9yZWRpcj9yZXNpZD0xMjMxMjQ0MTkzOTEyITEyJmF1dGhLZXk9MTIwMTkxOSExMjkyMSEx"
    )


# ------------------------------------------------------------ a fake tenant


def _file(item_id: str, name: str, tag: str = "1", size: int = 10, **extra: Any) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "size": size,
        "cTag": f"c:{item_id}:{tag}",
        "eTag": f"e:{item_id}:{tag}",
        "webUrl": f"{SITE_URL}/Shared%20Documents/{name}",
        "lastModifiedDateTime": "2026-11-01T10:00:00Z",
        "file": {"mimeType": "application/octet-stream"},
        **extra,
    }


class FakeTenant:
    """Graph and SharePoint REST, answered from dictionaries."""

    def __init__(self) -> None:
        self.children: dict[str, list[dict[str, Any]]] = {
            "d1/root-1": [
                _file("f-readme", "readme.txt"),
                {"id": "fold-specs", "name": "Specs", "folder": {"childCount": 2}},
                _file("f-video", "launch.mp4"),
                {
                    "id": "rem-1",
                    "name": "Shared with team",
                    "remoteItem": {
                        "id": "r-root",
                        "folder": {"childCount": 1},
                        "parentReference": {"driveId": "d2"},
                    },
                },
            ],
            "d1/fold-specs": [_file("f-plan", "plan.md"), _file("f-brief", "brief.txt")],
            "d2/r-root": [_file("f-remote", "partner.md")],
        }
        self.bodies = {
            "f-readme": b"Read me: the cedar relay is the team's courier.\n",
            "f-plan": b"# Plan\n\nShip the relay in December.\n",
            "f-brief": b"Brief: two pages.\n",
            "f-remote": b"# Partner\n\nShared from another drive.\n",
        }
        self.list_rows = [
            {
                "id": "1",
                "eTag": "li-1-a",
                "lastModifiedDateTime": "2026-11-02T08:00:00Z",
                "createdDateTime": "2026-10-30T08:00:00Z",
                "webUrl": f"{SITE_URL}/Lists/Tasks/DispForm.aspx?ID=1",
                "fields": {
                    "Title": "Fix the printer",
                    "Status": "In progress",
                    "Notes": "<div><p>Toner is <strong>low</strong>.</p><p>Order more.</p></div>",
                    "AssignedTo": [{"LookupValue": "Ada Lovelace"}],
                    "Attachments": True,
                    "_UIVersionString": "1.0",
                    "ContentType": "Item",
                },
            },
            {
                "id": "2",
                "eTag": "li-2-a",
                "lastModifiedDateTime": "2026-11-02T09:00:00Z",
                "webUrl": f"{SITE_URL}/Lists/Tasks/DispForm.aspx?ID=2",
                "fields": {"Title": "Water the plants", "Status": "Done", "Attachments": False},
            },
        ]
        self.calls: list[tuple[str, str]] = []
        self.forbidden: set[str] = set()
        self.unauthorized = False

    def __call__(self, method, url, token, *, headers=None, data=None, accept_json=True):  # noqa: ANN001
        self.calls.append((url, token))
        parsed = urlparse(url)
        path = unquote(parsed.path)
        if self.unauthorized:
            raise graph.GraphError(401, "InvalidAuthenticationToken", "expired", url)
        if any(fragment in url for fragment in self.forbidden):
            raise graph.GraphError(403, "accessDenied", "not shared with you", url)
        if parsed.hostname == "download.example":
            assert token == "", "a pre-authenticated download URL gets no bearer token"
            return self.bodies[path.strip("/")]
        if parsed.hostname == HOST:
            assert token == "sp-token", "SharePoint REST is called with its own audience"
            if path.endswith("/AttachmentFiles"):
                assert "lists(guid'l-tasks')/items(1)" in path
                return {
                    "value": [
                        {
                            "FileName": "toner order.txt",
                            "ServerRelativeUrl": ATTACHMENT,
                        }
                    ]
                }
            if "GetFileByServerRelativeUrl" in path:
                return b"Order 12 cartridges of toner.\n"
        assert token == "graph-token"
        route = path.removeprefix("/v1.0")
        if route == f"/sites/{HOST}:/sites/Team":
            return {"id": "site-1", "webUrl": SITE_URL, "displayName": "Team"}
        if route == "/sites/site-1/drives":
            return {
                "value": [
                    {"id": "d1", "name": "Documents", "webUrl": f"{SITE_URL}/Shared%20Documents"},
                    {"id": "dp", "name": "Site Pages", "webUrl": f"{SITE_URL}/SitePages"},
                ]
            }
        if route == "/drives/d1/root":
            return {"id": "root-1", "name": "root", "folder": {}}
        if route == "/drives/d1/root:/Specs:" or route == "/drives/d1/root:/Specs":
            return {"id": "fold-specs", "name": "Specs", "folder": {}}
        if route.endswith("/children"):
            drive, item = route.split("/")[2], route.split("/")[4]
            entries = self.children[f"{drive}/{item}"]
            # Two pages, so paging is followed.
            if "skiptoken" not in url and len(entries) > 2:
                return {"value": entries[:2], "@odata.nextLink": url + "&skiptoken=2"}
            return {"value": entries[2:] if "skiptoken" in url else entries}
        if route.endswith("/content"):
            item = route.split("/")[4]
            return {"Location": f"https://download.example/{item}"}
        if route == "/sites/site-1/lists":
            return {
                "value": [
                    {
                        "id": "l-tasks",
                        "name": "Tasks",
                        "displayName": "Tasks",
                        "webUrl": f"{SITE_URL}/Lists/Tasks",
                        "list": {"template": "genericList"},
                    },
                    {
                        "id": "l-docs",
                        "name": "Shared Documents",
                        "displayName": "Documents",
                        "list": {"template": "documentLibrary"},
                    },
                    {
                        "id": "l-hidden",
                        "name": "TaxonomyHiddenList",
                        "displayName": "TaxonomyHiddenList",
                        "list": {"template": "genericList", "hidden": True},
                    },
                    {
                        "id": "l-sys",
                        "name": "appdata",
                        "displayName": "appdata",
                        "list": {"template": "genericList"},
                        "system": {},
                    },
                ]
            }
        if route == "/sites/site-1/lists/l-tasks/columns":
            return {
                "value": [
                    {"name": "Title", "displayName": "Title"},
                    {"name": "Status", "displayName": "Status"},
                    {"name": "Notes", "displayName": "Notes"},
                    {"name": "AssignedTo", "displayName": "Assigned to"},
                    {"name": "_UIVersionString", "displayName": "Version", "readOnly": True},
                    {"name": "ContentType", "displayName": "Content Type", "hidden": True},
                ]
            }
        if route == "/sites/site-1/lists/l-tasks/items":
            return {"value": self.list_rows}
        if route == "/sites/site-1/pages/microsoft.graph.sitePage":
            return {
                "value": [
                    {
                        "id": "p-home",
                        "name": "Home.aspx",
                        "title": "Team home",
                        "eTag": "pe1",
                        "lastModifiedDateTime": "2026-11-03T00:00:00Z",
                        "webUrl": f"{SITE_URL}/SitePages/Home.aspx",
                    },
                ]
            }
        if route == "/sites/site-1/pages/p-home/microsoft.graph.sitePage":
            return {
                "id": "p-home",
                "title": "Team home",
                "webUrl": f"{SITE_URL}/SitePages/Home.aspx",
                "canvasLayout": {
                    "horizontalSections": [
                        {
                            "columns": [
                                {
                                    "webparts": [
                                        {
                                            "@odata.type": "#microsoft.graph.textWebPart",
                                            "innerHtml": WELCOME,
                                        }
                                    ]
                                }
                            ]
                        }
                    ],
                    "verticalSection": {
                        "webparts": [
                            {
                                "@odata.type": "#microsoft.graph.standardWebPart",
                                "webPartType": "quickLinks",
                                "data": {
                                    "title": "Useful links",
                                    "serverProcessedContent": {
                                        "searchablePlainTexts": [
                                            {"key": "t", "value": "Expense policy"}
                                        ]
                                    },
                                },
                            }
                        ]
                    },
                },
            }
        if route.startswith("/shares/"):
            return {
                "id": "f-plan",
                "name": "plan.md",
                "file": {},
                "eTag": "e:f-plan:1",
                "parentReference": {"driveId": "d1", "path": "/drives/d1/root:/Specs"},
            }
        if route == "/drives/d1":
            return {"name": "Documents"}
        raise AssertionError(f"unexpected request {url}")


@pytest.fixture()
def tenant(monkeypatch: pytest.MonkeyPatch) -> FakeTenant:
    fake = FakeTenant()
    monkeypatch.setattr(graph, "request", fake)
    monkeypatch.setenv("MICROSOFT_GRAPH_TOKEN", "graph-token")
    from pheasant.connections import microsoft

    monkeypatch.setattr(microsoft, "sharepoint_token", lambda secret, host: "sp-token")
    return fake


def _source(urls: list[str], **extra: Any) -> SourceConfig:
    return SourceConfig(
        name="sharepoint-contoso-team",
        type=PluginSourceType("sharepoint"),
        path=Path("/unused"),
        include=["**/*"],
        urls=urls,
        **extra,
    )


def _engine(tmp_path: Path, urls: list[str]):
    from pheasant.config.schema import PheasantConfig
    from pheasant.sync.engine import SyncEngine

    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "m365",
                "defaults": "standalone",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "sources": [
                {
                    "name": "sharepoint-contoso-team",
                    "type": "sharepoint",
                    "path": "/unused",
                    "include": ["**/*"],
                    "urls": urls,
                }
            ],
        }
    )
    return SyncEngine(config)


def _paths(engine) -> set[str]:
    return {
        row["relative_path"] for row in engine.state.rows("SELECT relative_path FROM artifacts", ())
    }


def test_a_site_syncs_as_folders_lists_attachments_and_pages(tenant: FakeTenant, tmp_path: Path):
    engine = _engine(tmp_path, [SITE_URL])
    try:
        engine.sync_source("sharepoint-contoso-team", "full")
        assert _paths(engine) == {
            "Documents/readme.txt",
            "Documents/Specs/plan.md",
            "Documents/Specs/brief.txt",
            "Documents/Shared with team/partner.md",
            "Lists/Tasks/1-fix-the-printer.md",
            "Lists/Tasks/1-fix-the-printer/toner order.txt",
            "Lists/Tasks/2-water-the-plants.md",
            "Pages/Home.html",
        }, "a video, a library list, a hidden and a system list are left out"
        rows = {
            row["relative_path"]: row
            for row in engine.state.rows("SELECT relative_path, path FROM artifacts", ())
        }
        assert rows["Pages/Home.html"]["path"] == f"{SITE_URL}/SitePages/Home.aspx"
        item = _text(engine, "Lists/Tasks/1-fix-the-printer.md")
        assert "# Fix the printer" in item
        assert "- **Status:** In progress" in item and "- **Assigned to:** Ada Lovelace" in item
        assert "Toner is low." in item and "<div>" not in item, "rich text is read as words"
        assert "_UIVersionString" not in item and "Content Type" not in item
        assert "[[Lists/Tasks/1-fix-the-printer/toner order.txt]]" in item
        page = _text(engine, "Pages/Home.html")
        assert "cedar relay" in page and "Expense policy" in page
        assert "12 cartridges" in _text(engine, "Lists/Tasks/1-fix-the-printer/toner order.txt")
    finally:
        engine.close()


def _text(engine, relative: str) -> str:
    rows = engine.state.rows(
        "SELECT c.text FROM chunks c JOIN artifacts a ON a.id = c.artifact_id "
        "WHERE a.relative_path = ? ORDER BY c.chunk_index",
        (relative,),
    )
    return "\n".join(str(row["text"]) for row in rows)


def test_a_second_sync_downloads_nothing_and_a_removed_file_leaves(
    tenant: FakeTenant, tmp_path: Path
):
    engine = _engine(tmp_path, [SITE_URL])
    try:
        engine.sync_source("sharepoint-contoso-team", "incremental")
        tenant.calls.clear()
        second = engine.sync_source("sharepoint-contoso-team", "incremental")
        assert second.indexed_artifacts == 0
        reads = [url for url, _ in tenant.calls if "/content" in url or "$value" in url]
        assert reads == [], "nothing unchanged is downloaded again"
        tenant.children["d1/fold-specs"] = [_file("f-plan", "plan.md")]
        engine.sync_source("sharepoint-contoso-team", "incremental")
        assert "Documents/Specs/brief.txt" not in _paths(engine)
        assert "Documents/Specs/plan.md" in _paths(engine)
    finally:
        engine.close()


def test_a_link_that_cannot_be_read_is_named_and_prunes_nothing(tenant: FakeTenant, tmp_path: Path):
    folder = f"{SITE_URL}/Shared%20Documents/Specs"
    lists = f"{SITE_URL}/Lists/Tasks"
    engine = _engine(tmp_path, [folder, lists])
    try:
        engine.sync_source("sharepoint-contoso-team", "incremental")
        assert "Documents/Specs/plan.md" in _paths(engine)
        assert "Documents/readme.txt" not in _paths(engine), "only the folder linked"
        tenant.forbidden.add("/lists")
        result = engine.sync_source("sharepoint-contoso-team", "incremental")
        assert "Lists/Tasks/1-fix-the-printer.md" in _paths(engine), "nothing pruned"
        assert "accessDenied" in json.dumps(result.details, default=str)
    finally:
        engine.close()


def test_a_refused_credential_fails_the_source(tenant: FakeTenant, tmp_path: Path):
    tenant.unauthorized = True
    connector = SharePointConnector(_source([SITE_URL]), _state(tmp_path))
    connector.begin_sync("full")
    with pytest.raises(ConnectorUnavailable, match="sign in again"):
        connector.list_items()


def test_a_sharing_link_lands_where_the_library_would_put_it(tenant: FakeTenant, tmp_path: Path):
    connector = SharePointConnector(
        _source([f"https://{HOST}/:t:/s/Team/EabcDEF"]), _state(tmp_path)
    )
    connector.begin_sync("full")
    assert [item.relative_path for item in connector.list_items()] == ["Documents/Specs/plan.md"]


def test_unreadable_attachments_are_said_in_the_item(
    tenant: FakeTenant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from pheasant.connections import microsoft

    def refuse(secret, host):  # noqa: ANN001
        raise microsoft.MicrosoftAuthError("a pasted Graph token cannot read list attachments")

    monkeypatch.setattr(microsoft, "sharepoint_token", refuse)
    connector = SharePointConnector(_source([f"{SITE_URL}/Lists/Tasks"]), _state(tmp_path))
    connector.begin_sync("full")
    items = {item.relative_path: item for item in connector.list_items()}
    assert set(items) == {"Lists/Tasks/1-fix-the-printer.md", "Lists/Tasks/2-water-the-plants.md"}
    text = connector.read_item(items["Lists/Tasks/1-fix-the-printer.md"]).content.decode()
    assert "Attachments could not be read: a pasted Graph token" in text


def _state(tmp_path: Path) -> StateStore:
    state = StateStore()
    state.migrate()
    return state


# ---------------------------------------------------------------- the client


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _http_error(code: int, headers: dict[str, str], body: bytes = b"") -> urllib.error.HTTPError:
    from email.message import Message

    message = Message()
    for key, value in headers.items():
        message[key] = value
    return urllib.error.HTTPError("https://x", code, "err", message, io.BytesIO(body))


def test_throttling_waits_for_retry_after(monkeypatch: pytest.MonkeyPatch):
    answers: list[Any] = [
        _http_error(429, {"Retry-After": "7"}),
        _http_error(503, {}),
        _Response(b'{"value": []}'),
    ]
    slept: list[float] = []

    class Opener:
        def open(self, request, timeout):  # noqa: ANN001
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr(graph.urllib.request, "build_opener", lambda *_: Opener())
    monkeypatch.setattr(graph.time, "sleep", slept.append)
    assert graph.request("GET", f"{G}/sites/x/lists", "t") == {"value": []}
    assert slept == [7.0, 4.0]


def test_an_error_carries_graphs_code(monkeypatch: pytest.MonkeyPatch):
    body = json.dumps({"error": {"code": "itemNotFound", "message": "gone"}}).encode()

    class Opener:
        def open(self, request, timeout):  # noqa: ANN001
            raise _http_error(404, {}, body)

    monkeypatch.setattr(graph.urllib.request, "build_opener", lambda *_: Opener())
    with pytest.raises(graph.GraphError) as error:
        graph.request("GET", f"{G}/drives/d/items/x", "t")
    assert (error.value.status, error.value.code, error.value.message) == (
        404,
        "itemNotFound",
        "gone",
    )


def test_a_download_follows_the_redirect_without_the_token(monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[str, str | None]] = []

    class Opener:
        def open(self, request, timeout):  # noqa: ANN001
            seen.append((request.full_url, request.get_header("Authorization")))
            if "graph.microsoft.com" in request.full_url:
                raise _http_error(302, {"Location": "https://download.example/blob"})
            return _Response(b"bytes")

    monkeypatch.setattr(graph.urllib.request, "build_opener", lambda *_: Opener())
    assert graph.download("secret", "d1", "f1") == b"bytes"
    assert seen == [
        (f"{G}/drives/d1/items/f1/content", "Bearer secret"),
        ("https://download.example/blob", None),
    ]


# ------------------------------------------------------------- the connection


def test_the_microsoft_365_connection(monkeypatch: pytest.MonkeyPatch):
    from pheasant.connections import microsoft, oauth, providers

    microsoft.forget_cached_tokens()
    entry = providers.provider("microsoft_365")
    assert entry.source_types == ("sharepoint",)
    assert [method.id for method in entry.methods] == ["oauth", "app", "token"]
    assert entry.sign_in().oauth is oauth.MICROSOFT_365

    posts: list[tuple[str, dict[str, str]]] = []

    def post(url, form):  # noqa: ANN001
        posts.append((url, dict(form)))
        return {"access_token": f"token-for:{form['scope']}", "expires_in": 3600}

    monkeypatch.setattr(oauth, "_post", post)
    app = {"tenant_id": "contoso.onmicrosoft.com", "client_id": "app", "client_secret": "s"}
    assert microsoft.graph_token(app) == "token-for:https://graph.microsoft.com/.default"
    assert (
        posts[0][0] == "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/token"
    )
    assert posts[0][1]["grant_type"] == "client_credentials"
    assert microsoft.sharepoint_token(app, HOST) == f"token-for:https://{HOST}/.default"

    signed_in = {"client_id": "app", "refresh_token": "r1", "tenant": "common"}
    assert microsoft.sharepoint_token(signed_in, HOST) == (
        f"token-for:https://{HOST}/AllSites.Read offline_access"
    )
    assert posts[-1][1]["grant_type"] == "refresh_token"
    with pytest.raises(microsoft.MicrosoftAuthError, match="cannot read list attachments"):
        microsoft.sharepoint_token({"token": "pasted"}, HOST)
    assert microsoft.sharepoint_token({"token": "g", "sharepoint_token": "sp"}, HOST) == "sp"

    asked: list[str] = []

    def request(url, headers, data=None):  # noqa: ANN001
        asked.append(url)
        return {"userPrincipalName": "ada@contoso.com"} if "/me" in url else {"webUrl": SITE_URL}

    monkeypatch.setattr(providers, "_request", request)
    assert providers._check_microsoft_365({"token": "g"}).account == "ada@contoso.com"
    assert providers._check_microsoft_365(app).account == SITE_URL, "an app has no /me"
    microsoft.forget_cached_tokens()


def test_a_sign_in_refresh_names_its_scopes(monkeypatch: pytest.MonkeyPatch):
    from pheasant.connections import oauth

    oauth.forget_cached_tokens()
    forms: list[dict[str, str]] = []

    def post(url, form):  # noqa: ANN001
        forms.append(dict(form))
        return {"access_token": "fresh", "expires_in": 3600}

    monkeypatch.setattr(oauth, "_post", post)
    secret = {"client_id": "app", "refresh_token": "r-graph", "expires_at": 0}
    assert oauth.access_token_for(oauth.MICROSOFT_365, secret) == "fresh"
    assert forms[0]["scope"].split() == list(oauth.MICROSOFT_365.scopes)
    oauth.forget_cached_tokens()


# ------------------------------------------------------------------- end to end


def test_quick_add_a_link_then_browse_it_like_a_web_source(
    tenant: FakeTenant, loaded_config, config_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from fastapi.testclient import TestClient

    import pheasant.sync.worker as worker_module
    from pheasant.api.app import create_app

    # A sync over HTTP (and the app's startup sync) runs in a child process,
    # which the fake tenant does not reach: here it does nothing, and the test
    # syncs in this process.
    monkeypatch.setattr(worker_module, "run_sync", lambda *a, **k: {"results": []})
    app = create_app(config=loaded_config, config_path=config_path)
    with TestClient(app) as client:
        for link in (f"{SITE_URL}/Lists/Tasks", f"{SITE_URL}/SitePages/Home.aspx"):
            added = client.post("/sources/quick-add", json={"target": link, "sync_now": False})
            assert added.status_code == 200, added.text
        source = next(
            s for s in client.get("/sources").json() if s["name"] == "sharepoint-contoso-team"
        )
        assert source["type"] == "sharepoint"
        assert json.loads(source["config_json"])["urls"] == [
            f"{SITE_URL}/Lists/Tasks",
            f"{SITE_URL}/SitePages/Home.aspx",
        ], "a second link into the site joins its source"
        app.state.engine.sync_source("sharepoint-contoso-team", "incremental")

        edited = client.post(
            "/sources/sharepoint-contoso-team/urls",
            json={"add": [f"{SITE_URL}/Shared%20Documents/Specs"], "sync": False},
        )
        assert edited.status_code == 200, edited.text
        refused = client.post(
            "/sources/sharepoint-contoso-team/urls", json={"add": ["https://example.com/x"]}
        )
        assert refused.status_code == 422 and "not a OneDrive or SharePoint link" in refused.text

        tree = client.get(
            "/documents/tree",
            params={"source_name": "sharepoint-contoso-team", "path": "Lists/Tasks"},
        ).json()
        names = {entry["name"] for entry in [*tree["directories"], *tree["files"]]}
        assert {"1-fix-the-printer.md", "1-fix-the-printer", "2-water-the-plants.md"} <= names

        page = client.get(
            "/documents/content",
            params={"source_name": "sharepoint-contoso-team", "path": "Pages/Home.html"},
        ).json()
        assert page["page"], "a site page keeps its HTML for the viewer"
        rendered = client.get(
            "/documents/page",
            params={"source_name": "sharepoint-contoso-team", "path": "Pages/Home.html"},
        )
        assert rendered.status_code == 200 and b"cedar relay" in rendered.content

        links = client.get(
            "/documents/links",
            params={
                "source_name": "sharepoint-contoso-team",
                "document": "Lists/Tasks/1-fix-the-printer.md",
            },
        ).json()
        assert "toner order.txt" in json.dumps(links), "the item links its attachment"
    app.state.engine.close()
