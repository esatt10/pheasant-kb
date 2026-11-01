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
import re
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
    # One query per component, and a request can name two dozen of them:
    # eight queries for 26 components bundled three a query, and a bundled
    # query's ten results were the overview pages, not the components' own.
    "planned_queries": 24,
    "followup_queries": 4,
    "per_query_results": 10,
    "max_context_passages": 64,
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
    # Documents are read in window order until this runs out, so it has to
    # cover the window: at 320k a 64-passage window over 46 documents read 40
    # of them, and the six it starved were components the writer then called
    # undocumented.
    "context_budget_chars": 400_000,
    # One call writes the whole artifact: sections written apart from each
    # other cannot reference each other's names.
    "max_output_tokens": 24_000,
}

#: Added to the planner's rules for a compose request.
PLANNER_COMPOSE_RULE = """
- This request composes a new artifact from many documented parts. List the \
components it needs and plan ONE query per component, each naming that \
component exactly as the corpus vocabulary spells it, in full. When the \
documents in scope are listed and one of them is that component's own page, \
add that document's name to the query too. Breadth across components matters \
more than depth on any one."""


def documents_in_scope(retriever: Any) -> str:
    """The planner's line naming the documents in scope, or ``""``.

    A component's own page is usually named after it, and a query naming it
    is what finds that page (``own_pages_first``): measured, the planner
    searched "API policy llm-token-limit" for a component whose page is
    ``api_management_api_policy``, and the page was never retrieved.
    """
    read = getattr(retriever, "document_names", None)
    names = read() if callable(read) else []
    return f"\nDocuments in scope, by name: {', '.join(names)}" if names else ""


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


#: A name spelled as an artifact spells a component — ``azurerm_key_vault``,
#: ``Microsoft.ApiManagement/service/backends`` — rather than a plain word.
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[._/][A-Za-z0-9]+)+")
#: A plain word long enough to be a document's whole name.
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{3,}")


def own_pages_first(passages: list, texts: list[str]) -> list:
    """Each named component's own page ahead of the rest of ``passages``.

    ``texts`` are the question and the queries searched for it; a name in them
    whose own page (``compose_review.names_page``, the rule the review checks
    keys against) was retrieved gets that page's best passage moved to the
    front, in rank order, and everything else keeps its order behind them.
    Measured on a 26-component request: every per-component query found the
    component's reference page, at ranks 3 to 10 behind two overview pages
    that ranked first for every query, so the merged window was the
    overviews' and the writer said the components were undocumented.

    A plain word names a page only by being its whole name (``subnet``,
    ``subscription``): the planner is shown the documents in scope by name
    (``documents_in_scope``) and writes them as they are listed.
    """
    names = {match.group(0) for text in texts for match in _NAME.finditer(text or "")}
    words = {match.group(0).lower() for text in texts for match in _WORD.finditer(text or "")}
    if not names and not words:
        return passages
    first, rest, seen = [], [], set()
    for passage in passages:
        key = str(getattr(passage, "node_id", "") or passage.key())
        title = str(getattr(passage, "relative_path", None) or getattr(passage, "title", "") or "")
        stem = title.replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0].lower()
        if key not in seen and (
            stem in words or any(compose_review.names_page(name, title) for name in names)
        ):
            seen.add(key)
            first.append(passage)
        else:
            rest.append(passage)
    return first + rest


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


_DECLARATION = re.compile(r'^\s*[A-Za-z_][\w-]*\s+"[^"]+"(?:\s+"[^"]+")?\s*\{', re.M)


def lost_code(draft: str, revised: str) -> bool:
    """Whether a rewrite lost part of the artifact it was asked to correct.

    Counted in declarations (``kind "type" "name" {`` and ``kind "name" {``)
    where the artifact has them, and in fenced blocks otherwise. Counting
    fences alone discarded a rewrite that added the one component the review
    asked for, because it returned the same declarations in fewer blocks.
    """
    before = "\n".join(compose_review.code_blocks(draft))
    after = "\n".join(compose_review.code_blocks(revised))
    declared = len(_DECLARATION.findall(before))
    if declared:
        return len(_DECLARATION.findall(after)) < declared
    return len(compose_review.code_blocks(revised)) < len(compose_review.code_blocks(draft))


#: At most this many reference pages are read for the review's checks.
REVIEW_PAGES = 16


def reference_for_review(retriever: Any, answer: str, documents: dict) -> dict:
    """Own reference pages of components the artifact declares and the window left out.

    Read for the review's deterministic checks only -- the writer and the
    model reviewer keep the evidence the answer was written from. Measured: a
    project resource whose page the window lacked went out without the block
    that page marks Required, because the check had nothing to read it from.
    """
    code = "\n".join(compose_review.code_blocks(answer))
    wanted = dict.fromkeys(kind for kind, _ in compose_review.declared_components(code) if kind)
    missing = [kind for kind in wanted if not compose_review.reference_pages(kind, documents)]
    index_of = getattr(retriever, "document_index", None)
    read = getattr(retriever, "documents", None)
    if not missing or not callable(index_of) or not callable(read):
        return {}
    index = index_of()
    ids = []
    for kind in missing:
        node_id = next(
            (i for stem, i in index.items() if compose_review.names_page(kind, stem)), None
        )
        if node_id and node_id not in ids:
            ids.append(node_id)
    if not ids:
        return {}
    try:
        pages = read(
            ids[:REVIEW_PAGES],
            max_chars=24_000,
            large_file_bytes=10**9,
            budget_chars=24_000 * REVIEW_PAGES,
        )
    except Exception:  # noqa: BLE001 - a check's extra evidence, never a blocker
        return {}
    return {f"reference:{node_id}": page for node_id, page in pages.items()}


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
        max_chars=int(options.get("context_budget_chars") or 400_000),
        reference=reference_for_review(ctx.get("retriever"), answer, documents),
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
    named = len(checked.unknown) + len(checked.issues)
    revised, step = _edit(
        llm, system, prompt, options, checked, answer, f"correcting {named} identifier(s)"
    )
    steps.append(step)
    if revised is None:
        return _Reviewed(answer, steps)
    # The rewrite is checked too, by the two checks that need no model and are
    # never wrong about what they report: measured, a rewrite asked to add a
    # missing component added it without the block its page marks Required.
    recheck = compose_review.review(
        None,
        state["question"],
        revised,
        documents,
        max_chars=0,
        reference=reference_for_review(ctx.get("retriever"), revised, documents),
    )
    left = compose_review.Review(unresolved=recheck.unresolved, required=recheck.required)
    if not left.found():
        return _Reviewed(revised, steps)
    fixed, step = _edit(
        llm,
        system,
        prompt,
        options,
        left,
        revised,
        f"completing {len(left.unresolved) + len(left.required)} reference(s) or "
        "required argument(s) the rewrite left",
    )
    steps.append(step)
    return _Reviewed(fixed or revised, steps)


def _edit(
    llm: Any,
    system: str,
    prompt: str,
    options: dict,
    checked: compose_review.Review,
    answer: str,
    what: str,
) -> tuple[str | None, WorkflowStep]:
    """One rewrite of ``answer`` correcting exactly what ``checked`` found.

    The rewrite edits the draft rather than writing a new one. Sent only the
    passages and the corrections, it regenerated from scratch: measured, a
    rewrite asked to add one missing component dropped two others and broke
    names the draft had right, and "change nothing else" meant nothing with
    nothing to keep. ``None`` when the rewrite is unavailable or lost code.
    """
    started = time.perf_counter()
    draft = "" if checked.no_artifact else f"\n\nDRAFT TO CORRECT:\n{answer}"
    try:
        revised = llm.complete(
            system + checked.revision(),
            prompt + draft,
            max_output_tokens=options.get("max_output_tokens"),
        )
    except ProviderError as exc:
        detail = f"rewrite unavailable ({short_reason(str(exc))}); returned as written"
        return None, WorkflowStep(name="revise", detail=detail)
    elapsed = time.perf_counter() - started
    # A rewrite that lost the artifact is worse than the identifiers it fixed.
    if lost_code(answer, revised):
        detail = "the rewrite dropped part of the artifact; kept the previous version"
        return None, WorkflowStep(name="revise", detail=detail, duration_seconds=elapsed)
    return revised, WorkflowStep(
        name="revise", detail=f"rewrote the artifact {what}", duration_seconds=elapsed
    )
