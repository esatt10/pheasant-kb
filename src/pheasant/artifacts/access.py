"""Saved-file ACL metadata, derived without parsing generated content."""

from __future__ import annotations

import json
import re
from typing import Any


def saved_acl(state: Any, kb_id: str, path: str) -> dict | None:
    match = re.search(r"--([0-9a-f]{24})(?:/|$)", path)
    if not match:
        return None
    rows = state.rows(
        "SELECT owner,payload_json FROM saved_artifacts WHERE id=? AND kb_id=?",
        (match.group(1), kb_id),
    )
    if not rows:
        return None
    metadata = json.loads(rows[0]["payload_json"])
    return {"scope": metadata.get("scope", "org"), "written_by": rows[0]["owner"]}
