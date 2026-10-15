"""WASM-accelerated `search.graph_search._scan_edges` (Step 34.5b).

Drop-in replacement for the edge-scoring half of relationship search.
Rather than re-marshal every field of every hit back out of the guest (the
host already has all of it — it built the input rows), the guest returns
only ``(edge_index, score)`` pairs and the host reconstructs the exact same
hit dict `_scan_edges` would have built, keyed by the edge's position in
the same iteration order it was serialized in. This keeps the wire format
proportional to what actually changed (scores), not what the host already
knows, and matches `_scan_edges`'s exact hit-dict shape byte for byte —
verified in `tests/test_wasm_accel.py`.
"""

from __future__ import annotations

import struct
from typing import Any

from pheasant.graph.simple import SimpleMultiDiGraph
from pheasant.sandbox.accel.loader import new_instance
from pheasant.search.graph_search import _SKIP_KEYS, _stringify


def _write_str(buf: bytearray, s: str) -> None:
    encoded = s.encode("utf-8")
    buf += struct.pack("<I", len(encoded))
    buf += encoded


def _serialize_input(
    rows: list[tuple[str, str, str, str, list[tuple[str, str]]]],
    tokens: list[str],
    q: str,
    source_name: str | None,
) -> bytes:
    buf = bytearray()
    buf += struct.pack("<I", len(tokens))
    for t in tokens:
        _write_str(buf, t)
    _write_str(buf, q)
    _write_str(buf, source_name or "")

    buf += struct.pack("<I", len(rows))
    for source_label, target_label, etype, edge_source_id, attrs in rows:
        _write_str(buf, source_label)
        _write_str(buf, target_label)
        _write_str(buf, etype)
        _write_str(buf, edge_source_id)
        buf += struct.pack("<I", len(attrs))
        for k, v in attrs:
            _write_str(buf, k)
            _write_str(buf, v)
    return bytes(buf)


def _edge_rows(
    graph: SimpleMultiDiGraph,
    source_name: str | None,
    tokens: list[str] | None = None,
) -> list[tuple[str, str, dict[str, Any], str, str]]:
    """(source, target, data, source_label, target_label) per edge instance, in iteration order.

    Narrowed through the graph's own candidate query where it has one, the
    same as the pure-Python scan. Acceleration that streamed every edge out of
    a database to hand it to a guest would be slower than the thing it
    accelerates — the WASM path pays off against a resident dict, which is
    what it was written for.
    """
    nodes = graph.node_map()
    rows = []
    narrow = getattr(graph, "candidate_edges", None)
    edges = narrow(tokens, source_name) if (callable(narrow) and tokens) else graph.iter_edges()
    # A store-backed node_map() fetches every endpoint in one query, as the
    # Python scan does (`graph_search._primed_candidates`); one query per
    # endpoint was up to 480 a search. The resident graph's map is a dict.
    prime = getattr(nodes, "prime", None)
    if callable(prime):
        edges = list(edges)
        prime(endpoint for pair, _edges in edges for endpoint in pair)
    for (source, target), edge_map in edges:
        source_label = str((nodes.get(source) or {}).get("label") or source)
        target_label = str((nodes.get(target) or {}).get("label") or target)
        for _key, data in edge_map.items():
            rows.append((source, target, data, source_label, target_label))
    return rows


def scan_edges_wasm(
    graph: SimpleMultiDiGraph,
    tokens: list[str],
    q: str,
    source_name: str | None,
) -> list[tuple[float, dict[str, Any]]]:
    """WASM-accelerated equivalent of ``_scan_edges``. Caller holds ``reading()``."""
    edge_rows = _edge_rows(graph, source_name, tokens)
    serializable_rows = [
        (
            source_label,
            target_label,
            str(data.get("type") or "related"),
            str(data.get("source_id") or ""),
            [(k, _stringify(v)) for k, v in data.items() if k not in _SKIP_KEYS and k != "type"],
        )
        for _source, _target, data, source_label, target_label in edge_rows
    ]

    store, instance = new_instance()
    exports = instance.exports(store)
    payload = _serialize_input(serializable_rows, tokens, q, source_name)

    in_ptr = exports["wasm_alloc"](store, len(payload))
    exports["memory"].write(store, payload, in_ptr)
    out_len_ptr = exports["wasm_alloc"](store, 4)
    out_ptr = exports["scan_edges"](store, in_ptr, len(payload), out_len_ptr)
    out_len = struct.unpack(
        "<I", bytes(exports["memory"].read(store, out_len_ptr, out_len_ptr + 4))
    )[0]
    raw = bytes(exports["memory"].read(store, out_ptr, out_ptr + out_len))

    pos = 0
    (hit_count,) = struct.unpack_from("<I", raw, pos)
    pos += 4
    hits: list[tuple[float, dict[str, Any]]] = []
    for _ in range(hit_count):
        edge_index, score = struct.unpack_from("<Id", raw, pos)
        pos += 12
        source, target, data, source_label, target_label = edge_rows[edge_index]
        etype = str(data.get("type") or "related")
        hits.append(
            (
                score,
                {
                    "kind": "relationship",
                    "node_id": source,
                    "type": "relationship",
                    "edge_type": etype,
                    "source": source,
                    "target": target,
                    "source_label": source_label,
                    "target_label": target_label,
                    "title": f"{source_label} —{etype}→ {target_label}",
                    "label": etype,
                    "score": round(score, 3),
                    "reason": "Graph relationship match",
                    "summary": f"{source_label} {etype} {target_label}",
                },
            )
        )
    return hits
