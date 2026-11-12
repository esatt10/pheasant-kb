"""Atomically stream one consistent row generation into a standard graph snapshot."""

from __future__ import annotations

import json
import os
from itertools import groupby
from typing import Any

from pheasant.persistence.graph_codec import edge_attrs_dict, node_attrs_dict
from pheasant.persistence.graph_store import _tmp_for


class SnapshotCancelled(Exception):
    pass


def _rows(state: Any, table: str, keys: tuple[str, ...], kb: str, cancelled: Any):
    # Stay on this thread's pinned transaction; the ordinary graph prefetcher
    # uses another connection, which is unsuitable for a consistent snapshot.
    after = None
    while True:
        if cancelled.is_set():
            raise SnapshotCancelled()
        where, params = "kb_id=?", (kb,)
        if after is not None:
            where += f" AND ({','.join(keys)}) > ({','.join('?' for _ in keys)})"
            params += after
        page = state.rows(
            f"SELECT * FROM {table} WHERE {where} ORDER BY {','.join(keys)} LIMIT ?",
            (*params, 2000),
        )
        if not page:
            return
        for row in page:
            if cancelled.is_set():
                raise SnapshotCancelled()
            yield row
        after = tuple(page[-1][key] for key in keys)


def write_snapshot(store: Any, kb: str, timestamp: str, cancelled: Any):
    import zstandard as zstd

    path = store.snapshot_path(kb, timestamp)
    temporary = _tmp_for(path)
    state = store.state
    count_nodes = count_edges = 0

    def encode(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")

    try:
        with state.conn:
            if state.dialect.is_postgres:
                state.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                state.conn.execute("SET LOCAL statement_timeout=10000")
            else:
                state.conn.execute("BEGIN")
            expected = store.rows.counts(kb)
            with temporary.open("wb") as raw:
                with zstd.ZstdCompressor(level=3).stream_writer(raw, closefd=False) as stream:
                    stream.write(b'{"directed":true,"graph":{},"links":[')
                    edges = _rows(
                        state, "graph_edges", ("source", "target", "type", "seq"), kb, cancelled
                    )
                    for (a, b), group in groupby(edges, lambda r: (r["source"], r["target"])):
                        parallel = [edge_attrs_dict(row) for row in group]
                        if len(parallel) > 1:
                            parallel.sort(key=encode)
                        for key, attrs in enumerate(parallel):
                            if count_edges:
                                stream.write(b",")
                            stream.write(encode({"source": a, "target": b, "key": key, **attrs}))
                            count_edges += 1
                    stream.write(b'],"multigraph":true,"nodes":[')
                    for row in _rows(state, "graph_nodes", ("node_id",), kb, cancelled):
                        if count_nodes:
                            stream.write(b",")
                        stream.write(encode({"id": row["node_id"], **node_attrs_dict(row)}))
                        count_nodes += 1
                    stream.write(b"]}")
                raw.flush()
                os.fsync(raw.fileno())
            if (count_nodes, count_edges) != expected:
                raise RuntimeError("snapshot row counts differ from its consistent generation")
        if cancelled.is_set():
            raise SnapshotCancelled()
        os.replace(temporary, path)
        store._fsync_dir(path.parent)
        return path
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
