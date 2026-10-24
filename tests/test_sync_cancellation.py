"""Cancelling a running sync: what stops, what is kept, and who may ask.

The property that matters most is the idempotency one: a sync cancelled
part-way, followed by an incremental sync, ends in exactly the state a single
full sync of the same corpus produces. Everything the cancelled pass committed
is kept (the incremental skips it by checksum), and nothing it never reached is
recorded as if it had been.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.job_snapshots import sync_job_status
from pheasant.mcp_server.tools import PheasantTools
from pheasant.services import ServiceContext, SourceNotFound, sync_control
from pheasant.services.errors import SyncNotRunning
from pheasant.sync import cancellation
from pheasant.sync.cancellation import SyncRun, request_cancel, running
from pheasant.sync.connectors import ConnectorItem
from pheasant.sync.engine import SyncEngine
from pheasant.sync.web_connector import WebCollectionConnector

FILES = 12
CANCEL_AT = 4


@pytest.fixture(autouse=True)
def _check_every_item(monkeypatch: pytest.MonkeyPatch) -> None:
    # The production throttle is a second; a test corpus syncs in less.
    monkeypatch.setattr(cancellation, "CHECK_INTERVAL_SECONDS", 0.0)


def _corpus(root: Path) -> Path:
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    for index in range(FILES):
        (corpus / f"note-{index:02d}.md").write_text(
            f"# Note {index}\n\nThe word marker{index} appears in note {index}.\n",
            encoding="utf-8",
        )
    return corpus


def _config(root: Path, corpus: Path, *, workers: int = 1) -> PheasantConfig:
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "cancel",
                "state_path": str(root / "state"),
                "workspace_root": str(root),
                "exports_path": str(root / "exports"),
            },
            "sources": [
                {
                    "name": "docs",
                    "type": "document_folder",
                    "path": str(corpus),
                    "include": ["**/*.md"],
                }
            ],
        }
    )
    config.sync.concurrency.max_parallel_files = workers
    return config


def _cancel_after(engine: SyncEngine, committed: int):
    """A progress hook that asks for a cancel once ``committed`` items landed."""

    asked: list[dict] = []

    def hook(phase: str, current: int, total: int | None, detail: str) -> None:
        if phase == "committing" and current >= committed and not asked:
            accepted = request_cancel(engine.state, "docs", requested_by="test", reason="enough")
            assert accepted is not None, "the running sync did not accept the cancel"
            asked.append(accepted)

    return hook, asked


def _indexed_state(engine: SyncEngine) -> dict[str, object]:
    return {
        "artifacts": sorted(
            (str(r["relative_path"]), str(r["sha256"]))
            for r in engine.state.rows("SELECT relative_path, sha256 FROM artifacts")
        ),
        "chunks": sorted(str(r["id"]) for r in engine.state.rows("SELECT id FROM chunks")),
        "nodes": sorted(str(node) for node in engine.graph_builder.graph.nodes),
    }


@pytest.mark.parametrize("workers", [1, 4], ids=["one-worker", "look-ahead"])
def test_a_cancelled_sync_keeps_what_it_committed_and_the_next_pass_completes_it(
    tmp_path: Path, workers: int
) -> None:
    corpus = _corpus(tmp_path)
    engine = SyncEngine(_config(tmp_path / "cancelled", corpus, workers=workers))
    try:
        hook, asked = _cancel_after(engine, CANCEL_AT)
        result = engine.sync_source("docs", "incremental", on_progress=hook)

        assert asked and asked[0]["first_request"] is True
        assert result.status == "cancelled"
        assert result.details["cancelled"]["requested_by"] == "test"
        assert result.details["cancelled"]["reason"] == "enough"
        kept = len(engine.state.rows("SELECT id FROM artifacts WHERE source_id='docs'"))
        assert CANCEL_AT <= kept < FILES, "the sync neither stopped nor kept its work"
        assert result.indexed_artifacts == kept
        # Nothing that claims the pass was complete was recorded.
        assert engine.state.get_source("docs")["last_status"] == "cancelled"
        assert engine.state.get_fingerprint("source:docs") is None
        events = engine.state.rows(
            "SELECT event_type FROM sync_events WHERE source_id='docs' ORDER BY rowid"
        )
        assert [str(e["event_type"]) for e in events] == ["sync.cancelled"]
        # And the run left nothing behind to cancel the next one.
        assert running(engine.state, "docs") is None

        finished = engine.sync_source("docs", "incremental")
        assert finished.status == "healthy"
        assert finished.indexed_artifacts == FILES - kept, "committed work was redone"
        after_cancel = _indexed_state(engine)
    finally:
        engine.close()

    fresh = SyncEngine(_config(tmp_path / "fresh", corpus, workers=workers))
    try:
        assert fresh.sync_source("docs", "full").indexed_artifacts == FILES
        assert after_cancel == _indexed_state(fresh)
    finally:
        fresh.close()


def test_a_cancelled_full_sync_is_completed_by_an_incremental_one(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    engine = SyncEngine(_config(tmp_path / "cancelled", corpus))
    try:
        hook, _ = _cancel_after(engine, CANCEL_AT)
        assert engine.sync_source("docs", "full", on_progress=hook).status == "cancelled"
        engine.sync_source("docs", "incremental")
        after_cancel = _indexed_state(engine)
    finally:
        engine.close()

    fresh = SyncEngine(_config(tmp_path / "fresh", corpus))
    try:
        fresh.sync_source("docs", "full")
        assert after_cancel == _indexed_state(fresh)
    finally:
        fresh.close()


def test_a_cancel_only_reaches_the_run_it_was_made_for(tmp_path: Path) -> None:
    engine = SyncEngine(_config(tmp_path, _corpus(tmp_path)))
    try:
        state = engine.state
        assert request_cancel(state, "docs", requested_by="early") is None, (
            "a cancel with nothing running must not be stored for the next sync"
        )
        with SyncRun(state, "docs") as first:
            assert running(state, "docs")["run_id"] == first.run_id
            accepted = request_cancel(state, "docs", requested_by="alice", reason="wrong list")
            again = request_cancel(state, "docs", requested_by="bob")
            assert accepted["first_request"] is True and again["first_request"] is False
            assert again["cancel_requested_by"] == "alice", "the first request is the record"
            with pytest.raises(cancellation.SyncCancelled, match="alice: wrong list"):
                first.checkpoint()
        assert running(state, "docs") is None

        # A run that died without clearing its row is not running.
        with SyncRun(state, "docs") as orphan:
            old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
            state.execute(
                "UPDATE sync_runs SET heartbeat_at=? WHERE run_id=?", (old, orphan.run_id)
            )
            assert request_cancel(state, "docs", requested_by="late") is None

        # A new run replaces the row, so nothing an old run was asked carries over.
        with SyncRun(state, "docs") as later:
            later.checkpoint()
    finally:
        engine.close()


def test_a_cancelled_web_pass_keeps_only_the_validators_of_pages_it_finished(
    tmp_path: Path,
) -> None:
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "web",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [
                {
                    "name": "site",
                    "type": "web_collection",
                    "path": "/unused",
                    "urls": [f"https://example.test/{n}" for n in ("a", "b", "c")],
                    "connector": {"allow_experimental": True},
                }
            ],
        }
    )
    engine = SyncEngine(config)
    try:
        connector = WebCollectionConnector(config.sources[0], engine.state)
        items = [
            ConnectorItem(
                identity=f"web:site:{n}",
                relative_path=f"example.test/{n}",
                uri=f"https://example.test/{n}",
            )
            for n in ("a", "b", "c")
        ]
        connector._previous_cursor = {"validators": {"https://example.test/c": {"etag": "old-c"}}}
        # `a` was finished; `b` was read ahead and never committed; `c` was
        # never reached and keeps what the previous checkpoint knew.
        connector._seen_validators = {
            "https://example.test/a": {"etag": "new-a"},
            "https://example.test/b": {"etag": "new-b"},
        }
        cursor, high_watermark = connector.partial_checkpoint_from_items(items, {"web:site:a"})
        assert cursor["validators"] == {
            "https://example.test/a": {"etag": "new-a"},
            "https://example.test/c": {"etag": "old-c"},
        }
        assert high_watermark["item_count"] == 3
        assert connector._seen_validators["https://example.test/b"] == {"etag": "new-b"}
    finally:
        engine.close()


def test_connectors_keep_their_checkpoint_after_a_cancel_unless_they_opt_in(
    tmp_path: Path,
) -> None:
    engine = SyncEngine(_config(tmp_path, _corpus(tmp_path)))
    try:
        from pheasant.sync.connectors import connector_for_source

        connector = connector_for_source(engine.config.sources[0], engine.state)
        assert connector.partial_checkpoint_from_items([], set()) is None
    finally:
        engine.close()


def test_a_cancelled_job_says_cancelled_and_a_failure_outranks_it() -> None:
    assert sync_job_status(["healthy"]) == "succeeded"
    assert sync_job_status(["healthy", "cancelled"]) == "cancelled"
    assert sync_job_status(["cancelled", "failed"]) == "failed"


# ---------------------------------------------------------------------------
# The operation, on both surfaces
# ---------------------------------------------------------------------------


@pytest.fixture
def region(tmp_path: Path) -> dict:
    config = _config(tmp_path, _corpus(tmp_path))
    tools = PheasantTools(config)
    client = TestClient(create_app(config=_config(tmp_path, tmp_path / "corpus")))
    return {"tools": tools, "client": client, "kb": config.knowledge_base_id}


def test_both_surfaces_refuse_a_cancel_with_nothing_running_in_the_same_words(
    region: dict,
) -> None:
    with pytest.raises(SyncNotRunning) as refused:
        region["tools"].cancel_sync(region["kb"], "docs")
    response = region["client"].post("/sources/docs/cancel")
    assert response.status_code == 409
    assert response.json()["detail"] == str(refused.value)
    assert refused.value.code == "SYNC_NOT_RUNNING" and not refused.value.retryable


def test_both_surfaces_refuse_an_unknown_source_in_the_same_words(region: dict) -> None:
    with pytest.raises(SourceNotFound) as refused:
        region["tools"].cancel_sync(region["kb"], "nope")
    response = region["client"].post("/sources/nope/cancel", json={"reason": "x"})
    assert response.status_code == 404
    assert response.json()["detail"] == str(refused.value)


def test_both_surfaces_reach_a_running_sync(region: dict) -> None:
    tools = region["tools"]
    with SyncRun(tools.state, "docs") as run:
        over_mcp = tools.cancel_sync(region["kb"], "docs", reason="stop", requested_by="agent")
        over_http = region["client"].post("/sources/docs/cancel", json={"requested_by": "ui"})
        assert over_http.status_code == 200, over_http.text
        assert over_mcp["status"] == "cancel_requested" and over_mcp["first_request"] is True
        assert over_mcp["run"]["run_id"] == run.run_id
        assert over_http.json()["first_request"] is False
        assert over_http.json()["requested_by"] == "agent"
        with pytest.raises(cancellation.SyncCancelled):
            run.checkpoint()


def test_the_service_refuses_a_knowledge_base_this_region_does_not_hold(region: dict) -> None:
    tools = region["tools"]
    context = ServiceContext(config=tools.config, state=tools.state, searcher=None)
    with pytest.raises(ValueError, match="Unknown knowledge base"):
        sync_control.cancel_sync(
            context, sync_control.CancelSyncRequest(source_name="docs", knowledge_base="other")
        )


def test_a_queued_sync_of_a_disabled_source_is_skipped(tmp_path: Path) -> None:
    """Disabling a source whose sync was looping must stop it: the task an
    indexer restart redelivers is acknowledged and nothing is read."""

    from pheasant.sync.queue import IndexTask

    engine = SyncEngine(_config(tmp_path, _corpus(tmp_path)))
    try:
        engine.sync_source("docs", "validate_only")  # registers the source row
        engine.state.set_source_enabled("docs", False)
        result = engine.apply_index_task(IndexTask(id="idx-docs", source_id="docs"))
        assert result.status == "disabled"
        assert engine.state.rows("SELECT 1 FROM artifacts WHERE source_id='docs'") == []
        assert sync_job_status([result.status]) == "succeeded", "a skip is not a failure"

        engine.state.set_source_enabled("docs", True)
        enabled = engine.apply_index_task(IndexTask(id="idx-docs", source_id="docs"))
        assert enabled.status == "healthy" and enabled.indexed_artifacts == FILES
    finally:
        engine.close()
