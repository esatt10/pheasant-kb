"""Answering a question about the knowledge base itself: look it up, write it out.

``assistant.inventory`` decides whether a question is one. This module answers
it from ``services.inventory``, the operation behind the ``describe_knowledge_base``
and ``list_documents`` tools, and writes the result as Markdown. Every number
and path in the text comes from that result. No model is called, so the answer
is as fast and as repeatable as the tool call. It also carries a footer naming
the tool and the ``@pheasant`` keyword, so a reader always knows how it was
produced and how to ask for it explicitly.

:func:`answer` is what ``assistant.answering`` calls around every workflow. It
returns a ``WorkflowResult``, so the payload, the trace and the route take the
same shape as any other answer.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from pheasant.assistant.inventory import KEYWORD, RECENT_DEFAULT, InventoryQuestion

logger = logging.getLogger(__name__)


def answer(
    asked: InventoryQuestion,
    question: str,
    *,
    config: Any,
    state: Any,
    search: Any,
    graph: Any,
    principal: str | None,
    principal_groups: list[str] | None,
    max_items: int,
    report: Any,
) -> tuple[Any, dict | None]:
    """``(WorkflowResult, data)``, or ``(None, None)`` when the lookup failed.

    On failure the caller answers by retrieval. A listing that cannot be
    produced should cost the reader a slower answer, not no answer. ``report``
    publishes each step as it completes.
    """

    from pheasant.assistant.workflows import WorkflowResult, WorkflowStep
    from pheasant.services import ServiceContext

    label = "explicit @pheasant" if asked.trigger == "keyword" else "rule"
    classify = WorkflowStep(
        name="classify", detail=f"about the knowledge base itself — {asked.why} ({label})"
    )
    report(classify)
    started = time.perf_counter()
    try:
        context = ServiceContext(config=config, state=state, searcher=search, graph=graph)
        found = lookup(
            asked,
            context,
            principal=principal,
            principal_groups=principal_groups,
            max_items=max_items,
        )
        text = render(asked, found, max_items=max_items)
    except Exception:
        logger.exception("inventory lookup failed for %r; answering by retrieval", question)
        return None, None
    result = found["result"]
    listed = result.get("documents") if isinstance(result.get("documents"), list) else []
    looked_up = WorkflowStep(
        name="inventory",
        detail=f"{found['tool']}: {asked.action}"
        + (f" ({len(listed)} of {result.get('total', 0)} documents)" if listed else ""),
        duration_seconds=time.perf_counter() - started,
    )
    report(looked_up)
    data = {
        "action": asked.action,
        "trigger": asked.trigger,
        "tool": found["tool"],
        "filters": {
            "source_name": asked.source_name,
            "extensions": list(asked.extensions),
            "path_contains": asked.path_contains,
            "limit": asked.limit,
        },
        "result": result,
    }
    return (
        WorkflowResult(
            answer=text,
            focus_node_ids=[str(doc["id"]) for doc in listed if doc.get("id")],
            mode="inventory",
            search_mode="inventory",
            counts={"intent": "inventory", "depth": "short", "documents": len(listed)},
            steps=[classify, looked_up],
            workflow="inventory",
            route={
                "intent": "inventory",
                "depth": "short",
                "why": {"intent": asked.why, "depth": "a listing has no length"},
                "decided_by": {"intent": asked.trigger, "depth": "rule"},
            },
        ),
        data,
    )


def lookup(
    asked: InventoryQuestion,
    context: Any,
    *,
    principal: str | None = None,
    principal_groups: list[str] | None = None,
    max_items: int = 50,
) -> dict[str, Any]:
    """Run the operation ``asked`` names. Returns ``{"tool", "result"}``."""

    from pheasant.services import inventory

    max_items = max(1, min(int(max_items or 50), inventory.MAX_DOCUMENTS))
    if asked.action in {"documents", "recent"}:
        limit = asked.limit or (RECENT_DEFAULT if asked.action == "recent" else max_items)
        result = inventory.documents(
            context,
            inventory.DocumentsRequest(
                source_name=asked.source_name,
                extensions=list(asked.extensions),
                path_contains=asked.path_contains,
                order="recent" if asked.action == "recent" else "path",
                limit=min(limit, max_items),
                principal=principal,
                principal_groups=principal_groups,
            ),
        )
        return {"tool": "list_documents", "result": result}
    if asked.action == "counts" and (asked.extensions or asked.source_name or asked.path_contains):
        result = inventory.documents(
            context,
            inventory.DocumentsRequest(
                source_name=asked.source_name,
                extensions=list(asked.extensions),
                path_contains=asked.path_contains,
                limit=1,
                principal=principal,
                principal_groups=principal_groups,
            ),
        )
        return {"tool": "list_documents", "result": result}
    result = inventory.overview(context, principal=principal, principal_groups=principal_groups)
    if asked.action == "sync":
        from pheasant.services.index_queue import queue_status

        try:
            result = {**result, "queue": queue_status(context, None, limit=20)}
        except Exception as exc:  # the listing still answers without it
            result = {**result, "queue": {"error": str(exc).splitlines()[0][:200]}}
    return {"tool": "describe_knowledge_base", "result": result}


def render(asked: InventoryQuestion, found: dict[str, Any], *, max_items: int = 50) -> str:
    """The answer text: Markdown, written from the operation's result alone."""

    result = found["result"]
    body = {
        "help": _render_help,
        "overview": _render_overview,
        "sources": _render_sources,
        "documents": _render_documents,
        "recent": _render_documents,
        "counts": _render_counts,
        "types": _render_types,
        "sync": _render_sync,
    }[asked.action](asked, result, max_items)
    notes = "".join(f"\n\n_Note: {note}._" for note in asked.notes)
    return f"{body}{notes}\n\n{_footer(asked, found['tool'])}"


def _footer(asked: InventoryQuestion, tool: str) -> str:
    how = (
        f"asked with {KEYWORD}"
        if asked.trigger == "keyword"
        else f"read as a question about the knowledge base itself; start with {KEYWORD} "
        "to ask one explicitly"
    )
    return f"_Answered directly from the index (`{tool}`), not by searching — {how}._"


def _render_help(asked: InventoryQuestion, result: dict[str, Any], _max: int) -> str:
    lead = (
        f"“{asked.unread}” did not read as a question about the knowledge base itself, "
        "so nothing was searched. Ask it without "
        f"{KEYWORD} to search the content, or use one of these.\n\n"
        if asked.unread
        else ""
    )
    totals = result.get("totals") or {}
    examples = "\n".join(
        f"- `{KEYWORD} {example}`"
        for example in (
            "list sources",
            "list documents",
            "list pdfs in <source>",
            "how many documents",
            "file types",
            "recent documents",
            "sync status",
            "overview",
        )
    )
    return (
        f"{lead}**{result.get('name') or result.get('knowledge_base')}** holds "
        f"{_n(totals.get('documents', 0), 'document')} across "
        f"{_n(totals.get('sources', 0), 'source')}. Questions about the knowledge base itself "
        f"are answered from the index directly, without searching. Start a message with "
        f"**{KEYWORD}** to ask one:\n\n{examples}"
    )


def _render_overview(asked: InventoryQuestion, result: dict[str, Any], max_items: int) -> str:
    totals = result.get("totals") or {}
    lines = [
        f"**{result.get('name') or result.get('knowledge_base')}** holds "
        f"{_n(totals.get('documents', 0), 'document')} "
        f"({_size(totals.get('size_bytes'))}) across {_n(totals.get('sources', 0), 'source')}."
    ]
    if result.get("description"):
        lines.append(f"\n{result['description']}")
    if result.get("last_indexed_at"):
        lines.append(f"\nLast indexed {_when(result['last_indexed_at'])}.")
    if result.get("sources"):
        lines.append("\n" + _sources_table(result["sources"], max_items))
    if result.get("by_extension"):
        kinds = ", ".join(
            f"{_cell(row['extension'])} {row['documents']}" for row in result["by_extension"]
        )
        lines.append(f"\nBy file type: {kinds}.")
    return "\n".join(lines)


def _render_sources(asked: InventoryQuestion, result: dict[str, Any], max_items: int) -> str:
    sources = result.get("sources") or []
    totals = result.get("totals") or {}
    if not sources:
        return "No sources are registered in this knowledge base yet."
    return (
        f"**{_n(len(sources), 'source')}** in "
        f"`{result.get('knowledge_base')}`, holding {_n(totals.get('documents', 0), 'document')}."
        f"\n\n{_sources_table(sources, max_items)}"
    )


def _render_documents(asked: InventoryQuestion, result: dict[str, Any], _max: int) -> str:
    docs = result.get("documents") or []
    total = int(result.get("total") or 0)
    what = f"{asked.type_label} document" if asked.type_label else "document"
    scope = _scope(asked)
    if not docs:
        return f"No {what}s{scope} are indexed."
    start = int((result.get("pagination") or {}).get("offset") or 0) + 1
    shown = (
        f"the {len(docs)} most recently indexed"
        if asked.action == "recent"
        else f"{start}–{start + len(docs) - 1}"
        if len(docs) < total
        else "all"
    )
    rows = [
        f"| {_cell(d['source'])} | `{_cell(d['path'])}` | {_size(d.get('size_bytes'))} | "
        f"{_when(d.get('last_indexed_at'))} |"
        for d in docs
    ]
    table = "| Source | Path | Size | Indexed |\n|---|---|---|---|\n" + "\n".join(rows)
    more = ""
    if total > len(docs) and asked.action != "recent":
        more = (
            f"\n\n…and {total - len(docs)} more. Narrow it (`{KEYWORD} list pdfs in <source>`), "
            "or page through them with `list_documents` / `GET /documents` and `offset`."
        )
    return f"**{_n(total, what)}**{scope} — showing {shown}.\n\n{table}{more}"


def _render_counts(asked: InventoryQuestion, result: dict[str, Any], max_items: int) -> str:
    if "documents" in result and isinstance(result["documents"], list):
        what = f"{asked.type_label} document" if asked.type_label else "document"
        return f"**{_n(int(result.get('total') or 0), what)}**{_scope(asked)} are indexed."
    totals = result.get("totals") or {}
    sources = result.get("sources") or []
    lines = [
        f"**{_n(totals.get('documents', 0), 'document')}** ({_size(totals.get('size_bytes'))}) "
        f"across **{_n(totals.get('sources', 0), 'source')}**."
    ]
    if sources:
        per = "; ".join(f"{_cell(s['name'])} {s['documents']}" for s in sources[:max_items])
        lines.append(f"\nPer source: {per}.")
    return "\n".join(lines)


def _render_types(asked: InventoryQuestion, result: dict[str, Any], _max: int) -> str:
    kinds = result.get("by_extension") or []
    if not kinds:
        return "No documents are indexed yet."
    rows = "\n".join(f"| {_cell(k['extension'])} | {k['documents']} |" for k in kinds)
    total = (result.get("totals") or {}).get("documents", 0)
    return f"**{_n(total, 'document')}** by file type:\n\n| Type | Documents |\n|---|---|\n{rows}"


def _render_sync(asked: InventoryQuestion, result: dict[str, Any], max_items: int) -> str:
    sources = result.get("sources") or []
    lines = []
    if result.get("last_indexed_at"):
        lines.append(f"Last indexed {_when(result['last_indexed_at'])}.")
    unhealthy = [s for s in sources if s.get("status") and s["status"] != "healthy"]
    if sources:
        lines.append(
            f"{len(sources) - len(unhealthy)} of {_n(len(sources), 'source')} healthy"
            + (
                f"; needs attention: {', '.join(_cell(s['name']) for s in unhealthy)}."
                if unhealthy
                else "."
            )
        )
        lines.append("\n" + _sources_table(sources, max_items))
    queue = result.get("queue") or {}
    if queue.get("enabled"):
        counts = queue.get("counts") or {}
        waiting = counts.get("awaiting_claim", 0) + counts.get("retry_scheduled", 0)
        lines.append(
            f"\nIndex queue ({queue.get('backend')}): {waiting} waiting, "
            f"{counts.get('claimed', 0)} claimed, {counts.get('dead', 0)} dead."
        )
    elif queue.get("error"):
        lines.append(f"\nIndex queue unavailable: {queue['error']}.")
    return "\n".join(lines) or "No sources are registered in this knowledge base yet."


def _sources_table(sources: list[dict[str, Any]], max_items: int) -> str:
    rows = [
        f"| {_cell(s['name'])} | {_cell(s.get('type') or '')} | {s.get('documents', 0)} | "
        f"{_cell(s.get('status') or ('enabled' if s.get('enabled') else 'disabled'))} | "
        f"{_when(s.get('last_indexed_at'))} |"
        for s in sources[:max_items]
    ]
    more = f"\n\n…and {len(sources) - max_items} more." if len(sources) > max_items else ""
    return (
        "| Source | Type | Documents | Status | Last indexed |\n|---|---|---|---|---|\n"
        + "\n".join(rows)
        + more
    )


def _scope(asked: InventoryQuestion) -> str:
    parts = []
    if asked.source_name:
        parts.append(f" in `{asked.source_name}`")
    if asked.path_contains:
        parts.append(f" with “{asked.path_contains}” in the path")
    return "".join(parts)


def _n(count: Any, noun: str) -> str:
    count = int(count or 0)
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def _size(value: Any) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover - the loop returns


def _when(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    return text.replace("T", " ")[:16]


def _cell(value: Any) -> str:
    """Text safe inside a Markdown table cell, without citation-looking markers."""

    text = str(value).replace("|", "\\|").replace("\n", " ")
    # `[3]` in a path would render as a citation chip of this answer.
    return re.sub(r"\[(\d{1,2}|fig:\d{1,2})\]", r"(\1)", text)
