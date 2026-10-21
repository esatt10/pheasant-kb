"""A pasted target never replaces a different source, and a web collection's
pages are edited one at a time.

Quick add and ``pheasant up`` name a source after what was pasted, and two
different things can derive one name. Registering under a taken name replaced
the source there and dropped its index: ``/a/docs`` then ``/b/docs``, two
repositories called ``tools``, two folders of one GitHub repository. A web
page shares its host's name with every other page of the site, and there the
shared name is right - that case is ``test_web_page_registration.py``.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig, PheasantSettings
from pheasant.registry.placement import place, same_source
from pheasant.targets import resolve_target, resolve_targets


def _config(tmp_path: Path) -> PheasantConfig:
    return PheasantConfig(
        pheasant=PheasantSettings(
            name="placement",
            state_path=tmp_path / "state",
            workspace_root=tmp_path,
            exports_path=tmp_path / "exports",
        ),
    )


def _registered(client: TestClient) -> dict[str, dict]:
    return {row["name"]: json.loads(row["config_json"]) for row in client.get("/sources").json()}


# ---------------------------------------------------------------------------
# folders and files
# ---------------------------------------------------------------------------


def test_two_folders_with_one_name_are_two_sources(tmp_path: Path) -> None:
    for parent, text in (("a", "alphawidget lives here"), ("b", "betagadget lives here")):
        folder = tmp_path / parent / "docs"
        folder.mkdir(parents=True)
        (folder / "notes.md").write_text(f"# Notes\n\n{text}\n", encoding="utf-8")

    client = TestClient(create_app(config=_config(tmp_path)))
    names = []
    for parent in ("a", "b"):
        response = client.post(
            "/sources/quick-add", json={"target": str(tmp_path / parent / "docs"), "wait": True}
        )
        assert response.status_code == 200, response.text
        names.extend(source["name"] for source in response.json()["sources"])
    assert names == ["docs", "docs-2"]
    registered = _registered(client)
    assert registered["docs"]["path"] == str(tmp_path / "a" / "docs")
    assert registered["docs-2"]["path"] == str(tmp_path / "b" / "docs")
    for word in ("alphawidget", "betagadget"):
        hits = client.post("/search", json={"query": word, "max_results": 3}).json()
        assert hits["results"], f"{word} was dropped when the second folder arrived"


def test_re_adding_a_folder_keeps_what_was_set_on_it(tmp_path: Path) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "notes.md").write_text("# Notes\n", encoding="utf-8")
    client = TestClient(create_app(config=_config(tmp_path)))
    client.post("/sources/quick-add", json={"target": str(folder), "sync_now": False})
    edited = client.put("/sources/docs", json={"description": "edited by hand"})
    assert edited.status_code == 200, edited.text

    again = client.post("/sources/quick-add", json={"target": str(folder), "sync_now": False})
    assert [s["name"] for s in again.json()["sources"]] == ["docs"]
    assert _registered(client)["docs"]["description"] == "edited by hand"


# ---------------------------------------------------------------------------
# repositories
# ---------------------------------------------------------------------------


def _repo(url: str, tmp_path: Path):
    return resolve_target(url, clone_root=tmp_path / "sources", workspace=tmp_path / "external")


def test_two_repositories_with_one_name_get_two_checkouts(tmp_path: Path) -> None:
    first = _repo("https://github.com/org-one/tools.git", tmp_path)
    second = _repo("https://github.com/org-two/tools.git", tmp_path)
    assert first.name == second.name == "tools"

    kind, placed = place(second, first.to_source_dict(), lambda name: name == "tools")
    assert kind == "new"
    assert placed.name == "tools-2"
    assert placed.clone_path == str(tmp_path / "sources" / "tools-2")
    assert placed.path == placed.clone_path, "its own checkout, not org-one's"

    one_call = resolve_targets(
        ["https://github.com/org-one/tools.git", "https://github.com/org-two/tools.git"],
        clone_root=tmp_path / "sources",
        workspace=tmp_path / "external",
    )
    assert [t.name for t in one_call] == ["tools", "tools-2"]
    assert len({t.clone_path for t in one_call}) == 2


def test_two_folders_of_one_repository_are_two_sources(tmp_path: Path) -> None:
    docs = _repo("https://github.com/org/repo/tree/main/docs", tmp_path)
    src = _repo("https://github.com/org/repo/tree/main/src", tmp_path)
    kind, placed = place(src, docs.to_source_dict(), lambda name: name == "repo")
    assert kind == "new"
    assert placed.name == "repo-2"
    assert Path(placed.path) == Path(placed.clone_path) / "src"

    again = _repo("https://github.com/org/repo/tree/main/docs", tmp_path)
    assert place(again, docs.to_source_dict(), lambda name: name == "repo")[0] == "same"
    other_ref = _repo("https://github.com/org/repo/tree/v2/docs", tmp_path)
    assert not same_source(other_ref, docs.to_source_dict())


def test_pheasant_up_places_a_repository_before_cloning_it(tmp_path: Path) -> None:
    """`pheasant up` fetched before it merged into the config, so a second
    `tools` would have fast-forwarded the first one's checkout."""

    from pheasant.quickstart import resolve_up_targets, state_root

    config_path = tmp_path / "pheasant.yaml"
    first = resolve_up_targets(["https://github.com/org-one/tools.git"], config_path)
    config_path.write_text(
        yaml.safe_dump({"sources": [t.to_source_dict() for t in first]}), encoding="utf-8"
    )
    (second,) = resolve_up_targets(["https://github.com/org-two/tools.git"], config_path)
    assert second.name == "tools-2"
    assert second.clone_path == str(state_root(config_path) / "sources" / "tools-2")
    (again,) = resolve_up_targets(["https://github.com/org-one/tools.git"], config_path)
    assert again.name == "tools", "the same repository is the source already there"


# ---------------------------------------------------------------------------
# editing a web collection's URLs, over HTTP and MCP
# ---------------------------------------------------------------------------


class _Pages(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        word = "page" + self.path.strip("/").replace("/", "")
        body = f"<html><body><h1>{self.path}</h1><p>{word} text</p></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture()
def site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Pages)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _collection(tmp_path: Path, site: str) -> tuple[TestClient, str]:
    config = _config(tmp_path)
    config.ingestion.extractor.html_text = True
    client = TestClient(create_app(config=config))
    response = client.post(
        "/sources",
        json={
            "name": "site",
            "type": "web_collection",
            "path": "/unused",
            "urls": [f"{site}/one", f"{site}/two"],
            "sync_now": True,
            "wait": True,
        },
    )
    assert response.status_code == 200, response.text
    return client, "site"


def _found(client: TestClient, word: str) -> bool:
    return bool(client.post("/search", json={"query": word, "max_results": 3}).json()["results"])


def test_urls_are_added_and_removed_without_restating_the_list(tmp_path: Path, site: str) -> None:
    client, name = _collection(tmp_path, site)
    response = client.post(
        f"/sources/{name}/urls",
        json={"add": [f"{site}/three", f"{site}/one"], "remove": [f"{site}/two", f"{site}/nine"]},
    )
    assert response.status_code == 200, response.text
    edit = response.json()
    assert edit["urls"] == [f"{site}/one", f"{site}/three"]
    assert edit["added"] == [f"{site}/three"]
    assert edit["already_listed"] == [f"{site}/one"]
    assert edit["removed"] == [f"{site}/two"]
    assert edit["not_listed"] == [f"{site}/nine"]
    assert edit["status"] == "updated" and edit["sync"]

    client.post("/sync", json={"source_name": name, "mode": "incremental", "wait": True})
    assert _found(client, "pageone"), "a page that stayed was dropped"
    assert _found(client, "pagethree"), "the added page was not fetched"
    assert not _found(client, "pagetwo"), "the removed page is still searchable"
    assert _registered(client)[name]["urls"] == edit["urls"]


def test_a_url_edit_refuses_what_it_cannot_do(tmp_path: Path, site: str) -> None:
    client, name = _collection(tmp_path, site)
    emptied = client.post(f"/sources/{name}/urls", json={"remove": [f"{site}/one", f"{site}/two"]})
    assert emptied.status_code == 422
    assert "remove the source instead" in emptied.json()["detail"]
    assert client.post("/sources/nope/urls", json={"add": [f"{site}/x"]}).status_code == 404
    unchanged = client.post(f"/sources/{name}/urls", json={"add": [f"{site}/one"]}).json()
    assert unchanged["status"] == "unchanged" and unchanged["sync"] is None


def test_mcp_edits_urls_the_same_way(tmp_path: Path, site: str) -> None:
    from pheasant.mcp_server.tools import PheasantTools

    client, name = _collection(tmp_path, site)
    config = _config(tmp_path)
    config.security.allow_agent_private_urls = True
    tools = PheasantTools(config)
    edit = tools.edit_source_urls(
        config.knowledge_base_id, name, add=[f"{site}/three"], remove=[f"{site}/two"]
    )
    assert edit["urls"] == [f"{site}/one", f"{site}/three"]
    assert json.loads(tools.state.get_source(name)["config_json"])["urls"] == edit["urls"]

    config.security.allow_agent_private_urls = False
    with pytest.raises(ValueError, match="127.0.0.1"):
        tools.edit_source_urls(config.knowledge_base_id, name, add=[f"{site}/four"])
    with pytest.raises(ValueError, match="remove the source instead") as mcp_refusal:
        tools.edit_source_urls(
            config.knowledge_base_id, name, remove=[f"{site}/one", f"{site}/three"]
        )
    http = client.post(f"/sources/{name}/urls", json={"remove": [f"{site}/one", f"{site}/three"]})
    assert str(mcp_refusal.value) == http.json()["detail"], "one refusal, spelled once"


def test_a_quick_added_collection_can_be_edited(tmp_path: Path, site: str) -> None:
    """Quick add gives a web collection an anchor path nothing reads or creates,
    and the edit form sends it back: `PUT /sources` checked it as a folder and
    refused every edit with "Path does not exist"."""

    client = TestClient(create_app(config=_config(tmp_path)))
    added = client.post("/sources/quick-add", json={"target": f"{site}/one", "sync_now": False})
    name = added.json()["sources"][0]["name"]
    edited = dict(_registered(client)[name])
    edited.pop("name")
    edited["urls"] = [f"{site}/one", f"{site}/two"]
    response = client.put(f"/sources/{name}", json=edited)
    assert response.status_code == 200, response.text
    assert _registered(client)[name]["urls"] == edited["urls"]
    moved = client.put(f"/sources/{name}", json={**edited, "path": str(tmp_path / "nowhere")})
    assert moved.status_code == 400, "a path that changed is still checked"


def test_replacing_a_collection_reads_its_pages_again(tmp_path: Path, site: str) -> None:
    """Replacing a source cleared its documents and kept its checkpoint, which
    is where each page's next check lives: the sync after an edit found every
    page "not due" and the collection stayed empty for an hour."""

    from pheasant.sync.engine import SyncEngine

    config = _config(tmp_path)
    config.ingestion.extractor.html_text = True
    config.sources = [
        PheasantConfig.model_validate(
            {
                "sources": [
                    {
                        "name": "site",
                        "type": "web_collection",
                        "urls": [f"{site}/one", f"{site}/two"],
                        "connector": {"allow_experimental": True},
                    }
                ]
            }
        ).sources[0]
    ]
    engine = SyncEngine(config)
    try:
        assert engine.sync_source("site", "incremental").indexed_artifacts == 2
        replaced = config.sources[0].model_dump(mode="json")
        replaced["urls"] = [f"{site}/one"]
        engine.replace_source(replaced)
        assert engine.sync_source("site", "incremental").indexed_artifacts == 1
    finally:
        engine.close()
