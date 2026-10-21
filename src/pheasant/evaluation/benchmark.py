"""Measure what an evaluation batch actually costs, and check the model against it.

`capacity.py` holds the coefficients that answer "how big should the evaluation
node and its volume be". This is the run that produces them, and the run that
notices when they have rotted.

Deterministic and fully offline: a seeded generator writes a synthetic corpus
and a synthetic ledger, then a **real** batch runs through the real search path
— no mocked retrieval anywhere, for the same reason
`pheasant.memory.benchmark` refuses to mock its own. A measurement taken
against a stand-in measures the stand-in.

What comes out is three things:

* **measured** — what this machine actually did: seconds, milliseconds per
  replay, bytes landed in ``/state``, peak checkpoint bytes.
* **projected** — what :func:`pheasant.capacity.project_evaluation` said it
  would do, for the same shape.
* **ladder** — the projection at production cohort sizes, which is the table an
  operator sizing a volume actually reads.

The comparison is the point. A model whose numbers nobody checks against a
machine is a model that quietly stops describing anything, and the specific
failure it guards is the one this repository has already shipped once: a
coefficient measured on a small corpus, extrapolated as a line, that was
actually a curve.

Reproduce with::

    python -m pheasant.evaluation.benchmark --output evaluation-capacity.json
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

WORDS = (
    "invoice retry policy ladder tenant backoff runbook rollout canary daemon "
    "filewatch schedule nightly escalation rota gateway billing finance ledger "
    "reconcile dispatch queue worker shard region contract vocabulary"
).split()


def _corpus(root: Path, files: int, seed: int) -> Path:
    """A synthetic corpus with enough vocabulary that retrieval has to choose.

    Uniform noise would let every query match everything, which measures the
    fusion's tie-breaking rather than its cost.
    """

    rng = random.Random(seed)
    docs = root / "corpus"
    docs.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        topic = WORDS[index % len(WORDS)]
        body = " ".join(rng.choice(WORDS) for _ in range(120))
        (docs / f"doc-{index:04d}.md").write_text(
            f"# {topic.title()} {index}\n\nThe {topic} subsystem. {body}\n", encoding="utf-8"
        )
    return docs


def _config(root: Path, docs: Path, queries: int) -> Any:
    from pheasant.config.schema import PheasantConfig

    for name in ("state", "exports", "memory"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "capacity",
                "defaults": "standalone",
                "state_path": str(root / "state"),
                "workspace_root": str(root),
                "exports_path": str(root / "exports"),
            },
            "storage": {"graph_snapshots": False},
            # Off: this seeds the ledger directly, and a live buffer with no
            # lifespan to tear it down would leak into whatever runs next.
            "observability": {"interactions": {"enabled": False}},
            "memory": {"steering_enabled": True},
            "evaluation": {
                "enabled": True,
                "maximum_queries_per_run": queries * 64,
                "maximum_runtime_seconds": 3600,
                "proof": {
                    "minimum_eligible_queries": 1,
                    "minimum_evidenced_queries": 1,
                    "minimum_independent_interactions": 1,
                    "maximum_single_query_proof_share": 1.0,
                },
                "cohorts": {
                    "anchor_minimum_queries": 2,
                    "maximum_queries_per_cohort": queries,
                },
            },
            "sources": [
                {
                    "name": "docs",
                    "type": "markdown_folder",
                    "path": str(docs),
                    "include": ["**/*.md"],
                    "sync": {"on_startup": False},
                }
            ],
        }
    )


def _seed_ledger(engine: Any, queries: int, seed: int) -> None:
    """Recorded traffic, plus proof on a quarter of it.

    A quarter rather than all of it because that is roughly what a real region
    looks like — most queries are never judged — and because a cohort where
    every query is evidenced would measure a path the metrics rarely take.
    """

    import pheasant.evaluation as evaluation
    from pheasant.sync.log_queue import write_events
    from pheasant.telemetry.interactions import InteractionEvent

    rng = random.Random(seed)
    rows = engine.state.rows("SELECT id FROM artifacts ORDER BY id LIMIT 200")
    ids = [str(row["id"]) for row in rows] or [""]
    texts = [f"where is {WORDS[index % len(WORDS)]} {index} configured" for index in range(queries)]
    write_events(
        engine.state,
        [
            InteractionEvent(
                kb_id=engine.config.knowledge_base_id,
                operation="/search",
                modality="ui",
                principal=f"user:{index % 5}",
                session_id=f"s{index % 12}",
                trace_id=f"{index:032x}",
                span_id=f"{index:016x}",
                started_at=f"2026-01-01T00:00:00.{index:06d}Z",
                status="ok",
                duration_ms=8.0,
                query_text=text,
                result_ids=[rng.choice(ids)],
                result_paths=["doc-0000.md"],
                result_count=1,
                top_score=0.8,
            )
            for index, text in enumerate(texts)
        ],
    )
    for index in range(0, queries, 4):
        evaluation.record_evidence(
            engine.state,
            engine.config,
            query=texts[index],
            target_id=rng.choice(ids),
            event_type="selected" if index % 8 else "explicit_accept",
            principal=f"user:{index % 5}",
            session_id=f"s{index % 12}",
            interaction_id=f"bench-{index}",
        )


def _state_bytes(state_path: Path) -> int:
    return sum(item.stat().st_size for item in state_path.rglob("*") if item.is_file())


def run_benchmark(*, queries: int = 40, files: int = 120, seed: int = 1337) -> dict[str, Any]:
    """One real batch, timed and measured, against the projection for its shape."""

    import pheasant.evaluation as evaluation
    from pheasant.api.app import create_app
    from pheasant.capacity import SECONDS_PER_QUERY_VARIANT, project_evaluation

    root = Path(tempfile.mkdtemp(prefix="pheasant-eval-bench-"))
    try:
        docs = _corpus(root, files, seed)
        config = _config(root, docs, queries)
        config_path = root / "pheasant.yaml"
        config_path.write_text("# generated by the capacity benchmark\n", encoding="utf-8")
        app = create_app(config, config_path=str(config_path))
        engine = app.state.engine
        try:
            engine.sync_source("docs", "full")
            _seed_ledger(engine, queries, seed)
            state_path = Path(config.pheasant.state_path)
            before_bytes = _state_bytes(state_path)

            # Peak checkpoint bytes are sampled *during* the run: they are
            # cleared when it completes, so measuring afterwards would report
            # zero and call the transient cost free.
            peak = {"bytes": 0}
            from pheasant.evaluation.replay import ReplayEngine

            original = ReplayEngine.replay_variant

            def sampled(self: Any, cohort: Any, variant: Any) -> Any:
                result = original(self, cohort, variant)
                try:
                    rows = engine.state.rows(
                        "SELECT COALESCE(SUM(LENGTH(results_json)), 0) AS c FROM evaluation_replays"
                    )
                    peak["bytes"] = max(peak["bytes"], int(rows[0]["c"]))
                except Exception:  # noqa: BLE001 - a sample must not fail the run
                    pass
                return result

            ReplayEngine.replay_variant = sampled  # type: ignore[method-assign]
            started = time.monotonic()
            try:
                outcome = evaluation.run(engine)
            finally:
                ReplayEngine.replay_variant = original  # type: ignore[method-assign]
            elapsed = time.monotonic() - started

            developer = outcome.report["explanations"]["developer"]
            replays = len(developer["runtime"]["variants"]) * len(developer["cohorts"])
            cohorts = len(developer["cohorts"])
            variants = len(developer["runtime"]["variants"])
            anchor = developer["cohorts"].get("anchor", {}).get("query_count", queries)
            replay_units = sum(
                cohort["query_count"] * variants for cohort in developer["cohorts"].values()
            )
            after_bytes = _state_bytes(state_path)

            metric_rows = engine.state.rows("SELECT COUNT(*) AS c FROM evaluation_metrics")[0]["c"]
            proof_rows = engine.state.rows("SELECT COUNT(*) AS c FROM evaluation_proofs")[0]["c"]
            projected = project_evaluation(
                anchor,
                variants=variants,
                cohorts=cohorts,
                max_stored_per_query_results=int(
                    config.evaluation.maximum_stored_per_query_results
                ),
            )

            ladder = [
                project_evaluation(size, variants=variants, cohorts=cohorts).as_dict()
                for size in (20, 50, 100, 200, 500, 1000)
            ]
            warnings: list[str] = []
            measured_ms = round(elapsed / max(1, replay_units) * 1000, 3)
            modelled_ms = SECONDS_PER_QUERY_VARIANT * 1000
            if measured_ms > modelled_ms * 40:
                warnings.append(
                    f"A replay took {measured_ms} ms against a modelled {modelled_ms:.1f} ms. "
                    "Either this machine is much slower than the one that produced the "
                    "coefficient, or the replay loop has changed shape."
                )
            if outcome.status != "completed":
                warnings.append(f"The benchmark batch ended as {outcome.status}, not completed.")

            return {
                "status": outcome.status,
                "spec": {"queries": queries, "files": files, "seed": seed},
                "measured": {
                    "queries": anchor,
                    "variants": variants,
                    "cohorts": cohorts,
                    "replays": replay_units,
                    "cohort_variant_pairs": replays,
                    "run_seconds": round(elapsed, 2),
                    "ms_per_replay": measured_ms,
                    "state_bytes": after_bytes - before_bytes,
                    "peak_checkpoint_bytes": peak["bytes"],
                    "metric_rows": int(metric_rows),
                    "proof_rows": int(proof_rows),
                },
                "projected": projected.as_dict()
                | {
                    "run_seconds": round(projected.run_seconds, 2),
                    "state_bytes_per_run": projected.state_bytes_per_run,
                    "peak_checkpoint_bytes": projected.peak_checkpoint_bytes,
                },
                "coefficients": {
                    "SECONDS_PER_QUERY_VARIANT": SECONDS_PER_QUERY_VARIANT,
                },
                "ladder": ladder,
                "warnings": warnings,
                "note": (
                    "Measured through the real search path on a synthetic corpus. The shape "
                    "holds across machines; the constants move, so re-run this on your own "
                    "hardware before trusting the ladder for provisioning."
                ),
            }
        finally:
            engine.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=int, default=40)
    parser.add_argument("--files", type=int, default=120)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--output", default=None, help="Write the report as JSON here.")
    args = parser.parse_args(argv)

    report = run_benchmark(queries=args.queries, files=args.files, seed=args.seed)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)
    for warning in report["warnings"]:
        print(f"WARNING: {warning}", file=sys.stderr)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
