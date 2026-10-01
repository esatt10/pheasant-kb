"""The default agentic workflow, built as a LangGraph state graph.

```
        ┌────────┐
        │  plan  │◄──────────────┐
        └───┬────┘               │
            ▼                    │ evidence is thin and
      ┌──────────┐               │ budget remains
      │ retrieve │               │
      └────┬─────┘               │
           ▼                     │
      ┌─────────┐                │
      │ expand  │  graph walk    │
      └────┬────┘                │
           ▼                     │
       ┌───────┐                 │
       │ grade │─────────────────┘
       └───┬───┘
           ▼ sufficient / out of budget
     ┌─────────────┐     ┌────────┐
     │ synthesize  │────►│ verify │
     └─────────────┘     └────────┘
```

Why a graph and not a chain: the `grade → plan` edge is conditional. A
question whose first search comes back thin gets a *different* query — one
informed by what did come back — rather than the same query with a bigger
`k`. That loop is the whole reason to reach for an agent framework here.

**Every node fully exercises pheasant's retrieval surface**, which a generic
RAG chain does not:

* `retrieve` fans out across `hybrid`, `vector` and `graph` modes, not one.
* `expand` walks the knowledge graph out of the best hits, reaching
  documents that share *no vocabulary* with the question but are connected
  through a concept, import or call edge.
* `plan` is told what the region can actually do (`RetrievalCapabilities`),
  so it never plans a semantic search against a region with no vector index.

**Everything is customizable.** Each node is a plain module-level function
taking and returning the state dict, `build_graph()` returns the compiled
LangGraph, and every knob is an `assistant.workflow_options` key. Swap a
node, re-wire the edges, or register your own workflow entirely — see
:mod:`pheasant.assistant.workflows`.

Requires ``pip install 'pheasant-kb[agent]'``.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, TypedDict

from pheasant.assistant import conversation, longform, routing
from pheasant.assistant.chat import (
    CONTENT_DEFAULTS,
    INTENTS,
    build_prompt,
    classify_intent,
    extractive_answer,
    hydrate_citations,
    mark_used_citations,
    passages_to_citations,
    short_reason,
    system_prompt_for,
)
from pheasant.assistant.providers import ProviderError, collect_token_usage
from pheasant.assistant.replies import json_object
from pheasant.assistant.workflows import WorkflowRequest, WorkflowResult, WorkflowStep

logger = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    # Which answer shape to plan and write toward: "auto" reads it off the
    # question (chat.classify_intent), or pin "knowledge" / "procedural".
    "intent": "auto",
    # How long the answer should be: "auto" reads it off the question
    # (assistant.routing.classify_depth), or pin "short" / "medium" / "long".
    # "short" is the answer this workflow has always written.
    "depth": "auto",
    # How many plan→retrieve→grade rounds before answering with what we have.
    "max_rounds": 3,
    # When hybrid is selected, run it directly before planning. If hybrid is
    # unavailable, stage graph/vector before searching the remaining arms.
    # Existing deployments opt in explicitly.
    "staged_retrieval": False,
    # Search modes to fan out over. "vector" is dropped automatically when no
    # vector index is built, so leaving it on is safe.
    #
    # Deliberately NOT "hybrid" here. Hybrid = text + vector + a linear scan of
    # every graph node, and this loop issues one search per query per mode
    # across up to `max_rounds` rounds — on a 500k-node graph that scan
    # dominated everything else the agent did (measured: 10.3s per hybrid
    # search vs 0.03s text, 1.6s vector, 0.6s for a model call). The graph is
    # not lost by leaving it out: `expand_graph` below walks the graph out of
    # the best hits, which is the structural signal this loop actually wants.
    # Set `retrieval_modes: ["hybrid"]` per request to trade the latency back
    # for graph-scored candidates.
    "retrieval_modes": ["text", "vector"],
    # Walk the knowledge graph out of the best hits for related material.
    "expand_graph": True,
    "expand_depth": 2,
    "expand_per_node": 4,
    # Passages fetched per query per mode.
    "per_query_results": 8,
    # Total passages offered to the synthesis step.
    "max_context_passages": 12,
    # Ask the model to grade its own evidence before answering.
    "grade_evidence": True,
    "grader_model": None,
    # Drop [n] markers that do not resolve to a real citation.
    "verify_citations": True,
    "max_facts": 12,
    # Read whole files behind the hits, not 500-char chunk previews.
    **CONTENT_DEFAULTS,
}

#: The retrieval half of the knowledge/procedural split. The two intents want
#: measurably different *evidence*, not just different wording:
#:
#: * a knowledge summary is answered by **breadth** — more documents, less of
#:   each, because the answer is how the parts relate;
#: * a procedural answer is answered by **depth** — fewer documents read
#:   further, because a usage example that stops halfway is not an example.
#:
#: Anything the caller set explicitly in ``workflow_options`` still wins; a
#: profile only overrides values that were still at their default.
INTENT_PROFILES: dict[str, dict[str, Any]] = {
    "knowledge": {
        "per_query_results": 6,
        "max_context_passages": 12,
        "expand_graph": True,
        "expand_per_node": 3,
        "passage_chars": 5000,
    },
    "procedural": {
        "per_query_results": 8,
        "max_context_passages": 8,
        "expand_graph": True,
        # Tighter: a procedural answer drifts if the graph walk wanders into
        # material that merely shares a concept with the tool being used.
        "expand_per_node": 2,
        "passage_chars": 9000,
    },
}

PLANNER_SYSTEM = """You plan retrieval over a private knowledge base. \
Given a question and a STRUCTURAL DESCRIPTION of the knowledge base, produce \
the search queries most likely to surface the answer.

The description tells you what this corpus actually is: its sources and \
their types, its directory layout, its file types and languages, the \
vocabulary its own documents use, and the symbols its code defines. Plan \
against that structure. Queries reusing the corpus's real directory names, \
file names, identifiers and vocabulary hit the lexical index exactly; \
generic paraphrases of the question do not.

Reply with JSON only, no prose:
{"queries": ["...", "..."], "modes": ["text"], "intent": "knowledge", \
"reasoning": "one short line"}

Rules:
- 1 to 3 queries. Where the question is compound, split it into its parts.
- Do not restate the question verbatim as the only query; add the specific \
terms a document answering it would contain.
- Ground at least one query in something structural from the description: a \
real directory, module, file name, symbol or corpus term. If the question \
names a thing that appears in the vocabulary or symbol list, use that exact \
spelling.
- LOCATION IS A SEARCH TERM. Many files share a name — a repository has one \
README.md per package, an __init__.py per module — so a bare file name cannot \
identify one of them. When the question is about a specific component, put \
its directory or package name IN the query beside the file kind: prefer \
"devui readme" or "packages/devui README" over "devui package overview". \
Ranking prefers files nearer the root, so a nested file needs its location \
said out loud to be found; a query that names only the file kind will return \
the project-wide one.
- Say the file kind plainly when the question implies one ("readme", \
"changelog", "pyproject", "test") — it matches the filename directly, which \
ranks far above a paraphrase of what the file contains.
- "modes" may include only the modes listed as available. Prefer "vector" \
when the question is conceptual and "text" when it names an exact \
identifier, path or symbol.
- "intent" is "procedural" when the asker wants to DO something (steps, \
usage, configuration, code examples) and "knowledge" when they want to \
UNDERSTAND something (what it is, what it does, how it is organised). A \
first reading is given to you below; change it only if it is clearly wrong.
- "depth" is "short" for a direct answer, "medium" for an overview in a few \
sections, "long" for a detailed, sectioned write-up. A first reading is given \
below; change it only if the question clearly asks for more or less.
- If an earlier conversation is shown, the question may refer back to it: \
plan queries for what it refers to, by name."""

GRADER_SYSTEM = """You judge whether retrieved passages are sufficient to \
answer a question.

Reply with JSON only, no prose:
{"sufficient": true|false, "missing": "what is absent", "next_query": "a \
better search query, or empty"}

Each passage is labelled with its file metadata — type, language, size, and \
the symbols it defines. Use it. The *shape* of the result set is evidence in \
its own right: passages that all come from documentation when the question \
needs source, or all from one directory when the question spans several, are \
a miss even when each one reads plausibly.

Be strict about sufficiency but realistic: if the passages substantially \
answer the question, say true. Only say false when a specific, nameable \
piece of information is missing that a different search might find — and let \
"next_query" go after that gap by name (a symbol, a path, a file type).

If the passages are the RIGHT KIND of file from the WRONG PLACE — a README \
from another package, a test instead of the implementation — that is a \
location miss, not a content miss. Say false and make "next_query" name the \
directory or package explicitly alongside the file kind."""

#: Sufficiency means different things for the two intents, and this is where
#: the replan loop earns its keep: "the right file, with no runnable example
#: in it" is a *pass* for a summary and a *fail* for a how-to.
GRADER_CRITERIA = {
    "knowledge": """
This is a knowledge-summary question. Sufficient means the passages let you \
say what the thing is, what its main parts are, and what each is for. \
Exhaustive coverage is not required — orientation is.""",
    "procedural": """
This is a procedural question. Sufficient means the passages contain a \
followable sequence AND at least one real example with the actual \
identifiers, imports, arguments or config keys. Passages that merely name \
the right file, or describe a capability in prose without showing its use, \
are NOT sufficient — say false and ask for the usage or example directly \
(e.g. the symbol name plus "example", "usage", or the caller's file).""",
}


class AgentState(TypedDict, total=False):
    """The state LangGraph threads through the nodes."""

    question: str
    options: dict
    #: Option keys the caller set explicitly. An intent profile may override
    #: a default, but never something the user asked for by name.
    explicit_options: list[str]
    intent: str
    depth: str
    #: ``{"depth": why}`` and ``{"depth": rule|planner|pinned}``.
    depth_why: str
    depth_by: str
    history: list
    search_question: str
    capabilities: Any
    queries: list[str]
    modes: list[str]
    passages: list
    round: int
    fallback_done: bool
    broadening_grade_pending: bool
    plan_notes: list[str]
    grade: dict
    citations: list[dict]
    facts: list[dict]
    figures: list[dict]
    answer: str
    answer_mode: str
    error: str | None
    steps: list[WorkflowStep]


def resolve_options(state: AgentState) -> dict:
    """The options in force for this question, with the intent profile applied.

    Layered lowest-to-highest: ``DEFAULTS`` → the profile for the classified
    intent → whatever the caller passed in ``workflow_options`` or the request
    body. The middle layer is why a "how do I…" question reads fewer files
    further in without anyone configuring it, and why pinning
    ``passage_chars`` by hand still wins.
    """
    options = dict(state.get("options") or {})
    profile = INTENT_PROFILES.get(str(state.get("intent") or ""), {})
    explicit = set(state.get("explicit_options") or ())
    for key, value in profile.items():
        if key not in explicit:
            options[key] = value
    # Depth layers over intent: a long procedural answer still reads files
    # deeply, it just reads more of them into more sections.
    options = routing.depth_options(str(state.get("depth") or "short"), options, explicit)
    floor = int(options.get("min_context_passages") or 0)
    options["max_context_passages"] = max(int(options["max_context_passages"]), floor)
    return options


# --------------------------------------------------------------------- nodes
# Each node is a plain function of (state, ctx) -> partial state. `ctx` carries
# the retriever and llm. Override any of them by name via
# assistant.workflow_options["nodes"], or import and reuse them in your own
# graph.


INTENT_LABELS = {
    "knowledge": "knowledge summary",
    "procedural": "procedural steps and examples",
}


def classify_node(state: AgentState, ctx: dict) -> dict:
    """Read the question as a knowledge-summary or a procedural one.

    Deterministic and model-free — the classification is a property of the
    question, so it costs nothing, cannot fail, and reads the same offline as
    it does with a provider attached. The planner may still overturn it with
    better judgement (see :func:`plan_node`); it is never *asked* to.

    Its own step exists because the reader should be able to see which way the
    agent took the question before the answer arrives. A summary served to
    someone who asked "how do I…" is the failure mode this whole split exists
    to prevent, and it is much easier to correct if the trace says so.
    """
    configured = str((state.get("options") or {}).get("intent") or "auto").strip().lower()
    if configured in INTENTS:
        intent, why = configured, "pinned by configuration"
    else:
        intent, why = classify_intent(state["question"])
    depth, depth_why, depth_by = routing.classify_depth(
        state["question"], (state.get("options") or {}).get("depth")
    )
    length = "" if depth == "short" else f"; {depth} answer ({depth_why})"
    return {
        "intent": intent,
        "depth": depth,
        "depth_why": depth_why,
        "depth_by": depth_by,
        "steps": [
            *state.get("steps", []),
            WorkflowStep(name="classify", detail=f"{INTENT_LABELS[intent]} — {why}{length}"),
        ],
    }


def plan_node(state: AgentState, ctx: dict) -> dict:
    """Decide what to search for, and in which modes."""
    retriever, llm = ctx["retriever"], ctx["llm"]
    options = resolve_options(state)
    capabilities = state.get("capabilities") or retriever.capabilities()
    question = state["question"]
    round_index = state.get("round", 0)
    intent = str(state.get("intent") or "knowledge")

    available = [m for m in options["retrieval_modes"] if m in capabilities.modes]
    if not available:
        available = ["hybrid"]
    if (
        options.get("staged_retrieval")
        and state.get("fallback_done")
        and "hybrid" in available
    ):
        # Once a broad hybrid pass has run, a grader-suggested query uses one
        # targeted hybrid pass instead of separate calls to its component arms.
        available = ["hybrid"]

    # A follow-up round already knows what was missing — use the grader's
    # suggestion rather than re-planning from scratch.
    previous = state.get("grade") or {}
    if round_index > 0 and previous.get("next_query"):
        return {
            "queries": [str(previous["next_query"])],
            "modes": available,
            "capabilities": capabilities,
            "plan_notes": [*state.get("plan_notes", []), f"refined: {previous['next_query']}"],
            "steps": [
                *state.get("steps", []),
                WorkflowStep(
                    name="replan",
                    detail=f"evidence was thin ({previous.get('missing', 'unclear')}); "
                    f"searching for “{previous['next_query']}”",
                ),
            ],
        }

    history = state.get("history") or []
    searched = state.get("search_question") or question
    queries = [searched]
    notes = "asked as-is" if searched == question else "follow-up, searched in context"
    steps = list(state.get("steps", []))
    depth = str(state.get("depth") or "short")
    depth_update: dict = {}
    if llm is not None:
        raw = llm.try_complete(
            PLANNER_SYSTEM,
            f"{capabilities.as_prompt_context()}\n\n"
            f"{conversation.history_block(history)}"
            f"First reading of the question: {intent}, {depth} answer\n"
            f"Question: {question}"
            + (f"\nStandalone form: {searched}" if searched != question else ""),
            max_output_tokens=400,
            json_mode=True,
        )
        parsed = _parse_json(raw)
        if parsed is None:
            # Say so: a planner that quietly never runs looks exactly like one
            # that ran and chose the literal question.
            notes = f"planner unavailable ({_why_not(llm, raw)}); {notes}"
        if parsed:
            planned = [str(q).strip() for q in parsed.get("queries", []) if str(q).strip()]
            if planned:
                # Always keep the original question: a planner that drifts
                # should not be able to lose the user's actual words.
                queries = _dedupe([searched, *planned])[:4]
                notes = str(parsed.get("reasoning") or "planned")
            planned_modes = [str(m) for m in parsed.get("modes", []) if m in capabilities.modes]
            # In the standard flow an explicit retrieval_modes list pins the
            # fan-out. Staged mode searches only arms not already covered.
            if planned_modes and "retrieval_modes" not in state.get("explicit_options", []):
                available = planned_modes
            # The planner reads the question with the corpus in front of it,
            # so it is allowed to overturn the heuristic — but only when the
            # intent was left on "auto", never when it was pinned.
            planned_intent = str(parsed.get("intent") or "").strip().lower()
            pinned = str(options.get("intent") or "auto").lower() in INTENTS
            if planned_intent in INTENTS and planned_intent != intent and not pinned:
                steps.append(
                    WorkflowStep(
                        name="reclassify",
                        detail=f"planner read this as {INTENT_LABELS[planned_intent]} instead",
                    )
                )
                intent = planned_intent
            # The same rule for depth: overturn only an unpinned reading.
            planned_depth = str(parsed.get("depth") or "").strip().lower()
            if (
                planned_depth in routing.DEPTHS
                and planned_depth != depth
                and state.get("depth_by") == "rule"
            ):
                steps.append(
                    WorkflowStep(
                        name="reclassify", detail=f"planner read this as a {planned_depth} answer"
                    )
                )
                depth_update = {
                    "depth": planned_depth,
                    "depth_why": "the planner's reading",
                    "depth_by": "planner",
                }

    return {
        **depth_update,
        "intent": intent,
        "queries": queries,
        "modes": available,
        "capabilities": capabilities,
        "plan_notes": [*state.get("plan_notes", []), notes],
        "steps": [
            *steps,
            WorkflowStep(
                name="plan",
                detail=f"{notes} → {len(queries)} quer{'y' if len(queries) == 1 else 'ies'} "
                f"across {', '.join(available)}",
            ),
        ],
    }


def retrieve_node(state: AgentState, ctx: dict) -> dict:
    """Run the fast first pass, or the modes selected by a later plan."""
    retriever = ctx["retriever"]
    request: WorkflowRequest = ctx["request"]
    options = resolve_options(state)
    planned = bool(state.get("queries"))
    queries = state.get("queries") or [state.get("search_question") or state["question"]]
    configured_modes = list(options.get("retrieval_modes") or ["hybrid"])
    fast_modes = [mode for mode in configured_modes if mode in {"graph", "vector"}]
    staged_retrieval = bool(options.get("staged_retrieval"))
    if staged_retrieval and "hybrid" in configured_modes:
        # Hybrid already runs text, vector, and graph together. Keep this a
        # single call so the fast-path setup does not repeat its own arms.
        first_pass_modes = ["hybrid"]
    else:
        first_pass_modes = (fast_modes or configured_modes) if staged_retrieval else configured_modes
    modes = state.get("modes") if planned else (first_pass_modes or ["hybrid"])
    modes = list(modes or ["hybrid"])
    fanout_timings: list[dict[str, Any]] = []

    found = retriever.multi_search(
        queries,
        modes=modes,
        limit=int(options["per_query_results"]),
        source_name=request.source_name,
        principal=request.principal,
        principal_groups=request.principal_groups,
        on_fanout=fanout_timings.append,
    )
    merged = _merge_passages(state.get("passages", []), found)
    # A follow-up keeps the previous question's evidence in play, on the first
    # round only: it is searched again under the same ACL and criteria as
    # everything else, and ranks below the follow-up's own hits.
    carried = conversation.carried_question(state.get("history") or [])
    if carried and state.get("search_question") and not state.get("round"):
        merged = conversation.carry(
            merged,
            retriever.multi_search(
                [carried],
                modes=modes,
                limit=int(options["per_query_results"]),
                source_name=request.source_name,
                principal=request.principal,
                principal_groups=request.principal_groups,
                on_fanout=fanout_timings.append,
                query_label="prior question",
            ),
        )
    return {
        "passages": merged,
        "queries": queries,
        "modes": modes,
        "fallback_done": (
            not staged_retrieval
            or "hybrid" in modes
            or not any(mode in {"graph", "vector"} for mode in modes)
        ),
        "steps": [
            *state.get("steps", []),
            WorkflowStep(
                name="retrieve",
                detail=f"{len(found)} passages from {len(queries)} "
                f"quer{'y' if len(queries) == 1 else 'ies'} "
                f"across {', '.join(modes)}",
                passages=len(found),
                fanout_timings=fanout_timings,
            ),
        ],
    }


def broaden_node(state: AgentState, ctx: dict) -> dict:
    """Search the remaining hybrid arms without repeating completed work."""
    retriever = ctx["retriever"]
    request: WorkflowRequest = ctx["request"]
    options = resolve_options(state)
    queries = state.get("queries") or [state.get("search_question") or state["question"]]
    searched_modes = set(state.get("modes") or [])
    modes = [mode for mode in ("text", "vector", "graph") if mode not in searched_modes]
    fanout_timings: list[dict[str, Any]] = []
    found = (
        retriever.multi_search(
            queries,
            modes=modes,
            limit=int(options["per_query_results"]),
            source_name=request.source_name,
            principal=request.principal,
            principal_groups=request.principal_groups,
            on_fanout=fanout_timings.append,
        )
        if modes
        else []
    )
    merged = _merge_passages(state.get("passages", []), found)
    carried = conversation.carried_question(state.get("history") or [])
    if carried and state.get("search_question") and not state.get("round"):
        extra = retriever.multi_search(
            [carried],
            modes=modes,
            limit=int(options["per_query_results"]),
            source_name=request.source_name,
            principal=request.principal,
            principal_groups=request.principal_groups,
            on_fanout=fanout_timings.append,
            query_label="prior question",
        )
        merged = conversation.carry(merged, extra)
    previous_modes = ", ".join(state.get("modes") or ["graph", "vector"])
    added_modes = ", ".join(modes) or "no additional modes"
    return {
        "passages": merged,
        "modes": modes,
        "fallback_done": True,
        "broadening_grade_pending": True,
        "steps": [
            *state.get("steps", []),
            WorkflowStep(
                name="broaden",
                detail=(
                    f"{previous_modes} evidence was insufficient; searched remaining modes: "
                    f"{added_modes}"
                ),
                passages=len(found),
                fanout_timings=fanout_timings,
            ),
        ],
    }


def expand_node(state: AgentState, ctx: dict) -> dict:
    """Walk the knowledge graph out of the best hits.

    This is the step a lexical or vector-only pipeline cannot do: a document
    that shares no words with the question is still reachable through the
    concepts, imports and calls pheasant recorded at index time.
    """
    retriever = ctx["retriever"]
    options = resolve_options(state)
    if not options["expand_graph"]:
        return {}
    passages = state.get("passages", [])
    if not passages:
        return {}
    related = retriever.expand(
        passages[:4],
        depth=int(options["expand_depth"]),
        per_node=int(options["expand_per_node"]),
    )
    if not related:
        return {}
    return {
        "passages": _merge_passages(passages, related),
        "steps": [
            *state.get("steps", []),
            WorkflowStep(
                name="expand",
                detail=f"followed graph edges to {len(related)} related document(s)",
                passages=len(related),
            ),
        ],
    }


def grade_node(state: AgentState, ctx: dict) -> dict:
    """Decide whether the evidence answers the question."""
    llm = ctx.get("grader_llm", ctx["llm"])
    options = resolve_options(state)
    passages = state.get("passages", [])
    fallback_grade = bool(state.get("broadening_grade_pending"))
    round_index = state.get("round", 0) + (0 if fallback_grade else 1)

    # Deterministic floor: nothing found is definitively insufficient, and
    # with no model there is nobody to ask, so take what we have.
    if not passages:
        return {
            "round": round_index,
            "broadening_grade_pending": False,
            "grade": {"sufficient": False, "missing": "no matching passages", "next_query": ""},
        }
    if llm is None or not options["grade_evidence"]:
        return {
            "round": round_index,
            "broadening_grade_pending": False,
            "grade": {"sufficient": True, "missing": "", "next_query": ""},
        }

    # Grading stays on snippets, not whole files: it is a routing decision
    # about whether to search again, it runs every round, and re-reading
    # everything here would buy a better-argued "yes" for the same answer at
    # the cost of the loop the reader is waiting on.
    #
    # Metadata is the exception, because it is nearly free and it is most of
    # what the decision turns on. Eight markdown notes under docs/ in answer
    # to "how do I call this" is a miss no snippet reveals — the prose reads
    # fine, it is the *shape* of the result set that is wrong. Handing the
    # grader paths, types, languages and the symbols each file defines lets it
    # say so, and name a next query that goes after the code.
    shapes = ctx["retriever"].metadata([p.node_id for p in passages[:8] if p.node_id])
    evidence = "\n\n".join(
        f"[{i + 1}] {p.title}{_describe(shapes.get(p.node_id or ''))}\n{p.snippet[:500]}"
        for i, p in enumerate(passages[:8])
    )
    if shapes:
        kinds = sorted({str(meta.get("type") or "?") for meta in shapes.values()})
        evidence += f"\n\nResult set: {len(passages)} passage(s), file types: {', '.join(kinds)}"
    raw = llm.try_complete(
        GRADER_SYSTEM + GRADER_CRITERIA.get(str(state.get("intent") or ""), ""),
        f"Question: {state['question']}\n\nPassages:\n{evidence}",
        max_output_tokens=300,
        json_mode=True,
    )
    parsed = _parse_json(raw)
    # An unusable grade ends the loop, as it always has — retrying rounds on a
    # grader that cannot answer would only spend them — but it says so. A
    # grader that silently read as "sufficient" is how a model switch turned
    # off every follow-up retrieval round without anything showing it.
    unavailable = None if parsed is not None else _why_not(llm, raw)
    parsed = parsed or {"sufficient": True}
    grade = {
        "sufficient": bool(parsed.get("sufficient", True)),
        "missing": str(parsed.get("missing") or ""),
        "next_query": str(parsed.get("next_query") or ""),
    }
    return {
        "round": round_index,
        "broadening_grade_pending": False,
        "grade": grade,
        "steps": [
            *state.get("steps", []),
            WorkflowStep(
                name="grade",
                detail=f"grader unavailable ({unavailable}); answering with what was found"
                if unavailable
                else "evidence is sufficient"
                if grade["sufficient"]
                else f"missing: {grade['missing'] or 'unclear'}",
                passages=len(passages),
            ),
        ],
    }


def synthesize_node(state: AgentState, ctx: dict) -> dict:
    """Write the grounded answer over the accumulated evidence.

    This is where retrieval stops being a list of the right files and starts
    being an answer about them: the cited chunks are joined back up into the
    files they came from (:func:`~pheasant.assistant.chat.hydrate_citations`)
    and the answering prompt is the one for the classified intent and depth.
    A ``long`` answer is written section by section (``assistant.longform``).
    """
    retriever, llm = ctx["retriever"], ctx["llm"]
    options = resolve_options(state)
    intent = str(state.get("intent") or "knowledge")
    depth = str(state.get("depth") or "short")
    passages = state.get("passages", [])[: int(options["max_context_passages"])]
    citations = passages_to_citations(passages, int(options["max_context_passages"]))
    node_ids = [c["node_id"] for c in citations if c.get("node_id")]
    facts = retriever.facts(node_ids, int(options["max_facts"]))
    collect = getattr(retriever, "figures", None)
    figures = collect(citations) if callable(collect) and citations else []

    if llm is None or not citations:
        return {
            "citations": citations,
            "facts": facts,
            "figures": figures,
            "answer": extractive_answer(state["question"], citations),
            "answer_mode": "extractive",
        }
    read_started = time.perf_counter()
    documents = hydrate_citations(retriever, citations, options)
    read_seconds = time.perf_counter() - read_started
    steps = list(state.get("steps", []))
    if documents:
        whole = sum(1 for doc in documents.values() if not doc.truncated)
        steps.append(
            WorkflowStep(
                name="read",
                detail=f"read {len(documents)} file(s) in full from their chunks"
                if whole == len(documents)
                else f"read {len(documents)} file(s) from their chunks "
                f"({len(documents) - whole} excerpted)",
                passages=len(documents),
                duration_seconds=read_seconds,
            )
        )
    history_text = conversation.history_block(state.get("history") or [])
    evidence = {"citations": citations, "facts": facts, "figures": figures}
    try:
        if depth == "long":
            answer, long_steps = longform.write_long(
                state, ctx, options, documents, history_text, evidence
            )
            steps.extend(long_steps)
        else:
            answer = llm.complete(
                system_prompt_for(intent, depth, figures=bool(figures)),
                build_prompt(
                    state["question"],
                    citations,
                    facts,
                    documents,
                    history_text=history_text,
                    figures=figures,
                ),
                max_output_tokens=options.get("max_output_tokens"),
            )
            steps.append(
                WorkflowStep(
                    name="synthesize",
                    detail=f"wrote a {INTENT_LABELS[intent]} answer from {len(citations)} passages"
                    + ("" if depth == "short" else f" ({depth})"),
                    passages=len(citations),
                )
            )
        return {**evidence, "answer": answer, "answer_mode": "llm", "steps": steps}
    except ProviderError as exc:
        return {
            **evidence,
            "answer": extractive_answer(
                state["question"], citations, reason=short_reason(str(exc))
            ),
            "answer_mode": "extractive",
            "error": str(exc),
            "steps": [
                *steps,
                WorkflowStep(
                    name="synthesize",
                    detail="model unavailable; returned extracted passages",
                    passages=len(citations),
                ),
            ],
        }


def verify_node(state: AgentState, ctx: dict) -> dict:
    """Strip citation markers that do not resolve to a real passage.

    A model asked to cite `[n]` will occasionally invent an `n` beyond the
    passages it was given. Emitting that unchecked would put a link in the UI
    that goes nowhere, which is worse than no citation at all.
    """
    options = resolve_options(state)
    answer = state.get("answer", "")
    citations = state.get("citations", [])
    if not options["verify_citations"] or not answer:
        mark_used_citations(answer, citations)
        return {}

    valid = {c["index"] for c in citations}
    dangling: set[int] = set()

    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index in valid:
            return match.group(0)
        dangling.add(index)
        return ""

    cleaned = re.sub(r"\[(\d{1,2})\]", replace, answer)
    cleaned = re.sub(r" +([.,;:])", r"\1", cleaned)
    mark_used_citations(cleaned, citations)
    if not dangling:
        return {"answer": cleaned}
    return {
        "answer": cleaned,
        "steps": [
            *state.get("steps", []),
            WorkflowStep(
                name="verify",
                detail=f"dropped {len(dangling)} citation marker(s) with no matching passage",
            ),
        ],
    }


def should_retry(state: AgentState, ctx: dict) -> str:
    """Choose broadening, a replan, or synthesis from the current grade."""
    options = resolve_options(state)
    grade = state.get("grade") or {}
    if grade.get("sufficient", True):
        return "synthesize"
    if options.get("staged_retrieval") and not state.get("fallback_done"):
        return "broaden"
    if state.get("round", 0) >= int(options["max_rounds"]):
        return "synthesize"
    if not grade.get("next_query") and not state.get("passages"):
        # Nothing found and no idea what else to try — another identical
        # round would just burn a model call.
        return "synthesize"
    return "plan"


def route_after_classify(state: AgentState, ctx: dict) -> str:
    """Use the direct fast pass only when staged retrieval is enabled."""
    return "retrieve" if resolve_options(state).get("staged_retrieval") else "plan"


NODES = {
    "classify": classify_node,
    "plan": plan_node,
    "retrieve": retrieve_node,
    "broaden": broaden_node,
    "expand": expand_node,
    "grade": grade_node,
    "synthesize": synthesize_node,
    "verify": verify_node,
}


#: The compiled default graph, built once per process.
#:
#: The topology below is fixed — it does not branch on ``options`` — and a
#: compiled LangGraph holds no per-invocation state (state and ctx both arrive
#: through ``invoke``), so there is nothing to rebuild per request. Compiling
#: is cheap (~10ms); the expensive part is importing langgraph at all, which
#: measured **4 seconds** and used to land on whoever asked the first question
#: after a restart.
_DEFAULT_GRAPH: Any = None


def warm() -> bool:
    """Pay the langgraph import + compile now, off the request path.

    Called at server startup in the background. Returns False when the
    ``[agent]`` extra is not installed, which is not an error: the assistant
    falls back to the simple workflow.
    """

    try:
        build_graph(DEFAULTS)
    except ImportError:
        return False
    except Exception:  # pragma: no cover - warming must never break startup
        logger.debug("agentic warm-up failed", exc_info=True)
        return False
    return True


def build_graph(options: dict[str, Any], nodes: dict[str, Any] | None = None):
    """Compile the LangGraph state graph.

    Import this to inspect or modify the default topology::

        from pheasant.assistant.workflows.agentic import build_graph, NODES
        graph = build_graph(options, nodes={**NODES, "grade": my_grader})

    The default topology is compiled once and reused; pass ``nodes`` to get a
    freshly compiled graph with your own node functions.
    """

    global _DEFAULT_GRAPH
    if nodes is None and _DEFAULT_GRAPH is not None:
        return _DEFAULT_GRAPH

    from langgraph.graph import END, START, StateGraph

    is_default = nodes is None
    nodes = nodes or NODES
    builder = StateGraph(AgentState)
    for name in (
        "classify",
        "plan",
        "retrieve",
        "broaden",
        "expand",
        "grade",
        "synthesize",
        "verify",
    ):
        # LangGraph calls node(state, config); ctx rides on the config so the
        # nodes stay plain, testable functions of (state, ctx).
        builder.add_node(
            name,
            _bind(nodes[name]),
        )
    builder.add_edge(START, "classify")
    # classify runs once. Most configured flows plan next; staged retrieval
    # starts with a direct graph/vector pass and plans only after escalation.
    builder.add_conditional_edges(
        "classify",
        _bind_router(route_after_classify),
        {"plan": "plan", "retrieve": "retrieve"},
    )
    builder.add_edge("plan", "retrieve")
    builder.add_edge("retrieve", "expand")
    builder.add_edge("expand", "grade")
    builder.add_conditional_edges(
        "grade",
        _bind_router(should_retry),
        {"broaden": "broaden", "plan": "plan", "synthesize": "synthesize"},
    )
    builder.add_edge("broaden", "expand")
    builder.add_edge("synthesize", "verify")
    builder.add_edge("verify", END)
    compiled = builder.compile()
    if is_default:
        _DEFAULT_GRAPH = compiled
    return compiled


def _bind(fn):
    def node(state, config):
        ctx = config["configurable"]["ctx"]
        before = len(state.get("steps") or [])
        started = time.perf_counter()
        with collect_token_usage() as usage:
            result = fn(state, ctx)
        elapsed = time.perf_counter() - started
        # Publish whatever this node appended, the moment it appended it. The
        # loop can take a minute over a large index, and "planning… retrieving
        # 35 passages… grading" is the difference between waiting and
        # wondering whether it hung. Nodes return the whole steps list, so
        # anything past the incoming length is new.
        request = ctx.get("request")
        if isinstance(result, dict):
            new_steps = (result.get("steps") or [])[before:]
            if new_steps:
                for step in new_steps[:-1]:
                    if step.duration_seconds is None:
                        step.duration_seconds = 0.0
                last = new_steps[-1]
                if last.duration_seconds is None:
                    last.duration_seconds = max(
                        0.0, elapsed - sum(step.duration_seconds or 0.0 for step in new_steps[:-1])
                    )
                last.input_tokens = usage.reported_input
                last.output_tokens = usage.reported_output
            for step in new_steps:
                if request is None:
                    continue
                request.report(step)
        return result

    node.__name__ = getattr(fn, "__name__", "node")
    return node


def _bind_router(fn):
    def router(state, config):
        return fn(state, config["configurable"]["ctx"])

    router.__name__ = getattr(fn, "__name__", "router")
    return router


class AgenticWorkflow:
    """Classify → plan → retrieve → expand → grade → (loop) → synthesize → verify."""

    name = "agentic"
    #: Subclasses pin one of :data:`~pheasant.assistant.chat.INTENTS`; ``None``
    #: means classify per question.
    intent: str | None = None

    def __init__(self, nodes: dict[str, Any] | None = None) -> None:
        self._nodes = nodes

    def run(self, request: WorkflowRequest, retriever: Any, llm: Any) -> WorkflowResult:
        requested = dict(request.options or {})
        options = {**DEFAULTS, **requested}
        if self.intent:
            # Choosing this workflow *is* choosing the intent; an explicit
            # per-request `intent` still wins so the pin stays overridable.
            options["intent"] = requested.get("intent") or self.intent
        # A caller asking for N results must not get fewer because an intent
        # profile prefers a smaller context.
        options["min_context_passages"] = int(request.max_results)
        options["max_context_passages"] = max(
            int(options["max_context_passages"]), int(request.max_results)
        )
        # None means "the stock topology", which is the compiled-once path.
        # Only a caller that actually swapped a node pays for a fresh compile.
        overrides = options.get("nodes") or {}
        nodes = self._nodes or ({**NODES, **overrides} if overrides else None)

        try:
            graph = build_graph(options, nodes)
        except ImportError as exc:  # extra not installed after all
            logger.warning("langgraph unavailable (%s); falling back to the simple workflow", exc)
            from pheasant.assistant.workflows.simple import SimpleWorkflow

            return SimpleWorkflow().run(request, retriever, llm)

        grader_model = options.get("grader_model")
        with_model = getattr(llm, "with_model", None)
        grader_llm = with_model(str(grader_model)) if grader_model and callable(with_model) else llm
        ctx = {"retriever": retriever, "llm": llm, "grader_llm": grader_llm, "request": request}
        initial: AgentState = {
            "question": request.question,
            "options": options,
            "explicit_options": list(requested),
            "passages": [],
            "round": 0,
            "plan_notes": [],
            "steps": [],
            "history": list(request.history or []),
            "search_question": request.search_question or "",
        }
        # `recursion_limit` is LangGraph's own runaway guard; size it to the
        # configured rounds so a pathological grader cannot spin forever.
        final = graph.invoke(
            initial,
            config={
                "configurable": {"ctx": ctx},
                "recursion_limit": 6 * max(1, int(options["max_rounds"])) + 8,
            },
        )

        citations = final.get("citations", [])
        depth = final.get("depth", "short")
        route = {
            "intent": final.get("intent", "knowledge"),
            "depth": depth,
            "why": {"depth": final.get("depth_why", "")},
            "decided_by": {"depth": final.get("depth_by", "rule")},
        }
        return WorkflowResult(
            answer=final.get("answer", ""),
            citations=citations,
            facts=final.get("facts", []),
            focus_node_ids=[c["node_id"] for c in citations if c.get("node_id")],
            mode=final.get("answer_mode", "extractive"),
            provider=llm.provider if llm else None,
            model=llm.model_id if llm else None,
            error=final.get("error"),
            search_mode="+".join(final.get("modes", [request.mode])),
            counts={
                "rounds": final.get("round", 0),
                "passages": len(final.get("passages", [])),
                "citations": len(citations),
                # How the question was read. Surfaced so a caller can tell a
                # summary from a how-to without re-parsing the answer.
                "intent": final.get("intent", "knowledge"),
                "depth": depth,
            },
            steps=final.get("steps", []),
            workflow=self.name,
            figures=final.get("figures", []),
            route=route,
        )


class KnowledgeSummaryWorkflow(AgenticWorkflow):
    """The agentic graph, pinned to the knowledge-summary reading.

    Same topology and the same nodes — only the intent is fixed, which fixes
    the retrieval profile (breadth over depth), the sufficiency bar and the
    answering prompt with it. Worth its own registration because "explain this
    corpus to me" is a standing mode of use, not a per-question accident: a
    reader browsing an unfamiliar knowledge base wants orientation even when
    a particular question happens to contain the word "use".
    """

    name = "knowledge-summary"
    intent = "knowledge"


# ------------------------------------------------------------------ helpers


def _describe(meta: dict | None) -> str:
    """One inline clause of file metadata, for the grader's evidence list."""
    if not meta:
        return ""
    bits = [str(meta["type"])] if meta.get("type") else []
    if meta.get("language"):
        bits.append(str(meta["language"]))
    if meta.get("lines"):
        bits.append(f"{meta['lines']} lines")
    if meta.get("chunk_count"):
        bits.append(f"{meta['chunk_count']} chunk(s)")
    if meta.get("symbols"):
        bits.append("defines " + ", ".join(meta["symbols"]))
    return f"  ({' · '.join(bits)})" if bits else ""


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(value.strip())
    return out


def _merge_passages(existing: list, incoming: list) -> list:
    """Union by passage key, keeping the strongest score; stable order."""
    merged = {p.key(): p for p in existing}
    for passage in incoming:
        current = merged.get(passage.key())
        if current is None or passage.score > current.score:
            merged[passage.key()] = passage
    return sorted(merged.values(), key=lambda p: -p.score)


def _parse_json(raw: str | None) -> dict | None:
    """Parse a JSON object out of a model reply (``assistant.replies``)."""
    return json_object(raw)


def _why_not(llm: Any, raw: str | None) -> str:
    """Why an optional model call produced nothing usable, in a few words."""
    failure = getattr(llm, "last_failure", None)
    if failure:
        return short_reason(str(failure))
    return "reply was not JSON" if raw else "no reply"
