"""Idempotent migration of saved artifact metadata and publication snapshots."""

from __future__ import annotations

import json
from typing import Any


def migrate_saved_artifacts(state: Any) -> None:
    """Add the session, channel and archive columns to a pre-existing ``saved_artifacts``.

    Both migration paths call it, for `_migrate_evaluation_progress`'s reason.
    """

    existing = state.backend.table_columns("saved_artifacts")
    if not existing:
        return
    for column, declaration in state._SAVED_ARTIFACT_COLUMNS:
        if column not in existing:
            state.conn.execute(f"ALTER TABLE saved_artifacts ADD COLUMN {column} {declaration}")
    state.conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_saved_artifacts_created "
        "ON saved_artifacts(kb_id, created_at, owner)"
    )
    from pheasant.persistence.artifact_publications import publish

    while True:
        pending = state.conn.execute(
            "SELECT a.* FROM saved_artifacts a LEFT JOIN artifact_publications p "
            "ON p.kb_id=a.kb_id AND p.artifact_id=a.id "
            "WHERE p.artifact_id IS NULL AND a.publication_state='published' "
            "ORDER BY a.id LIMIT 25"
        ).fetchall()
        if not pending:
            break
        for row in pending:
            content = {
                "id": row["id"],
                "kind": row["kind"],
                "title": row["title"],
                "created_at": row["created_at"],
                "owner": row["owner"],
                "payload": json.loads(row["payload_json"]),
            }
            publish(
                state,
                row["kb_id"],
                row["id"],
                content,
                receipt={"migration": "artifact-publications-v1"},
                transaction=False,
            )
            if row["archived_at"]:
                state.conn.execute(
                    "UPDATE artifact_publications SET status='deprecated',deprecated_at=? "
                    "WHERE kb_id=? AND artifact_id=?",
                    (row["archived_at"], row["kb_id"], row["id"]),
                )
    migrate_run_outputs(state)


def migrate_run_outputs(state: Any) -> None:
    """Seal pre-transition outputs before operational retention can prune them."""
    from pheasant.persistence.artifact_publications import publish

    for table, key, prefix, eligible in (
        (
            "evaluation_runs",
            "run_id",
            "evaluation:",
            "a.status IN ('completed','truncated','invalid') AND a.report_json IS NOT NULL "
            "AND a.report_json NOT IN ('','{}','null')",
        ),
        ("tuning_bundles", "bundle_id", "tuning:", "a.applied_at IS NOT NULL"),
    ):
        if not state.backend.table_columns(table):
            continue
        while True:
            rows = state.conn.execute(
                f"SELECT a.* FROM {table} a LEFT JOIN artifact_publications p "
                f"ON p.kb_id=a.kb_id AND p.artifact_id=? || a.{key} "
                f"WHERE p.artifact_id IS NULL AND {eligible} ORDER BY a.{key} LIMIT 25",
                (prefix,),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                row = dict(row)
                content = json.loads(row.get("report_json") or row.get("payload_json"))
                when = row.get("finished_at") or row.get("applied_at")
                if prefix == "tuning:":
                    content.update(applied_at=row["applied_at"], applied_by=row["applied_by"])
                artifact_id = prefix + row[key]
                publish(
                    state,
                    row["kb_id"],
                    artifact_id,
                    content,
                    receipt={"migration": "artifact-run-outputs-v1", "retrieval_eligible": False},
                    transaction=False,
                )
                state.conn.execute(
                    "UPDATE artifact_publications SET published_at=?,status=?,deprecated_at=? "
                    "WHERE kb_id=? AND artifact_id=?",
                    (
                        when,
                        "superseded" if row.get("superseded_at") else "published",
                        row.get("superseded_at"),
                        row["kb_id"],
                        artifact_id,
                    ),
                )
