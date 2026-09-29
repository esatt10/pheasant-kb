"""Grounded visuals: a diagram every box and arrow of which can say where it came from.

A user asks for "a diagram of the release process" or "a visual that explains
how retrieval works". The obvious implementation — ask the model for Mermaid
or SVG and render it — has two defects this module exists to avoid:

* **It cannot be checked.** A picture is a set of claims ("A feeds B", "C
  happens after D"), and a picture whose claims cannot be traced is the
  ungrounded answer the rest of the assistant refuses to give, drawn instead
  of written.
* **It is markup built from model output**, which the UI's answer renderer
  has never done and must not start doing.

So the model returns a small **diagram spec** — nodes and edges, each carrying
the passage numbers (``cites``) that support it — and :func:`validate_spec`
checks it the way ``verify_node`` checks ``[n]`` markers: a citation that does
not resolve is dropped, an element with no surviving citation is kept but
marked ``inferred`` (the renderers draw it dashed), and a diagram that is
mostly inference is **declined** with the reason, because a confident picture
of guesses is worse than no picture. Rendering is the viewer's job, from data;
``assistant.visual_export`` turns a spec into Mermaid (or a Markdown table)
for hosts that render those and for copying out.

The spec is not one picture type. A process is a flow, but "a timeline of
the incidents", "compare the three options", "an org chart of the teams" or
"plot the error budgets" are different shapes of the same evidence, and
``assistant.visual_specs`` is their shared grammar: fourteen kinds over one
core of cited nodes and edges, down to a free ``canvas`` for anything the
named kinds do not cover. The request picks the shape (or pins it), and the
same passages can be redrawn in another one without a new search.

With no model connected, :func:`graph_diagram` draws what the index itself
recorded — the graph's own edges between the cited documents and what they
reference — which is grounded by construction and needs no network.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pheasant.assistant import visual_specs
from pheasant.assistant.visual_export import to_markdown, to_mermaid
from pheasant.assistant.visual_specs import KINDS, MAX_NODES, normalize_kind

__all__ = ["KINDS", "MAX_NODES", "build_diagram", "declined", "graph_diagram", "validate_spec"]

#: Below this share of cited elements a diagram is declined. Half, not more:
#: a process diagram legitimately has a start and an end nobody wrote down,
#: but a diagram that is mostly inference is a guess with arrows.
MIN_GROUNDED = 0.5

DIAGRAM_SYSTEM = """You turn retrieved passages from a private knowledge base \
into ONE visual that answers the user's request, in whatever shape or from \
whatever viewpoint they asked for. You draw ONLY what the passages support.

Reply with JSON only, no prose:
{"kind": "flow", "title": "short title", "viewpoint": "",
 "summary": "one sentence saying what the visual shows",
 "nodes": [{"id": "n1", "label": "short label", "cites": [1]}],
 "edges": [{"from": "n1", "to": "n2", "label": "", "cites": [2]}]}

Kinds — pick the one the request names or implies; if none fits, use \
"canvas" and place the nodes yourself:
- "flow": a process or pipeline. Node "shape" may be box, round, pill, \
diamond (a decision), cylinder (a store), hexagon, ellipse, note.
- "sequence": actors exchanging messages; nodes are actors, edges IN ORDER \
are the messages.
- "hierarchy": a tree (part-of, reports-to, breakdown); edges parent->child.
- "mindmap": the FIRST node is the central idea; edges parent->child.
- "concept": how ideas relate, as a network; any labelled edges.
- "cycle": a loop; nodes in loop order (edges optional).
- "timeline": ordered events; each node has "when" (a date or phase).
- "swimlane": a process across owners; "groups": [{"id": "g1", "label": \
"Team", "cites": [1]}] and each node has "group": "g1"; edges as in flow.
- "layers": a stack, top layer first; "groups" are the layers, nodes sit \
in them via "group".
- "groups": things sorted into categories; "groups" are the categories.
- "table": a comparison; nodes are the rows, "columns": [{"id": "c1", \
"label": "..."}], "cells": [{"row": "n1", "column": "c1", "text": "...", \
"cites": [1]}].
- "quadrant": a 2x2; "axes": {"x": {"label": "", "low": "", "high": ""}, \
"y": {...}} and each node has "x" and "y" between 0 and 1.
- "chart": numbers stated in the passages; each node has a numeric "value"; \
"chart": "bar" or "line"; optional "unit" and "axes": {"y": {"label": ""}}.
- "canvas": anything else; each node has "x" and "y" from 0 to 100 and a \
"shape".

Rules:
- A "viewpoint" the user asks for ("for a new engineer", "from the \
operator's side") decides what you include and how you label it; say it in \
"viewpoint".
- 3 to 15 nodes. Labels of at most six words, using the passages' own names \
for files, components, commands and steps. Node "detail" may add one short \
sentence.
- EVERY node, edge, group and cell lists in "cites" the passage numbers [n] \
that support it. Never cite a number that was not given. If nothing \
supports an element, leave it out.
- A chart value must be a number the cited passage states; never compute \
or estimate one.
- Do not add steps, components, relationships or numbers the passages do \
not state."""


def build_diagram(
    request: str,
    citations: list[dict],
    llm: Any,
    *,
    prompt: str,
    kind: str | None = None,
    evidence: dict[int, str] | None = None,
    max_output_tokens: int = 1600,
) -> dict[str, Any]:
    """Ask the model for a spec and validate it. Never raises.

    ``prompt`` is the passage block the answering step already built — the
    visual reads exactly the evidence the answer read. ``kind`` pins a shape
    (a vocabulary kind or an alias such as "org chart"); without it the model
    picks the one the request implies. ``evidence`` (passage number → text)
    lets chart values be checked against what the passages state.
    """

    pinned = normalize_kind(kind)
    hint = f"\nUse kind: {pinned}." if pinned else ""
    raw = llm.try_complete(
        DIAGRAM_SYSTEM + hint,
        f"{prompt}\n\nDraw: {request}",
        max_output_tokens=max_output_tokens,
    )
    parsed = _parse_json(raw)
    if parsed is None:
        return declined("the model did not return a diagram")
    if pinned:
        # The caller asked for this shape; a model that answered in another
        # one does not get to overrule the reader.
        parsed["kind"] = pinned
    return validate_spec(parsed, citations, source="model", evidence=evidence)


def validate_spec(
    spec: dict[str, Any],
    citations: list[dict],
    *,
    source: str = "model",
    evidence: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Check a spec against the citations it claims. Deterministic."""

    valid = {int(c["index"]) for c in citations if c.get("index") is not None}
    kind = normalize_kind(spec.get("kind")) or "flow"
    checked = visual_specs.check(spec, valid, kind=kind, evidence=evidence)
    if isinstance(checked, str):
        return declined(checked)

    elements = visual_specs.elements(checked)
    cited = sum(1 for element in elements if not element["inferred"])
    ratio = cited / len(elements)
    if ratio < MIN_GROUNDED:
        return declined(
            f"only {cited} of {len(elements)} elements could be tied to a passage",
            grounding={"cited": cited, "inferred": len(elements) - cited, "ratio": ratio},
        )
    used = sorted({n for element in elements for n in element["cites"]})
    diagram = {
        "kind": kind,
        "title": visual_specs.label(spec.get("title"), 120) or "Diagram",
        "summary": visual_specs.label(spec.get("summary"), 300),
        **{key: value for key, value in checked.items() if key != "kind"},
    }
    viewpoint = visual_specs.label(spec.get("viewpoint"), 80)
    if viewpoint:
        diagram["viewpoint"] = viewpoint
    visual: dict[str, Any] = {
        "type": "diagram",
        "status": "ok",
        "source": source,
        "diagram": diagram,
        "citations": used,
        "grounding": {"cited": cited, "inferred": len(elements) - cited, "ratio": ratio},
        "mermaid": to_mermaid(diagram),
    }
    markdown = to_markdown(diagram)
    if markdown:
        visual["markdown"] = markdown
    return visual


def declined(reason: str, **extra: Any) -> dict[str, Any]:
    """A visual that was asked for and deliberately not drawn."""

    return {"type": "diagram", "status": "declined", "reason": reason, **extra}


def graph_diagram(citations: list[dict], facts: list[dict]) -> dict[str, Any]:
    """A concept map of what the index recorded between the cited sources.

    The model-free path. Every edge is a graph edge the sync wrote, so the
    diagram is grounded by construction: a fact whose subject is a cited
    document cites that document; objects inherit the citation of the fact
    that reached them.
    """

    by_node = {c.get("node_id"): int(c["index"]) for c in citations if c.get("node_id")}
    ids: dict[str, str] = {}
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    def node_for(graph_id: str, label: str, cite: int | None) -> str:
        if graph_id in ids:
            return ids[graph_id]
        local = f"n{len(ids) + 1}"
        ids[graph_id] = local
        nodes.append({"id": local, "label": label, "cites": [cite] if cite else []})
        return local

    for fact in facts:
        cite = by_node.get(fact.get("subject_id"))
        if cite is None:
            continue
        source = node_for(str(fact["subject_id"]), str(fact.get("subject") or ""), cite)
        target = node_for(
            str(fact["object_id"]), str(fact.get("object") or ""), by_node.get(fact["object_id"])
        )
        edges.append(
            {"from": source, "to": target, "label": fact.get("predicate") or "", "cites": [cite]}
        )
    # An object reached only through a cited subject is supported by that
    # subject's passage: it is the thing the passage's document references.
    for node in nodes:
        if not node["cites"]:
            node["cites"] = sorted(
                {c for edge in edges if edge["to"] == node["id"] for c in edge["cites"]}
            )
    spec = {
        "kind": "concept",
        "title": "What the cited sources connect to",
        "summary": "Relationships the index recorded between the cited sources.",
        "nodes": nodes,
        "edges": edges,
    }
    return validate_spec(spec, citations, source="graph")


def _parse_json(raw: str | None) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None
