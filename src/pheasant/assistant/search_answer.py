"""``@search``: the ranked hybrid-search hits, written out, with no model.

A grounded answer is a model's reading of the passages. Sometimes the reader
wants the passages: to judge the evidence themselves, to see what the
retriever ranks first, or because no model is connected and they want more
than the extractive answer's top three. ``@search <query>`` is that. It runs
the assistant's own retrieval call (the same arms, criteria, memory policy and
ACL as every answer) and returns the hits as a numbered table. Every row is a
citation, so the UI's source strip and the canvas work as they do for any
answer, and ``page N`` pages through deeper ranks.

Nothing is rewritten from history, nothing is planned, and no model is called,
so two identical questions over an unchanged region return identical text.
"""

from __future__ import annotations

import re
import time
from typing import Any

from pheasant.assistant.chat import passages_to_citations

#: Hits per page, and how deep ``page N`` may go.
PAGE_SIZE = 10
MAX_DEPTH = 100

_PAGE_RE = re.compile(r"(?:,? ?\(?(?:page|pg\.?|p\.) ?(?P<page>\d{1,3})\)?)\s*$", re.IGNORECASE)
_EXCERPT_CHARS = 220


def split_page(query: str) -> tuple[str, int]:
    """``("credential rotation", 2)`` for ``"credential rotation page 2"``."""

    matched = _PAGE_RE.search(query or "")
    if matched and matched.start() > 0:
        return query[: matched.start()].strip(" ,"), max(1, int(matched.group("page")))
    return (query or "").strip(), 1


def answer(
    query: str,
    retriever: Any,
    *,
    source_name: str | None,
    principal: str | None,
    principal_groups: list[str] | None,
    report: Any,
) -> Any:
    """A ``WorkflowResult`` listing the hits for ``query``."""

    from pheasant.assistant.workflows import WorkflowResult, WorkflowStep

    text, page = split_page(query)
    classify = WorkflowStep(name="classify", detail="@search: the ranked hits, no model")
    report(classify)
    started = time.perf_counter()
    depth = min(page * PAGE_SIZE, MAX_DEPTH)
    passages = (
        retriever.search(
            text,
            mode="hybrid",
            limit=depth,
            source_name=source_name,
            principal=principal,
            principal_groups=principal_groups,
        )
        if text
        else []
    )
    citations = passages_to_citations(passages, depth)
    start = (page - 1) * PAGE_SIZE
    shown = citations[start : start + PAGE_SIZE]
    searched = WorkflowStep(
        name="search",
        detail=f"hybrid search: {len(citations)} hit(s), showing {len(shown)}",
        passages=len(shown),
        duration_seconds=time.perf_counter() - started,
    )
    report(searched)
    for citation in shown:
        citation["used"] = True
    return WorkflowResult(
        answer=render(text, shown, total=len(citations), page=page, depth=depth),
        citations=shown,
        retrieved_evidence_ids=[str(c.get("chunk_id") or c.get("node_id")) for c in shown],
        focus_node_ids=[str(c["node_id"]) for c in shown if c.get("node_id")],
        mode="search",
        search_mode="hybrid",
        counts={"intent": "search", "depth": "short", "results": len(shown)},
        steps=[classify, searched],
        workflow="search",
        route={
            "intent": "search",
            "depth": "short",
            "why": {"intent": "@search asks for the hits themselves", "depth": "a listing"},
            "decided_by": {"intent": "keyword", "depth": "keyword"},
        },
    )


def render(query: str, citations: list[dict], *, total: int, page: int, depth: int) -> str:
    if not query:
        return "Add what to search for after `@search`, for example `@search credential rotation`."
    if not citations:
        if total:
            return f"“{query}” has {total} hit(s); page {page} is past the last of them."
        return f"Nothing in the indexed sources matches “{query}”."
    rows = []
    for citation in citations:
        path = citation.get("relative_path") or citation.get("title") or ""
        where = f"{citation.get('source_id') or ''}/{path}"
        section = citation.get("section") or citation.get("heading_path") or ""
        excerpt = _plain(" ".join(str(citation.get("snippet") or "").split()))
        if len(excerpt) > _EXCERPT_CHARS:
            excerpt = excerpt[:_EXCERPT_CHARS].rstrip() + "…"
        score = citation.get("score")
        rows.append(
            f"| [{citation['index']}] | `{_cell(where)}` | {_cell(section)} | "
            f"{f'{score:.3f}' if isinstance(score, int | float) else '—'} | {_cell(excerpt)} |"
        )
    first = citations[0]["index"]
    lines = [
        f"**Hybrid search for “{_cell(query)}”** — hits {first}–{citations[-1]['index']}"
        + (f" of the top {total}" if total >= depth else f" of {total}")
        + ", ranked. Nothing below was written by a model.",
        "",
        "| # | Document | Section | Score | Excerpt |",
        "|---|---|---|---|---|",
        *rows,
    ]
    if total >= depth and depth < MAX_DEPTH:
        lines += ["", f"Deeper: `@search {query} page {page + 1}`."]
    return "\n".join(lines)


def _plain(text: str) -> str:
    """An excerpt as text: Markdown links read as their words, markup escaped.

    The passage is quoted, not rendered. A link in it would point the reader
    at a path relative to a document they are not looking at.
    """

    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return re.sub(r"([\\`*_\[\]<>#])", r"\\\1", text)


def _cell(value: Any) -> str:
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return re.sub(r"\[(\d{1,2}|fig:\d{1,2})\]", r"(\1)", text)
