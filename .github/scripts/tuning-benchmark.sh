#!/usr/bin/env bash
# Run the tuning plane against SciFact, and assert the numbers are real.
#
# The other CI jobs prove the plane *works*. This one proves it says something:
# a diagnosis over a corpus whose known-positives are expert annotations rather
# than something this repository wrote.
#
# What it asserts, and why each would otherwise rot silently:
#
#   1. The corpus indexed, PDFs included. A PDF that extracts as nothing is
#      accepted and then indexed as nothing — the drift that
#      DOCUMENT_EXTENSIONS/EXTRACTED_EXTENSIONS is asserted set-equal to catch.
#   2. The batch reached a decision. "Completed with no opinion" and "failed"
#      are different, and only one of them is a result.
#   3. The ablation isolated the arms. A zero-weighted arm is not an excluded
#      arm, and getting that wrong produced a "vector alone" score that was the
#      text arm's ranking verbatim.
#   4. Retrieval lands in a plausible range for the benchmark. A pipeline that
#      quietly stops ranking still completes a batch; a reciprocal rank near
#      zero on SciFact does not happen to a working lexical arm.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
LOG="${ROOT}/tuning-benchmark.log"
CORPUS="${ROOT}/.benchmark-corpus/scifact"
: >"${LOG}"
trap 'rm -rf "${WORK}"' EXIT

cd "${ROOT}"
python - "${WORK}" "${CORPUS}" <<'PY' 2>&1 | tee -a "${LOG}"
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

work, corpus = Path(sys.argv[1]), Path(sys.argv[2])
index = json.loads((corpus.parent / "benchmark.json").read_text())
print(
    f"corpus: {index['documents']} documents ({index['pdfs']} PDFs), "
    f"{index['claims']} claims, {index['judgements']} expert judgements"
)

import yaml  # noqa: E402

from pheasant.config.schema import PheasantConfig  # noqa: E402

for name in ("state", "exports"):
    (work / name).mkdir(parents=True, exist_ok=True)
raw = {
    "pheasant": {
        "name": "scifact",
        "defaults": "standalone",
        "state_path": str(work / "state"),
        "workspace_root": str(corpus.parent),
        "exports_path": str(work / "exports"),
    },
    "storage": {"graph_snapshots": False},
    "sync": {"watcher": {"enabled": False}, "scheduler": {"enabled": False}},
    "observability": {"interactions": {"enabled": True, "flush_batch_size": 1}},
    "tuning": {"enabled": True, "max_results": 5, "minimum_paired_queries": 5},
    "evaluation": {
        "enabled": True,
        "proof": {
            "minimum_eligible_queries": 5,
            "minimum_evidenced_queries": 5,
            "minimum_independent_interactions": 2,
            "maximum_single_query_proof_share": 1.0,
        },
        "cohorts": {"anchor_minimum_queries": 10},
    },
    "sources": [
        {
            "name": "scifact",
            "type": "markdown_folder",
            "path": str(corpus),
            "include": ["**/*.md", "**/*.pdf"],
            "sync": {"on_startup": False},
        }
    ],
}
config_path = work / "pheasant.yaml"
config_path.write_text(yaml.safe_dump(raw))

from fastapi.testclient import TestClient  # noqa: E402

from pheasant.api.app import create_app  # noqa: E402

app = create_app(PheasantConfig.model_validate(raw), config_path=str(config_path))
engine = app.state.engine
engine.sync_source("scifact", "full")

# 1. The corpus indexed, PDFs included.
rows = engine.state.rows("SELECT id, relative_path FROM artifacts")
paths = [str(r["relative_path"]) for r in rows]
pdfs = [p for p in paths if p.endswith(".pdf")]
chunked = engine.state.rows(
    "SELECT COUNT(DISTINCT artifact_id) AS n FROM chunks "
    "WHERE artifact_id IN (SELECT id FROM artifacts WHERE relative_path LIKE '%.pdf')"
)[0]["n"]
print(f"indexed: {len(paths)} artifacts, {len(pdfs)} PDFs, {chunked} PDFs produced chunks")
assert len(paths) >= index["documents"] * 0.9, "most of the corpus failed to index"
assert pdfs, "no PDFs indexed; the extraction path was not exercised"
assert chunked >= len(pdfs) * 0.9, (
    f"only {chunked} of {len(pdfs)} PDFs produced chunks — accepted and indexed as nothing"
)

with TestClient(app) as client:
    artifacts = {str(r["relative_path"]): str(r["id"]) for r in rows}
    for entry in index["queries"]:
        client.post("/search", json={"query": entry["query"], "max_results": 5})
    recorded = 0
    for entry in index["evidence"]:
        target = artifacts.get(entry["path"])
        if not target:
            continue
        client.post(
            "/evaluation/evidence",
            json={
                "query": entry["query"],
                "target_id": target,
                "event_type": entry["event_type"],
                "principal": "ci",
                "session_id": "ci",
                "interaction_id": f"ci-{entry['doc_id']}",
            },
        )
        recorded += 1
print(f"recorded {recorded} expert judgements")
assert recorded >= 20, "too few judgements reached the region to measure anything"

from pheasant.tuning.runner import run_tuning  # noqa: E402

outcome = run_tuning(engine, force=True)
print(f"batch: {outcome.status}, {outcome.trials_run} trials")

# 2. The batch reached a decision.
assert outcome.status == "completed", outcome.skipped_reason
assert outcome.decision is not None, "the batch completed without a decision"
print(f"decision: {outcome.decision.outcome} — {outcome.decision.reason[:140]}")

diagnosis = outcome.diagnosis
print(f"diagnosis: {diagnosis.summary}")

# 3. The ablation isolated the arms.
mechanisms = outcome.mechanisms
assert mechanisms, "no mechanism ablation was produced"
for arm, entry in sorted(mechanisms.items()):
    score = entry["objective_score"]
    gain = entry.get("hybrid_gain")
    suffix = f"  merge adds {gain:+.4f}" if gain is not None else "  <- the merge"
    print(f"  {arm:8} {score if score is None else round(score, 4)}{suffix}")
text_only = mechanisms.get("text", {}).get("objective_score")
hybrid = mechanisms.get("hybrid", {}).get("objective_score")
assert text_only is not None and hybrid is not None, "arms were not measured"
# With no embedder configured the vector arm has nothing, and must say so
# rather than reporting whichever arm did have candidates.
if mechanisms.get("vector", {}).get("provider") == "off":
    assert mechanisms["vector"]["objective_score"] == 0.0, (
        "the vector arm has no embedder and scored non-zero — the ablation is "
        "measuring another arm's ranking"
    )

# 4. Retrieval is in a plausible range for this benchmark.
assert 0.3 <= text_only <= 1.0, (
    f"lexical reciprocal rank {text_only:.4f} is outside anything a working BM25 "
    "arm produces on SciFact"
)
print("benchmark assertions passed")
engine.close()
PY
