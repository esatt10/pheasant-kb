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
    focus = _focus_ids(result)
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
            "path": asked.path,
            "direction": asked.direction,
            "other_source": asked.other_source,
            "edge_types": list(asked.edge_types),
            "cross_source_only": asked.cross_source_only,
        },
        "result": result,
    }
    if found.get("page"):
        data["page"] = found["page"]
    return (
        WorkflowResult(
            answer=text,
            focus_node_ids=focus,
            mode="inventory",
            search_mode="inventory",
            counts={"intent": "inventory", "depth": "short", "documents": len(focus)},
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
    """Run the operation ``asked`` names. Returns ``{"tool", "result"}``, and
    ``page`` for a listing: where it is, and how to ask for the next one."""

    from pheasant.services import inventory, inventory_detail
    from pheasant.services.errors import ServiceError

    max_items = max(1, min(int(max_items or 50), inventory.MAX_DOCUMENTS))
    page = max(1, int(asked.page or 1))
    if asked.action in {"documents", "recent"}:
        limit = min(
            asked.limit or (RECENT_DEFAULT if asked.action == "recent" else max_items), max_items
        )
        request = inventory.DocumentsRequest(
            source_name=asked.source_name,
            extensions=list(asked.extensions),
            path_contains=asked.path_contains,
            order="recent" if asked.action == "recent" else "path",
            limit=limit,
            offset=(page - 1) * limit,
            principal=principal,
            principal_groups=principal_groups,
        )
        result = inventory.documents(context, request)
        params = {
            "source_name": request.source_name,
            "extension": list(result["filters"]["extensions"]),
            "path_contains": result["filters"]["path_contains"],
            "order": request.order,
        }
        return {
            "tool": "list_documents",
            "result": result,
            "page": _page(asked, result, page, limit, "/documents", params),
        }
    if asked.action == "links":
        request = inventory_detail.LinksRequest(
            source_name=asked.source_name,
            other_source=asked.other_source,
            edge_types=list(asked.edge_types),
            cross_source_only=asked.cross_source_only,
            document=asked.path,
            direction=asked.direction if asked.path else None,
            limit=max_items,
            offset=(page - 1) * max_items,
            principal=principal,
            principal_groups=principal_groups,
        )
        try:
            result = inventory_detail.links(context, request)
        except ServiceError as refused:
            if asked.trigger != "keyword" or not asked.path:
                raise
            # A document the reader named that is absent or ambiguous is said.
            return {
                "tool": "describe_document",
                "result": {"error": str(refused), "code": refused.code, "query": asked.path},
            }
        params = {
            "source_name": request.source_name,
            "other_source": request.other_source,
            "edge_type": list(request.edge_types),
            "cross_source_only": request.cross_source_only,
            "document": request.document,
            "direction": request.direction,
        }
        return {
            "tool": "list_document_links",
            "result": result,
            "page": _page(asked, result, page, max_items, "/documents/links", params),
        }
    if asked.action == "source":
        result = inventory_detail.source(
            context,
            str(asked.source_name),
            principal=principal,
            principal_groups=principal_groups,
        )
        return {"tool": "describe_source", "result": result}
    if asked.action == "document":
        try:
            result = inventory_detail.document(
                context,
                str(asked.path),
                principal=principal,
                principal_groups=principal_groups,
            )
        except ServiceError as refused:
            if asked.trigger != "keyword":
                # The rules guessed at a path; a miss is answered by searching.
                raise
            result = {"error": str(refused), "code": refused.code, "query": asked.path}
        return {"tool": "describe_document", "result": result}
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


def _page(
    asked: InventoryQuestion,
    result: dict[str, Any],
    page: int,
    size: int,
    endpoint: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Where a listing is, and how to move: as a question and as an HTTP call.

    The question is for any client, an agent over MCP included: asking it
    returns the next page, with or without the conversation's history. The
    endpoint and parameters are for a client that pages in place (the web UI
    scrolls a long listing without asking a new question).
    """

    total = int(result.get("total") or 0)
    pages = max(1, -(-total // size))
    command = f"{KEYWORD} {asked.text}".strip() if asked.text else f"{KEYWORD} {asked.action}"
    return {
        "number": page,
        "size": size,
        "total": total,
        "pages": pages,
        "next_question": f"{command} page {page + 1}" if page < pages else None,
        "previous_question": f"{command} page {page - 1}" if page > 1 else None,
        "endpoint": endpoint,
        "params": {key: value for key, value in params.items() if value not in (None, [], False)},
    }


def _focus_ids(result: dict[str, Any]) -> list[str]:
    """The graph nodes an answer is about, for the canvas to light up."""

    ids: list[str] = []
    for doc in [*(result.get("documents") or []), *(result.get("recent") or [])]:
        ids.append(str(doc.get("id") or ""))
    if isinstance(result.get("document"), dict):
        ids.append(str(result["document"].get("id") or ""))
        for key in ("links_to", "linked_from"):
            for item in (result.get(key) or {}).get("items") or []:
                ids.append(str(item["document"].get("id") or ""))
    links = result.get("links")
    for link in links if isinstance(links, list) else []:
        ids.extend([str(link["from"].get("id") or ""), str(link["to"].get("id") or "")])
    return list(dict.fromkeys(i for i in ids if i))


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
        "source": _render_source,
        "document": _render_document,
        "links": _render_links,
    }[asked.action](asked, result, max_items)
    notes = "".join(f"\n\n_Note: {note}._" for note in asked.notes)
    paging = _render_page(found.get("page"))
    return f"{body}{paging}{notes}\n\n{_footer(asked, found['tool'])}"


def _render_page(page: dict[str, Any] | None) -> str:
    if not page or int(page.get("pages") or 1) <= 1:
        return ""
    moves = []
    if page.get("next_question"):
        moves.append(f"next: `{page['next_question']}` (or `{KEYWORD} more`)")
    if page.get("previous_question"):
        moves.append(f"previous: `{page['previous_question']}`")
    return f"\n\nPage {page['number']} of {page['pages']}" + (
        f" — {'; '.join(moves)}." if moves else "."
    )


def _footer(asked: InventoryQuestion, tool: str) -> str:
    how = (
        f"asked with {asked.keyword or KEYWORD}"
        if asked.trigger == "keyword"
        else f"read as a question about the knowledge base itself; start with {KEYWORD} "
        "to ask one explicitly"
    )
    return f"_Answered directly from the index (`{tool}`), not by searching — {how}._"


def _render_help(asked: InventoryQuestion, result: dict[str, Any], _max: int) -> str:
    from pheasant.assistant import keywords

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
            "source <name>",
            "list documents",
            "list pdfs in <source> page 2",
            "document <path>",
            "what links to <path>",
            "links between <source> and <source>",
            "how many documents",
            "file types",
            "recent documents",
            "sync status",
        )
    )
    groups = []
    for group, members in keywords.help_rows():
        rows = "\n".join(
            f"| `@{k.name}` | {k.summary} | `{k.example}` |"
            for k in members
            if k.name != keywords.INVENTORY_KEYWORD
        )
        groups.append(f"**{group}**\n\n| Keyword | Answers with | Example |\n|---|---|---|\n{rows}")
    return (
        f"{lead}**{result.get('name') or result.get('knowledge_base')}** holds "
        f"{_n(totals.get('documents', 0), 'document')} across "
        f"{_n(totals.get('sources', 0), 'source')}. Questions about the knowledge base itself "
        f"are answered from the index directly, without searching. Start a message with "
        f"**{KEYWORD}** to ask one:\n\n{examples}\n\n"
        "A keyword as the **first word** of a message also says what kind of answer you "
        "want, and several can lead one (`@detailed @table …`):\n\n" + "\n\n".join(groups)
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
        if total:
            return f"**{_n(total, what)}**{scope}; this page is past the last of them."
        return f"No {what}s{scope} are indexed."
    start = int((result.get("pagination") or {}).get("offset") or 0) + 1
    shown = _span(start, len(docs), total)
    order = ", most recently indexed first" if asked.action == "recent" else ""
    rows = [
        f"| {_cell(d['source'])} | `{_cell(d['path'])}` | {_size(d.get('size_bytes'))} | "
        f"{_when(d.get('last_indexed_at'))} |"
        for d in docs
    ]
    table = "| Source | Path | Size | Indexed |\n|---|---|---|---|\n" + "\n".join(rows)
    return f"**{_n(total, what)}**{scope} — showing {shown}{order}.\n\n{table}"


def _render_source(asked: InventoryQuestion, result: dict[str, Any], max_items: int) -> str:
    source = result.get("source") or {}
    totals = result.get("totals") or {}
    status = source.get("status") or ("enabled" if source.get("enabled") else "disabled")
    lines = [
        f"**{_cell(source.get('name'))}** — {_cell(source.get('type') or 'source')}, {status}; "
        f"{_n(totals.get('documents', 0), 'document')} ({_size(totals.get('size_bytes'))}), "
        f"last indexed {_when(source.get('last_indexed_at'))}."
    ]
    if source.get("location"):
        lines.append(f"\nReads from `{_cell(source['location'])}`.")
    if source.get("description"):
        lines.append(f"\n{source['description']}")
    if result.get("by_extension"):
        kinds = ", ".join(
            f"{_cell(row['extension'])} {row['documents']}" for row in result["by_extension"]
        )
        lines.append(f"\nBy file type: {kinds}.")
    if result.get("by_directory"):
        rows = "\n".join(
            f"| `{_cell(row['directory'])}` | {row['documents']} |"
            for row in result["by_directory"]
        )
        lines.append(f"\n| Folder | Documents |\n|---|---|\n{rows}")
    if result.get("recent"):
        newest = ", ".join(f"`{_cell(d['path'])}`" for d in result["recent"])
        lines.append(f"\nNewest: {newest}.")
    links = result.get("links")
    name = source.get("name")
    if links is None:
        lines.append("\nLinks are unavailable: this process serves no graph.")
    elif links.get("outgoing") or links.get("incoming"):
        for title, key, column in (
            ("Its documents link to", "outgoing", "To source"),
            ("Linked from", "incoming", "From source"),
        ):
            if links.get(key):
                rows = "\n".join(
                    f"| {_cell(row['source'])} | {_cell(row['edge_type'])} | {row['links']} |"
                    for row in links[key][:max_items]
                )
                lines.append(f"\n{title}:\n\n| {column} | Edge | Links |\n|---|---|---|\n{rows}")
        lines.append(f"\nList them one by one with `{KEYWORD} links in {name}`.")
    else:
        lines.append("\nNo document here links to another document, or is linked from one.")
    return "\n".join(lines)


def _render_document(asked: InventoryQuestion, result: dict[str, Any], max_items: int) -> str:
    if result.get("error"):
        return (
            f"No document matches “{_cell(result.get('query'))}”. A path, `<source>/<path>` or "
            f"part of a file name works; to look for it, try "
            f"`{KEYWORD} documents matching {_cell(result.get('query'))}`."
        )
    if result.get("document") is None:
        rows = "\n".join(
            f"| {_cell(d['source'])} | `{_cell(d['path'])}` |"
            for d in result.get("candidates") or []
        )
        more = int(result.get("total_candidates") or 0) - len(result.get("candidates") or [])
        tail = f"\n\n…and {more} more." if more > 0 else ""
        return (
            f"“{_cell(result.get('query'))}” matches "
            f"**{_n(result.get('total_candidates'), 'document')}**. Say which, for example "
            f"`{KEYWORD} document <source>/<path>`:\n\n| Source | Path |\n|---|---|\n"
            f"{rows}{tail}"
        )
    doc = result["document"]
    lines = [
        f"**`{_cell(doc['source'])}/{_cell(doc['path'])}`** — {_cell(doc.get('type'))}, "
        f"{_size(doc.get('size_bytes'))}, {_n(result.get('chunks'), 'chunk')}, "
        f"indexed {_when(doc.get('last_indexed_at'))}."
    ]
    linked = {
        "out": ("links_to", "Links to"),
        "in": ("linked_from", "Linked from"),
    }
    order = ["in", "out"] if asked.direction == "in" else ["out", "in"]
    if asked.direction in linked:
        key, _title = linked[asked.direction]
        block = result.get(key)
        if block is not None:
            verb = "links to" if asked.direction == "out" else "is linked from"
            lines.append(f"\nIt {verb} **{_n(block.get('total'), 'document')}**.")
    for direction in order:
        key, title = linked[direction]
        block = result.get(key)
        if block is None:
            if direction == order[0]:
                lines.append("\nLinks are unavailable: this process serves no graph.")
            continue
        if not block.get("items"):
            if asked.direction in (None, direction):
                lines.append(f"\n{title}: nothing.")
            continue
        rows = "\n".join(
            f"| {_cell(item['document']['source'])} | `{_cell(item['document']['path'])}` | "
            f"{_cell(', '.join(item['edge_types']))} |"
            for item in block["items"][:max_items]
        )
        more = int(block.get("total") or 0) - min(len(block["items"]), max_items)
        way = "to" if direction == "in" else "from"
        tail = (
            f"\n\n…and {more} more; page through all of them with "
            f"`{KEYWORD} links {way} {_cell(doc['source'])}/{_cell(doc['path'])}`."
            if more > 0
            else ""
        )
        lines.append(
            f"\n{title} ({block.get('total')}):\n\n| Source | Path | Edge |\n"
            f"|---|---|---|\n{rows}{tail}"
        )
    outline = result.get("outline") or {}
    if outline.get("headings") and asked.direction is None:
        shown = outline["headings"][: min(max_items, 30)]
        items = "\n".join(f"- {_cell(h)}" for h in shown)
        more = int(outline.get("total") or 0) - len(shown)
        lines.append(
            f"\nOutline:\n\n{items}" + (f"\n- …and {more} more sections" if more > 0 else "")
        )
    symbols = result.get("symbols") or {}
    if symbols.get("items") and asked.direction is None:
        shown = symbols["items"][: min(max_items, 30)]
        rows = "\n".join(
            f"| {_cell(sym.get('kind') or '')} | `{_cell(sym.get('name') or '')}` | "
            f"{sym.get('line') or '—'} |"
            for sym in shown
        )
        more = int(symbols.get("total") or 0) - len(shown)
        lines.append(
            f"\nDefines {_n(symbols.get('total'), 'symbol')}:\n\n| Kind | Name | Line |\n"
            f"|---|---|---|\n{rows}" + (f"\n\n…and {more} more." if more > 0 else "")
        )
    unresolved = result.get("unresolved_references") or {}
    if unresolved.get("items") and asked.direction in (None, "out"):
        refs = ", ".join(f"`{_cell(r['reference'])}`" for r in unresolved["items"][:20])
        more = int(unresolved.get("total") or 0) - min(len(unresolved["items"]), 20)
        lines.append(
            f"\nRefers to {_n(unresolved.get('total'), 'thing')} this knowledge base does not "
            f"hold: {refs}" + (f", and {more} more." if more > 0 else ".")
        )
    return "\n".join(lines)


def _render_links(asked: InventoryQuestion, result: dict[str, Any], max_items: int) -> str:
    if result.get("error"):
        # The document the links were asked for is absent or ambiguous.
        return _render_document(asked, result, max_items)
    scope = ""
    if asked.path:
        way = {"in": "to", "out": "from"}.get(str(asked.direction), "to and from")
        scope = f" {way} `{asked.path}`"
    elif asked.source_name and asked.other_source:
        scope = f" between `{asked.source_name}` and `{asked.other_source}`"
    elif asked.source_name:
        scope = f" in and out of `{asked.source_name}`"
    if asked.cross_source_only:
        scope = f"{scope or ' between documents'} that cross sources"
    if asked.edge_types:
        scope += f" ({', '.join(asked.edge_types)})"
    if not result.get("available", True):
        return "Links are unavailable: this process serves no graph."
    total = int(result.get("total") or 0)
    if not total:
        return f"No links{scope or ' between documents'}."
    lines = [f"**{_n(total, 'link')}**{scope or ' between documents'}."]
    summary = result.get("summary") or []
    if len(summary) > 1 or not result.get("links"):
        rows = "\n".join(
            f"| {_cell(row['from_source'])} | {_cell(row['to_source'])} | "
            f"{_cell(row['edge_type'])} | {row['links']} |"
            for row in summary[:20]
        )
        lines.append(
            f"\n| From source | To source | Edge | Links |\n|---|---|---|---|\n{rows}"
            + (f"\n\n…and {len(summary) - 20} more pairs." if len(summary) > 20 else "")
        )
    listed = result.get("links") or []
    if listed:
        start = int((result.get("pagination") or {}).get("offset") or 0) + 1
        shown = _span(start, len(listed), total)
        rows = "\n".join(
            f"| `{_cell(link['from']['source'])}/{_cell(link['from']['path'])}` | "
            f"{_cell(', '.join(link['edge_types']))} | "
            f"`{_cell(link['to']['source'])}/{_cell(link['to']['path'])}` |"
            for link in listed
        )
        lines.append(f"\nShowing {shown}:\n\n| From | Edge | To |\n|---|---|---|\n{rows}")
    return "\n".join(lines)


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


def _span(start: int, count: int, total: int) -> str:
    if count >= total:
        return "all"
    return str(start) if count == 1 else f"{start}–{start + count - 1}"


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
