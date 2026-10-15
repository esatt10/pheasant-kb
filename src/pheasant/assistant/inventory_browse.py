"""``@taxonomy``: one folder of a source, answered from the index.

``assistant.inventory`` reads ``@taxonomy notes guides`` (``@pheasant
taxonomy …``) as the action ``taxonomy``; this module looks the folder up with
``services.source_browser.browse`` — the operation behind the MCP
``browse_source`` tool and ``GET /documents/tree`` — and writes it as Markdown.
Kept beside ``inventory_answer`` rather than inside it for the module budget,
and because a folder listing is its own shape: folders first, each with what it
holds, then files.

A miss is said rather than searched. ``@taxonomy`` with no source lists the
sources, so the reader can pick one; a folder that is not there falls back to
the source's root with a note, since the reader asked for the index explicitly
and the root is the nearest thing to what they named.

The web UI reads the answer's ``inventory.result`` (``source`` and ``path``)
and opens its source browser at that folder, beside the chat. Every other
client gets the listing.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pheasant.assistant.inventory import KEYWORD, InventoryQuestion


def lookup(
    asked: InventoryQuestion,
    context: Any,
    *,
    principal: str | None,
    principal_groups: list[str] | None,
    max_items: int,
) -> tuple[InventoryQuestion, dict[str, Any]]:
    """``(asked, found)``: ``asked`` gains a note when the folder was not found."""

    from pheasant.services import source_browser
    from pheasant.services.errors import ServiceError

    page = max(1, int(asked.page or 1))
    request = source_browser.BrowseRequest(
        source_name=asked.source_name,
        path=(asked.path or "") if asked.source_name else "",
        limit=max_items,
        offset=(page - 1) * max_items,
        principal=principal,
        principal_groups=principal_groups,
    )
    notes: list[str] = []
    if asked.path and not asked.source_name:
        notes.append(
            f"“{asked.path}” is not a source; name the source the folder is in "
            f"(`@taxonomy <source> {asked.path}`)"
        )
    try:
        result = source_browser.browse(context, request)
    except ServiceError as refused:
        if not asked.path:
            raise
        notes.append(f"{refused}; showing the root of `{asked.source_name}` instead")
        result = source_browser.browse(context, replace(request, path="", offset=0))
    if notes:
        asked = replace(asked, notes=(*asked.notes, *notes))
    found: dict[str, Any] = {"tool": "browse_source", "result": result}
    files = int((result.get("totals") or {}).get("files") or 0)
    if result.get("source") and files > max_items:
        pages = -(-files // max_items)
        where = " ".join(p for p in (result["source"], result.get("path") or "") if p)
        command = f"{KEYWORD} taxonomy {where}"
        found["page"] = {
            "number": page,
            "size": max_items,
            "total": files,
            "pages": pages,
            "next_question": f"{command} page {page + 1}" if page < pages else None,
            "previous_question": f"{command} page {page - 1}" if page > 1 else None,
            "endpoint": "/documents/tree",
            "params": {"source_name": result["source"], "path": result.get("path") or ""},
        }
    return asked, found


def render(asked: InventoryQuestion, result: dict[str, Any], max_items: int) -> str:
    from pheasant.assistant.inventory_answer import _cell, _n, _size, _when

    folders = result.get("directories") or []
    files = result.get("files") or []
    totals = result.get("totals") or {}
    source = result.get("source")
    if not source:
        if not folders:
            return "No sources are registered in this knowledge base yet."
        rows = [
            f"| 📁 {_cell(f['name'])} | {_cell(f.get('source_type') or '')} | "
            f"{f.get('documents', 0)} |"
            for f in folders[:max_items]
        ]
        return (
            f"**{_n(len(folders), 'source')}**, holding "
            f"{_n(totals.get('documents', 0), 'document')}. Open one with "
            f"`@taxonomy <source>`.\n\n| Source | Type | Documents |\n|---|---|---|\n"
            + "\n".join(rows)
        )
    where = " / ".join(crumb["name"] for crumb in result.get("breadcrumbs") or []) or source
    head = (
        f"**{where}** — {_n(totals.get('directories', 0), 'folder')} and "
        f"{_n(totals.get('files', 0), 'file')} here, "
        f"{_n(totals.get('documents', 0), 'document')} in all."
    )
    if not folders and not files:
        return f"{head}\n\nThis folder holds nothing indexed."
    rows = [
        f"| 📁 `{_cell(f['name'])}/` | folder | {_n(f.get('documents', 0), 'document')} | — |"
        for f in folders[:max_items]
    ]
    rows += [
        f"| `{_cell(f['name'])}` | {_cell(f.get('viewer') or f.get('type') or '')} | "
        f"{_size(f.get('size_bytes'))} | {_when(f.get('last_indexed_at'))} |"
        for f in files
    ]
    parent = result.get("parent")
    if parent is None:
        up = "\n\nAll sources: `@taxonomy`"
    else:
        up = f"\n\nUp: `@taxonomy {f'{source} {parent}'.strip()}`"
    return f"{head}\n\n| Name | Kind | Size | Indexed |\n|---|---|---|---|\n" + "\n".join(rows) + up
