"""Capture legacy/formed records as immutable artifact versions on projection."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pheasant.artifacts.projection import effective_valid_until, supersession_index
from pheasant.persistence.artifact_publications import keep_version, publish


def capture_records(state: Any, kb_id: str, source: str, records: list[Any]) -> None:
    endings = supersession_index(records)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with state.conn:
        for record in records:
            content = {k: v for k, v in record.as_dict().items() if k != "path"}
            keep_version(state, kb_id, record.record_id, content)
            if record.scope == "session":
                continue
            existing = state.conn.execute(
                "SELECT status FROM artifact_publications WHERE kb_id=? AND artifact_id=?",
                (kb_id, record.record_id),
            ).fetchall()
            if not existing:
                publish(
                    state,
                    kb_id,
                    record.record_id,
                    content,
                    receipt={"source": source, "kind": record.kind},
                    transaction=False,
                )
            end = effective_valid_until(record, endings)
            if end and end <= now:
                state.conn.execute(
                    "UPDATE artifact_publications SET status='superseded',deprecated_at=? "
                    "WHERE kb_id=? AND artifact_id=? AND status='published'",
                    (end, kb_id, record.record_id),
                )
