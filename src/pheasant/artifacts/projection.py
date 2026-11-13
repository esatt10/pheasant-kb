"""Project memory record files into queryable SQLite rows (Step 33.5).

Memory records are source content — one append-only Markdown file each — and
that stays true. What was missing is that their *metadata* was reachable only
as prose inside the indexed chunk text: `scope`, `subject`, `asserted_at` and
`supersedes` were BM25 terms, not columns. Nothing could ask for "user-scope
memories about deployment", and nothing could drop a corrected record at query
time — supersession was enforced only by the batch consolidation pass, so a
memory that had already been corrected stayed retrievable until a full re-sync
ran.

This module builds the projection. It is deliberately a *derivation*, never a
second source of truth: every field comes from the files, so
`replace_memory_records` can rebuild a source wholesale and losing the table
costs a re-sync rather than data.

Rule 1 holds by construction — this is frontmatter parsing and interval
arithmetic, no model and no network.
"""

from __future__ import annotations

import logging
from typing import Any

from pheasant.artifacts.normalize import acl_class_for, normalized_digest
from pheasant.artifacts.store import MemoryRecord


def artifact_id_for(source_name: str, record: MemoryRecord) -> str:
    """The id the ingestion pipeline mints for this record's file.

    `file:{source}:{relpath}:branch={b}` is a stable-ID contract (rule 3), so
    this reproduces the grammar rather than inventing a parallel one — the
    projection has to join to `artifacts` on exactly the id that indexing
    wrote. A record lives at `<scope>/<record_id>.md` beneath the source root,
    and a memory source is never git-backed, so the branch is always "none".
    """
    return f"file:{source_name}:{record.scope}/{record.record_id}.md:branch=none"


def effective_valid_until(
    record: MemoryRecord,
    superseded_at: dict[str, str],
) -> str | None:
    """When this record stopped being current, or None if it still is.

    Two independent ways a record can end, and the **earlier one wins**:

    * it was corrected — some later record names it in `supersedes`, so its
      validity ends the moment that correction was asserted;
    * it said so itself — an explicit `valid_until` in the frontmatter.

    Taking the minimum rather than preferring one source is what makes the
    predicate honest in the awkward case: a record that declared it was good
    until December but got corrected in March is stale from March, not
    December.
    """
    ends = [value for value in (record.valid_until, superseded_at.get(record.record_id)) if value]
    return min(ends) if ends else None


def supersession_index(records: list[MemoryRecord]) -> dict[str, str]:
    """`{superseded_record_id: when the correction was asserted}`.

    When two records correct the same target — an ordinary outcome for a fact
    that was revised twice — the *earliest* correction is the one that ended
    the original's validity, so that is the timestamp kept.
    """
    out: dict[str, str] = {}
    for record in records:
        target = record.supersedes
        if not target or not record.asserted_at:
            continue
        previous = out.get(target)
        if previous is None or record.asserted_at < previous:
            out[target] = record.asserted_at
    return out


def project(source_name: str, records: list[MemoryRecord]) -> list[dict[str, object]]:
    """Rows for `StateStore.replace_memory_records`, deterministic in `records`.

    Sorted by record id so a rebuild writes rows in a stable order and two
    runs over the same files produce byte-identical state.
    """
    superseded_at = supersession_index(records)
    rows = [
        {
            "record_id": record.record_id,
            "artifact_id": artifact_id_for(source_name, record),
            "source_id": source_name,
            "scope": record.scope,
            "subject": record.subject,
            "kind": record.kind,
            "asserted_at": record.asserted_at,
            "valid_from": record.valid_from or record.asserted_at,
            "valid_until": effective_valid_until(record, superseded_at),
            "supersedes": record.supersedes,
            "tags": ", ".join(record.tags) if record.tags else None,
            "written_by": record.written_by,
            # Phase 1 — a pure function of this record's own fields (scope,
            # subject, kind, ACL partition, normalized text), so it is
            # recomputed on every rebuild rather than carried over, unlike
            # observations/last_seen/variants just below it in the row
            # (those are earned by write-path reinforcement; see
            # `StateStore.replace_memory_records`).
            "canon_key": normalized_digest(
                scope=record.scope,
                subject=record.subject,
                kind=record.kind,
                acl_class=acl_class_for(record.scope, record.written_by),
                text=record.text,
            ),
            "schema_version": record.schema_version,
        }
        for record in records
    ]
    rows.sort(key=lambda row: str(row["record_id"]))
    return rows


def refresh(state: Any, kb_id: str, source: Any) -> None:
    if str(source.type) not in {"memory", "artifact"}:
        return
    from pheasant.artifacts.store import ArtifactRecordStore
    from pheasant.artifacts.versioning import capture_records

    try:
        records = ArtifactRecordStore(source.path).list_records()
        state.replace_memory_records(source.name, project(source.name, records))
        capture_records(state, kb_id, source.name, records)
    except Exception:
        logging.getLogger(__name__).warning(
            "artifact projection failed for %s; keeping the previous rows",
            source.name,
            exc_info=True,
        )
