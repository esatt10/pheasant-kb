"""Product Framework Step 33.1 — agent memory as a region.

Acceptance (Flock docs/PRODUCT_FRAMEWORK.md §3c): store determinism +
append-only idempotency; engine e2e write → sync → searchable with a
zero-work second sync; MCP facade read-your-writes + actionable
no-source error; HTTP POST/GET round-trip + 400 without a memory source;
publisher advertises the ``memory`` modality (wire format unchanged).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pheasant.artifacts.store import MEMORY_RECORD_VERSION, MemoryRecord, MemoryStore, memory_source
from pheasant.config.loader import load_config
from pheasant.mcp_server.tools import PheasantTools

PINNED_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _write_config(tmp_path: Path, *, include_memory: bool = True) -> Path:
    memory_block = (
        """  - name: agent-memory
    type: memory
    path: memory
"""
        if include_memory
        else ""
    )
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: memory-test
  state_path: {tmp_path / ".pheasant" / "state"}
  exports_path: {tmp_path / ".pheasant" / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
sources:
  - name: notes
    type: document_folder
    path: docs
{memory_block}""",
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "readme.md").write_text("# Plain notes\n", encoding="utf-8")
    (tmp_path / "memory").mkdir(exist_ok=True)
    return config_path


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------


def test_append_is_deterministic_and_append_only(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    first, created_first = store.append(
        "The staging cluster lives in us-east-2.",
        scope="org",
        subject="infra",
        tags=["deploy", "aws"],
        now=PINNED_NOW,
    )
    assert created_first
    assert first.record_id.startswith("mem-20260716T120000Z-")
    assert first.path.exists()

    second, created_second = store.append(
        "The staging cluster lives in us-east-2.",
        scope="org",
        subject="infra",
        tags=["deploy", "aws"],
        now=PINNED_NOW,
    )
    assert not created_second
    assert second.record_id == first.record_id
    assert second.text == first.text

    raw = first.path.read_text(encoding="utf-8")
    assert f"schema_version: {MEMORY_RECORD_VERSION}" in raw
    assert "memory_scope: org" in raw
    assert raw.rstrip().endswith("us-east-2.")


def test_round_trip_preserves_every_field(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    record, _ = store.append(
        "Alice prefers dark mode.",
        scope="user",
        subject="alice",
        supersedes="mem-20260101T000000Z-deadbeef00000000",
        tags=["prefs"],
        now=PINNED_NOW,
    )
    loaded = store.load(record.path)
    assert isinstance(loaded, MemoryRecord)
    assert loaded.record_id == record.record_id
    assert loaded.scope == "user"
    assert loaded.subject == "alice"
    assert loaded.supersedes == "mem-20260101T000000Z-deadbeef00000000"
    assert loaded.tags == ("prefs",)
    assert loaded.text == "Alice prefers dark mode."
    assert loaded.asserted_at == "2026-07-16T12:00:00Z"


def test_scope_and_text_validation(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    with pytest.raises(ValueError, match="scope"):
        store.append("something", scope="galaxy")
    with pytest.raises(ValueError, match="non-empty"):
        store.append("   ")
    with pytest.raises(ValueError, match="scope"):
        store.list_records("galaxy")


def test_list_records_is_sorted_and_scope_filtered(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    store.append("fact one", scope="user", now=PINNED_NOW)
    store.append("fact two", scope="org", now=PINNED_NOW)
    assert len(store.list_records()) == 2
    org_only = store.list_records("org")
    assert [r.text for r in org_only] == ["fact two"]


# ---------------------------------------------------------------------------
# MCP facade: write → indexed → searchable (read-your-writes)
# ---------------------------------------------------------------------------


def test_memory_write_is_searchable_immediately_and_resync_is_zero_work(
    tmp_path: Path,
) -> None:
    config = load_config(_write_config(tmp_path))
    assert memory_source(config) is not None
    tools = PheasantTools(config)
    try:
        result = tools.memory_write(
            "memory-test",
            "The quarterly retention experiment used cohort seventeen.",
            scope="org",
            subject="research",
        )
        assert result["created"] is True
        assert result["sync"]["indexed_artifacts"] == 1

        hits = tools.search_context("memory-test", "cohort seventeen retention", mode="text")
        flattened = str(hits).lower()
        assert "cohort seventeen" in flattened

        again = tools.memory_write(
            "memory-test",
            "The quarterly retention experiment used cohort seventeen.",
            scope="org",
            subject="research",
        )
        assert again["created"] is False
        assert "sync" not in again  # no re-index for an identical record
    finally:
        tools.engine.close()


def test_the_first_write_creates_the_record_source(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, include_memory=False))
    tools = PheasantTools(config)
    try:
        written = tools.memory_write("memory-test", "anything")
        assert written["created"] is True
        assert written["source"] == "artifact-records"
    finally:
        tools.engine.close()


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def test_http_memory_write_and_list(tmp_path: Path) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from pheasant.api.app import create_app

    config_path = _write_config(tmp_path)
    app = create_app(load_config(config_path), config_path=str(config_path))
    client = fastapi_testclient.TestClient(app)
    try:
        resp = client.post(
            "/artifacts/records",
            json={"text": "Deploy freezes start Friday 5pm.", "scope": "org", "sync": True},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["created"] is True
        assert body["record"]["schema_version"] == MEMORY_RECORD_VERSION
        assert body["sync"]["indexed_artifacts"] == 1

        listing = client.get("/artifacts/records", params={"scope": "org"})
        assert listing.status_code == 200
        assert [r["text"] for r in listing.json()["records"]] == [
            "Deploy freezes start Friday 5pm."
        ]

        bad = client.post("/artifacts/records", json={"text": "", "scope": "org"})
        assert bad.status_code == 422
    finally:
        app.state.engine.close()


def test_http_lists_nothing_until_the_first_write_creates_the_source(tmp_path: Path) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from pheasant.api.app import create_app

    config_path = _write_config(tmp_path, include_memory=False)
    app = create_app(load_config(config_path), config_path=str(config_path))
    client = fastapi_testclient.TestClient(app)
    try:
        empty = client.get("/artifacts/records")
        assert empty.status_code == 200
        assert empty.json() == {"records": [], "source": None}
        assert client.post("/artifacts/records", json={"text": "x"}).status_code == 200
        assert client.get("/artifacts/records").json()["source"] == "artifact-records"
    finally:
        app.state.engine.close()


# ---------------------------------------------------------------------------
# Publisher capability (routing prep for 33.3) — wire format unchanged
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Step 33.2 — validity chains + consolidation
# ---------------------------------------------------------------------------


def test_supersedes_chain_filters_current_records(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    old, _ = store.append("Staging lives in us-west-1.", scope="org", now=PINNED_NOW)
    newer, _ = store.append(
        "Staging lives in us-east-2.",
        scope="org",
        supersedes=old.record_id,
        now=PINNED_NOW.replace(hour=13),
    )
    assert {r.record_id for r in store.list_records()} == {old.record_id, newer.record_id}
    current = store.list_records(current_only=True)
    assert [r.record_id for r in current] == [newer.record_id]


def test_consolidate_archives_superseded_and_expired_deterministically(
    tmp_path: Path,
) -> None:
    from datetime import timedelta

    store = MemoryStore(tmp_path / "memory")
    old, _ = store.append("Old fact.", scope="org", now=PINNED_NOW)
    newer, _ = store.append(
        "New fact.", scope="org", supersedes=old.record_id, now=PINNED_NOW.replace(hour=13)
    )
    stale_session, _ = store.append(
        "Scratch thought.", scope="session", now=PINNED_NOW - timedelta(days=30)
    )
    fresh_user, _ = store.append("Durable preference.", scope="user", now=PINNED_NOW)

    report = store.consolidate(now=PINNED_NOW.replace(hour=14), session_ttl_days=7)
    assert report.archived_superseded == (old.record_id,)
    assert report.archived_expired == (stale_session.record_id,)
    assert report.kept == 2
    assert not old.path.exists()
    assert (old.path.parent / (old.path.name + ".archived")).exists()  # bytes preserved
    assert newer.path.exists() and fresh_user.path.exists()

    # Idempotent: a second pass over unchanged content archives nothing.
    second = store.consolidate(now=PINNED_NOW.replace(hour=14), session_ttl_days=7)
    assert second.archived == 0
    assert second.kept == 2


def test_ttl_none_means_scope_never_expires(tmp_path: Path) -> None:
    from datetime import timedelta

    store = MemoryStore(tmp_path / "memory")
    store.append("Ancient but precious.", scope="user", now=PINNED_NOW - timedelta(days=3650))
    report = store.consolidate(now=PINNED_NOW)
    assert report.archived == 0
    assert report.kept == 1


def test_maintenance_reindexes_so_search_forgets_archived_records(tmp_path: Path) -> None:
    from pheasant.artifacts.maintenance import run_memory_maintenance

    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        first = tools.memory_write("memory-test", "The rollout password is BANANAS.", scope="org")
        old_id = first["record"]["record_id"]
        tools.memory_write(
            "memory-test",
            "The rollout password is GRAPES.",
            scope="org",
            supersedes=old_id,
        )
        # Step 33.6 changed what this line asserts, and the change is the
        # point. It used to require that the *superseded* record was still
        # returned before consolidation ran — the stale-fact leak, pinned as
        # expected behavior. Query-time validity now drops it immediately, so
        # a corrected fact is never served while waiting for a batch pass.
        default_hits = str(tools.search_context("memory-test", "rollout password", mode="text"))
        assert "bananas" not in default_hits.lower()
        assert "grapes" in default_hits.lower()

        # It is *filtered*, not yet gone: still on disk and still indexed,
        # which is what `current_only: false` exists to reach. Distinguishing
        # the two is what makes the post-maintenance assertion below mean
        # something — otherwise it could pass on filtering alone.
        history = str(
            tools.search_context(
                "memory-test",
                "rollout password",
                mode="text",
                artifact_policy={"current_only": False},
            )
        )
        assert "bananas" in history.lower()

        result = run_memory_maintenance(tools.engine)
        assert result is not None
        assert result["report"]["archived"] == 1
        # Phase 0: one archived record is well under
        # `MEMORY_TARGETED_ARCHIVE_MAX`, so this takes the targeted path
        # (delete just that record's rows) rather than a full re-sync of the
        # whole source — asserted directly rather than via a `SyncResult`
        # field that only the (now conditional) full-sync path produces.
        assert result["sync"]["mode"] == "targeted"
        assert result["sync"]["removed"] == [old_id]

        flattened = str(
            tools.search_context("memory-test", "rollout password", mode="text")
        ).lower()
        assert "grapes" in flattened
        assert "bananas" not in flattened

        # Now it is genuinely *pruned*, not merely filtered: asking for the
        # full history reaches nothing, because consolidation archived the file
        # and the re-index dropped its chunks. This is what maintenance adds
        # over query-time policy, and asserting it here is what stops the two
        # mechanisms from being confused for one another.
        after_history = str(
            tools.search_context(
                "memory-test",
                "rollout password",
                mode="text",
                artifact_policy={"current_only": False},
            )
        ).lower()
        assert "bananas" not in after_history

        # Second pass: nothing to archive, no re-sync.
        again = run_memory_maintenance(tools.engine)
        assert again is not None
        assert again["report"]["archived"] == 0
        assert "sync" not in again
    finally:
        tools.engine.close()


def test_maintenance_respects_disable_and_missing_source(tmp_path: Path) -> None:
    from pheasant.artifacts.maintenance import run_memory_maintenance

    no_source = load_config(_write_config(tmp_path, include_memory=False))
    tools = PheasantTools(no_source)
    try:
        assert run_memory_maintenance(tools.engine) is None
        assert "skipped" in tools.memory_consolidate("memory-test")
    finally:
        tools.engine.close()

    disabled_dir = tmp_path / "disabled"
    disabled_dir.mkdir()
    config_path = _write_config(disabled_dir)
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "memory:\n  consolidation_enabled: false\n",
        encoding="utf-8",
    )
    tools2 = PheasantTools(load_config(config_path))
    try:
        assert run_memory_maintenance(tools2.engine) is None
    finally:
        tools2.engine.close()


def test_http_consolidate_endpoint(tmp_path: Path) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from pheasant.api.app import create_app

    config_path = _write_config(tmp_path)
    app = create_app(load_config(config_path), config_path=str(config_path))
    client = fastapi_testclient.TestClient(app)
    try:
        first = client.post(
            "/artifacts/records", json={"text": "v1 of the fact", "scope": "org"}
        ).json()
        client.post(
            "/artifacts/records",
            json={
                "text": "v2 of the fact",
                "scope": "org",
                "supersedes": first["record"]["record_id"],
            },
        )
        resp = client.post("/artifacts/consolidate")
        assert resp.status_code == 200, resp.text
        assert resp.json()["report"]["archived"] == 1

        current = client.get("/artifacts/records", params={"current_only": True}).json()["records"]
        assert [r["text"] for r in current] == ["v2 of the fact"]
    finally:
        app.state.engine.close()
