"""The fast search provider: candidate locations, never conclusions.

The provider here is the region's own hybrid retrieval. Its contract is
candidate discovery: ranked references with a revision hint and the index
version that produced them. A candidate is a place to look; the runtime then
opens the registered source at its *current* revision and reads what is there,
so a stale index entry (old text, a deleted file) is a discrepancy recorded in
the ledger, never a citation.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from pheasant_harness.common import HarnessError


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": offset}).encode()).decode()


def _offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return int(json.loads(base64.urlsafe_b64decode(cursor.encode()))["offset"])
    except Exception as exc:  # noqa: BLE001
        raise HarnessError("discovery cursor is not one this provider issued") from exc


def _indexed_digest(state: Any, source: str | None, relative: str) -> str | None:
    if not source:
        return None
    rows = state.rows(
        "SELECT a.sha256 FROM artifacts a JOIN sources s ON s.id=a.source_id "
        "WHERE (s.name=? OR s.id=?) AND a.relative_path=?",
        (source, source, relative),
    )
    return "sha256:" + rows[0]["sha256"] if rows and rows[0]["sha256"] else None


def discover(
    harness: Any,
    question: str,
    *,
    filters: dict | None = None,
    cursor: str | None = None,
    limit: int = 10,
    registrations: list | None = None,
    enabled: bool = True,
) -> dict:
    if not enabled:
        return {
            "outcome": "partial",
            "status": "disabled",
            "candidates": [],
            "provider": {"name": "pheasant-hybrid", "index_version": "unknown"},
            "next_cursor": None,
            "note": "discovery is off; inspect registered sources directly",
        }
    from pheasant.services import retrieval

    offset = _offset(cursor)
    filters = dict(filters or {})
    try:
        response = retrieval.search(
            harness.services,
            retrieval.SearchRequest(
                query=question,
                max_results=offset + limit,
                source_name=filters.get("source_name"),
                path_prefix=filters.get("path_prefix"),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - the provider is optional; say it failed
        return {
            "outcome": "transient_error",
            "status": "unavailable",
            "candidates": [],
            "provider": {"name": "pheasant-hybrid", "index_version": "unknown"},
            "error": type(exc).__name__,
            "next_cursor": None,
        }
    results = response.get("results", [])
    page = results[offset : offset + limit]
    candidates = []
    for rank, hit in enumerate(page, start=offset + 1):
        provenance = hit.get("provenance") or {}
        source = provenance.get("source_name") or provenance.get("source_id")
        relative = hit.get("relative_path") or provenance.get("relative_path") or ""
        chunk = (hit.get("chunks") or [{}])[0]
        candidate = {
            "rank": rank,
            "uri": f"pheasant://{source}/{relative}" if source else None,
            "source": source,
            "path": relative,
            "absolute_path": provenance.get("path") or hit.get("path"),
            "locator": {"start_line": chunk.get("start_line"), "end_line": chunk.get("end_line")},
            # What the index read; the runtime compares it with what is there now.
            "indexed_digest": _indexed_digest(harness.state, source, relative),
            "snippet": (chunk.get("text_preview") or hit.get("summary") or "")[:400],
            "retrieved_by": hit.get("retrieved_by"),
            "registrations": _matching(registrations or [], source, relative, hit),
        }
        candidates.append(candidate)
    generation = response.get("graph_generation") or (response.get("lineage") or {}).get(
        "graph_generation"
    )
    return {
        "outcome": "success",
        "status": "ok",
        "candidates": candidates,
        "provider": {
            "name": "pheasant-hybrid",
            "index_version": generation or "unknown",
            "query_id": response.get("query_id"),
        },
        "next_cursor": _cursor(offset + limit) if len(results) >= offset + limit else None,
    }


def _matching(registrations: list, source: str | None, relative: str, hit: dict) -> list[dict]:
    """Registrations a hit lives in, with the path inside each."""

    absolute = (hit.get("provenance") or {}).get("path") or hit.get("path") or ""
    found = []
    for reg in registrations:
        if reg.resolver == "pheasant" and reg.scope.get("source") == source:
            found.append({"registration_id": reg.id, "path": relative})
        elif reg.resolver in ("folder", "git") and absolute:
            root = Path(reg.scope["root"]).resolve()
            try:
                inside = Path(absolute).resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            found.append({"registration_id": reg.id, "path": inside})
    return found
