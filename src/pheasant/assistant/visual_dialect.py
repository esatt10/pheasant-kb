"""The spellings of a diagram spec that models actually write, read as the one the grammar means.

``assistant.visual_specs`` is strict on purpose: it is the check, and a check
that guesses is not one. But it was strict about *spelling* as well as
*substance*, and the two are not the same kind of failure. A model that
writes ``{"source": "a", "target": "b"}`` has stated the same edge as one
that writes ``{"from": "a", "to": "b"}``; a node whose id is ``"build image"``
is the same node as one whose id is ``"build_image"``; a hierarchy written as
nested ``children`` is the same tree as a node list plus parent→child edges.
Read strictly, each of those is a diagram with nothing in it — declined as
"fewer than 2 nodes could be drawn", which reads like a grounding judgement
and is nothing of the kind. Which spellings a model reaches for varies by
vendor and by release, so a visual that works on one model and silently
fails on the next is exactly this module's absence.

So this module does two things, both deterministic and neither of which can
make a claim a model did not make:

* :func:`extract` finds the JSON object in a reply — inside a fence, after a
  preamble, beside a ``<think>`` block, with a trailing comma, or written as a
  Python literal.
* :func:`normalize` rewrites the spellings below onto the grammar's own,
  *before* the check runs. It renames and restructures; it never invents a
  citation, a node or an edge. An element with no citation in any spelling
  still reaches the check with none, and is still marked ``inferred``.

Pure functions over plain data; no model, no I/O.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

from pheasant.assistant.visual_specs import normalize_kind

#: Keys a wrapped reply puts the spec under: ``{"diagram": {...}}``.
_WRAPPERS = ("diagram", "visual", "spec", "visualization", "result", "data", "output")

#: Where each part of the grammar is found under other names. Order matters:
#: the first present key wins, and the grammar's own name is always first.
_NODE_KEYS = (
    "nodes",
    "vertices",
    "steps",
    "items",
    "elements",
    "entities",
    "events",
    "actors",
    "participants",
    "states",
    "classes",
    "stages",
)
_EDGE_KEYS = (
    "edges",
    "links",
    "connections",
    "relationships",
    "relations",
    "arrows",
    "transitions",
    "messages",
    "flows",
)
_GROUP_KEYS = ("groups", "lanes", "swimlanes", "layers", "partitions", "categories", "clusters")
_CITE_KEYS = (
    "cites",
    "citations",
    "citation",
    "cite",
    "cited",
    "refs",
    "references",
    "passages",
    "passage",
    "evidence",
    "support",
    "sources",
)
_LABEL_KEYS = ("label", "name", "title", "text")
_EDGE_LABEL_KEYS = ("label", "text", "message", "name", "description")
_FROM_KEYS = ("from", "source", "src", "from_id", "start", "parent", "origin")
_TO_KEYS = ("to", "target", "dst", "to_id", "end", "child", "destination")
_WHEN_KEYS = ("when", "date", "time", "year", "period", "phase")
_DETAIL_KEYS = ("detail", "description", "details", "summary", "note")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_ARROW_RE = re.compile(r"^\s*(.+?)\s*(?:-+>|→|=>)\s*(.+?)\s*$")
_THINK_RE = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```[A-Za-z0-9_-]*\s*\n?(.*?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")

#: How deep a nested ``children`` tree is followed; a model does not draw a
#: tree deeper than this, and a cycle in a malformed reply must terminate.
_MAX_DEPTH = 8


# --------------------------------------------------------------- extraction


def extract(raw: str | None) -> dict[str, Any] | None:
    """The spec object in a model reply, or ``None`` if there is none.

    Tried in order, and the first *spec-looking* object wins over the first
    object: a reply that quotes a small JSON example before its answer should
    be read for its answer.
    """

    if not raw:
        return None
    text = _THINK_RE.sub("", str(raw)).strip()
    candidates: list[dict[str, Any]] = []
    for block in [text, *_FENCE_RE.findall(text)]:
        candidates.extend(_objects_in(block))
    if not candidates:
        return None
    for candidate in candidates:
        unwrapped = _unwrap(candidate)
        if _looks_like_spec(unwrapped):
            return unwrapped
    return _unwrap(candidates[0])


def _objects_in(text: str) -> list[dict[str, Any]]:
    """Every top-level JSON object ``text`` contains, lenient forms last."""

    text = text.strip()
    found: list[dict[str, Any]] = []
    for attempt in (text, _TRAILING_COMMA_RE.sub(r"\1", text)):
        if found:
            break
        decoder = json.JSONDecoder()
        index = attempt.find("{")
        while index != -1:
            try:
                value, end = decoder.raw_decode(attempt, index)
            except json.JSONDecodeError:
                index = attempt.find("{", index + 1)
                continue
            if isinstance(value, dict):
                found.append(value)
            elif isinstance(value, list):
                found.extend(item for item in value if isinstance(item, dict))
            index = attempt.find("{", end)
    if not found:
        # A Python literal (single quotes, True/None) is what some models
        # write when they forget which language they are in. ``literal_eval``
        # evaluates literals only, never code.
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                value = ast.literal_eval(text[start : end + 1])
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                value = None
            if isinstance(value, dict):
                found.append(value)
    return found


def _unwrap(value: dict[str, Any]) -> dict[str, Any]:
    for _ in range(3):
        if _looks_like_spec(value):
            return value
        inner = next(
            (value[key] for key in _WRAPPERS if isinstance(value.get(key), dict)),
            None,
        )
        if inner is None:
            return value
        # Keep what the wrapper said about the whole (a title beside the
        # diagram), under anything the diagram says itself.
        value = {**{k: v for k, v in value.items() if k not in _WRAPPERS}, **inner}
    return value


def _looks_like_spec(value: dict[str, Any]) -> bool:
    return any(isinstance(value.get(key), list) for key in (*_NODE_KEYS, *_EDGE_KEYS, "rows"))


# ------------------------------------------------------------ normalization


def normalize(spec: dict[str, Any], *, kind: str | None = None) -> dict[str, Any]:
    """``spec`` in the grammar's own spelling. Never adds a claim.

    ``kind`` is the shape the caller settled on, when it has one: a table's
    rows and a hierarchy's nesting are read differently from a flow's.
    """

    if not isinstance(spec, dict):
        return {}
    out = dict(spec)
    if not normalize_kind(out.get("kind")):
        for key in ("type", "diagram_type", "shape", "visual_type", "chart_type"):
            if normalize_kind(out.get(key)):
                out["kind"] = normalize_kind(out.get(key))
                break
    kind = normalize_kind(kind) or normalize_kind(out.get("kind")) or "flow"

    groups = _first_list(out, _GROUP_KEYS)
    raw_nodes = _first_list(out, _NODE_KEYS)
    if raw_nodes is None and kind == "table" and isinstance(out.get("rows"), list):
        raw_nodes = out["rows"]
    raw_edges = _first_list(out, _EDGE_KEYS) or []

    nodes: list[dict[str, Any]] = []
    tree_edges: list[dict[str, Any]] = []
    for item in raw_nodes or []:
        _flatten(item, nodes, tree_edges, parent=None, group=None, depth=0)
    # Nodes nested inside their lane, layer or category.
    normalized_groups: list[dict[str, Any]] = []
    for position, group in enumerate(groups or []):
        if isinstance(group, str):
            group = {"label": group}
        if not isinstance(group, dict):
            continue
        group = _with_cites(_with_label(dict(group)))
        group.setdefault("id", group.get("label") or f"g{position + 1}")
        members = _first_list(group, ("nodes", "items", "members", "children", "steps"))
        for member in members or []:
            _flatten(member, nodes, tree_edges, parent=None, group=str(group["id"]), depth=0)
        normalized_groups.append(
            {key: value for key, value in group.items() if key in {"id", "label", "cites"}}
        )

    ids = _assign_ids(nodes)
    edges = [edge for edge in (_edge(item) for item in raw_edges) if edge is not None]
    edges.extend(tree_edges)
    for edge in edges:
        edge["from"] = ids.resolve(edge.get("from"))
        edge["to"] = ids.resolve(edge.get("to"))

    out["kind"] = kind
    out["nodes"] = nodes
    out["edges"] = edges
    if normalized_groups:
        out["groups"] = normalized_groups
    if kind == "table":
        _normalize_table(out, nodes, ids)
    return out


def _first_list(spec: dict[str, Any], keys: tuple[str, ...]) -> list[Any] | None:
    for key in keys:
        value = spec.get(key)
        if isinstance(value, list):
            return value
    return None


def _first(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", []):
            return value
    return None


def _with_label(item: dict[str, Any], keys: tuple[str, ...] = _LABEL_KEYS) -> dict[str, Any]:
    if not item.get("label"):
        value = _first(item, keys)
        if isinstance(value, (str, int, float)):
            item["label"] = str(value)
    return item


def _with_cites(item: dict[str, Any], keys: tuple[str, ...] = _CITE_KEYS) -> dict[str, Any]:
    if "cites" not in item or item.get("cites") in (None, "", []):
        value = _first(item, keys)
        if value is not None:
            item["cites"] = _cite_values(value)
    else:
        item["cites"] = _cite_values(item["cites"])
    return item


def _cite_values(value: Any) -> list[Any]:
    """Citations as a list the grammar reads: ``{"index": 2}`` is ``2``."""

    if not isinstance(value, list):
        value = [value]
    out: list[Any] = []
    for item in value:
        if isinstance(item, dict):
            item = _first(item, ("index", "n", "passage", "number", "id"))
        if item is not None:
            out.append(item)
    return out


def _flatten(
    item: Any,
    nodes: list[dict[str, Any]],
    tree_edges: list[dict[str, Any]],
    *,
    parent: str | None,
    group: str | None,
    depth: int,
) -> None:
    """One node, and — for a nested tree — its descendants and the edges to them."""

    if depth > _MAX_DEPTH:
        return
    if isinstance(item, (str, int, float)) and not isinstance(item, bool):
        item = {"label": str(item)}
    if not isinstance(item, dict):
        return
    node = _with_cites(_with_label(dict(item)))
    if "id" not in node or node.get("id") in (None, ""):
        node["id"] = _first(node, ("key", "node_id", "name")) or None
    if not node.get("when"):
        when = _first(node, _WHEN_KEYS[1:])
        if isinstance(when, (str, int, float)):
            node["when"] = str(when)
    if not node.get("detail"):
        detail = _first(node, _DETAIL_KEYS[1:])
        if isinstance(detail, str):
            node["detail"] = detail
    if group is not None and not node.get("group"):
        node["group"] = group
    children = node.pop("children", None)
    node.pop("nodes", None)
    node["_ref"] = f"__node{len(nodes)}"
    nodes.append(node)
    if parent is not None:
        # The edge a nesting states is supported by what supports the child:
        # "B is part of A" is the claim the child's passage makes.
        tree_edges.append({"from": parent, "to": node["_ref"], "cites": node.get("cites", [])})
    for child in children if isinstance(children, list) else []:
        _flatten(child, nodes, tree_edges, parent=node["_ref"], group=group, depth=depth + 1)


class _Ids:
    """Every name an edge may use for a node, mapped to the node's grammar id."""

    def __init__(self) -> None:
        self.by_name: dict[str, str] = {}

    def add(self, name: Any, node_id: str) -> None:
        if name is None or isinstance(name, (dict, list)):
            return
        for key in (str(name), str(name).strip().lower()):
            self.by_name.setdefault(key, node_id)

    def resolve(self, name: Any) -> Any:
        if name is None or isinstance(name, (dict, list)):
            return name
        text = str(name)
        return self.by_name.get(text, self.by_name.get(text.strip().lower(), text))


def _assign_ids(nodes: list[dict[str, Any]]) -> _Ids:
    """A valid, unique id per node, and a lookup from every name it went by.

    An id the grammar would reject (``"build image"``, ``"step #1"``) is
    slugged rather than the node being dropped; a node with none gets one. An
    edge may then name a node by its id, its original id, or its label.
    """

    ids = _Ids()
    taken: set[str] = set()
    for position, node in enumerate(nodes):
        original = node.get("id")
        candidate = str(original).strip() if original not in (None, "") else ""
        if not _ID_RE.match(candidate):
            candidate = re.sub(r"[^A-Za-z0-9_-]+", "_", candidate).strip("_")[:32]
        if not candidate or candidate in taken:
            base = candidate[:28] or "n"
            suffix = position + 1
            candidate = f"{base}{suffix}"
            while candidate in taken:
                suffix += 1
                candidate = f"{base}{suffix}"
        taken.add(candidate)
        ref = node.pop("_ref")
        ids.add(ref, candidate)
        ids.add(candidate, candidate)
        ids.add(original, candidate)
        ids.add(node.get("label"), candidate)
        node["id"] = candidate
    return ids


def _edge(item: Any) -> dict[str, Any] | None:
    """One edge from a dict, a ``[from, to, label]`` list or an ``"A -> B"`` string."""

    if isinstance(item, str):
        match = _ARROW_RE.match(item)
        return {"from": match.group(1), "to": match.group(2)} if match else None
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        edge = {"from": item[0], "to": item[1]}
        if len(item) >= 3 and isinstance(item[2], str):
            edge["label"] = item[2]
        return edge
    if not isinstance(item, dict):
        return None
    # ``source`` is an endpoint on an edge, never a citation.
    edge = _with_cites(dict(item), tuple(key for key in _CITE_KEYS if key != "sources"))
    if edge.get("from") in (None, ""):
        edge["from"] = _first(edge, _FROM_KEYS[1:])
    if edge.get("to") in (None, ""):
        edge["to"] = _first(edge, _TO_KEYS[1:])
    if not edge.get("label"):
        value = _first(edge, _EDGE_LABEL_KEYS[1:])
        if isinstance(value, str):
            edge["label"] = value
    return edge


def _normalize_table(out: dict[str, Any], nodes: list[dict[str, Any]], ids: _Ids) -> None:
    """Columns as the grammar spells them, and cells from rows that carry their own.

    ``{"label": "Option A", "cells": {"Cost": "low"}, "cites": [1]}`` — a row
    holding its values — is the table most models write. Its cells inherit
    the row's citations only when they name none themselves: the row's
    passage is what the model said supports that row.
    """

    columns: list[dict[str, str]] = []
    for position, column in enumerate(out.get("columns") or []):
        if isinstance(column, str):
            column = {"id": column, "label": column}
        if isinstance(column, dict):
            column = _with_label(dict(column), ("label", "name", "title", "header"))
            column.setdefault("id", column.get("label") or f"c{position + 1}")
            columns.append(column)
    by_name = {}
    for column in columns:
        by_name.setdefault(str(column["id"]), str(column["id"]))
        by_name.setdefault(str(column.get("label") or "").strip().lower(), str(column["id"]))

    cells = []
    for item in out.get("cells") or []:
        if isinstance(item, dict):
            cell = _with_cites(dict(item))
            cell["row"] = ids.resolve(_first(cell, ("row", "row_id", "node")))
            wanted = _first(cell, ("column", "col", "column_id", "header"))
            cell["column"] = by_name.get(str(wanted), by_name.get(str(wanted).lower(), wanted))
            if not cell.get("text"):
                value = _first(cell, ("value", "content", "label"))
                if isinstance(value, (str, int, float)):
                    cell["text"] = str(value)
            cells.append(cell)
    if not cells:
        for node in nodes:
            values = _first(node, ("cells", "values", "columns", "fields"))
            if isinstance(values, list):
                values = {
                    _first(v, ("column", "col", "header", "label")): v
                    for v in values
                    if isinstance(v, dict)
                }
            if not isinstance(values, dict):
                continue
            for name, value in values.items():
                if name is None:
                    continue
                column = by_name.get(str(name), by_name.get(str(name).strip().lower()))
                if column is None:
                    column = str(name)
                    columns.append({"id": column, "label": column})
                    by_name[column] = column
                    by_name[column.lower()] = column
                cite = node.get("cites", [])
                if isinstance(value, dict):
                    own = _with_cites(dict(value))
                    text = _first(own, ("text", "value", "content"))
                    cite = own.get("cites") or cite
                else:
                    text = value
                if isinstance(text, (str, int, float)) and not isinstance(text, bool):
                    cells.append(
                        {"row": node["id"], "column": column, "text": str(text), "cites": cite}
                    )
    out["columns"] = columns
    out["cells"] = cells
