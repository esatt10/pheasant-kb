"""Phase 35.6a — process roles.

The property under test is narrow: `all` is unchanged, and no other role
indexes when it should not. Everything else — the startup refusal, the 409,
the drain loop — exists to make that safe to rely on.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig, SourceConfig, SourceType
from pheasant.deployment.roles import (
    POLICIES,
    Role,
    RoleConfigurationError,
    describe,
    resolve_role,
    validate_role,
)
from pheasant.sync.queue import DONE, PENDING, LocalQueue


def _config(
    tmp_path: Path,
    *,
    state_name: str = "state",
    sources: int = 2,
    queue: bool = False,
    role: str | None = None,
    observability: bool = False,
) -> PheasantConfig:
    workspace = tmp_path / f"workspace-{state_name}"
    entries = []
    for index in range(sources):
        folder = workspace / f"src{index}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "note.md").write_text(
            f"# Note {index}\n\nThe deployment gateway rotates credentials nightly.\n",
            encoding="utf-8",
        )
        entries.append(
            {
                "name": f"src{index}",
                "type": "markdown_folder",
                "path": str(folder),
                "include": ["**/*.md"],
            }
        )
    payload: dict[str, Any] = {
        "pheasant": {
            "name": "roles",
            "state_path": str(tmp_path / state_name),
            "workspace_root": str(workspace),
            "exports_path": str(tmp_path / f"{state_name}-exports"),
        },
        "storage": {"graph_snapshots": False},
        "sync": {"concurrency": {"lock_timeout_seconds": 5}},
        "sources": entries,
    }
    if queue:
        payload["sync"]["queue"] = {"enabled": True}
    if observability:
        payload["observability"] = {"interactions": {"enabled": True, "queue": {"enabled": True}}}
    # Loopback, because that is what an in-process test actually is. A role
    # other than `all` refuses an unauthenticated bind other machines can
    # reach (see the exposure tests below); saying 0.0.0.0 here would be
    # claiming a fleet posture that none of these tests deploys.
    payload["server"] = {"host": "127.0.0.1"}
    if role is not None:
        payload["server"]["role"] = role
    return PheasantConfig.model_validate(payload)


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_the_default_role_is_all_and_is_todays_behavior(tmp_path: Path) -> None:
    policy = resolve_role(_config(tmp_path))

    assert policy.role is Role.ALL
    assert policy.is_default
    assert policy.runs_watcher and policy.runs_scheduler and policy.indexes_locally
    # `all` must NOT drain, or switching the queue on for crash resumption
    # would quietly turn a single container into a fleet member.
    assert policy.drains_queue is False


def test_the_flag_beats_the_config(tmp_path: Path) -> None:
    config = _config(tmp_path, role="indexer")
    assert resolve_role(config).role is Role.INDEXER
    assert resolve_role(config, "worker").role is Role.WORKER


def test_an_unknown_role_raises_rather_than_defaulting(tmp_path: Path) -> None:
    """A typo in a Deployment's args must not silently produce a full pod."""

    with pytest.raises(RoleConfigurationError, match="Unknown role"):
        resolve_role(_config(tmp_path), "indexr")


@pytest.mark.parametrize("role", list(Role))
def test_every_role_has_a_policy_and_describes_itself(role: Role) -> None:
    policy = POLICIES[role]
    described = describe(policy)
    assert described["role"] == role.value
    assert set(described) == {
        "role",
        "watcher",
        "scheduler",
        "drains_queue",
        "drains_log_queue",
        "indexes_locally",
        "refreshes_graph",
    }


def test_only_the_indexer_drains_and_only_serving_roles_index() -> None:
    """The whole table, asserted rather than left to the docstring."""

    assert [role.value for role in Role if POLICIES[role].drains_queue] == ["indexer"]
    assert sorted(role.value for role in Role if POLICIES[role].indexes_locally) == [
        "all",
        "indexer",
    ]
    assert sorted(role.value for role in Role if POLICIES[role].runs_watcher) == ["all", "indexer"]
    # The log tier is its own drain. `all` is absent for the same reason it is
    # absent from `drains_queue`: a single container must behave identically
    # whether or not a queue exists, so it rolls its own logs inline on the
    # maintenance beat instead of growing a second worker.
    assert [role.value for role in Role if POLICIES[role].drains_log_queue] == ["logger"]
    # And the log tier does nothing else. If this ever grows a second True,
    # the tier has stopped being an isolated failure domain.
    logger_policy = POLICIES[Role.LOGGER]
    assert not any(
        (
            logger_policy.runs_watcher,
            logger_policy.runs_scheduler,
            logger_policy.drains_queue,
            logger_policy.indexes_locally,
            logger_policy.serves_ui,
            logger_policy.refreshes_graph,
        )
    )


def test_indexer_orchestration_has_one_leader_and_fails_over(tmp_path: Path) -> None:
    """Extra indexers are hot standbys, never duplicate schedulers."""

    from pheasant.cli import _OrchestrationSupervisor
    from pheasant.persistence.state_store import StateStore

    class Service:
        def __init__(self) -> None:
            self.running = False
            self.starts = 0

        def start(self) -> None:
            self.running = True
            self.starts += 1

        def stop(self) -> None:
            self.running = False

    path = tmp_path / "leadership.db"
    stores = [StateStore(path), StateStore(path)]
    for store in stores:
        store.migrate()
    service_sets = [[Service(), Service(), Service()] for _ in stores]
    promotions = [0, 0]

    def promoted(index: int) -> None:
        promotions[index] += 1

    supervisors = [
        _OrchestrationSupervisor(
            store,
            "kb",
            watcher=services[0],
            scheduler=services[1],
            drainer=services[2],
            on_promote=lambda index=index: promoted(index),
            promotion_lock=threading.Lock(),
            poll_interval=0.05,
        )
        for index, (store, services) in enumerate(zip(stores, service_sets, strict=True))
    ]

    def wait_until(predicate, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError("condition did not become true")

    try:
        for supervisor in supervisors:
            supervisor.start()
        wait_until(lambda: sum(item.leader for item in supervisors) == 1)
        wait_until(lambda: sum(promotions) == 1)
        leader = next(item for item in supervisors if item.leader)
        standby = next(item for item in supervisors if not item.leader)
        assert sum(service.running for services in service_sets for service in services) == 3

        leader.stop()
        wait_until(lambda: standby.leader)
        wait_until(lambda: sum(promotions) == 2)
        assert all(service.running for service in service_sets[supervisors.index(standby)])
    finally:
        for supervisor in supervisors:
            supervisor.stop()
        for store in stores:
            store.close()


# --------------------------------------------------------------------------
# The startup refusal
# --------------------------------------------------------------------------


def test_the_api_role_refuses_to_start_without_a_queue(tmp_path: Path) -> None:
    """Otherwise every sync request is accepted and never runs."""

    config = _config(tmp_path, role="api")
    with pytest.raises(RoleConfigurationError, match="sync.queue.enabled"):
        validate_role(resolve_role(config), config)

    with pytest.raises(RoleConfigurationError):
        create_app(config, config_path=str(tmp_path / "pheasant.yaml"), role="api")


def test_the_api_role_starts_with_a_queue(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="api-ok", role="api", queue=True)
    validate_role(resolve_role(config), config)
    client = TestClient(create_app(config, role="api"))
    assert client.get("/health").json()["role"] == "api"


@pytest.mark.parametrize("role", ["all", "indexer", "graph", "worker"])
def test_other_roles_need_no_queue(tmp_path: Path, role: str) -> None:
    """Only `api` depends on a queue: the rest can index or do nothing."""

    # No sources for the worker: it is the one role that refuses to hold a
    # source list at all, which the allow-list tests below cover directly.
    config = _config(
        tmp_path,
        state_name=f"noqueue-{role}",
        role=role,
        sources=0 if role == "worker" else 2,
    )
    validate_role(resolve_role(config), config)


# --------------------------------------------------------------------------
# What the role changes at runtime
# --------------------------------------------------------------------------


def test_health_and_ready_identify_the_pod(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="probes", role="indexer")
    client = TestClient(create_app(config, role="indexer"))

    health = client.get("/health").json()
    assert health["status"] == "ok" and health["role"] == "indexer"

    ready = client.get("/ready")
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ready"
    assert body["role"] == "indexer"
    assert body["drains_queue"] is True
    assert body["knowledge_base"] == config.knowledge_base_id


def test_indexer_coordinator_does_not_duplicate_the_persisted_graph(tmp_path: Path) -> None:
    from pheasant.sync.engine import SyncEngine

    config = _config(tmp_path, state_name="coordinator-graph", role="indexer")
    writer = SyncEngine(config)
    try:
        writer.sync_source("src0", "full")
        persisted_nodes = writer.graph_builder.graph.number_of_nodes()
    finally:
        writer.close()

    assert persisted_nodes > 1
    app = create_app(config, role="indexer")
    assert app.state.engine._loads_persisted_graph is False
    assert app.state.engine.graph_builder.graph.number_of_nodes() == 1
    assert app.state.engine.vectors is None
    assert app.state.engine.extractor is None


def test_ready_reports_503_when_the_state_store_is_unreachable(tmp_path: Path) -> None:
    """Readiness takes a pod out of the Service; liveness restarts it.

    A state store that has gone away is the first case, not the second, so
    `/ready` must actually check something rather than return a constant.
    """

    config = _config(tmp_path, state_name="unready")
    app = create_app(config)
    client = TestClient(app)
    assert client.get("/ready").status_code == 200

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database is gone")

    app.state.state.rows = broken  # type: ignore[method-assign]
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    # Liveness must stay green: restarting the pod does not bring a database
    # back, and a restart loop is worse than a pod that stops taking traffic.
    assert client.get("/health").status_code == 200


def test_the_api_role_publishes_a_sync_instead_of_running_it(tmp_path: Path) -> None:
    """The load-bearing cell of the table.

    Three api replicas that each indexed on request would put three processes
    on one source. Publishing is what makes the replica count free.
    """

    config = _config(tmp_path, state_name="api-publish", role="api", queue=True)
    client = TestClient(create_app(config, role="api"))

    response = client.post("/sync/src0", json={"mode": "full", "wait": False})
    assert response.status_code == 200
    body = response.json()
    # "queued", not "syncing": nothing is indexing yet, and there is no local
    # job to watch. The task id used to come back as `job_id`, so every caller
    # polled GET /jobs/<task id> and got a 404 — the registry is in-process and
    # the work belongs to an indexer in another pod.
    assert body["status"] == "queued"
    assert body["job_id"] is None
    assert body["queued_tasks"] and all(t.startswith("idx-") for t in body["queued_tasks"])
    assert client.get(f"/jobs/{body['queued_tasks'][0]}").status_code == 404

    engine = client.app.state.engine
    queue = LocalQueue(engine.state)
    assert queue.depth()[PENDING] == 1
    # Nothing was indexed here: the artifacts table is still empty.
    rows = engine.state.rows("SELECT COUNT(*) AS c FROM artifacts", ())
    assert int(rows[0]["c"]) == 0


def test_the_api_role_never_runs_startup_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured on-startup sources belong to the indexer tier, not the API."""

    called = threading.Event()

    def startup(_self: Any) -> list[Any]:
        called.set()
        return []

    monkeypatch.setattr("pheasant.sync.worker.WorkerBackedEngine.startup", startup)
    config = _config(tmp_path, state_name="api-startup", role="api", queue=True)

    with TestClient(create_app(config, role="api")):
        time.sleep(0.05)

    assert not called.is_set()


def test_an_orchestrated_indexer_defers_startup_to_leader_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = threading.Event()

    def startup(_self: Any) -> list[Any]:
        called.set()
        return []

    monkeypatch.setattr("pheasant.sync.worker.WorkerBackedEngine.startup", startup)
    config = _config(tmp_path, state_name="standby-startup", role="indexer", queue=True)
    app = create_app(config, role="indexer")
    app.state.orchestration = object()

    with TestClient(app):
        time.sleep(0.05)

    assert not called.is_set()


def test_durable_backlog_takes_priority_over_startup_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pheasant.cli import _durable_backlog_depth
    from pheasant.sync.queue import IndexTask, queue_from_config

    config = _config(tmp_path, state_name="backlog-first", role="indexer", queue=True)
    app = create_app(config, role="indexer")
    queue = queue_from_config(config, app.state.state)
    assert queue is not None
    queue.publish(IndexTask(id="operator-task", source_id="src0"))
    assert _durable_backlog_depth(config, app.state.state) == 1
    assert queue.depth()[PENDING] == 1
    queue.close()


def test_the_api_role_defers_repository_clone_to_the_indexer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API has read-only mounts; the queued indexer materializes URLs."""

    def must_not_fetch(_target: Any) -> None:
        raise AssertionError("the API role attempted to clone a repository")

    monkeypatch.setattr("pheasant.targets.fetch_target", must_not_fetch)
    config = _config(
        tmp_path,
        state_name="api-remote-source",
        sources=0,
        role="api",
        queue=True,
    )
    client = TestClient(create_app(config, role="api"))

    response = client.post(
        "/sources/quick-add",
        json={
            "target": "https://github.com/example/managed-repo",
            "sync_now": True,
            "wait": False,
        },
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    source = body["sources"][0]
    expected = Path(config.pheasant.workspace_root) / "sources" / "managed-repo"
    assert source["path"] == str(expected.resolve())
    assert source["repo"] == {
        "clone_url": "https://github.com/example/managed-repo",
        "clone_path": str(expected.resolve()),
        "clone_ref": None,
    }
    assert body["status"] == "registered"
    assert body["queued_tasks"]


def test_the_api_role_deduplicates_a_double_click(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="api-dedup", role="api", queue=True)
    client = TestClient(create_app(config, role="api"))

    first = client.post("/sync/src0", json={"mode": "full", "wait": False}).json()
    second = client.post("/sync/src0", json={"mode": "full", "wait": False}).json()

    # Compared on `queued_tasks`, not `job_id`: on this role `job_id` is None
    # for both calls, so the original assertion held whether or not the two
    # clicks deduplicated.
    assert first["queued_tasks"], "nothing was published"
    assert first["queued_tasks"] == second["queued_tasks"]
    assert LocalQueue(client.app.state.engine.state).depth()[PENDING] == 1


def test_api_source_removal_is_an_ordered_writer_task(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="api-remove", role="api", queue=True)
    client = TestClient(create_app(config, role="api"))

    response = client.delete("/sources/src0")

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["status"] == "removal_queued"
    assert body["queued_tasks"]
    row = client.app.state.state.get_source("src0")
    assert row is not None
    assert int(row["enabled"]) == 0
    queue = LocalQueue(client.app.state.state)
    task = queue.claim("test-control-writer")
    assert task is not None
    assert task.payload["operation"] == "delete_source"


def test_sync_all_includes_a_source_registered_only_in_state(tmp_path: Path) -> None:
    """Scheduler/API all-sync must not forget UI sources after restart."""

    from pheasant.registry.source_registry import SourceRegistry

    config = _config(
        tmp_path,
        state_name="api-runtime-all",
        sources=0,
        role="api",
        queue=True,
    )
    client = TestClient(create_app(config, role="api"))
    folder = Path(config.pheasant.workspace_root) / "runtime"
    folder.mkdir(parents=True)
    source = PheasantConfig.model_validate(
        {
            "sources": [
                {
                    "name": "runtime-docs",
                    "type": "markdown_folder",
                    "path": str(folder),
                    "include": ["**/*.md"],
                }
            ]
        }
    ).sources[0]
    SourceRegistry(config, client.app.state.engine.state).register_source(source)

    response = client.post("/sync", json={"mode": "incremental", "wait": False})

    assert response.status_code == 200
    assert response.json()["sources"] == ["runtime-docs"]
    assert LocalQueue(client.app.state.engine.state).depth()[PENDING] == 1


def test_the_api_role_refuses_a_blocking_sync_with_a_usable_message(tmp_path: Path) -> None:
    """409 and the fix, rather than lying or quietly changing the contract."""

    config = _config(tmp_path, state_name="api-block", role="api", queue=True)
    client = TestClient(create_app(config, role="api"))

    response = client.post("/sync/src0", json={"mode": "full", "wait": True})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "role 'api' does not index" in detail
    assert "wait=false" in detail


def test_the_all_role_still_indexes_in_process(tmp_path: Path) -> None:
    """Rule 7 in role form: the default path is untouched."""

    config = _config(tmp_path, state_name="all-index")
    client = TestClient(create_app(config))

    response = client.post("/sync/src0", json={"mode": "full", "wait": True})
    assert response.status_code == 200
    assert response.json()["indexed_artifacts"] == 1

    rows = client.app.state.engine.state.rows("SELECT COUNT(*) AS c FROM index_tasks", ())
    assert int(rows[0]["c"]) == 0, "the default role published instead of indexing"


# --------------------------------------------------------------------------
# The indexer's drain loop
# --------------------------------------------------------------------------


def _write_config_file(config: PheasantConfig, path: Path, *, role: str) -> Path:
    lines = [
        "pheasant:",
        f"  name: {config.pheasant.name}",
        f"  state_path: {config.pheasant.state_path}",
        f"  workspace_root: {config.pheasant.workspace_root}",
        f"  exports_path: {config.pheasant.exports_path}",
        "storage:",
        "  graph_snapshots: false",
        "server:",
        f"  role: {role}",
        "sync:",
        "  queue:",
        "    enabled: true",
        "sources:",
    ]
    for source in config.sources:
        lines += [
            f"  - name: {source.name}",
            "    type: markdown_folder",
            f"    path: {source.path}",
            "    include:",
            '      - "**/*.md"',
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_the_indexer_drains_what_an_api_replica_published(tmp_path: Path) -> None:
    """The hand-off, end to end, across two processes' worth of objects.

    An api replica publishes and indexes nothing; an indexer claims the task
    and the artifacts appear. This is the whole point of the role split, so it
    is tested as one flow rather than as two halves.
    """

    from pheasant.cli import _QueueDrainer

    config = _config(tmp_path, state_name="handoff", role="api", queue=True)
    config_path = _write_config_file(config, tmp_path / "pheasant.yaml", role="indexer")

    api = TestClient(create_app(config, role="api"))
    api.post("/sync/src0", json={"mode": "full", "wait": False})
    api.post("/sync/src1", json={"mode": "full", "wait": False})
    assert LocalQueue(api.app.state.engine.state).depth()[PENDING] == 2

    from pheasant.config.loader import load_config

    indexer_config = load_config(config_path)
    drainer = _QueueDrainer(indexer_config, str(config_path))
    drainer.start()
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            depth = LocalQueue(api.app.state.engine.state).depth()
            if depth[DONE] == 2:
                break
            time.sleep(0.5)
    finally:
        drainer.stop()

    depth = LocalQueue(api.app.state.engine.state).depth()
    assert depth[DONE] == 2, f"indexer did not drain the backlog: {depth}"
    rows = api.app.state.engine.state.rows(
        "SELECT source_id, COUNT(*) AS c FROM artifacts GROUP BY source_id ORDER BY source_id"
    )
    assert [(row["source_id"], int(row["c"])) for row in rows] == [("src0", 1), ("src1", 1)]


def test_the_drainer_does_not_claim_while_a_sync_coordinator_owns_the_lock(
    tmp_path: Path,
) -> None:
    from pheasant.cli import _QueueDrainer
    from pheasant.config.loader import load_config

    config = _config(tmp_path, state_name="locked-handoff", sources=1, queue=True)
    config_path = _write_config_file(config, tmp_path / "pheasant.yaml", role="indexer")
    api = TestClient(create_app(config, role="api"))
    api.post("/sync/src0", json={"mode": "full", "wait": False})

    sync_lock = threading.Lock()
    sync_lock.acquire()
    drainer = _QueueDrainer(load_config(config_path), str(config_path), sync_lock=sync_lock)
    drainer.start()
    try:
        time.sleep(1.0)
        assert LocalQueue(api.app.state.engine.state).depth()[PENDING] == 1
    finally:
        sync_lock.release()

    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if LocalQueue(api.app.state.engine.state).depth()[DONE] == 1:
                break
            time.sleep(0.25)
    finally:
        drainer.stop()
    assert LocalQueue(api.app.state.engine.state).depth()[DONE] == 1


def test_a_drainer_stops_promptly(tmp_path: Path) -> None:
    """SIGTERM must not wait out an idle poll, let alone a multi-hour index."""

    from pheasant.cli import _QueueDrainer

    config = _config(tmp_path, state_name="stop", sources=1, queue=True)
    config_path = _write_config_file(config, tmp_path / "pheasant.yaml", role="indexer")

    from pheasant.config.loader import load_config

    drainer = _QueueDrainer(load_config(config_path), str(config_path))
    drainer.start()
    time.sleep(1.0)
    started = time.monotonic()
    drainer.stop()
    assert time.monotonic() - started < 10, "stop() did not return promptly"


def test_no_drainer_is_built_for_a_role_that_does_not_drain(tmp_path: Path) -> None:
    from pheasant.cli import _queue_drainer

    config = _config(tmp_path, state_name="nodrain", queue=True)
    for role in ("all", "api", "worker"):
        assert _queue_drainer(config, "pheasant.yaml", resolve_role(config, role)) is None
    assert _queue_drainer(config, "pheasant.yaml", resolve_role(config, "indexer")) is not None


def test_queue_drainer_parent_never_loads_the_worker_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pheasant.cli import _QueueDrainer

    captured: dict[str, Any] = {}

    class Coordinator:
        state = object()

        @staticmethod
        def close() -> None:
            return None

    def fake_engine(_path: Path, **kwargs: Any) -> Coordinator:
        captured.update(kwargs)
        return Coordinator()

    monkeypatch.setattr("pheasant.cli._engine", fake_engine)
    monkeypatch.setattr("pheasant.sync.queue.queue_from_config", lambda *_args: None)
    drainer = _QueueDrainer(SimpleNamespace(), "pheasant.yaml")
    drainer._stop = threading.Event()

    drainer._run()

    assert captured == {
        "load_persisted_graph": False,
        "initialize_indexing_components": False,
    }


# --------------------------------------------------------------------------
# The api role's graph refresh
# --------------------------------------------------------------------------


def test_only_query_serving_roles_refresh_the_graph() -> None:
    """Legacy APIs and the graph service follow indexer snapshot commits."""

    assert [role.value for role in Role if POLICIES[role].refreshes_graph] == ["api", "graph"]


def test_an_api_replica_picks_up_a_graph_another_process_wrote(tmp_path: Path) -> None:
    """The gap that made a role split quietly wrong.

    A process loads the graph once at startup, and the only reload path runs
    after a sync *that process* performed. An api replica never indexes, so
    without this it would answer graph queries from whatever the graph was
    when its pod started — indefinitely, and silently, while text and vector
    search stayed current from the shared database.
    """

    from pheasant.cli import _GraphRefresher
    from pheasant.sync.engine import SyncEngine

    config = _config(tmp_path, state_name="refresh", sources=1, role="api", queue=True)

    # The "indexer": a separate engine over the same /state that really syncs.
    indexer = SyncEngine(config)
    try:
        indexer.sync_source("src0", "full")
    finally:
        indexer.close()

    # The "api replica": a second engine, started before more content exists.
    api = SyncEngine(config)
    try:
        refresher = _GraphRefresher(api, interval_seconds=1)
        before = api.graph_builder.graph.number_of_nodes()
        assert before > 0
        assert refresher.check_once() is False, "nothing changed, yet it reloaded"

        folder = Path(config.sources[0].path)
        (folder / "second.md").write_text(
            "# Second\n\nThe failover drill runs quarterly.\n", encoding="utf-8"
        )
        writer = SyncEngine(config)
        try:
            writer.sync_source("src0", "full")
        finally:
            writer.close()

        assert refresher.check_once() is True, "a newer graph on disk was not picked up"
        assert api.graph_builder.graph.number_of_nodes() > before
        # Idempotent: a second check with nothing new does not reload again.
        assert refresher.check_once() is False
    finally:
        api.close()


def test_no_refresher_is_built_for_a_role_that_indexes(tmp_path: Path) -> None:
    from pheasant.cli import _graph_refresher
    from pheasant.sync.engine import SyncEngine

    config = _config(tmp_path, state_name="norefresh", sources=1, queue=True)
    engine = SyncEngine(config)
    try:
        for role in ("all", "indexer", "worker"):
            assert _graph_refresher(config, engine, resolve_role(config, role)) is None
        assert _graph_refresher(config, engine, resolve_role(config, "api")) is not None
        assert _graph_refresher(config, engine, resolve_role(config, "graph")) is not None

        # And it is switchable off, for a deployment where the graph is not
        # shared between pods at all.
        config.server.api.graph_refresh_seconds = 0
        assert _graph_refresher(config, engine, resolve_role(config, "api")) is None
    finally:
        engine.close()


def test_serve_refuses_a_misconfigured_role_at_the_cli(tmp_path: Path, capsys: Any) -> None:
    from pheasant.cli import main

    config = _config(tmp_path, state_name="cli-role", sources=1)
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        "pheasant:\n"
        f"  name: {config.pheasant.name}\n"
        f"  state_path: {config.pheasant.state_path}\n"
        f"  workspace_root: {config.pheasant.workspace_root}\n"
        f"  exports_path: {config.pheasant.exports_path}\n",
        encoding="utf-8",
    )

    assert main(["serve", "--config", str(config_path), "--role", "api"]) == 1
    assert "sync.queue.enabled" in capsys.readouterr().out


def test_background_services_start_only_for_their_roles(tmp_path: Path, monkeypatch: Any) -> None:
    """`_serve_app` is where the table stops being a docstring.

    Asserted by intercepting uvicorn rather than by reading the code, so a
    future refactor that moves a `.start()` outside its `if` is caught.
    """

    import pheasant.cli as cli

    started: dict[str, list[str]] = {}

    class Recorder:
        def __init__(self, label: str, bucket: list[str]) -> None:
            self.label = label
            self.bucket = bucket

        def start(self) -> None:
            self.bucket.append(self.label)

        def stop(self) -> None:
            pass

    def run_for(role: str) -> list[str]:
        bucket: list[str] = []
        started[role] = bucket
        config = _config(
            tmp_path,
            state_name=f"svc-{role}",
            # A worker holds no source list -- `validate_role` refuses one,
            # because a worker is handed bytes rather than going to find them.
            sources=0 if role == "worker" else 1,
            queue=True,
            # `validate_role` refuses `logger` without it, which is the point
            # of that guard: a log tier with nothing to drain is a pod that
            # reports healthy and does nothing forever.
            observability=role == "logger",
        )
        monkeypatch.setattr(
            cli,
            "_sync_services",
            lambda engine, cfg, config_path=None, policy=None: (
                Recorder("watcher", bucket),
                Recorder("scheduler", bucket),
                threading.Lock(),
            ),
        )
        monkeypatch.setattr(
            cli,
            "_queue_drainer",
            lambda cfg, path, policy, **_kwargs: (
                Recorder("drainer", bucket) if policy.drains_queue else None
            ),
        )
        monkeypatch.setattr(
            cli,
            "_log_drainer",
            lambda cfg, engine, policy: (
                Recorder("log-drainer", bucket) if policy.drains_log_queue else None
            ),
        )
        monkeypatch.setattr(
            cli,
            "_graph_refresher",
            lambda cfg, engine, policy: (
                Recorder("refresher", bucket) if policy.refreshes_graph else None
            ),
        )
        monkeypatch.setattr(cli, "_report_ui", lambda app_obj, cfg: bucket.append("ui"))
        import uvicorn

        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
        cli._serve_app(config, str(tmp_path / "pheasant.yaml"), role=role)
        return bucket

    assert sorted(run_for("all")) == ["scheduler", "ui", "watcher"]
    assert sorted(run_for("api")) == ["refresher", "ui"]
    assert sorted(run_for("indexer")) == ["drainer", "scheduler", "watcher"]
    assert sorted(run_for("graph")) == ["refresher"]
    assert sorted(run_for("worker")) == []
    # The log tier runs one thing and nothing else -- no watcher, no
    # scheduler, no index drainer, no UI. That isolation *is* the tier.
    assert sorted(run_for("logger")) == ["log-drainer"]


def test_a_thread_is_not_left_running_after_stop(tmp_path: Path) -> None:
    from pheasant.cli import _QueueDrainer

    config = _config(tmp_path, state_name="threads", sources=1, queue=True)
    config_path = _write_config_file(config, tmp_path / "pheasant.yaml", role="indexer")

    from pheasant.config.loader import load_config

    before = {thread.name for thread in threading.enumerate()}
    drainer = _QueueDrainer(load_config(config_path), str(config_path))
    drainer.start()
    time.sleep(0.5)
    drainer.stop()
    time.sleep(0.5)

    leaked = {thread.name for thread in threading.enumerate()} - before
    assert "pheasant-drainer" not in leaked


# --------------------------------------------------------------------------
# Phase 35.8 — what a role may hold, and who may reach it
#
# Three deployment invariants, all of the same shape: none of them stops a
# process working, which is exactly why each has to be refused at startup
# rather than discovered later. They follow the template the two checks above
# already set — refuse, and name the field and the reason.
# --------------------------------------------------------------------------


def _fleet_config(tmp_path: Path, **overrides: Any) -> PheasantConfig:
    """A serving config shaped like a pod's: routable bind, no sources."""

    payload: dict[str, Any] = {
        "pheasant": {
            "name": "fleet",
            "state_path": str(tmp_path / "fleet-state"),
            "workspace_root": str(tmp_path / "fleet-workspace"),
            "exports_path": str(tmp_path / "fleet-exports"),
        },
        "server": {"host": "0.0.0.0", "role": "api"},  # noqa: S104 - the point of the test
        "sync": {"queue": {"enabled": True}},
        "sources": [],
    }
    for key, value in overrides.items():
        payload[key] = value
    return PheasantConfig.model_validate(payload)


@pytest.mark.parametrize("role", ["api", "graph", "indexer"])
def test_a_serving_role_refuses_a_routable_bind_with_no_authentication(
    tmp_path: Path, role: str, monkeypatch: Any
) -> None:
    """The single-container posture must not ship into the fleet unchanged.

    Unauthenticated, plus able to register a source over any allow-listed path
    and read it, plus one port-publishing decision away from the network, is a
    combination that stays safe by luck. A pod binds 0.0.0.0 by necessity, so
    the bind is not the control it is in Compose.
    """

    monkeypatch.delenv("PHEASANT_API_TOKEN", raising=False)
    config = _fleet_config(tmp_path)
    with pytest.raises(RoleConfigurationError) as refusal:
        validate_role(resolve_role(config, role), config)
    # The message has to name the fix; a refusal an operator cannot act on is
    # a crash loop with extra steps.
    assert "PHEASANT_API_TOKEN" in str(refusal.value)
    assert "behind_authenticating_proxy" in str(refusal.value)


def test_the_single_container_is_exempt(tmp_path: Path, monkeypatch: Any) -> None:
    """Rule 7: a router-less, infrastructure-free pheasant keeps working.

    `all` on 0.0.0.0 with no token is `pheasant up`, `pheasant serve` on a
    laptop, and every standalone container ever deployed. Refusing it would
    make a security posture for fleets into a breaking change for everyone.
    """

    monkeypatch.delenv("PHEASANT_API_TOKEN", raising=False)
    config = _fleet_config(tmp_path, server={"host": "0.0.0.0", "role": "all"})  # noqa: S104
    validate_role(resolve_role(config, "all"), config)


@pytest.mark.parametrize(
    "satisfier",
    [
        pytest.param("token", id="a token of its own"),
        pytest.param("proxy", id="an ingress that authenticates"),
        pytest.param("no-api", id="no knowledge-base API at all"),
        pytest.param("loopback", id="a bind nothing else can reach"),
    ],
)
def test_the_four_ways_to_satisfy_the_exposure_check(
    tmp_path: Path, satisfier: str, monkeypatch: Any
) -> None:
    """Each is a real deployment, so each has to be a way through."""

    monkeypatch.delenv("PHEASANT_API_TOKEN", raising=False)
    config = _fleet_config(tmp_path)
    if satisfier == "token":
        monkeypatch.setenv("PHEASANT_API_TOKEN", "a-random-value")
    elif satisfier == "proxy":
        config.security.api_auth.behind_authenticating_proxy = True
    elif satisfier == "no-api":
        config.server.api.enabled = False
    else:
        config.server.host = "127.0.0.1"
    validate_role(resolve_role(config, "api"), config)


def test_the_graph_and_worker_tokens_may_not_be_one_secret(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The shipped Compose file wired one value to both, and it read as thrift.

    Workers are the least-trusted tier and hold the indexing token by
    necessity. One value means compromising any worker also yields the
    credential for the internal graph-query API, which serves the whole graph.
    """

    monkeypatch.setenv("PHEASANT_API_TOKEN", "api-token")
    monkeypatch.setenv("PHEASANT_INDEX_WORKER_TOKEN", "shared-by-mistake")
    monkeypatch.setenv("PHEASANT_GRAPH_SERVICE_TOKEN", "shared-by-mistake")
    config = _fleet_config(tmp_path)
    with pytest.raises(RoleConfigurationError) as refusal:
        validate_role(resolve_role(config, "api"), config)
    assert "same value" in str(refusal.value)

    monkeypatch.setenv("PHEASANT_GRAPH_SERVICE_TOKEN", "its-own-value")
    validate_role(resolve_role(config, "api"), config)


def test_one_variable_named_for_both_boundaries_is_refused(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Pointing both settings at one variable is the same collapse, spelled in YAML."""

    monkeypatch.setenv("PHEASANT_API_TOKEN", "api-token")
    config = _fleet_config(tmp_path)
    config.graph.query_service_token_env = "PHEASANT_INDEX_WORKER_TOKEN"
    with pytest.raises(RoleConfigurationError) as refusal:
        validate_role(resolve_role(config, "api"), config)
    assert "two trust boundaries" in str(refusal.value)


def test_the_landing_token_is_its_own_boundary_too(tmp_path: Path, monkeypatch: Any) -> None:
    """A write credential shared with the least-trusted tier is worse, not better.

    The landing token lets its holder put bytes into the corpus. A worker
    parses bytes the indexer hands it and has no business choosing what the
    region indexes, so the collision the graph token is already refused for is
    refused here as well — by value and by variable name.
    """

    monkeypatch.setenv("PHEASANT_API_TOKEN", "api-token")
    monkeypatch.setenv("PHEASANT_INDEX_WORKER_TOKEN", "shared-by-mistake")
    monkeypatch.setenv("PHEASANT_GRAPH_SERVICE_TOKEN", "its-own-value")
    monkeypatch.setenv("PHEASANT_INGESTION_SERVICE_TOKEN", "shared-by-mistake")
    config = _fleet_config(tmp_path)
    config.ingestion.landing_service_token_env = "PHEASANT_INGESTION_SERVICE_TOKEN"

    with pytest.raises(RoleConfigurationError) as refusal:
        validate_role(resolve_role(config, "api"), config)
    assert "same value" in str(refusal.value)

    monkeypatch.setenv("PHEASANT_INGESTION_SERVICE_TOKEN", "a-fourth-value")
    validate_role(resolve_role(config, "api"), config)

    # And the same collapse spelled in YAML rather than in the environment.
    config.ingestion.landing_service_token_env = "PHEASANT_INDEX_WORKER_TOKEN"
    with pytest.raises(RoleConfigurationError) as refusal:
        validate_role(resolve_role(config, "api"), config)
    assert "two trust boundaries" in str(refusal.value)


@pytest.mark.parametrize(
    ("variable", "fragment"),
    [
        ("PHEASANT_DATABASE_URL", "the state database"),
        ("OPENAI_API_KEY", "provider"),
        ("PHEASANT_GRAPH_SERVICE_TOKEN", "graph-query API"),
        # A *write* credential: holding it means being able to put bytes into
        # the corpus. A worker parses bytes the indexer hands it and has no
        # business choosing what the region indexes.
        ("PHEASANT_INGESTION_SERVICE_TOKEN", "landing service"),
        ("IDP_TOKEN", "identity provider"),
    ],
)
def test_a_worker_refuses_every_credential_it_can_never_use(
    tmp_path: Path, variable: str, fragment: str, monkeypatch: Any
) -> None:
    """`worker.yaml` shrank the surface; nothing structurally held it there.

    A worker parses bytes it is handed and returns chunks. One shared
    `environment:` anchor in Compose, or a misapplied ConfigMap, gave the
    least-trusted tier in the fleet a credential it was designed never to
    hold — and it started happily.
    """

    for name in (
        "PHEASANT_DATABASE_URL",
        "OPENAI_API_KEY",
        "PHEASANT_GRAPH_SERVICE_TOKEN",
        "PHEASANT_INGESTION_SERVICE_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    config = _fleet_config(tmp_path, server={"host": "0.0.0.0", "role": "worker"})  # noqa: S104
    config.server.api.enabled = False
    validate_role(resolve_role(config, "worker"), config)  # clean, so it starts

    monkeypatch.setenv(variable, "a-secret")
    with pytest.raises(RoleConfigurationError) as refusal:
        validate_role(resolve_role(config, "worker"), config)
    assert variable in str(refusal.value)
    assert fragment in str(refusal.value)


def test_a_worker_refuses_a_state_backend_and_a_source_list(tmp_path: Path) -> None:
    """Both mean the same thing: this process is running the indexer's config."""

    config = _fleet_config(tmp_path, server={"host": "127.0.0.1", "role": "worker"})
    config.storage.backend = "postgres"
    with pytest.raises(RoleConfigurationError) as refusal:
        validate_role(resolve_role(config, "worker"), config)
    assert "storage.backend" in str(refusal.value)

    config.storage.backend = "sqlite"
    config.sources = [
        SourceConfig(name="notes", type=SourceType.markdown_folder, path=tmp_path / "notes")
    ]
    with pytest.raises(RoleConfigurationError) as refusal:
        validate_role(resolve_role(config, "worker"), config)
    assert "source" in str(refusal.value)


def test_a_worker_may_hold_its_own_front_door_key(tmp_path: Path, monkeypatch: Any) -> None:
    """The forbidden list is credentials to *elsewhere*, not this process's own.

    Otherwise the two guards contradict: the exposure check would demand a
    token that the allow-list refuses, and a worker serving HTTP could never
    start at all.
    """

    monkeypatch.setenv("PHEASANT_API_TOKEN", "a-random-value")
    config = _fleet_config(tmp_path, server={"host": "0.0.0.0", "role": "worker"})  # noqa: S104
    validate_role(resolve_role(config, "worker"), config)


def test_a_grpc_worker_needs_no_token_for_an_api_it_does_not_serve(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """`pheasant worker --transport grpc` binds a gRPC port, not the app.

    The exposure check is about a reachable *knowledge-base API*. Applying it
    to a process that serves none demands a token for a surface that does not
    exist — and refuses a working deployment, which is the expensive direction
    for a guard to be wrong in. What such a process may *hold* is unaffected,
    and is asserted below.
    """

    monkeypatch.delenv("PHEASANT_API_TOKEN", raising=False)
    config = _fleet_config(tmp_path, server={"host": "0.0.0.0", "role": "worker"})  # noqa: S104
    assert config.server.api.enabled is True  # the default, and irrelevant here

    with pytest.raises(RoleConfigurationError):
        validate_role(resolve_role(config, "worker"), config)  # serves_http defaults True
    validate_role(resolve_role(config, "worker"), config, serves_http=False)

    # The allow-list still applies: not serving an API is not a licence to
    # hold the database.
    monkeypatch.setenv("PHEASANT_DATABASE_URL", "postgresql://u:p@db/x")
    with pytest.raises(RoleConfigurationError) as refusal:
        validate_role(resolve_role(config, "worker"), config, serves_http=False)
    assert "PHEASANT_DATABASE_URL" in str(refusal.value)
