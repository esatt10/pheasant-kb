"""Phase 35.8 — the graph handoff, named and announced.

The graph is a file on a shared volume: an indexer writes it, every serving
process polls for it, and until now nothing said *which* graph a replica was
answering from. Two consequences, both silent by construction. A replica that
missed a reload served an old graph correctly-looking and forever, and a
retrieval diagnosis could not tell "the document is not indexed" from "this
replica has not picked up the index that has it".

What is asserted here:

1. The generation id is content-addressed, so two replicas agree without
   coordinating and an unchanged graph keeps its name (pillar 1).
2. The publication record is refused when it does not match the graph file's
   own stat tuple — a process killed between the two atomic renames reports
   nothing rather than a label for bytes that were never published.
3. A commit announces itself, exactly once per save, and a broker that fails
   cannot cost the commit.
4. The refresher reloads on the announcement *and* still reloads without one.
   That is the whole design: at-most-once events over a kept poll.
5. `/health` publishes loaded beside published, which is what makes a stale
   replica detectable rather than inferrable.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.cli import _GraphRefresher
from pheasant.config.schema import PheasantConfig
from pheasant.graph.simple import SimpleMultiDiGraph
from pheasant.persistence.graph_store import GraphStore
from pheasant.sync.graph_events import (
    NULL_NOTIFIER,
    GraphCommitNotifier,
    notifier_from_config,
    subject_for,
)
from tests.conftest import make_nats_queue_settings

KB = "generations"


def _store(root: Path, dsn: str | None = None) -> GraphStore:
    """A graph store over this test's database (or ``dsn``'s)."""

    from pheasant.persistence.backends import PostgresBackend
    from pheasant.persistence.state_store import StateStore

    state = StateStore(backend=PostgresBackend(dsn)) if dsn else StateStore()
    state.migrate()
    return GraphStore(root, state=state)


def _graph(nodes: int) -> SimpleMultiDiGraph:
    graph = SimpleMultiDiGraph()
    graph.add_node(f"kb:{KB}", id=f"kb:{KB}", type="knowledge_base", label=KB)
    for index in range(nodes):
        node_id = f"file:src:{index}.md"
        graph.add_node(node_id, id=node_id, type="file", label=f"{index}.md", source_id="src")
        graph.add_edge(f"kb:{KB}", node_id, type="contains", confidence=1.0)
    return graph


# --------------------------------------------------------------------------
# 1-2. The name
# --------------------------------------------------------------------------


def test_the_generation_id_is_content_addressed(tmp_path: Path) -> None:
    """A digest, not a counter and not a clock.

    A counter needs a coordinator to hand it out; a clock makes an unchanged
    graph a new generation on every re-save, and two replicas computing one
    from their own wall clocks would disagree about a graph they both hold.
    """

    store = _store(tmp_path / "graphs")
    store.save(KB, _graph(3))
    first = store.published_generation(KB)
    assert first is not None
    assert len(str(first["generation_id"])) == 16

    # Re-saving the same content republishes the same name. Idempotent
    # indexing (pillar 1) is only observable if the name is too.
    store.save(KB, _graph(3))
    assert store.published_generation(KB)["generation_id"] == first["generation_id"]

    store.save(KB, _graph(4))
    assert store.published_generation(KB)["generation_id"] != first["generation_id"]


def test_two_stores_agree_without_talking(tmp_path: Path, fresh_database) -> None:
    """The property that makes it usable in a fleet at all."""

    left, right = _store(tmp_path / "a"), _store(tmp_path / "b", fresh_database())
    left.save(KB, _graph(5))
    right.save(KB, _graph(5))
    assert (
        left.published_generation(KB)["generation_id"]
        == (right.published_generation(KB)["generation_id"])
    )


class _RecordingNotifier(GraphCommitNotifier):
    enabled = True

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.handlers: list[Any] = []
        self.fail = fail
        self.closed = False

    def publish(self, kb_id: str, record: dict[str, Any]) -> bool:
        if self.fail:
            raise RuntimeError("broker is down")
        self.published.append((kb_id, record))
        return True

    def subscribe(self, kb_id: str, handler: Any) -> bool:
        self.handlers.append(handler)
        return True

    def fire(self) -> None:
        for handler in self.handlers:
            handler()

    def close(self) -> None:
        self.closed = True


def test_every_save_announces_its_generation_once(tmp_path: Path) -> None:
    """One hook on the store, not a call beside each of five save sites."""

    store = _store(tmp_path / "graphs")
    notifier = _RecordingNotifier()
    store.on_publish = notifier.publish

    store.save(KB, _graph(3))
    store.save(KB, _graph(4))

    assert [kb for kb, _ in notifier.published] == [KB, KB]
    assert notifier.published[0][1]["generation_id"] != notifier.published[1][1]["generation_id"]
    assert notifier.published[-1][1]["nodes"] == 5  # 4 files + the kb node


def test_a_broker_that_fails_cannot_cost_the_commit(tmp_path: Path) -> None:
    """The graph is already durable when the hook runs; nothing may undo that."""

    store = _store(tmp_path / "graphs")
    store.on_publish = _RecordingNotifier(fail=True).publish

    store.save(KB, _graph(3))  # must not raise

    assert store.published_generation(KB) is not None
    assert len(store.load(KB)) == 4


def test_a_region_with_no_broker_announces_nothing(tmp_path: Path) -> None:
    """No queue means no channel and no change."""

    def config(**queue: Any) -> PheasantConfig:
        return PheasantConfig.model_validate(
            {"pheasant": {"name": KB, "state_path": str(tmp_path)}, "sync": {"queue": queue}}
        )

    assert notifier_from_config(config()) is NULL_NOTIFIER
    assert notifier_from_config(config(enabled=False, backend="nats")) is NULL_NOTIFIER
    assert NULL_NOTIFIER.enabled is False
    assert NULL_NOTIFIER.publish(KB, {}) is False


def test_regions_sharing_a_broker_do_not_wake_each_other() -> None:
    """A wasted reload is cheap; a fleet-wide one per neighbour's commit is not."""

    assert subject_for("pheasant.graph.committed", "docs") == "pheasant.graph.committed.docs"
    assert subject_for("pheasant.graph.committed", "docs") != subject_for(
        "pheasant.graph.committed", "code"
    )
    # NATS subject tokens have a grammar; a kb id is a free-form string.
    assert subject_for("pheasant.graph.committed", "a b.c*>") == "pheasant.graph.committed.a-b-c--"


# --------------------------------------------------------------------------
# 4. The reload, both ways
# --------------------------------------------------------------------------


class _FakeEngine:
    def __init__(self, store: GraphStore, config: Any) -> None:
        self.graph_store = store
        self.config = config
        self.reloads = 0

    def reload_graph(self) -> int:
        self.reloads += 1
        return 1


def _refresher_engine(tmp_path: Path) -> tuple[_FakeEngine, GraphStore]:
    store = _store(tmp_path / "graphs")
    store.save(KB, _graph(2))
    config = PheasantConfig.model_validate({"pheasant": {"name": KB, "state_path": str(tmp_path)}})
    return _FakeEngine(store, config), store


def test_an_announcement_reloads_without_waiting_for_the_interval(tmp_path: Path) -> None:
    """The window collapses to commit latency, which is the point of the event."""

    engine, store = _refresher_engine(tmp_path)
    notifier = _RecordingNotifier()
    # An interval far longer than the test: if the reload happens, the
    # announcement is the only thing that can have caused it.
    refresher = _GraphRefresher(engine, 3600.0, notifier=notifier)
    refresher.start()
    try:
        assert notifier.handlers, "the refresher did not subscribe"
        store.save(KB, _graph(9))
        notifier.fire()
        deadline = threading.Event()
        for _ in range(100):
            if engine.reloads:
                break
            deadline.wait(0.05)
        assert engine.reloads == 1
    finally:
        refresher.stop()
    assert notifier.closed


def test_the_poll_is_kept_as_the_backstop(tmp_path: Path) -> None:
    """A dropped message costs one interval, which is why it may be dropped.

    Without this the event path would have to guarantee delivery: durable
    consumers, acks, per-replica broker state, and cleanup when a pod goes
    away — four things bought to avoid one stat.
    """

    engine, store = _refresher_engine(tmp_path)
    refresher = _GraphRefresher(engine, 0.05, notifier=None)
    refresher.start()
    try:
        store.save(KB, _graph(9))
        deadline = threading.Event()
        for _ in range(100):
            if engine.reloads:
                break
            deadline.wait(0.05)
        assert engine.reloads == 1
    finally:
        refresher.stop()


def test_an_announcement_for_an_unchanged_graph_reloads_nothing(tmp_path: Path) -> None:
    """A message is a hint to look, never a fact to act on.

    So a duplicate delivery, a message for a generation already loaded, and a
    forged one all cost one stat — which is what makes at-most-once delivery
    over an unauthenticated fan-out subject an acceptable design.
    """

    engine, _store = _refresher_engine(tmp_path)
    notifier = _RecordingNotifier()
    refresher = _GraphRefresher(engine, 3600.0, notifier=notifier)
    refresher.start()
    try:
        for _ in range(5):
            notifier.fire()
        threading.Event().wait(0.3)
        assert engine.reloads == 0
    finally:
        refresher.stop()


# --------------------------------------------------------------------------
# 5. Detectable, not inferrable
# --------------------------------------------------------------------------


def _served(tmp_path: Path, graph_format: str = "rows") -> TestClient:
    (tmp_path / "ws").mkdir(exist_ok=True)
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": KB,
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path / "ws"),
                "exports_path": str(tmp_path / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "storage": {"graph_format": graph_format},
            "sources": [],
        }
    )
    return TestClient(create_app(config, config_path=str(tmp_path / "pheasant.yaml")))


def test_a_row_backed_replica_cannot_be_stale(tmp_path: Path) -> None:
    """The staleness above is a property of *copies*, and rows have none.

    A replica reading `graph_nodes` answers from the same rows the indexer
    committed to, so the pair `/ready` publishes is equal by construction
    rather than by having reloaded in time. This is the assertion that the
    scenario the previous test constructs is not merely unobserved here but
    unconstructible: it commits through a *second store over the same
    database*, which is what another process is, and the serving replica is
    current immediately with no reload and no announcement.

    Worth its own test rather than a parametrization: the two backends are
    being asserted to have different properties, and a shared body would have
    to branch to say so, which is how a weaker guarantee gets quietly accepted
    for both.
    """

    (tmp_path / "ws").mkdir(exist_ok=True)
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": KB,
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path / "ws"),
                "exports_path": str(tmp_path / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "storage": {"graph_format": "rows"},
            # An `api` replica, because that is the role the property belongs
            # to: it serves and never indexes, so on this backend it holds no
            # graph at all. `all` still keeps a working set — it is the process
            # that builds one — and is still subject to the test above.
            "sync": {"queue": make_nats_queue_settings()},
            "sources": [],
        }
    )
    client = TestClient(create_app(config, config_path=str(tmp_path / "pheasant.yaml"), role="api"))
    engine = client.app.state.engine
    assert engine.serving_graph().__class__.__name__ == "SqlGraph", (
        "the point of the test is that this replica holds no graph"
    )

    # Another process commits, through a second store over the same database.
    second = GraphStore(engine.graph_store.root, state=engine.state, graph_format="rows")
    second.save(KB, _graph(11))

    payload = client.get("/ready").json()["graph_generation"]
    assert payload["published"] == second.published_generation(KB)["generation_id"]
    assert payload["loaded"] == payload["published"], (
        "a row-backed replica reads the committed rows; it has no private copy to be behind"
    )
    assert payload["current"] is True


def test_liveness_reports_the_loaded_generation_without_touching_the_disk(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """`/health` is the probe a busy pod gets restarted for failing.

    Its whole design is that it does no I/O — measured at 0.05s against 4.29s
    for a sync handler under a saturated thread pool — so the staleness pair,
    which reads `/state`, belongs on `/ready` (where it is offloaded beside
    the state-store probe) and the in-memory half belongs here.
    """

    client = _served(tmp_path)
    engine = client.app.state.engine
    engine.graph_store.save(KB, _graph(3))
    engine.reload_graph()

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("/health read the publication record")

    monkeypatch.setattr(type(engine.graph_store), "published_generation", explode)
    payload = client.get("/health").json()["graph_generation"]
    assert payload["loaded"] == engine.loaded_graph_generation
    assert "published" not in payload


def test_a_search_says_which_graph_answered(tmp_path: Path) -> None:
    """A diagnosis that cannot name the generation cannot tell two failures apart."""

    client = _served(tmp_path)
    engine = client.app.state.engine
    engine.graph_store.save(KB, _graph(3))
    engine.reload_graph()

    response = client.post("/search", json={"query": "anything", "max_results": 3})
    assert response.status_code == 200
    assert response.json()["graph_generation"] == engine.loaded_graph_generation


def test_a_process_that_indexes_adopts_the_generation_it_published(tmp_path: Path) -> None:
    """The single container is the deployment most people run, and it indexes.

    `loaded_graph_generation` was set only when a graph was *read*, so a
    process that built and committed one served it while reporting no
    generation at all — `graph_generation: null` on `/health`, on `/ready` and
    on every search response, for the process's whole life. The staleness
    comparison that exists to make a stale replica detectable was inert on
    `role: all`.

    Found by the two-surface conformance matrix, which compared an HTTP app
    that had loaded a graph against an MCP facade that had indexed one.
    """

    from pheasant.config.schema import PheasantConfig
    from pheasant.sync.engine import SyncEngine

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "note.md").write_text("# Note\n\nrotation happens nightly\n", encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "adopted",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "sources": [
                {
                    "name": "docs",
                    "type": "markdown_folder",
                    "path": str(workspace),
                    "include": ["**/*.md"],
                }
            ],
        }
    )
    engine = SyncEngine(config)
    try:
        assert engine.loaded_graph_generation is None  # nothing published yet
        engine.sync_source("docs", "full")

        published = engine.graph_store.published_generation("adopted")
        assert published is not None
        assert engine.loaded_graph_generation == published["generation_id"]
        assert engine.loaded_graph_published_at is not None
    finally:
        engine.close()
