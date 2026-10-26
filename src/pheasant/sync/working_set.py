"""Which working set a sync process indexes into, and what follows from it.

A ``pheasant sync`` child -- every out-of-process sync, which is every sync a
server runs -- used to load the whole graph before its first commit. On the
row backend it now indexes into a
:class:`~pheasant.graph.store_backed.StoreBackedGraph`, which reads the store a
selection at a time and writes the same delta. Two things that assumed a whole
graph in memory follow it here, so ``engine.py`` carries one line for each:

* the search index's wholesale rebuild (a delta past
  ``SyncEngine._INDEX_REBUILD_THRESHOLD``) is built from the *saved* rows,
  after the commit, rather than from a graph this process does not hold;
* counts come from the store's maintained row, not from a walk.

Serving is untouched: a process holding a store-backed working set serves from
the store (``SyncEngine.serving_graph``), exactly as an api replica does, and
a standalone server keeps serving the resident graph it always loaded.
"""

from __future__ import annotations

import logging
from typing import Any

from pheasant.graph.simple import _search_blob
from pheasant.graph.store_backed import StoreBackedGraph
from pheasant.persistence.graph_selections import WorkingSetReads

logger = logging.getLogger(__name__)


def adopt_store_backed_graph(engine: Any) -> StoreBackedGraph:
    """Give the engine's builder a working set over the store, and say so."""

    kb_id = engine.config.knowledge_base_id
    graph = StoreBackedGraph(WorkingSetReads(engine.graph_store.rows), kb_id)
    engine.graph_builder.graph = graph
    # A fresh region has no root yet. A resident load kept the one the builder
    # made at construction for exactly this case; an existing root is the
    # store's, and re-asserting it here would be a write for nothing.
    if not graph.has_node(kb_id):
        engine.graph_builder.upsert_node(kb_id, "knowledge_base", kb_id, {})
    # Not `_graph_loaded`: that says the whole graph is in memory, and the
    # point of this graph is that it never is.
    engine._note_loaded_generation()
    return graph


def rebuild_node_index_from_store(engine: Any) -> int:
    """The search index rebuilt from the saved rows, which is the whole graph.

    What :meth:`SyncEngine.rebuild_node_index` does from a resident graph,
    read off the store instead: the same ``(node_id, source_id, body)`` rows,
    because the body is the same function of the same attributes. Only correct
    after the delta that made it necessary has been committed, which is why the
    engine runs it from the publish hook.
    """

    kb_id = engine.config.knowledge_base_id
    rows = (
        (node_id, str(attrs.get("source_id") or ""), _search_blob(attrs))
        for node_id, attrs in engine.graph_store.rows.iter_nodes(kb_id)
    )
    written = engine.node_index.replace_all(rows)
    # Everything pending is in the rows that were just indexed.
    engine.graph_builder.graph.take_index_delta()
    logger.info("Graph node index rebuilt from saved rows: %d nodes", written)
    return written
