"""The files an artifact's folder holds: its README (with its evidence) and the rest.

Pure functions of the record, so the publisher, the deleter and the
re-filer agree on what a folder contains.
"""

from __future__ import annotations

import json
from typing import Any

from pheasant.services import artifact_render as render
from pheasant.services.errors import InvalidRequest

#: Each artifact's index page: what it is, and the documents it cites.
INDEX_FILE = "README.md"
#: The rendered report, for a report that draws a diagram.
REPORT_FILE = "report.html"


def index_page(record: dict[str, Any]) -> str:
    """The artifact's ``README.md``: what it is, its content's gist, and its sources.

    Each source is a Markdown link to the cited document's path, which is
    what makes it an edge: the Markdown pass reads the link as a reference
    and cross-source resolution resolves the path to that document.
    """

    payload = record.get("payload") or {}
    kind = render.KIND_LABELS.get(str(record.get("kind")), "Artifact")
    lines = [f"# {record.get('title')}", ""]
    if payload.get("question"):
        lines += [f"> {payload['question']}", ""]
    lines += [
        f"- Artifact: `{record.get('id')}`",
        f"- Kind: {kind}",
        f"- Saved by: {record.get('owner') or 'shared'}",
        f"- Saved: {str(record.get('created_at') or '')[:16].replace('T', ' ')} UTC",
    ]
    files = [str(f.get("name")) for f in payload.get("files") or []]
    if files:
        lines.append(f"- Files: {', '.join(f'`{name}`' for name in files)}")
    lines.append("")
    body = (
        payload.get("answer")
        if record.get("kind") in ("report", "thread")
        else payload.get("summary")
    )
    if body:
        lines += [str(body).strip(), ""]
    if payload.get("not_covered"):
        lines += ["## Not covered by the sources", "", str(payload["not_covered"]).strip(), ""]
    cited = [c for c in payload.get("citations") or [] if c.get("path")]
    if cited:
        lines += ["## Sources", ""]
        for citation in cited:
            label = f"{citation.get('source')} · {citation.get('path')}"
            if citation.get("section"):
                label += f" — {citation['section']}"
            target = str(citation["path"]).replace(" ", "%20").replace(")", "%29")
            lines.append(f"{citation.get('index')}. [{label.replace(']', '')}]({target})")
        lines.append("")
    return "\n".join(lines)


def source_files(record: dict[str, Any]) -> list[tuple[str, str]]:
    """``(name, text)`` for every file of the artifact's folder, the index page first."""

    payload = record.get("payload") or {}
    files = [(INDEX_FILE, index_page(record))]
    files += [
        (render.safe_name(str(f.get("name") or ""), "file.txt"), str(f.get("content") or ""))
        for f in payload.get("files") or []
    ]
    # Any kept artifact with a drawn diagram gets the page that draws it.
    if render.has_visual(payload):
        files.append((REPORT_FILE, render.overview(record)))
    return files


#: The largest diagram spec a caller may hand over to be kept.
MAX_VISUAL_BYTES = 300_000
_VISUAL_KEYS = ("type", "status", "kind", "diagram", "grounding", "fallback_from", "title")


def kept_visual(value: Any) -> dict[str, Any] | None:
    """A drawn diagram a caller sends to be kept -- one ``create_visual`` (or
    "Redraw as") returned -- or a refusal; ``None`` when nothing was sent.

    Kept as data and only ever drawn by the knowledge view's own renderers
    (never as markup), so what is checked is the shape and the size.
    """

    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or value.get("status") != "ok"
        or not isinstance(value.get("diagram"), dict)
    ):
        raise InvalidRequest(
            'visual must be a drawn diagram as create_visual returns it: {"status": "ok", '
            '"diagram": {...}}'
        )
    kept = {key: value[key] for key in _VISUAL_KEYS if key in value}
    if len(json.dumps(kept, default=str)) > MAX_VISUAL_BYTES:
        raise InvalidRequest(f"a kept diagram holds at most {MAX_VISUAL_BYTES // 1000} KB")
    return kept
