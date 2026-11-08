"""Indexes added after their tables shipped, each applied once per region.

`CREATE INDEX IF NOT EXISTS` in the core schema never reaches an upgraded
Postgres region (the schema-version marker skips the DDL), so an index that
existing regions need is created here, behind a marker of its own, from both
migration paths. Split out of :mod:`.state_store` because each is derived DDL
over tables that are the truth -- no row is touched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

#: The edge types a bounded walk ranks first (`graph.traversal.HIERARCHY_EDGE_TYPES`),
#: spelled here because persistence cannot import the graph layer. The
#: partial index's predicate must match the query's literally, or neither
#: planner uses it; `tests/test_graph_backends.py` holds the two equal.
HIERARCHY_EDGE_TYPE = "contains"


def migrate_derived_indexes(store: Any) -> None:
    """Every derived index, each once. Safe to call on every boot."""

    _artifact_terms_node_lookup(store)
    _graph_edges_hierarchy(store)


def _applied(store: Any, marker: str) -> bool:
    return bool(store.rows("SELECT value FROM pheasant_schema_meta WHERE key=?", (marker,)))


def _record(store: Any, marker: str) -> None:
    store.conn.execute(
        "INSERT INTO pheasant_schema_meta(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (marker, "1", datetime.now(UTC).isoformat()),
    )
    store.conn.commit()


def _artifact_terms_node_lookup(store: Any) -> None:
    """Replace the legacy wide artifact-term B-tree index once.

    ``node_id`` is a parser-produced text identifier, not a bounded key.
    PostgreSQL rejects a B-tree row over one third of a page, which used to
    turn one unusually long heading or symbol into a failed whole-source
    sync. The old index serves only retired historical concept rollups;
    the replacement retains their type/artifact filter without indexing an
    unbounded value. This changes derived DDL only, never term rows.
    """

    marker = "artifact_terms_node_lookup_v2"
    if _applied(store, marker):
        return
    store.conn.execute("DROP INDEX IF EXISTS idx_artifact_terms_node_lookup")
    store.conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_artifact_terms_type_artifact "
        "ON artifact_terms(node_type, artifact_id)"
    )
    _record(store, marker)


def _graph_edges_hierarchy(store: Any) -> None:
    """A partial index over the hierarchy edges, for the bounded walk's first scan.

    A bounded frontier asks each source for its first hierarchy targets
    (`graph_frontier.bounded_rows`). The primary key leads ``(kb_id, source,
    target)``, so that scan read a source's *whole* key range to find them,
    and an entity mentioned by every page of a 25k-page web source has none:
    4,396 of the 13,465 pages one 7-hub fetch touched, every one of them a
    separate read off the Azure fleet's network share when cold. This index
    holds only hierarchy edges, so the same scan reads a page or two.
    """

    marker = "graph_edges_hierarchy_v1"
    if _applied(store, marker):
        return
    store.conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_graph_edges_hierarchy "
        f"ON graph_edges(kb_id, source, target) WHERE type = '{HIERARCHY_EDGE_TYPE}'"
    )
    _record(store, marker)
