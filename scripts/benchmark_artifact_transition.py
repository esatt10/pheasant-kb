"""Measure typed knowledge and saved artifact paths in isolated temporary regions.

The memory label exercises the deprecated adapter; record uses the canonical
artifact operation. Earlier result files capture the pre-transition baseline.
No provider calls: SQLite, BM25 and graph, with no embedding index. Writes
are batched, then indexed once; save latency is NOT time-to-searchable.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any


def distribution(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
    }


def measure(kind: str, count: int, repeats: int, *, taxonomy: bool = False) -> dict[str, Any]:
    from pheasant.config.schema import PheasantConfig
    from pheasant.mcp_server.tools import PheasantTools
    from pheasant.services import ServiceContext, artifact_records, artifacts
    from pheasant.services.artifact_render import overview
    from pheasant.tuning import refusion

    with tempfile.TemporaryDirectory(prefix="pheasant-artifact-transition-") as directory:
        root = Path(directory)
        memory = root / "memory"
        memory.mkdir()
        config = PheasantConfig.model_validate(
            {
                "pheasant": {
                    "defaults": "standalone",
                    "name": "transition-benchmark",
                    "state_path": str(root / "state"),
                    "exports_path": str(root / "exports"),
                    "workspace_root": str(root),
                },
                "storage": {"graph_snapshots": False},
                "artifacts": {"index_on_save": False, "taxonomy_enabled": taxonomy},
                "observability": {"interactions": {"enabled": False}},
                "sources": [
                    {
                        "name": "agent-memory",
                        "type": "memory",
                        "path": str(memory),
                        "sync": {"on_startup": False},
                        "taxonomy": {"enabled": taxonomy},
                    }
                ],
            }
        )
        tools = PheasantTools(config)
        try:
            context = ServiceContext(config, tools.state, None, engine=tools.engine)
            writes = []
            kept = None
            for index in range(count):
                text = (
                    f"Service zephyr{index:05d} uses invoice retry policy {index % 7}, "
                    f"runs in region {index % 5} and is owned by team {index % 13}."
                )
                started = time.perf_counter()
                if kind == "memory":
                    tools.memory_write(config.knowledge_base_id, text, scope="org", sync=False)
                elif kind == "record":
                    artifact_records.write(
                        context, artifact_records.WriteRequest(text, scope="org", sync=False)
                    )
                else:
                    kept = artifacts.save(
                        context,
                        artifacts.SaveRequest(title=f"Service {index}", markdown=text),
                    )
                writes.append((time.perf_counter() - started) * 1000)
            source = "artifacts" if kind == "artifact" else "agent-memory"
            if taxonomy and kind == "artifact":
                from pheasant.registry.source_registry import SourceRegistry

                registered = next(s for s in config.sources if s.name == source)
                registered.taxonomy.enabled = True
                SourceRegistry(config, tools.state).register_source(registered)
            started = time.perf_counter()
            tools.engine.sync_source(source, "incremental")
            indexing = time.perf_counter() - started
            search_times = []
            successful = 0
            capture = None
            for step in range(repeats + 5):
                index = (step * 37) % count
                started = time.perf_counter()
                result = tools.searcher.search_context(
                    config.knowledge_base_id,
                    f"zephyr{index:05d}",
                    mode="hybrid",
                    max_results=5,
                    graph=tools.graph,
                    explain=True,
                )
                if step >= 5:
                    search_times.append((time.perf_counter() - started) * 1000)
                    successful += any(
                        f"zephyr{index:05d}" in json.dumps(hit) for hit in result["results"]
                    )
                capture = result["stages"]
            ranking = tools.searcher.ranking_parameters()
            equivalent, reason = refusion.verify_equivalence(capture, ranking)
            if not equivalent:
                raise AssertionError(reason)
            started = time.perf_counter()
            for _ in range(1000):
                refusion.refuse(capture["fusion_input"], capture["max_results"], ranking)
            refusion_ms = (time.perf_counter() - started) * 1000 / 1000
            render_times = []
            if kept:
                for _ in range(repeats):
                    started = time.perf_counter()
                    overview(kept)
                    render_times.append((time.perf_counter() - started) * 1000)
            state_bytes = sum(p.stat().st_size for p in (root / "state").rglob("*") if p.is_file())
            # Memory originals live outside /state; artifact originals live
            # inside it. Include both for a comparable, point-in-time total.
            region_bytes = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
            return {
                "kind": kind,
                "implementation": "artifact-first",
                "records": count,
                "taxonomy_enabled": taxonomy,
                "write_batched": distribution(writes),
                "index_seconds": round(indexing, 3),
                "search_explain": distribution(search_times),
                "exact_token_top5": successful / repeats,
                "refusion_equivalent": equivalent,
                "refusion_ms_per_trial_1000": round(refusion_ms, 4),
                "render": distribution(render_times) if render_times else None,
                "state_bytes": state_bytes,
                "region_bytes_including_originals_and_wal": region_bytes,
            }
        finally:
            tools.jobs.close()
            tools.engine.close()
            tools.state.close()
            gc.collect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", nargs="+", type=int, default=[100, 1000])
    parser.add_argument("--repeats", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--taxonomy", action="store_true")
    parser.add_argument(
        "--kinds",
        nargs="+",
        choices=["memory", "record", "artifact"],
        default=["memory", "artifact"],
    )
    args = parser.parse_args()
    if any(n <= 0 for n in args.counts) or args.repeats <= 0:
        parser.error("counts and repeats must be positive")
    results = []
    for count in args.counts:
        for kind in args.kinds:
            result = measure(kind, count, args.repeats, taxonomy=args.taxonomy)
            results.append(result)
            print(json.dumps(result), flush=True)
    args.output.write_text(
        json.dumps(
            {
                "scope": (
                    "Artifact-first paths; batched writes; SQLite; no vectors/providers; sequential"
                ),
                "results": results,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
