"""WASM-accelerated `graph.enrichment.resolve_cross_source_edges` (Step 34.5a).

Drop-in replacement — same signature, same return type, same edge set and
`attrs` shape as the pure-Python function, verified in
`tests/test_wasm_accel.py`. Ported in full (both the `python_import` and
`document_link`/`url` reference-type paths — see `accel/rust/src/lib.rs`'s
module docstring for why a partial port would be a silent correctness
regression here, not just a performance change).
"""

from __future__ import annotations

import struct
from typing import Any

from pheasant.graph.enrichment import (
    ARTIFACT_NODE_TYPES,
    EnrichmentEdge,
    resolve_cross_source_edges,
)
from pheasant.sandbox.accel.loader import new_instance


def _write_str(buf: bytearray, s: str) -> None:
    encoded = s.encode("utf-8")
    buf += struct.pack("<I", len(encoded))
    buf += encoded


class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def u32(self) -> int:
        (value,) = struct.unpack_from("<I", self.data, self.pos)
        self.pos += 4
        return value

    def u8(self) -> int:
        value = self.data[self.pos]
        self.pos += 1
        return value

    def string(self) -> str:
        n = self.u32()
        s = self.data[self.pos : self.pos + n].decode("utf-8")
        self.pos += n
        return s

    def opt_string(self) -> str | None:
        s = self.string()
        return s or None


def _serialize_input(
    nodes: list[tuple[str, dict[str, Any]]],
    edges: list[tuple[str, str, str, str | None]],
) -> bytes:
    buf = bytearray()
    artifacts = [
        (node_id, attrs.get("relative_path"), attrs.get("source_id"))
        for node_id, attrs in nodes
        if attrs.get("type") in ARTIFACT_NODE_TYPES
        and attrs.get("relative_path")
        and attrs.get("source_id")
    ]
    buf += struct.pack("<I", len(artifacts))
    for node_id, rel, src in artifacts:
        _write_str(buf, node_id)
        _write_str(buf, rel)
        _write_str(buf, src)

    ext_nodes = [
        (node_id, attrs.get("source_id"), attrs.get("reference"))
        for node_id, attrs in nodes
        if attrs.get("type") == "external_reference"
    ]
    valid_ext = [(nid, src, ref) for nid, src, ref in ext_nodes if ref and src]
    buf += struct.pack("<I", len(valid_ext))
    for ext_id, src, ref in valid_ext:
        _write_str(buf, ext_id)
        _write_str(buf, src)
        _write_str(buf, ref)

    buf += struct.pack("<I", len(edges))
    for artifact_id, ext_id, edge_type, reference_type in edges:
        _write_str(buf, artifact_id)
        _write_str(buf, ext_id)
        _write_str(buf, edge_type)
        _write_str(buf, reference_type or "")
    return bytes(buf)


#: The reference types the compiled guest resolves. Every other type (the
#: non-Python code imports, see ``graph.code_imports``) is resolved by the
#: pure-Python function and merged, so turning acceleration on can never drop
#: an edge the default path draws. Porting a type to Rust means adding it here.
WASM_REFERENCE_TYPES = frozenset({"python_import", "document_link", "url"})


def resolve_cross_source_edges_wasm(
    nodes: list[tuple[str, dict[str, Any]]],
    edges: list[tuple[str, str, str, str | None]],
) -> list[EnrichmentEdge]:
    """WASM-accelerated equivalent of ``resolve_cross_source_edges``.

    Edges of a type the guest was not built for go to the Python resolver.
    One artifact is one language, so the two halves never produce the same
    ``(source, target, type)`` and merging them is a sort, not a dedup.

    Raises whatever ``pheasant.sandbox.wasm_runtime``/``wasmtime`` raises on
    failure (unavailable extra, trap, etc.) — callers decide whether to
    fall back to the pure-Python implementation; this function does not
    swallow errors itself; see ``graph/builder.py``'s call site.
    """
    native = [edge for edge in edges if edge[3] in WASM_REFERENCE_TYPES]
    others = [edge for edge in edges if edge[3] not in WASM_REFERENCE_TYPES]
    results = _resolve_in_guest(nodes, native) if native else []
    if others:
        results.extend(resolve_cross_source_edges(nodes, others))
        results.sort(key=lambda e: (e.source, e.target, e.type))
    return results


def _resolve_in_guest(
    nodes: list[tuple[str, dict[str, Any]]],
    edges: list[tuple[str, str, str, str | None]],
) -> list[EnrichmentEdge]:
    store, instance = new_instance()
    exports = instance.exports(store)
    payload = _serialize_input(nodes, edges)

    in_ptr = exports["wasm_alloc"](store, len(payload))
    exports["memory"].write(store, payload, in_ptr)
    out_len_ptr = exports["wasm_alloc"](store, 4)
    out_ptr = exports["resolve_cross_source"](store, in_ptr, len(payload), out_len_ptr)
    out_len = struct.unpack(
        "<I", bytes(exports["memory"].read(store, out_len_ptr, out_len_ptr + 4))
    )[0]
    raw = bytes(exports["memory"].read(store, out_ptr, out_ptr + out_len))

    r = _Reader(raw)
    count = r.u32()
    results: list[EnrichmentEdge] = []
    for _ in range(count):
        source = r.string()
        target = r.string()
        etype = r.string()
        source_id = r.string()
        target_source_id = r.string()
        reference = r.string()
        reference_type = r.opt_string()
        cross_source = bool(r.u8())
        results.append(
            EnrichmentEdge(
                source,
                target,
                etype,
                {
                    "source_id": source_id,
                    "target_source_id": target_source_id,
                    "reference": reference,
                    "reference_type": reference_type,
                    "cross_source": cross_source,
                    "enrichment_pass": (
                        "cross_source_resolution" if cross_source else "internal_resolution"
                    ),
                },
            )
        )
    return results
