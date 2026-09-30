"""UML visuals: class, activity, state machine and use case diagrams.

They are four more kinds of the grounded spec grammar (``assistant.visual_uml``),
so everything ``tests/test_visual_shapes.py`` asserts holds for them too. What
UML adds, and what is asserted here:

* **pseudo-nodes are notation, not claims** — a start dot, an end bullseye, a
  fork bar or a choice diamond needs no label and no citation, and neither they
  nor the edges that only say where a flow begins or ends count toward the
  grounding share, in either direction;
* class members given as strings share their class's citations, and members
  given with their own ``cites`` are checked on their own;
* relationships use UML's names, and the everyday ones ("extends",
  "implements", "has", "includes") land on them;
* a state may transition to itself, and nothing else may;
* each exports as Mermaid's own diagram type, with the arrow pointing the way
  UML reads it;
* routing reads "a UML class diagram of", "a state machine of" and the rest.
"""

from __future__ import annotations

from typing import Any

import pytest

from pheasant.assistant import routing, visual_uml, visuals

CITATIONS = [{"index": i} for i in (1, 2, 3)]


def _check(kind: str, **spec: Any) -> dict:
    return visuals.validate_spec({"kind": kind, "title": kind, **spec}, CITATIONS)


# ---------------------------------------------------------------------------
# grounding
# ---------------------------------------------------------------------------


def test_pseudo_nodes_need_no_label_or_citation_and_do_not_count() -> None:
    result = _check(
        "activity",
        nodes=[
            {"id": "s", "type": "start"},
            {"id": "b", "label": "Build", "cites": [1]},
            {"id": "f", "type": "fork"},
            {"id": "u", "label": "Unit tests", "cites": [1]},
            {"id": "i", "label": "Integration tests", "cites": [1]},
            {"id": "j", "type": "join"},
            {"id": "e", "type": "end"},
        ],
        edges=[
            {"from": "s", "to": "b"},
            {"from": "b", "to": "f", "cites": [1]},
            {"from": "f", "to": "u", "cites": [1]},
            {"from": "f", "to": "i", "cites": [1]},
            {"from": "u", "to": "j", "cites": [1]},
            {"from": "i", "to": "j", "cites": [1]},
            {"from": "j", "to": "e"},
        ],
    )

    nodes = {n["id"]: n for n in result["diagram"]["nodes"]}
    assert nodes["s"]["type"] == "initial" and nodes["e"]["type"] == "final"
    assert nodes["s"]["label"] == "start" and nodes["f"]["label"] == "fork"
    assert all(nodes[i]["structural"] and not nodes[i]["inferred"] for i in "sfje")
    # 3 actions + the 5 edges between them; the start and end edges are notation.
    assert result["grounding"] == {"cited": 8, "inferred": 0, "ratio": 1.0}


def test_notation_cannot_carry_a_diagram_of_guesses() -> None:
    """With the pseudo-nodes left out, two uncited actions are what is left —
    and a diagram of those is declined, however much punctuation surrounds it."""

    result = _check(
        "activity",
        nodes=[
            {"id": "s", "type": "initial"},
            {"id": "a", "label": "Guess one"},
            {"id": "b", "label": "Guess two"},
            {"id": "e", "type": "final"},
        ],
        edges=[
            {"from": "s", "to": "a"},
            {"from": "a", "to": "b"},
            {"from": "b", "to": "e"},
        ],
    )
    assert result["status"] == "declined"
    assert "0 of 3 elements" in result["reason"]


def test_class_members_share_their_class_citations_unless_they_bring_their_own() -> None:
    result = _check(
        "class",
        nodes=[
            {
                "id": "rel",
                "label": "Release",
                "cites": [1],
                "stereotype": "«Abstract»",
                "attributes": ["version: string", {"text": "secret: key", "cites": [9]}],
                "operations": [{"text": "promote()", "cites": [2]}],
            }
        ],
    )

    (node,) = result["diagram"]["nodes"]
    assert node["stereotype"] == "abstract"
    assert node["attributes"] == [
        {"text": "version: string", "cites": [1], "inferred": False},
        {"text": "secret: key", "cites": [], "inferred": True},
    ]
    assert node["operations"] == [{"text": "promote()", "cites": [2], "inferred": False}]


@pytest.mark.parametrize(
    ("kind", "given", "relation"),
    [
        ("class", "extends", "inheritance"),
        ("class", "implements", "realization"),
        ("class", "has", "aggregation"),
        ("class", "owns", "composition"),
        ("class", "depends on", "dependency"),
        ("class", "teleports", "association"),
        ("usecase", "includes", "include"),
        ("usecase", "extend", "extend"),
        ("usecase", "inherits", "generalization"),
    ],
)
def test_relationships_land_on_umls_own_names(kind: str, given: str, relation: str) -> None:
    result = _check(
        kind,
        nodes=[{"id": "a", "label": "A", "cites": [1]}, {"id": "b", "label": "B", "cites": [1]}],
        edges=[{"from": "a", "to": "b", "relation": given, "cites": [1]}],
    )
    assert result["diagram"]["edges"][0]["relation"] == relation


def test_multiplicities_must_look_like_multiplicities() -> None:
    result = _check(
        "class",
        nodes=[{"id": "a", "label": "A", "cites": [1]}, {"id": "b", "label": "B", "cites": [1]}],
        edges=[
            {"from": "a", "to": "b", "from_mult": "1", "to_mult": "0..*", "cites": [1]},
            {"from": "b", "to": "a", "from_mult": "lots", "to_mult": '"]; x', "cites": [1]},
        ],
    )
    first, second = result["diagram"]["edges"]
    assert (first["from_mult"], first["to_mult"]) == ("1", "0..*")
    assert "from_mult" not in second and "to_mult" not in second


def test_a_state_may_transition_to_itself_and_nothing_else_may() -> None:
    loop = [{"from": "a", "to": "a", "trigger": "tick", "cites": [1]}]
    nodes = [
        {"id": "a", "label": "Waiting", "cites": [1]},
        {"id": "b", "label": "Done", "cites": [1]},
    ]

    state = _check("state", nodes=nodes, edges=loop)
    flow = _check("flow", nodes=nodes, edges=loop)

    assert [e["label"] for e in state["diagram"]["edges"]] == ["tick"]
    assert flow["diagram"]["edges"] == []


def test_a_transition_reads_trigger_guard_effect() -> None:
    result = _check(
        "state",
        nodes=[
            {"id": "c", "label": "Canary", "cites": [1], "do": "compare error rate"},
            {"id": "r", "label": "Rolled back", "cites": [2], "entry": "route back"},
        ],
        edges=[
            {
                "from": "c",
                "to": "r",
                "trigger": "breach",
                "guard": "[over budget]",
                "effect": "block release",
                "cites": [2],
            }
        ],
    )
    (edge,) = result["diagram"]["edges"]
    assert edge["label"] == "breach [over budget] / block release"
    assert result["diagram"]["nodes"][0]["do"] == "compare error rate"


# ---------------------------------------------------------------------------
# exports
# ---------------------------------------------------------------------------


def test_a_class_diagram_exports_with_arrows_pointing_the_way_uml_reads() -> None:
    result = _check(
        "class",
        nodes=[
            {"id": "rel", "label": "Release", "cites": [1], "attributes": ["version: string"]},
            {"id": "can", "label": "CanaryRelease", "cites": [1]},
            {"id": "img", "label": "SignedImage", "cites": [2], "stereotype": "entity"},
            {"id": "rb", "label": "Rollbackable", "cites": [2], "operations": ["rollBack(to)"]},
        ],
        edges=[
            {"from": "can", "to": "rel", "relation": "inheritance", "cites": [1]},
            {"from": "rel", "to": "rb", "relation": "realization", "cites": [2]},
            {
                "from": "rel",
                "to": "img",
                "relation": "composition",
                "from_mult": "1",
                "to_mult": "0..3",
                "label": "keeps",
                "cites": [2],
            },
        ],
    )
    text = result["mermaid"]

    assert text.startswith("classDiagram")
    assert "    rel <|-- can" in text, "the parent is on the arrowhead's side"
    assert "    rb <|.. rel" in text
    assert '    rel "1" *-- "0..3" img : keeps' in text
    assert "        version: string" in text and "        rollBack()" in text
    assert "        <<entity>>" in text


def test_a_state_machine_exports_its_pseudo_states_as_mermaids_own() -> None:
    result = _check(
        "state",
        nodes=[
            {"id": "s", "type": "initial"},
            {"id": "c", "label": "Canary", "cites": [1]},
            {"id": "q", "type": "choice"},
            {"id": "p", "label": "Promoted", "cites": [1]},
            {"id": "e", "type": "final"},
        ],
        edges=[
            {"from": "s", "to": "c"},
            {"from": "c", "to": "q", "trigger": "30 min", "cites": [1]},
            {"from": "q", "to": "p", "guard": "in budget", "cites": [1]},
            {"from": "p", "to": "e"},
        ],
    )
    text = result["mermaid"]
    assert text.splitlines()[0] == "stateDiagram-v2"
    assert "    [*] --> c" in text and "    p --> [*]" in text
    assert "    state q <<choice>>" in text
    assert "    q --> p : [in budget]" in text, "a guard keeps its brackets"


def test_activity_and_use_case_diagrams_export_as_flowcharts() -> None:
    activity = _check(
        "activity",
        nodes=[
            {"id": "s", "type": "initial"},
            {"id": "f", "type": "fork"},
            {"id": "a", "label": "Unit tests", "cites": [1]},
            {"id": "b", "label": "Integration tests", "cites": [1]},
        ],
        edges=[
            {"from": "s", "to": "f"},
            {"from": "f", "to": "a", "cites": [1]},
            {"from": "f", "to": "b", "cites": [1]},
        ],
    )["mermaid"]
    assert activity.startswith("flowchart TD") and "    class f bar" in activity

    usecase = _check(
        "usecase",
        groups=[{"id": "sys", "label": "Release system", "cites": [1]}],
        nodes=[
            {"id": "rm", "label": "Release manager", "type": "actor", "cites": [1]},
            {"id": "u1", "label": "Roll back", "cites": [1]},
            {"id": "u2", "label": "File review", "cites": [2]},
        ],
        edges=[
            {"from": "rm", "to": "u1", "cites": [1]},
            {"from": "u1", "to": "u2", "relation": "include", "cites": [2]},
        ],
    )["mermaid"]
    assert 'subgraph sys["Release system"]' in usecase
    assert "    rm --- u1" in usecase and "    u1 -.->|include| u2" in usecase


def test_uml_labels_cannot_escape_their_exports() -> None:
    hostile = 'x"] ; click a "javascript:alert(1)" |<script> {'
    for kind in visual_uml.UML_KINDS:
        result = _check(
            kind,
            nodes=[
                {"id": "a", "label": hostile, "cites": [1], "attributes": [hostile]},
                {"id": "b", "label": "B", "cites": [1]},
            ],
            edges=[{"from": "a", "to": "b", "label": hostile, "trigger": hostile, "cites": [1]}],
        )
        text = result["mermaid"]
        assert "<script>" not in text and "alert(1)" not in text and "|<" not in text, kind


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "shape"),
    [
        ("Draw a UML class diagram of the release domain", "class"),
        ("draw the domain model for billing", "class"),
        ("Draw an activity diagram of releasing a change", "activity"),
        ("Draw a state machine of a release", "state"),
        ("draw a behavior diagram of the canary", "state"),
        ("show the order lifecycle as a statechart", "state"),
        ("Draw a use case diagram of the release system", "usecase"),
        ("draw the use cases for the release dashboard", "usecase"),
    ],
)
def test_routing_reads_the_uml_diagram_a_question_names(question: str, shape: str) -> None:
    route = routing.route_question(question, intent=("knowledge", "rule"))
    assert (route.visual, route.shape) == ("diagram", shape)


@pytest.mark.parametrize(
    ("pin", "kind"),
    [
        ("class diagram", "class"),
        ("behavior", "state"),
        ("use case", "usecase"),
        ("activity", "activity"),
    ],
)
def test_a_uml_diagram_can_be_pinned_by_name(pin: str, kind: str) -> None:
    route = routing.route_question(
        "what is the release process", intent=("knowledge", "x"), visual=pin
    )
    assert (route.visual, route.shape) == ("diagram", kind)
