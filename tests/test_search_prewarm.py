"""The search path's tables are read into cache at boot, before anyone's question.

`persistence/prewarm.py` and its caller `search/keep_warm.py`. On the Azure
region the graph arm's candidate fetch read up to 2,000 node rows off
NFS-backed storage for every new query (3-7 s) and the first search after a
wake took 11.9 s; these tests hold the mechanism on a real Postgres.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pheasant.config.schema import PheasantConfig
from pheasant.persistence import prewarm
from pheasant.persistence.state_store import StateStore
from pheasant.search import keep_warm
from pheasant.services import ServiceContext
from pheasant.services import region_status as region


def _state(tmp_path: Path) -> StateStore:
    state = StateStore.from_config(
        PheasantConfig.model_validate(
            {"pheasant": {"name": "prewarm", "defaults": "standalone", "state_path": str(tmp_path)}}
        )
    )
    state.migrate()
    return state


def test_the_search_tables_and_their_indexes_are_read_into_cache(tmp_path: Path) -> None:
    state = _state(tmp_path)
    try:
        outcome = prewarm.warm_search_relations(state)
        assert outcome["available"] is True, outcome
        # The relations with pages (a fresh region's indexes), and nothing failed.
        assert outcome["relations"] > 0
        assert outcome["failed"] == [] and outcome["skipped"] == []
        assert outcome["blocks"] > 0
        # A fresh region fits easily in half of shared_buffers.
        assert outcome["mode"] == "buffer"
        assert state.rows("SELECT 1 AS ok FROM pg_extension WHERE extname='pg_prewarm'")
    finally:
        state.close()


def test_the_read_is_bounded_and_skips_what_does_not_exist(tmp_path: Path) -> None:
    state = _state(tmp_path)
    try:
        nothing = prewarm.warm_search_relations(state, max_bytes=0)
        assert nothing["relations"] == 0 and nothing["blocks"] == 0
        assert nothing["skipped"], "nothing was over the bound"
        absent = prewarm.warm_search_relations(state, relations=("no_such_table", "chunks"))
        assert absent["failed"] == [] and absent["relations"] >= 1
    finally:
        state.close()


def test_a_server_without_the_extension_is_left_alone() -> None:
    class Bare:
        statements: list[str] = []

        def rows(self, sql: str, _params: Any = ()) -> list:
            self.statements.append(sql)
            return []  # neither installed nor available

        def execute(self, sql: str, _params: Any = ()) -> None:  # pragma: no cover
            raise AssertionError("must not try to create an extension it does not have")

    bare = Bare()
    outcome = prewarm.warm_search_relations(bare)
    assert outcome == {"available": False, "reason": "pg_prewarm is not installed on this server"}
    assert not any("pg_prewarm(" in sql for sql in bare.statements)


def test_a_failure_while_planning_never_raises() -> None:
    class Broken:
        def rows(self, sql: str, _params: Any = ()) -> list:
            raise RuntimeError("database went away")

    outcome = prewarm.warm_search_relations(Broken())
    assert outcome["available"] is False and "went away" in outcome["reason"]


def test_a_prewarm_running_anywhere_shows_the_database_as_warming(tmp_path: Path) -> None:
    """The status reads `pg_stat_activity`, so any replica's prewarm counts."""

    state = _state(tmp_path)
    other = _state(tmp_path)
    context = ServiceContext(config=None, state=state, searcher=None)
    try:
        assert region._database(context, elapsed=10.0)["state"] == "ready"
        running = threading.Thread(
            target=other.rows, args=(f"SELECT /* {prewarm.MARKER} */ pg_sleep(4)",), daemon=True
        )
        running.start()
        deadline = time.monotonic() + 3
        seen = None
        while time.monotonic() < deadline:
            seen = region._database(context, elapsed=10.0)
            if seen["state"] == "warming":
                break
            time.sleep(0.1)
        assert seen is not None and seen["state"] == "warming", seen
        running.join(10)
        assert region._database(context, elapsed=10.0)["state"] == "ready"
    finally:
        other.close()
        state.close()


def test_keep_warm_warms_the_tables_and_searches_before_its_first_interval(
    monkeypatch: Any,
) -> None:
    order: list[str] = []
    searched = threading.Event()

    class Search:
        store = SimpleNamespace(state=object())
        vector = None
        usage_tracking = stage_sample_rate = observed = True

        def search_context(self, *args: Any, **kwargs: Any) -> dict:
            order.append("search")
            searched.set()
            return {}

    monkeypatch.setattr(
        prewarm, "warm_search_relations", lambda state: order.append("prewarm") or {}
    )
    monkeypatch.setattr(keep_warm.corpus_shape, "vocabulary", lambda *a, **k: None)
    stop = threading.Event()
    thread = keep_warm.start(Search(), "kb", interval=3600.0, stop=stop)
    assert searched.wait(5), "the canned search waited for the first interval"
    assert order[:2] == ["prewarm", "search"]
    stop.set()
    thread.join(5)
    assert not thread.is_alive(), "the keep-warm thread outlived its stop"


def test_building_an_api_app_runs_no_search_it_was_not_asked_for(
    tmp_path: Path, monkeypatch: Any, nats_queue_settings: dict[str, Any]
) -> None:
    """Keep-warm starts with the served lifespan, not in `create_app`: CI
    caught its canned search racing a test that records every search."""

    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    started: list[str] = []
    monkeypatch.setattr(keep_warm, "start", lambda *a, **k: started.append("start"))
    monkeypatch.setenv("PHEASANT_API_TOKEN", "t")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "warm-app",
                "defaults": "standalone",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "security": {"api_auth": {"enabled": True, "token_env": "PHEASANT_API_TOKEN"}},
            # An api publishes syncs, so it refuses to start without a queue.
            "sync": {"queue": nats_queue_settings},
        }
    )
    app = create_app(config, role="api")
    try:
        assert started == [], "create_app started keep-warm"
        with TestClient(app):
            assert started == ["start"], "a served api did not start keep-warm"
    finally:
        app.state.engine.close()
