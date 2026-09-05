"""The readiness plane, end to end and in pieces.

Split roughly the way the plane is: the primitives it rests on (receipts,
seals, lineage, refusal codes), then the contract that cannot rot, then the
gates that cannot be vacuously true, then one full check driven through the
real facade.

The end-to-end test is the one that matters most and is deliberately not a
smoke test: it asserts the *verdict*, which means every probe has to have run
and every gate has to have been decided. Three defects in the plane's first
version were invisible to unit tests of its parts and obvious the moment a
check ran against a region — a contract naming symbols that did not exist, a
counter that counted the region's own writes, and a gate set reporting PASS
with three quarters of its gates skipped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.mcp_server.tools import PheasantTools
from pheasant.persistence import receipts as receipt_ledger
from pheasant.services import ingestion as ingestion_service
from pheasant.services import retrieval as retrieval_service
from pheasant.services import snapshots as snapshot_service
from pheasant.services.errors import ServiceError

CORPUS = {
    "runbook.md": (
        "# Rotation runbook\n\nThe gateway rotates credentials nightly and the "
        "sidecar reloads without downtime.\n"
    ),
    "protocol.md": (
        "# Coordination protocol\n\nA region publishes a semantic contract "
        "derived from its own content.\n"
    ),
}


def _config(root: Path) -> PheasantConfig:
    workspace = root / "workspace"
    for relative, text in CORPUS.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    payload: dict[str, Any] = {
        "pheasant": {
            "name": "readiness",
            "state_path": str(root / "state"),
            "workspace_root": str(workspace),
            "exports_path": str(root / "exports"),
        },
        "server": {"host": "127.0.0.1"},
        "storage": {"graph_snapshots": False},
        "readiness": {
            "enabled": True,
            "corpus_denylist": ["benchmark/*"],
            # Small enough to keep the suite fast, large enough that the
            # latency probe's own floor (5) does not skip it.
            "latency_probe_queries": 6,
            "concurrency_probe_writers": 3,
            "concurrency_probe_items": 2,
        },
        "sources": [
            {
                "name": "docs",
                "type": "markdown_folder",
                "path": str(workspace),
                "include": ["**/*.md"],
            }
        ],
    }
    return PheasantConfig.model_validate(payload)


@pytest.fixture(scope="module")
def region(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("readiness")
    config = _config(root)
    tools = PheasantTools(config)
    tools.engine.sync_source("docs", "full")
    tools.engine.reload_graph()
    client = TestClient(create_app(config, config_path=str(root / "pheasant.yaml")))
    return {
        "config": config,
        "tools": tools,
        "services": tools.services,
        "client": client,
        "kb": config.knowledge_base_id,
    }


# ---------------------------------------------------------------------------
# Receipts and idempotency
# ---------------------------------------------------------------------------


def _submit(region: dict[str, Any], *items: ingestion_service.SubmissionItem, **kwargs: Any):
    return ingestion_service.submit(
        region["services"],
        ingestion_service.SubmissionRequest(items=list(items), source_name="probe", **kwargs),
    )


def _item(path: str, text: str = "content", key: str | None = None):
    return ingestion_service.SubmissionItem(
        relative_path=path, content=text.encode("utf-8"), idempotency_key=key
    )


def test_three_writes_under_one_key_produce_one_receipt(region: dict[str, Any]) -> None:
    """The specification's idempotency box, spelled as it spells it.

    Three rather than two, because a fold that is correct once and wrong on
    accumulation is a real shape: `submissions` must reach 3 while the stored
    object stays 1, and two submissions cannot tell an incrementing counter
    from a boolean.
    """

    receipts = [
        _submit(region, _item("idem.md", "same bytes", key="k-idem"))["accepted"][0]
        for _ in range(3)
    ]
    assert len({receipt["receipt_id"] for receipt in receipts}) == 1
    assert len({receipt["artifact_id"] for receipt in receipts}) == 1
    assert receipts[-1]["submissions"] == 3


def test_crossing_the_index_barrier_is_not_a_submission(region: dict[str, Any]) -> None:
    """The counter says what its name says.

    Regression for a real defect: acknowledging the barrier wrote the receipt
    row too, and the first version counted that — so three caller submissions
    plus one acknowledgement reported four. A harness proving its retry was
    absorbed would have been reading a number that moves when the *region*
    acts. The same shape `pheasant_memory_reinforcement_ratio` was caught by.
    """

    submission = _submit(region, _item("barrier-count.md", "text", key="k-barrier"))
    key = submission["accepted"][0]["idempotency_key"]
    region["tools"].engine.sync_source("docs", "incremental")
    before = ingestion_service.ingest_status(region["services"], None, idempotency_key=key)[
        "receipts"
    ][0]
    ingestion_service.acknowledge_indexed(region["services"], None, None)
    after = ingestion_service.ingest_status(region["services"], None, idempotency_key=key)[
        "receipts"
    ][0]
    assert after["submissions"] == before["submissions"]


def test_a_bad_item_rejects_itself_and_nothing_beside_it(region: dict[str, Any]) -> None:
    """One malformed item must not cost the good items in the same batch.

    The shape this codebase has already been bitten by one layer down: a
    batched insert inside one transaction made a single null `trace_id` roll
    back hundreds of good observations. Validation happens per item, before
    anything is written.
    """

    submission = _submit(
        region,
        _item("good-a.md", "alpha", key="k-good-a"),
        _item("empty.md", "", key="k-empty"),
        _item("good-b.md", "beta", key="k-good-b"),
    )
    assert len(submission["accepted"]) == 2
    assert len(submission["rejected"]) == 1
    rejected = submission["rejected"][0]
    assert rejected["error_code"] == "INVALID_REQUEST"
    assert rejected["retryable"] is False


def test_a_denylisted_path_is_refused_at_the_write(region: dict[str, Any]) -> None:
    """Enforcement, not detection.

    A check that benchmark artifacts are absent can only run once they have
    been indexed, and by then they have been retrievable. The refusal carries
    its own code so a caller cannot mistake it for a size limit and retry
    smaller.
    """

    submission = _submit(region, _item("benchmark/answers.md", "the answer is 42"))
    assert not submission["accepted"]
    assert submission["rejected"][0]["error_code"] == "CORPUS_DENYLISTED"


def test_reconciliation_names_silent_loss_rather_than_differencing_totals(
    region: dict[str, Any],
) -> None:
    """A receipt claiming an artifact the region does not hold is the finding.

    Deliberately not a difference between two counts: two totals can agree
    while one item was lost and another double-written, which is the case a
    reconciliation exists to catch.
    """

    clean = ingestion_service.reconcile(region["services"], None)
    assert clean["silent_loss"] == 0

    receipt_ledger.record(
        region["services"].state,
        kb_id=region["kb"],
        idempotency_key="k-phantom",
        submission_id="sub-phantom",
        source_name="probe",
        now="2026-01-01T00:00:00Z",
        disposition="indexed",
        artifact_id="file:probe:never-existed.md:branch=none",
    )
    dirty = ingestion_service.reconcile(region["services"], None)
    assert dirty["silent_loss"] == 1
    assert dirty["reconciled"] is False
    region["services"].state.execute(
        "DELETE FROM ingest_receipts WHERE idempotency_key=?", ("k-phantom",)
    )


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def test_sealing_twice_over_an_unchanged_region_is_one_snapshot(
    region: dict[str, Any],
) -> None:
    """The id is a digest of the state, so a defensive re-seal is free.

    A harness seals at the start of every arm. If that produced a snapshot per
    attempt, the identifier would name the attempt rather than the state, and
    two arms over one corpus could not be shown to have seen it.
    """

    first = snapshot_service.seal(region["services"], snapshot_service.SealRequest())
    second = snapshot_service.seal(region["services"], snapshot_service.SealRequest())
    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["sealed_at"] == second["sealed_at"]


def test_a_pinned_search_refuses_rather_than_answering_from_a_moved_corpus(
    region: dict[str, Any],
) -> None:
    """The immutability guarantee, in the form this region can actually give.

    It holds one version of its corpus, so it cannot serve a sealed snapshot
    after the corpus behind it moves. What it must never do is answer such a
    query with a *different* corpus while the caller attributes the result to
    the sealed one — so it refuses, with a code that is not retryable because
    the corpus will not move back.
    """

    sealed = snapshot_service.seal(region["services"], snapshot_service.SealRequest())
    snapshot_id = sealed["snapshot_id"]
    answered = retrieval_service.search(
        region["services"],
        retrieval_service.SearchRequest(query="rotation", snapshot_id=snapshot_id),
    )
    assert answered["lineage"]["state"]["snapshot_id"] == snapshot_id
    assert answered["lineage"]["state"]["snapshot_current"] is True

    _submit(region, _item("drift.md", "a document that moves the corpus", key="k-drift"))
    region["tools"].engine.sync_source("docs", "incremental")
    (Path(region["config"].pheasant.workspace_root) / "drifted.md").write_text(
        "# Drifted\n\nNew content after the seal.\n", encoding="utf-8"
    )
    region["tools"].engine.sync_source("docs", "incremental")

    with pytest.raises(ServiceError) as refusal:
        retrieval_service.search(
            region["services"],
            retrieval_service.SearchRequest(query="rotation", snapshot_id=snapshot_id),
        )
    assert refusal.value.code == "SNAPSHOT_DRIFTED"
    assert refusal.value.retryable is False
    assert snapshot_id in str(refusal.value)


def test_an_unknown_snapshot_is_a_refusal_not_an_empty_answer(
    region: dict[str, Any],
) -> None:
    with pytest.raises(ServiceError) as refusal:
        snapshot_service.describe(region["services"], "kb-nonexistent")
    assert refusal.value.code == "UNKNOWN_SNAPSHOT"


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------


def test_every_search_carries_the_lineage_the_specification_requires(
    region: dict[str, Any],
) -> None:
    """Checked as a field list, because what goes wrong is a key disappearing.

    `memory_policy` is reported only when the region holds memory, which is
    right for that key and would be wrong for these: an arm that ran with
    memory off has to be able to *record* that it did, and an absent key is
    indistinguishable from a key nobody looked at.
    """

    payload = retrieval_service.search(
        region["services"],
        retrieval_service.SearchRequest(query="rotation", principal="user:probe"),
    )
    lineage = payload["lineage"]
    assert set(lineage) >= {"query_id", "knowledge_base", "state", "timing", "trace_id"}
    assert set(lineage["state"]) >= {
        "snapshot_id",
        "graph_generation",
        "ranking",
        "memory",
        "as_of",
        "principal",
        "acl_enforced",
        "criteria",
        "mode",
        "max_results",
    }
    assert set(lineage["timing"]) >= {"retrieval_ms", "truncated", "returned"}
    assert "bundle_id" in lineage["state"]["ranking"]
    assert "enabled" in lineage["state"]["memory"]


def test_the_query_id_is_content_addressed_and_clock_free(region: dict[str, Any]) -> None:
    """Two runs of one query under one configuration are one measurement.

    The same argument the snapshot id makes, and for the same reason: an id
    that moved with the wall clock would make them two rows nothing can pair,
    which is what a harness joining its operands to a Pheasant result needs.
    """

    request = retrieval_service.SearchRequest(query="rotation", max_results=5)
    first = retrieval_service.search(region["services"], request)["lineage"]["query_id"]
    second = retrieval_service.search(region["services"], request)["lineage"]["query_id"]
    assert first == second

    different = retrieval_service.search(
        region["services"],
        retrieval_service.SearchRequest(query="rotation", max_results=6),
    )["lineage"]["query_id"]
    assert different != first


def test_the_only_non_deterministic_field_in_a_search_response_is_the_duration(
    region: dict[str, Any],
) -> None:
    """Everything else is a function of the request and the region.

    Worth asserting rather than assuming. A measured field in a response
    breaks every whole-payload comparison downstream — it already broke one in
    `tests/test_taxonomy.py` — so if a second one appears it should be a
    decision somebody made, not a test that quietly starts ignoring more.
    """

    request = retrieval_service.SearchRequest(query="coordination protocol")
    first = retrieval_service.search(region["services"], request)
    second = retrieval_service.search(region["services"], request)
    for payload in (first, second):
        payload["lineage"]["timing"].pop("retrieval_ms")
    assert first == second


def test_both_surfaces_carry_the_same_lineage_state(region: dict[str, Any]) -> None:
    """One implementation, so the state half must be identical.

    Two fields are excluded and for opposite reasons. `timing` is a
    measurement, which is the whole reason it is a separate block. And
    `graph_generation` is a property of the *replica* rather than of the
    operation — it exists precisely so a replica that has not reloaded is
    detectable — so two surfaces backed by two engines, one of which has
    indexed since the other last reloaded, correctly disagree about it. This
    test caught exactly that, in a module where an earlier test ingests.
    Asserting it equal here would be asserting that the staleness signal does
    not work.
    """

    over_http = (
        region["client"]
        .post("/search", json={"query": "rotation", "mode": "hybrid", "max_results": 5})
        .json()
    )
    over_mcp = region["tools"].search_context(
        knowledge_base=region["kb"], query="rotation", mode="hybrid", max_results=5
    )
    http_state = dict(over_http["lineage"]["state"])
    mcp_state = dict(over_mcp["lineage"]["state"])
    assert http_state.pop("graph_generation")
    assert mcp_state.pop("graph_generation")
    assert http_state == mcp_state
    assert over_http["lineage"]["query_id"] == over_mcp["lineage"]["query_id"]


# ---------------------------------------------------------------------------
# Structured refusals
# ---------------------------------------------------------------------------


def test_a_refusal_carries_a_code_and_a_retryability_flag() -> None:
    """Branching on English is how a client breaks on a reworded sentence.

    `SNAPSHOT_DRIFTED` and `REGION_BUSY` share a status code and differ in
    exactly the field that decides what a caller does next, which is why
    `retryable` is a field rather than something inferred from the status.
    """

    from pheasant.services.errors import RegionBusy, SnapshotDrifted

    drifted = SnapshotDrifted("kb-x", ["corpus.content_manifest_digest"])
    busy = RegionBusy("a sync holds the lease")
    assert drifted.status == busy.status == 409
    assert drifted.retryable is False
    assert busy.retryable is True
    assert drifted.as_dict()["code"] == "SNAPSHOT_DRIFTED"


def test_http_carries_the_code_beside_the_unchanged_text(region: dict[str, Any]) -> None:
    """The asymmetry between the surfaces, asserted where it lives.

    MCP's `ToolError` carries a string and nothing else, so the code rides
    beside the text over HTTP rather than inside it — spelling it into the
    message would change the refusal text on *both* surfaces to serve one.
    """

    response = region["client"].post("/search", json={"query": "x", "knowledge_base": "nope"})
    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "Unknown knowledge base: nope"
    assert body["code"] == "UNKNOWN_KNOWLEDGE_BASE"
    assert body["retryable"] is False


def test_the_predicted_artifact_id_matches_the_pipeline_s_own(region: dict[str, Any]) -> None:
    """A receipt names its artifact before the sync runs, so the grammar is duplicated.

    Rule 3 makes the stable-ID grammar a contract, and this plane predicts one
    in `services/ingestion._artifact_id` so an acknowledgement can be a lookup
    rather than a search. Duplicated grammar is a hazard, so it is asserted
    against the pipeline's construction rather than trusted: if the id grammar
    ever changes, this fails here instead of silently making every receipt
    unacknowledgeable.
    """

    from pheasant.config.schema import SourceConfig, SourceType
    from pheasant.registry.source_registry import SourceRegistry

    submission = _submit(region, _item("predicted.md", "content", key="k-predicted"))
    predicted = submission["accepted"][0]["artifact_id"]
    tools = region["tools"]
    if not any(source.name == "probe" for source in tools.config.sources):
        source = SourceConfig(
            name="probe",
            type=SourceType.document_folder,
            path=Path(submission["directory"]),
            enabled=True,
        )
        SourceRegistry(tools.config, tools.state).register_source(source)
        tools.config.sources.append(source)
    tools.engine.sync_source("probe", "incremental")
    rows = region["services"].state.rows("SELECT id FROM artifacts WHERE id=?", (predicted,))
    assert rows, (
        f"the pipeline did not mint {predicted}. `services/ingestion._artifact_id` "
        "duplicates `ingestion/pipeline`'s grammar; one of them moved."
    )


# ---------------------------------------------------------------------------
# The denylist governs every door, not just the submission one
# ---------------------------------------------------------------------------


def test_a_denylisted_file_in_a_folder_source_is_never_indexed(
    tmp_path: Path,
) -> None:
    """The door the first version of this control left open.

    `check_denylist` was enforced only in `services/ingestion.submit`, so a
    region with a denylist configured still indexed a benchmark answer key that
    arrived through a folder source, the UI drop zone, a git repository or any
    connector — while the readiness gate reported the boundary intact. A
    control on one of several doors is a door with a sign on it.

    This plants the file the way every one of those paths effectively does —
    bytes in a directory a source watches — and asserts the engine refuses it
    before reading it.
    """

    workspace = tmp_path / "workspace"
    (workspace / "benchmark").mkdir(parents=True)
    (workspace / "ordinary.md").write_text("# Ordinary\n\nRegular content.\n", encoding="utf-8")
    (workspace / "benchmark" / "answers.md").write_text(
        "# Answer key\n\nThe answer is zarquon.\n", encoding="utf-8"
    )
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "denylist",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "readiness": {"enabled": True, "corpus_denylist": ["benchmark/*"]},
            "sources": [
                {
                    "name": "docs",
                    "type": "markdown_folder",
                    "path": str(workspace),
                    "include": ["**/*.md"],
                }
            ],
        }
    )
    tools = PheasantTools(config)
    try:
        result = tools.engine.sync_source("docs", "full")
        held = [
            str(row["relative_path"])
            for row in tools.state.rows("SELECT relative_path FROM artifacts", ())
        ]
    finally:
        tools.engine.close()

    assert "ordinary.md" in held
    assert not [path for path in held if path.startswith("benchmark/")]
    # Counted and named, never silent: a control whose only evidence is the
    # absence of something cannot be told from a typo in the pattern.
    refused = result.details.get("refused") or []
    assert [entry["relative_path"] for entry in refused] == ["benchmark/answers.md"]
    assert refused[0]["pattern"] == "benchmark/*"
    assert result.details["refused_total"] == 1


def test_a_region_with_no_denylist_reports_no_refusals(tmp_path: Path) -> None:
    """The no-configuration path is byte-identical, which is rule 7.

    An empty denylist costs one truth test per item and adds no key to the
    sync report — the same "add the key only when it says something" rule
    `heading_path` and `memory_policy` follow.
    """

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "ordinary.md").write_text("# Ordinary\n\nContent.\n", encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "no-denylist",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "sources": [
                {
                    "name": "docs",
                    "type": "markdown_folder",
                    "path": str(workspace),
                    "include": ["**/*.md"],
                }
            ],
        }
    )
    tools = PheasantTools(config)
    try:
        result = tools.engine.sync_source("docs", "full")
    finally:
        tools.engine.close()
    assert result.indexed_artifacts == 1
    assert "refused" not in result.details
    assert "refused_total" not in result.details


def test_the_submission_and_indexing_doors_enforce_one_rule() -> None:
    """Both call `security.corpus_policy.denied_by`, so they cannot disagree.

    Two spellings of one boundary is the same defect as two implementations of
    one operation — and here it would be worse, because the halves would
    diverge silently and the gate would report whichever one it happened to
    exercise.
    """

    import inspect

    from pheasant.security import corpus_policy
    from pheasant.services.ingestion import check_denylist
    from pheasant.sync import engine as sync_engine

    assert "corpus_policy.denied_by" in inspect.getsource(check_denylist)
    assert "corpus_policy.denied_by" in inspect.getsource(sync_engine.SyncEngine._prepare_item)
    assert corpus_policy.denied_by("benchmark/x.md", ["benchmark/*"]) == "benchmark/*"
    assert corpus_policy.denied_by("src/x.md", ["benchmark/*"]) is None
