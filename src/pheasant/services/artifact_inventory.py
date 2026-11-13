"""One inventory for saved files, typed knowledge and durable run outputs."""

from __future__ import annotations

import json
from typing import Any

from pheasant.services import ServiceContext
from pheasant.services.artifact_files import INDEX_FILE
from pheasant.services.artifact_scope import ARTIFACTS_SOURCE


def mark_indexed(context: ServiceContext, records: list[dict[str, Any]]) -> None:
    """``index_status`` for each record: whether the source holds its index page yet."""

    paths = {
        f"{r['payload']['folder']}/{INDEX_FILE}": r
        for r in records
        if (r.get("payload") or {}).get("folder")
    }
    held: set[str] = set()
    names = list(paths)
    for start in range(0, len(names), 200):
        batch = names[start : start + 200]
        marks = ",".join("?" for _ in batch)
        held |= {
            str(row["relative_path"])
            for row in context.state.rows(
                "SELECT relative_path FROM artifacts "
                f"WHERE source_id=? AND relative_path IN ({marks})",
                (ARTIFACTS_SOURCE, *batch),
            )
        }
    for path, record in paths.items():
        record["index_status"] = (
            "archived" if record.get("archived_at") else "indexed" if path in held else "pending"
        )


def list_artifacts(
    context: ServiceContext,
    principal: str | None = None,
    *,
    limit: int = 50,
    owner_name: str | None = None,
    conversation_id: str | None = None,
    kind: str | None = None,
    archived: bool = False,
) -> dict[str, Any]:
    """The region's artifacts without contents: newest day first, then person, then time.

    ``owner_name``, ``conversation_id`` and ``kind`` narrow it; ``archived``
    lists what retention (or a person) archived instead of what is live.
    """

    from pheasant.services.artifacts import COLUMNS, _record, owner, summary_of

    limit = max(1, min(int(limit or 50), 200))
    clauses = ["kb_id=?", "archived_at IS NOT NULL" if archived else "archived_at IS NULL"]
    params: list[Any] = [context.config.knowledge_base_id]
    for column, value in (("owner", owner_name), ("session_id", conversation_id), ("kind", kind)):
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    rows = context.state.rows(
        f"SELECT {COLUMNS} FROM saved_artifacts WHERE {' AND '.join(clauses)} "
        "ORDER BY substr(created_at, 1, 10) DESC, COALESCE(owner, '') ASC, "
        "created_at DESC, id DESC LIMIT ?",
        (*params, limit),
    )
    records = [_record(row) for row in rows]
    records = [
        r
        for r in records
        if r["payload"].get("scope", "org") == "org" or r.get("owner") == owner(principal)
    ]
    from pheasant.services import artifact_lifecycle

    artifact_lifecycle.file_legacy(context, records)
    mark_indexed(context, records)
    from pheasant.services import artifact_outputs, artifact_records

    if not owner_name and not conversation_id and kind in (None, "report"):
        records += artifact_outputs.listing(context, archived=archived, limit=limit)

    if not archived and kind in (None, "record"):
        typed = artifact_records.list_records(context, principal)["records"]
        for item in typed:
            if owner_name and item.get("written_by") != owner_name:
                continue
            from types import SimpleNamespace

            records.append(
                artifact_records.get_record(
                    context,
                    item["record_id"],
                    principal,
                    record=SimpleNamespace(**item, as_dict=lambda data=item: data),
                )
            )
    # Preserve the date/person/time contract, including across typed outputs.
    records = sorted(records, key=lambda r: str(r["created_at"]), reverse=True)
    records = sorted(records, key=lambda r: str(r.get("owner") or ""))
    records = sorted(records, key=lambda r: str(r["created_at"])[:10], reverse=True)[:limit]
    return {
        "artifacts": [summary_of(record) for record in records],
        "limit": limit,
        "source": ARTIFACTS_SOURCE,
        "order": ["day desc", "owner", "time desc"],
    }


def record(row: Any) -> dict[str, Any]:
    row = dict(row)
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "created_at": row["created_at"],
        "owner": row.get("owner"),
        "payload": json.loads(row.get("payload_json") or "{}"),
        "conversation_id": row.get("session_id"),
        "channel": row.get("channel"),
        "archived_at": row.get("archived_at"),
    }


def summary_of(record: dict[str, Any]) -> dict[str, Any]:
    """What a listing shows: no file contents."""

    from pheasant.services import artifact_render as render
    from pheasant.services.artifact_trace import trace_summary as _trace_summary

    payload = record.get("payload") or {}
    folder = payload.get("folder")
    return {
        "id": record["id"],
        "kind": record["kind"],
        "title": record["title"],
        "created_at": record["created_at"],
        "owner": record.get("owner"),
        "source": record.get("source") or (ARTIFACTS_SOURCE if folder else None),
        "folder": record.get("folder") or folder,
        "semantic_type": record.get("semantic_type"),
        "bulk_supported": not record.get("semantic_type"),
        "files": [
            {
                "name": f.get("name"),
                "language": f.get("language"),
                "previewable": render.previewable(str(f.get("name") or ""), f.get("language")),
            }
            for f in payload.get("files") or []
        ],
        "has_visual": render.has_visual(payload),
        "conversation_id": record.get("conversation_id"),
        "channel": record.get("channel"),
        "archived_at": record.get("archived_at"),
        # `indexed` once the artifacts source has it, `pending` until the beat
        # reaches it; a record read without the check says nothing.
        "index_status": record.get("index_status"),
        "trace": _trace_summary(payload),
    }
