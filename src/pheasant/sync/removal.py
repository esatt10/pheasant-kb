"""Targeted row-backed removals, without materializing unrelated knowledge."""

from __future__ import annotations

from typing import Any

from pheasant.persistence.graph_codec import node_attrs_dict


def source(engine: Any, name: str) -> None:
    rows = engine.graph_store.rows
    kb = engine.config.knowledge_base_id
    ids = [
        str(r["node_id"])
        for r in engine.state.rows(
            "SELECT node_id FROM graph_nodes WHERE kb_id=? AND source_id=?", (kb, name)
        )
    ]
    _drop(engine, ids)
    # Source-type hubs have no owner; remove one only after its last source.
    structural = engine.state.rows(
        "SELECT * FROM graph_nodes WHERE kb_id=? AND type IN ('source','source_type')", (kb,)
    )
    nodes = [(str(r["node_id"]), node_attrs_dict(r)) for r in structural]
    active = {a.get("source_type") for _, a in nodes if a.get("type") == "source"}
    _drop(
        engine,
        [
            n
            for n, a in nodes
            if a.get("type") == "source_type" and a.get("source_type") not in active
        ],
    )
    metadata = rows.publish(kb)
    engine.graph_store._announce(kb, metadata)


def documents(engine: Any, name: str, gone: list[tuple[str, str, str | None]]) -> None:
    """Retract owned nodes, empty directory ancestors and unshared enrichment."""
    rows = engine.graph_store.rows
    kb = engine.config.knowledge_base_id
    ids = [a for a, _, _ in gone]
    owned = set(ids)
    for start in range(0, len(ids), 200):
        clause, params = engine.state.dialect.in_clause("artifact_id", ids[start : start + 200])
        owned.update(
            str(r["node_id"])
            for r in engine.state.rows(
                f"SELECT node_id FROM graph_nodes WHERE kb_id=? AND {clause} "
                "AND type IN ('chunk','heading','symbol')",
                (kb, *params),
            )
        )
    edges = rows.out_edges(kb, ids)
    targets = {b for group in edges.values() for _, b, _ in group}
    shared = {
        n
        for n, a in rows.get_nodes(kb, targets).items()
        if a.get("type") in {"external_reference", "symbol", "entity"} and n not in owned
    }
    _drop(engine, sorted(owned))
    directories = set()
    for _, path, branch in gone:
        parts = path.replace("\\", "/").split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            directories.add(f"directory:{name}:{'/'.join(parts[:depth])}:branch={branch or 'none'}")
    for directory in sorted(directories, key=lambda d: d.count("/"), reverse=True):
        if not rows.out_edges(kb, [directory]).get(directory):
            _drop(engine, [directory])
    for node in sorted(shared):
        if not engine.state.rows(
            "SELECT 1 FROM graph_edges WHERE kb_id=? AND (source=? OR target=?) LIMIT 1",
            (kb, node, node),
        ):
            _drop(engine, [node])
    engine.graph_store._announce(kb, rows.publish(kb))


def _drop(engine: Any, ids: list[str]) -> None:
    if not ids:
        return
    engine.graph_store.rows.apply_delta(engine.config.knowledge_base_id, node_removals=ids)
    engine.node_index.apply([], ids)
    if engine._graph_loaded:
        graph = engine.graph_builder.graph
        graph.remove_nodes_from(ids)
        # The row deletion above is already durable. Clear only that delta,
        # never let a cached writer fold the same removals out a second time.
        graph.clear_graph_delta(graph.graph_delta())
        graph.take_index_delta()


def apply_source(engine: Any, source_name: str, on_progress: Any = None) -> Any:
    import shutil

    from pheasant.artifacts.source_removal import (
        reconcile_after_source_removal,
        retire_source_memories,
    )
    from pheasant.ingestion.landing import owned_upload_directory
    from pheasant.sync.engine import SyncResult, _progress_reporter

    report = _progress_reporter(on_progress, source_name)
    report("claimed", 0, 1, f"removing {source_name}")
    with engine._source_write_lock(source_name):
        with engine._sync_mutex:
            registered_source = engine.state.get_source(source_name)
            uploaded_files = (
                owned_upload_directory(
                    engine.paths.state,
                    str(registered_source["name"]),
                    str(registered_source["type"]),
                    str(registered_source["path"]),
                )
                if registered_source
                else None
            )
            if engine.graph_store.rows is not None:
                engine.checkpoint_graph()
                retire_source_memories(engine, source_name)
                source(engine, source_name)
            else:
                engine._ensure_persisted_graph_loaded()
                retire_source_memories(engine, source_name)
                engine.graph_builder.remove_source_content(source_name)
                engine.flush_node_index()
                engine.graph_store.save(
                    engine.config.knowledge_base_id,
                    engine.graph_builder.graph,
                )
            # Prune vectors after graph publication so retries finish cleanup.
            if engine.vectors is not None:
                engine.vectors.prune_source(source_name, set())
                engine.vectors.flush()
            # Delete only upload-owned bytes; leave ordinary source files.
            if uploaded_files is not None and uploaded_files.exists():
                shutil.rmtree(uploaded_files)
            engine.manifests.delete(source_name)
            engine.state.delete_source(source_name)
            engine.config.sources = [s for s in engine.config.sources if s.name != source_name]
    reconcile_after_source_removal(engine, source_name)
    nodes, edges = engine._graph_counts()
    report("saving", 1, 1, f"removed {source_name}")
    return SyncResult(source_name, 0, 0, nodes, edges, "removed")
