"""Immutable output versions and publication pointers, independent of jobs."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any


def keep_version(state: Any, kb_id: str, artifact_id: str, content: dict) -> str:
    encoded = json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)
    revision = hashlib.sha256(encoded.encode()).hexdigest()
    state.conn.execute(
        "INSERT INTO artifact_versions(artifact_id,revision_id,kb_id,payload_json,created_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(kb_id,artifact_id,revision_id) DO NOTHING",
        (artifact_id, revision, kb_id, encoded, datetime.now(UTC).isoformat()),
    )
    return revision


def publish(
    state: Any,
    kb_id: str,
    artifact_id: str,
    content: dict,
    *,
    receipt: dict | None = None,
    replaces: str | None = None,
    transaction: bool = True,
) -> str:
    with state.conn if transaction else nullcontext():
        revision = keep_version(state, kb_id, artifact_id, content)
        if replaces:
            old = state.conn.execute(
                "UPDATE artifact_publications SET status='superseded', deprecated_at=? "
                "WHERE artifact_id=? AND kb_id=? AND status='published' RETURNING artifact_id",
                (datetime.now(UTC).isoformat(), replaces, kb_id),
            ).fetchall()
            if not old:
                raise ValueError("the replaced publication is unknown or is no longer current")
        state.conn.execute(
            "INSERT INTO artifact_publications(artifact_id,kb_id,revision_id,status,replaces,"
            "receipt_json,published_at) VALUES(?,?,?,'published',?,?,?) "
            "ON CONFLICT(kb_id,artifact_id) DO UPDATE SET revision_id=excluded.revision_id, "
            "status='published', receipt_json=excluded.receipt_json, deprecated_at=NULL",
            (
                artifact_id,
                kb_id,
                revision,
                replaces,
                json.dumps(receipt or {}, default=str),
                datetime.now(UTC).isoformat(),
            ),
        )
    return revision


def status(state: Any, kb_id: str, artifact_id: str) -> dict | None:
    rows = state.rows(
        "SELECT * FROM artifact_publications WHERE artifact_id=? AND kb_id=?", (artifact_id, kb_id)
    )
    return dict(rows[0]) if rows else None


def deprecate(state: Any, kb_id: str, artifact_id: str) -> bool:
    with state.conn:
        rows = state.conn.execute(
            "UPDATE artifact_publications SET status='deprecated',deprecated_at=? "
            "WHERE artifact_id=? AND kb_id=? AND status='published' RETURNING artifact_id",
            (datetime.now(UTC).isoformat(), artifact_id, kb_id),
        ).fetchall()
    return bool(rows)


def version(state: Any, kb_id: str, artifact_id: str, revision_id: str) -> dict | None:
    rows = state.rows(
        "SELECT payload_json FROM artifact_versions "
        "WHERE kb_id=? AND artifact_id=? AND revision_id=?",
        (kb_id, artifact_id, revision_id),
    )
    return json.loads(rows[0]["payload_json"]) if rows else None
