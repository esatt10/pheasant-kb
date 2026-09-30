"""UML in the visual grammar: class, activity, state machine and use case diagrams.

These are four more kinds of ``assistant.visual_specs``, checked the same way
— every class, action, state, use case, relationship and transition cites a
passage or is marked ``inferred`` — with the notation each diagram needs:

========== ==================================================================
kind       nodes and edges carry
========== ==================================================================
class      node ``stereotype`` (interface, abstract, enumeration…),
           ``attributes`` and ``operations`` (strings, or ``{text, cites}``);
           edge ``relation`` (inheritance, realization, association,
           aggregation, composition, dependency) and ``from_mult`` /
           ``to_mult`` multiplicities
activity   node ``type`` (action, initial, final, flow_final, decision, merge,
           fork, join) and an optional partition ``group``; edge ``guard``
state      node ``type`` (state, initial, final, choice) and ``entry`` /
           ``do`` / ``exit`` activities; edge ``trigger``, ``guard``,
           ``effect`` — drawn as ``trigger [guard] / effect``; self-transitions
           are allowed
usecase    node ``type`` (usecase, actor); ``groups`` is the system
           boundary; edge ``relation`` (association, include, extend,
           generalization)
========== ==================================================================

**Pseudo-nodes are notation, not claims.** A start dot, an end bullseye, a
fork bar or a choice diamond states nothing a passage could support, so they
need no label and no citation and are left out of the grounding share —
counting them either way would let a diagram's punctuation decide whether its
content was grounded.

A class member given as a bare string belongs to its class and shares the
class's citations; one given as ``{text, cites}`` is checked on its own and
marked ``inferred`` when nothing supports it.
"""

from __future__ import annotations

import re
from typing import Any

UML_KINDS = ("class", "activity", "state", "usecase")

NODE_TYPES = {
    "activity": ("action", "initial", "final", "flow_final", "decision", "merge", "fork", "join"),
    "state": ("state", "initial", "final", "choice"),
    "usecase": ("usecase", "actor"),
}
#: Pseudo-nodes: notation with nothing to cite.
STRUCTURAL = frozenset({"initial", "final", "flow_final", "merge", "fork", "join", "choice"})
_DEFAULT_LABEL = {
    "initial": "start",
    "final": "end",
    "flow_final": "end of flow",
    "merge": "merge",
    "fork": "fork",
    "join": "join",
    "choice": "choice",
}

RELATIONS = {
    "class": (
        "association",
        "inheritance",
        "realization",
        "aggregation",
        "composition",
        "dependency",
    ),
    "usecase": ("association", "include", "extend", "generalization"),
}
RELATION_ALIASES = {
    "extends": "inheritance",
    "inherits": "inheritance",
    "is a": "inheritance",
    "generalization": "inheritance",
    "subclass": "inheritance",
    "implements": "realization",
    "realizes": "realization",
    "has": "aggregation",
    "has a": "aggregation",
    "owns": "composition",
    "contains": "composition",
    "part of": "composition",
    "uses": "dependency",
    "depends on": "dependency",
    "calls": "dependency",
    "includes": "include",
    "extends use case": "extend",
}

MAX_MEMBERS = 12
_MULT_RE = re.compile(r"^(?:\*|\d+(?:\.\.(?:\d+|\*|n))?|n)$")


def default_label(kind: str, item: dict[str, Any]) -> str:
    """The label a pseudo-node gets when the spec gives it none."""

    node_type = _node_type(kind, item)
    return _DEFAULT_LABEL.get(node_type, "") if node_type in STRUCTURAL else ""


def node_fields(
    kind: str,
    item: dict[str, Any],
    node: dict[str, Any],
    valid: set[int],
    *,
    label: Any,
    cites: Any,
) -> None:
    """Add a UML node's notation to ``node`` (already holding id/label/cites)."""

    node_type = _node_type(kind, item)
    if node_type:
        node["type"] = node_type
    if node_type in STRUCTURAL:
        # Notation, not a claim: it cites nothing and needs nothing.
        node["structural"] = True
        node["inferred"] = False
        node["cites"] = []
    if kind == "class":
        stereotype = label(item.get("stereotype"), 30).strip("«»<> ").lower()
        if stereotype:
            node["stereotype"] = stereotype
        for field in ("attributes", "operations"):
            members = _members(item.get(field), node["cites"], valid, label=label, cites=cites)
            if members:
                node[field] = members
    if kind == "state":
        for field in ("entry", "do", "exit"):
            text = label(item.get(field), 60)
            if text:
                node[field] = text


def edge_fields(
    kind: str,
    item: dict[str, Any],
    edge: dict[str, Any],
    *,
    label: Any,
    types: dict[str, str] | None = None,
) -> None:
    """Add a UML edge's notation to ``edge`` (already holding from/to/label/cites).

    ``types`` maps node id to UML node type. An edge out of the start dot or
    into the end bullseye says where the flow begins or ends — notation, like
    the pseudo-nodes themselves, so it is not a claim either.
    """

    types = types or {}
    if types.get(edge["from"]) == "initial" or types.get(edge["to"]) in {"final", "flow_final"}:
        if not edge["cites"]:
            edge["structural"] = True
            edge["inferred"] = False

    if kind in RELATIONS:
        edge["relation"] = _relation(kind, item.get("relation") or item.get("type"))
    if kind == "class":
        for field in ("from_mult", "to_mult"):
            text = str(item.get(field) or "").strip().replace(" ", "")
            if _MULT_RE.match(text):
                edge[field] = text
    if kind == "activity":
        guard = label(item.get("guard"), 40).strip("[] ")
        if guard:
            edge["guard"] = guard
            edge["label"] = edge["label"] or f"[{guard}]"
    if kind == "state":
        parts = {field: label(item.get(field), 40) for field in ("trigger", "guard", "effect")}
        parts["guard"] = parts["guard"].strip("[] ")
        for field, text in parts.items():
            if text:
                edge[field] = text
        if not edge["label"] and any(parts.values()):
            edge["label"] = transition_label(edge)


def transition_label(edge: dict[str, Any]) -> str:
    """``trigger [guard] / effect``, UML's own spelling of a transition."""

    text = edge.get("trigger") or ""
    if edge.get("guard"):
        text += f" [{edge['guard']}]"
    if edge.get("effect"):
        text += f" / {edge['effect']}"
    return text.strip()[:80]


def allows_self_loops(kind: str) -> bool:
    """A state may transition to itself (a retry, a timer); nothing else loops."""

    return kind == "state"


def minimum(kind: str) -> tuple[str, int] | None:
    """What a UML kind needs at least: one class is already a class diagram."""

    return ("nodes", 1) if kind == "class" else None


# ------------------------------------------------------------------ export


def to_mermaid(diagram: dict[str, Any], text: Any, plain: Any) -> str | None:
    """Mermaid for a UML kind, or ``None`` for any other. ``text``/``plain``
    are the export module's two escapers (bracketed and line grammars)."""

    kind = diagram.get("kind")
    if kind == "class":
        return _class_mermaid(diagram, text, plain)
    if kind == "state":
        return _state_mermaid(diagram, plain)
    if kind == "activity":
        return _activity_mermaid(diagram, text)
    if kind == "usecase":
        return _usecase_mermaid(diagram, text)
    return None


_CLASS_ARROWS = {
    # (arrow, whether the arrow reads parent/whole first, i.e. "to" on the left)
    "inheritance": ("<|--", True),
    "realization": ("<|..", True),
    "composition": ("*--", False),
    "aggregation": ("o--", False),
    "dependency": ("..>", False),
    "association": ("-->", False),
}


def _class_mermaid(diagram: dict[str, Any], text: Any, plain: Any) -> str:
    lines = ["classDiagram"]
    for node in diagram.get("nodes") or []:
        lines.append(f'    class {node["id"]}["{text(node["label"])}"]')
        members = [
            f"        {_member(m['text'], plain)}"
            for m in [*node.get("attributes", []), *node.get("operations", [])]
        ]
        if node.get("stereotype") or members:
            lines[-1] += " {"
            if node.get("stereotype"):
                lines.append(f"        <<{plain(node['stereotype'])}>>")
            lines.extend(members)
            lines.append("    }")
    for edge in diagram.get("edges") or []:
        arrow, reversed_ = _CLASS_ARROWS[edge.get("relation") or "association"]
        left, right = (edge["to"], edge["from"]) if reversed_ else (edge["from"], edge["to"])
        left_mult = edge.get("to_mult") if reversed_ else edge.get("from_mult")
        right_mult = edge.get("from_mult") if reversed_ else edge.get("to_mult")
        left_part = f'{left} "{left_mult}"' if left_mult else left
        right_part = f'"{right_mult}" {right}' if right_mult else right
        line = f"    {left_part} {arrow} {right_part}"
        if edge.get("label"):
            line += f" : {plain(edge['label'])}"
        lines.append(line)
    return "\n".join(lines)


def _member(value: str, plain: Any) -> str:
    # Parentheses are what make a member an operation in Mermaid; keep one
    # empty pair when the source had any. A colon is how a member states its
    # type and is plain text inside a class body, so it stays.
    operation = "(" in value
    name = _safe(re.sub(r"\(.*?\)", "", value), keep=":")
    return f"{name}()" if operation else name


def _safe(value: Any, keep: str = "") -> str:
    """Strip what is syntax in a Mermaid body line, keeping the characters
    ``keep`` names — ``[]/`` for a transition's ``trigger [guard] / effect``."""

    syntax = "".join(c for c in '{}()<>|;`"#:[]' if c not in keep)
    text = re.sub("[" + re.escape(syntax) + "]", " ", str(value))
    return " ".join(text.split()) or " "


def _state_mermaid(diagram: dict[str, Any], plain: Any) -> str:
    nodes = {node["id"]: node for node in diagram.get("nodes") or []}
    lines = ["stateDiagram-v2"]

    def ref(node_id: str, outgoing: bool) -> str:
        node_type = nodes[node_id].get("type")
        if node_type == "initial" and outgoing:
            return "[*]"
        if node_type == "final" and not outgoing:
            return "[*]"
        return node_id

    for node in nodes.values():
        if node.get("type") == "choice":
            lines.append(f"    state {node['id']} <<choice>>")
        elif node.get("type") not in {"initial", "final"}:
            lines.append(f'    state "{plain(node["label"])}" as {node["id"]}')
            for field in ("entry", "do", "exit"):
                if node.get(field):
                    lines.append(f"    {node['id']} : {field} / {_safe(node[field])}")
    for edge in diagram.get("edges") or []:
        line = f"    {ref(edge['from'], True)} --> {ref(edge['to'], False)}"
        if edge.get("label"):
            line += f" : {_safe(edge['label'], keep='[]/')}"
        lines.append(line)
    return "\n".join(lines)


_ACTIVITY_SHAPES = {
    "initial": ('((" "))', ""),
    "final": ('(((" ")))', ""),
    "flow_final": ('((("x")))', ""),
    "decision": ("{", "}"),
    "merge": ("{", "}"),
    "fork": ('[" "]', ""),
    "join": ('[" "]', ""),
}


def _activity_mermaid(diagram: dict[str, Any], text: Any) -> str:
    lines = ["flowchart TD"]
    bars: list[str] = []
    for node in diagram.get("nodes") or []:
        node_type = node.get("type") or "action"
        if node_type in {"decision", "merge"}:
            lines.append(f'    {node["id"]}{{"{text(node["label"])}"}}')
        elif node_type in _ACTIVITY_SHAPES:
            lines.append(f"    {node['id']}{_ACTIVITY_SHAPES[node_type][0]}")
            if node_type in {"fork", "join"}:
                bars.append(node["id"])
        else:
            lines.append(f'    {node["id"]}("{text(node["label"])}")')
    for edge in diagram.get("edges") or []:
        arrow = "-.->" if edge.get("inferred") else "-->"
        label = edge.get("label")
        lines.append(
            f"    {edge['from']} {arrow}|{text(label)}| {edge['to']}"
            if label
            else f"    {edge['from']} {arrow} {edge['to']}"
        )
    if bars:
        lines.append("    classDef bar fill:#333,stroke:#333")
        lines.append(f"    class {','.join(bars)} bar")
    return "\n".join(lines)


def _usecase_mermaid(diagram: dict[str, Any], text: Any) -> str:
    nodes = diagram.get("nodes") or []
    lines = ["flowchart LR"]
    boundary = (diagram.get("groups") or [None])[0]
    actors = [node for node in nodes if node.get("type") == "actor"]
    cases = [node for node in nodes if node.get("type") != "actor"]
    for node in actors:
        lines.append(f'    {node["id"]}(("{text("Actor " + node["label"])}"))')
    if boundary:
        lines.append(f'    subgraph {boundary["id"]}["{text(boundary["label"])}"]')
    for node in cases:
        lines.append(f'    {"    " if boundary else ""}{node["id"]}(["{text(node["label"])}"])')
    if boundary:
        lines.append("    end")
    for edge in diagram.get("edges") or []:
        relation = edge.get("relation") or "association"
        if relation in {"include", "extend"}:
            lines.append(f"    {edge['from']} -.->|{relation}| {edge['to']}")
        elif relation == "generalization":
            lines.append(f"    {edge['from']} --> {edge['to']}")
        else:
            lines.append(f"    {edge['from']} --- {edge['to']}")
    return "\n".join(lines)


# ------------------------------------------------------------------ parts


def _node_type(kind: str, item: dict[str, Any]) -> str:
    allowed = NODE_TYPES.get(kind)
    if not allowed:
        return ""
    value = str(item.get("type") or "").strip().lower().replace(" ", "_").replace("-", "_")
    value = {"start": "initial", "end": "final", "stop": "final", "use_case": "usecase"}.get(
        value, value
    )
    return value if value in allowed else allowed[0]


def _relation(kind: str, value: Any) -> str:
    text = " ".join(str(value or "").strip().lower().replace("_", " ").split())
    text = RELATION_ALIASES.get(text, text)
    if kind == "usecase" and text == "inheritance":
        text = "generalization"
    return text if text in RELATIONS[kind] else RELATIONS[kind][0]


def _members(
    raw: Any, owner_cites: list[int], valid: set[int], *, label: Any, cites: Any
) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            text = label(item.get("text") or item.get("name"), 80)
            if not text:
                continue
            found = cites(item.get("cites"), valid) if "cites" in item else list(owner_cites)
        else:
            text = label(item, 80)
            if not text:
                continue
            found = list(owner_cites)
        members.append({"text": text, "cites": found, "inferred": not found})
        if len(members) >= MAX_MEMBERS:
            break
    return members
