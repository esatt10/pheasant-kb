"""Offline sync-throughput benchmark for local capacity planning.

Run with ``python -m pheasant.sync.benchmark``. The fixture is generated in a
temporary directory, uses deterministic text and optional stub embeddings, and
never calls a network service. Results are JSON so CI or a capacity worksheet
can compare worker counts without scraping prose.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from pheasant.config.schema import PheasantConfig
from pheasant.sync.engine import SyncEngine


def _fixture(root: Path, files: int, lines: int) -> Path:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    body = "\n".join(
        f"Deterministic benchmark line {line}: parsing chunking graph embeddings."
        for line in range(lines)
    )
    for index in range(files):
        (workspace / f"document-{index:05d}.md").write_text(
            f"# Benchmark document {index}\n\n{body}\n",
            encoding="utf-8",
        )
    return workspace


def _config(
    root: Path,
    workspace: Path,
    workers: int,
    embeddings: bool,
    file_executor: str,
    run_id: str,
) -> PheasantConfig:
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "sync-benchmark",
                # A local SQLite region, measured as the code has always run it.
                "defaults": "standalone",
                "state_path": str(root / f"state-{file_executor}-{workers}-{run_id}"),
                "workspace_root": str(workspace),
                "exports_path": str(root / "exports"),
            },
            "storage": {"graph_snapshots": False, "graph_checkpoint_seconds": 0},
            "search": {
                "embeddings": {
                    "enabled": embeddings,
                    "provider": "stub",
                    "dimensions": 64,
                    "batch_size": 64,
                },
                "vector_store": {"provider": "numpy"},
            },
            "sync": {
                "concurrency": {
                    "max_parallel_sources": 1,
                    "max_parallel_files": workers,
                    "max_parallel_embeddings": min(workers, 4),
                    "file_executor": file_executor,
                }
            },
            "sources": [
                {
                    "name": "corpus",
                    "type": "markdown_folder",
                    "path": str(workspace),
                    "include": ["**/*.md"],
                }
            ],
        }
    )


def run_benchmark(
    *,
    files: int = 200,
    lines: int = 80,
    workers: tuple[int, ...] = (1, 2, 4, 8),
    embeddings: bool = False,
    file_executor: str = "thread",
    repeats: int = 3,
) -> dict[str, Any]:
    workers = tuple(dict.fromkeys(max(1, int(count)) for count in workers))
    if not workers:
        raise ValueError("at least one worker count is required")
    root = Path(tempfile.mkdtemp(prefix="pheasant-sync-benchmark-"))
    try:
        workspace = _fixture(root, files, lines)
        repeats = max(1, int(repeats))

        def run_once(worker_count: int, run_id: str) -> tuple[float, float, Any, Any, int]:
            engine = SyncEngine(
                _config(
                    root,
                    workspace,
                    worker_count,
                    embeddings,
                    file_executor,
                    run_id,
                )
            )
            started = time.perf_counter()
            try:
                result = engine.sync_source("corpus", "full")
                full_elapsed = time.perf_counter() - started
                calls_before = engine.vectors.embedder.calls if engine.vectors is not None else 0
                incremental_started = time.perf_counter()
                incremental = engine.sync_source("corpus", "incremental")
                incremental_elapsed = time.perf_counter() - incremental_started
                calls_after = engine.vectors.embedder.calls if engine.vectors is not None else 0
            finally:
                engine.close()
            return (
                full_elapsed,
                incremental_elapsed,
                result,
                incremental,
                calls_after - calls_before,
            )

        # Warm imports, parser caches and the fixture's filesystem pages before
        # comparing capacities. Cold-storage latency is deployment-specific.
        run_once(workers[0], "warmup")
        full_samples: dict[int, list[float]] = {count: [] for count in workers}
        incremental_samples: dict[int, list[float]] = {count: [] for count in workers}
        results: dict[int, Any] = {}
        incremental_results: dict[int, Any] = {}
        incremental_embedding_calls: dict[int, int] = {}
        for repeat in range(repeats):
            offset = repeat % len(workers)
            cycle = workers[offset:] + workers[:offset]
            for worker_count in cycle:
                full_elapsed, incremental_elapsed, result, incremental, calls = run_once(
                    worker_count, f"trial-{repeat}"
                )
                full_samples[worker_count].append(full_elapsed)
                incremental_samples[worker_count].append(incremental_elapsed)
                results[worker_count] = result
                incremental_results[worker_count] = incremental
                incremental_embedding_calls[worker_count] = calls

        medians = {count: statistics.median(values) for count, values in full_samples.items()}
        incremental_medians = {
            count: statistics.median(values) for count, values in incremental_samples.items()
        }
        baseline = medians[workers[0]]
        runs = []
        for worker_count in workers:
            elapsed = medians[worker_count]
            incremental_elapsed = incremental_medians[worker_count]
            result = results[worker_count]
            incremental = incremental_results[worker_count]
            runs.append(
                {
                    "workers": worker_count,
                    "seconds": round(elapsed, 4),
                    "trial_seconds": [round(value, 4) for value in full_samples[worker_count]],
                    "files_per_second": round(files / elapsed, 2),
                    "speedup": round(baseline / elapsed, 3),
                    "indexed_artifacts": result.indexed_artifacts,
                    "graph_nodes": result.graph_nodes,
                    "graph_edges": result.graph_edges,
                    "unchanged_incremental_seconds": round(incremental_elapsed, 4),
                    "unchanged_incremental_trial_seconds": [
                        round(value, 4) for value in incremental_samples[worker_count]
                    ],
                    "unchanged_incremental_files_per_second": round(files / incremental_elapsed, 2),
                    "unchanged_incremental_indexed_artifacts": incremental.indexed_artifacts,
                    "unchanged_incremental_skipped_artifacts": incremental.skipped_artifacts,
                    "unchanged_incremental_embedding_calls": incremental_embedding_calls[
                        worker_count
                    ],
                }
            )
        return {
            "fixture": {"files": files, "lines_per_file": lines},
            "embeddings": "stub" if embeddings else "disabled",
            "file_executor": file_executor,
            "cache": "warm",
            "repeats": repeats,
            "runs": runs,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _resident_bytes() -> int | None:
    """Current RSS, or None where the platform will not say.

    ``/proc`` first because it is exact and current; ``ru_maxrss`` is a
    high-*water* mark, which for a sweep that grows monotonically is close
    enough and is the only portable option.
    """

    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    try:
        import resource
        import sys

        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(maxrss) if sys.platform == "darwin" else int(maxrss) * 1024
    except Exception:  # pragma: no cover - platform-specific
        return None


def _directory_bytes(root: Path) -> dict[str, int]:
    """Bytes under /state, split by what actually holds them.

    Split rather than totalled because the parts scale differently: the
    database tracks *content* (it stores every chunk's text again, plus its
    FTS index), the graph tracks *structure*, and vectors track chunks times
    dimensions. A single total would hide which one is about to be the
    problem.
    """

    buckets = {"database": 0, "graph": 0, "vectors": 0, "manifests": 0, "other": 0}
    if not root.exists():
        return buckets
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        size = path.stat().st_size
        name = path.name
        relative = str(path.relative_to(root))
        if name.startswith("pheasant.db"):
            buckets["database"] += size
        elif "graph" in relative:
            buckets["graph"] += size
        elif "vector" in relative or path.suffix in {".lance", ".npy"}:
            buckets["vectors"] += size
        elif "manifest" in relative:
            buckets["manifests"] += size
        else:
            buckets["other"] += size
    return buckets


def run_capacity(
    *,
    sizes: tuple[int, ...] = (250, 1000, 4000),
    lines: int = 80,
    embeddings: bool = False,
    workers: int = 1,
) -> dict[str, Any]:
    """Sweep corpus *size* and record what each one costs (Phase 35.7).

    A different axis from :func:`run_benchmark`, which sweeps worker counts at
    one size. This one answers the sizing questions: how much RAM, how much
    disk, how long — the coefficients in :mod:`pheasant.capacity`.

    Each size gets its own process-lifetime measurement of RSS. That is the
    honest caveat: RSS is measured in *this* process, which has already
    imported everything and holds the previous size's garbage, so the reported
    delta is a floor. `python -m pheasant.graph.capacity` isolates the graph
    itself if you need the number without that noise.
    """

    from pheasant.capacity import (
        BYTES_PER_CHUNK,
        NODES_PER_FILE,
        SECONDS_PER_1K_FILES,
        STATE_BYTES_PER_CORPUS_BYTE,
    )

    root = Path(tempfile.mkdtemp(prefix="pheasant-capacity-"))
    points: list[dict[str, Any]] = []
    try:
        for size in sorted(dict.fromkeys(max(1, int(value)) for value in sizes)):
            workspace = _fixture(root / f"corpus-{size}", size, lines)
            corpus_bytes = sum(path.stat().st_size for path in workspace.rglob("*.md"))
            config = _config(root, workspace, workers, embeddings, "thread", f"cap-{size}")
            state_root = Path(config.pheasant.state_path)

            rss_before = _resident_bytes()
            engine = SyncEngine(config)
            started = time.perf_counter()
            try:
                result = engine.sync_source("corpus", "full")
                elapsed = time.perf_counter() - started
                chunk_rows = engine.state.rows("SELECT COUNT(*) AS c FROM chunks", ())
                chunks = int(chunk_rows[0]["c"]) if chunk_rows else 0
            finally:
                engine.close()
            rss_after = _resident_bytes()

            state = _directory_bytes(state_root)
            state_total = sum(state.values())
            points.append(
                {
                    "files": size,
                    "corpus_bytes": corpus_bytes,
                    "corpus_mb": round(corpus_bytes / (1024 * 1024), 2),
                    "seconds": round(elapsed, 3),
                    "seconds_per_1k_files": round(elapsed / size * 1000, 2),
                    "files_per_second": round(size / elapsed, 1),
                    "graph_nodes": result.graph_nodes,
                    "graph_edges": result.graph_edges,
                    "nodes_per_file": round(result.graph_nodes / size, 3),
                    "chunks": chunks,
                    "chunks_per_file": round(chunks / size, 3),
                    "bytes_per_chunk": round(corpus_bytes / chunks) if chunks else None,
                    "state_bytes": state_total,
                    "state_mb": round(state_total / (1024 * 1024), 2),
                    "state_bytes_per_corpus_byte": (
                        round(state_total / corpus_bytes, 3) if corpus_bytes else None
                    ),
                    "state_breakdown_bytes": state,
                    "rss_bytes": rss_after,
                    "rss_delta_bytes": (
                        (rss_after - rss_before) if (rss_after and rss_before) else None
                    ),
                }
            )
        return {
            "fixture": {"lines_per_file": lines, "workers": workers},
            "embeddings": "stub" if embeddings else "disabled",
            "points": points,
            # The constants this run is meant to check. Printed beside the
            # measurements so drift is visible without opening two files.
            "model": {
                "nodes_per_file": NODES_PER_FILE,
                "bytes_per_chunk": BYTES_PER_CHUNK,
                "state_bytes_per_corpus_byte": STATE_BYTES_PER_CORPUS_BYTE,
                "seconds_per_1k_files": SECONDS_PER_1K_FILES,
            },
            "caveats": [
                "RSS is process-lifetime, not per-size: earlier sizes' garbage is "
                "included, so treat the value as a floor. Use "
                "`python -m pheasant.graph.capacity` for the graph in isolation.",
                "The fixture is uniform markdown. A real corpus of PDFs, code or "
                "very large files will move seconds_per_1k_files most and "
                "nodes_per_file least.",
            ],
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("workers", "capacity"),
        default="workers",
        help="workers: throughput vs worker count. capacity: cost vs corpus size.",
    )
    parser.add_argument("--files", type=int, default=200)
    parser.add_argument("--sizes", default="250,1000,4000", help="capacity mode: corpus sizes")
    parser.add_argument("--lines", type=int, default=80)
    parser.add_argument("--workers", default="1,2,4,8")
    parser.add_argument("--embeddings", action="store_true")
    parser.add_argument("--executor", choices=("thread", "process"), default="thread")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    worker_counts = tuple(max(1, int(value)) for value in args.workers.split(",") if value.strip())
    if args.mode == "capacity":
        report = run_capacity(
            sizes=tuple(int(value) for value in args.sizes.split(",") if value.strip()),
            lines=max(1, args.lines),
            embeddings=args.embeddings,
            workers=worker_counts[0],
        )
    else:
        report = run_benchmark(
            files=max(1, args.files),
            lines=max(1, args.lines),
            workers=worker_counts,
            embeddings=args.embeddings,
            file_executor=args.executor,
            repeats=max(1, args.repeats),
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
