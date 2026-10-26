"""Seed a small region for the UI render check. Prints the config path.

Its own file rather than a string inside `ui/ci-render.mjs`, for two reasons
that both cost time before it moved here. Escaping a Python program through a
JavaScript template literal is a minefield — every backslash means something to
both languages — and the first version failed with an unterminated string
literal that was invisible in either file alone. And a seeding routine that
lives in a `.mjs` is a Python program nothing lints, formats or typechecks.

It deliberately does **not** import from `tests/`. The version that did pulled
in pytest, so it passed locally where the dev extra is installed and failed in
CI's Node-only job; and a CI script coupled to test internals breaks whenever a
fixture is renamed, for a check that has nothing to do with the tests.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import yaml

#: Questions the region is asked. Two documents answer them; the rest of the
#: corpus is decoys that plausibly could.
QUERIES = [
    "where is invoice retry configured",
    "filewatch daemon restart schedule",
    "invoice retry handler location",
    "which module owns the retry policy",
    "how does the runbook describe restarts",
    "invoice retry policy owner",
    "nightly restart window",
    "retry behaviour for invoices",
]


def write_corpus(docs: Path) -> None:
    """Two answers and six decoys.

    The decoys are the point. With a narrow result cut they are what makes
    *rank* matter — without them every query is served whatever the ranking
    does, the stage histogram is pure `served`, the batch correctly proposes
    nothing, and the sweep charts have no marks to draw. A render check over
    that corpus would pass while photographing an empty page.
    """

    docs.mkdir(parents=True, exist_ok=True)
    (docs / "invoice.md").write_text(
        "# Invoice retry\n\nInvoiceRetryPolicy governs invoice retry behaviour.\n",
        encoding="utf-8",
    )
    (docs / "runbook.md").write_text(
        "# Kestrel Runbook\n\nThe filewatch daemon restarts nightly at 0300 UTC.\n",
        encoding="utf-8",
    )
    for index in range(6):
        (docs / f"noise-{index}.md").write_text(
            f"# Retry notes {index}\n\nretry retry invoice retry policy handler "
            f"restart nightly daemon configuration module {index}.\n",
            encoding="utf-8",
        )


def config_for(work: Path, docs: Path, port: int) -> dict:
    return {
        "pheasant": {
            "name": "kb",
            "defaults": "standalone",
            "state_path": str(work / "state"),
            "workspace_root": str(work / "ws"),
            "exports_path": str(work / "exports"),
        },
        "storage": {"graph_snapshots": False},
        # `server.port`, not `server.api.port` — the latter is not a field, so
        # `model_validate` drops it and the region quietly serves on the
        # default. The render check then polled the right port by accident,
        # which would have become a mystifying failure the moment anybody
        # changed either side.
        "server": {"port": port},
        "observability": {
            "interactions": {
                "enabled": True,
                "flush_batch_size": 1,
                # Every search, so the live-health panel clears its own
                # minimum-sample floor and renders rates rather than an empty
                # state — which is what the check is here to photograph.
                "stage_sample_rate": 1.0,
            }
        },
        # A narrow cut, for the reason `write_corpus` explains.
        "tuning": {"enabled": True, "max_results": 2, "minimum_paired_queries": 2},
        "evaluation": {
            "enabled": True,
            "proof": {
                "minimum_eligible_queries": 1,
                "minimum_evidenced_queries": 1,
                "minimum_independent_interactions": 1,
                "maximum_single_query_proof_share": 1.0,
            },
            "cohorts": {"anchor_minimum_queries": 2},
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", required=True)
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args()

    logging.disable(logging.INFO)
    work = Path(args.work)
    docs = work / "ws" / "docs"
    for name in ("state", "exports"):
        (work / name).mkdir(parents=True, exist_ok=True)
    write_corpus(docs)

    raw = config_for(work, docs, args.port)
    config_path = work / "pheasant.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app
    from pheasant.config.schema import PheasantConfig

    app = create_app(PheasantConfig.model_validate(raw), config_path=str(config_path))
    engine = app.state.engine
    engine.sync_source("docs", "full")

    artifacts = {
        str(row["relative_path"]): str(row["id"])
        for row in engine.state.rows("SELECT id, relative_path FROM artifacts")
    }
    with TestClient(app) as client:
        for index, query in enumerate(QUERIES):
            answer = "runbook.md" if ("restart" in query or "runbook" in query) else "invoice.md"
            client.post(
                "/search",
                json={"query": query, "max_results": 2},
                headers={
                    "X-Pheasant-Session": f"s{index % 2}",
                    "X-Pheasant-Principal": "user:ci",
                },
            )
            client.post(
                "/evaluation/evidence",
                json={
                    "query": query,
                    "target_id": artifacts[answer],
                    "event_type": "explicit_accept",
                    "principal": "user:ci",
                    "session_id": f"s{index % 2}",
                    "interaction_id": f"i{index}",
                },
            )
        # Repeat passes, so the live-health panel has samples to report.
        for _ in range(3):
            for query in QUERIES:
                client.post("/search", json={"query": query, "max_results": 2})

    from pheasant.tuning.runner import run_tuning

    outcome = run_tuning(engine, force=True)
    print(f"batch: {outcome.status}, {outcome.trials_run} trials")
    if outcome.status != "completed":
        raise SystemExit(f"the seeded batch ended as {outcome.status}: {outcome.skipped_reason}")
    if not outcome.trials_run:
        # No trials means no sweeps, and the render check asserts marks were
        # drawn. Fail here, where the cause is legible, rather than there.
        raise SystemExit("the seeded batch ran no trials; the sweep charts would be empty")

    buffer = getattr(app.state, "interaction_buffer", None)
    if buffer is not None and hasattr(buffer, "flush"):
        buffer.flush()
    engine.close()
    print(str(config_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
