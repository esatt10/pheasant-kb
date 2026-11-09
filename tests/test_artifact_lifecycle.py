"""Keeping is opt-in, indexed on a schedule, filed by date then person, traceable,
deletable in bulk and archived by retention (`services/artifact_*.py`)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.mcp_server.tools import PheasantTools
from pheasant.services import ServiceContext, artifact_lifecycle, artifacts
from pheasant.services.archive import Indexing
from pheasant.sync import pipeline

COMPOSED = "Counts accounts [1].\n\n### `counts.js`\n\n```javascript\nexport const n = 1;\n```\n"
CITED = [{"index": 1, "source_id": "architecture-notes", "relative_path": "sync-engine.md"}]


def _answer(question: str, intent: str = "compose") -> dict[str, Any]:
    return {
        "question": question,
        "answer": COMPOSED if intent == "compose" else "The engine syncs sources [1].",
        "answer_mode": "llm",
        "route": {"intent": intent, "depth": "short"},
        "keywords": {"used": ["@compose"]} if intent == "compose" else None,
        "decisions": {"accepted": True, "sufficiency": 0.91},
        "citations": CITED,
        "steps": [{"name": "plan", "detail": "one query per component"}],
        "visual": {"status": "none"},
    }


@pytest.fixture()
def composing(monkeypatch: pytest.MonkeyPatch) -> None:
    from pheasant.assistant import chat

    monkeypatch.setattr(chat, "answer_question", lambda question, **_k: _answer(question))


def _context(app: Any) -> ServiceContext:
    return ServiceContext(config=app.state.config, state=app.state.state, searcher=None)


def test_keeping_is_opt_in_and_the_switch_reaches_every_surface(
    loaded_config, composing: None
) -> None:
    app = create_app(config=loaded_config)
    client = TestClient(app)
    tools = PheasantTools(loaded_config)
    kb = loaded_config.pheasant.name

    # Off by default: an answer is not kept unless somebody asks.
    assert "artifact" not in client.post("/assistant/chat", json={"question": "@compose c"}).json()
    asked = client.post(
        "/assistant/chat",
        json={"question": "@compose c", "save": True, "conversation_id": "conv-7"},
        headers={"X-Pheasant-Modality": "ui"},
    ).json()
    kept = artifacts.get(_context(app), asked["artifact"]["id"])
    assert kept["channel"] == "ui" and kept["conversation_id"] == "conv-7"
    assert "/conv-7/" in kept["folder"]
    trace = kept["payload"]["trace"]
    assert trace["intent"] == "compose" and trace["confidence"]["accepted"] is True
    assert trace["steps"][0]["name"] == "plan" and kept["index_status"] == "pending"

    # The switch, set over HTTP, is read by MCP -- and the other way round.
    assert client.put("/artifacts/settings", json={"auto_save": True}).json()["settings"][
        "auto_save"
    ]
    assert tools.artifact_settings(kb)["settings"]["auto_save"] is True
    assert "artifact" in client.post("/assistant/chat", json={"question": "@compose d"}).json()
    via_mcp = tools.ask_knowledge_base(kb, "@compose e", conversation_id="s-1")
    assert artifacts.get(_context(app), via_mcp["artifact"]["id"])["channel"] == "mcp"
    tools.artifact_settings(kb, {"auto_save": False})
    described = client.get("/artifacts/settings").json()
    assert described["settings"]["auto_save"] is False and described["changed"] == []
    assert "artifact" not in client.post("/assistant/chat", json={"question": "@compose f"}).json()

    refused = client.put("/artifacts/settings", json={"autosave": True})
    assert refused.status_code == 422 and "unknown artifacts setting" in refused.text
    client.put("/artifacts/settings", json={"enabled": False})
    off = client.post("/artifacts", json={"markdown": "x"})
    assert off.status_code == 422 and "turned off" in off.text


def test_saving_starts_the_sync_unless_told_to_wait_for_the_schedule(loaded_config) -> None:
    """``index_on_save`` (the default) starts the artifacts sync; off, the schedule indexes it."""

    app = create_app(config=loaded_config)
    started: list[str] = []
    context = ServiceContext(
        config=loaded_config,
        state=app.state.state,
        searcher=None,
        indexing=Indexing(local=True, start_sync=lambda name: started.append(name) or {}),
    )
    saved = artifacts.save(context, artifacts.SaveRequest(markdown="# Kept"))
    assert started == ["artifacts"] and saved["index_status"] == "pending"
    from pheasant.services import artifact_settings as settings_service

    settings_service.update(context, {"index_on_save": False}, "ops")
    artifacts.save(context, artifacts.SaveRequest(markdown="# Later"))
    assert started == ["artifacts"]
    source = next(s for s in loaded_config.sources if s.name == "artifacts")
    assert source.sync.yield_to_content and source.sync.min_interval_seconds == 1800
    assert source.sync.on_startup is False
    # A change to the schedule reaches the registered source.
    from pheasant.services import artifact_settings

    artifact_settings.update(context, {"index_interval_seconds": 600}, "ops")
    source = next(s for s in loaded_config.sources if s.name == "artifacts")
    assert source.sync.min_interval_seconds == 600


def test_a_thread_keeps_every_turn_with_its_trace_and_a_composes_files(loaded_config) -> None:
    app = create_app(config=loaded_config)
    client = TestClient(app)
    turns = [
        {"question": "How does sync work?", "answer": _answer("How does sync work?", "knowledge")},
        {"question": "@compose a counter", "answer": _answer("@compose a counter")},
        {"question": "Explain the file", "answer": _answer("Explain the file", "knowledge")},
    ]
    saved = client.post(
        "/artifacts", json={"kind": "thread", "turns": turns, "conversation_id": "conv-9"}
    ).json()
    assert saved["kind"] == "thread" and "/conv-9/" in saved["folder"]
    payload = saved["payload"]
    assert [t["question"] for t in payload["turns"]] == [t["question"] for t in turns]
    assert payload["turns"][1]["trace"]["intent"] == "compose"
    assert [f["name"] for f in payload["files"]] == ["turn-2-counts.js"]
    assert {c["turn"] for c in payload["citations"]} == {1, 2, 3}
    page = client.get(f"/artifacts/{saved['id']}/view").text
    assert "How does sync work?" in page and "Explain the file" in page
    listed = client.get("/artifacts", params={"conversation_id": "conv-9"}).json()["artifacts"]
    assert [a["id"] for a in listed] == [saved["id"]]
    too_many = client.post("/artifacts", json={"kind": "thread", "turns": []})
    assert too_many.status_code == 422


def _row(state: Any, kb: str, artifact_id: str, owner: str, created: str) -> None:
    state.execute(
        "INSERT INTO saved_artifacts(id, kb_id, owner, kind, title, payload_json, created_at, "
        "updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            artifact_id,
            kb,
            owner,
            "report",
            artifact_id,
            json.dumps(
                {"answer": "x", "files": [], "folder": f"2026/11/01/{owner}/s/x--{artifact_id}"}
            ),
            created,
            created,
        ),
    )


def test_a_listing_reads_newest_day_first_then_person_then_time(loaded_config) -> None:
    app = create_app(config=loaded_config)
    state, kb = app.state.state, loaded_config.knowledge_base_id
    _row(state, kb, "a" * 24, "zoe", "2026-11-02T09:00:00+00:00")
    _row(state, kb, "b" * 24, "ada", "2026-11-02T08:00:00+00:00")
    _row(state, kb, "c" * 24, "ada", "2026-11-02T10:00:00+00:00")
    _row(state, kb, "d" * 24, "ada", "2026-11-01T23:00:00+00:00")
    listed = artifacts.list_artifacts(_context(app))["artifacts"]
    assert [(a["created_at"][:13], a["owner"]) for a in listed] == [
        ("2026-11-02T10", "ada"),
        ("2026-11-02T08", "ada"),
        ("2026-11-02T09", "zoe"),
        ("2026-11-01T23", "ada"),
    ]


def test_bulk_delete_says_what_it_would_take_then_takes_it(loaded_config) -> None:
    app = create_app(config=loaded_config)
    client = TestClient(app)
    context = _context(app)
    for n in range(3):
        artifacts.save(context, artifacts.SaveRequest(title=f"a{n}", markdown="x", principal="ada"))
    keep = artifacts.save(
        context, artifacts.SaveRequest(title="g", markdown="x", principal="grace")
    )

    dry = client.post("/artifacts/delete", json={"owner": "ada", "dry_run": True}).json()
    assert dry["matched"] == 3 and len(artifacts.list_artifacts(context)["artifacts"]) == 4
    done = client.post("/artifacts/delete", json={"owner": "ada"}).json()
    assert done["matched"] == 3 and len(done["deleted"]) == 3
    assert [a["id"] for a in artifacts.list_artifacts(context)["artifacts"]] == [keep["id"]]

    nothing = client.post("/artifacts/delete", json={})
    assert nothing.status_code == 422 and "name the artifacts" in nothing.text
    # MCP's bulk delete is a dry run unless told otherwise.
    tools = PheasantTools(loaded_config)
    kb = loaded_config.pheasant.name
    assert tools.artifacts_lifecycle(kb, "delete", dry_run=True, owner="grace")["dry_run"]
    assert artifacts.get(context, keep["id"])["id"] == keep["id"]


def test_retention_archives_old_work_out_of_the_index_and_restore_brings_it_back(
    loaded_config,
) -> None:
    from pheasant.services import artifact_settings
    from pheasant.sync.engine import SyncEngine

    engine = SyncEngine(loaded_config)
    try:
        context = ServiceContext(
            config=loaded_config,
            state=engine.state,
            searcher=None,
            engine=engine,
            indexing=Indexing(local=True, start_sync=lambda name: {}),
        )
        old = artifacts.save(context, artifacts.SaveRequest(title="Old", markdown="# Old"))
        new = artifacts.save(context, artifacts.SaveRequest(title="New", markdown="# New"))
        engine.sync_source("artifacts", "incremental")
        long_ago = (datetime.now(UTC) - timedelta(days=40)).isoformat(timespec="seconds")
        engine.state.execute(
            "UPDATE saved_artifacts SET created_at=? WHERE id=?", (long_ago, old["id"])
        )

        assert artifact_lifecycle.run_retention(context) == {"archived": 0, "enabled": False}
        artifact_settings.update(context, {"retention_days": 30}, "ops")
        assert artifact_lifecycle.run_retention(context)["archived"] == 1
        live = artifacts.list_artifacts(context)["artifacts"]
        assert [a["id"] for a in live] == [new["id"]]
        archived = artifacts.list_artifacts(context, archived=True)["artifacts"]
        assert [a["id"] for a in archived] == [old["id"]]
        # Out of the index, and still held: removing its index page did not retire it.
        engine.sync_source("artifacts", "incremental")
        paths = {
            r["relative_path"]
            for r in engine.state.rows(
                "SELECT relative_path FROM artifacts WHERE source_id=?", ("artifacts",)
            )
        }
        assert not any(p.startswith(old["folder"]) for p in paths)
        assert artifacts.get(context, old["id"])["archived_at"]

        restored = artifact_lifecycle.restore(
            context, artifact_lifecycle.Selection(ids=[old["id"]])
        )
        assert restored["restored"] == [old["id"]]
        engine.sync_source("artifacts", "incremental")
        paths = {
            r["relative_path"]
            for r in engine.state.rows(
                "SELECT relative_path FROM artifacts WHERE source_id=?", ("artifacts",)
            )
        }
        assert f"{old['folder']}/README.md" in paths
    finally:
        engine.close()


class _State:
    def __init__(self, indexed: dict[str, str], running: list[str]) -> None:
        self.indexed, self.running = indexed, running

    def rows(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        if "FROM sources" in sql:
            stamp = self.indexed.get(params[0])
            return [{"last_indexed_at": stamp}] if stamp else []
        return [{"source_id": name} for name in self.running]


def _source(name: str, **sync: Any) -> Any:
    settings = {"schedule": "auto", "paused": False, "pipeline": True, **sync}
    return SimpleNamespace(name=name, enabled=True, sync=SimpleNamespace(**settings))


def test_the_beat_holds_a_paced_source_to_its_interval_and_lets_content_go_first() -> None:
    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    ago = lambda seconds: (now - timedelta(seconds=seconds)).isoformat()  # noqa: E731
    content = _source("docs")
    paced = _source("artifacts", min_interval_seconds=1800, yield_to_content=True)

    def due(indexed: dict[str, str], running: list[str]) -> list[str]:
        state = _State(indexed, running)
        return [s.name for s in pipeline.beat_sources([paced, content], state, now=now)]

    # Never indexed: due, and after the content.
    assert due({}, []) == ["docs", "artifacts"]
    # Inside its interval: held.
    assert due({"artifacts": ago(60)}, []) == ["docs"]
    # Due, but content is syncing: it gives way...
    assert due({"artifacts": ago(2000)}, ["docs"]) == ["docs"]
    # ...until it has waited four intervals.
    assert due({"artifacts": ago(4 * 1800)}, ["docs"]) == ["docs", "artifacts"]
    # Its own heartbeat is not content.
    assert due({"artifacts": ago(2000)}, ["artifacts"]) == ["docs", "artifacts"]
    # An unpaced region is untouched.
    assert [s.name for s in pipeline.beat_sources([content], _State({}, []), now=now)] == ["docs"]


def test_an_artifact_filed_under_the_old_layout_moves_to_the_date_first_one(
    loaded_config,
) -> None:
    from pheasant.sync.engine import SyncEngine

    engine = SyncEngine(loaded_config)
    try:
        context = ServiceContext(
            config=loaded_config,
            state=engine.state,
            searcher=None,
            engine=engine,
            indexing=Indexing(local=True, start_sync=lambda name: {}),
        )
        saved = artifacts.save(context, artifacts.SaveRequest(title="Plan", markdown="# Plan"))
        record = artifacts._load(context, saved["id"])
        old = f"ada@example.com/2026/11/plan--{saved['id']}"
        record["payload"]["folder"] = old
        artifacts._save_payload(context, saved["id"], record["payload"])
        artifacts._publish(context, record)
        engine.sync_source("artifacts", "full")

        [listed] = artifacts.list_artifacts(context)["artifacts"]
        assert listed["folder"].startswith(saved["created_at"][:10].replace("-", "/"))
        engine.sync_source("artifacts", "incremental")
        paths = {
            r["relative_path"]
            for r in engine.state.rows(
                "SELECT relative_path FROM artifacts WHERE source_id=?", ("artifacts",)
            )
        }
        assert f"{listed['folder']}/README.md" in paths
        assert not any(p.startswith(old) for p in paths)
        assert artifacts.get(context, saved["id"])["folder"] == listed["folder"]
    finally:
        engine.close()


DRAWN = {
    "type": "diagram",
    "status": "ok",
    "diagram": {"kind": "sequence", "title": "Redrawn", "nodes": [], "edges": []},
    "redraw": {"knowledge_base": "kb", "request": "q", "node_ids": ["n1"]},
}


def test_a_diagram_drawn_after_the_answer_is_the_one_kept(loaded_config) -> None:
    """A redraw (MCP Apps "Redraw as", create_visual) saved is drawn in the report."""

    from pheasant.services import artifact_files

    app = create_app(config=loaded_config)
    client = TestClient(app)
    saved = client.post(
        "/artifacts",
        json={
            "title": "Flow",
            "markdown": "The engine syncs [1].",
            "visual": DRAWN,
            "citations": [{"index": 1, "source_id": "docs", "relative_path": "a.md"}],
        },
    ).json()
    kept = saved["payload"]["visual"]
    assert kept["diagram"]["kind"] == "sequence" and "redraw" not in kept
    assert saved["has_visual"] and saved["payload"]["citations"][0]["path"] == "a.md"
    record = artifacts._load(_context(app), saved["id"])
    assert "report.html" in [name for name, _text in artifact_files.source_files(record)]
    page = client.get(f"/artifacts/{saved['id']}/view").text
    assert 'id="visual-data"' in page and "Redrawn" in page

    # Over MCP too, and a thread turn keeps the diagram drawn for it.
    tools = PheasantTools(loaded_config)
    kb = loaded_config.pheasant.name
    assert tools.save_artifact(kb, title="v", visual=DRAWN)["has_visual"]
    turns = [{"question": "Draw it", "answer": _answer("Draw it", "knowledge"), "visual": DRAWN}]
    thread = client.post("/artifacts", json={"kind": "thread", "turns": turns}).json()
    assert thread["payload"]["visual"]["diagram"]["title"] == "Redrawn"
    assert thread["payload"]["turns"][0]["visual"]["diagram"]["kind"] == "sequence"

    bad = client.post("/artifacts", json={"markdown": "x", "visual": {"status": "declined"}})
    assert bad.status_code == 422 and "drawn diagram" in bad.text


def test_the_app_view_saves_its_diagram_through_the_host() -> None:
    """The view's Save asks the host for save_artifact; the web UI's host answers it."""

    from pathlib import Path

    view = Path("src/pheasant/mcp_server/apps/knowledge_view.html").read_text(encoding="utf-8")
    assert 'name: "save_artifact"' in view and "holder.drawn" in view
    host = Path("ui/src/chat/McpAppFrame.tsx").read_text(encoding="utf-8")
    assert 'name === "save_artifact"' in host


def test_a_folder_is_removed_and_reinstated_on_both_surfaces(loaded_config) -> None:
    """Every document beneath a folder, by one name; an artifacts folder retires its artifacts."""

    from pheasant.services import archive
    from pheasant.sync.engine import SyncEngine

    engine = SyncEngine(loaded_config)
    try:
        context = ServiceContext(
            config=loaded_config,
            state=engine.state,
            searcher=None,
            engine=engine,
            indexing=Indexing(local=True, start_sync=lambda name: {}),
        )
        kept = [
            artifacts.save(
                context,
                artifacts.SaveRequest(
                    title=f"t{n}", markdown="# x", principal="ada", conversation_id="c1"
                ),
            )
            for n in range(2)
        ]
        other = artifacts.save(
            context, artifacts.SaveRequest(title="g", markdown="# y", principal="grace")
        )
        engine.sync_source("artifacts", "incremental")
        day_person = kept[0]["folder"].rsplit("/", 2)[0]  # <yyyy>/<mm>/<dd>/ada

        refused = archive.ArchiveRequest(folders=["artifacts"])
        with pytest.raises(ValueError, match="whole source"):
            archive.archive_documents(context, refused)
        with pytest.raises(ValueError, match="not a source"):
            archive.archive_documents(context, archive.ArchiveRequest(folders=["nowhere/x"]))

        removed = archive.archive_documents(
            context, archive.ArchiveRequest(folders=[f"artifacts/{day_person}"])
        )
        assert removed["documents_total"] == 2  # two README.md files
        remaining = [a["id"] for a in artifacts.list_artifacts(context)["artifacts"]]
        assert remaining == [other["id"]]
    finally:
        engine.close()

    # The same folder removal over HTTP and MCP, on an ordinary source.
    from pheasant.mcp_server.tools import PheasantTools

    app = create_app(config=loaded_config)
    client = TestClient(app)
    source = next(s for s in loaded_config.sources if s.name == "architecture-notes")
    engine = SyncEngine(loaded_config)
    engine.sync_source(source.name, "full")
    engine.close()
    files = [
        str(r["relative_path"])
        for r in app.state.state.rows(
            "SELECT relative_path FROM artifacts WHERE source_id=?", (source.name,)
        )
    ]
    assert files
    first = files[0]
    gone = client.post("/documents/archive", json={"folders": [f"{source.name}/{first}"]}).json()
    assert gone["documents_total"] == 1
    back = PheasantTools(loaded_config).reinstate_documents(
        loaded_config.pheasant.name, folders=[f"{source.name}/{first}"]
    )
    assert back["documents"][0]["path"] == first


def test_each_source_runs_on_its_own_cadence_or_the_regions(loaded_config) -> None:
    from pheasant.services import sync_pipelines

    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    ago = lambda seconds: (now - timedelta(seconds=seconds)).isoformat()  # noqa: E731
    hourly_by_default = _source("docs")
    daily = _source("wiki", min_interval_seconds=86400)
    state = _State({"docs": ago(1200), "wiki": ago(7200)}, [])
    due = lambda default: [  # noqa: E731
        s.name
        for s in pipeline.beat_sources(
            [hourly_by_default, daily], state, now=now, default_interval=default
        )
    ]
    assert due(None) == ["docs"]  # every beat for docs; wiki waits for its day
    assert due(3600) == []  # docs ran 20 minutes ago: not due hourly
    assert pipeline.interval_of(daily, 3600) == 86400

    app = create_app(config=loaded_config)
    client = TestClient(app)
    tools = PheasantTools(loaded_config)
    kb = loaded_config.pheasant.name
    listed = client.get("/sync/pipelines").json()
    assert listed["defaults"]["default_source_interval_seconds"] is None
    assert 3600 in listed["defaults"]["choices"]
    assert (
        client.put(
            "/sync/pipelines/defaults", json={"default_source_interval_seconds": 3600}
        ).json()["default_source_interval_seconds"]
        == 3600
    )
    name = listed["pipelines"][0]["name"]
    row = sync_pipelines.pipeline(_context(app), name)
    assert (
        row["schedule"]["interval_seconds"] == 3600
        and row["schedule"]["interval_from"] == "region default"
    )
    changed = client.patch(f"/sync/pipelines/{name}", json={"min_interval_seconds": 86400}).json()
    assert changed["settings"]["min_interval_seconds"] == 86400
    assert changed["schedule"]["interval_from"] == "source"
    tools.configure_sync_defaults(kb, None)
    assert (
        client.get("/sync/pipelines").json()["defaults"]["default_source_interval_seconds"] is None
    )
    bad = client.put("/sync/pipelines/defaults", json={"default_source_interval_seconds": -5})
    assert bad.status_code == 422
