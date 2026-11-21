"""Shared acceptance-test fixtures for pheasant.

These fixtures exercise the public package, CLI and HTTP seams — the surfaces
other people build against — rather than internals, so a refactor behind them
does not rewrite the suite.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

# The suite runs on the offline defaults: no queue, one process, stub models,
# no keys. pheasant's own default is pheasant-kb's configuration (NATS,
# OpenAI), which tests/test_flagship_defaults.py exercises explicitly. Nothing
# above imports pheasant, so this is set before any config is built, and every
# CLI the suite runs as a subprocess inherits it.
os.environ.setdefault("PHEASANT_DEFAULTS", "standalone")

#: The state store is PostgreSQL (Postgres 17 with pg_textsearch preloaded:
#: `deploy/postgres`). This names a database the test user may connect to and
#: create databases from; each test gets a fresh database cloned from a
#: migrated template, and PHEASANT_DATABASE_URL points at it.
ADMIN_DSN_ENV = "PHEASANT_TEST_POSTGRES_DSN"
DATABASE_ENV = "PHEASANT_DATABASE_URL"

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures"
SAMPLE_WORKSPACE = FIXTURE_ROOT / "sample_workspace"
CONFIG_TEMPLATE = FIXTURE_ROOT / "configs" / "pheasant.example.yaml"


def import_any(module_names: Iterable[str]) -> Any:
    """Import the first available module from a list of public candidates."""

    errors: list[str] = []
    for module_name in module_names:
        try:
            return importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name and exc.name not in {module_name, module_name.split(".")[0]}:
                pytest.skip(f"Dependency required by {module_name} is not installed: {exc.name}")
            errors.append(f"{module_name}: {exc}")
    pytest.skip("pheasant implementation module is not available yet: " + "; ".join(errors))


def first_attr(obj: Any, names: Iterable[str]) -> Any | None:
    """Return the first named attribute present on an object."""

    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def require_attr(obj: Any, names: Iterable[str], purpose: str) -> Any:
    """Return a public attribute or skip with an actionable message."""

    attr = first_attr(obj, names)
    if attr is None:
        pytest.skip(f"No public {purpose} found. Tried: {', '.join(names)}")
    return attr


def read_count(value: Any, keys: tuple[str, ...]) -> int | None:
    """Extract a count from dict-like, object, or callable statistics results."""

    if callable(value):
        value = value()
    if isinstance(value, Mapping):
        for key in keys:
            if key in value:
                return int(value[key])
    for key in keys:
        if hasattr(value, key):
            candidate = getattr(value, key)
            if callable(candidate):
                candidate = candidate()
            return int(candidate)
    return None


def sync_result_counts(result: Any, engine: Any) -> dict[str, int]:
    """Normalize sync result/state statistics for idempotency assertions."""

    stats_candidates = [
        result,
        first_attr(result, ("stats", "summary", "counts")),
        first_attr(engine, ("stats", "summary", "counts", "graph_stats", "get_stats")),
        first_attr(
            first_attr(engine, ("graph", "store", "index")) or object(),
            ("stats", "summary", "counts", "get_stats"),
        ),
    ]
    normalized: dict[str, int] = {}
    key_aliases = {
        "node_count": ("node_count", "nodes", "num_nodes"),
        "edge_count": ("edge_count", "edges", "num_edges"),
        "artifact_count": ("artifact_count", "artifacts", "num_artifacts"),
        "chunk_count": ("chunk_count", "chunks", "num_chunks"),
    }
    for stat_source in stats_candidates:
        if stat_source is None:
            continue
        for canonical, aliases in key_aliases.items():
            if canonical not in normalized:
                value = read_count(stat_source, aliases)
                if value is not None:
                    normalized[canonical] = value
    missing = sorted(set(key_aliases) - set(normalized))
    if missing:
        pytest.skip(
            "Sync implementation does not expose stable count statistics yet: " + ", ".join(missing)
        )
    return normalized


@pytest.fixture(autouse=True)
def _fresh_model_availability():
    """Start every test with no model marked unavailable.

    A failed model call marks that model, resource and key unavailable for 60 s
    in this process (``LLM.available``), so automatic routing skips it. Across
    tests that is leaked state: a test calling a fake endpoint left every later
    test using the same model and key with no eligible model inside the
    window, and the order the files ran in decided which one failed.
    """
    from pheasant.assistant import llm

    llm.reset_availability()
    yield


@pytest.fixture(autouse=True)
def _fresh_usage_meter():
    """Start and end every test with the process's usage meter unbound and empty.

    The meter is one per process. A test that closed its engine without
    unbinding left rows the meter could no longer write to that database (its
    pool is closed), and the next test to bind the meter received them.
    """

    from pheasant.telemetry.usage import METER

    def reset() -> None:
        with METER._lock:
            METER._target = None
            METER._mirror = None
            METER._rows.clear()
            METER._inflight = {}

    reset()
    yield
    reset()


# --------------------------------------------------------------------------
# One PostgreSQL database per test
# --------------------------------------------------------------------------


def _admin_dsn() -> str:
    dsn = os.environ.get(ADMIN_DSN_ENV, "").strip()
    if not dsn:
        pytest.exit(
            f"{ADMIN_DSN_ENV} is unset. The suite runs on PostgreSQL 17 with pg_textsearch "
            "preloaded (deploy/postgres). Start one, e.g.\n"
            "  docker run -d --name pheasant-test-pg -p 127.0.0.1:55432:5432 "
            "-e POSTGRES_USER=pheasant -e POSTGRES_PASSWORD=test "
            "ghcr.io/esatt10/pheasant-postgres:17.11-pgts1.5.1-r1\n"
            f"  export {ADMIN_DSN_ENV}=postgresql://pheasant:test@127.0.0.1:55432/pheasant",
            returncode=2,
        )
    return dsn


def database_dsn(name: str) -> str:
    """The admin DSN with its database swapped for ``name``."""

    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(_admin_dsn())
    return urlunsplit(parts._replace(path=f"/{name}"))


def _admin_execute(*statements: str) -> None:
    from pheasant.persistence import pg_driver

    connection = pg_driver.connect(_admin_dsn(), autocommit=True)
    try:
        for statement in statements:
            connection.execute(statement)
    finally:
        connection.close()


@pytest.fixture(scope="session")
def _template_database() -> str:
    """A database every table has been created in, cloned once per test.

    Running the schema migration per test costs seconds; ``CREATE DATABASE …
    TEMPLATE`` copies the files and costs tens of milliseconds.
    """

    from pheasant.persistence.backends import PostgresBackend
    from pheasant.persistence.state_store import StateStore

    name = f"pheasant_template_{os.getpid()}"
    _admin_execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)', f'CREATE DATABASE "{name}"')
    store = StateStore(backend=PostgresBackend(database_dsn(name), pool_size=1))
    try:
        store.migrate()
    finally:
        store.close()
    yield name
    _admin_execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _make_database(template: str) -> str:
    import uuid

    name = f"pheasant_t_{uuid.uuid4().hex[:12]}"
    _admin_execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
    return name


def _drop_database(name: str) -> None:
    _admin_execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture()
def fresh_database(_template_database: str) -> Callable[[], str]:
    """Make another empty, migrated database and return its DSN.

    For a test that stands up two regions: each needs its own state store, as
    each had its own file before the store was a server.
    """

    made: list[str] = []

    def make() -> str:
        made.append(_make_database(_template_database))
        return database_dsn(made[-1])

    yield make
    for name in made:
        _drop_database(name)


def _database_variables(template: str):
    made: list[tuple[str, str]] = []

    def make() -> str:
        name = _make_database(template)
        variable = f"PHEASANT_TEST_DB_{name.rsplit('_', 1)[-1].upper()}"
        os.environ[variable] = database_dsn(name)
        made.append((variable, name))
        return variable

    def drop() -> None:
        for variable, name in made:
            os.environ.pop(variable, None)
            _drop_database(name)

    return make, drop


@pytest.fixture()
def database_variable(_template_database: str) -> Callable[[], str]:
    """Make a database and return the name of a variable holding its DSN.

    For a second region in one test: its config names the variable as
    ``storage.dsn_env``, so it keeps a state store of its own beside the one
    ``PHEASANT_DATABASE_URL`` names, as it kept a state directory of its own.
    """

    make, drop = _database_variables(_template_database)
    yield make
    drop()


@pytest.fixture(scope="session")
def session_database_variable(_template_database: str) -> Callable[[], str]:
    """``database_variable`` for a region a module-scoped fixture builds."""

    make, drop = _database_variables(_template_database)
    yield make
    drop()


#: A module whose fixtures build one corpus for all its tests marks itself
#: ``pytestmark = pytest.mark.module_database``: the tests reading that corpus
#: share the database it was indexed into, as they shared its state directory.
MODULE_DATABASE = "module_database"


@pytest.fixture(scope="module", autouse=True)
def _module_database(request: pytest.FixtureRequest, _template_database: str):
    if request.node.get_closest_marker(MODULE_DATABASE) is None:
        yield None
        return
    name = _make_database(_template_database)
    dsn = database_dsn(name)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(DATABASE_ENV, dsn)
        yield dsn
    _drop_database(name)


def _uses_module_fixture(request: pytest.FixtureRequest) -> bool:
    """Whether the test takes a fixture its module built once (its corpus)."""

    own = {"_module_database", "_template_database"}
    for name, definitions in request._pyfuncitem._fixtureinfo.name2fixturedefs.items():
        if name not in own and any(d.scope == "module" for d in definitions):
            return True
    return False


@pytest.fixture(autouse=True)
def _state_database(
    request: pytest.FixtureRequest,
    _module_database: str | None,
    fresh_database: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """Every test's state store is a database of its own.

    A test reading its module's shared corpus reads the module's database; one
    that builds a region of its own beside it gets a fresh one, as it got a
    state directory of its own.
    """

    shared = _module_database is not None and _uses_module_fixture(request)
    dsn = _module_database if shared else fresh_database()
    monkeypatch.setenv(DATABASE_ENV, dsn)
    return dsn


@pytest.fixture()
def no_state_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process that must hold no state credential: a preparation worker."""

    monkeypatch.delenv(DATABASE_ENV, raising=False)


def seed_source(store: Any, source_id: str = "demo", kb_id: str = "demo") -> None:
    """Register a knowledge base and a source, for a test writing rows by hand.

    Every artifact, chunk and checkpoint names its source, and Postgres
    enforces the foreign key that SQLite only declared.
    """

    now = "2026-01-01T00:00:00Z"
    store.upsert_knowledge_base(kb_id, kb_id, None, "test", now)
    store.upsert_source(source_id, kb_id, source_id, "folder", "/w", True, {})


@pytest.fixture()
def workspace_copy(tmp_path: Path) -> Path:
    """Copy the sample workspace so tests can mutate it safely."""

    destination = tmp_path / "workspace"
    shutil.copytree(SAMPLE_WORKSPACE, destination)
    return destination


@pytest.fixture()
def state_path(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    path.mkdir()
    return path


@pytest.fixture()
def exports_path(tmp_path: Path) -> Path:
    path = tmp_path / "exports"
    path.mkdir()
    return path


@pytest.fixture()
def config_path(
    tmp_path: Path,
    workspace_copy: Path,
    state_path: Path,
    exports_path: Path,
) -> Path:
    """Render the acceptance-test YAML config with temporary paths."""

    rendered = CONFIG_TEMPLATE.read_text(encoding="utf-8").format(
        workspace_path=workspace_copy.as_posix(),
        state_path=state_path.as_posix(),
        exports_path=exports_path.as_posix(),
    )
    path = tmp_path / "pheasant.yaml"
    path.write_text(rendered, encoding="utf-8")
    return path


@pytest.fixture()
def loaded_config(config_path: Path) -> Any:
    """Load a pheasant config through the public loader when available."""

    config_module = import_any(("pheasant.config.loader", "pheasant.config"))
    loader: Callable[..., Any] = require_attr(
        config_module,
        ("load_config", "load_pheasant_config", "load", "from_yaml"),
        "config loader",
    )
    return loader(config_path)


@pytest.fixture()
def sync_engine(loaded_config: Any, config_path: Path) -> Any:
    """Construct the public sync/indexing engine for integration tests."""

    sync_module = import_any(
        (
            "pheasant.sync.engine",
            "pheasant.sync",
            "pheasant.ingestion.pipeline",
            "pheasant.indexing",
            "pheasant.indexer",
        )
    )
    engine_factory = require_attr(
        sync_module,
        ("SyncEngine", "Indexer", "KnowledgeIndexer", "create_sync_engine"),
        "sync engine",
    )
    try:
        return engine_factory(loaded_config)
    except TypeError:
        return engine_factory(config_path=config_path)


def make_vector_engine(tmp_path: Path, vector_provider: str = "numpy") -> Any:
    """SyncEngine over a tiny workspace with stub embeddings enabled.

    The stub embedder maps planted synonyms onto shared directions
    (``automobile -> car``), so ``vehicles.md`` is retrievable for the
    query "car" through vector search while FTS misses it. Used by the
    vector-search acceptance tests and the idempotency spine.
    """

    sync_module = import_any(("pheasant.sync.engine",))
    schema_module = import_any(("pheasant.config.schema",))

    notes = tmp_path / "vector-workspace" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "vehicles.md").write_text(
        "# Fleet\n\nThe automobile fleet needs quarterly inspection and battery checks.\n",
        encoding="utf-8",
    )
    (notes / "kitchen.md").write_text(
        "# Kitchen\n\nWash dishes nightly and restock pantry shelves.\n",
        encoding="utf-8",
    )
    config = schema_module.PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "vector-acceptance",
                "state_path": str(tmp_path / "vector-state"),
                "workspace_root": str(tmp_path / "vector-workspace"),
                "exports_path": str(tmp_path / "vector-exports"),
            },
            "search": {
                "embeddings": {"enabled": True, "provider": "stub", "dimensions": 64},
                "vector_store": {"provider": vector_provider},
            },
            "sources": [{"name": "notes", "type": "markdown_folder", "path": str(notes)}],
        }
    )
    return sync_module.SyncEngine(config)


def run_sync(engine: Any, source_name: str = "pheasant-repo", mode: str = "full") -> Any:
    """Run a source sync using common public method names."""

    sync_method = require_attr(
        engine,
        ("sync_source", "sync", "index_source", "index", "refresh_source"),
        "source sync method",
    )
    for kwargs in (
        {"source_name": source_name, "mode": mode},
        {"name": source_name, "mode": mode},
        {"source": source_name, "mode": mode},
        {"source_name": source_name},
        {"name": source_name},
        {"source": source_name},
        {},
    ):
        try:
            return sync_method(**kwargs)
        except TypeError:
            continue
    return sync_method(source_name)


def search_context(query: str, loaded_config: Any | None = None, engine: Any | None = None) -> Any:
    """Call the public search_context equivalent."""

    if engine is not None:
        engine_search = first_attr(engine, ("search_context", "search", "query"))
        if engine_search is not None:
            return engine_search(query)

    search_module = import_any(("pheasant.search", "pheasant.search.engine", "pheasant.api.tools"))
    search_factory = first_attr(
        search_module, ("SearchEngine", "ContextSearch", "create_search_engine")
    )
    if search_factory is not None:
        search_engine = (
            search_factory(loaded_config) if loaded_config is not None else search_factory()
        )
        method = require_attr(search_engine, ("search_context", "search", "query"), "search method")
        return method(query)

    search_func = require_attr(
        search_module, ("search_context", "search"), "search_context function"
    )
    try:
        return search_func(query=query, config=loaded_config)
    except TypeError:
        return search_func(query)


def result_items(search_result: Any) -> list[Any]:
    """Normalize search results into a list of result items."""

    if isinstance(search_result, Mapping):
        for key in ("results", "items", "chunks", "matches"):
            if key in search_result:
                return list(search_result[key])
    for key in ("results", "items", "chunks", "matches"):
        if hasattr(search_result, key):
            value = getattr(search_result, key)
            return list(value() if callable(value) else value)
    if isinstance(search_result, (list, tuple)):
        return list(search_result)
    pytest.skip("Search response does not expose iterable results yet.")


def item_text(item: Any) -> str:
    """Flatten a search/export result item for assertion-friendly matching."""

    if isinstance(item, Mapping):
        return json.dumps(item, sort_keys=True, default=str)
    return json.dumps(getattr(item, "__dict__", str(item)), sort_keys=True, default=str)


def call_cli(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the pheasant CLI via module or console script."""

    commands = [
        [sys.executable, "-m", "pheasant", *args],
        ["pheasant", *args],
    ]
    last_error: FileNotFoundError | None = None
    for command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=cwd or REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=20,
            )
        except FileNotFoundError as exc:
            last_error = exc
            continue
        if "No module named pheasant" in result.stderr:
            continue
        return result
    pytest.skip(
        f"pheasant CLI is not available yet: {last_error or 'pheasant module not importable'}"
    )
