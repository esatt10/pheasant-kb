"""Measure what an ANN index would cost this region in recall, before turning it on.

    python -m pheasant.search.vector_ann_benchmark [--config pheasant.yaml]
        [--index ivf_hnsw_sq] [--queries 32] [--k 40,300] [--nprobes 8,16,32]

Recall depends on how the region's own embeddings cluster, which no number
measured elsewhere can stand in for: the table in ``search/vector_ann.py`` was
taken on hashed bag-of-words vectors and is a floor. This copies the region's
LanceDB table to a temporary directory (the live table is never written),
builds the index there, and compares each query's approximate top-k with the
exact one. The queries are the region's own stored vectors -- real embeddings
of real chunks -- with each query's own row left out of both lists, so a
vector finding itself is not counted as recall. Those are in-distribution;
short questions can do worse (0.92 here against 0.68-0.81 on the lab corpus).
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

from pheasant.config.loader import load_config
from pheasant.search import vector_ann


def _exact(matrix: np.ndarray, ids: list[str], query: np.ndarray, k: int, own: str) -> list[str]:
    scores = matrix @ query
    top = np.argpartition(-scores, k)[: k + 1]
    top = top[np.argsort(-scores[top], kind="stable")]
    return [ids[i] for i in top if ids[i] != own][:k]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, default=Path("pheasant.yaml"))
    parser.add_argument("--index", default="ivf_hnsw_sq", choices=sorted(vector_ann.INDEX_TYPES))
    parser.add_argument("--queries", type=int, default=32)
    parser.add_argument("--k", default="40,300")
    parser.add_argument("--nprobes", default="8,16,32")
    args = parser.parse_args(argv)

    import lancedb

    config = load_config(args.config)
    settings = config.search.vector_store
    base = Path(settings.path or (config.pheasant.state_path / "vectors"))
    live = base / config.knowledge_base_id
    with tempfile.TemporaryDirectory(prefix="pheasant-ann-") as scratch:
        shutil.copytree(live, Path(scratch) / "copy")
        table = lancedb.connect(str(Path(scratch) / "copy")).open_table("chunks")
        arrow = table.search().select(["chunk_id", "vector"]).limit(None).to_arrow()
        ids = arrow.column("chunk_id").to_pylist()
        flat = arrow.column("vector").combine_chunks()
        matrix = np.array(flat.values, dtype=np.float32).reshape(len(ids), flat.type.list_size)
        matrix /= np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
        picks = np.random.default_rng(0).choice(len(ids), min(args.queries, len(ids)), False)
        ks = [int(k) for k in args.k.split(",")]
        exact = {(i, k): _exact(matrix, ids, matrix[i], k, ids[i]) for i in picks for k in ks}
        print(f"{len(ids)} vectors x {matrix.shape[1]} dims, {len(picks)} queries")

        started = time.monotonic()
        wanted = replace(settings, ann_index=args.index, ann_min_rows=0)
        outcome = vector_ann.reconcile(table, wanted)
        print(f"{args.index}: {outcome} in {time.monotonic() - started:.1f}s")
        for nprobes in (int(n) for n in args.nprobes.split(",")):
            shaped = replace(wanted, ann_nprobes=nprobes)
            for k in ks:
                latency, recall = [], []
                for i in picks:
                    query = table.search(matrix[i].tolist()).distance_type("cosine").limit(k + 1)
                    query = query.select(["chunk_id", "_distance"])
                    query = vector_ann.shape_query(query, shaped, k)
                    began = time.perf_counter()
                    found = [h["chunk_id"] for h in query.to_list() if h["chunk_id"] != ids[i]]
                    latency.append((time.perf_counter() - began) * 1000)
                    truth = exact[(i, k)]
                    recall.append(len(set(found[:k]) & set(truth)) / max(1, len(truth)))
                print(
                    f"  nprobes={nprobes:<3d} k={k:<4d} p50 {statistics.median(latency):7.1f} ms"
                    f"  recall {statistics.mean(recall):.3f} (worst query {min(recall):.2f})"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
