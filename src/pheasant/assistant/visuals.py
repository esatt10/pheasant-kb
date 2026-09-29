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
:func:`to_mermaid` is a text export for hosts that render Mermaid and for
copying out.

With no model connected, :func:`graph_diagram` draws what the index itself
recorded — the graph's own edges between the cited documents and what they
reference — which is grounded by construction and needs no network.
"""

from __future__ import annotations

import json
import re
from typing import Any

KINDS = ("flow", "sequence", "hierarchy", "concept", "timeline")
MAX_NODES = 30
MAX_EDGES = 60
MAX_LABEL = 80
#: Below this share of cited elements a diagram is declined. Half, not more:
#: a process diagram legitimately has a start and an end nobody wrote down,
#: but a diagram that is mostly inference is a guess with arrows.
MIN_GROUNDED = 0.5

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

DIAGRAM_SYSTEM = """You turn retrieved passages from a private knowledge base \
into a small diagram that answers the user's request. You draw ONLY what the \
passages support.

Reply with JSON only, no prose:
{"kind": "flow", "title": "short title",
 "nodes": [{"id": "n1", "label": "short label", "cites": [1], "group": ""}],
 "edges": [{"from": "n1", "to": "n2", "label": "", "cites": [2]}],
 "summary": "one sentence saying what the diagram shows"}

Rules:
- "kind": "flow" for a process or pipeline, "sequence" for actors exchanging \
messages over time (nodes are the actors, edges in order are the messages), \
"hierarchy" for part-of or containment, "concept" for how ideas relate, \
"timeline" for dated or ordered events.
- 3 to 15 nodes. Labels of at most six words, using the passages' own names \
for files, components, commands and steps.
- EVERY node and edge lists in "cites" the passage numbers [n] that support \
it. Never cite a number that was not given. If nothing supports an element, \
leave it out.
- Do not add steps, components or relationships the passages do not state."""


def build_diagram(
    request: str,
    citations: list[dict],
    llm: Any,
    *,
    prompt: str,
    kind: str | None = None,
    max_output_tokens: int = 1200,
) -> dict[str, Any]:
    """Ask the model for a spec and validate it. Never raises.

    ``prompt`` is the passage block the answering step already built — the
    diagram reads exactly the evidence the answer read.
    """

    hint = f"\nUse kind: {kind}." if kind in KINDS else ""
    raw = llm.try_complete(
        DIAGRAM_SYSTEM + hint,
        f"{prompt}\n\nDraw: {request}",
        max_output_tokens=max_output_tokens,
    )
    parsed = _parse_json(raw)
    if parsed is None:
        return declined("the model did not return a diagram")
    return validate_spec(parsed, citations, source="model")


def validate_spec(
    spec: dict[str, Any], citations: list[dict], *, source: str = "model"
) -> dict[str, Any]:
    """Check a spec against the citations it claims. Deterministic."""

    valid = {int(c["index"]) for c in citations if c.get("index") is not None}
    kind = str(spec.get("kind") or "flow").lower()
    if kind not in KINDS:
        kind = "flow"

    nodes: list[dict[str, Any]] = []
    known: set[str] = set()
    for raw_node in (spec.get("nodes") or [])[: MAX_NODES * 2]:
        if not isinstance(raw_node, dict):
            continue
        node_id = str(raw_node.get("id") or "").strip()
        label = _label(raw_node.get("label"))
        if not _ID_RE.match(node_id) or node_id in known or not label:
            continue
        cites = _cites(raw_node.get("cites"), valid)
        node = {"id": node_id, "label": label, "cites": cites, "inferred": not cites}
        group = _label(raw_node.get("group"), 40)
        if group:
            node["group"] = group
        nodes.append(node)
        known.add(node_id)
        if len(nodes) >= MAX_NODES:
            break

    edges: list[dict[str, Any]] = []
    for raw_edge in (spec.get("edges") or [])[: MAX_EDGES * 2]:
        if not isinstance(raw_edge, dict):
            continue
        source_id = str(raw_edge.get("from") or "").strip()
        target_id = str(raw_edge.get("to") or "").strip()
        if source_id not in known or target_id not in known or source_id == target_id:
            continue
        cites = _cites(raw_edge.get("cites"), valid)
        edges.append(
            {
                "from": source_id,
                "to": target_id,
                "label": _label(raw_edge.get("label"), 40),
                "cites": cites,
                "inferred": not cites,
            }
        )
        if len(edges) >= MAX_EDGES:
            break

    if len(nodes) < 2:
        return declined("fewer than two elements could be drawn from the passages")
    elements = nodes + edges
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
        "title": _label(spec.get("title"), 120) or "Diagram",
        "summary": _label(spec.get("summary"), 300),
        "nodes": nodes,
        "edges": edges,
    }
    return {
        "type": "diagram",
        "status": "ok",
        "source": source,
        "diagram": diagram,
        "citations": used,
        "grounding": {"cited": cited, "inferred": len(elements) - cited, "ratio": ratio},
        "mermaid": to_mermaid(diagram),
    }


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


def to_mermaid(diagram: dict[str, Any]) -> str:
    """The diagram as Mermaid text. Labels are escaped; nothing is executable."""

    nodes = diagram.get("nodes") or []
    edges = diagram.get("edges") or []
    if diagram.get("kind") == "sequence":
        lines = ["sequenceDiagram"]
        for node in nodes:
            lines.append(f"    participant {node['id']} as {_mermaid_text(node['label'])}")
        for edge in edges:
            arrow = "-->>" if edge.get("inferred") else "->>"
            label = _mermaid_text(edge.get("label") or " ")
            lines.append(f"    {edge['from']}{arrow}{edge['to']}: {label}")
        return "\n".join(lines)
    direction = "LR" if diagram.get("kind") in {"flow", "timeline"} else "TD"
    lines = [f"flowchart {direction}"]
    for node in nodes:
        lines.append(f'    {node["id"]}["{_mermaid_text(node["label"])}"]')
    for edge in edges:
        arrow = "-.->" if edge.get("inferred") else "-->"
        label = edge.get("label")
        if label:
            lines.append(f"    {edge['from']} {arrow}|{_mermaid_text(label)}| {edge['to']}")
        else:
            lines.append(f"    {edge['from']} {arrow} {edge['to']}")
    inferred = [node["id"] for node in nodes if node.get("inferred")]
    if inferred:
        lines.append("    classDef inferred stroke-dasharray: 4 3")
        lines.append(f"    class {','.join(inferred)} inferred")
    return "\n".join(lines)


def _mermaid_text(text: str) -> str:
    # Quotes, brackets, pipes and angle brackets are Mermaid syntax or HTML;
    # entity-encode the first and drop the rest rather than let a label close
    # a node early or smuggle markup into a renderer.
    cleaned = re.sub(r"[\[\]{}()<>|;`]", " ", str(text))
    return cleaned.replace('"', "#quot;").strip() or " "


def _label(value: Any, limit: int = MAX_LABEL) -> str:
    text = _CONTROL_RE.sub(" ", str(value or "")).strip()
    text = " ".join(text.split())
    return text[:limit]


def _cites(value: Any, valid: set[int]) -> list[int]:
    if not isinstance(value, list):
        value = [value] if value is not None else []
    out: list[int] = []
    for item in value:
        try:
            number = int(str(item).strip("[] "))
        except (TypeError, ValueError):
            continue
        if number in valid and number not in out:
            out.append(number)
    return out


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
