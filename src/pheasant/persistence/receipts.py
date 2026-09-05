"""What happened to what a caller submitted.

Free functions over a state store rather than methods on it, following
`evaluation/store.py`: this is one bounded context with four operations and no
state of its own, and `state_store.py` is a file every feature touches — the
module ratchet in `tests/test_module_budget.py` is explicit that the fix for
that file is to split it by context rather than to keep adding to it.

The ledger's own argument is in `services/ingestion.py`. In one line: the
`artifacts` table cannot distinguish "never submitted" from "submitted and
lost", and a harness reconciling its own submissions needs those to be
different answers.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def receipt_id(kb_id: str, idempotency_key: str) -> str:
    """The receipt's identity, derived rather than minted.

    Content-addressed for the reason every other id in this schema is: two
    replicas handed the same retry must write the same row without talking to
    each other, which is what makes the ``ON CONFLICT`` fold below an
    idempotent write instead of a race.
    """

    digest = hashlib.blake2b(digest_size=16)
    digest.update(kb_id.encode("utf-8"))
    digest.update(b"\x1f")
    digest.update(idempotency_key.encode("utf-8"))
    return digest.hexdigest()


def record(
    state: Any,
    *,
    kb_id: str,
    idempotency_key: str,
    submission_id: str,
    source_name: str | None,
    now: str,
    disposition: str,
    artifact_id: str | None = None,
    content_sha256: str | None = None,
    chunk_count: int | None = None,
    indexed_at: str | None = None,
    error_code: str | None = None,
    retryable: bool = False,
    detail: dict[str, Any] | None = None,
    counts_as_submission: bool = True,
) -> dict[str, Any]:
    """Write (or fold onto) one receipt, and return it.

    The fold is the idempotency contract. A second submission carrying a key
    this region has already seen updates the row it wrote — it never inserts a
    second one — and bumps ``submissions``, so the *evidence* of the retry
    survives while the stored object stays single. That separation is the
    point: a caller needs to know its retry was a retry, and a reconciliation
    needs the count of logical objects to be one.

    ``disposition`` only ever moves forward. A receipt that reached ``indexed``
    is not walked back to ``accepted`` by a retry that stopped at the
    transport, because a barrier a caller waits on must not un-cross itself; a
    retry that *fails* does record the failure, since that is new information
    rather than a stale earlier state.

    ``counts_as_submission`` is what keeps ``submissions`` meaning what its
    name says. Crossing the index barrier writes this row too, and the first
    version counted that — so three caller submissions plus one acknowledgement
    reported four, and a harness proving its retry was absorbed would have read
    a number that moves when the *region* acts. It is the shape
    `pheasant_memory_reinforcement_ratio` was already caught by: a counter
    derived from every write measures writes, not the thing the name promises.
    """

    key = receipt_id(kb_id, idempotency_key)
    state.execute(
        """INSERT INTO ingest_receipts(
            receipt_id,kb_id,idempotency_key,submission_id,source_name,
            submitted_at,updated_at,disposition,indexed_at,artifact_id,
            content_sha256,chunk_count,submissions,error_code,retryable,detail_json
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(receipt_id) DO UPDATE SET
            updated_at=excluded.updated_at,
            submission_id=excluded.submission_id,
            source_name=COALESCE(excluded.source_name, ingest_receipts.source_name),
            disposition=CASE
                WHEN excluded.disposition IN ('rejected','failed') THEN excluded.disposition
                WHEN ingest_receipts.disposition = 'indexed' THEN 'indexed'
                ELSE excluded.disposition
            END,
            indexed_at=COALESCE(excluded.indexed_at, ingest_receipts.indexed_at),
            artifact_id=COALESCE(excluded.artifact_id, ingest_receipts.artifact_id),
            content_sha256=COALESCE(excluded.content_sha256, ingest_receipts.content_sha256),
            chunk_count=COALESCE(excluded.chunk_count, ingest_receipts.chunk_count),
            submissions=ingest_receipts.submissions + ?,
            error_code=excluded.error_code,
            retryable=excluded.retryable,
            detail_json=excluded.detail_json""",
        (
            key,
            kb_id,
            idempotency_key,
            submission_id,
            source_name,
            now,
            now,
            disposition,
            indexed_at,
            artifact_id,
            content_sha256,
            chunk_count,
            1 if counts_as_submission else 0,
            error_code,
            int(bool(retryable)),
            json.dumps(detail or {}, default=str, sort_keys=True),
            1 if counts_as_submission else 0,
        ),
    )
    stored = get(state, kb_id, idempotency_key)
    if stored is None:  # pragma: no cover - a ledger that cannot read itself back
        raise RuntimeError(f"receipt {key} was written and could not be read back")
    return stored


def get(state: Any, kb_id: str, idempotency_key: str) -> dict[str, Any] | None:
    rows = state.rows(
        "SELECT * FROM ingest_receipts WHERE receipt_id=?",
        (receipt_id(kb_id, idempotency_key),),
    )
    return _row(rows[0]) if rows else None


def listing(
    state: Any,
    kb_id: str,
    *,
    submission_id: str | None = None,
    disposition: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    where = ["kb_id=?"]
    params: list[Any] = [kb_id]
    if submission_id:
        where.append("submission_id=?")
        params.append(submission_id)
    if disposition:
        where.append("disposition=?")
        params.append(disposition)
    params.append(int(limit))
    rows = state.rows(
        f"""SELECT * FROM ingest_receipts
            WHERE {" AND ".join(where)}
            ORDER BY submitted_at DESC, receipt_id
            LIMIT ?""",
        tuple(params),
    )
    return [_row(row) for row in rows]


def reconcile(state: Any, kb_id: str, *, submission_id: str | None = None) -> dict[str, Any]:
    """Submissions against what the region actually holds.

    ``unaccounted`` is the number this exists to produce: receipts that say
    ``indexed`` and name an artifact the ``artifacts`` table does not have.
    That is silent loss, and it is the one failure a count of stored rows
    cannot reveal on its own — a corpus missing a document looks exactly like a
    corpus that was never sent it.

    The join is deliberately a LEFT JOIN from receipts rather than a count on
    either side: two aggregates can agree by coincidence when one item was lost
    and another double-written.
    """

    where = ["r.kb_id=?"]
    params: list[Any] = [kb_id]
    if submission_id:
        where.append("r.submission_id=?")
        params.append(submission_id)
    clause = " AND ".join(where)
    grouped = state.rows(
        f"""SELECT r.disposition AS disposition,
                   COUNT(*) AS total,
                   SUM(CASE WHEN r.submissions > 1 THEN 1 ELSE 0 END) AS resubmitted
            FROM ingest_receipts r
            WHERE {clause}
            GROUP BY r.disposition""",
        tuple(params),
    )
    by_disposition = {str(row["disposition"]): int(row["total"]) for row in grouped}
    resubmitted = sum(int(row["resubmitted"] or 0) for row in grouped)
    missing = state.rows(
        f"""SELECT r.receipt_id AS receipt_id, r.artifact_id AS artifact_id
            FROM ingest_receipts r
            LEFT JOIN artifacts a ON a.id = r.artifact_id
            WHERE {clause}
              AND r.disposition = 'indexed'
              AND r.artifact_id IS NOT NULL
              AND a.id IS NULL
            ORDER BY r.receipt_id""",
        tuple(params),
    )
    duplicated = state.rows(
        f"""SELECT r.artifact_id AS artifact_id, COUNT(*) AS receipts
            FROM ingest_receipts r
            WHERE {clause} AND r.artifact_id IS NOT NULL
            GROUP BY r.artifact_id
            HAVING COUNT(*) > 1
            ORDER BY r.artifact_id""",
        tuple(params),
    )
    return {
        "kb_id": kb_id,
        "submission_id": submission_id,
        "submitted": sum(by_disposition.values()),
        "by_disposition": by_disposition,
        "indexed": by_disposition.get("indexed", 0),
        "rejected": by_disposition.get("rejected", 0),
        "failed": by_disposition.get("failed", 0),
        "accepted_not_indexed": by_disposition.get("accepted", 0),
        "resubmitted": resubmitted,
        "unaccounted": len(missing),
        "unaccounted_receipts": [str(row["receipt_id"]) for row in missing],
        "duplicated_artifacts": [str(row["artifact_id"]) for row in duplicated],
    }


def _row(row: Any) -> dict[str, Any]:
    receipt = dict(row)
    receipt["retryable"] = bool(receipt.get("retryable"))
    receipt["detail"] = json.loads(receipt.pop("detail_json") or "{}")
    return receipt
