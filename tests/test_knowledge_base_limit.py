"""A knowledge base's size limit: at it, the region keeps what it has and takes no more.

``storage.knowledge_base_limit_gb`` is a ceiling on indexed content. What must
hold: unset, nothing changes (the standalone path); set, the sync that would
add content is skipped *in the engine*, which every way of starting a sync
passes; content handed over directly is refused with one spelling on HTTP and
MCP; and room made by raising the limit or removing content is room at once,
because the measure is what is held and not what a disk remembers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.mcp_server.tools import PheasantTools
from pheasant.persistence import size_limit
from pheasant.services.errors import KnowledgeBaseFull
from pheasant.sync.engine import SyncEngine

#: 150 bytes, stated in gigabytes: above the first note and below the first two.
LIMIT_GB = 150 / size_limit.BYTES_PER_GB


def _config(tmp_path: Path, limit_gb: float | None) -> PheasantConfig:
    for name in ("ws", "state", "exports"):
        (tmp_path / name).mkdir(exist_ok=True)
    notes = tmp_path / "ws" / "notes"
    notes.mkdir(exist_ok=True)
    (notes / "a.md").write_text("# Pheasants\n\n" + "They roost in trees. " * 5 + "\n")
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "kb",
                "defaults": "standalone",
                "workspace_root": str(tmp_path / "ws"),
                "state_path": str(tmp_path / "state"),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"knowledge_base_limit_gb": limit_gb},
            "artifacts": {"index_on_save": False},
            "sources": [
                {
                    "name": "notes",
                    "type": "document_folder",
                    "path": "notes",
                    "include": ["**/*.md"],
                }
            ],
        }
    )


def _paths(engine: SyncEngine) -> set[str]:
    return {
        str(dict(row)["relative_path"])
        for row in engine.state.rows("SELECT relative_path FROM artifacts")
    }


def _second_note(tmp_path: Path) -> None:
    (tmp_path / "ws" / "notes" / "b.md").write_text(
        "# Quail\n\n" + "They nest on the ground. " * 5 + "\n"
    )


def test_with_no_limit_a_region_is_what_it_was(tmp_path: Path) -> None:
    config = _config(tmp_path, None)
    assert PheasantConfig().storage.knowledge_base_limit_gb is None
    engine = SyncEngine(config)
    try:
        assert engine.sync_source("notes").status != "skipped"
        _second_note(tmp_path)
        assert engine.sync_source("notes").status != "skipped"
        assert _paths(engine) == {"a.md", "b.md"}
        assert size_limit.reached(config, engine.state) is None
        assert size_limit.describe(config, engine.state)["limit_gb"] is None
    finally:
        engine.close()


def test_a_full_knowledge_base_skips_the_sync_that_would_add_to_it(tmp_path: Path) -> None:
    config = _config(tmp_path, LIMIT_GB)
    engine = SyncEngine(config)
    try:
        # Empty is under any limit: the first sync runs, and may pass the limit.
        assert engine.sync_source("notes").status != "skipped"
        _second_note(tmp_path)
        assert engine.sync_source("notes").status != "skipped"
        assert _paths(engine) == {"a.md", "b.md"}
        described = size_limit.describe(config, engine.state)
        assert described["full"] and described["limit_used"] >= 1

        (tmp_path / "ws" / "notes" / "c.md").write_text("# Grouse\n\nThey drum.\n")
        for mode in ("incremental", "full"):
            skipped = engine.sync_source("notes", mode)  # type: ignore[arg-type]
            assert skipped.status == "skipped"
            assert skipped.details["reason"] == "knowledge base full"
            assert skipped.details["limit_gb"] == LIMIT_GB
        # Nothing was taken in, and nothing held was given up.
        assert _paths(engine) == {"a.md", "b.md"}
        # Reading and reconciling what is held add nothing, so they still run.
        assert engine.sync_source("notes", "validate_only").status != "skipped"
    finally:
        engine.close()


def test_room_is_room_at_once(tmp_path: Path) -> None:
    config = _config(tmp_path, LIMIT_GB)
    engine = SyncEngine(config)
    try:
        engine.sync_source("notes")
        _second_note(tmp_path)
        engine.sync_source("notes")
        assert size_limit.reached(config, engine.state) is not None

        # Removing content: the measure is what is held, so it falls with it.
        (tmp_path / "ws" / "notes" / "b.md").unlink()
        engine.state.rows("SELECT 1")  # the store is still usable while full
        config.storage.knowledge_base_limit_gb = None
        engine.sync_source("notes")  # the deletion is synced with the limit lifted
        config.storage.knowledge_base_limit_gb = LIMIT_GB
        assert _paths(engine) == {"a.md"}
        assert size_limit.reached(config, engine.state) is None

        # Raising the limit: the operator's answer to a storage request.
        _second_note(tmp_path)
        engine.sync_source("notes")
        assert size_limit.reached(config, engine.state) is not None
        config.storage.knowledge_base_limit_gb = 1.0
        (tmp_path / "ws" / "notes" / "c.md").write_text("# Grouse\n\nThey drum.\n")
        assert engine.sync_source("notes").status != "skipped"
        assert _paths(engine) == {"a.md", "b.md", "c.md"}
    finally:
        engine.close()


def test_content_handed_to_a_full_knowledge_base_is_refused_the_same_on_both_surfaces(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, LIMIT_GB)
    engine = SyncEngine(config)
    try:
        engine.sync_source("notes")
        _second_note(tmp_path)
        engine.sync_source("notes")
    finally:
        engine.close()

    document = {"relative_path": "new.md", "text": "# New\n\nSomething more.\n"}
    with TestClient(create_app(config)) as client:
        refused = client.post(
            "/ingest/submit", json={"knowledge_base": "kb", "documents": [document]}
        )
        assert refused.status_code == 507
        body = refused.json()
        assert "KNOWLEDGE_BASE_FULL" in str(body)
        added = client.post("/sources/quick-add", json={"target": str(tmp_path / "ws" / "notes")})
        assert added.status_code == 507

    tools = PheasantTools(config)
    try:
        with pytest.raises(KnowledgeBaseFull) as raised:
            tools.submit_documents("kb", [document])
        assert raised.value.code == "KNOWLEDGE_BASE_FULL"
        assert not raised.value.retryable
        assert str(raised.value) in str(body)
        assert "request_capacity" in str(raised.value)
    finally:
        tools.engine.close()
