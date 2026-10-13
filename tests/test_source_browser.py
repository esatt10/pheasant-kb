"""Browsing a source as folders and reading one file (`services.source_browser`).

Four things are held here:

1. **The tree is the index's.** One level at a time, folders with the number
   of documents beneath them, files directly in the folder, breadcrumbs back
   up — read from the artifact rows, so a folder the index does not hold is
   refused and a case-blind path comes back spelled as indexed.
2. **A read is what the index holds.** The indexed file when it is still the
   one indexed, otherwise the chunks reassembled without the overlap between
   them — so editing or deleting the file after indexing never shows text no
   chunk or citation describes.
3. **Both surfaces, one implementation**, refusal text included; the raw bytes
   are HTTP only, and say so.
4. **`@taxonomy`** is answered from the index with no model, honours the
   chat's source scope, and says when a folder it was asked for is missing.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.assistant import inventory, keywords
from pheasant.config.schema import PheasantConfig
from pheasant.mcp_server.tools import PheasantTools
from pheasant.services import source_browser
from pheasant.services.errors import DocumentNotFound, InvalidRequest, ServiceError, SourceNotFound

#: A 1x1 PNG, so the media and raw paths have real image bytes to serve.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
LONG = "\n".join(
    f"Line {n:03d} of the rotation runbook, which says something." for n in range(1, 61)
)

CORPUS: dict[str, str | bytes] = {
    "README.md": "# Readme\n\nWelcome.\n",
    "guide/deploy.md": "# Deploy\n\nIntro.\n\n## Steps\n\nRestart the vault.\n",
    "guide/Setup/install.md": "# Install\n\nRun the installer.\n",
    "guide/rotation.txt": LONG + "\n",
    "code/app.py": "def main():\n    return 1\n",
    "data/table.csv": "name,port\ngateway,8080\nvault,8200\n",
    "data/config.yaml": "service:\n  name: gateway\n  ports: [8080, 8443]\n",
    "img/logo.png": PNG,
}


def _region(root: Path, **overrides: Any) -> dict[str, Any]:
    workspace = root / "workspace"
    for relative, body in CORPUS.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body, encoding="utf-8")
    raw: dict[str, Any] = {
        "pheasant": {
            "name": "browser",
            "state_path": str(root / "state"),
            "workspace_root": str(workspace),
            "exports_path": str(root / "exports"),
        },
        "server": {"host": "127.0.0.1"},
        "storage": {"graph_snapshots": False},
        "assistant": {"provider": "none", "inventory": {"max_items": 50}},
        "sources": [
            {
                "name": "docs",
                "type": "document_folder",
                "path": str(workspace),
                "include": ["**/*"],
                # Small, overlapping chunks, so reassembly has overlap to remove.
                "chunking": {"max_chars": 400, "overlap_chars": 120},
            }
        ],
    }
    for section, values in overrides.items():
        raw.setdefault(section, {}).update(values)
    config = PheasantConfig.model_validate(raw)
    tools = PheasantTools(config)
    tools.engine.sync_source("docs", "full")
    tools.engine.reload_graph()
    client = TestClient(create_app(config, config_path=str(root / "pheasant.yaml")))
    return {
        "config": config,
        "tools": tools,
        "client": client,
        "kb": config.knowledge_base_id,
        "workspace": workspace,
    }


@pytest.fixture(scope="module")
def region(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _region(tmp_path_factory.mktemp("browser"))


def _http(region: dict[str, Any], route: str, params: dict[str, Any]) -> Any:
    response = region["client"].get(route, params=params)
    assert response.status_code == 200, f"{route} -> {response.status_code}: {response.text}"
    return response.json()


# ---------------------------------------------------------------------------
# 1. The tree
# ---------------------------------------------------------------------------


def test_the_top_level_is_the_sources(region: dict[str, Any]) -> None:
    top = region["tools"].browse_source(region["kb"])

    assert top["source"] is None and top["breadcrumbs"] == []
    assert [(d["name"], d["documents"]) for d in top["directories"]] == [("docs", len(CORPUS))]
    assert top["directories"][0]["source_type"] == "document_folder"


def test_a_source_root_lists_folders_with_counts_then_its_own_files(region: dict[str, Any]) -> None:
    root = region["tools"].browse_source(region["kb"], "docs")

    assert {d["name"]: d["documents"] for d in root["directories"]} == {
        "code": 1,
        "data": 2,
        "guide": 3,
        "img": 1,
    }
    assert [f["path"] for f in root["files"]] == ["README.md"]
    assert root["files"][0]["viewer"] == "markdown"
    assert root["totals"] == {"directories": 4, "files": 1, "documents": len(CORPUS)}
    assert root["parent"] is None
    assert root["breadcrumbs"] == [{"name": "docs", "source": "docs", "path": ""}]


def test_a_folder_walks_down_and_its_breadcrumbs_lead_back_up(region: dict[str, Any]) -> None:
    tools, kb = region["tools"], region["kb"]
    guide = tools.browse_source(kb, "docs", "guide")
    assert [d["path"] for d in guide["directories"]] == ["guide/Setup"]
    assert [f["name"] for f in guide["files"]] == ["deploy.md", "rotation.txt"]

    deepest = tools.browse_source(kb, "docs", guide["directories"][0]["path"])
    assert [f["path"] for f in deepest["files"]] == ["guide/Setup/install.md"]
    assert deepest["directories"] == []
    assert deepest["parent"] == "guide"
    assert [c["path"] for c in deepest["breadcrumbs"]] == ["", "guide", "guide/Setup"]

    back = tools.browse_source(kb, "docs", deepest["parent"])
    assert back["path"] == "guide"


def test_a_folder_is_matched_without_case_and_spelled_as_indexed(region: dict[str, Any]) -> None:
    found = region["tools"].browse_source(region["kb"], "docs", "/GUIDE/setup/")

    assert found["path"] == "guide/Setup"
    assert [f["name"] for f in found["files"]] == ["install.md"]


def test_files_are_paged_in_name_order(region: dict[str, Any]) -> None:
    first = region["tools"].browse_source(region["kb"], "docs", "data", limit=1)
    second = region["tools"].browse_source(region["kb"], "docs", "data", limit=1, offset=1)

    assert [f["name"] for f in first["files"]] == ["config.yaml"]
    assert first["page"]["has_more"] is True
    assert [f["name"] for f in second["files"]] == ["table.csv"]
    assert second["page"]["has_more"] is False


def test_what_the_tree_refuses(region: dict[str, Any]) -> None:
    tools, kb = region["tools"], region["kb"]
    with pytest.raises(SourceNotFound):
        tools.browse_source(kb, "nowhere")
    with pytest.raises(InvalidRequest, match="No folder 'nope'"):
        tools.browse_source(kb, "docs", "nope")
    with pytest.raises(InvalidRequest, match="needs a source_name"):
        tools.browse_source(kb, None, "guide")
    # `..` cannot climb out of a source: it is dropped, not followed.
    assert source_browser.clean_directory("../../etc/") == "etc"


# ---------------------------------------------------------------------------
# 2. One document
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "viewer", "language"),
    [
        ("README.md", "markdown", None),
        ("code/app.py", "code", "python"),
        ("data/table.csv", "table", None),
        ("data/config.yaml", "yaml", "yaml"),
        ("guide/rotation.txt", "text", None),
        ("img/logo.png", "image", None),
    ],
)
def test_a_document_is_classified_for_its_viewer(
    region: dict[str, Any], path: str, viewer: str, language: str | None
) -> None:
    read = region["tools"].read_document(region["kb"], path, "docs")

    assert (read["viewer"], read["language"]) == (viewer, language)
    assert read["document"]["path"] == path


def test_a_text_file_reads_as_the_file_that_was_indexed(region: dict[str, Any]) -> None:
    read = region["tools"].read_document(region["kb"], "guide/deploy.md", "docs")

    assert read["content_origin"] == "file"
    assert read["content"] == CORPUS["guide/deploy.md"]
    assert [c["path"] for c in read["breadcrumbs"]] == ["", "guide"]
    assert read["raw"] == {"available": False, "mime_type": None}


def test_a_cited_passage_names_the_lines_it_spans(region: dict[str, Any]) -> None:
    state = region["tools"].engine.state
    chunk = state.rows(
        "SELECT id, start_line, end_line FROM chunks WHERE artifact_id=? ORDER BY chunk_index DESC",
        ("file:docs:guide/rotation.txt:branch=none",),
    )[0]
    read = region["tools"].read_document(region["kb"], "guide/rotation.txt", "docs", chunk["id"])

    assert read["focus"]["start_line"] == chunk["start_line"]
    assert read["focus"]["end_line"] == chunk["end_line"]
    # A chunk of another document focuses nothing here.
    other = state.rows(
        "SELECT id FROM chunks WHERE artifact_id=?", ("file:docs:README.md:branch=none",)
    )[0]["id"]
    assert (
        region["tools"].read_document(region["kb"], "guide/rotation.txt", "docs", other)["focus"]
        is None
    )


def test_an_edited_file_reads_as_its_chunks_without_their_overlap(tmp_path: Path) -> None:
    """The file changed after indexing, so its bytes are not what the index
    holds. The read falls back to the chunks — which overlap — and must give
    back exactly the indexed text, each line once."""

    mine = _region(tmp_path)
    chunks = mine["tools"].engine.state.rows(
        "SELECT start_line, end_line FROM chunks WHERE artifact_id=? ORDER BY chunk_index",
        ("file:docs:guide/rotation.txt:branch=none",),
    )
    assert len(chunks) > 2, "the fixture must produce several chunks"
    pairs = zip(chunks, chunks[1:], strict=False)
    assert any(b["start_line"] <= a["end_line"] for a, b in pairs), (
        "the fixture must produce overlapping chunks, or there is nothing to remove"
    )
    (mine["workspace"] / "guide" / "rotation.txt").write_text("edited since\n", encoding="utf-8")

    read = mine["tools"].read_document(mine["kb"], "guide/rotation.txt", "docs")

    assert read["content_origin"] == "chunks"
    assert read["content"].strip() == LONG


def test_an_image_has_its_bytes_and_its_caption(region: dict[str, Any]) -> None:
    read = region["tools"].read_document(region["kb"], "img/logo.png", "docs")
    assert read["raw"] == {"available": True, "mime_type": "image/png"}

    response = region["client"].get("/documents/raw", params={"path": "img/logo.png"})
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_raw_bytes_are_only_for_binary_formats_and_only_as_indexed(tmp_path: Path) -> None:
    mine = _region(tmp_path)
    text = mine["client"].get("/documents/raw", params={"path": "README.md"})
    assert text.status_code == 422
    assert "read as text" in text.json()["detail"]

    (mine["workspace"] / "img" / "logo.png").write_bytes(PNG + b"tampered")
    changed = mine["client"].get("/documents/raw", params={"path": "img/logo.png"})
    assert changed.status_code == 404


def test_what_a_read_refuses(region: dict[str, Any]) -> None:
    tools, kb = region["tools"], region["kb"]
    with pytest.raises(DocumentNotFound):
        tools.read_document(kb, "missing.md")
    with pytest.raises(InvalidRequest, match="names 3 documents"):
        tools.read_document(kb, ".md")
    with pytest.raises(SourceNotFound):
        tools.read_document(kb, "README.md", "nowhere")


def test_an_acl_enforcing_region_lists_and_reads_only_what_the_caller_may(
    tmp_path: Path,
) -> None:
    private = _region(tmp_path, security={"acl_enforced": True, "default_visibility": "private"})
    tools, kb = private["tools"], private["kb"]

    root = tools.browse_source(kb, "docs")
    assert root["directories"] == [] and root["files"] == []
    assert root["totals"]["documents"] == 0
    assert tools.browse_source(kb)["directories"] == []
    with pytest.raises(DocumentNotFound):
        tools.read_document(kb, "README.md", "docs")


# ---------------------------------------------------------------------------
# 3. Both surfaces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "kwargs"),
    [
        ({}, {}),
        ({"source_name": "docs"}, {"source_name": "docs"}),
        ({"source_name": "docs", "path": "guide"}, {"source_name": "docs", "path": "guide"}),
        (
            {"source_name": "docs", "path": "data", "limit": 1, "offset": 1},
            {"source_name": "docs", "path": "data", "limit": 1, "offset": 1},
        ),
    ],
)
def test_the_tree_answers_identically_on_both_surfaces(
    region: dict[str, Any], params: dict[str, Any], kwargs: dict[str, Any]
) -> None:
    assert _http(region, "/documents/tree", params) == region["tools"].browse_source(
        region["kb"], **kwargs
    )


@pytest.mark.parametrize("path", ["guide/deploy.md", "docs/code/app.py", "table.csv", "logo.png"])
def test_a_read_answers_identically_on_both_surfaces(region: dict[str, Any], path: str) -> None:
    assert _http(region, "/documents/content", {"path": path}) == region["tools"].read_document(
        region["kb"], path
    )


@pytest.mark.parametrize(
    ("route", "params", "call", "status"),
    [
        ("/documents/tree", {"source_name": "nowhere"}, ("browse_source", ("nowhere",)), 404),
        (
            "/documents/tree",
            {"source_name": "docs", "path": "nope"},
            ("browse_source", ("docs", "nope")),
            422,
        ),
        ("/documents/content", {"path": "missing.md"}, ("read_document", ("missing.md",)), 404),
        ("/documents/content", {"path": ".md"}, ("read_document", (".md",)), 422),
    ],
)
def test_both_surfaces_refuse_with_one_text(
    region: dict[str, Any], route: str, params: dict[str, Any], call: tuple, status: int
) -> None:
    response = region["client"].get(route, params=params)
    name, args = call
    with pytest.raises(ServiceError) as refused:
        getattr(region["tools"], name)(region["kb"], *args)

    assert response.status_code == status
    assert response.json()["detail"] == str(refused.value)


# ---------------------------------------------------------------------------
# 4. @taxonomy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "source", "path"),
    [
        ("@taxonomy", None, None),
        ("@taxonomy docs", "docs", None),
        ("@taxonomy docs guide/setup", "docs", "guide/setup"),
        ("@taxonomy docs/guide setup", "docs", "guide/setup"),
        ("@taxonomy the docs source", "docs", None),
        ("@taxonomy guide", None, "guide"),
        ("@browse docs", "docs", None),
        ("@pheasant folders in docs", "docs", None),
    ],
)
def test_taxonomy_reads_a_source_and_a_folder(
    typed: str, source: str | None, path: str | None
) -> None:
    read = inventory.read_question(keywords.inventory_form(typed), sources=["docs", "notes"])

    assert read is not None and read.action == "taxonomy"
    assert (read.source_name, read.path) == (source, path)


def test_tree_is_still_the_hierarchy_diagram() -> None:
    """`@tree` was a diagram shape before `@taxonomy` existed and stays one."""

    assert keywords.read("@tree the sync pipeline").visual == "hierarchy"


def test_taxonomy_lists_a_folder_from_the_index(region: dict[str, Any]) -> None:
    answer = region["tools"].ask_knowledge_base(region["kb"], "@taxonomy docs guide")

    assert answer["route"]["intent"] == "inventory"
    assert answer["inventory"]["action"] == "taxonomy"
    assert answer["inventory"]["tool"] == "browse_source"
    assert answer["inventory"]["result"]["path"] == "guide"
    assert "| `deploy.md` |" in answer["answer"]
    assert "| 📁 `Setup/` |" in answer["answer"]
    assert "Up: `@taxonomy docs`" in answer["answer"]


def test_taxonomy_alone_lists_the_sources(region: dict[str, Any]) -> None:
    answer = region["tools"].ask_knowledge_base(region["kb"], "@taxonomy")

    assert answer["inventory"]["result"]["source"] is None
    assert "| 📁 docs | document_folder |" in answer["answer"]


def test_taxonomy_browses_the_source_the_chat_is_scoped_to(region: dict[str, Any]) -> None:
    from pheasant.services.assistant import AnswerRequest, answer

    scoped = answer(
        region["tools"].services, AnswerRequest(question="@taxonomy guide", source_name="docs")
    )

    assert scoped["inventory"]["result"]["source"] == "docs"
    assert scoped["inventory"]["result"]["path"] == "guide"


def test_a_missing_folder_is_said_and_the_root_shown(region: dict[str, Any]) -> None:
    answer = region["tools"].ask_knowledge_base(region["kb"], "@taxonomy docs nope")

    assert answer["inventory"]["result"]["path"] == ""
    assert "No folder 'nope' in source 'docs'" in answer["answer"]


POSTGRES_DSN = __import__("os").environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()


@pytest.mark.skipif(not POSTGRES_DSN, reason="set PHEASANT_TEST_POSTGRES_DSN to run backend parity")
def test_the_tree_and_reads_agree_across_backends(tmp_path: Path) -> None:
    """The tree's SQL is hand-written for both dialects (`LIKE … ESCAPE`, the
    removed-source anti-join, the outline's `GROUP BY`): one corpus, both
    backends, the same folders, files and text."""

    import psycopg

    with psycopg.connect(POSTGRES_DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    lite = _region(tmp_path / "sqlite")
    storage = {"backend": "postgres", "dsn_env": "PHEASANT_TEST_POSTGRES_DSN"}
    # CI's Postgres preloads pg_textsearch, the default; a stock server (a
    # developer's local one) runs this with PHEASANT_TEST_LEXICAL_INDEX=tsvector.
    lexical = __import__("os").environ.get("PHEASANT_TEST_LEXICAL_INDEX")
    if lexical:
        storage["lexical_index"] = lexical
    pg = _region(tmp_path / "postgres", storage=storage)

    def steady(payload: dict[str, Any]) -> dict[str, Any]:
        for row in [*payload.get("files", []), payload.get("document") or {}]:
            row.pop("last_indexed_at", None)
        return payload

    for args in [(), ("docs",), ("docs", "guide"), ("docs", "GUIDE/setup"), ("docs", "data")]:
        a = steady(lite["tools"].browse_source(lite["kb"], *args))
        b = steady(pg["tools"].browse_source(pg["kb"], *args))
        assert a == b, args
    for path in ("README.md", "guide/rotation.txt", "data/table.csv", "img/logo.png"):
        a = steady(lite["tools"].read_document(lite["kb"], path, "docs"))
        b = steady(pg["tools"].read_document(pg["kb"], path, "docs"))
        assert a == b, path
    with pytest.raises(InvalidRequest, match="No folder"):
        pg["tools"].browse_source(pg["kb"], "docs", "nope")
