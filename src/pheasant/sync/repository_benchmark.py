"""Offline architecture regression benchmark over real public repositories.

CI checks out five public repositories, then this module samples their actual
source files deterministically. All indexing and retrieval remain offline: the
stub embedder supplies vectors, the assistant is disabled, and no provider key
is read. The committed budgets are deliberately guard rails for significant
regressions rather than microbenchmark assertions on a shared GitHub runner.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from pheasant.config.schema import DEFAULT_EXCLUDES, PheasantConfig
from pheasant.ingestion.content_types import TEXT_EXTENSIONS, suffix_globs
from pheasant.ingestion.pipeline import _match_any
from pheasant.search.hybrid import HybridSearch
from pheasant.search.sqlite_store import SearchStore
from pheasant.sync.engine import SyncEngine

REQUIRED_REPOSITORIES = {"spark", "mlflow", "vscode", "langgraph", "deepagents"}
RETRIEVAL_MODES = ("vector", "graph", "hybrid")
SOURCE_EXTENSIONS = set(TEXT_EXTENSIONS)
#: What each benchmark source includes, and so the only files worth sampling:
#: a sample the source's own include cannot reach is a file the benchmark
#: expects and the sync never sees. (`.R` was exactly that, the day `.r`
#: joined the text formats: sampled by a lower-cased suffix, skipped by a
#: case-sensitive glob.)
SOURCE_INCLUDE = [
    glob for extension in sorted(SOURCE_EXTENSIONS) for glob in suffix_globs(extension)
]
SKIP_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
}


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    repositories = list(manifest.get("repositories") or [])
    names = {str(item.get("name") or "") for item in repositories}
    if names != REQUIRED_REPOSITORIES:
        raise ValueError(
            "repository benchmark must contain exactly "
            f"{sorted(REQUIRED_REPOSITORIES)}; got {sorted(names)}"
        )
    if len(names) != len(repositories):
        raise ValueError("repository benchmark contains duplicate repository names")
    return manifest


def _candidate_files(repository: Path, max_file_bytes: int) -> list[Path]:
    candidates: list[Path] = []
    for path in repository.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(repository)
        if any(part in SKIP_PARTS for part in relative.parts):
            continue
        relative_key = relative.as_posix()
        if any(
            fnmatch.fnmatch(relative_key, pattern) or fnmatch.fnmatch(f"/{relative_key}", pattern)
            for pattern in DEFAULT_EXCLUDES
        ):
            continue
        if not _match_any(relative_key, SOURCE_INCLUDE):
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                continue
        except OSError:
            continue
        candidates.append(path)
    return candidates


def _sample_files(repository: Path, limit: int, max_file_bytes: int) -> list[Path]:
    candidates = _candidate_files(repository, max_file_bytes)
    ranked = sorted(
        candidates,
        key=lambda path: (
            hashlib.sha256(path.relative_to(repository).as_posix().encode()).digest(),
            path.relative_to(repository).as_posix(),
        ),
    )
    return ranked[:limit]


def _materialize_samples(
    manifest: dict[str, Any], repositories_root: Path, workspace: Path
) -> dict[str, dict[str, Any]]:
    limit = max(1, int(manifest.get("sample_files_per_repository") or 1))
    max_bytes = max(1, int(manifest.get("max_file_bytes") or 1))
    samples: dict[str, dict[str, Any]] = {}
    for repository in manifest["repositories"]:
        name = str(repository["name"])
        source = repositories_root / name
        if not source.is_dir():
            raise FileNotFoundError(f"missing CI repository checkout: {source}")
        selected = _sample_files(source, limit, max_bytes)
        if not selected:
            raise ValueError(f"repository {name!r} produced no benchmark files")
        destination = workspace / name
        for path in selected:
            relative = path.relative_to(source)
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)
        commit = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        samples[name] = {
            "files": len(selected),
            "commit": commit or None,
            "sample_digest": hashlib.sha256(
                "\n".join(path.relative_to(source).as_posix() for path in selected).encode()
            ).hexdigest(),
        }
    return samples


def _config(root: Path, workspace: Path, manifest: dict[str, Any]) -> PheasantConfig:
    sources = []
    for repository in manifest["repositories"]:
        name = str(repository["name"])
        sources.append(
            {
                "name": name,
                "type": "repository",
                "path": str(workspace / name),
                "include": SOURCE_INCLUDE,
                "sync": {
                    "on_startup": False,
                    "on_file_change": False,
                    "on_git_commit": False,
                },
            }
        )
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "ci-repository-architecture",
                "state_path": str(root / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(root / "exports"),
            },
            "storage": {"graph_snapshots": False, "graph_checkpoint_seconds": 0},
            "search": {
                "default_mode": "hybrid",
                "embeddings": {
                    "enabled": True,
                    "provider": "stub",
                    "dimensions": 64,
                    "batch_size": 128,
                },
                "vector_store": {"provider": "numpy"},
            },
            "sync": {
                "watcher": {"enabled": False},
                "scheduler": {"enabled": False},
                "concurrency": {
                    "max_parallel_sources": 1,
                    "max_parallel_files": 8,
                    "max_parallel_embeddings": 4,
                    "file_executor": "thread",
                },
            },
            "assistant": {
                "enabled": False,
                "provider": "none",
                "retrieval": {"retrieval_modes": list(RETRIEVAL_MODES)},
            },
            "sources": sources,
        }
    )


def _resident_mb() -> float | None:
    try:
        import resource
        import sys

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        bytes_used = value if sys.platform == "darwin" else value * 1024
        return round(bytes_used / (1024 * 1024), 2)
    except (ImportError, OSError, ValueError):  # pragma: no cover - platform-specific
        return None


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[int((len(ordered) - 1) * fraction)]


def _checks(report: dict[str, Any], budgets: dict[str, Any]) -> list[dict[str, Any]]:
    full = float(report["totals"]["full_seconds"])
    incremental = float(report["totals"]["incremental_seconds"])
    files_per_second = float(report["totals"]["full_files_per_second"])
    search_p95 = float(report["search"]["p95_seconds"])
    checks = [
        ("full wall time", full, "<=", float(budgets["max_full_seconds"])),
        (
            "full throughput",
            files_per_second,
            ">=",
            float(budgets["min_full_files_per_second"]),
        ),
        (
            "incremental wall time",
            incremental,
            "<=",
            float(budgets["max_incremental_seconds"]),
        ),
        (
            "incremental/full ratio",
            incremental / max(full, 0.000001),
            "<=",
            float(budgets["max_incremental_to_full_ratio"]),
        ),
        (
            "search p95",
            search_p95,
            "<=",
            float(budgets["max_search_p95_seconds"]),
        ),
    ]
    rss = report["totals"].get("peak_rss_mb")
    if rss is not None:
        checks.append(("peak RSS", float(rss), "<=", float(budgets["max_peak_rss_mb"])))
    return [
        {
            "name": name,
            "value": round(value, 4),
            "operator": operator,
            "limit": limit,
            "passed": value <= limit if operator == "<=" else value >= limit,
        }
        for name, value, operator, limit in checks
    ]


def run_repository_benchmark(
    manifest_path: Path,
    repositories_root: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    root = Path(tempfile.mkdtemp(prefix="pheasant-repository-architecture-"))
    try:
        workspace = root / "workspace"
        samples = _materialize_samples(manifest, repositories_root, workspace)
        config = _config(root, workspace, manifest)
        engine = SyncEngine(config)
        full_rows: list[dict[str, Any]] = []
        incremental_rows: list[dict[str, Any]] = []
        search_rows: list[dict[str, Any]] = []
        try:
            for repository in manifest["repositories"]:
                name = str(repository["name"])
                started = time.perf_counter()
                result = engine.sync_source(name, "full")
                elapsed = time.perf_counter() - started
                full_rows.append(
                    {
                        "source": name,
                        "seconds": round(elapsed, 4),
                        "indexed_artifacts": result.indexed_artifacts,
                        "expected_artifacts": samples[name]["files"],
                        "graph_nodes": result.graph_nodes,
                        "graph_edges": result.graph_edges,
                        "status": result.status,
                    }
                )

            embedder = engine.vectors.embedder if engine.vectors is not None else None
            calls_before = int(getattr(embedder, "calls", 0))
            for repository in manifest["repositories"]:
                name = str(repository["name"])
                started = time.perf_counter()
                result = engine.sync_source(name, "incremental")
                elapsed = time.perf_counter() - started
                incremental_rows.append(
                    {
                        "source": name,
                        "seconds": round(elapsed, 4),
                        "indexed_artifacts": result.indexed_artifacts,
                        "skipped_artifacts": result.skipped_artifacts,
                        "expected_artifacts": samples[name]["files"],
                        "status": result.status,
                    }
                )
            incremental_embedding_calls = int(getattr(embedder, "calls", 0)) - calls_before

            search = HybridSearch(
                SearchStore(engine.state),
                vector=engine.vector_searcher(),
                node_index=engine.node_index,
            )
            for repository in manifest["repositories"]:
                name = str(repository["name"])
                query = str(repository["query"])
                for mode in RETRIEVAL_MODES:
                    started = time.perf_counter()
                    payload = search.search_context(
                        config.knowledge_base_id,
                        query,
                        mode=mode,
                        max_results=8,
                        source_name=name,
                        graph=engine.graph_builder.graph,
                    )
                    elapsed = time.perf_counter() - started
                    search_rows.append(
                        {
                            "source": name,
                            "mode": mode,
                            "seconds": round(elapsed, 4),
                            "results": len(payload["results"]),
                            "counts": payload["counts"],
                        }
                    )
        finally:
            engine.close()

        total_files = sum(item["files"] for item in samples.values())
        full_seconds = sum(float(item["seconds"]) for item in full_rows)
        incremental_seconds = sum(float(item["seconds"]) for item in incremental_rows)
        search_seconds = [float(item["seconds"]) for item in search_rows]
        report: dict[str, Any] = {
            "schema_version": 1,
            "offline": {"llm": "disabled", "embeddings": "stub", "network_calls": 0},
            "retrieval_modes": list(RETRIEVAL_MODES),
            "samples": samples,
            "full": full_rows,
            "incremental": incremental_rows,
            "incremental_embedding_calls": incremental_embedding_calls,
            "search": {
                "runs": search_rows,
                "median_seconds": round(statistics.median(search_seconds), 4),
                "p95_seconds": round(_percentile(search_seconds, 0.95), 4),
            },
            "totals": {
                "files": total_files,
                "full_seconds": round(full_seconds, 4),
                "full_files_per_second": round(total_files / max(full_seconds, 0.000001), 4),
                "incremental_seconds": round(incremental_seconds, 4),
                "peak_rss_mb": _resident_mb(),
            },
            "budgets": dict(manifest["budgets"]),
        }
        correctness = {
            "all_sources_healthy": all(item["status"] == "healthy" for item in full_rows),
            "all_samples_indexed": all(
                item["indexed_artifacts"] == item["expected_artifacts"] for item in full_rows
            ),
            "incremental_is_noop": all(
                item["indexed_artifacts"] == 0
                and item["skipped_artifacts"] == item["expected_artifacts"]
                for item in incremental_rows
            ),
            "incremental_embedding_calls_zero": incremental_embedding_calls == 0,
            "all_searches_return_results": all(item["results"] > 0 for item in search_rows),
            "text_not_duplicated_in_fanout": "text" not in RETRIEVAL_MODES,
        }
        report["correctness"] = correctness
        report["checks"] = _checks(report, manifest["budgets"])
        report["passed"] = all(correctness.values()) and all(
            item["passed"] for item in report["checks"]
        )
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return report
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--repositories-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run_repository_benchmark(args.manifest, args.repositories_root, args.output)
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":  # pragma: no cover - exercised by CI
    raise SystemExit(main())
