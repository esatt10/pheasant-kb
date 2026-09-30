"""The visual vocabulary: what shapes a grounded visual can take, and how each is checked.

A visual is a **spec**, never markup (``assistant.visuals`` says why). This
module is the spec's grammar. One core — ``nodes`` and ``edges``, every one
carrying the passage numbers that support it — and a few fields that only
some shapes use:

========== =============================================================
kind       what it draws, and the fields beyond nodes/edges it reads
========== =============================================================
flow       a process or pipeline, left to right (``shape`` per node)
sequence   actors exchanging messages in order; edges are the messages
hierarchy  part-of / reports-to / breakdown, as a tree; edges parent→child
mindmap    one idea radiating out; the first node is the centre
concept    how ideas relate, as a network; any edges
cycle      a loop that comes back to its start; node order is the loop
timeline   dated or ordered events; ``when`` per node
swimlane   a process across owners; ``groups`` are the lanes
layers     a stack (architecture, OSI-style); ``groups`` top to bottom
groups     things sorted into categories; ``groups`` are the categories
table      a comparison; nodes are rows, ``columns`` + ``cells``
quadrant   a 2x2 positioning; ``axes`` + ``x``/``y`` (0..1) per node
chart      numbers from the passages; ``value`` per node, ``chart`` bar|line
canvas     anything else: ``x``/``y`` (0..100) and ``shape`` per node
class      UML classes, members and relationships (``assistant.visual_uml``)
activity   UML activity: actions, decisions, fork/join, guards, partitions
state      UML state machine: states, pseudo-states, event [guard] / effect
usecase    UML use cases: actors, use cases, system boundary, include/extend
========== =============================================================

Every element a reader could take as a claim — a node, an edge, a lane, a
table cell — carries ``cites``. An element whose citations do not resolve is
kept and marked ``inferred``; a spec that is mostly inference is declined by
the caller. Numbers get one check more: a chart value that appears in none of
its cited passages is not a cited number, whatever the model says it cites.

Pure functions over plain data; no model, no I/O.
"""

from __future__ import annotations

import re
from typing import Any

from pheasant.assistant import visual_uml

KINDS = (
    "flow",
    "sequence",
    "hierarchy",
    "mindmap",
    "concept",
    "cycle",
    "timeline",
    "swimlane",
    "layers",
    "groups",
    "table",
    "quadrant",
    "chart",
    "canvas",
    # UML (``assistant.visual_uml``): class, activity, state machine, use case.
    "class",
    "activity",
    "state",
    "usecase",
)

#: The words people use for a shape, mapped onto the vocabulary. A request or
#: a model reply that names one of these gets the shape it meant.
ALIASES = {
    "process": "flow",
    "pipeline": "flow",
    "flowchart": "flow",
    "workflow": "flow",
    "steps": "flow",
    "tree": "hierarchy",
    "org chart": "hierarchy",
    "orgchart": "hierarchy",
    "breakdown": "hierarchy",
    "taxonomy": "hierarchy",
    "mind map": "mindmap",
    "network": "concept",
    "map": "concept",
    "concept map": "concept",
    "relationships": "concept",
    "graph": "concept",
    "lifecycle": "cycle",
    "loop": "cycle",
    "chronology": "timeline",
    "history": "timeline",
    "lanes": "swimlane",
    "swim lane": "swimlane",
    "stack": "layers",
    "architecture": "layers",
    "clusters": "groups",
    "categories": "groups",
    "venn": "groups",
    "comparison": "table",
    "matrix": "table",
    "2x2": "quadrant",
    "bar": "chart",
    "bar chart": "chart",
    "line": "chart",
    "line chart": "chart",
    "plot": "chart",
    "freeform": "canvas",
    "class diagram": "class",
    "uml class": "class",
    "uml class diagram": "class",
    "domain model": "class",
    "object model": "class",
    "activity diagram": "activity",
    "uml activity": "activity",
    "uml activity diagram": "activity",
    "state machine": "state",
    "state diagram": "state",
    "state chart": "state",
    "statechart": "state",
    "behavior": "state",
    "behaviour": "state",
    "behavior diagram": "state",
    "behaviour diagram": "state",
    "behavioral state machine": "state",
    "use case": "usecase",
    "use case diagram": "usecase",
    "use cases": "usecase",
    "free-form": "canvas",
    "custom": "canvas",
}

SHAPES = ("box", "round", "pill", "ellipse", "circle", "diamond", "cylinder", "hexagon", "note")
CHARTS = ("bar", "line")

MAX_NODES = 30
MAX_EDGES = 60
MAX_GROUPS = 8
MAX_COLUMNS = 6
MAX_CELLS = 120
MAX_LABEL = 80

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
#: A number standing alone as a token — after a bracket, a comma, a space, a
#: ``#`` or the start — never the tail of an identifier such as ``n1``.
_CITE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_.])#?(\d{1,4})(?![A-Za-z0-9_.])")

#: What each kind needs at least, beyond the grounding share: a table of one
#: cell or a chart of one bar is not a picture of anything.
_MINIMUM = {"table": ("cells", 2), "chart": ("nodes", 2), "class": ("nodes", 1)}


def normalize_kind(value: Any) -> str | None:
    """A vocabulary kind for ``value`` (a kind, an alias, or a phrase), else ``None``."""

    text = " ".join(str(value or "").strip().lower().replace("_", " ").split())
    if not text:
        return None
    if text.replace(" ", "") in KINDS:
        return text.replace(" ", "")
    if text in ALIASES:
        return ALIASES[text]
    return None


def check(
    spec: dict[str, Any],
    valid: set[int],
    *,
    kind: str,
    evidence: dict[int, str] | None = None,
) -> dict[str, Any] | str:
    """The checked diagram for ``spec``, or a reason it cannot be drawn.

    ``valid`` is the set of passage numbers the request was given;
    ``evidence`` maps a passage number to its text, when the caller has it,
    for the chart-value check.
    """

    groups, group_ids = _groups(spec.get("groups"), valid)
    nodes = _nodes(spec.get("nodes"), valid, kind=kind, group_ids=group_ids)
    if kind == "chart":
        nodes = [node for node in nodes if "value" in node]
        _check_values(nodes, evidence)
    known = {node["id"] for node in nodes}
    types = {node["id"]: node["type"] for node in nodes if node.get("type")}
    edges = _edges(spec.get("edges"), valid, known, kind=kind, types=types)

    diagram: dict[str, Any] = {"kind": kind, "nodes": nodes, "edges": edges}
    if groups:
        diagram["groups"] = groups
    if kind == "table":
        columns, cells = _table(spec, valid, known)
        diagram["columns"] = columns
        diagram["cells"] = cells
    if kind in {"quadrant", "chart"}:
        axes = _axes(spec.get("axes"))
        if axes:
            diagram["axes"] = axes
    if kind == "chart":
        chart = str(spec.get("chart") or "bar").lower()
        diagram["chart"] = chart if chart in CHARTS else "bar"
        unit = label(spec.get("unit"), 20)
        if unit:
            diagram["unit"] = unit

    needed_field, needed = _MINIMUM.get(kind, ("nodes", 2))
    if len(diagram.get(needed_field) or []) < needed:
        return f"fewer than {needed} {needed_field} could be drawn from the passages"
    return diagram


def elements(diagram: dict[str, Any]) -> list[dict[str, Any]]:
    """Everything in a checked diagram a reader could take as a claim.

    UML pseudo-nodes (a start dot, a fork bar) and the edges that only say
    where a flow begins or ends are notation, and are left out.
    """

    return [
        *[node for node in diagram.get("nodes", []) if not node.get("structural")],
        *[edge for edge in diagram.get("edges", []) if not edge.get("structural")],
        *diagram.get("groups", []),
        *diagram.get("cells", []),
    ]


def label(value: Any, limit: int = MAX_LABEL) -> str:
    text = _CONTROL_RE.sub(" ", str(value or "")).strip()
    text = " ".join(text.split())
    return text[:limit]


def cites(value: Any, valid: set[int]) -> list[int]:
    """The passage numbers ``value`` names that were given, in order, once each.

    Read the ways a model writes them: ``2``, ``"[2]"``, ``"[1][3]"``,
    ``"1, 3"``, ``"passage 2"``, ``"#2"``. A token that is not a bare number
    (``"n1"`` — a node id in the wrong field) is not read as one: a
    citation this check invents is worse than one it misses.
    """

    if not isinstance(value, list):
        value = [value] if value is not None else []
    out: list[int] = []
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            numbers = [int(item)] if float(item).is_integer() else []
        else:
            numbers = [int(token) for token in _CITE_TOKEN_RE.findall(str(item))]
        for number in numbers:
            if number in valid and number not in out:
                out.append(number)
    return out


# ------------------------------------------------------------------ parts


def _groups(raw: Any, valid: set[int]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Lanes, layers or categories, and a lookup from what a node may name them by."""

    groups: list[dict[str, Any]] = []
    lookup: dict[str, str] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = label(item.get("label"), 60)
        group_id = str(item.get("id") or "").strip() or name
        if not name or not group_id or group_id in lookup:
            continue
        found = cites(item.get("cites"), valid)
        groups.append({"id": group_id[:32], "label": name, "cites": found, "inferred": not found})
        lookup[group_id] = group_id[:32]
        lookup.setdefault(name.lower(), group_id[:32])
        if len(groups) >= MAX_GROUPS:
            break
    return groups, lookup


def _nodes(
    raw: Any, valid: set[int], *, kind: str, group_ids: dict[str, str]
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    known: set[str] = set()
    for item in (raw if isinstance(raw, list) else [])[: MAX_NODES * 2]:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or "").strip()
        name = label(item.get("label")) or visual_uml.default_label(kind, item)
        if not _ID_RE.match(node_id) or node_id in known or not name:
            continue
        found = cites(item.get("cites"), valid)
        node: dict[str, Any] = {"id": node_id, "label": name, "cites": found, "inferred": not found}
        group = label(item.get("group"), 40)
        if group:
            # A lane or layer the spec declared is referenced by id; a free
            # label still groups (the renderer makes a band for it).
            node["group"] = group_ids.get(group, group_ids.get(group.lower(), group))
        detail = label(item.get("detail"), 160)
        if detail:
            node["detail"] = detail
        shape = str(item.get("shape") or "").strip().lower()
        if shape in SHAPES:
            node["shape"] = shape
        when = label(item.get("when"), 40)
        if when:
            node["when"] = when
        if kind == "chart":
            value = _number(item.get("value"))
            if value is not None:
                node["value"] = value
        if kind in {"quadrant", "canvas"}:
            scale = 1.0 if kind == "quadrant" else 100.0
            x, y = _number(item.get("x")), _number(item.get("y"))
            if x is not None and y is not None:
                node["x"] = min(1.0, max(0.0, x / scale))
                node["y"] = min(1.0, max(0.0, y / scale))
        if kind in visual_uml.UML_KINDS:
            visual_uml.node_fields(kind, item, node, valid, label=label, cites=cites)
        nodes.append(node)
        known.add(node_id)
        if len(nodes) >= MAX_NODES:
            break
    return nodes


def _edges(
    raw: Any,
    valid: set[int],
    known: set[str],
    *,
    kind: str,
    types: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    loops = visual_uml.allows_self_loops(kind)
    for item in (raw if isinstance(raw, list) else [])[: MAX_EDGES * 2]:
        if not isinstance(item, dict):
            continue
        source = str(item.get("from") or "").strip()
        target = str(item.get("to") or "").strip()
        if source not in known or target not in known or (source == target and not loops):
            continue
        found = cites(item.get("cites"), valid)
        edge = {
            "from": source,
            "to": target,
            "label": label(item.get("label"), 40),
            "cites": found,
            "inferred": not found,
        }
        if kind in visual_uml.UML_KINDS:
            visual_uml.edge_fields(kind, item, edge, label=label, types=types)
        edges.append(edge)
        if len(edges) >= MAX_EDGES:
            break
    return edges


def _table(
    spec: dict[str, Any], valid: set[int], rows: set[str]
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    columns: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in spec.get("columns") if isinstance(spec.get("columns"), list) else []:
        if isinstance(item, str):
            item = {"id": item, "label": item}
        if not isinstance(item, dict):
            continue
        name = label(item.get("label"), 40)
        column_id = str(item.get("id") or name).strip()[:32]
        if not name or not column_id or column_id in seen:
            continue
        seen.add(column_id)
        columns.append({"id": column_id, "label": name})
        if len(columns) >= MAX_COLUMNS:
            break
    cells: list[dict[str, Any]] = []
    placed: set[tuple[str, str]] = set()
    for item in spec.get("cells") if isinstance(spec.get("cells"), list) else []:
        if not isinstance(item, dict):
            continue
        row, column = str(item.get("row") or ""), str(item.get("column") or "")
        text = label(item.get("text"), 120)
        if row not in rows or column not in seen or not text or (row, column) in placed:
            continue
        placed.add((row, column))
        found = cites(item.get("cites"), valid)
        cells.append(
            {"row": row, "column": column, "text": text, "cites": found, "inferred": not found}
        )
        if len(cells) >= MAX_CELLS:
            break
    return columns, cells


def _axes(raw: Any) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict):
        return {}
    axes: dict[str, dict[str, str]] = {}
    for name in ("x", "y"):
        axis = raw.get(name)
        if not isinstance(axis, dict):
            continue
        cleaned = {key: label(axis.get(key), 40) for key in ("label", "low", "high")}
        cleaned = {key: value for key, value in cleaned.items() if value}
        if cleaned:
            axes[name] = cleaned
    return axes


def _check_values(nodes: list[dict[str, Any]], evidence: dict[int, str] | None) -> None:
    """A value none of its cited passages states is not a cited value.

    Only when the caller has the passages' text: without it nothing can be
    checked, and the citation stands as the model gave it.
    """

    if not evidence:
        return
    for node in nodes:
        if not node["cites"]:
            continue
        wanted = node["value"]
        stated = any(_states(evidence.get(number, ""), wanted) for number in node["cites"])
        if not stated:
            node["cites"] = []
            node["inferred"] = True
            node["unverified_value"] = True


def _states(text: str, value: float) -> bool:
    for match in _NUMBER_RE.finditer(text):
        number = _number(match.group(0))
        if number is not None and abs(number - value) <= 1e-9 * max(1.0, abs(value)):
            return True
    return False


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().replace(",", "").rstrip("%")
    try:
        return float(text)
    except ValueError:
        return None
