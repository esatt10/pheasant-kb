"""Submission, receipt, and reconciliation — the write side's contract.

pheasant's ingestion has always been idempotent: an artifact is keyed by a
stable id, its bytes by sha256, and a re-sync of unchanged content does no
work. What it has never had is a way for the *caller* to prove any of that.

The difference matters as soon as something other than a person is submitting.
An agent that uploads a document and gets a 200 knows the bytes reached the
process. It does not know whether they were persisted, whether parsing
rejected them, whether they are indexed yet, or — the case that actually
breaks an experiment — whether the retry it issued after a timeout produced a
second copy. `artifacts` cannot answer any of those, because a row's absence
is the same answer for "never submitted", "rejected" and "lost".

So a submission is receipted. One row per item, keyed by the caller's
idempotency key, carrying the disposition it reached and the artifact it
became. Three properties fall out, and each is a gate in
``docs/stress-test-readiness.md``:

* **A retry is visible and singular.** The same key folds onto the same
  receipt and bumps ``submissions``; the stored object stays one. A caller can
  therefore tell "my retry was absorbed" from "my retry wrote a second copy",
  which is the difference between a corpus of 400 documents and a corpus of
  400 documents that scores as 520.
* **Acceptance and indexing are different facts.** ``accepted`` means the bytes
  are persisted; ``indexed`` means a search can return them. Collapsing them is
  how a harness starts querying for content the region holds and has not yet
  indexed, then records the empty result as a retrieval failure.
* **Partial failure is per item.** One oversized file in a batch of forty
  rejects itself and nothing else, and says which one and why.

**There is still no second ingestion path.** Bytes land in a directory under
``/state/uploads`` registered as an ordinary ``document_folder`` source, exactly
as `ingestion/landing.py` has always done, and everything downstream is the normal
connector → parse → chunk → graph pipeline. This module owns the ledger and
the idempotent placement, not a parallel pipeline — a second one would have to
re-earn every property the first one has, starting with rule 1.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pheasant.persistence import receipts as receipt_ledger
from pheasant.security import corpus_policy
from pheasant.services import ServiceContext
from pheasant.services.errors import ContaminationRefused, InvalidRequest

#: Dispositions, in the order a receipt may travel through them. `accepted` and
#: `indexed` are a barrier, not a status gradient: `rejected` and `failed` are
#: terminal, and the difference between them is whose problem it is — a
#: rejection is the submission's (it will be rejected identically forever), a
#: failure is the region's (it may not be).
DISPOSITIONS = ("accepted", "indexed", "rejected", "failed")


def _now() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class SubmissionItem:
    """One document offered to the region."""

    #: Path relative to the submission's source. Sanitised before use.
    relative_path: str
    content: bytes
    #: The caller's retry identity. Defaults to a digest over
    #: ``(relative_path, content)`` — content-addressed, so a caller that does
    #: not supply one still gets idempotency rather than a fresh row per
    #: attempt. That default is the same argument `index_tasks` makes.
    idempotency_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        if self.idempotency_key:
            return self.idempotency_key
        digest = hashlib.sha256()
        digest.update(self.relative_path.encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(self.content)
        return f"sha256:{digest.hexdigest()}"

    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class SubmissionRequest:
    """A batch of items destined for one source."""

    items: list[SubmissionItem]
    source_name: str = "submissions"
    knowledge_base: str | None = None
    #: Groups the batch's receipts. Minted when absent, because a caller that
    #: cannot name its batch cannot reconcile it either.
    submission_id: str | None = None
    #: Who submitted. Recorded on every receipt so a swarm can attribute a
    #: document to the worker that found it.
    agent_id: str | None = None


def submit(context: ServiceContext, request: SubmissionRequest) -> dict[str, Any]:
    """Persist a batch, receipt every item, and report per-item disposition.

    Nothing here indexes. Acceptance is a *separate* fact from searchability
    and this call only establishes the first: the caller then syncs the source
    (or lets the scheduler do it) and crosses the barrier with
    :func:`acknowledge_indexed`. Doing both here would make the two
    indistinguishable in exactly the payload built to distinguish them.
    """

    if not request.items:
        raise InvalidRequest("A submission needs at least one item")
    kb_id = context.knowledge_base(request.knowledge_base)
    submission_id = request.submission_id or f"sub-{uuid.uuid4().hex[:16]}"
    source_name = _safe_source_name(request.source_name)
    directory = _submission_root(context, source_name)
    denylist = _denylist(context.config)
    limits = getattr(getattr(context.config, "sync", None), "limits", None)
    max_bytes = (getattr(limits, "max_file_size_mb", 0) or 0) * 1024 * 1024 or None

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in request.items:
        key = item.key()
        existing = receipt_ledger.get(context.state, kb_id, key)
        try:
            relative = _placement(item.relative_path, existing)
            check_denylist(relative, denylist)
            _check_size(relative, item.content, max_bytes)
        except InvalidRequest as exc:
            receipt = receipt_ledger.record(
                context.state,
                kb_id=kb_id,
                idempotency_key=key,
                submission_id=submission_id,
                source_name=source_name,
                now=_now(),
                disposition="rejected",
                content_sha256=item.sha256(),
                error_code=exc.code,
                retryable=exc.retryable,
                detail={"reason": str(exc), "agent_id": request.agent_id, **item.metadata},
            )
            rejected.append(receipt)
            continue
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Written unconditionally, and to the *same* path on a retry. Writing
        # identical bytes over identical bytes is what makes the retry free
        # downstream: the connector's sha256 comparison then skips the file
        # before reading it, which is pillar 2 doing the deduplication rather
        # than this module inventing its own.
        target.write_bytes(item.content)
        receipt = receipt_ledger.record(
            context.state,
            kb_id=kb_id,
            idempotency_key=key,
            submission_id=submission_id,
            source_name=source_name,
            now=_now(),
            disposition="accepted",
            artifact_id=_artifact_id(source_name, relative),
            content_sha256=item.sha256(),
            detail={
                "relative_path": relative,
                "path": str(target),
                "agent_id": request.agent_id,
                **item.metadata,
            },
        )
        accepted.append(receipt)

    return {
        "knowledge_base": kb_id,
        "submission_id": submission_id,
        "source_name": source_name,
        "directory": str(directory),
        "submitted": len(request.items),
        "accepted": [_public(receipt) for receipt in accepted],
        "rejected": [_public(receipt) for receipt in rejected],
        # The barrier, stated rather than implied: nothing in this payload
        # means "searchable", and a caller that treats acceptance as
        # searchability will query for content that is not there yet.
        "indexed": 0,
        "next": "sync the source, then call ingest_acknowledge to cross the index barrier",
    }


def acknowledge_indexed(
    context: ServiceContext,
    knowledge_base: str | None,
    submission_id: str | None = None,
) -> dict[str, Any]:
    """Cross the index barrier for receipts whose artifacts now exist.

    Read from `artifacts` rather than from whatever the sync reported, on
    purpose: a sync's own summary is a claim about what it did, and the thing a
    caller needs is whether the region *holds* the artifact. Those agree almost
    always, and the case where they do not is precisely the one worth catching.
    """

    kb_id = context.knowledge_base(knowledge_base)
    pending = receipt_ledger.listing(
        context.state, kb_id, submission_id=submission_id, disposition="accepted", limit=10_000
    )
    crossed = 0
    for receipt in pending:
        artifact_id = receipt.get("artifact_id")
        if not artifact_id:
            continue
        rows = context.state.rows(
            "SELECT a.id AS id, a.sha256 AS sha256,"
            " (SELECT COUNT(*) FROM chunks c WHERE c.artifact_id = a.id) AS chunks"
            " FROM artifacts a WHERE a.id=?",
            (artifact_id,),
        )
        if not rows:
            continue
        receipt_ledger.record(
            context.state,
            kb_id=kb_id,
            idempotency_key=str(receipt["idempotency_key"]),
            submission_id=str(receipt["submission_id"]),
            source_name=receipt.get("source_name"),
            now=_now(),
            disposition="indexed",
            indexed_at=_now(),
            artifact_id=artifact_id,
            chunk_count=int(rows[0]["chunks"] or 0),
            detail=dict(receipt.get("detail") or {}),
            # The region crossing a barrier is not the caller submitting
            # again. Counting it here made three submissions plus one
            # acknowledgement report four, which is a counter that moves when
            # nobody submitted anything.
            counts_as_submission=False,
        )
        crossed += 1
    return {
        "knowledge_base": kb_id,
        "submission_id": submission_id,
        "acknowledged": crossed,
        "still_accepted": len(pending) - crossed,
    }


def ingest_status(
    context: ServiceContext,
    knowledge_base: str | None,
    *,
    idempotency_key: str | None = None,
    submission_id: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """One receipt, or a submission's worth of them."""

    kb_id = context.knowledge_base(knowledge_base)
    if idempotency_key:
        receipt = receipt_ledger.get(context.state, kb_id, idempotency_key)
        return {
            "knowledge_base": kb_id,
            "receipts": [_public(receipt)] if receipt else [],
            "found": receipt is not None,
        }
    receipts = receipt_ledger.listing(
        context.state, kb_id, submission_id=submission_id, limit=limit
    )
    return {
        "knowledge_base": kb_id,
        "submission_id": submission_id,
        "receipts": [_public(receipt) for receipt in receipts],
        "found": bool(receipts),
    }


def reconcile(
    context: ServiceContext,
    knowledge_base: str | None,
    submission_id: str | None = None,
) -> dict[str, Any]:
    """Submitted against held, with the silent-loss count named.

    ``silent_loss`` is the number the readiness gate reads. It is not a
    difference between two totals — two totals can agree while one item was
    lost and another double-written — but the count of receipts that claim an
    artifact the region does not have.
    """

    kb_id = context.knowledge_base(knowledge_base)
    report = receipt_ledger.reconcile(context.state, kb_id, submission_id=submission_id)
    report["silent_loss"] = report["unaccounted"]
    report["duplicated"] = len(report["duplicated_artifacts"])
    report["reconciled"] = report["silent_loss"] == 0 and report["duplicated"] == 0
    return report


# ---------------------------------------------------------------------------
# Placement, naming and refusal.
# ---------------------------------------------------------------------------


def _artifact_id(source_name: str, relative: str) -> str:
    """The id the pipeline will mint for this file, predicted here.

    Duplicated grammar is a hazard (rule 3), so this is asserted against the
    pipeline's own construction in `tests/test_ingest_receipts.py` rather than
    trusted. Predicting it is what lets a receipt name its artifact *before*
    the sync runs, which is what makes the acknowledgement a lookup instead of
    a search.
    """

    return f"file:{source_name}:{relative}:branch=none"


def _safe_source_name(raw: str) -> str:
    from pheasant.ingestion.landing import safe_filename

    return safe_filename(raw or "submissions", fallback="submissions")


def _submission_root(context: ServiceContext, source_name: str) -> Path:
    from pheasant.ingestion.landing import upload_root

    return upload_root(Path(context.config.pheasant.state_path), source_name)


def _placement(raw: str, existing: dict[str, Any] | None) -> str:
    """Where this item's bytes go — the same place every time.

    A retry must land on the path its first attempt used, which is why the
    receipt is consulted rather than a fresh name allocated. The drop-zone
    route deliberately does the opposite (`unique_path` appends a suffix) because a
    person dropping two files called `notes.md` means two files; a retry
    carrying one idempotency key means one.
    """

    from pheasant.ingestion.landing import safe_filename

    if existing:
        recorded = str((existing.get("detail") or {}).get("relative_path") or "")
        if recorded:
            return recorded
    raw_parts = str(raw or "").replace("\\", "/").split("/")
    parts = [part for part in raw_parts if part not in ("", ".", "..")]
    if not parts:
        raise InvalidRequest("An item needs a relative_path")
    cleaned = [safe_filename(part, fallback="item") for part in parts]
    return "/".join(cleaned)


def _check_size(relative: str, content: bytes, max_bytes: int | None) -> None:
    if not content:
        raise InvalidRequest(f"{relative} is empty")
    if max_bytes is not None and len(content) > max_bytes:
        raise InvalidRequest(
            f"{relative} is {len(content) // (1024 * 1024)} MB, over the "
            f"{max_bytes // (1024 * 1024)} MB per-item limit (sync.limits.max_file_size_mb)"
        )


def _denylist(config: Any) -> tuple[str, ...]:
    return corpus_policy.denylist_of(config)


def check_denylist(relative: str, patterns: tuple[str, ...]) -> None:
    """Refuse a write the region has declared may never enter the corpus.

    The rule itself is `security.corpus_policy.denied_by`, at layer 1, because
    the *sync* path enforces it too and `sync/` cannot import `services/`. This
    is the submission half: it raises, because a caller submitting one item is
    making a request that can be refused. The indexing half skips and counts,
    because a connector listing a thousand files is not.

    Enforcing it in both places is the correction that matters. The first
    version lived here alone, so a region with a denylist configured still
    indexed a benchmark file arriving through a folder source, the UI drop
    zone, a git repository or any connector — while the readiness gate reported
    the boundary intact. A control on one of several doors is a door with a
    sign on it.

    Empty by default, and an empty list costs one truth test per item — the
    no-configuration path is unchanged, which is rule 7.
    """

    pattern = corpus_policy.denied_by(relative, patterns)
    if pattern:
        raise ContaminationRefused(relative, pattern)


def _public(receipt: dict[str, Any] | None) -> dict[str, Any]:
    """The receipt as a caller reads it.

    ``receipt_id`` and the dispositions travel; the raw row's column names do
    not, because a stored schema and a wire format that are the same object
    cannot be evolved independently.
    """

    if not receipt:
        return {}
    return {
        "receipt_id": receipt["receipt_id"],
        "idempotency_key": receipt["idempotency_key"],
        "submission_id": receipt["submission_id"],
        "source_name": receipt.get("source_name"),
        "disposition": receipt["disposition"],
        "submitted_at": receipt["submitted_at"],
        "updated_at": receipt["updated_at"],
        "indexed_at": receipt.get("indexed_at"),
        "artifact_id": receipt.get("artifact_id"),
        "content_sha256": receipt.get("content_sha256"),
        "chunk_count": receipt.get("chunk_count"),
        "submissions": receipt.get("submissions", 1),
        "error_code": receipt.get("error_code"),
        "retryable": bool(receipt.get("retryable")),
        "detail": receipt.get("detail") or {},
    }
