"""The cold writer produces the resident writer's graph without reading it all."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.graph.working_set import RowWorkingSet
from pheasant.sync.engine import SyncEngine


def _config(root: Path, workspace: Path) -> PheasantConfig:
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "parity",
                "state_path": str(root),
                "workspace_root": str(workspace),
                "exports_path": str(root / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "sources": [
                {
                    "name": "docs",
                    "type": "repository",
                    "path": str(workspace),
                    "include": ["**/*.py", "**/*.md"],
                }
            ],
        }
    )


def _content(engine):
    def stable(attrs):
        return {k: v for k, v in attrs.items() if k not in {"created_at", "updated_at"}}

    rows, kb = engine.graph_store.rows, engine.config.knowledge_base_id
    return (
        sorted(
            (key, json.dumps(stable(attrs), sort_keys=True)) for key, attrs in rows.iter_nodes(kb)
        ),
        sorted(
            (a, b, json.dumps(stable(attrs), sort_keys=True))
            for (a, b), parallel in rows.iter_edges(kb)
            for attrs in parallel.values()
        ),
    )


def test_cold_changed_files_match_resident_graph_without_whole_graph_reads(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "app.py").write_text("import target\ndef before():\n    return helper()\n")
    (workspace / "guide.md").write_text("# Gateway\n\nSee [later](later.md).\n")
    resident = SyncEngine(_config(tmp_path / "resident", workspace))
    row = SyncEngine(_config(tmp_path / "rows", workspace), defer_persisted_graph_load=True)
    catalog_reads = []
    typed_nodes = RowWorkingSet.typed_nodes

    def tracked(graph, types):
        catalog_reads.append(types)
        return typed_nodes(graph, types)

    monkeypatch.setattr(RowWorkingSet, "typed_nodes", tracked)
    monkeypatch.setattr(row.graph_store, "load", lambda *_: pytest.fail("whole graph loaded"))
    try:
        for engine in (resident, row):
            engine.sync_source("docs", "incremental")
        assert _content(row) == _content(resident)
        for name in ("iter_nodes", "iter_edges"):
            monkeypatch.setattr(RowWorkingSet, name, lambda *_: pytest.fail("whole graph walked"))
        changes = [
            ("target.py", "def helper():\n    return 1\n"),
            ("later.md", "# Later\n\nA late-arriving link target.\n"),
            ("app.py", "def after():\n    return 2\n"),
        ]
        for name, content in changes:
            (workspace / name).write_text(content)
            for engine in (resident, row):
                engine.sync_source("docs", "incremental")
            assert _content(row) == _content(resident), name
        (workspace / "later.md").unlink()
        for engine in (resident, row):
            engine.sync_source("docs", "incremental")
        assert _content(row) == _content(resident)
        generation = row.graph_store.published_generation("parity")["generation_id"]
        row.reload_graph()  # another producer's generation invalidates only the cache
        row.sync_source("docs", "incremental")
        assert row.graph_store.published_generation("parity")["generation_id"] == generation
        assert len(catalog_reads) == 1, "a warm file edit reread the catalogue"
    finally:
        resident.close()
        row.close()


def test_hydration_is_read_only_and_parallel_edges_survive(tmp_path):
    engine = SyncEngine(_config(tmp_path / "state", tmp_path))
    rows = engine.graph_store.rows
    rows.apply_delta(
        "parity",
        node_upserts=[
            ("a", {"id": "a", "type": "entity"}),
            ("b", {"id": "b", "type": "entity"}),
            ("c", {"id": "c", "type": "entity"}),
        ],
        edge_upserts=[("a", "b", 0, {"type": "mentions"}), ("a", "b", 1, {"type": "about"})],
    )
    rows.publish("parity")
    graph = RowWorkingSet(rows, "parity")
    try:
        assert graph.has_node("a")
        assert len(graph.out_edges("a")[0][2]) == 2
        assert not any(graph.graph_delta().values())
        graph.remove_edges_where("a", "b", lambda attrs: attrs["type"] == "mentions")
        engine.graph_store.save("parity", graph)
        persisted = rows.out_edges("parity", ["a"])["a"][0][2]
        assert [a["type"] for a in persisted.values()] == ["about"]
        graph.add_edge("a", "b", type="calls")
        engine.graph_store.save("parity", graph)
        persisted = rows.out_edges("parity", ["a"])["a"][0][2]
        assert {a["type"] for a in persisted.values()} == {"about", "calls"}
        assert graph.release_clean_cache()
        assert not graph._nodes
        assert {node["id"] for node in graph.to_node_link()["nodes"]} == {"a", "b", "c"}
    finally:
        engine.close()


@pytest.mark.parametrize("corpus", ["markdown", "python", "structured", "opaque"])
def test_row_memory_bridging_matches_resident_without_graph_walk(tmp_path, monkeypatch, corpus):
    from pheasant.config.loader import load_config
    from pheasant.memory.store import MemoryStore
    from tests.test_memory_graph import _config as memory_config

    path = memory_config(tmp_path, corpus=corpus)
    MemoryStore(tmp_path / "memory").append(
        "See [the runbook](runbook.md). SyncEngine runs the Kestrel Gateway. Term and Termination.",
        scope="org",
    )
    config = load_config(path)
    config.storage.graph_snapshots = False
    resident = SyncEngine(config)
    try:
        resident.sync_source("corpus", "full")
        resident.sync_source("agent-memory", "full")
        expected = _content(resident)
    finally:
        resident.close()
    row = SyncEngine(load_config(path), defer_persisted_graph_load=True)
    row.config.storage.graph_snapshots = False
    monkeypatch.setattr(row.graph_store, "load", lambda *_: pytest.fail("whole graph loaded"))
    for name in ("iter_nodes", "iter_edges"):
        monkeypatch.setattr(RowWorkingSet, name, lambda *_: pytest.fail("whole graph walked"))
    try:
        row._ensure_persisted_graph_loaded()
        report = row.graph_builder.add_memory_edges(row.state)
        assert report["records"] == 1
        row.graph_store.save(row.config.knowledge_base_id, row.graph_builder.graph)
        assert _content(row) == expected
    finally:
        row.close()


def test_returned_failed_result_discards_the_writer(monkeypatch, capsys):
    import io
    import sys
    from types import SimpleNamespace

    from pheasant.sync import writer

    calls = []

    class Engine:
        def __init__(self, *args, **kwargs):
            self.lease = SimpleNamespace(wait_timeout_s=0)
            self._graph_loaded = False
            self.graph_builder = SimpleNamespace(graph=SimpleNamespace())

        def apply_index_task(self, *args, **kwargs):
            calls.append("task")
            return SyncResult("docs", 0, 0, 0, 0, "failed", {"error": "read failed"})

        def close(self):
            calls.append("closed")

    from pheasant.sync.engine import SyncResult

    request = {"source": "docs", "payload": {}, "mode": "incremental", "full_scan": False}
    monkeypatch.setattr("pheasant.sync.engine.SyncEngine", Engine)
    monkeypatch.setattr("pheasant.config.loader.load_config", lambda _: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO((json.dumps(request) + "\n") * 2))
    writer.serve("unused.yaml")
    report = json.loads(capsys.readouterr().out)["report"]
    assert report["status"] == "failed"
    assert calls == ["task", "closed"]
