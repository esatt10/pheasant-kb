"""Private progress and evidence for a GTM source.

The ordinary source checkpoint carries counts and a revision only. Keeping
publisher excerpts here prevents generic sync and source APIs from exposing
them to tool-enabled clients.
"""

from __future__ import annotations

import json
from typing import Any


def load(state: Any, source_id: str) -> dict:
    rows = state.rows(
        "SELECT payload_json FROM gtm_collection_state WHERE source_id=?", (source_id,)
    )
    return json.loads(rows[0]["payload_json"]) if rows else {}


def save(state: Any, source_id: str, payload: dict) -> None:
    state.execute(
        "INSERT INTO gtm_collection_state(source_id,payload_json) VALUES(?,?) "
        "ON CONFLICT(source_id) DO UPDATE SET payload_json=excluded.payload_json",
        (source_id, json.dumps(payload, separators=(",", ":"))),
    )
