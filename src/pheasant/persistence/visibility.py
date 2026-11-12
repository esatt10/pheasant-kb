"""Immediate retrieval exclusion while the writer reclaims removed knowledge."""

from __future__ import annotations

from typing import Any

from pheasant.search.criteria import source_of
from pheasant.security.acl import acl_subject


def admit(state: Any, hits: list[dict]) -> list[dict]:
    if not hits:
        return hits
    removed = {str(r["source_id"]) for r in state.rows("SELECT source_id FROM removed_sources")}

    def subject(hit: dict) -> str:
        attrs = hit.get("attrs") or hit.get("attributes") or hit
        return acl_subject(
            str(hit.get("id") or hit.get("node_id") or ""),
            hit.get("type") or attrs.get("type"),
            hit.get("artifact_id") or attrs.get("artifact_id"),
        )

    subjects = {subject(h) for h in hits} - {""}
    hidden: set[str] = set()
    names = list(subjects)
    for start in range(0, len(names), 200):
        clause, params = state.dialect.in_clause("a.id", names[start : start + 200])
        hidden.update(
            str(r["id"])
            for r in state.rows(
                "SELECT a.id FROM artifacts a JOIN archived_documents h "
                "ON h.source_id=a.source_id AND h.relative_path=a.relative_path "
                f"WHERE {clause}",
                params,
            )
        )
    return [h for h in hits if source_of(h) not in removed and subject(h) not in hidden]
