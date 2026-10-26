"""Rows to attributes and back, and the digest that names a generation.

The translation half of the row-backed graph, split from
:mod:`pheasant.persistence.graph_rows` when that module outgrew the size
budget: this is a codec — pure functions over one row — and the store is a
reader and writer. They change for different reasons, and only this half is
what :mod:`pheasant.graph.sql` and the tests reach for directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Iterator, MutableMapping
from typing import Any

logger = logging.getLogger(__name__)

#: Attributes promoted out of ``attrs`` into their own column. Chosen because
#: something filters or joins on each of them — not to make rows look tidy.
#: They are stored once: :func:`node_row` removes them from the JSON and
#: :func:`node_attrs` merges them back, so the projection lives in one place.
PROMOTED_NODE_KEYS = ("type", "label", "source_id", "relative_path", "artifact_id")

#: Same, for an edge. ``type`` and ``seq`` are part of the primary key.
PROMOTED_EDGE_KEYS = ("type", "source_id")

#: The composite key of ``graph_edges``, in its declared order. Named once
#: because it is both the primary key and the order every streaming read
#: pages by, and those two agreeing is what makes the paging a range scan.
EDGE_KEY = ("source", "target", "type", "seq")

#: Rows per statement. The same figure ``persistence/migrate.py`` uses, for the
#: same reason: a million-row table should not be a million round trips, and a
#: parameter list should not be measured in gigabytes.
BATCH = 1_000

#: Endpoint *sources* per statement when addressing edges by exact pair.
#:
#: Deliberately far smaller than :data:`BATCH`. Addressing pairs needs an
#: ``OR`` chain — ``source IN (…) AND target IN (…)`` is a cross product, which
#: over-matches, and over-matching here would fold out digests that were never
#: replaced and delete edges nobody touched. An ``OR`` chain nests one level
#: per term, and SQLite refuses an expression tree deeper than 1000 outright:
#: batching pairs at ``BATCH`` raised "Expression tree is too large" on the
#: first real graph. Grouping the targets under each source keeps the chain
#: proportional to *distinct sources*, and this caps that.
PAIR_GROUP_BATCH = 100

#: Targets listed per source inside one group. An ``IN`` list is one node in
#: the tree however long it is, so this bounds the parameter count rather than
#: the depth.
PAIR_TARGET_BATCH = 400

#: Hex digits of SHA-256 kept per row. 128 bits is far past what a collision
#: between two versions of one region's node needs, and the fold is stored as
#: text so it reads in a `SELECT` and survives both dialects without a bytea
#: round trip.
DIGEST_HEX = 32

_ZERO_FOLD = "0" * DIGEST_HEX


def _canonical(payload: dict[str, Any]) -> str:
    """Deterministic JSON for hashing and for the ``attrs`` column.

    Key-sorted and separator-compact, exactly as the file backend serialized
    the whole graph — so a node's bytes here are the bytes it had there, and
    the digest of an unchanged node does not depend on which backend wrote it.
    """

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _digest(*parts: str) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part.encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()[:DIGEST_HEX]


def fold(current: str, *digests: str) -> str:
    """XOR ``digests`` into ``current``.

    Its own inverse, which is the whole reason the id can be maintained
    incrementally: folding a digest in twice is folding it out.
    """

    value = int(current or _ZERO_FOLD, 16)
    for item in digests:
        value ^= int(item, 16)
    return format(value, f"0{DIGEST_HEX}x")


def node_row(kb_id: str, node_id: str, attrs: dict[str, Any]) -> tuple:
    """One ``graph_nodes`` row, with its digest, from a node's attributes."""

    rest = {key: value for key, value in attrs.items() if key not in PROMOTED_NODE_KEYS}
    encoded = _canonical(rest)
    promoted = [_text(attrs.get(key)) for key in PROMOTED_NODE_KEYS]
    digest = _digest(node_id, *[value or "" for value in promoted], encoded)
    return (kb_id, node_id, *promoted, encoded, digest)


class _LazyAttrs(MutableMapping):
    """A row's attributes, with the JSON column parsed only if something asks.

    The promoted columns — a node's ``type``/``label``/``source_id``, an edge's
    ``type`` — are exactly the ones the hot paths read, and they are *not* in
    the JSON blob. So parsing it up front is work done for nothing on every
    traversal hop and every scored edge, and a hub node makes that enormous: a
    depth-3 walk over one ``source`` node that indexes 8,000 artifacts ran
    **8,040 `json.loads` calls, 42% of the walk**, to read `type` 8,040 times.

    Behaves as a complete mapping — anything that iterates, expands with
    ``**`` or mutates gets the parsed dict, because those callers genuinely
    need all of it. Only the point lookups the walks and the scorer make stay
    cheap, which is the whole difference.
    """

    __slots__ = ("_promoted", "_encoded", "_parsed")

    def __init__(self, promoted: dict[str, Any], encoded: str | None) -> None:
        self._promoted = promoted
        self._encoded = encoded
        self._parsed: dict[str, Any] | None = None

    def _materialize(self) -> dict[str, Any]:
        if self._parsed is None:
            merged = dict(json.loads(self._encoded or "{}"))
            merged.update(self._promoted)
            self._parsed = merged
        return self._parsed

    def __getitem__(self, key: str) -> Any:
        if self._parsed is None and key in self._promoted:
            return self._promoted[key]
        return self._materialize()[key]

    def get(self, key: str, default: Any = None) -> Any:
        if self._parsed is None and key in self._promoted:
            return self._promoted[key]
        return self._materialize().get(key, default)

    def __setitem__(self, key: str, value: Any) -> None:
        self._materialize()[key] = value

    def __delitem__(self, key: str) -> None:
        del self._materialize()[key]

    def __iter__(self):
        return iter(self._materialize())

    def __len__(self) -> int:
        return len(self._materialize())

    def __repr__(self) -> str:
        return repr(self._materialize())


def node_attrs(row: Any) -> Any:
    """Rebuild a node's attribute dict from its row, lazily.

    The inverse of :func:`node_row`, and the *only* inverse: a caller that
    reads ``row["attrs"]`` directly gets a node missing its type and label.
    Promoted columns win over the JSON so a row written by an older schema
    cannot shadow them — which is also why they can be served without parsing
    it at all. See :class:`_LazyAttrs`.

    For a caller that is going to read *every* attribute, use
    :func:`node_attrs_dict` instead — laziness is a cost there, not a saving.
    """

    promoted = {key: row[key] for key in PROMOTED_NODE_KEYS if row[key] is not None}
    return _LazyAttrs(promoted, row["attrs"])


def node_attrs_dict(row: Any) -> dict[str, Any]:
    """The same attributes, materialized in one step.

    :class:`_LazyAttrs` pays off for point reads — a traversal hop reads
    ``type`` and nothing else. It is a *loss* for a caller that reads all of
    them, and the loss is not the JSON parse (which such a caller owes
    anyway): it is that ``dict(lazy)`` goes through the mapping protocol, one
    Python-level ``__getitem__`` per key. The graph search arm does exactly
    that for every candidate — measured **8.0µs per node against 3.75µs**, and
    1,836 candidates a query, so 15ms of a query spent converting a mapping
    into the dict it was already holding.

    Same result as ``dict(node_attrs(row))``, asserted in
    ``tests/test_graph_backends.py``; this is the shape, not a shortcut.
    """

    merged: dict[str, Any] = json.loads(row["attrs"] or "{}")
    for key in PROMOTED_NODE_KEYS:
        value = row[key]
        if value is not None:
            merged[key] = value
    return merged


def edge_row(kb_id: str, source: str, target: str, seq: int, attrs: dict[str, Any]) -> tuple:
    """One ``graph_edges`` row, with its digest."""

    rest = {key: value for key, value in attrs.items() if key not in PROMOTED_EDGE_KEYS}
    encoded = _canonical(rest)
    edge_type = _text(attrs.get("type")) or "related"
    source_id = _text(attrs.get("source_id"))
    digest = _digest(source, target, edge_type, str(seq), source_id or "", encoded)
    return (kb_id, source, target, edge_type, int(seq), source_id, encoded, digest)


def edge_attrs(row: Any) -> Any:
    """Rebuild an edge's attribute dict from its row. See :class:`_LazyAttrs`."""

    promoted = {"type": row["type"]}
    if row["source_id"] is not None:
        promoted["source_id"] = row["source_id"]
    return _LazyAttrs(promoted, row["attrs"])


def edge_attrs_dict(row: Any) -> dict[str, Any]:
    """The same attributes, materialized in one step. See :func:`node_attrs_dict`.

    ``dict(edge_attrs(row))`` goes through the mapping protocol per key, and
    a whole-graph load does it 761k times: ~8 s of the indexer's working-set
    load at 123k nodes, for the dict it was already holding.
    """

    merged: dict[str, Any] = json.loads(row["attrs"] or "{}")
    merged["type"] = row["type"]
    source_id = row["source_id"]
    if source_id is not None:
        merged["source_id"] = source_id
    return merged


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _batched(items: list, size: int = BATCH) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _pair_clauses(kb_id: str, pairs: Iterable[tuple[str, str]]) -> Iterator[tuple[str, list[Any]]]:
    """``(sql, params)`` fragments addressing an exact set of endpoint pairs.

    Grouped by source and capped twice — see :data:`PAIR_GROUP_BATCH`. Yields
    nothing for an empty set, which is what lets both callers skip the
    statement entirely rather than build ``WHERE ... AND ()``.

    **``kb_id`` is repeated inside every term, and that is the whole point.**
    Factoring it out to ``WHERE kb_id=? AND (… OR …)`` reads better and costs
    a full table scan: SQLite's multi-index-OR optimization needs each branch
    to be independently indexable, and ``source=?`` alone is not a prefix of
    ``(kb_id, source, target, type, seq)``. ``EXPLAIN QUERY PLAN`` says
    ``SEARCH … USING INDEX idx_graph_edges_target (kb_id=?)`` for the tidy
    form and ``MULTI-INDEX OR`` with exact primary-key lookups for this one.
    Measured: 300ms per incremental commit at 100k files versus 2ms, and the
    tidy form was O(total graph) — which would have made the row backend cost
    what the file backend cost, for the same reason, one layer down.
    """

    grouped: dict[str, list[str]] = {}
    for source, target in sorted(pairs):
        grouped.setdefault(source, []).append(target)
    groups: list[tuple[str, list[str]]] = []
    for source, targets in grouped.items():
        for chunk in _batched(targets, PAIR_TARGET_BATCH):
            groups.append((source, chunk))
    for batch in _batched(groups, PAIR_GROUP_BATCH):
        clause = " OR ".join(
            f"(kb_id=? AND source=? AND target IN ({','.join('?' for _ in targets)}))"
            for _source, targets in batch
        )
        params: list[Any] = []
        for source, targets in batch:
            params.extend((kb_id, source))
            params.extend(targets)
        yield clause, params


def _endpoint_clauses(kb_id: str, node_ids: list[str]) -> Iterator[tuple[str, list[Any]]]:
    """``(sql, params)`` fragments for every edge touching one of ``node_ids``.

    Two indexable branches per node rather than one ``OR`` between two ``IN``
    lists, for the reason :func:`_pair_clauses` spells out: the out-edge branch
    rides the primary key and the in-edge branch rides
    ``idx_graph_edges_target``, and neither can be reached with ``kb_id``
    hoisted out of the ``OR``.
    """

    for batch in _batched(node_ids, PAIR_GROUP_BATCH):
        placeholders = ",".join("?" for _ in batch)
        clause = (
            f"(kb_id=? AND source IN ({placeholders})) OR (kb_id=? AND target IN ({placeholders}))"
        )
        yield clause, [kb_id, *batch, kb_id, *batch]
