"""Images a document references: linked at index time, stored, served, cited.

The chain this module drives end to end, on a real sync over a real corpus:

1. ``![Deploy pipeline](../images/diagram.png)`` in a Markdown document is
   recorded as an ``image_link`` and resolved to the ``image`` artifact as an
   ``embeds`` edge — relative to the document first, by path suffix otherwise.
2. The indexer stores the image's bytes, content-addressed, in the media store,
   so a serving process can show it without the source mounted.
3. ``GET /media`` and the MCP ``get_image`` tool return the same bytes, under
   the same refusal when the node is not an image.
4. An answer citing the document carries the image as a numbered figure, and
   a ``[fig:n]`` marker that names no figure is dropped.

Offline: the stub captioner and an authored ``.caption.txt`` sidecar.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.graph.media_links import image_links
from pheasant.ingestion.media import MediaStore, media_store_for_config
from pheasant.mcp_server.tools import PheasantTools
from pheasant.services.errors import ServiceError

FIXTURE_IMAGE = Path(__file__).parent / "fixtures" / "sample_workspace" / "images" / "diagram.png"
FIXTURE_CAPTION = FIXTURE_IMAGE.with_name(FIXTURE_IMAGE.name + ".caption.txt")
DESIGN = "file:gallery:docs/design.md:branch=none"
DIAGRAM = "file:gallery:images/diagram.png:branch=none"


def _build(root: Path) -> dict[str, Any]:
    workspace = root / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "notes").mkdir()
    (workspace / "images").mkdir()
    (workspace / "images" / "diagram.png").write_bytes(FIXTURE_IMAGE.read_bytes())
    (workspace / "images" / "diagram.png.caption.txt").write_text(
        FIXTURE_CAPTION.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (workspace / "images" / "unreferenced.png").write_bytes(FIXTURE_IMAGE.read_bytes() + b"\x01")
    (workspace / "docs" / "design.md").write_text(
        "# Deployment design\n\nThe deploy pipeline routes releases through three regions.\n\n"
        "![Deploy pipeline](../images/diagram.png)\n\nRollbacks restore the previous release.\n",
        encoding="utf-8",
    )
    # A bare file name from another directory: resolves by path suffix.
    (workspace / "notes" / "summary.md").write_text(
        "# Summary\n\nSee the router diagram: ![](diagram.png)\n", encoding="utf-8"
    )
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "gallery",
                "state_path": str(root / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(root / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "storage": {"graph_snapshots": False},
            "sources": [
                {
                    "name": "gallery",
                    "type": "document_folder",
                    "path": str(workspace),
                    "include": ["**/*.md", "**/*.png"],
                }
            ],
        }
    )
    tools = PheasantTools(config)
    tools.engine.sync_source("gallery", "full")
    tools.engine.reload_graph()
    client = TestClient(create_app(config, config_path=str(root / "pheasant.yaml")))
    return {"config": config, "tools": tools, "client": client, "kb": config.knowledge_base_id}


@pytest.fixture(scope="module")
def gallery(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _build(tmp_path_factory.mktemp("gallery"))


def _embeds(graph: Any, source: str) -> list[tuple[str, dict]]:
    return [
        (target, data)
        for _s, target, edge_map in graph.out_edges(source)
        for data in edge_map.values()
        if data.get("type") == "embeds" and graph.nodes[target].get("type") == "image"
    ]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_every_image_syntax_is_found_and_remote_images_are_not() -> None:
    links, rest = image_links(
        '![Arch](img/arch.png "t") ![](./img/empty.png) ![[flow.png|300]] '
        "![[chart.png|Quarterly chart]] <img alt='Deploy' src=\"../a/deploy.jpg\"> "
        "![remote](https://example.com/x.png) ![inline](data:image/png;base64,AAA=) "
        "![not an image](notes.pdf)"
    )

    assert links == [
        ("img/arch.png", "Arch"),
        ("./img/empty.png", ""),
        ("flow.png", ""),  # |300 is Obsidian's width, not alt text
        ("chart.png", "Quarterly chart"),
        ("../a/deploy.jpg", "Deploy"),
    ]
    # Remote and data: images stay in the text for the ordinary link pass.
    assert "https://example.com/x.png" in rest
    assert "img/arch.png" not in rest


def test_a_repeated_image_is_one_link() -> None:
    links, _ = image_links("![a](x.png) and again ![b](x.png)")
    assert links == [("x.png", "a")]


# ---------------------------------------------------------------------------
# Resolution and storage
# ---------------------------------------------------------------------------


def test_a_relative_image_link_resolves_to_the_image_artifact(gallery: dict[str, Any]) -> None:
    graph = gallery["tools"].engine.graph_builder.graph
    embeds = _embeds(graph, DESIGN)

    assert [target for target, _ in embeds] == [DIAGRAM]
    assert embeds[0][1]["alt"] == "Deploy pipeline"
    assert embeds[0][1]["reference_type"] == "image_link"


def test_a_bare_file_name_resolves_by_suffix(gallery: dict[str, Any]) -> None:
    graph = gallery["tools"].engine.graph_builder.graph
    assert [t for t, _ in _embeds(graph, "file:gallery:notes/summary.md:branch=none")] == [DIAGRAM]


def test_the_image_link_is_not_also_recorded_as_a_document_link(gallery: dict[str, Any]) -> None:
    graph = gallery["tools"].engine.graph_builder.graph
    references = [
        graph.nodes[target].get("reference")
        for _s, target, edge_map in graph.out_edges(DESIGN)
        for data in edge_map.values()
        if data.get("type") == "references"
    ]
    assert "../images/diagram.png" not in references


def test_the_indexer_stores_image_bytes_by_content(gallery: dict[str, Any]) -> None:
    store = media_store_for_config(gallery["config"])
    rows = gallery["tools"].state.rows("SELECT sha256 FROM artifacts WHERE id=?", (DIAGRAM,))

    assert store.get(rows[0]["sha256"], ".png") == FIXTURE_IMAGE.read_bytes()
    assert store.usage()["files"] == 2  # both images, documents never


def test_the_store_refuses_what_it_cannot_serve(tmp_path: Path) -> None:
    store = MediaStore(tmp_path)
    assert store.put("not-a-sha", ".png", b"x") is None
    assert store.put("a" * 64, ".svg", b"<svg/>") is None, "SVG can carry script"
    first = store.put("b" * 64, ".png", b"png")
    assert first is not None and store.put("b" * 64, ".png", b"png") == first


# ---------------------------------------------------------------------------
# Serving, on both surfaces
# ---------------------------------------------------------------------------


def test_media_is_served_identically_on_both_surfaces(gallery: dict[str, Any]) -> None:
    response = gallery["client"].get("/media", params={"node_id": DIAGRAM})
    over_mcp = gallery["tools"].get_image(gallery["kb"], DIAGRAM)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.content == FIXTURE_IMAGE.read_bytes()
    assert base64.b64decode(over_mcp["data"]) == response.content
    assert over_mcp["mime_type"] == "image/png"
    assert "router" in over_mcp["caption"]


@pytest.mark.parametrize("node_id", [DESIGN, "file:gallery:missing.png:branch=none"])
def test_a_node_that_is_not_a_stored_image_is_refused_with_one_text(
    gallery: dict[str, Any], node_id: str
) -> None:
    response = gallery["client"].get("/media", params={"node_id": node_id})
    with pytest.raises(ServiceError) as refused:
        gallery["tools"].get_image(gallery["kb"], node_id)

    assert response.status_code == 404
    assert response.json()["code"] == refused.value.code == "UNKNOWN_MEDIA"
    assert response.json()["detail"] == str(refused.value)


# ---------------------------------------------------------------------------
# Figures in answers
# ---------------------------------------------------------------------------


def test_an_answer_citing_the_document_carries_its_figure(gallery: dict[str, Any]) -> None:
    payload = gallery["tools"].ask_knowledge_base(
        gallery["kb"], "show me the deploy pipeline diagram", workflow="simple"
    )

    figures = {figure["node_id"]: figure for figure in payload["figures"]}
    assert DIAGRAM in figures
    figure = figures[DIAGRAM]
    assert figure["figure"] >= 1
    assert "router" in figure["caption"]
    assert payload["route"]["visual"] == "image"
    assert payload["visual"]["type"] == "images" and payload["visual"]["status"] == "ok"


def test_a_figure_marker_with_no_figure_is_dropped() -> None:
    from pheasant.assistant.answering import verify_figures

    figures = [{"figure": 1, "node_id": "img"}]
    cleaned, dropped = verify_figures("See [fig:1] and [fig:7].", figures)

    assert cleaned == "See [fig:1] and ."
    assert dropped == 1
    assert figures[0]["shown"] is True


def test_the_http_answer_matches_the_mcp_answer(gallery: dict[str, Any]) -> None:
    question = "what does the deployment design say about rollbacks?"
    over_http = gallery["client"].post(
        "/assistant/chat", json={"question": question, "workflow": "simple"}
    )
    over_mcp = gallery["tools"].ask_knowledge_base(gallery["kb"], question, workflow="simple")

    assert over_http.status_code == 200
    body = over_http.json()
    assert body["answer"] == over_mcp["answer"]
    assert [c["node_id"] for c in body["citations"]] == [
        c["node_id"] for c in over_mcp["citations"]
    ]
    assert body["figures"] == over_mcp["figures"]
    assert body["route"] == over_mcp["route"]
