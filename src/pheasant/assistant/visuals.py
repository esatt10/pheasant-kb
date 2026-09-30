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
``assistant.visual_specs`` is their shared grammar: eighteen kinds over one
core of cited nodes and edges — UML class, activity, state machine and use
case diagrams included (``assistant.visual_uml``) — down to a free
``canvas`` for anything the named kinds do not cover. The request picks
the shape (or pins it), and the same passages can be redrawn in another one
without a new search.

With no model connected, :func:`graph_diagram` draws what the index itself
recorded — the graph's own edges between the cited documents and what they
reference — which is grounded by construction and needs no network.

**Any model, not one.** The drawing call was tuned against one model and
failed quietly on the next, for reasons that all surfaced as "no diagram":
a reasoning model spending a 1,600-token cap on thinking and returning
nothing, prose or a fence around the JSON, the grammar's keys spelled another
way. Four layers now stand between a model's reply and "no diagram", and none
of them lowers the grounding bar:

1. ``assistant.visual_prompt`` states the contract at both ends of the call,
   lists the passage numbers that may be cited, and shows a worked reply of
   the shape being drawn; the provider is asked for JSON where it can be.
2. The budget is sized for a model that thinks before it writes, and a reply
   cut off before any text is asked again with room to think
   (``LLM.complete``, for every call the assistant makes).
3. ``assistant.visual_dialect`` reads the spellings models actually use onto
   the grammar before the check — renaming, never adding a claim.
4. A reply that still cannot be *read* gets one repair turn saying what was
   wrong. A diagram declined as *ungrounded* does not: asking for citations
   until the check passes is asking the model to pass the check.

When all of that fails, the caller (``assistant.answering``) falls back to
:func:`graph_diagram` and says so, rather than showing nothing.
"""

from __future__ import annotations

from typing import Any

from pheasant.assistant import visual_dialect, visual_prompt, visual_specs
from pheasant.assistant.providers import ProviderError
from pheasant.assistant.visual_export import to_markdown, to_mermaid
from pheasant.assistant.visual_prompt import DIAGRAM_SYSTEM
from pheasant.assistant.visual_specs import KINDS, MAX_NODES, normalize_kind

__all__ = [
    "DIAGRAM_SYSTEM",
    "KINDS",
    "MAX_NODES",
    "build_diagram",
    "declined",
    "graph_diagram",
    "validate_spec",
]

#: Below this share of cited elements a diagram is declined. Half, not more:
#: a process diagram legitimately has a start and an end nobody wrote down,
#: but a diagram that is mostly inference is a guess with arrows.
MIN_GROUNDED = 0.5

#: The output cap for a drawing call. A spec is a few hundred to ~2,000
#: tokens of JSON, but a reasoning model (GPT-6, Gemini 2.5, anything that
#: thinks first) spends hidden tokens out of the same cap before it writes a
#: character — so the old 1,600 was routinely spent on thinking alone, and the
#: reply was an empty 200 that read as "the model did not return a diagram".
#: A model that does not think writes what it writes and stops; the cap costs
#: it nothing.
DIAGRAM_OUTPUT_TOKENS = 8192

#: Transport failures a drawing call absorbs rather than raising: a visual is
#: an addition to an answer, and must never be the thing that fails it.
_CALL_FAILURES = (ProviderError, OSError, ValueError)


def build_diagram(
    request: str,
    citations: list[dict],
    llm: Any,
    *,
    prompt: str,
    kind: str | None = None,
    evidence: dict[int, str] | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Ask the model for a spec and validate it. Never raises.

    ``prompt`` is the passage block the answering step already built — the
    visual reads exactly the evidence the answer read. ``kind`` pins a shape
    (a vocabulary kind or an alias such as "org chart"); without it the model
    picks the one the request implies. ``evidence`` (passage number → text)
    lets chart values be checked against what the passages state.

    At most two turns: the drawing, and one repair if the first reply could
    not be read (the module docstring says why a grounding decline gets none).
    A declined result says which it was; the caller decides what to show
    instead.
    """

    pinned = normalize_kind(kind)
    valid = sorted({int(c["index"]) for c in citations if c.get("index") is not None})
    system = visual_prompt.system(pinned)
    budget = max(
        int(max_output_tokens or 0),
        int(getattr(llm, "max_output_tokens", 0) or 0),
        DIAGRAM_OUTPUT_TOKENS,
    )
    turn = visual_prompt.user(prompt, request, valid)
    raw, failure = _ask(llm, system, turn, budget)
    if failure is not None:
        record_model_outcome(llm, "no_reply")
        return declined(f"the model did not return a diagram ({failure})", retryable=True)
    result = _read(raw, citations, pinned, evidence)
    if _readable(result):
        record_model_outcome(llm, "drawn" if result["status"] == "ok" else "ungrounded")
        return result

    problem = result.get("reason") or "it was not a diagram"
    repaired_raw, failure = _ask(
        llm, system, visual_prompt.repair(prompt, request, valid, raw, problem), budget
    )
    if failure is None:
        repaired = _read(repaired_raw, citations, pinned, evidence)
        if _readable(repaired):
            record_model_outcome(llm, "repaired" if repaired["status"] == "ok" else "ungrounded")
            return repaired
    record_model_outcome(llm, "unreadable")
    return result


def _ask(llm: Any, system: str, turn: str, budget: int) -> tuple[str | None, str | None]:
    """One drawing turn: ``(reply, None)`` or ``(None, why there is none)``.

    A reply cut off before any text is retried with room to think by
    :meth:`LLM.complete` itself; anything that still fails is reported here,
    never raised.
    """

    try:
        return llm.complete(system, turn, max_output_tokens=budget, json_mode=True), None
    except _CALL_FAILURES as exc:
        return None, str(exc) or type(exc).__name__


def _read(
    raw: str | None,
    citations: list[dict],
    pinned: str | None,
    evidence: dict[int, str] | None,
) -> dict[str, Any]:
    parsed = visual_dialect.extract(raw)
    if parsed is None:
        return declined("the model did not return a diagram")
    spec = visual_dialect.normalize(parsed, kind=pinned)
    if pinned:
        # The caller asked for this shape; a model that answered in another
        # one does not get to overrule the reader.
        spec["kind"] = pinned
    return validate_spec(spec, citations, source="model", evidence=evidence)


def _readable(result: dict[str, Any]) -> bool:
    """Drawn, or declined *on grounding* — either way the model was understood."""

    return result.get("status") == "ok" or "grounding" in result


def record_model_outcome(llm: Any, outcome: str) -> None:
    """Count how the model half of a diagram went, per provider."""

    from pheasant.telemetry import metrics

    metrics.REGISTRY.inc(
        "pheasant_assistant_visual_model_total",
        provider=str(getattr(llm, "provider", "") or "unknown"),
        outcome=outcome,
    )


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
