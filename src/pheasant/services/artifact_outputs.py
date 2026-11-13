"""Evaluation and ranking outputs as artifacts, kept independently of run rows.

These are inspectable and exportable, but never indexed as evidence for their
own evaluation. Operational IDs in the receipt may refer to deleted jobs.
"""

from __future__ import annotations

import json
from typing import Any

from pheasant.artifacts import publications
from pheasant.services import ServiceContext
from pheasant.services.errors import ArtifactNotFound, InvalidRequest

PREFIXES = ("evaluation:", "tuning:", "baseline:")


def held(artifact_id: str) -> bool:
    return artifact_id.startswith(PREFIXES)


def get(context: ServiceContext, artifact_id: str) -> dict[str, Any]:
    kb = context.config.knowledge_base_id
    publication = publications.status(context.state, kb, artifact_id)
    if not publication or not held(artifact_id):
        raise ArtifactNotFound(artifact_id)
    content = publications.version(context.state, kb, artifact_id, publication["revision_id"])
    if content is None:
        raise ArtifactNotFound(artifact_id)
    prefix, _, identifier = artifact_id.partition(":")
    title = (
        {
            "evaluation": "Evaluation report",
            "tuning": "Retrieval configuration",
            "baseline": "Retrieval baseline",
        }[prefix]
        + " · "
        + identifier
    )
    answer = f"# {title}\n\nStatus: {publication['status']}.\n"
    if content.get("parameters"):
        answer += "\n| Parameter | Value |\n| --- | ---: |\n" + "\n".join(
            f"| {name} | {value} |" for name, value in sorted(content["parameters"].items())
        )
    return {
        "id": artifact_id,
        "kind": "report",
        "title": title,
        "created_at": publication["published_at"],
        "owner": None,
        "archived_at": publication["deprecated_at"],
        "index_status": "not_indexed",
        "semantic_type": prefix,
        "revision_id": publication["revision_id"],
        "publication_status": publication["status"],
        "payload": {
            "answer": answer,
            "citations": [],
            "files": [
                {
                    "name": f"{prefix}.json",
                    "language": "json",
                    "content": json.dumps(content, indent=2, default=str),
                }
            ],
            "receipt": json.loads(publication["receipt_json"]),
        },
    }


def listing(context: ServiceContext, *, archived: bool = False, limit: int = 50) -> list[dict]:
    status = "status<>'published'" if archived else "status='published'"
    rows = context.state.rows(
        "SELECT artifact_id FROM artifact_publications WHERE kb_id=? AND "
        + status
        + " AND (artifact_id LIKE 'evaluation:%' OR artifact_id LIKE 'tuning:%' "
        "OR artifact_id LIKE 'baseline:%') ORDER BY published_at DESC LIMIT ?",
        (context.config.knowledge_base_id, limit),
    )
    return [get(context, row["artifact_id"]) for row in rows]


def retire(context: ServiceContext, artifact_id: str) -> dict:
    if artifact_id.startswith("tuning:"):
        from pheasant.tuning.store import active_overlay

        active = active_overlay(context.state, context.config.knowledge_base_id)
        if active and active["bundle_id"] == artifact_id.partition(":")[2]:
            raise InvalidRequest(
                "switch the active ranking configuration before retiring its output"
            )
    result = get(context, artifact_id)
    publications.deprecate(context.state, context.config.knowledge_base_id, artifact_id)
    return {"deleted": artifact_id, "title": result["title"], "removed_files": []}
