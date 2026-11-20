"""The Admin page's measures as files: the usage report, and the traces behind it.

One implementation behind ``GET /usage/export`` and MCP ``export_usage``, and
behind MCP ``export_usage_traces`` beside HTTP's JSON-lines downloads (rule 13).
Nothing here measures anything: the report is ``services.usage.describe`` and
the month's spend ``services.region_controls.describe``, written out, so a file
and the page can never disagree.

* ``json`` is the report and the controls as one document, exactly as the API
  returns them.
* ``csv`` is a ZIP of one table per section (``totals.csv``, ``by_day.csv``,
  ``by_model.csv``, ``by_organization.csv``, ``by_channel.csv``,
  ``people.csv``, ``latency.csv``, ``spend.csv``) plus ``report.json``, for a
  spreadsheet.

Admin only (``security.manage``), and the amounts follow the page's rule:
cost and spend are an operator admin's, ``null`` in an Admin a customer
granted's export.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import zipfile
from datetime import UTC, datetime
from typing import Any

from pheasant.services import ServiceContext, region_controls, usage, usage_traces
from pheasant.services.errors import InvalidRequest

FORMATS = ("csv", "json")
#: Trace rows one MCP call carries; page with ``offset`` for more.
MAX_TRACE_ROWS = 2_000
#: The usage tables a CSV export writes, in order.
TABLES = ("by_day", "by_model", "by_organization", "by_channel", "people")


def _flat(row: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """One CSV row: nested mappings become ``parent.child`` columns."""

    out: dict[str, Any] = {}
    for key, value in row.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flat(value, f"{name}."))
        elif isinstance(value, list):
            out[name] = json.dumps(value, ensure_ascii=False)
        else:
            out[name] = value
    return out


def _csv(rows: list[dict[str, Any]]) -> str:
    flat = [_flat(row) for row in rows]
    columns: list[str] = []
    for row in flat:
        columns.extend(key for key in row if key not in columns)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns or ["empty"], lineterminator="\n")
    writer.writeheader()
    for row in flat:
        writer.writerow(row)
    return buffer.getvalue()


def report(context: ServiceContext, *, days: int = 30) -> dict[str, Any]:
    """The usage report and the month's controls, as one document."""

    measured = usage.describe(context, days=days)
    return {
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "usage": measured,
        "controls": region_controls.describe(context),
    }


def export_usage(context: ServiceContext, *, days: int = 30, format: str = "csv") -> dict[str, Any]:  # noqa: A002
    """The Admin page's measures as one file.

    Returns ``{filename, media_type, encoding, content, bytes}``: ``content``
    is base64 for the ZIP and UTF-8 text for JSON, as ``export_source`` returns.
    """

    fmt = str(format or "csv").strip().lower()
    if fmt not in FORMATS:
        raise InvalidRequest(f"format must be one of {', '.join(FORMATS)}; got {format!r}")
    document = report(context, days=days)
    measured = document["usage"]
    window = measured["window"]
    stamp = f"{measured['knowledge_base']}-usage-{window['from']}-to-{window['to']}"
    if fmt == "json":
        text = json.dumps(document, indent=2, ensure_ascii=False, default=str) + "\n"
        return {
            "filename": f"{stamp}.json",
            "media_type": "application/json",
            "encoding": "utf-8",
            "content": text,
            "bytes": len(text.encode("utf-8")),
        }
    latency = measured["latency"]
    spend = document["controls"]["spend"]
    access = document["controls"]["access"]
    files = {
        "totals.csv": _csv(
            [
                {
                    "from": measured["window"]["from"],
                    "to": measured["window"]["to"],
                    "currency": measured["currency"],
                    **measured["totals"],
                }
            ]
        ),
        **{f"{name}.csv": _csv(measured[name]) for name in TABLES},
        "latency.csv": _csv(
            [{"operation": name, **latency[name]} for name in ("answer", "search")]
        ),
        "spend.csv": _csv([{**spend, "access_suspended": access["suspended"]}]),
        "report.json": json.dumps(document, indent=2, ensure_ascii=False, default=str) + "\n",
    }
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, text in files.items():
            bundle.writestr(f"{stamp}/{name}", text)
    data = archive.getvalue()
    return {
        "filename": f"{stamp}.zip",
        "media_type": "application/zip",
        "encoding": "base64",
        "content": base64.b64encode(data).decode("ascii"),
        "bytes": len(data),
        "files": list(files),
    }


def export_traces(
    context: ServiceContext,
    *,
    days: int = 7,
    principal: str | None = None,
    organization: str | None = None,
    model: str | None = None,
    session_key: str | None = None,
    calls: str = "questions",
    match: str | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """A page of the traces' JSON lines, for a caller that cannot take a download.

    The same rows, in the same order, as ``GET /usage/traces/export`` and
    ``/usage/sessions/{session}/export`` (``usage_traces.export_lines``). While
    ``has_more`` is true, ask again with ``offset=next_offset``. ``limit`` is
    at most ``MAX_TRACE_ROWS``, which it defaults to.
    """

    try:
        offset = max(0, int(offset))
        limit = MAX_TRACE_ROWS if limit is None else int(limit)
    except (TypeError, ValueError) as exc:
        raise InvalidRequest("offset and limit are whole numbers") from exc
    if not 1 <= limit <= MAX_TRACE_ROWS:
        raise InvalidRequest(f"limit must be between 1 and {MAX_TRACE_ROWS}")
    lines = usage_traces.export_lines(
        context,
        days=days,
        principal=principal,
        organization=organization,
        model=model,
        session_key=session_key,
        calls=calls,
        match=match,
    )
    page: list[str] = []
    more = False
    for index, line in enumerate(lines):
        if index < offset:
            continue
        if len(page) == limit:
            more = True
            break
        page.append(line)
    stamp = "".join(c if c.isalnum() or c in "-_" else "-" for c in (session_key or "traces"))[:60]
    content = "".join(page)
    return {
        "filename": f"{stamp}.jsonl",
        "media_type": "application/x-ndjson",
        "encoding": "utf-8",
        "content": content,
        "rows": len(page),
        "offset": offset,
        "has_more": more,
        "next_offset": offset + len(page) if more else None,
    }
