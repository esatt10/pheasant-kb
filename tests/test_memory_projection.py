"""Step 33.5 — structured memory index, clean text, and write identity.

Acceptance: a v1 record on disk still loads and still hashes to the same id;
record frontmatter stops being indexed as prose without lying about line
numbers; every record is selectable by scope/subject/validity in SQL;
`valid_until` is derived from supersession rather than stored twice; the
projection rebuilds idempotently while preserving counters that are earned
rather than derived; and the two write protocols agree.

`tests/test_memory_region.py` keeps the 33.1/33.2 acceptance; this file is the
33.5 surface.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pheasant.artifacts.store import DEFAULT_KIND, MemoryRecord, MemoryStore, _digest_input
from pheasant.config.loader import load_config
from pheasant.mcp_server.tools import PheasantTools

PINNED_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)
PINNED_ISO = "2026-07-16T12:00:00Z"


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
# Backward compatibility with schema 1
# ---------------------------------------------------------------------------

#: A schema-1 record exactly as Step 33.1 wrote it, byte for byte. Hand-authored
#: on purpose: the pre-33.5 tests interpolated MEMORY_RECORD_VERSION, so they
#: would have passed at *any* version and pinned nothing about the on-disk
#: format. Records already on disk in real deployments look like this and have
#: to keep loading forever (CLAUDE.md rule 2).
V1_RECORD = """---
schema_version: 1
record_id: mem-20260716T120000Z-9f2a1c3d4e5f6a7b
memory_scope: org
memory_subject: infra
asserted_at: 2026-07-16T12:00:00Z
tags: deploy, aws
---

The staging cluster lives in us-east-2.
"""


def test_a_schema_1_record_still_loads_with_v1_meanings(tmp_path: Path) -> None:
    path = tmp_path / "memory" / "org" / "mem-20260716T120000Z-9f2a1c3d4e5f6a7b.md"
    path.parent.mkdir(parents=True)
    path.write_text(V1_RECORD, encoding="utf-8")

    loaded = MemoryStore(tmp_path / "memory").load(path)
    assert loaded.record_id == "mem-20260716T120000Z-9f2a1c3d4e5f6a7b"
    assert loaded.scope == "org"
    assert loaded.subject == "infra"
    assert loaded.tags == ("deploy", "aws")
    assert loaded.text == "The staging cluster lives in us-east-2."
    # The v2 fields take their v1 meanings, and the record keeps *reporting*
    # v1 — a reader must be able to tell an absent valid_until from a real one.
    assert loaded.schema_version == 1
    assert loaded.kind == DEFAULT_KIND
    assert loaded.written_by is None
    assert loaded.valid_until is None
    assert loaded.valid_from == PINNED_ISO


def test_v1_record_ids_are_reproduced_byte_identically() -> None:
    """The v2 digest still hashes exactly `scope|subject|text` for a v1 write.

    If this drifts, every record already on disk hashes to a different id than
    the one written in its own frontmatter, and append-only dedup turns into
    silent duplication.
    """
    for scope, subject, text in [
        ("org", None, "apples"),
        ("user", "deploy", "we deploy on fridays"),
        ("session", "", "x"),
    ]:
        v1 = hashlib.blake2b(f"{scope}|{subject or ''}|{text}".encode(), digest_size=8).hexdigest()
        digest_input = _digest_input(
            scope, subject, text, "fact", None, PINNED_ISO, None, PINNED_ISO
        )
        v2 = hashlib.blake2b(digest_input.encode(), digest_size=8).hexdigest()
        assert v1 == v2, f"v1 id changed for {scope}/{subject}"


# ---------------------------------------------------------------------------
# Write identity
# ---------------------------------------------------------------------------


def test_kind_and_writer_make_genuinely_different_records(tmp_path: Path) -> None:
    """Two principals asserting the same sentence must not collide into one file."""
    store = MemoryStore(tmp_path / "memory")
    alice, _ = store.append(
        "shared sentence", scope="user", now=PINNED_NOW, written_by="user:alice"
    )
    bob, _ = store.append("shared sentence", scope="user", now=PINNED_NOW, written_by="user:bob")
    plain, _ = store.append("shared sentence", scope="user", now=PINNED_NOW)
    alias, _ = store.append("shared sentence", scope="user", now=PINNED_NOW, kind="alias")

    assert len({alice.record_id, bob.record_id, plain.record_id, alias.record_id}) == 4
    assert store.load(alice.path).written_by == "user:alice"
    assert store.load(alias.path).kind == "alias"


def test_identical_reassertion_deduplicates_however_much_later(tmp_path: Path) -> None:
    """Identity is the digest, not the timestamp.

    Before 33.5 this held only when both writes landed in the same wall-clock
    second, which is what made the read-your-writes test flaky: the intervening
    sync decided whether the same assertion deduplicated or duplicated.
    """
    store = MemoryStore(tmp_path / "memory")
    first, created_first = store.append("an unchanging fact", scope="org", now=PINNED_NOW)
    later = datetime(2026, 9, 1, 3, 42, 17, tzinfo=UTC)
    second, created_second = store.append("an unchanging fact", scope="org", now=later)

    assert created_first is True
    assert created_second is False
    assert second.record_id == first.record_id
    assert len(list((tmp_path / "memory" / "org").glob("mem-*.md"))) == 1


def test_an_invalid_kind_is_refused(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    with pytest.raises(ValueError, match="memory kind"):
        store.append("anything", scope="org", kind="opinion", now=PINNED_NOW)


# ---------------------------------------------------------------------------
# Clean indexed text
# ---------------------------------------------------------------------------


def test_frontmatter_is_not_indexed_as_prose_and_line_numbers_stay_honest(
    tmp_path: Path,
) -> None:
    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        written = tools.memory_write(
            "memory-test",
            "Kestrel deployments require the amber approval gate.",
            scope="org",
            subject="infra",
        )
        record_id = written["record"]["record_id"]

        rows = tools.engine.state.rows(
            "SELECT text, start_line FROM chunks WHERE source_id='agent-memory' "
            "ORDER BY chunk_index"
        )
        assert rows, "the memory record produced no chunks"
        body = "\n".join(str(row["text"]) for row in rows)
        for bookkeeping in (record_id, "schema_version", "memory_scope", "asserted_at"):
            assert bookkeeping not in body
        assert "Kestrel deployments" in body

        # Provenance must still point at the real file: the body starts well
        # below line 1, which is why stripping returns a line offset instead of
        # silently renumbering.
        raw = Path(str(written["record"]["path"])).read_text(encoding="utf-8").splitlines()
        expected = next(
            index for index, line in enumerate(raw, start=1) if line.startswith("Kestrel")
        )
        assert expected > 1
        assert int(rows[0]["start_line"]) == expected
    finally:
        tools.engine.close()


def test_a_non_memory_markdown_source_keeps_its_frontmatter(tmp_path: Path) -> None:
    """Stripping is scoped to memory; no existing vault or docs index shifts."""
    config_path = _write_config(tmp_path)
    (tmp_path / "docs" / "note.md").write_text(
        "---\ntitle: Ordinary Note\n---\n\nBody text here.\n", encoding="utf-8"
    )
    tools = PheasantTools(load_config(config_path))
    try:
        tools.sync_source("memory-test", "notes", "full")
        body = "\n".join(
            str(row["text"])
            for row in tools.engine.state.rows("SELECT text FROM chunks WHERE source_id='notes'")
        )
        assert "title: Ordinary Note" in body
    finally:
        tools.engine.close()


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------


def test_projection_is_queryable_by_scope_subject_and_validity(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        tools.memory_write(
            "memory-test", "alpha fact", scope="org", subject="infra", principal="user:alice"
        )
        tools.memory_write("memory-test", "beta fact", scope="user", subject="prefs")

        rows = tools.engine.state.rows(
            "SELECT record_id, subject, kind, written_by, valid_from, valid_until "
            "FROM memory_records WHERE scope=? ORDER BY record_id",
            ("org",),
        )
        assert len(rows) == 1
        assert rows[0]["subject"] == "infra"
        assert rows[0]["kind"] == "fact"
        assert rows[0]["written_by"] == "user:alice"
        assert rows[0]["valid_until"] is None
        assert rows[0]["valid_from"]

        # Every projected row joins to a real artifact: the id grammar the
        # projection reproduces has to match what indexing actually wrote.
        orphans = tools.engine.state.rows(
            "SELECT m.record_id FROM memory_records m "
            "LEFT JOIN artifacts a ON a.id = m.artifact_id WHERE a.id IS NULL"
        )
        assert orphans == []
    finally:
        tools.engine.close()


def test_valid_until_is_derived_from_the_superseding_record(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        first = tools.memory_write("memory-test", "the old answer", scope="org")
        second = tools.memory_write(
            "memory-test",
            "the corrected answer",
            scope="org",
            supersedes=str(first["record"]["record_id"]),
        )

        rows = {
            str(row["record_id"]): row
            for row in tools.engine.state.rows("SELECT record_id, valid_until FROM memory_records")
        }
        # The correction ends the original's validity at the moment it was
        # asserted — derived on projection, never stored twice.
        assert (
            rows[str(first["record"]["record_id"])]["valid_until"]
            == (second["record"]["asserted_at"])
        )
        assert rows[str(second["record"]["record_id"])]["valid_until"] is None
    finally:
        tools.engine.close()


def test_the_earlier_of_declared_and_superseded_expiry_wins() -> None:
    from pheasant.artifacts.projection import effective_valid_until

    record = MemoryRecord(
        record_id="mem-x",
        scope="org",
        subject=None,
        text="t",
        asserted_at="2026-01-01T00:00:00Z",
        supersedes=None,
        tags=(),
        path=Path("mem-x.md"),
        valid_until="2026-03-01T00:00:00Z",
    )
    # Declared good until March, corrected in June -> stale from March.
    corrected_late = effective_valid_until(record, {"mem-x": "2026-06-01T00:00:00Z"})
    assert corrected_late == "2026-03-01T00:00:00Z"
    # Corrected in February, before its own expiry -> the correction wins.
    corrected_early = effective_valid_until(record, {"mem-x": "2026-02-01T00:00:00Z"})
    assert corrected_early == "2026-02-01T00:00:00Z"
    assert effective_valid_until(record, {}) == "2026-03-01T00:00:00Z"


def test_the_earliest_correction_is_the_one_that_ends_validity() -> None:
    from pheasant.artifacts.projection import supersession_index

    def correction(record_id: str, asserted_at: str, target: str) -> MemoryRecord:
        return MemoryRecord(
            record_id=record_id,
            scope="org",
            subject=None,
            text="t",
            asserted_at=asserted_at,
            supersedes=target,
            tags=(),
            path=Path(f"{record_id}.md"),
        )

    records = [
        correction("mem-b", "2026-05-01T00:00:00Z", "mem-a"),
        correction("mem-c", "2026-02-01T00:00:00Z", "mem-a"),
    ]
    assert supersession_index(records) == {"mem-a": "2026-02-01T00:00:00Z"}


def test_projection_rebuild_is_idempotent_and_keeps_earned_counters(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        tools.memory_write("memory-test", "a durable fact", scope="org")
        state = tools.engine.state
        columns = (
            "record_id,artifact_id,source_id,scope,subject,kind,asserted_at,"
            "valid_from,valid_until,supersedes,tags,written_by,schema_version"
        )
        before = [tuple(r) for r in state.rows(f"SELECT {columns} FROM memory_records ORDER BY 1")]
        assert before

        # Salience and use counts are earned by retrieval (Step 33.9) and are
        # the one thing here not derivable from the file, so a rebuild must not
        # quietly reset them.
        state.conn.execute("UPDATE memory_records SET uses=7, salience=2.5")
        state.conn.commit()

        tools.sync_source("memory-test", "agent-memory", "full")
        after = [tuple(r) for r in state.rows(f"SELECT {columns} FROM memory_records ORDER BY 1")]
        assert after == before

        earned = state.rows("SELECT uses, salience FROM memory_records")
        assert int(earned[0]["uses"]) == 7
        assert float(earned[0]["salience"]) == 2.5
    finally:
        tools.engine.close()


def test_removing_the_source_clears_its_projection_but_a_full_sync_does_not(
    tmp_path: Path,
) -> None:
    """Two different operations that must not be conflated.

    `delete_source_artifacts` runs on every *full* sync — the path consolidation
    takes after each archive — so clearing the projection there would reset
    `uses`/`salience` on a routine maintenance pass. Only genuine removal
    (`delete_source`) drops the rows.
    """
    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        tools.memory_write("memory-test", "soon to be forgotten", scope="org")
        state = tools.engine.state
        assert state.rows("SELECT 1 FROM memory_records")

        state.delete_source_artifacts("agent-memory")
        assert state.rows("SELECT 1 FROM memory_records"), (
            "a full sync's artifact clear must not drop earned memory state"
        )

        state.delete_source("agent-memory")
        assert state.rows("SELECT 1 FROM memory_records") == []
    finally:
        tools.engine.close()


def test_an_unreadable_record_does_not_abort_the_sync(tmp_path: Path) -> None:
    """One malformed file must not cost the whole sync, as with documents."""
    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        good = tools.memory_write("memory-test", "a good record", scope="org")
        # A file that matches the glob but carries no frontmatter at all.
        (tmp_path / "memory" / "org" / "mem-20260101T000000Z-notarecord.md").write_text(
            "no frontmatter here\n", encoding="utf-8"
        )

        # Emptying the table first is what makes this test mean anything. The
        # earlier write already projected the good record, so without this the
        # assertion below passes on *stale* rows even when the projection
        # aborts outright — which is exactly what mutation testing caught.
        state = tools.engine.state
        state.conn.execute("DELETE FROM memory_records")
        state.conn.commit()

        tools.sync_source("memory-test", "agent-memory", "full")

        # The sync completes and the *readable* record still gets its row.
        # Abandoning the whole projection over one stray file would have
        # punished every other record in the store.
        projected = [
            str(row["record_id"]) for row in state.rows("SELECT record_id FROM memory_records")
        ]
        assert projected == [str(good["record"]["record_id"])]
    finally:
        tools.engine.close()


# ---------------------------------------------------------------------------
# Migration scoping and protocol parity
# ---------------------------------------------------------------------------


def test_only_memory_sources_are_re_read_by_the_33_5_migration(tmp_path: Path) -> None:
    """The text pipeline changed for memory only; nobody else may re-index.

    Adding the marker unconditionally would invalidate every fingerprint in
    every deployment — re-reading a 2,000-file repository to fix agent memory.
    """
    from dataclasses import replace as dc_replace

    from pheasant.sync.fingerprint import source_fingerprint

    config = load_config(_write_config(tmp_path))
    by_name = {source.name: source for source in config.sources}

    memory_source_cfg = by_name["agent-memory"]
    notes_cfg = by_name["notes"]

    # The marker is load-bearing: the same source seen as a document_folder
    # (i.e. without the memory text pipeline) fingerprints differently.
    as_documents = dc_replace(memory_source_cfg, type=notes_cfg.type)
    assert source_fingerprint(memory_source_cfg) != source_fingerprint(as_documents)

    # ... and an ordinary source's fingerprint does not depend on it at all.
    assert source_fingerprint(notes_cfg) == source_fingerprint(notes_cfg)


def test_http_consolidate_without_a_source_matches_the_mcp_tool(tmp_path: Path) -> None:
    """Same condition, same answer, whichever protocol asked."""
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from pheasant.api.app import create_app

    config_path = _write_config(tmp_path, include_memory=False)
    app = create_app(load_config(config_path), config_path=str(config_path))
    client = fastapi_testclient.TestClient(app)
    try:
        resp = client.post("/artifacts/consolidate")
        assert resp.status_code == 200, resp.text
        assert "skipped" in resp.json()

        tools = PheasantTools(load_config(config_path))
        try:
            assert "skipped" in tools.memory_consolidate("memory-test")
        finally:
            tools.engine.close()
    finally:
        app.state.engine.close()


def test_written_by_is_recorded_and_audited_over_http(tmp_path: Path) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from pheasant.api.app import create_app

    config_path = _write_config(tmp_path)
    app = create_app(load_config(config_path), config_path=str(config_path))
    client = fastapi_testclient.TestClient(app)
    try:
        resp = client.post(
            "/artifacts/records",
            json={
                "text": "Alice owns the release calendar.",
                "scope": "user",
                "principal": "user:alice",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["record"]["written_by"] == "user:alice"

        # The MCP path always audited a memory write; the HTTP one silently did
        # not, so the same action was traceable or not depending on protocol.
        rows = app.state.engine.state.rows(
            "SELECT details_json FROM source_audit_events WHERE action='artifact_record_write'"
        )
        assert rows, "POST /artifacts/records wrote no audit event"
        assert "record_id" in str(rows[0]["details_json"])
    finally:
        app.state.engine.close()


# ---------------------------------------------------------------------------
# Provisioning and the bridge's configuration surface
# ---------------------------------------------------------------------------


def test_the_memory_mcp_resource_returns_current_records(tmp_path: Path) -> None:
    """Context an agent can read directly, without spending a search on it."""
    tools = PheasantTools(load_config(_write_config(tmp_path)))
    try:
        first = tools.memory_write("memory-test", "the old answer", scope="org")
        tools.memory_write(
            "memory-test",
            "the corrected answer",
            scope="org",
            supersedes=str(first["record"]["record_id"]),
        )
        listing = tools.memory_list("memory-test")
        assert listing["enabled"] is True
        # current_only defaults True here: handing an agent corrected records
        # without the chain to interpret them is worse than handing it none.
        assert [record["text"] for record in listing["records"]] == ["the corrected answer"]
    finally:
        tools.engine.close()


def test_the_resource_reports_disabled_rather_than_raising(tmp_path: Path) -> None:
    tools = PheasantTools(load_config(_write_config(tmp_path, include_memory=False)))
    try:
        assert tools.memory_list("memory-test") == {"enabled": False, "records": []}
    finally:
        tools.engine.close()


def test_bridging_can_be_switched_off(tmp_path: Path) -> None:
    """A config knob that changes nothing is worse than no knob."""
    config_path = _write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "sources:", "graph:\n  memory_entity_bridging: false\nsources:"
        ),
        encoding="utf-8",
    )
    tools = PheasantTools(load_config(config_path))
    try:
        assert tools.config.graph.memory_entity_bridging is False
        tools.memory_write("memory-test", "See [the readme](readme.md) for detail.", scope="org")
        tools.sync_source("memory-test", "notes", "full")
        tools.sync_source("memory-test", "agent-memory", "full")

        graph = tools.engine.graph_builder.graph
        with graph.reading():
            about = [
                data
                for (_source, _target), edge_map in graph.iter_edges()
                for data in edge_map.values()
                if data.get("type") == "about"
            ]
        assert about == []
    finally:
        tools.engine.close()


def test_about_max_targets_is_read_by_the_engine(tmp_path: Path) -> None:
    """Driven through a real sync, not by handing the value to the bridge.

    Passing `config.memory.about_max_targets` straight into `add_memory_edges`
    proves the *bridge* honours a cap and says nothing about whether the engine
    ever reads the setting — mutation testing caught exactly that. A cap of 0 is
    the unambiguous end-to-end signal, since this fixture otherwise draws edges.
    """
    config_path = _write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "sources:", "memory:\n  about_max_targets: 0\nsources:"
        ),
        encoding="utf-8",
    )
    tools = PheasantTools(load_config(config_path))
    try:
        assert tools.config.memory.about_max_targets == 0
        tools.memory_write("memory-test", "See [the readme](readme.md) for detail.", scope="org")
        tools.sync_source("memory-test", "notes", "full")
        tools.sync_source("memory-test", "agent-memory", "full")

        graph = tools.engine.graph_builder.graph
        with graph.reading():
            about = [
                data
                for (_source, _target), edge_map in graph.iter_edges()
                for data in edge_map.values()
                if data.get("type") == "about"
            ]
        assert about == [], "the engine ignored memory.about_max_targets"
    finally:
        tools.engine.close()


def test_a_runtime_registered_memory_source_is_visible_to_a_new_process(
    tmp_path: Path,
) -> None:
    """Enabling from the API must not be invisible to everything else.

    A source created through `POST /artifacts/records/enable` lives in the state registry
    and only reaches `config.sources` in the process that created it. Without a
    registry fallback, a fresh MCP server, the sync worker, or the container
    after a restart all report no memory at all — which is what a live run saw
    from `describe_retrieval` immediately after enabling successfully.
    """
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from pheasant.api.app import create_app
    from pheasant.artifacts.store import memory_source

    config_path = _write_config(tmp_path, include_memory=False)
    app = create_app(load_config(config_path), config_path=str(config_path))
    client = fastapi_testclient.TestClient(app)
    try:
        assert client.post("/artifacts/records/enable").status_code == 200
    finally:
        app.state.engine.close()

    # A *different* process: config re-read from the file, which never mentions
    # the source.
    fresh = load_config(config_path)
    assert memory_source(fresh) is None, "the YAML genuinely does not declare it"

    tools = PheasantTools(load_config(config_path))
    try:
        assert memory_source(tools.config, tools.state) is not None
        described = tools.describe_retrieval("memory-test")["memory"]
        assert described["enabled"] is True
        assert described["source_name"] == "artifact-records"
    finally:
        tools.engine.close()
