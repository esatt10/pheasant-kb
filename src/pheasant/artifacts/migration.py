"""Adopt legacy record sources as typed artifacts without moving user files.

Paths and record/file IDs stay intact; derived chunks keep content-based IDs.
The original source definition
is retained once; reapplying a mounted legacy definition is idempotent.
The memory_* tables remain derived compatibility projections, not content stores.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pheasant.config.schema import SourceType


def adopt_source(config: Any, state: Any, source: Any) -> bool:
    if source.type == SourceType.artifact:
        source.taxonomy.enabled = taxonomy_enabled(config, state)
        return False
    if source.type != SourceType.memory:
        return False
    original = json.dumps(source.model_dump(mode="json"), sort_keys=True)
    with state.conn:
        state.conn.execute(
            "INSERT INTO artifact_source_migrations(source_id, original_config_json, migrated_at) "
            "VALUES(?,?,?) ON CONFLICT(source_id) DO NOTHING",
            (source.name, original, datetime.now(UTC).isoformat()),
        )
    source.type = SourceType.artifact
    source.taxonomy.enabled = taxonomy_enabled(config, state)
    return True


def taxonomy_enabled(config: Any, state: Any) -> bool:
    rows = state.rows(
        "SELECT data_json FROM region_settings WHERE kb_id=? AND section='artifacts'",
        (config.knowledge_base_id,),
    )
    values = json.loads(rows[0]["data_json"]) if rows else {}
    return bool(values.get("taxonomy_enabled", config.artifacts.taxonomy_enabled))


def retrieval_source_ids(state: Any) -> list[str]:
    """Saved work and typed records form the artifact retrieval intervention."""
    if state is None:
        return ["artifacts"]
    return list(
        dict.fromkeys(
            [
                "artifacts",
                *(
                    str(row["id"])
                    for row in state.rows(
                        "SELECT id FROM sources WHERE type IN ('artifact','memory') "
                        "AND enabled=1 ORDER BY id"
                    )
                ),
            ]
        )
    )
