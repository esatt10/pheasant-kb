"""The write side: submission, receipt, barrier, reconciliation, refusal.

Six probes covering the specification's Core ingestion boxes. They share a
scratch source and are ordered so each leaves the next something to work with,
but none of them depends on another's *result* — a failed `ingest_roundtrip`
still lets `partial_failure` report, which is the point of running probes
rather than one long test.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pheasant.readiness.probes import (
    PROBE_MARKER,
    PROBE_SOURCE,
    NotRunnable,
    ProbeContext,
    ProbeResult,
    probe,
)
from pheasant.security import corpus_policy
from pheasant.services import ingestion as ingest_service
from pheasant.services import retrieval as retrieval_service
from pheasant.services.errors import ContaminationRefused
from pheasant.services.ingestion import check_denylist


def probe_documents(count: int, *, prefix: str, marker: str = PROBE_MARKER) -> list[Any]:
    return [
        ingest_service.SubmissionItem(
            relative_path=f"{prefix}-{index}.md",
            content=(
                f"# {prefix} {index}\n\n"
                f"The {marker} protocol governs case {prefix}-{index}. "
                "It is a readiness probe document and carries no knowledge.\n"
            ).encode(),
        )
        for index in range(count)
    ]


def index_probe_source(context: ProbeContext) -> None:
    if context.sync is None:
        raise NotRunnable("no sync callable was supplied, so nothing can cross the index barrier")
    context.sync(PROBE_SOURCE)


@probe("ingest_roundtrip")
def _ingest_roundtrip(context: ProbeContext) -> ProbeResult:
    """One document, from submission to receipt to a retrievable locator.

    The specification's central Core box, and the reason it is one box rather
    than three: a region can accept a write, can index *something*, and can
    return *a* result, and still fail to connect them — which is exactly the
    failure that makes an experiment's citations unverifiable.
    """

    submission = ingest_service.submit(
        context.services,
        ingest_service.SubmissionRequest(
            items=probe_documents(1, prefix=f"roundtrip-{context.run_id}"),
            source_name=PROBE_SOURCE,
            agent_id="readiness",
        ),
    )
    if not submission["accepted"]:
        return ProbeResult(
            "ingest_roundtrip",
            False,
            "the region accepted nothing",
            observed={"rejected": submission["rejected"]},
        )
    index_probe_source(context)
    acknowledged = ingest_service.acknowledge_indexed(
        context.services, None, submission["submission_id"]
    )
    receipt = ingest_service.ingest_status(
        context.services, None, submission_id=submission["submission_id"]
    )["receipts"][0]
    found = _find_marker(context, receipt["artifact_id"])
    passed = (
        receipt["disposition"] == "indexed"
        and acknowledged["acknowledged"] >= 1
        and found is not None
    )
    return ProbeResult(
        "ingest_roundtrip",
        passed,
        (
            "submitted → receipted → indexed → retrieved at an exact locator"
            if passed
            else "the chain broke; see observed"
        ),
        observed={
            "submission_id": submission["submission_id"],
            "disposition": receipt["disposition"],
            "artifact_id": receipt["artifact_id"],
            "chunk_count": receipt["chunk_count"],
            "locator": found,
        },
    )


def _find_marker(context: ProbeContext, artifact_id: str | None) -> dict[str, Any] | None:
    """Retrieve the probe's own document and return its exact locator."""

    payload = retrieval_service.search(
        context.services,
        retrieval_service.SearchRequest(
            query=PROBE_MARKER, mode="text", max_results=20, source_name=PROBE_SOURCE
        ),
    )
    for result in payload.get("results") or []:
        if artifact_id and result.get("node_id") != artifact_id:
            continue
        chunks = result.get("chunks") or [{}]
        return {
            "artifact_id": result.get("node_id"),
            "chunk_id": result.get("chunk_id") or chunks[0].get("chunk_id"),
            "relative_path": result.get("relative_path"),
            "start_line": chunks[0].get("start_line"),
            "end_line": chunks[0].get("end_line"),
        }
    return None


def _find_marker(context: ProbeContext, artifact_id: str | None) -> dict[str, Any] | None:
    """Retrieve the probe's own document and return its exact locator."""

    payload = retrieval_service.search(
        context.services,
        retrieval_service.SearchRequest(
            query=PROBE_MARKER, mode="text", max_results=20, source_name=PROBE_SOURCE
        ),
    )
    for result in payload.get("results") or []:
        if artifact_id and result.get("node_id") != artifact_id:
            continue
        chunks = result.get("chunks") or [{}]
        return {
            "artifact_id": result.get("node_id"),
            "chunk_id": result.get("chunk_id") or chunks[0].get("chunk_id"),
            "relative_path": result.get("relative_path"),
            "start_line": chunks[0].get("start_line"),
            "end_line": chunks[0].get("end_line"),
        }
    return None


@probe("idempotent_write")
def _idempotent_write(context: ProbeContext) -> ProbeResult:
    """The same write, three times, under one key — one stored object.

    Three rather than two because the specification says three, and because a
    fold that is correct once and wrong on accumulation is a real shape: the
    row's ``submissions`` counter has to reach 3 while the artifact count stays
    at 1, and a check that only submitted twice could not tell an incrementing
    counter from a boolean.
    """

    item = probe_documents(1, prefix=f"idem-{context.run_id}")[0]
    receipts = []
    for _ in range(3):
        submission = ingest_service.submit(
            context.services,
            ingest_service.SubmissionRequest(
                items=[item], source_name=PROBE_SOURCE, agent_id="readiness"
            ),
        )
        receipts.append(submission["accepted"][0])
    ids = {receipt["receipt_id"] for receipt in receipts}
    artifacts = {receipt["artifact_id"] for receipt in receipts}
    index_probe_source(context)
    ingest_service.acknowledge_indexed(context.services, None, None)
    stored = context.state.rows(
        "SELECT COUNT(*) AS c FROM artifacts WHERE id=?", (receipts[-1]["artifact_id"],)
    )
    held = int(stored[0]["c"]) if stored else 0
    final = ingest_service.ingest_status(
        context.services, None, idempotency_key=receipts[-1]["idempotency_key"]
    )["receipts"][0]
    passed = len(ids) == 1 and len(artifacts) == 1 and held == 1 and final["submissions"] == 3
    return ProbeResult(
        "idempotent_write",
        passed,
        (
            "three submissions under one key produced one receipt and one artifact"
            if passed
            else "a retry was not absorbed"
        ),
        observed={
            "distinct_receipts": len(ids),
            "distinct_artifacts": len(artifacts),
            "artifact_rows": held,
            "submissions_counted": final["submissions"],
        },
    )


@probe("partial_failure")
def _partial_failure(context: ProbeContext) -> ProbeResult:
    """One bad item in a batch rejects itself and nothing beside it.

    The shape this codebase has already been bitten by: a batch written in one
    transaction makes a single malformed row cost every good row next to it,
    and the fix was validating before the statement rather than rolling back
    after it. Same lesson, one layer up.
    """

    good = probe_documents(2, prefix=f"partial-{context.run_id}")
    empty = ingest_service.SubmissionItem(
        relative_path=f"partial-{context.run_id}-empty.md", content=b""
    )
    submission = ingest_service.submit(
        context.services,
        ingest_service.SubmissionRequest(
            items=[good[0], empty, good[1]], source_name=PROBE_SOURCE, agent_id="readiness"
        ),
    )
    accepted, rejected = submission["accepted"], submission["rejected"]
    passed = (
        len(accepted) == 2
        and len(rejected) == 1
        and bool(rejected[0]["error_code"])
        and rejected[0]["retryable"] is False
    )
    return ProbeResult(
        "partial_failure",
        passed,
        (
            "the bad item was rejected with a code; the good ones were kept"
            if passed
            else "partial failure did not report per item"
        ),
        observed={
            "accepted": len(accepted),
            "rejected": len(rejected),
            "error_code": rejected[0]["error_code"] if rejected else None,
            "retryable": rejected[0]["retryable"] if rejected else None,
        },
    )


@probe("index_barrier")
def _index_barrier(context: ProbeContext) -> ProbeResult:
    """Acceptance and searchability are distinguishable, and the lag is bounded.

    Two facts in one probe because they are the same fact seen twice: a region
    that cannot tell them apart cannot report a lag either, and a harness that
    queries on acceptance will record an empty result as a retrieval failure.
    """

    settings = context.settings()
    budget = float(getattr(settings, "max_index_lag_ms", 0.0) or 0.0)
    if budget <= 0:
        raise NotRunnable("readiness.max_index_lag_ms is not configured")
    submission = ingest_service.submit(
        context.services,
        ingest_service.SubmissionRequest(
            items=probe_documents(1, prefix=f"barrier-{context.run_id}"),
            source_name=PROBE_SOURCE,
            agent_id="readiness",
        ),
    )
    key = submission["accepted"][0]["idempotency_key"]

    def _receipt() -> dict[str, Any]:
        return ingest_service.ingest_status(context.services, None, idempotency_key=key)[
            "receipts"
        ][0]

    before = _receipt()
    started = time.perf_counter()
    index_probe_source(context)
    ingest_service.acknowledge_indexed(context.services, None, submission["submission_id"])
    lag_ms = (time.perf_counter() - started) * 1000.0
    after = _receipt()
    distinguishable = before["disposition"] == "accepted" and after["disposition"] == "indexed"
    passed = distinguishable and lag_ms <= budget
    return ProbeResult(
        "index_barrier",
        passed,
        (
            f"accepted → indexed in {lag_ms:.0f}ms, inside the {budget:.0f}ms budget"
            if passed
            else "the barrier is not observable, or the lag is over budget"
        ),
        observed={
            "before": before["disposition"],
            "after": after["disposition"],
            "index_lag_ms": round(lag_ms, 1),
            "budget_ms": budget,
        },
    )


@probe("reconciliation")
def _reconciliation(context: ProbeContext) -> ProbeResult:
    """Every submitted item resolves to a receipt, and nothing is unaccounted."""

    report = ingest_service.reconcile(context.services, None)
    passed = report["silent_loss"] == 0 and report["duplicated"] == 0
    return ProbeResult(
        "reconciliation",
        passed,
        (
            f"{report['submitted']} receipts reconcile; silent loss 0"
            if passed
            else f"{report['silent_loss']} unaccounted, {report['duplicated']} duplicated"
        ),
        observed=report,
    )


@probe("contamination_refused")
def _contamination_refused(context: ProbeContext) -> ProbeResult:
    """A denylisted path is refused at every door, and the corpus is clean.

    Three assertions, and the third is the one the specification's box actually
    asks for.

    **Refused at the submission door.** With its own error code, so a caller
    cannot mistake it for a size limit and retry smaller.

    **Refused at the indexing door.** The submission path is not the only way
    in — a folder source, the UI drop zone, a git repository and every
    connector reach the corpus without passing through it. The first version of
    this probe checked only the first door and reported the boundary intact on
    a region where the other four were open, which is exactly the overclaim the
    control exists to prevent. So this writes a denylisted file *into a real
    source's directory* and syncs, then asserts it did not land.

    **And the corpus is clean.** Enforcement stops new arrivals; it says
    nothing about content indexed before the denylist existed. Only a scan of
    what the region actually holds can, so the gate reads `artifacts` — which
    is what makes it a statement about this corpus rather than about this code.
    """

    patterns = tuple(getattr(context.settings(), "corpus_denylist", ()) or ())
    if not patterns:
        raise NotRunnable(
            "readiness.corpus_denylist is empty, so there is no boundary to prove. "
            "Set it to the paths your benchmark artifacts use."
        )
    probe_path = _first_matching_name(patterns)
    body = b"# benchmark answer key\n\nThis must never be indexed.\n"

    submission = ingest_service.submit(
        context.services,
        ingest_service.SubmissionRequest(
            items=[ingest_service.SubmissionItem(relative_path=probe_path, content=body)],
            source_name=PROBE_SOURCE,
            agent_id="readiness",
        ),
    )
    rejected = submission["rejected"]
    refused_at_submission = (
        bool(rejected)
        and rejected[0]["error_code"] == "CORPUS_DENYLISTED"
        and not submission["accepted"]
    )

    # The other door: bytes placed directly in the source directory, which is
    # what a folder source, a git checkout or the UI drop zone all amount to.
    landed = _plant_in_source(context, probe_path, body)
    if landed is None:
        raise NotRunnable(
            "the readiness scratch source has no directory yet; run the "
            "ingestion probes first so there is a real source to plant in"
        )
    index_probe_source(context)
    refused_at_index = not context.state.rows(
        "SELECT 1 FROM artifacts WHERE relative_path=? LIMIT 1", (probe_path,)
    )
    landed.unlink(missing_ok=True)

    held = _denylisted_artifacts(context, patterns)
    passed = refused_at_submission and refused_at_index and not held
    return ProbeResult(
        "contamination_refused",
        passed,
        (
            f"{probe_path} was refused at both doors, and no artifact in this "
            "corpus matches the denylist"
            if passed
            else "a denylisted path reached the corpus, or was not refused"
        ),
        observed={
            "pattern_count": len(patterns),
            "probe_path": probe_path,
            "refused_at_submission": refused_at_submission,
            "refused_at_index": refused_at_index,
            "error_code": rejected[0]["error_code"] if rejected else None,
            "denylisted_artifacts_held": held[:20],
            "denylisted_artifacts_total": len(held),
        },
    )


def _plant_in_source(context: ProbeContext, relative: str, body: bytes) -> Path | None:
    """Write bytes straight into the scratch source, bypassing submission.

    Deliberately not through `submit`: the point is to exercise the door that
    does *not* go through it. This is what a folder source watching a directory
    somebody dropped a file into looks like from the engine's side.
    """

    rows = context.state.rows("SELECT path FROM sources WHERE name=?", (PROBE_SOURCE,))
    if not rows:
        return None
    target = Path(str(rows[0]["path"])) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


def _denylisted_artifacts(context: ProbeContext, patterns: tuple[str, ...]) -> list[str]:
    """Every artifact this region holds whose path the denylist forbids.

    Matched in Python rather than in SQL because the patterns are fnmatch and
    the two do not agree — `LIKE` has no character classes and `GLOB` has no
    `**` — and a gate that quietly matched a *different* rule than the one the
    write path enforces would report a boundary nobody has.
    """

    held: list[str] = []
    for row in context.state.rows("SELECT relative_path FROM artifacts", ()):
        relative = str(row["relative_path"] or "")
        if relative and corpus_policy.denied_by(relative, patterns):
            held.append(relative)
    return held


def _first_matching_name(patterns: tuple[str, ...]) -> str:
    """A path the configured denylist will actually reject.

    Derived from the operator's own patterns rather than hardcoded: a probe
    that submits ``benchmark/answers.md`` against a denylist of ``eval-*.json``
    would be refused by nothing and report a boundary that does not exist —
    a *false failure*, which is the worst thing a gate can produce, because
    the region is behaving correctly and the check says it is not.

    So a derived candidate is **verified against the same predicate the write
    path uses** rather than assumed to match. Deriving a filename from a glob
    is a guess: `**/*.json` un-globs to `readiness.json`, which `fnmatch` then
    does *not* match, because `**` is not a path-aware operator there. Rather
    than encode a second, subtly different glob semantics here, each candidate
    is tried against `_check_denylist` and the first one that is genuinely
    refused wins.

    When no candidate matches — an exotic pattern, or one that only ever
    matches a path this probe may not write — the probe reports ``skipped``
    with the patterns it tried, which is honest. It is not evidence that the
    boundary is broken; it is evidence that this check could not exercise it.
    """

    for pattern in patterns:
        for candidate in _candidates(pattern):
            try:
                check_denylist(candidate, patterns)
            except ContaminationRefused:
                return candidate
    raise NotRunnable(
        "could not derive a path that readiness.corpus_denylist rejects, from "
        f"{list(patterns)}. The boundary may well hold; this probe cannot show "
        "it. Add a literal-ish pattern (e.g. 'benchmark/*') to make it checkable."
    )


def _candidates(pattern: str) -> list[str]:
    """Concrete paths a glob might have been written to describe."""

    stripped = pattern.strip("/")
    plain = stripped.replace("?", "x")
    filled = plain.replace("**/", "").replace("*", "readiness")
    leaf = filled.rsplit("/", 1)[-1]
    return [value for value in (filled, f"readiness/{leaf}", leaf) if value]
