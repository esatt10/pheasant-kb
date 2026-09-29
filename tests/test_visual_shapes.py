"""Visual shapes: one grounded spec, fourteen ways to draw it.

A request names a shape ("a timeline of", "compare … as a table", "an org
chart of") or leaves it to the model, and the spec grammar in
``assistant.visual_specs`` checks every shape the same way: each element a
reader could take as a claim cites a passage or is marked ``inferred``. What
is asserted here:

* every kind survives validation with the fields it needs, and a kind or an
  everyday name for one ("org chart", "2x2", "venn") lands on the vocabulary;
* a shape the caller pinned beats the one the model answered in;
* a chart number no cited passage states is not a cited number;
* tables and lanes count toward grounding like nodes and edges do;
* the Mermaid (and Markdown) exports cannot be broken out of by a label;
* routing reads the shape off the question and does not mistake SQL for art;
* the model draws from the whole documents the answer read, not 500-character
  previews — the defect that turned a five-step process into three steps;
* a visual carries what it needs to be redrawn from the same passages, and a
  redraw over either surface is the same evidence in another shape;
* captions end where a reader expects, and an ``embeds`` fact reads forward.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.assistant import answering, routing, visual_export, visual_specs, visuals
from pheasant.assistant.chat import EDGE_PHRASES, EDGE_PHRASES_PASSIVE
from pheasant.assistant.llm import LLM
from pheasant.config.schema import PheasantConfig
from pheasant.graph.figures import tidy_caption, with_full_captions
from pheasant.mcp_server.tools import PheasantTools

CITATIONS = [{"index": i, "node_id": f"file:n{i}", "title": f"n{i}"} for i in (1, 2, 3)]


def _node(node_id: str, label: str, cites: list[int], **extra: Any) -> dict:
    return {"id": node_id, "label": label, "cites": cites, **extra}


def _chain(ids: list[str]) -> list[dict]:
    return [{"from": a, "to": b, "cites": [1]} for a, b in zip(ids, ids[1:], strict=False)]


#: One representative spec per kind, each using the fields its shape reads.
SPECS: dict[str, dict] = {
    "flow": {
        "nodes": [_node("a", "Build", [1], shape="box"), _node("b", "Ok?", [2], shape="diamond")],
        "edges": _chain(["a", "b"]),
    },
    "sequence": {
        "nodes": [_node("c", "Client", [1]), _node("s", "Server", [2])],
        "edges": [{"from": "c", "to": "s", "label": "hello", "cites": [1]}],
    },
    "hierarchy": {
        "nodes": [_node("r", "Platform", [1]), _node("x", "Search", [2]), _node("y", "Sync", [3])],
        "edges": [{"from": "r", "to": "x", "cites": [2]}, {"from": "r", "to": "y", "cites": [3]}],
    },
    "mindmap": {
        "nodes": [_node("r", "Releases", [1]), _node("x", "Canary", [2])],
        "edges": [{"from": "r", "to": "x", "cites": [2]}],
    },
    "concept": {
        "nodes": [_node("a", "Router", [1]), _node("b", "Region", [2])],
        "edges": [{"from": "a", "to": "b", "label": "routes to", "cites": [1]}],
    },
    "cycle": {"nodes": [_node("p", "Plan", [1]), _node("d", "Do", [2]), _node("k", "Check", [3])]},
    "timeline": {
        "nodes": [
            _node("v1", "First release", [1], when="2024"),
            _node("v2", "Rollbacks", [2], when="2025"),
        ]
    },
    "swimlane": {
        "groups": [
            {"id": "dev", "label": "Developers", "cites": [1]},
            {"id": "ops", "label": "Ops"},
        ],
        "nodes": [_node("a", "Merge", [1], group="dev"), _node("b", "Deploy", [2], group="ops")],
        "edges": _chain(["a", "b"]),
    },
    "layers": {
        "groups": [
            {"id": "ui", "label": "UI", "cites": [1]},
            {"id": "db", "label": "Data", "cites": [2]},
        ],
        "nodes": [
            _node("a", "React app", [1], group="ui"),
            _node("b", "Postgres", [2], group="db"),
        ],
    },
    "groups": {
        "groups": [{"id": "g", "label": "Stores", "cites": [1]}],
        "nodes": [
            _node("a", "SQLite", [1], group="Stores"),
            _node("b", "Postgres", [2], group="g"),
        ],
    },
    "table": {
        "nodes": [_node("sq", "SQLite", [1]), _node("pg", "Postgres", [2])],
        "columns": [{"id": "scale", "label": "Scales to"}, "Needs"],
        "cells": [
            {"row": "sq", "column": "scale", "text": "one host", "cites": [1]},
            {"row": "pg", "column": "scale", "text": "a fleet", "cites": [2]},
            {"row": "pg", "column": "Needs", "text": "a server", "cites": [2]},
        ],
    },
    "quadrant": {
        "axes": {"x": {"label": "Effort", "low": "low", "high": "high"}, "y": {"label": "Risk"}},
        "nodes": [
            _node("a", "Canary", [1], x=0.2, y=0.3),
            _node("b", "Big bang", [2], x=0.9, y=0.95),
        ],
    },
    "chart": {
        "chart": "line",
        "unit": "%",
        "nodes": [_node("a", "eu-west", [1], value=0.5), _node("b", "us-east", [2], value="1,200")],
    },
    "canvas": {
        "nodes": [
            _node("a", "Store", [1], x=10, y=20, shape="cylinder"),
            _node("b", "Worker", [2], x=80, y=70, shape="hexagon"),
        ],
        "edges": _chain(["a", "b"]),
    },
}


def test_the_vocabulary_is_the_set_of_specs_this_test_draws() -> None:
    assert set(SPECS) == set(visual_specs.KINDS)


@pytest.mark.parametrize("kind", visual_specs.KINDS)
def test_every_kind_validates_with_the_fields_its_shape_reads(kind: str) -> None:
    result = visuals.validate_spec({"kind": kind, "title": kind, **SPECS[kind]}, CITATIONS)

    assert result["status"] == "ok", result
    diagram = result["diagram"]
    assert diagram["kind"] == kind
    expected = {
        "swimlane": "groups",
        "layers": "groups",
        "groups": "groups",
        "table": "cells",
        "quadrant": "axes",
        "chart": "chart",
    }.get(kind)
    if expected:
        assert diagram[expected], f"{kind} lost its {expected}"
    if kind == "timeline":
        assert [n["when"] for n in diagram["nodes"]] == ["2024", "2025"]
    if kind == "canvas":
        assert diagram["nodes"][0]["x"] == pytest.approx(0.1)
        assert diagram["nodes"][1]["shape"] == "hexagon"
    if kind == "chart":
        assert [n["value"] for n in diagram["nodes"]] == [0.5, 1200.0]
    if kind == "groups":
        # A node may name its category by label as well as by id.
        assert {n["group"] for n in diagram["nodes"]} == {"g"}
    # Where Mermaid has the shape, the export exists; a table exports Markdown.
    assert (result["mermaid"] is None) == (kind == "table")
    assert ("markdown" in result) == (kind == "table")


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("org chart", "hierarchy"),
        ("Venn", "groups"),
        ("2x2", "quadrant"),
        ("bar chart", "chart"),
        ("mind_map", "mindmap"),
        ("Swim Lane", "swimlane"),
        ("freeform", "canvas"),
        ("TIMELINE", "timeline"),
        ("a hologram", None),
        (None, None),
    ],
)
def test_everyday_names_land_on_the_vocabulary(name: Any, kind: str | None) -> None:
    assert visual_specs.normalize_kind(name) == kind


def test_an_unknown_kind_is_drawn_as_a_flow_rather_than_refused() -> None:
    result = visuals.validate_spec({"kind": "hologram", **SPECS["flow"]}, CITATIONS)
    assert result["diagram"]["kind"] == "flow"


class _Drawer(LLM):
    """Answers every drawing request with ``reply`` and remembers what it was asked."""

    def __init__(self, reply: dict) -> None:
        super().__init__(provider="openai", api_key="k")
        self.reply = reply
        self.seen: list[tuple[str, str]] = []

    def complete(self, system, prompt, **kwargs):  # type: ignore[override]
        self.seen.append((system, prompt))
        return json.dumps(self.reply)


def test_a_pinned_shape_beats_the_one_the_model_answered_in() -> None:
    drawer = _Drawer({"kind": "flow", **SPECS["timeline"]})
    result = visuals.build_diagram(
        "the releases", CITATIONS, drawer, prompt="[1] x", kind="chronology"
    )

    assert result["diagram"]["kind"] == "timeline"
    assert "Use kind: timeline." in drawer.seen[0][0]


def test_the_prompt_teaches_every_kind_and_the_viewpoint() -> None:
    for kind in visual_specs.KINDS:
        assert f'"{kind}"' in visuals.DIAGRAM_SYSTEM, f"{kind} is not described to the model"
    assert "viewpoint" in visuals.DIAGRAM_SYSTEM
    result = visuals.validate_spec(
        {"kind": "flow", "viewpoint": "for a new engineer", **SPECS["flow"]}, CITATIONS
    )
    assert result["diagram"]["viewpoint"] == "for a new engineer"


def test_a_chart_value_no_cited_passage_states_is_not_a_cited_value() -> None:
    spec = {
        "kind": "chart",
        "nodes": [
            _node("a", "eu-west", [1], value=0.5),
            _node("b", "us-east", [2], value=1200),
            _node("c", "ap-south", [3], value=7),
            _node("d", "moon", [1], value="n/a"),
        ],
    }
    evidence = {1: "an error budget of 0.5%", 2: "about 1,200 requests", 3: "seventy"}

    result = visuals.validate_spec(spec, CITATIONS, evidence=evidence)

    nodes = {n["id"]: n for n in result["diagram"]["nodes"]}
    assert "d" not in nodes, "a point with no number is not a point on a chart"
    assert nodes["a"]["cites"] == [1] and nodes["b"]["cites"] == [2]
    assert nodes["c"]["inferred"] and nodes["c"]["unverified_value"]
    # Without the passages' text there is nothing to check against.
    unchecked = visuals.validate_spec(spec, CITATIONS)
    assert {n["id"]: n["cites"] for n in unchecked["diagram"]["nodes"]}["c"] == [3]


def test_table_cells_are_claims_and_count_toward_grounding() -> None:
    spec = {
        **SPECS["table"],
        "kind": "table",
        "cells": [
            *SPECS["table"]["cells"],
            {"row": "sq", "column": "Needs", "text": "nothing", "cites": [9]},
            {"row": "ghost", "column": "scale", "text": "x", "cites": [1]},
            {"row": "sq", "column": "missing", "text": "x", "cites": [1]},
            {"row": "sq", "column": "scale", "text": "a second value", "cites": [1]},
        ],
    }
    result = visuals.validate_spec(spec, CITATIONS)

    cells = result["diagram"]["cells"]
    assert len(cells) == 4, "unknown rows, unknown columns and a second value per cell are dropped"
    assert cells[-1]["inferred"] is True
    # 2 rows + 4 cells, one of them uncited.
    assert result["grounding"] == {"cited": 5, "inferred": 1, "ratio": 5 / 6}


def test_a_table_of_one_cell_is_declined() -> None:
    spec = {**SPECS["table"], "kind": "table", "cells": SPECS["table"]["cells"][:1]}
    result = visuals.validate_spec(spec, CITATIONS)
    assert result["status"] == "declined" and "cells" in result["reason"]


def test_lanes_are_claims_too() -> None:
    spec = {**SPECS["swimlane"], "kind": "swimlane"}
    result = visuals.validate_spec(spec, CITATIONS)
    lanes = {g["id"]: g for g in result["diagram"]["groups"]}
    assert lanes["ops"]["inferred"] is True, "a lane nothing supports is marked like a node"


@pytest.mark.parametrize("kind", ["flow", "mindmap", "timeline", "quadrant", "chart", "swimlane"])
def test_labels_cannot_escape_any_export(kind: str) -> None:
    hostile = 'x"] ; click a href "javascript:alert(1)" |<script>'
    spec = json.loads(json.dumps(SPECS[kind]))
    spec["nodes"][0]["label"] = hostile
    result = visuals.validate_spec({"kind": kind, "title": hostile, **spec}, CITATIONS)
    text = result["mermaid"]

    assert "<script>" not in text and "alert(1)" not in text and "|<" not in text
    # The label reaches the export only in its sanitized spelling, whatever
    # the grammar around it.
    sanitized = visual_export.mermaid_text(hostile)
    plain = visual_export._plain(hostile)
    assert sanitized in text or plain in text, text
    for character in '[]{}()<>|;`"':
        assert character not in sanitized.replace("#quot;", "")
        assert character not in plain


def test_exports_speak_each_shapes_mermaid() -> None:
    def export(kind: str) -> str:
        return visuals.validate_spec({"kind": kind, "title": "T", **SPECS[kind]}, CITATIONS)[
            "mermaid"
        ]

    assert export("mindmap").startswith("mindmap\n  root((Releases))\n    Canary")
    assert "    2024 : First release" in export("timeline")
    assert "quadrantChart" in export("quadrant") and "Canary: [0.20, 0.30]" in export("quadrant")
    assert export("chart").splitlines()[-1] == "    line [0.5, 1200]"
    swimlane = export("swimlane")
    assert 'subgraph dev["Developers"]' in swimlane and "a --> b" in swimlane
    assert '{"Ok?"}' in export("flow"), "a decision is a diamond"
    cycle = export("cycle")
    assert "k --> p" in cycle, "a cycle with no edges closes its loop"


def test_a_table_exports_as_markdown_with_its_citations() -> None:
    result = visuals.validate_spec({"kind": "table", **SPECS["table"]}, CITATIONS)
    table = visual_export.to_markdown(result["diagram"])
    assert table.splitlines()[0] == "| | Scales to | Needs |"
    assert "| Postgres [2] | a fleet [2] | a server [2] |" in table
    assert "| SQLite [1] | one host [1] |  |" in table


# ---------------------------------------------------------------------------
# routing: the shape a question names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "visual", "shape"),
    [
        ("Draw a diagram of the release process", "diagram", "flow"),
        ("Make a timeline of the incidents", "diagram", "timeline"),
        ("Create a table comparing the three rollout options", "diagram", "table"),
        ("show it as a mind map", "diagram", "mindmap"),
        ("Give me an org chart of the teams", "diagram", "hierarchy"),
        ("Plot the error budgets by service", "diagram", "chart"),
        ("draw a bar chart of error rates per region", "diagram", "chart"),
        ("draw a sequence diagram of the handshake", "diagram", "sequence"),
        ("Visualize the release lifecycle", "diagram", "cycle"),
        ("turn this into a 2x2 of risk vs effort", "diagram", "quadrant"),
        ("draw the architecture as layers", "diagram", "layers"),
        ("draw who does what in a release as swim lanes", "diagram", "swimlane"),
        ("draw a venn of the overlapping features", "diagram", "groups"),
        ("draw a freeform picture of how the parts fit", "diagram", "canvas"),
        ("draw a concept map of retrieval", "diagram", "concept"),
        ("visualize how retrieval works", "diagram", None),
        # Shape words that are not requests for a picture.
        ("how do I create a table in postgres", "none", None),
        ("what is the history of the router", "none", None),
        ("which layers does the cache sit between", "none", None),
        ("show me the deploy topology image", "image", None),
    ],
)
def test_routing_reads_the_shape_a_question_names(
    question: str, visual: str, shape: str | None
) -> None:
    route = routing.route_question(question, intent=("knowledge", "rule"))
    assert (route.visual, route.shape) == (visual, shape)


def test_a_shape_named_in_the_visual_pin_is_a_diagram_in_that_shape() -> None:
    route = routing.route_question(
        "what are the teams", intent=("knowledge", "x"), visual="org chart"
    )
    assert (route.visual, route.shape, route.decided_by["visual"]) == (
        "diagram",
        "hierarchy",
        "pinned",
    )
    assert route.as_dict()["shape"] == "hierarchy"


# ---------------------------------------------------------------------------
# the drawing reads whole documents, and a visual can be redrawn
# ---------------------------------------------------------------------------

STEPS = "\n".join(
    f"{n}. **{name}** — " + "the release pipeline does this carefully and records it. " * 7
    for n, name in enumerate(["Build", "Test", "Stage", "Canary", "Promote"], start=1)
)


@pytest.fixture(scope="module")
def region(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("shapes")
    workspace = root / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "images").mkdir()
    (workspace / "docs" / "release.md").write_text(
        f"# Release process\n\nEvery release moves through five gates.\n\n{STEPS}\n\n"
        "![Pipeline](../images/pipeline.png)\n",
        encoding="utf-8",
    )
    (workspace / "images" / "pipeline.png").write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
        )
    )
    caption = (
        "Release pipeline diagram: Build then Test then Stage then Canary then Promote to "
        "every region; a canary that breaches its error budget branches to an automatic "
        "rollback, and the release is blocked until an incident review is filed by the "
        "owning team."
    )
    (workspace / "images" / "pipeline.png.caption.txt").write_text(caption, encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "shapes",
                "state_path": str(root / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(root / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "storage": {"graph_snapshots": False},
            "sources": [
                {
                    "name": "docs",
                    "type": "document_folder",
                    "path": str(workspace),
                    "include": ["**/*.md", "**/*.png"],
                }
            ],
        }
    )
    tools = PheasantTools(config)
    tools.engine.sync_source("docs", "full")
    tools.engine.reload_graph()
    client = TestClient(create_app(config, config_path=str(root / "pheasant.yaml")))
    return {"tools": tools, "client": client, "kb": config.knowledge_base_id, "caption": caption}


RELEASE = "file:docs:docs/release.md:branch=none"


def _drawing(nodes: list[str]) -> dict:
    return {
        "kind": "flow",
        "title": "Release",
        "nodes": [_node(f"s{i}", name, [1]) for i, name in enumerate(nodes)],
        "edges": _chain([f"s{i}" for i in range(len(nodes))]),
    }


def test_the_drawing_reads_the_whole_document_not_a_preview(
    region: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    drawer = _Drawer(_drawing(["Build", "Promote"]))
    monkeypatch.setattr(answering, "resolve_llm", lambda *a, **k: drawer)

    result = region["tools"].create_visual(region["kb"], "the release process", node_ids=[RELEASE])

    assert result["visual"]["status"] == "ok"
    prompt = drawer.seen[0][1]
    # Step 5 starts well past the 500-character search preview (and past the
    # 1,500-character snippet a named passage carries).
    assert STEPS.index("5. **Promote**") > 1500
    assert "**Promote**" in prompt, "the model was shown a preview, not the document"


def test_a_visual_carries_its_redraw_and_a_redraw_is_the_same_evidence(
    region: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    drawer = _Drawer(_drawing(["Build", "Test", "Promote"]))
    monkeypatch.setattr(answering, "resolve_llm", lambda *a, **k: drawer)
    first = region["tools"].create_visual(region["kb"], "the release", node_ids=[RELEASE])
    redraw = first["visual"]["redraw"]

    assert redraw["node_ids"] == [RELEASE] and redraw["knowledge_base"] == region["kb"]
    assert set(redraw["kinds"]) == set(visual_specs.KINDS)

    drawer.reply = {"kind": "flow", **SPECS["timeline"]}
    body = {"request": redraw["request"], "node_ids": redraw["node_ids"], "kind": "timeline"}
    over_http = region["client"].post("/assistant/visual", json=body).json()
    over_mcp = region["tools"].create_visual(region["kb"], **body)

    assert over_http["visual"] == over_mcp["visual"]
    assert over_mcp["visual"]["diagram"]["kind"] == "timeline"
    assert [c["node_id"] for c in over_mcp["citations"]] == [
        c["node_id"] for c in first["citations"]
    ], "a redraw may change the shape, never the evidence"


def test_a_pinned_shape_with_no_model_says_what_it_drew_instead(region: dict) -> None:
    result = region["tools"].create_visual(
        region["kb"], "the release", node_ids=[RELEASE], kind="timeline"
    )
    visual = result["visual"]
    assert visual["source"] == "graph" and visual["diagram"]["kind"] == "concept"
    assert "timeline needs a connected model" in visual["note"]


def test_figure_captions_are_the_images_whole_text(region: dict) -> None:
    result = region["tools"].create_visual(
        region["kb"], "the pipeline", node_ids=[RELEASE], kind="image"
    )
    (figure,) = result["figures"]
    assert figure["caption"] == region["caption"], "not the graph's 180-character summary"


def test_a_caption_cut_upstream_ends_at_a_word_and_says_so() -> None:
    text = "Release pipeline: Build then Test then Stage then Canary then Promote (all regions); a"
    assert tidy_caption(text[:70], truncated=True).endswith("then …")
    assert tidy_caption(text, truncated=True) == (
        "Release pipeline: Build then Test then Stage then Canary then Promote (all regions) …"
    )
    assert tidy_caption("A short caption.") == "A short caption."
    assert tidy_caption("word " * 200).endswith(" …") and len(tidy_caption("word " * 200)) <= 402


def test_captions_fall_back_to_the_tidied_summary_without_a_state_store() -> None:
    figures = [{"node_id": "x", "caption": "kept"}]
    assert with_full_captions(None, figures) == figures


def test_an_embeds_fact_reads_forward_both_ways() -> None:
    assert EDGE_PHRASES["embeds"] == "shows the image"
    assert EDGE_PHRASES_PASSIVE["embeds"] == "shown in"


def test_every_visual_path_can_be_given_the_documents(tmp_path: Path) -> None:
    """``visual_documents`` needs only a state store, so a deferred visual and
    an on-demand one read what the answer read without a retriever of their own."""

    assert answering.visual_documents([], state=object(), knowledge_base="kb") == {}
    assert answering.visual_documents(CITATIONS, state=None, knowledge_base="kb") == {}
