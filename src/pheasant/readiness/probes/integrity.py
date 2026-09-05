"""The boundaries: snapshots, isolation, refusals, concurrency.

The probes whose failures cannot be corrected for after the fact. A latency
regression is a number to improve; a snapshot that silently answered from a
different corpus, a principal who saw another's records, or a lost concurrent
write is an experiment that has to be re-run from collection.
"""

from __future__ import annotations

from typing import Any

from pheasant.readiness.probes import (
    PROBE_MARKER,
    PROBE_SOURCE,
    NotRunnable,
    ProbeContext,
    ProbeResult,
    probe,
)
from pheasant.readiness.probes.ingestion import index_probe_source, probe_documents
from pheasant.services import ingestion as ingest_service
from pheasant.services import retrieval as retrieval_service
from pheasant.services import snapshots as snapshot_service
from pheasant.services.errors import ServiceError


@probe("snapshot_seal")
def _snapshot_seal(context: ProbeContext) -> ProbeResult:
    """A seal is durable, addressable, and reports its own currency."""

    sealed = snapshot_service.seal(
        context.services,
        snapshot_service.SealRequest(label=f"readiness-{context.run_id}", sealed_by="readiness"),
    )
    fetched = snapshot_service.describe(context.services, sealed["snapshot_id"])
    verified = snapshot_service.verify(context.services, sealed["snapshot_id"])
    listed = snapshot_service.catalogue(context.services)
    known = any(
        row["snapshot_id"] == sealed["snapshot_id"] and row["sealed"] for row in listed["snapshots"]
    )
    passed = bool(sealed["sealed"]) and fetched["sealed"] and verified["current"] and known
    return ProbeResult(
        "snapshot_seal",
        passed,
        (
            f"sealed {sealed['snapshot_id']} and resolved it back"
            if passed
            else "a sealed snapshot could not be resolved or did not verify"
        ),
        observed={
            "snapshot_id": sealed["snapshot_id"],
            "complete": sealed["complete"],
            "incomplete": sealed["incomplete"],
            "listed": known,
        },
    )


@probe("snapshot_drift_refused")
def _snapshot_drift_refused(context: ProbeContext) -> ProbeResult:
    """Ingesting after a seal makes a pinned search refuse, not answer.

    The one probe that establishes the guarantee is a *refusal* rather than
    time travel. It has to ingest to do so, which is why it runs last and into
    the scratch source: the drift it causes is real, and a check that faked it
    would be checking its own fake.
    """

    sealed = snapshot_service.seal(
        context.services,
        snapshot_service.SealRequest(label=f"drift-{context.run_id}", sealed_by="readiness"),
    )
    snapshot_id = sealed["snapshot_id"]
    pinned = retrieval_service.search(
        context.services,
        retrieval_service.SearchRequest(
            query=PROBE_MARKER, mode="text", max_results=5, snapshot_id=snapshot_id
        ),
    )
    answered_before = pinned["lineage"]["state"]["snapshot_id"] == snapshot_id

    ingest_service.submit(
        context.services,
        ingest_service.SubmissionRequest(
            items=probe_documents(1, prefix=f"drift-{context.run_id}"),
            source_name=PROBE_SOURCE,
            agent_id="readiness",
        ),
    )
    index_probe_source(context)

    refused = False
    code = ""
    try:
        retrieval_service.search(
            context.services,
            retrieval_service.SearchRequest(
                query=PROBE_MARKER, mode="text", max_results=5, snapshot_id=snapshot_id
            ),
        )
    except ServiceError as exc:
        refused = True
        code = exc.code
    passed = answered_before and refused and code == "SNAPSHOT_DRIFTED"
    return ProbeResult(
        "snapshot_drift_refused",
        passed,
        (
            "a pinned search answered before the corpus moved and refused after"
            if passed
            else "a pinned search did not refuse after the corpus moved"
        ),
        observed={
            "snapshot_id": snapshot_id,
            "answered_before_drift": answered_before,
            "refused_after_drift": refused,
            "code": code,
        },
    )


@probe("principal_isolation")
def _principal_isolation(context: ProbeContext) -> ProbeResult:
    """Two principals, and nothing one wrote appears for the other.

    Reports ``skipped`` when ``security.acl_enforced`` is off, and that is the
    honest answer rather than a failure: a single-user region with enforcement
    off is correctly configured for what it is, and a green isolation gate over
    a region that enforces nothing is worse than no gate.
    """

    security = getattr(context.config, "security", None)
    if not getattr(security, "acl_enforced", False):
        raise NotRunnable(
            "security.acl_enforced is off, so this region does not partition by "
            "principal. Turn it on before running an isolated multi-arm experiment."
        )
    alice = retrieval_service.search(
        context.services,
        retrieval_service.SearchRequest(
            query=PROBE_MARKER, mode="text", max_results=25, principal="user:alice"
        ),
    )
    bob = retrieval_service.search(
        context.services,
        retrieval_service.SearchRequest(
            query=PROBE_MARKER, mode="text", max_results=25, principal="user:bob"
        ),
    )
    private = _private_hits(context, alice, "user:bob") + _private_hits(context, bob, "user:alice")
    return ProbeResult(
        "principal_isolation",
        not private,
        (
            "neither principal saw a record scoped to the other"
            if not private
            else f"{len(private)} cross-principal results"
        ),
        observed={
            "alice_results": len(alice.get("results") or []),
            "bob_results": len(bob.get("results") or []),
            "crossings": private,
        },
    )


def _private_hits(context: ProbeContext, payload: dict[str, Any], other: str) -> list[str]:
    """Results in this payload whose ACL allows only ``other``."""

    ids = [
        result.get("node_id") for result in payload.get("results") or [] if result.get("node_id")
    ]
    if not ids:
        return []
    acls = context.state.artifact_acls([str(node) for node in ids])
    crossings = []
    for artifact_id, raw in acls.items():
        if raw and other in str(raw) and '"public": true' not in str(raw):
            crossings.append(str(artifact_id))
    return crossings


def _private_hits(context: ProbeContext, payload: dict[str, Any], other: str) -> list[str]:
    """Results in this payload whose ACL allows only ``other``."""

    ids = [
        result.get("node_id") for result in payload.get("results") or [] if result.get("node_id")
    ]
    if not ids:
        return []
    acls = context.state.artifact_acls([str(node) for node in ids])
    crossings = []
    for artifact_id, raw in acls.items():
        if raw and other in str(raw) and '"public": true' not in str(raw):
            crossings.append(str(artifact_id))
    return crossings


@probe("structured_errors")
def _structured_errors(context: ProbeContext) -> ProbeResult:
    """A refusal carries a code and a retryability flag, not just prose."""

    seen: list[dict[str, Any]] = []
    try:
        retrieval_service.search(
            context.services,
            retrieval_service.SearchRequest(query="x", knowledge_base="no-such-region"),
        )
    except ServiceError as exc:
        seen.append(exc.as_dict())
    try:
        snapshot_service.describe(context.services, "kb-does-not-exist")
    except ServiceError as exc:
        seen.append(exc.as_dict())
    codes = {row["code"] for row in seen}
    passed = codes == {"UNKNOWN_KNOWLEDGE_BASE", "UNKNOWN_SNAPSHOT"} and all(
        row["retryable"] is False and row["error"] for row in seen
    )
    return ProbeResult(
        "structured_errors",
        passed,
        (
            "refusals carry stable codes and a retryability flag"
            if passed
            else f"unexpected refusal shape: {sorted(codes)}"
        ),
        observed={"refusals": seen},
    )


@probe("concurrent_writers")
def _concurrent_writers(context: ProbeContext) -> ProbeResult:
    """Parallel submitters lose nothing, duplicate nothing, cross-link nothing.

    Threads rather than processes because the failure being probed is a race
    inside the ledger's fold, and the trap this codebase already fell into is
    the mirror of it: an owner identifying a *process* could not arbitrate a
    race inside one, and every sequential test passed throughout. So the probe
    runs writers that share a process on purpose.
    """

    import threading

    settings = context.settings()
    writers = int(getattr(settings, "concurrency_probe_writers", 4) or 4)
    per_writer = int(getattr(settings, "concurrency_probe_items", 5) or 5)
    if writers < 2:
        raise NotRunnable("readiness.concurrency_probe_writers is below 2")

    submission_id = f"concurrent-{context.run_id}"
    failures: list[str] = []
    lock = threading.Lock()

    def work(worker: int) -> None:
        try:
            ingest_service.submit(
                context.services,
                ingest_service.SubmissionRequest(
                    items=probe_documents(per_writer, prefix=f"conc-{context.run_id}-w{worker}"),
                    source_name=PROBE_SOURCE,
                    submission_id=submission_id,
                    agent_id=f"readiness-w{worker}",
                ),
            )
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            with lock:
                failures.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=work, args=(index,)) for index in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    receipts = ingest_service.ingest_status(
        context.services, None, submission_id=submission_id, limit=10_000
    )["receipts"]
    expected = writers * per_writer
    artifacts = {receipt["artifact_id"] for receipt in receipts}
    passed = not failures and len(receipts) == expected and len(artifacts) == expected
    return ProbeResult(
        "concurrent_writers",
        passed,
        (
            f"{writers} concurrent writers produced {expected} distinct receipts"
            if passed
            else "concurrent submission lost, duplicated or cross-linked items"
        ),
        observed={
            "writers": writers,
            "expected": expected,
            "receipts": len(receipts),
            "distinct_artifacts": len(artifacts),
            "errors": failures,
        },
    )
