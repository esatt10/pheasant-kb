"""Text exports of a checked visual: Mermaid where Mermaid has the shape, Markdown for tables.

Renderers draw from the spec (``mcp_server/apps/knowledge_view.html``); these
are for copying out and for hosts that render Mermaid themselves. Every label
goes through :func:`mermaid_text`, which strips the characters that are
Mermaid syntax or HTML, so a label can neither close an element early nor
smuggle markup into a renderer. Nothing here is executable.
"""

from __future__ import annotations

import re
from typing import Any

from pheasant.assistant import visual_uml

#: Mermaid node brackets per spec ``shape``. The label goes between them.
_BRACKETS = {
    "box": ('["', '"]'),
    "round": ('("', '")'),
    "pill": ('(["', '"])'),
    "ellipse": ('(("', '"))'),
    "circle": ('(("', '"))'),
    "diamond": ('{"', '"}'),
    "cylinder": ('[("', '")]'),
    "hexagon": ('{{"', '"}}'),
    "note": ('>"', '"]'),
}
_FLOWCHART_KINDS = {
    "flow",
    "hierarchy",
    "concept",
    "cycle",
    "swimlane",
    "layers",
    "groups",
    "canvas",
}


def to_mermaid(diagram: dict[str, Any]) -> str | None:
    """The diagram as Mermaid text, or ``None`` for a shape Mermaid does not draw."""

    kind = diagram.get("kind")
    uml = visual_uml.to_mermaid(diagram, mermaid_text, _plain)
    if uml is not None:
        return uml
    if kind == "sequence":
        return _sequence(diagram)
    if kind == "mindmap":
        return _mindmap(diagram)
    if kind == "timeline":
        return _timeline(diagram)
    if kind == "quadrant":
        return _quadrant(diagram)
    if kind == "chart":
        return _chart(diagram)
    if kind in _FLOWCHART_KINDS:
        return _flowchart(diagram)
    return None


def to_markdown(diagram: dict[str, Any]) -> str | None:
    """A table visual as a Markdown table with its citations, else ``None``."""

    if diagram.get("kind") != "table":
        return None
    columns = diagram.get("columns") or []
    cells = {(cell["row"], cell["column"]): cell for cell in diagram.get("cells") or []}

    def text(value: str) -> str:
        return str(value).replace("|", "\\|")

    def cited(item: dict[str, Any]) -> str:
        marks = "".join(f"[{n}]" for n in item.get("cites") or [])
        return f"{text(item.get('label') or item.get('text') or '')} {marks}".strip()

    lines = [
        "| | " + " | ".join(text(column["label"]) for column in columns) + " |",
        "|---|" + "---|" * len(columns),
    ]
    for row in diagram.get("nodes") or []:
        values = [
            cited(cells[(row["id"], column["id"])]) if (row["id"], column["id"]) in cells else ""
            for column in columns
        ]
        lines.append(f"| {cited(row)} | " + " | ".join(values) + " |")
    return "\n".join(lines)


def mermaid_text(text: Any) -> str:
    # Quotes, brackets, pipes and angle brackets are Mermaid syntax or HTML;
    # entity-encode the first and drop the rest.
    cleaned = re.sub(r"[\[\]{}()<>|;`]", " ", str(text))
    return cleaned.replace('"', "#quot;").strip() or " "


def _plain(text: Any) -> str:
    """For the line-oriented grammars (timeline, mindmap, quadrant): no colons either."""

    return " ".join(mermaid_text(text).replace(":", " ").replace("#quot;", "'").split()) or " "


def _flowchart(diagram: dict[str, Any]) -> str:
    kind = diagram.get("kind")
    nodes = diagram.get("nodes") or []
    direction = "LR" if kind in {"flow", "swimlane", "cycle"} else "TD"
    lines = [f"flowchart {direction}"]

    def node_line(node: dict[str, Any], indent: str = "    ") -> str:
        default = "circle" if kind == "cycle" else "box"
        opening, closing = _BRACKETS.get(node.get("shape") or default, _BRACKETS["box"])
        return f"{indent}{node['id']}{opening}{mermaid_text(node['label'])}{closing}"

    groups = diagram.get("groups") or []
    grouped: set[str] = set()
    for group in groups:
        members = [node for node in nodes if node.get("group") == group["id"]]
        if not members:
            continue
        lines.append(f'    subgraph {group["id"]}["{mermaid_text(group["label"])}"]')
        for node in members:
            lines.append(node_line(node, "        "))
            grouped.add(node["id"])
        lines.append("    end")
    for node in nodes:
        if node["id"] not in grouped:
            lines.append(node_line(node))

    edges = list(diagram.get("edges") or [])
    if kind == "cycle" and not edges and len(nodes) > 1:
        edges = [
            {"from": nodes[i]["id"], "to": nodes[(i + 1) % len(nodes)]["id"], "label": ""}
            for i in range(len(nodes))
        ]
    for edge in edges:
        arrow = "-.->" if edge.get("inferred") else "-->"
        if edge.get("label"):
            lines.append(f"    {edge['from']} {arrow}|{mermaid_text(edge['label'])}| {edge['to']}")
        else:
            lines.append(f"    {edge['from']} {arrow} {edge['to']}")
    inferred = [node["id"] for node in nodes if node.get("inferred")]
    if inferred:
        lines.append("    classDef inferred stroke-dasharray: 4 3")
        lines.append(f"    class {','.join(inferred)} inferred")
    return "\n".join(lines)


def _sequence(diagram: dict[str, Any]) -> str:
    lines = ["sequenceDiagram"]
    for node in diagram.get("nodes") or []:
        lines.append(f"    participant {node['id']} as {mermaid_text(node['label'])}")
    for edge in diagram.get("edges") or []:
        arrow = "-->>" if edge.get("inferred") else "->>"
        lines.append(
            f"    {edge['from']}{arrow}{edge['to']}: {mermaid_text(edge.get('label') or ' ')}"
        )
    return "\n".join(lines)


def _mindmap(diagram: dict[str, Any]) -> str:
    nodes = diagram.get("nodes") or []
    children: dict[str, list[str]] = {}
    has_parent: set[str] = set()
    for edge in diagram.get("edges") or []:
        if edge["to"] not in has_parent:
            children.setdefault(edge["from"], []).append(edge["to"])
            has_parent.add(edge["to"])
    by_id = {node["id"]: node for node in nodes}
    root = nodes[0]["id"]
    lines = ["mindmap", f"  root(({_plain(by_id[root]['label'])}))"]
    seen = {root}

    def walk(node_id: str, depth: int) -> None:
        for child in children.get(node_id, []):
            if child in seen:
                continue
            seen.add(child)
            lines.append("  " * (depth + 1) + _plain(by_id[child]["label"]))
            walk(child, depth + 1)

    walk(root, 1)
    # Anything the edges did not reach still belongs on the map, off the root.
    for node in nodes:
        if node["id"] not in seen:
            lines.append("    " + _plain(node["label"]))
    return "\n".join(lines)


def _timeline(diagram: dict[str, Any]) -> str:
    lines = ["timeline", f"    title {_plain(diagram.get('title') or 'Timeline')}"]
    for node in diagram.get("nodes") or []:
        when = _plain(node.get("when") or "")
        lines.append(
            f"    {when} : {_plain(node['label'])}"
            if when.strip()
            else f"    {_plain(node['label'])}"
        )
    return "\n".join(lines)


def _quadrant(diagram: dict[str, Any]) -> str:
    axes = diagram.get("axes") or {}
    x, y = axes.get("x") or {}, axes.get("y") or {}
    lines = ["quadrantChart", f"    title {_plain(diagram.get('title') or 'Quadrant')}"]
    lines.append(
        f"    x-axis {_plain(x.get('low') or 'Low')} --> {_plain(x.get('high') or 'High')}"
    )
    lines.append(
        f"    y-axis {_plain(y.get('low') or 'Low')} --> {_plain(y.get('high') or 'High')}"
    )
    for node in diagram.get("nodes") or []:
        if "x" in node and "y" in node:
            lines.append(f"    {_plain(node['label'])}: [{node['x']:.2f}, {node['y']:.2f}]")
    return "\n".join(lines)


def _chart(diagram: dict[str, Any]) -> str:
    nodes = diagram.get("nodes") or []
    axes = diagram.get("axes") or {}
    y_label = (axes.get("y") or {}).get("label") or diagram.get("unit") or "value"
    labels = ", ".join(f'"{_plain(node["label"])}"' for node in nodes)
    values = ", ".join(f"{node['value']:g}" for node in nodes)
    series = "line" if diagram.get("chart") == "line" else "bar"
    return "\n".join(
        [
            "xychart-beta",
            f'    title "{_plain(diagram.get("title") or "Chart")}"',
            f"    x-axis [{labels}]",
            f'    y-axis "{_plain(y_label)}"',
            f"    {series} [{values}]",
        ]
    )
