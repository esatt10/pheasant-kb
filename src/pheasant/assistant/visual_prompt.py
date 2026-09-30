"""What a model is told when it is asked to draw, written for any model rather than one.

The drawing prompt was tuned against one model and worked there. Moved to
another it failed in ways that all looked alike from outside — no diagram —
and had different causes: prose around the JSON, a fenced block after a
paragraph of reasoning, ``source``/``target`` where the grammar says
``from``/``to``, citations under ``"citations"``, or a citation to a passage
number that was never given. ``assistant.visual_dialect`` absorbs the
spellings after the fact; this module is the other half, and makes them rarer
in the first place, with three things that do not depend on which model
reads them:

* **a contract at both ends** — what the whole reply is, stated in the
  system prompt and repeated as the last line the model reads, where the
  instructions that matter most belong for a model that weights recency;
* **the passage numbers it may cite, spelled out** — "cite the passages"
  leaves a model to count them, and one that miscounts cites a number the
  check then drops, which is how a correct diagram gets declined as
  ungrounded;
* **one worked example of the shape being drawn** — prose describes eighteen
  kinds; an example shows one exactly, and a model that has seen a valid
  ``state`` reply writes one. Every example here passes the grammar's own
  check, which ``tests/test_visual_robustness.py`` asserts, so the prompt
  cannot teach a shape the validator refuses.

Pure strings and plain data; no model, no I/O.
"""

from __future__ import annotations

import json
from typing import Any

#: The reply contract, stated once at the top of the system prompt and once
#: as the last thing in the user turn.
CONTRACT = (
    "Your ENTIRE reply is one JSON object: it starts with { and ends with }. "
    "No markdown fence, no prose before or after it, no comments, no trailing commas."
)

DIAGRAM_SYSTEM = f"""You turn retrieved passages from a private knowledge base \
into ONE visual that answers the user's request, in whatever shape or from \
whatever viewpoint they asked for. You draw ONLY what the passages support.

{CONTRACT}

The object has this form:
{{"kind": "flow", "title": "short title", "viewpoint": "",
 "summary": "one sentence saying what the visual shows",
 "nodes": [{{"id": "n1", "label": "short label", "cites": [1]}}],
 "edges": [{{"from": "n1", "to": "n2", "label": "", "cites": [2]}}]}}

Use exactly these key names: "nodes", "edges", "id", "label", "from", "to", \
"cites". Node ids are short and contain only letters, digits, "_" or "-" \
(n1, n2, build_step). An edge's "from" and "to" are node ids.

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
- "swimlane": a process across owners; "groups": [{{"id": "g1", "label": \
"Team", "cites": [1]}}] and each node has "group": "g1"; edges as in flow.
- "layers": a stack, top layer first; "groups" are the layers, nodes sit \
in them via "group".
- "groups": things sorted into categories; "groups" are the categories.
- "table": a comparison; nodes are the rows, "columns": [{{"id": "c1", \
"label": "..."}}], "cells": [{{"row": "n1", "column": "c1", "text": "...", \
"cites": [1]}}].
- "quadrant": a 2x2; "axes": {{"x": {{"label": "", "low": "", "high": ""}}, \
"y": {{...}}}} and each node has "x" and "y" between 0 and 1.
- "chart": numbers stated in the passages; each node has a numeric "value"; \
"chart": "bar" or "line"; optional "unit" and "axes": {{"y": {{"label": ""}}}}.
- "canvas": anything else; each node has "x" and "y" from 0 to 100 and a \
"shape".
- "class" (UML class diagram): nodes are classes with optional \
"stereotype" (interface, abstract, enumeration), "attributes" and \
"operations" (lists of strings such as "id: string" or "promote()"); edges \
have "relation": inheritance or realization (from the subclass TO the \
parent), composition or aggregation (from the whole TO the part), \
association or dependency; optional "from_mult"/"to_mult" such as "1", \
"0..*".
- "activity" (UML activity diagram): node "type" is initial, final, \
action, decision, merge, fork or join (initial, final, fork, join and merge \
need no label or cites); edges may carry a "guard"; nodes may sit in \
partitions via "group" with "groups" declared.
- "state" (UML state machine, a behavior diagram): node "type" is initial, \
state, choice or final, and a state may have "entry", "do", "exit"; edges \
are transitions with "trigger", "guard" and "effect"; a state may \
transition to itself.
- "usecase" (UML use case diagram): node "type" is actor or usecase; \
"groups": [one system boundary]; edges have "relation": association (actor \
to use case), include, extend or generalization.

Rules:
- A "viewpoint" the user asks for ("for a new engineer", "from the \
operator's side") decides what you include and how you label it; say it in \
"viewpoint".
- 3 to 15 nodes. Labels of at most six words, using the passages' own names \
for files, components, commands and steps. Node "detail" may add one short \
sentence.
- EVERY node, edge, group and cell lists in "cites" the passage numbers [n] \
that support it (UML start/end/fork/join/merge/choice pseudo-nodes excepted). \
"cites" is always a list of integers, such as [1] or [2, 3]. Never cite a \
number that was not given. If nothing supports an element, leave it out.
- A chart value must be a number the cited passage states; never compute \
or estimate one.
- Do not add steps, components, relationships or numbers the passages do \
not state.
- If the passages support only a small visual, draw a small one; a visual \
of two or three cited elements is better than none."""


def _n(node_id: str, label: str, cites: list[int], **extra: Any) -> dict[str, Any]:
    return {"id": node_id, "label": label, "cites": cites, **extra}


def _e(source: str, target: str, cites: list[int], **extra: Any) -> dict[str, Any]:
    return {"from": source, "to": target, "cites": cites, **extra}


#: One valid reply per kind. The labels are deliberately generic: the example
#: teaches the *structure*, and a model that copies a label out of it has
#: drawn something no passage says — which the check then marks as such.
EXAMPLES: dict[str, dict[str, Any]] = {
    "flow": {
        "nodes": [
            _n("n1", "First step", [1]),
            _n("n2", "Check passes?", [2], shape="diamond"),
            _n("n3", "Final step", [2]),
        ],
        "edges": [_e("n1", "n2", [1]), _e("n2", "n3", [2], label="yes")],
    },
    "sequence": {
        "nodes": [_n("a", "Client", [1]), _n("b", "Service", [1]), _n("c", "Store", [2])],
        "edges": [
            _e("a", "b", [1], label="request"),
            _e("b", "c", [2], label="write"),
            _e("b", "a", [1], label="response"),
        ],
    },
    "hierarchy": {
        "nodes": [_n("r", "Whole", [1]), _n("p1", "Part one", [1]), _n("p2", "Part two", [2])],
        "edges": [_e("r", "p1", [1]), _e("r", "p2", [2])],
    },
    "mindmap": {
        "nodes": [_n("c", "Central idea", [1]), _n("b1", "Branch", [1]), _n("b2", "Branch", [2])],
        "edges": [_e("c", "b1", [1]), _e("c", "b2", [2])],
    },
    "concept": {
        "nodes": [_n("a", "Idea A", [1]), _n("b", "Idea B", [2]), _n("c", "Idea C", [2])],
        "edges": [_e("a", "b", [1], label="depends on"), _e("b", "c", [2], label="produces")],
    },
    "cycle": {
        "nodes": [
            _n("s1", "Stage one", [1]),
            _n("s2", "Stage two", [1]),
            _n("s3", "Stage three", [2]),
        ],
        "edges": [_e("s1", "s2", [1]), _e("s2", "s3", [2]), _e("s3", "s1", [2])],
    },
    "timeline": {
        "nodes": [
            _n("t1", "First event", [1], when="2024-01"),
            _n("t2", "Second event", [2], when="2024-06"),
            _n("t3", "Third event", [2], when="2025"),
        ],
    },
    "swimlane": {
        "groups": [
            {"id": "g1", "label": "Team one", "cites": [1]},
            {"id": "g2", "label": "Team two", "cites": [2]},
        ],
        "nodes": [
            _n("a", "Hand off", [1], group="g1"),
            _n("b", "Receive", [2], group="g2"),
            _n("c", "Finish", [2], group="g2"),
        ],
        "edges": [_e("a", "b", [1]), _e("b", "c", [2])],
    },
    "layers": {
        "groups": [
            {"id": "top", "label": "Top layer", "cites": [1]},
            {"id": "base", "label": "Base layer", "cites": [2]},
        ],
        "nodes": [_n("a", "Component", [1], group="top"), _n("b", "Component", [2], group="base")],
        "edges": [_e("a", "b", [1], label="calls")],
    },
    "groups": {
        "groups": [
            {"id": "g1", "label": "Category one", "cites": [1]},
            {"id": "g2", "label": "Category two", "cites": [2]},
        ],
        "nodes": [
            _n("a", "Member", [1], group="g1"),
            _n("b", "Member", [1], group="g1"),
            _n("c", "Member", [2], group="g2"),
        ],
    },
    "table": {
        "nodes": [_n("r1", "Option one", [1]), _n("r2", "Option two", [2])],
        "columns": [{"id": "c1", "label": "Property"}, {"id": "c2", "label": "Trade-off"}],
        "cells": [
            {"row": "r1", "column": "c1", "text": "what [1] says", "cites": [1]},
            {"row": "r2", "column": "c1", "text": "what [2] says", "cites": [2]},
            {"row": "r2", "column": "c2", "text": "what [2] says", "cites": [2]},
        ],
    },
    "quadrant": {
        "axes": {
            "x": {"label": "Effort", "low": "low", "high": "high"},
            "y": {"label": "Impact", "low": "low", "high": "high"},
        },
        "nodes": [_n("a", "Item one", [1], x=0.2, y=0.8), _n("b", "Item two", [2], x=0.7, y=0.3)],
    },
    "chart": {
        "chart": "bar",
        "unit": "ms",
        "axes": {"y": {"label": "Latency"}},
        "nodes": [_n("a", "Case one", [1], value=120), _n("b", "Case two", [2], value=45)],
    },
    "canvas": {
        "nodes": [
            _n("a", "Store", [1], x=15, y=50, shape="cylinder"),
            _n("b", "Worker", [2], x=70, y=50, shape="hexagon"),
        ],
        "edges": [_e("a", "b", [1], label="feeds")],
    },
    "class": {
        "nodes": [
            _n(
                "p",
                "Parent",
                [1],
                stereotype="abstract",
                attributes=["id: string"],
                operations=["run()"],
            ),
            _n("c", "Child", [2], attributes=["extra: int"]),
            _n("d", "Part", [2]),
        ],
        "edges": [
            _e("c", "p", [2], relation="inheritance"),
            _e("p", "d", [1], relation="composition", from_mult="1", to_mult="0..*"),
        ],
    },
    "activity": {
        "nodes": [
            {"id": "s", "type": "initial"},
            _n("a1", "Do the work", [1], type="action"),
            _n("d1", "Succeeded?", [2], type="decision"),
            _n("a2", "Retry", [2], type="action"),
            {"id": "e", "type": "final"},
        ],
        "edges": [
            {"from": "s", "to": "a1"},
            _e("a1", "d1", [1]),
            _e("d1", "a2", [2], guard="no"),
            _e("a2", "a1", [2]),
            {"from": "d1", "to": "e", "guard": "yes"},
        ],
    },
    "state": {
        "nodes": [
            {"id": "i", "type": "initial"},
            _n("s1", "Pending", [1], type="state"),
            _n("s2", "Active", [1], type="state", entry="start timer"),
            {"id": "f", "type": "final"},
        ],
        "edges": [
            {"from": "i", "to": "s1"},
            _e("s1", "s2", [1], trigger="approve", guard="checks pass"),
            _e("s2", "s2", [2], trigger="heartbeat"),
            _e("s2", "f", [2], trigger="close", effect="archive"),
        ],
    },
    "usecase": {
        "groups": [{"id": "sys", "label": "The system", "cites": [1]}],
        "nodes": [
            _n("u", "User role", [1], type="actor"),
            _n("uc1", "Main use case", [1], type="usecase", group="sys"),
            _n("uc2", "Included step", [2], type="usecase", group="sys"),
        ],
        "edges": [
            _e("u", "uc1", [1], relation="association"),
            _e("uc1", "uc2", [2], relation="include"),
        ],
    },
}


def example(kind: str) -> dict[str, Any]:
    """The worked reply for ``kind`` (a flow when the kind is not settled)."""

    body = EXAMPLES.get(kind) or EXAMPLES["flow"]
    return {
        "kind": kind if kind in EXAMPLES else "flow",
        "title": "Short title",
        "summary": "One sentence.",
        **body,
    }


def system(kind: str | None) -> str:
    """The system turn: the grammar, the pinned shape, and one worked reply.

    With no pinned shape the example is a flow — the model still chooses the
    kind, and the example teaches the core every kind shares.
    """

    pinned = f"\nUse kind: {kind}." if kind else ""
    shown = json.dumps(example(kind or "flow"), separators=(",", ":"))
    return (
        f"{DIAGRAM_SYSTEM}{pinned}\n\n"
        f'A valid reply for kind "{kind or "flow"}" (copy the structure and key names; '
        f"never the labels or numbers — those come from the passages):\n{shown}"
    )


def user(prompt: str, request: str, valid: list[int]) -> str:
    """The user turn: the passages, the numbers they may be cited by, the request, the contract."""

    numbers = ", ".join(str(number) for number in valid) or "none"
    return f"{prompt}\n\nPassage numbers you may cite: {numbers}.\n\nDraw: {request}\n\n{CONTRACT}"


def repair(prompt: str, request: str, valid: list[int], reply: str | None, problem: str) -> str:
    """A second user turn that says what was wrong with the first reply, and nothing more.

    Only for replies that could not be *read* — never for a diagram the check
    declined as ungrounded, because asking a model to add citations until the
    check passes is asking it to make the check pass.
    """

    shown = (reply or "").strip()
    if len(shown) > 3000:
        shown = shown[:3000] + " …"
    previous = f"\nYour previous reply was:\n{shown}\n" if shown else "\n"
    return (
        f"{user(prompt, request, valid)}\n\n"
        f"Your previous reply could not be used: {problem}.{previous}\n"
        f"Reply again. {CONTRACT}"
    )
