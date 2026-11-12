"""Durable intent to publish or remove an artifact's file projection."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Any


def enqueue(state: Any, kb: str, folder: str, operation: str, payload: dict) -> None:
    # The caller owns the transaction, together with the saved-artifact row.
    state.conn.execute(
        "INSERT INTO artifact_outbox(kb_id,folder,revision,operation,payload_json,created_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(kb_id,folder) DO UPDATE SET "
        "revision=excluded.revision,operation=excluded.operation,payload_json=excluded.payload_json,"
        "created_at=excluded.created_at,last_error=NULL",
        (
            kb,
            folder,
            secrets.token_hex(12),
            operation,
            json.dumps(payload),
            datetime.now(UTC).isoformat(),
        ),
    )


def pending(state: Any, kb: str, limit: int = 20) -> list[dict]:
    return [
        dict(r)
        for r in state.rows(
            "SELECT * FROM artifact_outbox WHERE kb_id=? ORDER BY created_at,folder LIMIT ?",
            (kb, limit),
        )
    ]
