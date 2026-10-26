"""A sync that never loads the graph writes exactly what one that loads it does.

`graph.store_backed.StoreBackedGraph` is the working set of every ``pheasant
sync`` process on the row backend: it reads the store a selection at a time
instead of decoding the whole graph first. The property that makes it safe is
the strongest one available, and it is asserted directly: run the same
sequence of syncs twice over the same files -- once through engines that load
the graph (the old way, and still the way an in-process sync works), once
through engines that do not -- and the saved graph is the same **row for
row**, digest included, so the published generation id is the same too. The
node index, which a partial graph has to maintain without a whole graph to
rebuild from, is compared the same way.

Each step uses a **fresh** engine on both sides, because that is what a sync
process is, and the clock is frozen per step because ``created_at`` and
``updated_at`` are graph content.

And the point of it: no step takes the whole-graph fallback, and a sync of a
few files costs a few queries rather than a load.
"""

from __future__ import annotations

import base64
import os
import shutil
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.graph.store_backed import StoreBackedGraph
from pheasant.ingestion import pipeline
from pheasant.memory.store import MemoryStore
from pheasant.sync.engine import SyncEngine, SyncResult
from tests.test_okf_bundles import write_bundle

KB = "working-set"

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _write(root: Path, files: dict[str, str | bytes]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")


def _corpus(root: Path) -> dict[str, Path]:
    docs, code, bundle = root / "docs", root / "code", root / "bundle"
    _write(
        docs,
        {
            "guide.md": (
                "# Guide\n\n## Install\n\nSee [the loader](../code/pkg/loader.py) and "
                "[setup](setup.md).\n\n![diagram](img/flow.png)\n\n## Usage\n\n"
                "Call `load_config` from the gateway. Kubernetes and Postgres are used.\n"
            ),
            "setup.md": "# Setup\n\nRead [the guide](guide.md). Postgres runs the store.\n",
            "notes/deep/one.md": "# One\n\nLinks to [two](two.md) and [guide](../../guide.md).\n",
            "notes/deep/two.md": "# Two\n\nNothing but text about Kubernetes.\n",
            "img/flow.png": PNG,
        },
    )
    _write(
        code,
        {
            "pkg/__init__.py": "",
            "pkg/loader.py": (
                "import json\nfrom pkg import util\n\n\n"
                "def load_config(path):\n    return util.read(path)\n\n\n"
                "class Loader:\n    def run(self):\n        return load_config('x')\n"
            ),
            "pkg/util.py": "def read(path):\n    return open(path).read()\n",
            "pkg/extra.py": (
                "from pkg.loader import Loader\n\n\ndef go():\n    return Loader().run()\n"
            ),
        },
    )
    write_bundle(bundle)
    # Memory, so the bridge runs: one record per rung -- a link, a symbol and
    # an entity -- across two sources.
    memory = root / "memory"
    memory.mkdir()
    store = MemoryStore(memory)
    for second, text in enumerate(
        (
            "Read [the guide](guide.md) before changing the gateway.",
            "load_config is safe to call twice.",
            "Kubernetes runs the store in production.",
        )
    ):
        store.append(text, scope="org", now=datetime(2026, 10, 1, 0, 0, second, tzinfo=UTC))
    return {"docs": docs, "code": code, "bundle": bundle, "memory": memory}


def _config(
    state: Path, corpus: dict[str, Path], root: Path, storage: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "pheasant": {
            "name": KB,
            "state_path": str(state),
            "workspace_root": str(root),
            "exports_path": str(state.parent / f"{state.name}-exports"),
        },
        "server": {"host": "127.0.0.1"},
        "storage": {"graph_format": "rows", "graph_snapshots": False, **(storage or {})},
        "sources": [
            {
                "name": "docs",
                "type": "markdown_folder",
                "path": str(corpus["docs"]),
                "include": ["**/*.md", "*.md", "**/*.png"],
                "taxonomy": {"enabled": True},
            },
            {
                "name": "code",
                "type": "document_folder",
                "path": str(corpus["code"]),
                "include": ["**/*.py"],
            },
            {
                "name": "bundle",
                "type": "markdown_folder",
                "path": str(corpus["bundle"]),
                "include": ["**/*.md", "*.md"],
            },
            {"name": "agent-memory", "type": "memory", "path": str(corpus["memory"])},
        ],
    }


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Every module's ``utc_now`` returns the step's instant."""

    clock = {"now": "2026-10-26T00:00:00Z"}
    original = pipeline.utc_now
    for name, module in list(sys.modules.items()):
        if name.startswith("pheasant") and getattr(module, "utc_now", None) is original:
            monkeypatch.setattr(module, "utc_now", lambda: clock["now"])
    yield clock


def _graph_rows(engine: SyncEngine) -> tuple[list[tuple], list[tuple], list[tuple]]:
    state = engine.state
    nodes = [
        tuple(row)
        for row in state.rows(
            "SELECT node_id,type,label,source_id,relative_path,artifact_id,attrs,digest"
            " FROM graph_nodes WHERE kb_id=? ORDER BY node_id",
            (KB,),
        )
    ]
    edges = [
        tuple(row)
        for row in state.rows(
            "SELECT source,target,type,seq,source_id,attrs,digest FROM graph_edges"
            " WHERE kb_id=? ORDER BY source,target,type,seq",
            (KB,),
        )
    ]
    index = sorted(
        tuple(row) for row in state.rows("SELECT node_id, source_id, body FROM graph_nodes_fts")
    )
    return nodes, edges, index


class Pair:
    """Two state directories over the same files: one resident, one store-backed."""

    def __init__(
        self,
        tmp_path: Path,
        clock: dict[str, str],
        storage: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.root = tmp_path / "files"
        self.corpus = _corpus(self.root)
        self.clock = clock
        self.configs = {
            mode: _config(
                tmp_path / f"state-{mode}", self.corpus, self.root, (storage or {}).get(mode)
            )
            for mode in ("resident", "store")
        }
        self.whole_graph_reads: list[str] = []
        self.fault_queries: list[dict[str, int]] = []

    def engine(self, mode: str) -> SyncEngine:
        config = PheasantConfig.model_validate(self.configs[mode])
        engine = SyncEngine(config, defer_persisted_graph_load=mode == "store")
        if mode == "resident" and not engine.graph_store.rows._is_empty(KB):
            # What a sync process did before this: load the graph and drop the
            # index delta the load itself generated (see
            # `_ensure_persisted_graph_loaded`, which keeps the builder's own
            # graph -- root included -- when the store is empty). An engine
            # that loads at construction keeps that delta and rewrites every
            # index row on its first flush, in stored-row key order: the same
            # words, ordered differently, and not the behaviour compared here.
            engine.graph_builder.graph.take_index_delta()
        return engine

    def step(self, at: str, action) -> None:
        self.clock["now"] = at
        for mode in ("resident", "store"):
            engine = self.engine(mode)
            try:
                graph = engine.graph_builder.graph
                assert isinstance(graph, StoreBackedGraph) == (mode == "store")
                action(engine)
                if mode == "store":
                    self.whole_graph_reads.extend(graph.whole_graph_reads)
                    self.fault_queries.append(dict(graph.fault_queries))
            finally:
                engine.close()
        self.assert_same()

    def edge_types(self) -> dict[str, int]:
        engine = self.engine("resident")
        try:
            rows = engine.state.rows(
                "SELECT type, COUNT(*) AS n FROM graph_edges WHERE kb_id=? GROUP BY type", (KB,)
            )
            return {str(row["type"]): int(row["n"]) for row in rows}
        finally:
            engine.close()

    def assert_same(self) -> None:
        engines = {mode: self.engine(mode) for mode in ("resident", "store")}
        try:
            resident, store = (_graph_rows(engines[mode]) for mode in ("resident", "store"))
            assert len(resident[0]) > 30, "the corpus is too small to say anything"
            for kind, left, right in zip(("nodes", "edges", "index"), resident, store, strict=True):
                missing = sorted(set(left) - set(right))[:3]
                extra = sorted(set(right) - set(left))[:3]
                assert left == right, f"{kind} differ: resident-only {missing}, store-only {extra}"
            generations = {
                mode: engines[mode].graph_store.published_generation(KB)["generation_id"]
                for mode in engines
            }
            assert generations["resident"] == generations["store"]
        finally:
            for engine in engines.values():
                engine.close()


SOURCES = ("docs", "code", "bundle", "agent-memory")


def _sync_all(mode: str = "incremental"):
    def run(engine: SyncEngine) -> None:
        for name in SOURCES:
            engine.sync_source(name, mode)

    return run


@pytest.fixture
def pair(tmp_path: Path, frozen_clock: dict[str, str]) -> Pair:
    return Pair(tmp_path, frozen_clock)


@pytest.fixture
def postgres_pair(
    tmp_path: Path, frozen_clock: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> Iterator[Pair]:
    """The same pair on Postgres: one fresh database per side, dropped afterwards.

    Separate databases rather than separate knowledge bases, so the search
    index -- which carries no ``kb_id`` -- compares only what each side wrote.
    """

    import uuid

    import psycopg
    from psycopg.conninfo import make_conninfo

    base = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()
    if not base:
        pytest.skip("set PHEASANT_TEST_POSTGRES_DSN to run the working set on Postgres")
    names = {mode: f"pheasant_ws_{uuid.uuid4().hex[:12]}_{mode}" for mode in ("resident", "store")}
    storage = {}
    with psycopg.connect(base, autocommit=True) as conn:
        for mode, name in names.items():
            conn.execute(f'CREATE DATABASE "{name}"')
            variable = f"PHEASANT_TEST_WS_{mode.upper()}_DSN"
            monkeypatch.setenv(variable, make_conninfo(base, dbname=name))
            storage[mode] = {"backend": "postgres", "dsn_env": variable}
    try:
        yield Pair(tmp_path, frozen_clock, storage)
    finally:
        with psycopg.connect(base, autocommit=True) as conn:
            for name in names.values():
                conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_a_sync_that_reads_the_store_writes_what_a_resident_one_writes(pair: Pair) -> None:
    _the_whole_sequence(pair)


def test_the_same_holds_on_postgres(postgres_pair: Pair) -> None:
    """`= ANY`, `strpos` and the typed join are dialect code; run them where they run."""

    _the_whole_sequence(postgres_pair)


def _the_whole_sequence(pair: Pair) -> None:
    docs, code, bundle = (pair.corpus[name] for name in ("docs", "code", "bundle"))
    pair.step("2026-10-26T01:00:00Z", _sync_all("full"))
    # The sequence only proves something if every pass it claims to cover drew
    # something to compare: links within and across sources, an image, the
    # taxonomy, OKF and the memory bridge.
    drawn = pair.edge_types()
    for kind in ("references", "imports", "embeds", "has_heading", "links_to", "about"):
        assert drawn.get(kind), f"the scenario drew no {kind} edges: {drawn}"
    # Nothing changed: nothing written, on either side.
    pair.step("2026-10-26T02:00:00Z", _sync_all())

    # Edits that retract: a dropped link, a dropped image, a dropped import, a
    # renamed function, a heading moved; and a new cross-source reference.
    _write(
        docs,
        {
            "guide.md": (
                "# Guide\n\n## Usage\n\nCall `load_config` from the gateway. "
                "See [util](../code/pkg/util.py).\n\n## Extra\n\nMore text about Postgres.\n"
            ),
        },
    )
    _write(
        code,
        {
            "pkg/loader.py": (
                "from pkg import util\n\n\ndef load_settings(path):\n    return util.read(path)\n"
            ),
        },
    )
    _write(bundle, {"metrics/margin.md": "---\ntype: Metric\ntitle: Gross Margin\n---\n\nPlain.\n"})
    pair.step("2026-10-26T03:00:00Z", _sync_all())

    # A memory correction: `supersedes`, and an `about` that moves.
    (first,) = [r for r in MemoryStore(pair.corpus["memory"]).list_records() if "guide" in r.text]
    MemoryStore(pair.corpus["memory"]).append(
        "Read [setup](setup.md) instead.",
        scope="org",
        supersedes=first.record_id,
        now=datetime(2026, 10, 2, tzinfo=UTC),
    )
    pair.step("2026-10-26T03:30:00Z", _sync_all())
    # The sequence only proves something if every pass it claims to cover drew
    # something to compare.
    drawn = pair.edge_types()
    for kind in ("about", "supersedes"):
        assert drawn.get(kind), f"the scenario drew no {kind} edges: {drawn}"

    # A deletion and an addition in one pass: vanished pruning and a new file.
    (docs / "notes" / "deep" / "two.md").unlink()
    _write(docs, {"notes/three.md": "# Three\n\n[setup](../setup.md) and [one](deep/one.md).\n"})
    (code / "pkg" / "extra.py").unlink()
    pair.step("2026-10-26T04:00:00Z", _sync_all())

    # A whole directory gone, and a full re-sync of one source.
    shutil.rmtree(docs / "notes")
    pair.step("2026-10-26T05:00:00Z", lambda engine: engine.sync_source("docs", "incremental"))
    pair.step("2026-10-26T06:00:00Z", lambda engine: engine.sync_source("code", "full"))

    # Removing a source takes everything it contributed, on both endpoints.
    pair.step("2026-10-26T07:00:00Z", lambda engine: engine.remove_source("code"))
    pair.step("2026-10-26T08:00:00Z", _sync_all_except("code"))

    assert pair.whole_graph_reads == [], pair.whole_graph_reads


def _sync_all_except(skipped: str):
    def run(engine: SyncEngine) -> None:
        for name in SOURCES:
            if name != skipped:
                engine.sync_source(name, "incremental")

    return run


def test_an_unchanged_sync_reads_almost_nothing(pair: Pair) -> None:
    """The cost of a sync is the change's, not the region's."""

    pair.step("2026-10-26T01:00:00Z", _sync_all("full"))
    pair.fault_queries.clear()
    pair.step("2026-10-26T02:00:00Z", _sync_all())
    (queries,) = pair.fault_queries
    # One: whether the region's root exists, asked once when the working set
    # is made. Nothing else, because nothing changed.
    assert queries == {"nodes": 1, "outs": 0, "pairs": 0, "selections": 0}, queries


def test_editing_one_file_faults_in_its_neighbourhood_not_the_graph(pair: Pair) -> None:
    pair.step("2026-10-26T01:00:00Z", _sync_all("full"))
    _write(pair.corpus["docs"], {"setup.md": "# Setup\n\nNow about Kubernetes instead.\n"})
    pair.fault_queries.clear()
    held: dict[str, Any] = {}

    def run(engine: SyncEngine) -> None:
        engine.sync_source("docs", "incremental")
        graph = engine.graph_builder.graph
        if isinstance(graph, StoreBackedGraph):
            held["nodes"] = len(graph._nodes)
            held["total"] = engine.graph_store.counts(KB)[0]

    pair.step("2026-10-26T02:00:00Z", run)
    (queries,) = pair.fault_queries
    # A handful of batched point reads plus the linking pass's selections.
    assert sum(queries.values()) <= 40, queries
    assert held["nodes"] < held["total"] / 2, held
    assert pair.whole_graph_reads == []


def test_a_whole_graph_read_still_answers_correctly(tmp_path: Path, frozen_clock) -> None:
    """The fallback is slow and right: every node, store plus this process's writes."""

    pair = Pair(tmp_path, frozen_clock)
    pair.step("2026-10-26T01:00:00Z", _sync_all("full"))
    engine = pair.engine("store")
    try:
        graph = engine.graph_builder.graph
        graph.add_node("late:node", type="symbol", label="late", source_id="docs")
        graph.remove_nodes_from([f"source:{KB}:bundle"])
        total = engine.graph_store.counts(KB)[0]
        assert graph.number_of_nodes() == total  # +1 added, -1 removed
        assert graph.whole_graph_reads == ["number_of_nodes()"]
        assert "late:node" in graph and f"source:{KB}:bundle" not in graph
    finally:
        engine.close()


def _store_graph(pair: Pair) -> tuple[SyncEngine, StoreBackedGraph]:
    pair.step("2026-10-26T01:00:00Z", _sync_all("full"))
    engine = pair.engine("store")
    graph = engine.graph_builder.graph
    assert isinstance(graph, StoreBackedGraph)
    return engine, graph


ARTIFACT = "file:docs:setup.md:branch=none"
SOURCE = f"source:{KB}:docs"


def test_a_write_merges_over_what_the_store_holds(pair: Pair) -> None:
    """`add_node` merges, so the stored node has to be read first, by the write itself."""

    engine, graph = _store_graph(pair)
    try:
        graph.add_node(ARTIFACT, note="added")
        attrs = graph.nodes[ARTIFACT]
        assert attrs["note"] == "added" and attrs["relative_path"] == "setup.md"
        (upsert,) = [n for n, a in graph.graph_delta()["node_upserts"] if n == ARTIFACT]
        assert upsert == ARTIFACT
    finally:
        engine.close()


def test_an_added_edge_joins_its_stored_pair_rather_than_replacing_it(pair: Pair) -> None:
    """A delta replaces a pair whole, so a write that held half of one would delete the rest."""

    engine, graph = _store_graph(pair)
    try:
        graph.add_edge(SOURCE, ARTIFACT, type="annotates", source_id="docs")
        types = sorted(a["type"] for a in graph.get_edge_data(SOURCE, ARTIFACT).values())
        assert types == ["annotates", "contains", "indexes"]
        written = [e for e in graph.graph_delta()["edge_upserts"] if e[:2] == (SOURCE, ARTIFACT)]
        assert sorted(e[3]["type"] for e in written) == ["annotates", "contains", "indexes"]
    finally:
        engine.close()


def test_reading_a_node_s_edges_never_overwrites_a_pair_changed_here(pair: Pair) -> None:
    engine, graph = _store_graph(pair)
    try:
        edges = graph.get_edge_data(SOURCE, ARTIFACT).values()
        (data,) = [attrs for attrs in edges if attrs["type"] == "indexes"]
        data["confidence"] = 0.25  # an in-place change, as `upsert_edge` makes
        graph.touch_edge(SOURCE, ARTIFACT)
        held = {target: edges for _s, target, edges in graph.out_edges(SOURCE)}
        (kept,) = [a for a in held[ARTIFACT].values() if a["type"] == "indexes"]
        assert kept["confidence"] == 0.25
        assert len(held) > 1, "the out-edge read must have fetched the rest of the node"
    finally:
        engine.close()


def test_a_removed_node_s_stored_edges_never_come_back(pair: Pair) -> None:
    """The commit cascades them; until it does, a read must not show them either."""

    engine, graph = _store_graph(pair)
    try:
        graph.remove_nodes_from([ARTIFACT])
        assert ARTIFACT not in {target for _s, target, _e in graph.out_edges(SOURCE)}
        assert graph.get_edge_data(SOURCE, ARTIFACT) is None
        assert ARTIFACT not in graph.targets_with_incoming([ARTIFACT])
        assert ARTIFACT in graph.graph_delta()["node_removals"]
    finally:
        engine.close()


def test_a_sync_process_serves_from_the_store_never_from_its_working_set(
    tmp_path: Path, frozen_clock
) -> None:
    pair = Pair(tmp_path, frozen_clock)
    pair.step("2026-10-26T01:00:00Z", _sync_all("full"))
    for mode, expected in (("store", "SqlGraph"), ("resident", "SimpleMultiDiGraph")):
        engine = pair.engine(mode)
        try:
            assert type(engine.serving_graph()).__name__ == expected
        finally:
            engine.close()


# --------------------------------------------------------------------------
# History snapshots, taken beside a sync process rather than inside it
# --------------------------------------------------------------------------


def _snapshot_engine(pair: Pair, **storage: Any) -> SyncEngine:
    payload = pair.configs["store"]
    payload["storage"].update({"graph_snapshots": True, **storage})
    return pair.engine("store")


def _snapshots(engine: SyncEngine) -> list[Path]:
    return engine.graph_store.list_snapshots(KB)


def test_a_supervised_sync_leaves_its_snapshot_to_its_supervisor(pair: Pair) -> None:
    from pheasant.sync.graph_history import snapshot_after_sync

    pair.step("2026-10-26T01:00:00Z", _sync_all("full"))
    engine = _snapshot_engine(pair)
    try:
        engine.defer_snapshots = True
        snapshot_after_sync(engine)
        assert _snapshots(engine) == []
        engine.defer_snapshots = False
        snapshot_after_sync(engine)
        assert len(_snapshots(engine)) == 1
    finally:
        engine.close()


def test_the_snapshot_job_writes_the_published_generation_once_per_interval(pair: Pair) -> None:
    from pheasant.sync.graph_history import snapshot_published

    pair.step("2026-10-26T01:00:00Z", _sync_all("full"))
    engine = _snapshot_engine(pair)
    try:
        written = snapshot_published(engine)
        assert written is not None and _snapshots(engine) == [written]
        restored = engine.graph_store.read_snapshot(written)
        assert restored.number_of_nodes() == engine.graph_store.counts(KB)[0]
        assert snapshot_published(engine) is None  # not due again within the interval
    finally:
        engine.close()


def test_a_snapshot_that_a_commit_overtook_is_discarded(
    pair: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read beside syncs, so a commit can land mid-read: never keep a mixed graph."""

    from pheasant.sync.graph_history import snapshot_published

    pair.step("2026-10-26T01:00:00Z", _sync_all("full"))
    engine = _snapshot_engine(pair)
    try:
        generations = iter([{"generation_id": "before"}, {"generation_id": "after"}])
        monkeypatch.setattr(
            engine.graph_store, "published_generation", lambda _kb: next(generations)
        )
        assert snapshot_published(engine) is None
        assert _snapshots(engine) == []
    finally:
        engine.close()


def test_the_snapshot_job_runs_as_its_own_module(pair: Pair, tmp_path: Path, capsys) -> None:
    import json

    import yaml

    from pheasant.sync.graph_history import main

    pair.step("2026-10-26T01:00:00Z", _sync_all("full"))
    payload = pair.configs["store"]
    payload["storage"]["graph_snapshots"] = True
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    assert main(["--config", str(config_path)]) == 0
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert report["status"] == "ok" and report["snapshot"]
    assert Path(report["snapshot"]).exists()


def test_the_supervisor_starts_one_snapshot_job_and_only_when_due(
    pair: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from pheasant.sync import worker as worker_module
    from pheasant.sync.worker import WorkerBackedEngine

    pair.step("2026-10-26T01:00:00Z", _sync_all("full"))
    engine = _snapshot_engine(pair)
    started: list[str] = []
    release = threading.Event()

    def slow_snapshot(config_path, **_kwargs):
        started.append(str(config_path))
        release.wait(10)
        return {"status": "ok"}

    monkeypatch.setattr(worker_module, "run_snapshot", slow_snapshot)
    monkeypatch.setattr(worker_module, "_snapshot_job", None)
    try:
        backed = WorkerBackedEngine(engine, "/config/pheasant.yaml")
        unchanged = [SyncResult("docs", 0, 3, 0, 0)]
        changed = [SyncResult("docs", 2, 1, 0, 0)]
        # History stores changes: a sync that changed nothing starts nothing.
        backed._snapshot_in_background(unchanged, "incremental")
        assert worker_module._snapshot_job is None
        backed._snapshot_in_background(changed, "incremental")
        backed._snapshot_in_background(changed, "incremental")  # one is already running
        release.set()
        worker_module._snapshot_job.join(10)
        assert started == ["/config/pheasant.yaml"]
        # Once the interval's snapshot exists, nothing is started.
        engine.graph_store.snapshot_current(KB, "2026-10-26T01:30:00Z")
        backed._snapshot_in_background(changed, "incremental")
        assert len(started) == 1
    finally:
        engine.close()


def test_only_a_supervised_store_backed_sync_defers_its_snapshot(
    pair: Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor's word, and only where reading the rows back is the cost."""

    from pheasant.sync.graph_history import SNAPSHOT_BY_SUPERVISOR, snapshot_after_sync

    pair.step("2026-10-26T01:00:00Z", _sync_all("full"))
    monkeypatch.setenv(SNAPSHOT_BY_SUPERVISOR, "1")
    engine = _snapshot_engine(pair)
    try:
        snapshot_after_sync(engine)
        assert _snapshots(engine) == []  # a store-backed child: its supervisor takes it
        engine._partial_graph = False  # a resident graph serializes itself cheaply
        snapshot_after_sync(engine)
        assert len(_snapshots(engine)) == 1
    finally:
        engine.close()


def test_the_supervisor_tells_its_sync_child(monkeypatch: pytest.MonkeyPatch) -> None:
    from pheasant.sync import worker as worker_module
    from pheasant.sync.graph_history import SNAPSHOT_BY_SUPERVISOR

    seen: dict[str, Any] = {}

    class Done:
        returncode, stdout, stderr = 0, '{"status": "ok", "results": []}', ""

    def fake_run(command, **kwargs):
        seen.update(kwargs.get("env") or {})
        return Done()

    monkeypatch.setattr(worker_module.subprocess, "run", fake_run)
    worker_module.run_sync("/config/pheasant.yaml", "docs")
    assert seen.get(SNAPSHOT_BY_SUPERVISOR) == "1"
