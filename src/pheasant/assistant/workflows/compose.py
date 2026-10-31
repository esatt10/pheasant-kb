"""The compose answer shape's own pieces, apart from the loop that runs them.

A compose question asks for a new artifact assembled from documented parts
(``chat.COMPOSE_SYSTEM``). Everything here exists because the other shapes
assume the finished thing is in the corpus: the breadth it runs with, the
planner rule that splits it into components, the window it is written from
and the identifier review it gets before it is returned. ``agentic`` reads
these; this module reads neither it nor ``decision_gates``.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from pheasant.assistant import compose_review
from pheasant.assistant.chat import short_reason
from pheasant.assistant.providers import ProviderError
from pheasant.assistant.workflows import WorkflowStep

#: What a compose question runs with, whatever the general keys say.
#:
#: A composed artifact needs every component it is made of, which is breadth
#: the general keys are deliberately tuned not to spend on a 150-word answer
#: (and a config that sets them pins them against the intent profiles below).
#: So compose's knobs are their own block, ``compose_options``, applied over
#: everything else for compose questions and for nothing else. Measured on a
#: 43-document infrastructure corpus asking for an 18-resource configuration:
#: one staged query found 10 passages from 4 documents and the writer declined;
#: planned per-component queries found 44 passages from 22 documents.
COMPOSE_DEFAULTS: dict[str, Any] = {
    # Plan before searching: the question names a dozen components, and one
    # search of all of them at once returns the two its words weigh most.
    "staged_retrieval": False,
    "defer_graph_until_insufficient": False,
    # The combined call writes a capped, JSON-wrapped short answer.
    "combine_grade_and_answer": False,
    "planned_queries": 8,
    "followup_queries": 4,
    "per_query_results": 10,
    "max_context_passages": 40,
    # Proportional to each document's size, not a fixed count: a 120-chunk
    # PDF's overview may need 30 of its passages, a one-page note needs two.
    "passages_per_document": "auto",
    "document_fraction": 0.25,
    "document_floor": 2,
    "document_window_share": 0.75,
    # ...and the passages a document is given are then actually read: its
    # hydration allowance grows with how many of them the window kept.
    "scale_document_chars": True,
    "chars_per_passage": 4000,
    "expand_per_node": 2,
    "passage_chars": 12_000,
    "code_passage_chars": 24_000,
    "context_budget_chars": 240_000,
    # One call writes the whole artifact: sections written apart from each
    # other cannot reference each other's names.
    "max_output_tokens": 24_000,
}

#: Added to the planner's rules for a compose request.
PLANNER_COMPOSE_RULE = """
- This request composes a new artifact from many documented parts. List the \
components it needs and plan ONE query per component, each naming that \
component exactly as the corpus vocabulary spells it. Breadth across \
components matters more than depth on any one."""


def document_quota(chunk_count: int | None, options: dict) -> int | None:
    """How many of the window's passages one document may take, or ``None``.

    An integer ``passages_per_document`` is a fixed cap. ``"auto"`` scales with
    the document: ``document_fraction`` of its chunks, never below
    ``document_floor`` and never more than ``document_window_share`` of the
    window — a fixed cap of two would read two passages of a 300-page PDF whose
    overview is spread over thirty, and no cap at all lets one long file push
    every other component out of a composed artifact. A document whose size is
    unknown gets the floor.
    """
    cap = options.get("passages_per_document")
    if isinstance(cap, bool):
        return None
    if isinstance(cap, int):
        return cap if cap > 0 else None
    if str(cap or "").lower() != "auto":
        return None
    limit = int(options["max_context_passages"])
    floor = max(1, int(options.get("document_floor") or 2))
    ceiling = max(floor, int(limit * float(options.get("document_window_share") or 0.75)))
    if not chunk_count:
        return floor
    fraction = float(options.get("document_fraction") or 0.25)
    return max(floor, min(ceiling, math.ceil(chunk_count * fraction)))


def document_sizes(retriever: Any, passages: list, options: dict) -> dict[str, int]:
    """``{node_id: chunk_count}`` for the documents behind ``passages``.

    Read only when quotas are ``"auto"``, from the index's own chunk counts
    (``PheasantRetriever.metadata``, one cached query batch); a retriever
    without it leaves every document at the floor.
    """
    if str(options.get("passages_per_document") or "").lower() != "auto":
        return {}
    read = getattr(retriever, "metadata", None)
    node_ids = list(dict.fromkeys(str(p.node_id) for p in passages if getattr(p, "node_id", None)))
    if not callable(read) or not node_ids:
        return {}
    try:
        shapes = read(node_ids)
    except Exception:  # noqa: BLE001 - sizes refine the window, never block it
        return {}
    return {node: int(meta.get("chunk_count") or 0) for node, meta in shapes.items()}


def synthesis_window(passages: list, options: dict, sizes: dict[str, int] | None = None) -> list:
    """The top ``max_context_passages``, each document within its quota.

    ``sizes`` is ``{node_id: chunk_count}``, which ``"auto"`` quotas read.
    Ranking is unchanged: a passage passed over for its file's quota leaves
    room for the next document down the list, and nothing past the window
    moves up ahead of something inside it.
    """
    limit = int(options["max_context_passages"])
    if document_quota(1, options) is None:
        return passages[:limit]
    taken: dict[str, int] = {}
    quotas: dict[str, int] = {}
    window = []
    for passage in passages:
        key = str(getattr(passage, "node_id", "") or passage.key())
        if key not in quotas:
            quotas[key] = document_quota((sizes or {}).get(key), options) or limit
        if taken.get(key, 0) >= quotas[key]:
            continue
        taken[key] = taken.get(key, 0) + 1
        window.append(passage)
        if len(window) >= limit:
            break
    return window


@dataclass
class _Reviewed:
    text: str
    steps: list[WorkflowStep]


def review_artifact(
    state: dict,
    ctx: dict,
    options: dict,
    answer: str,
    documents: dict,
    system: str,
    prompt: str,
) -> _Reviewed:
    """Check a composed artifact identifier by identifier; rewrite once if needed."""

    llm = ctx["llm"]
    started = time.perf_counter()
    checked = compose_review.review(
        llm,
        state["question"],
        answer,
        documents,
        max_chars=int(options.get("context_budget_chars") or 240_000),
    )
    steps = [
        WorkflowStep(
            name="review",
            detail=checked.summary(),
            passages=len(documents),
            duration_seconds=time.perf_counter() - started,
        )
    ]
    if not checked.found():
        return _Reviewed(answer, steps)
    started = time.perf_counter()
    try:
        revised = llm.complete(
            system + checked.revision(),
            prompt,
            max_output_tokens=options.get("max_output_tokens"),
        )
    except ProviderError as exc:
        steps.append(
            WorkflowStep(
                name="revise",
                detail=f"rewrite unavailable ({short_reason(str(exc))}); returned as written",
            )
        )
        return _Reviewed(answer, steps)
    # A rewrite that lost the artifact is worse than the identifiers it fixed.
    if len(compose_review.code_blocks(revised)) < len(compose_review.code_blocks(answer)):
        steps.append(
            WorkflowStep(
                name="revise",
                detail="the rewrite dropped part of the artifact; kept the first draft",
                duration_seconds=time.perf_counter() - started,
            )
        )
        return _Reviewed(answer, steps)
    named = len(checked.unknown) + len(checked.issues)
    steps.append(
        WorkflowStep(
            name="revise",
            detail=f"rewrote the artifact correcting {named} identifier(s)",
            duration_seconds=time.perf_counter() - started,
        )
    )
    return _Reviewed(revised, steps)
