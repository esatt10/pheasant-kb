"""Grounded visuals: every element cites a passage, or says it does not.

The model proposes a diagram; :func:`validate_spec` decides what survives, the
way ``verify_node`` decides which ``[n]`` markers do. What is asserted:

* a citation to a passage that was not given is dropped, and an element left
  with none is kept but marked ``inferred`` (drawn dashed);
* a diagram that is mostly inference is **declined** with a reason;
* labels cannot break out of the Mermaid export or smuggle markup into it;
* with no model, the diagram is the graph's own edges between the cited
  sources — grounded by construction;
* ``create_visual`` / ``POST /assistant/visual`` answer identically, and a
  passage the caller may not read is refused rather than drawn.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.assistant import visuals
from pheasant.assistant.llm import LLM
from pheasant.config.schema import PheasantConfig
from pheasant.mcp_server.tools import PheasantTools
from pheasant.services.errors import ServiceError

CITATIONS = [{"index": i, "node_id": f"file:n{i}", "title": f"n{i}"} for i in (1, 2, 3)]


def _spec(**overrides: Any) -> dict:
    spec = {
        "kind": "flow",
        "title": "Release",
        "nodes": [
            {"id": "build", "label": "Build", "cites": [1]},
            {"id": "test", "label": "Test", "cites": [2, 9]},
            {"id": "ship", "label": "Ship", "cites": [3]},
        ],
        "edges": [
            {"from": "build", "to": "test", "cites": [1]},
            {"from": "test", "to": "ship", "label": "on green", "cites": ["[2]"]},
        ],
    }
    spec.update(overrides)
    return spec


def test_citations_are_checked_and_unsupported_elements_are_marked() -> None:
    spec = _spec()
    spec["nodes"].append({"id": "party", "label": "Celebrate", "cites": [42]})
    spec["edges"].append({"from": "ship", "to": "party", "cites": []})

    result = visuals.validate_spec(spec, CITATIONS)

    assert result["status"] == "ok"
    nodes = {node["id"]: node for node in result["diagram"]["nodes"]}
    assert nodes["test"]["cites"] == [2], "a citation to a passage never given is dropped"
    assert nodes["party"]["inferred"] is True and nodes["party"]["cites"] == []
    assert result["diagram"]["edges"][1]["cites"] == [2], "'[2]' is read as 2"
    assert result["grounding"] == {"cited": 5, "inferred": 2, "ratio": 5 / 7}
    assert result["citations"] == [1, 2, 3]


def test_a_mostly_inferred_diagram_is_declined() -> None:
    spec = _spec(
        nodes=[{"id": f"n{i}", "label": f"Step {i}", "cites": []} for i in range(4)],
        edges=[{"from": "n0", "to": "n1", "cites": [1]}],
    )
    result = visuals.validate_spec(spec, CITATIONS)

    assert result["status"] == "declined"
    assert "1 of 5 elements" in result["reason"]


@pytest.mark.parametrize(
    "spec",
    [
        {"nodes": [{"id": "only", "label": "One", "cites": [1]}]},
        {
            "nodes": [
                {"id": "bad id!", "label": "x", "cites": [1]},
                {"id": "a", "label": "", "cites": [1]},
            ]
        },
        {},
    ],
)
def test_a_spec_with_under_two_drawable_elements_is_declined(spec: dict) -> None:
    assert visuals.validate_spec(spec, CITATIONS)["status"] == "declined"


def test_edges_must_join_known_nodes_and_sizes_are_capped() -> None:
    nodes = [{"id": f"n{i}", "label": f"N{i}", "cites": [1]} for i in range(80)]
    edges = [{"from": "n0", "to": "ghost", "cites": [1]}, {"from": "n1", "to": "n1", "cites": [1]}]
    result = visuals.validate_spec({"nodes": nodes, "edges": edges}, CITATIONS)

    assert len(result["diagram"]["nodes"]) == visuals.MAX_NODES
    assert result["diagram"]["edges"] == []
    assert result["diagram"]["kind"] == "flow", "an unknown kind falls back to flow"


def test_labels_cannot_escape_the_mermaid_export() -> None:
    spec = _spec(
        nodes=[
            {"id": "a", "label": 'Say "hi"] --> x[<script>alert(1)</script>', "cites": [1]},
            {"id": "b", "label": "B|C;D", "cites": [2]},
        ],
        edges=[{"from": "a", "to": "b", "label": "go|now", "cites": [1]}],
    )
    mermaid = visuals.validate_spec(spec, CITATIONS)["mermaid"]

    assert mermaid.startswith("flowchart LR")
    assert "<script>" not in mermaid and "]" not in mermaid.split("\n")[1][len('    a["') : -2]
    assert "#quot;" in mermaid
    assert "|go now|" in mermaid


def test_a_sequence_diagram_exports_as_one() -> None:
    spec = _spec(kind="sequence")
    mermaid = visuals.validate_spec(spec, CITATIONS)["mermaid"]
    assert mermaid.splitlines()[0] == "sequenceDiagram"
    assert "participant build as Build" in mermaid


def test_with_no_model_the_diagram_is_the_graphs_own_edges() -> None:
    facts = [
        {
            "subject": "n1",
            "subject_id": "file:n1",
            "predicate": "imports",
            "object": "n2",
            "object_id": "file:n2",
        },
        {
            "subject": "n1",
            "subject_id": "file:n1",
            "predicate": "calls",
            "object": "helper",
            "object_id": "symbol:helper",
        },
    ]
    result = visuals.graph_diagram(CITATIONS, facts)

    assert result["status"] == "ok" and result["source"] == "graph"
    labels = {node["label"]: node for node in result["diagram"]["nodes"]}
    assert labels["n2"]["cites"] == [2], "a cited object keeps its own citation"
    assert labels["helper"]["cites"] == [1], "an uncited object inherits its subject's"
    assert result["grounding"]["inferred"] == 0


def test_a_model_reply_that_is_not_json_is_declined_not_drawn() -> None:
    class Broken(LLM):
        def __init__(self) -> None:
            super().__init__(provider="openai", api_key="k")

        def complete(self, system, prompt, **kwargs):  # type: ignore[override]
            return "Here is a lovely diagram: A -> B"

    result = visuals.build_diagram("draw it", CITATIONS, Broken(), prompt="passages")
    assert result == {
        "type": "diagram",
        "status": "declined",
        "reason": "the model did not return a diagram",
    }


def test_the_model_reads_the_same_evidence_the_answer_read() -> None:
    seen: dict[str, str] = {}

    class Drawer(LLM):
        def __init__(self) -> None:
            super().__init__(provider="openai", api_key="k")

        def complete(self, system, prompt, **kwargs):  # type: ignore[override]
            seen["system"], seen["prompt"] = system, prompt
            return "```json\n" + json.dumps(_spec()) + "\n```"

    result = visuals.build_diagram(
        "the release", CITATIONS, Drawer(), prompt="Passages: [1] build", kind="sequence"
    )

    assert result["status"] == "ok" and result["source"] == "model"
    assert seen["prompt"].startswith("Passages: [1] build")
    assert "Use kind: sequence." in seen["system"]


# ---------------------------------------------------------------------------
# create_visual over both surfaces
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def region(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("visuals")
    workspace = root / "workspace"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "release.md").write_text(
        "# Release process\n\nBuild the image, then run the tests, then ship it. "
        "See [the checklist](checklist.md).\n",
        encoding="utf-8",
    )
    (workspace / "pkg" / "checklist.md").write_text(
        "# Checklist\n\nTag the release and publish notes.\n", encoding="utf-8"
    )
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "visuals",
                "state_path": str(root / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(root / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "storage": {"graph_snapshots": False},
            "sources": [{"name": "docs", "type": "markdown_folder", "path": str(workspace)}],
        }
    )
    tools = PheasantTools(config)
    tools.engine.sync_source("docs", "full")
    tools.engine.reload_graph()
    client = TestClient(create_app(config, config_path=str(root / "pheasant.yaml")))
    return {"tools": tools, "client": client, "kb": config.knowledge_base_id}


def test_visualizing_a_passage_answers_identically_on_both_surfaces(region: dict) -> None:
    node = "file:docs:pkg/release.md:branch=none"
    body = {"request": "the release process", "node_ids": [node]}
    over_http = region["client"].post("/assistant/visual", json=body)
    over_mcp = region["tools"].create_visual(region["kb"], "the release process", node_ids=[node])

    assert over_http.status_code == 200, over_http.text
    assert over_http.json()["visual"] == over_mcp["visual"]
    assert [c["node_id"] for c in over_mcp["citations"]] == [node]
    assert over_mcp["citations"][0]["snippet"].startswith("# Release process")


def test_an_unknown_passage_is_refused_with_one_text(region: dict) -> None:
    body = {"request": "x", "node_ids": ["file:docs:nope.md:branch=none"]}
    response = region["client"].post("/assistant/visual", json=body)
    with pytest.raises(ServiceError) as refused:
        region["tools"].create_visual(region["kb"], "x", node_ids=body["node_ids"])

    assert response.status_code == 404
    assert (
        response.json()["detail"]
        == str(refused.value)
        == ("Unknown node: file:docs:nope.md:branch=none")
    )


@pytest.mark.parametrize(
    ("request_text", "node_ids", "fragment"),
    [("   ", [], "request must say what to draw"), ("x", ["n"] * 13, "at most 12 passages")],
)
def test_a_malformed_visual_request_is_refused(
    region: dict, request_text: str, node_ids: list[str], fragment: str
) -> None:
    with pytest.raises(ServiceError) as refused:
        region["tools"].create_visual(region["kb"], request_text, node_ids=node_ids)
    assert fragment in str(refused.value)


def test_a_passage_the_principal_may_not_read_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "secret.md").write_text("# Secret\n\nThe launch codes.\n", encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "acl-visuals",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / "exports"),
            },
            "security": {"acl_enforced": True, "default_visibility": "private"},
            "sources": [{"name": "docs", "type": "markdown_folder", "path": str(workspace)}],
        }
    )
    tools = PheasantTools(config)
    tools.engine.sync_source("docs", "full")

    # Under private visibility an un-ACL'd artifact is readable by any
    # authenticated principal and by no anonymous one (`security.acl`).
    with pytest.raises(ServiceError) as refused:
        tools.create_visual(
            config.knowledge_base_id, "draw it", node_ids=["file:docs:secret.md:branch=none"]
        )
    assert refused.value.code == "NOT_PERMITTED"
    allowed = tools.create_visual(
        config.knowledge_base_id,
        "draw it",
        node_ids=["file:docs:secret.md:branch=none"],
        principal="user:alice",
    )
    assert allowed["citations"][0]["relative_path"] == "secret.md"
