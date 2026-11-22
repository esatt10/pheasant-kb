"""What a knowledge graph actually costs, measured (Phase 35.3).

pheasant holds the whole graph in memory as a :class:`~pheasant.graph.simple.
SimpleMultiDiGraph` and persists it as one zstd node-link blob. That is fine
at 5 MB of corpus and impossible at hundreds of GB, and the interesting
question — the one that decides whether to shard, to move the graph into the
database, or to do nothing — is *where between those it stops being fine*.

Phase 34.7 asked a version of this and answered "not a memory problem",
measuring 20.5 MB at 13.5k nodes and 205 MB at 135k. That was true and is
still true, but it stopped at 10x the demo corpus and drew the conclusion for
every scale. This module exists so the recommendation is a number rather than
an extrapolation, and so it can be re-run on real hardware instead of trusted
from a doc.

Run it with ``python -m pheasant.graph.capacity``. Deterministic, offline, and
it measures four things per scale point:

* **resident bytes** — what the container actually has to be given, via
  ``tracemalloc`` for the graph itself plus RSS delta for the true cost
  including interpreter overhead;
* **persisted bytes** — compressed and uncompressed, since the uncompressed
  size is what a save has to serialize and hold transiently;
* **save and load seconds** — the load is what a restart pays before serving,
  and the save is what every checkpoint pays;
* **edge-scan milliseconds** — the ``_scan_edges`` query path, which is
  O(edges) with no index and lands on user-visible search latency.
"""

from __future__ import annotations

import argparse
import gc
import json
import time
import tracemalloc
from pathlib import Path
from typing import Any

from pheasant.graph.simple import SimpleMultiDiGraph

#: Roughly what a real corpus produces per file, taken from the live
#: measurements in CLAUDE.md: the 2,132-file demo corpus produced 13,503 nodes
#: and 13,502 edges after the concept layer was retired, so ~6.3 nodes and
#: ~6.3 edges per file. Scale points below are expressed in *files* so the
#: output can be read against a corpus size directly.
NODES_PER_FILE = 6.3
EDGES_PER_FILE = 6.3

#: Out-edges on the synthetic hub node. A `source` node carries an `indexes`
#: edge to every artifact it holds, so on a real corpus this is the file count
#: — 8,040 on the 8,000-file stress corpus. Fixed here so the hub number is
#: comparable between scale points instead of growing with them.
HUB_FAN_OUT = 8000

#: Node types in the proportion a real graph has them, so attribute payloads
#: (which dominate the JSON size) are representative rather than uniform.
NODE_SHAPES = (
    ("chunk", 0.55),
    ("symbol", 0.20),
    ("file", 0.15),
    ("entity", 0.07),
    ("heading", 0.03),
)


def _resident_bytes() -> int | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def build_graph(files: int) -> SimpleMultiDiGraph:
    """A graph the size a corpus of ``files`` files would produce.

    Attribute payloads mirror what the builder actually attaches — a stable
    id, a type, a label, a relative path, a source and a summary — because the
    node-link JSON is dominated by those strings, not by the topology.
    """

    graph = SimpleMultiDiGraph()
    node_count = int(files * NODES_PER_FILE)
    ids: list[str] = []
    index = 0
    for node_type, share in NODE_SHAPES:
        for _ in range(int(node_count * share)):
            relative = f"src/module_{index % 997}/file_{index % 331}.py"
            node_id = f"{node_type}:corpus:{relative}:branch=main:n={index}"
            graph.add_node(
                node_id,
                type=node_type,
                label=f"{node_type}_{index}",
                relative_path=relative,
                source_id="corpus",
                summary=f"Deterministic {node_type} summary for node {index}.",
            )
            ids.append(node_id)
            index += 1
    edge_count = int(files * EDGES_PER_FILE)
    for position in range(edge_count):
        if len(ids) < 2:
            break
        source = ids[position % len(ids)]
        target = ids[(position * 7 + 1) % len(ids)]
        graph.add_edge(
            source,
            target,
            type=("contains", "references", "imports", "has_chunk")[position % 4],
            confidence=0.9,
        )
    return graph


def measure_rows(files: int, root: Path, graph: SimpleMultiDiGraph) -> dict[str, Any]:
    """The same scale point against the row backend (Phase 35.10).

    The comparison that decided the default, and the one CI republishes so it
    cannot rot. Three numbers matter and only one of them is a speed:

    * **incremental commit** — the cost of publishing after a one-file change.
      This is the number that made the file backend a ceiling: it was O(total
      graph) for an O(one file) change, on the sole commit authority, under
      the sync mutex.
    * **resident bytes to serve** — zero here by construction, which is what
      lets an api replica answer a bounded walk without being given the graph.
    * **stored bytes** — the cost side. Rows plus their two indexes are larger
      than the compressed blob they replace, and pretending otherwise in the
      capacity model would fill somebody's volume.
    """

    from pheasant.persistence.graph_rows import GraphRowStore
    from pheasant.persistence.state_store import StateStore

    # The database PHEASANT_DATABASE_URL names: give the benchmark its own.
    state = StateStore()
    state.migrate()
    rows = GraphRowStore(state)
    kb = "capacity"

    graph.mark_all_pending()
    delta = graph.graph_delta()
    started = time.perf_counter()
    rows.apply_delta(
        kb,
        node_upserts=delta["node_upserts"],
        node_removals=delta["node_removals"],
        edge_upserts=delta["edge_upserts"],
        edge_removals=delta["edge_removals"],
    )
    rows.publish(kb)
    initial_seconds = time.perf_counter() - started

    # One file's worth of change, which is what an incremental sync actually
    # produces: NODES_PER_FILE nodes and the edges that hang off them.
    touched = delta["node_upserts"][: int(NODES_PER_FILE)]
    touched_edges = delta["edge_upserts"][: int(EDGES_PER_FILE)]
    started = time.perf_counter()
    rows.apply_delta(kb, node_upserts=touched, edge_upserts=touched_edges)
    rows.publish(kb)
    incremental_seconds = time.perf_counter() - started

    from pheasant.graph.sql import SqlGraph
    from pheasant.graph.traversal import neighbors

    served = SqlGraph(state, kb)
    start_node = delta["node_upserts"][0][0]
    gc.collect()
    rss_before = _resident_bytes()
    started = time.perf_counter()
    for _ in range(20):
        neighbors(served, start_node, depth=3, max_nodes=100)
    walk_seconds = (time.perf_counter() - started) / 20
    rss_after = _resident_bytes()

    # The same walk off a **hub**, which is the case that separates the two
    # backends and the one no fixture has. A real `source` node indexes every
    # artifact, so a bounded walk there has thousands of edges to choose its
    # hundred from — and fetching that fan-out is the cost residency used to
    # hide. Reported separately because quoting only the ordinary number would
    # describe the easy half.
    hub = f"hub:{kb}"
    rows.apply_delta(
        kb,
        node_upserts=[(hub, {"type": "source", "label": "hub", "source_id": "corpus"})],
        edge_upserts=[
            (hub, node_id, 0, {"type": "contains", "source_id": "corpus"})
            for node_id, _attrs in delta["node_upserts"][:HUB_FAN_OUT]
        ],
    )
    started = time.perf_counter()
    for _ in range(10):
        neighbors(served, hub, depth=3, max_nodes=100)
    hub_walk_seconds = (time.perf_counter() - started) / 10

    stored = int(
        state.rows(
            "SELECT pg_total_relation_size('graph_nodes') + pg_total_relation_size('graph_edges')"
            " AS n"
        )[0]["n"]
    )
    state.close()
    return {
        "initial_load_seconds": round(initial_seconds, 4),
        "incremental_commit_seconds": round(incremental_seconds, 5),
        "stored_bytes": stored,
        "serving_rss_delta_bytes": (rss_after - rss_before) if (rss_after and rss_before) else None,
        "bounded_walk_seconds": round(walk_seconds, 6),
        "hub_walk_seconds": round(hub_walk_seconds, 6),
        "hub_fan_out": HUB_FAN_OUT,
    }


def measure(files: int, root: Path) -> dict[str, Any]:
    """One scale point. Returns plain numbers, ready for a table."""

    import zstandard as zstd

    gc.collect()
    rss_before = _resident_bytes()
    tracemalloc.start()
    graph = build_graph(files)
    in_memory, _peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    gc.collect()
    rss_after = _resident_bytes()

    started = time.perf_counter()
    payload = json.dumps(graph.to_node_link(), sort_keys=True, separators=(",", ":")).encode()
    serialize_seconds = time.perf_counter() - started

    started = time.perf_counter()
    compressed = zstd.ZstdCompressor(level=3).compress(payload)
    compress_seconds = time.perf_counter() - started

    path = root / f"graph-{files}.json.zst"
    path.write_bytes(compressed)

    started = time.perf_counter()
    restored = json.loads(zstd.ZstdDecompressor().decompress(compressed))
    load_seconds = time.perf_counter() - started
    assert len(restored["nodes"]) == graph.number_of_nodes()

    # The query-path cost: one full edge scan, which is what `_scan_edges`
    # does on every relationship search with no index to narrow it.
    started = time.perf_counter()
    matched = 0
    with graph.reading():
        # Exactly `_scan_edges`'s shape: ((source, target), {key: attrs}).
        for _endpoints, parallel in graph.iter_edges():
            for attrs in parallel.values():
                if "reference" in str(attrs.get("type", "")):
                    matched += 1
    scan_seconds = time.perf_counter() - started

    rows = measure_rows(files, root, graph)
    return {
        "files": files,
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "rows": rows,
        "graph_bytes": in_memory,
        "rss_delta_bytes": (rss_after - rss_before) if (rss_after and rss_before) else None,
        "json_bytes": len(payload),
        "compressed_bytes": len(compressed),
        "serialize_seconds": round(serialize_seconds, 4),
        "compress_seconds": round(compress_seconds, 4),
        "load_seconds": round(load_seconds, 4),
        "edge_scan_seconds": round(scan_seconds, 5),
        "matched_edges": matched,
    }


def run(scales: tuple[int, ...], root: Path) -> dict[str, Any]:
    results = [measure(files, root) for files in scales]
    return {
        "assumptions": {
            "nodes_per_file": NODES_PER_FILE,
            "edges_per_file": EDGES_PER_FILE,
            "source": "measured on the 2,132-file demo corpus (13,503 nodes / 13,502 edges)",
        },
        "scales": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--files",
        default="2000,20000,100000,500000",
        help="Comma-separated corpus sizes, in files.",
    )
    parser.add_argument("--output", help="Write JSON here instead of stdout.")
    args = parser.parse_args()
    scales = tuple(int(value) for value in args.files.split(",") if value.strip())
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="pheasant-capacity-"))
    try:
        report = run(scales, root)
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
