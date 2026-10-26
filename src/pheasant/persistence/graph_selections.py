"""What a sync process reads instead of loading the graph, and a snapshot as text.

:class:`~pheasant.graph.store_backed.StoreBackedGraph` -- every ``pheasant
sync`` child's working set on the row backend -- reads the store a selection
at a time through :class:`WorkingSetReads`. Every answer is materialized (the
builder mutates what it reads) and grouped by endpoint pair in key order,
which is the unit a delta replaces and the order a whole-graph load would
have seen. :meth:`WorkingSetReads.node_link_text` is the same store read as
the snapshot a process without the graph writes.

Beside :class:`~pheasant.persistence.graph_rows.GraphRowStore` rather than in
it, the way :mod:`pheasant.persistence.graph_candidates` is: the row store is
the write path and the serving reads, and these are a third consumer's.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

from pheasant.persistence.graph_codec import (
    EDGE_KEY,
    PROMOTED_NODE_KEYS,
    _batched,
    edge_attrs_dict,
    node_attrs_dict,
)
from pheasant.persistence.graph_rows import _EDGE_READ, _NODE_READ, GraphRowStore


class WorkingSetReads:
    """The selections a store-backed working set makes, over one row store."""

    def __init__(self, rows: GraphRowStore) -> None:
        self.rows = rows

    def iter_nodes(self, kb_id: str) -> Iterator[tuple[str, dict[str, Any]]]:
        return self.rows.iter_nodes(kb_id)

    def iter_edges(self, kb_id: str) -> Iterator[tuple[tuple[str, str], dict[int, dict]]]:
        return self.rows.iter_edges(kb_id)

    def select_nodes(
        self,
        kb_id: str,
        *,
        types: Iterable[str] | None = None,
        source_id: str | None = None,
        ids: Iterable[str] | None = None,
        artifact_ids: Iterable[str] | None = None,
        promoted_only: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Nodes matching every filter given, ``{node_id: attrs}`` in key order.

        ``promoted_only`` returns the promoted columns and skips the blob, for
        a caller that reads nothing else: the memory bridge asks this of every
        entity in the region, and decoding 45k blobs it never reads was most of
        a sync's linking time.

        Each filter is a promoted column, so each is a predicate the database
        can answer without decoding a blob: ``source_id`` and ``type`` lead an
        index, ``ids`` is the primary key. ``artifact_id`` has no index of its
        own and is a scan, which is acceptable for the one caller that asks
        (memory maintenance retiring a handful of records).
        """

        dialect = self.rows.state.dialect
        where = ["kb_id=?"]
        params: list[Any] = [kb_id]
        if source_id is not None:
            where.append("source_id=?")
            params.append(source_id)
        if types is not None:
            wanted = sorted(set(types))
            if not wanted:
                return {}
            clause, values = dialect.in_clause("type", wanted)
            where.append(clause)
            params.extend(values)
        keyed: list[tuple[str, list[str]]] = []
        if ids is not None:
            keyed.append(("node_id", sorted(set(ids))))
        if artifact_ids is not None:
            keyed.append(("artifact_id", sorted(set(artifact_ids))))
        found: dict[str, dict[str, Any]] = {}
        if promoted_only:
            select = ", ".join(("node_id", *PROMOTED_NODE_KEYS))

            def decode(row: Any) -> dict[str, Any]:
                return {key: row[key] for key in PROMOTED_NODE_KEYS if row[key] is not None}
        else:
            select, decode = "*", node_attrs_dict
        if not keyed:
            statement = (
                f"SELECT {select} FROM graph_nodes WHERE {' AND '.join(where)} ORDER BY node_id"
            )
            for row in self.rows.state.rows(statement, tuple(params)):
                found[str(row["node_id"])] = decode(row)
            return found
        for column, values in keyed:
            for batch in _batched(values):
                if not batch:
                    continue
                clause, batch_params = dialect.in_clause(column, batch)
                statement = (
                    f"SELECT {select} FROM graph_nodes WHERE {' AND '.join(where)} AND {clause}"
                    " ORDER BY node_id"
                )
                for row in self.rows.state.rows(statement, (*params, *batch_params)):
                    found[str(row["node_id"])] = decode(row)
        return dict(sorted(found.items()))

    def existing_node_ids(self, kb_id: str, ids: Iterable[str]) -> set[str]:
        """Which of ``ids`` the store holds, without decoding any of them."""

        held: set[str] = set()
        for batch in _batched(sorted(set(ids))):
            if not batch:
                continue
            clause, params = self.rows.state.dialect.in_clause("node_id", batch)
            for row in self.rows.state.rows(
                f"SELECT node_id FROM graph_nodes WHERE kb_id=? AND {clause}",
                (kb_id, *params),
            ):
                held.add(str(row["node_id"]))
        return held

    def _edge_pairs(self, statement: str, params: tuple) -> dict[tuple[str, str], list[dict]]:
        grouped: dict[tuple[str, str], list[dict]] = {}
        for row in self.rows.state.rows(statement, params):
            pair = (str(row["source"]), str(row["target"]))
            grouped.setdefault(pair, []).append(edge_attrs_dict(row))
        return grouped

    def out_edge_pairs(
        self,
        kb_id: str,
        sources: Iterable[str],
        targets: Iterable[str] | None = None,
    ) -> dict[tuple[str, str], list[dict]]:
        """Every edge of every pair leaving ``sources`` (landing in ``targets``).

        A pair is always returned whole — all of its parallel edges — because
        it is the unit a delta replaces: a working set holding half a pair
        would write the other half out of existence.
        """

        dialect = self.rows.state.dialect
        source_list = sorted(set(sources))
        target_batches: list[list[str] | None] = (
            [None]
            if targets is None
            else [batch for batch in _batched(sorted(set(targets))) if batch]
        )
        grouped: dict[tuple[str, str], list[dict]] = {}
        for batch in _batched(source_list):
            if not batch:
                continue
            source_clause, source_params = dialect.in_clause("source", batch)
            for target_batch in target_batches:
                where = f"kb_id=? AND {source_clause}"
                params: list[Any] = [kb_id, *source_params]
                if target_batch is not None:
                    target_clause, target_params = dialect.in_clause("target", target_batch)
                    where += f" AND {target_clause}"
                    params.extend(target_params)
                grouped.update(
                    self._edge_pairs(
                        f"SELECT {', '.join(_EDGE_READ)} FROM graph_edges WHERE {where}"
                        " ORDER BY source, target, type, seq",
                        tuple(params),
                    )
                )
        return grouped

    def edge_pairs_into_types(
        self, kb_id: str, target_types: Iterable[str]
    ) -> dict[tuple[str, str], list[dict]]:
        """Every pair whose target node has one of ``target_types``, whole."""

        wanted = sorted(set(target_types))
        if not wanted:
            return {}
        clause, params = self.rows.state.dialect.in_clause("n.type", wanted)
        columns = ", ".join(f"e.{column}" for column in _EDGE_READ)
        return self._edge_pairs(
            f"SELECT {columns} FROM graph_edges e JOIN graph_nodes n"
            " ON n.kb_id = e.kb_id AND n.node_id = e.target"
            f" WHERE e.kb_id=? AND {clause} ORDER BY e.source, e.target, e.type, e.seq",
            (kb_id, *params),
        )

    def pairs_into(self, kb_id: str, targets: Iterable[str]) -> set[tuple[str, str]]:
        """Endpoint pairs landing on ``targets``, off ``idx_graph_edges_target``."""

        found: set[tuple[str, str]] = set()
        for batch in _batched(sorted(set(targets))):
            if not batch:
                continue
            clause, params = self.rows.state.dialect.in_clause("target", batch)
            for row in self.rows.state.rows(
                f"SELECT DISTINCT source, target FROM graph_edges WHERE kb_id=? AND {clause}",
                (kb_id, *params),
            ):
                found.add((str(row["source"]), str(row["target"])))
        return found

    def edge_pairs_containing(
        self, kb_id: str, sources: Iterable[str], needle: str
    ) -> dict[tuple[str, str], list[dict]]:
        """Edges leaving ``sources`` whose attribute JSON contains ``needle``.

        Only the matching edges of a pair, not the pair: this answers "which
        edges did a pass draw", never "what does this pair hold". ``needle``
        is a ``"key":value`` fragment of the canonical encoding
        (:func:`~pheasant.persistence.graph_codec._canonical` sorts keys and
        drops whitespace), so a substring test is an exact test of one
        attribute's value; a string value cannot fake it, because its own
        quotes are escaped. ``instr``/``strpos`` rather than ``LIKE``, which
        folds case on SQLite and not on Postgres.
        """

        contains = "strpos(attrs, ?) > 0" if self.rows._postgres else "instr(attrs, ?) > 0"
        grouped: dict[tuple[str, str], list[dict]] = {}
        for batch in _batched(sorted(set(sources))):
            if not batch:
                continue
            clause, params = self.rows.state.dialect.in_clause("source", batch)
            grouped.update(
                self._edge_pairs(
                    f"SELECT {', '.join(_EDGE_READ)} FROM graph_edges"
                    f" WHERE kb_id=? AND {clause} AND {contains}"
                    " ORDER BY source, target, type, seq",
                    (kb_id, *params, needle),
                )
            )
        return grouped

    def node_link_text(self, kb_id: str) -> Iterator[str]:
        """The whole graph as node-link JSON text, assembled from the rows as stored.

        For a snapshot, which is history read back with ``json.loads`` and
        nothing else. The ``attrs`` column is already canonical JSON, so an
        object is its promoted columns spliced in front of that text: nothing
        is decoded into Python and nothing re-encoded. Materializing the same
        document as dicts and dumping it measured 39-52 s on a 123k-node /
        762k-edge region; this is what made reading rows back cheap enough for
        a sync process that does not hold the graph.

        A column a blob also carried would come *after* the promoted value,
        and ``json.loads`` keeps the last -- the same precedence
        ``{**promoted, **attrs}`` gives a dict. Nothing writes one: ``node_row``
        and ``edge_row`` strip the promoted keys from the blob.
        """

        # The C function under `json.dumps` for a string, without the encoder
        # object `dumps` builds per call -- three million of those were most of
        # the cost. Same output, since `dumps` defaults to `ensure_ascii`, and
        # every value spliced here is a TEXT column: a string or NULL.
        quote = json.encoder.encode_basestring_ascii
        node_keys = [(column, quote(column) + ":") for column in PROMOTED_NODE_KEYS]

        def splice(head: str, attrs: str | None) -> str:
            body = attrs or "{}"
            return head + ("," + body[1:] if len(body) > 2 else "}")

        yield '{"directed":true,"graph":{},"links":['
        separator = ""
        current: tuple[str, str] | None = None
        key = 0
        batch: list[str] = []
        for row in self.rows._stream("graph_edges", EDGE_KEY, kb_id, _EDGE_READ):
            source, target = str(row["source"]), str(row["target"])
            key = key + 1 if (source, target) == current else 0
            current = (source, target)
            head = (
                separator
                + '{"source":'
                + quote(source)
                + ',"target":'
                + quote(target)
                + ',"key":'
                + str(key)
                + ',"type":'
                + quote(str(row["type"]))
            )
            if row["source_id"] is not None:
                head += ',"source_id":' + quote(str(row["source_id"]))
            batch.append(splice(head, row["attrs"]))
            separator = ","
            if len(batch) >= self.rows.PAGE:
                yield "".join(batch)
                batch = []
        yield "".join(batch) + '],"multigraph":true,"nodes":['
        separator, batch = "", []
        for row in self.rows._stream("graph_nodes", ("node_id",), kb_id, _NODE_READ):
            promoted = ",".join(
                prefix + quote(str(row[column]))
                for column, prefix in node_keys
                if row[column] is not None
            )
            if promoted:
                batch.append(splice(separator + "{" + promoted, row["attrs"]))
            else:
                batch.append(separator + (row["attrs"] or "{}"))
            separator = ","
            if len(batch) >= self.rows.PAGE:
                yield "".join(batch)
                batch = []
        yield "".join(batch) + "]}"
