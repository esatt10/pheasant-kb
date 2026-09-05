"""One whole readiness check, against a region that can actually pass it.

The plane's parts are tested elsewhere; this is the test that the parts add up.
It is not a smoke test — it asserts the **verdict**, which means every probe
has to have run and every gate has to have been decided, and it asserts the
verdict twice: once on a region configured for a scored experiment (memory
source, ACL enforcement, a denylist) and once on a region that is not.

That second half is the point. A readiness plane that only ever reports GO on
a fully-configured region is a plane nobody learns anything from; what an
operator actually needs is for an under-configured region to say *which* box
is unchecked and why, without that reading as a failure of the region.

Three defects in this plane's first version were invisible to unit tests of
its parts and obvious the moment a check ran: a contract naming three symbols
that did not exist, a counter that counted the region's own writes as caller
submissions, and a gate set reporting PASS with three quarters of its gates
skipped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.mcp_server.tools import PheasantTools

CORPUS = {
    "runbook.md": "# Rotation runbook\n\nThe gateway rotates credentials nightly.\n",
    "protocol.md": "# Coordination\n\nA region publishes a semantic contract.\n",
}


def _region(root: Path, *, configured: bool) -> PheasantConfig:
    """A region set up for an experiment, or one that merely runs.

    ``configured`` is the whole variable: a memory source, ACL enforcement and
    a corpus denylist are what the memory and isolation gates need, and a
    region without them is not broken — it is a region that has not been asked
    to hold an isolated multi-arm experiment yet.
    """

    workspace = root / "workspace"
    for relative, text in CORPUS.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    sources: list[dict[str, Any]] = [
        {
            "name": "docs",
            "type": "markdown_folder",
            "path": str(workspace),
            "include": ["**/*.md"],
        }
    ]
    payload: dict[str, Any] = {
        "pheasant": {
            "name": "readiness-check",
            "state_path": str(root / "state"),
            "workspace_root": str(workspace),
            "exports_path": str(root / "exports"),
        },
        "server": {"host": "127.0.0.1"},
        "storage": {"graph_snapshots": False},
        "readiness": {
            "enabled": True,
            "latency_probe_queries": 5,
            "concurrency_probe_writers": 3,
            "concurrency_probe_items": 2,
        },
        "sources": sources,
    }
    if configured:
        payload["readiness"]["corpus_denylist"] = ["benchmark/*", "*.answers.json"]
        payload["security"] = {"acl_enforced": True}
        memory_root = root / "state" / "memory"
        memory_root.mkdir(parents=True, exist_ok=True)
        sources.append({"name": "memory", "type": "memory", "path": str(memory_root)})
    return PheasantConfig.model_validate(payload)


def _run(config: PheasantConfig) -> dict[str, Any]:
    tools = PheasantTools(config)
    try:
        for source in config.sources:
            tools.engine.sync_source(source.name, "full")
        tools.engine.reload_graph()
        return tools.run_readiness_check()
    finally:
        tools.engine.close()


@pytest.fixture(scope="module")
def configured(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _run(_region(tmp_path_factory.mktemp("ready"), configured=True))


@pytest.fixture(scope="module")
def bare(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _run(_region(tmp_path_factory.mktemp("bare"), configured=False))


def test_a_configured_region_reaches_go(configured: dict[str, Any]) -> None:
    """Every probe ran, every gate was decided, and the verdict is GO.

    The assertion on `probes_skipped` is the load-bearing half: a check that
    reached GO by skipping is the shape this plane refuses to produce, and it
    would look identical here without it.
    """

    assert configured["summary"]["probes_skipped"] == {}
    assert configured["summary"]["probes_failed"] == []
    assert configured["verdict"] == "go", configured["summary"]
    for name, row in configured["gate_sets"].items():
        assert row["passed"] is True, (name, row["blocking_failures"], row["skipped"])


def test_the_core_gates_are_the_specification_s_core_boxes(
    configured: dict[str, Any],
) -> None:
    """Every Core box in §7.1 that pheasant can answer has a gate here.

    Named individually rather than counted: a gate silently dropped from the
    set would keep the count honest only until somebody added another one.
    """

    decided = {gate["gate_id"] for gate in configured["gate_sets"]["core"]["gates"]}
    assert decided >= {
        "service_identity",
        "mcp_discovery",
        "ingest_to_retrieval",
        "idempotency",
        "partial_failure",
        "index_barrier",
        "reconciliation",
        "snapshot_immutable",
        "retrieval_lineage",
        "resolve_result",
        "benchmark_contamination",
        "structured_errors",
        "performance_slo",
        "memory_state_visible",
    }


def test_an_underconfigured_region_says_which_box_is_unchecked(
    bare: dict[str, Any],
) -> None:
    """Not a failure — an incomplete answer, with the reason attached.

    A region with no memory source and no ACL enforcement cannot demonstrate
    memory isolation, and reporting that as a *failure* would tell an operator
    to go and fix something that is not broken. Reporting it as a pass would be
    worse. So the gate set is `None`, and each skipped gate carries the
    sentence that says what to turn on.

    **Core is incomplete here too**, and that surprised the author of this
    test: with `corpus_denylist` empty there is no benchmark boundary to prove,
    so `benchmark_contamination` is unevaluated. Which is right, and is the
    specification's own rule — "any unchecked item is a NO-GO for scored
    results". A region can be perfectly healthy and still not ready to be
    measured, and the two are different sentences.
    """

    assert bare["gate_sets"]["core"]["passed"] is None
    assert bare["gate_sets"]["memory"]["passed"] is None
    assert bare["verdict"] == "incomplete"

    core_reasons = " ".join(row["reason"] for row in bare["gate_sets"]["core"]["skipped"])
    assert "corpus_denylist" in core_reasons

    memory_reasons = " ".join(row["reason"] for row in bare["gate_sets"]["memory"]["skipped"])
    assert "memory source" in memory_reasons
    assert "acl_enforced" in memory_reasons

    # And everything the region *could* demonstrate, it did.
    assert bare["summary"]["probes_failed"] == []


def test_a_skipped_probe_is_never_counted_as_passed(bare: dict[str, Any]) -> None:
    skipped = bare["summary"]["probes_skipped"]
    assert skipped, "the bare region should skip the memory probes"
    for probe in bare["probes"]:
        if probe["probe_id"] in skipped:
            assert probe["passed"] is None
            assert probe["skipped_reason"]


def test_the_contract_reports_what_this_build_cannot_do(configured: dict[str, Any]) -> None:
    """`unsupported` is an answer, and it carries its reason.

    A harness needs to tell "this region cannot" from "this region did not
    mention it". Both rows here are real limits: one version of the corpus, and
    no claim-to-claim stance edge.
    """

    contract = configured["contract"]
    assert set(contract["unsupported"]) == {"temporal.corpus_as_of", "evidence.claim_stance"}
    for row in contract["capabilities"]:
        if row["status"] == "unsupported":
            assert row["detail"], f"{row['logical']} is unsupported with no reason"


def test_the_report_renders_failures_and_skips_before_passes(
    bare: dict[str, Any],
) -> None:
    """A reader opening this has a decision to make.

    The twelve gates that passed are not what they are here for, so the
    heading has to say INCOMPLETE rather than leave it to a caveat further
    down that somebody skips.
    """

    from pheasant.readiness.runner import render_report

    text = render_report(bare)
    assert "**Verdict: INCOMPLETE**" in text
    assert "## core — INCOMPLETE" in text
    assert "## memory — INCOMPLETE" in text
    assert "not evaluated:" in text


def test_the_check_leaves_the_configured_sources_alone(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A check writes only to a scratch source it owns.

    It submits documents, indexes them and seals snapshots, all of which are
    real work. None of it may land in a source the region was configured with —
    a readiness check that contaminated the corpus it was certifying would be
    the most expensive possible bug in this plane.
    """

    from pheasant.readiness.probes import PROBE_SOURCE

    root = tmp_path_factory.mktemp("isolated")
    config = _region(root, configured=False)
    workspace = Path(config.pheasant.workspace_root)
    before = sorted(path.name for path in workspace.rglob("*.md"))
    _run(config)
    assert sorted(path.name for path in workspace.rglob("*.md")) == before

    tools = PheasantTools(config)
    try:
        rows = tools.state.rows(
            "SELECT DISTINCT s.name AS name FROM artifacts a "
            "JOIN sources s ON s.id = a.source_id ORDER BY s.name",
            (),
        )
    finally:
        tools.engine.close()
    names = {str(row["name"]) for row in rows}
    assert "docs" in names
    assert names <= {"docs", PROBE_SOURCE}


def test_the_http_surface_gates_the_check_on_opting_in(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The contract is always served; the check is not.

    A check writes, so a stranger's HTTP request must not start one on a region
    that has not opted in. The contract has the opposite rule: answering "can
    this region be measured" with a 404 is indistinguishable from an old build
    that has no contract at all, so it is served either way and reports
    `readiness_enabled` so the caller can tell.
    """

    root = tmp_path_factory.mktemp("optin")
    config = _region(root, configured=False)
    config.readiness.enabled = False
    client = TestClient(create_app(config, config_path=str(root / "pheasant.yaml")))

    contract = client.get("/readiness/contract")
    assert contract.status_code == 200
    assert contract.json()["readiness_enabled"] is False

    refused = client.post("/readiness/check", json={})
    assert refused.status_code == 409
    assert "readiness.enabled" in refused.json()["detail"]
