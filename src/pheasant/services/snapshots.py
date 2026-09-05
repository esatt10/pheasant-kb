"""Sealing a knowledge state, and refusing to answer as if it had not moved.

The evaluation plane already computes the hard part. `SnapshotManifest` is a
set of content digests over every input capable of changing what retrieval
returns — corpus, graph, lexical and vector index, chunking, fusion, memory,
steering, ACL — computed identically on any replica with no clock in the id.
What it has never been is *public*: an outside harness could not ask this
region to name the state it is about to be measured against, and could not
find out afterwards whether that state had moved underneath it.

Sealing is that missing fact. It says: somebody has declared this manifest the
reference state for a run. It is stored separately from the manifest because
the two have different lifetimes — an evaluation batch seals nothing, and a
sealed snapshot outlives every run that used it.

**What a seal does and does not promise.** This region holds one version of
its corpus. It cannot serve a sealed snapshot's results after the corpus behind
it has changed, and pretending otherwise would take multi-version storage that
nothing here has. So the guarantee is stated as a refusal rather than as time
travel:

    a search pinned to a snapshot is answered from that snapshot's state, or
    it is not answered.

That is weaker than "new ingestion cannot change a sealed snapshot's results"
and it is *sufficient for the property a scored experiment needs*, which is
that two runs claiming the same snapshot cannot silently have seen different
corpora. The distinction is written down here rather than smoothed over,
because a reader who assumes time travel will design an experiment that
ingests during a run and reports the results as reproducible.

`as_of` has the same shape and the same honesty. Memory records carry validity
and a correction supersedes rather than overwrites, so memory genuinely
replays at an earlier instant; corpus *content* does not, and a pinned
snapshot is what stands in for it. :func:`verify` reports which of the two a
caller is getting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pheasant.evaluation.snapshots import build_snapshot
from pheasant.evaluation.store import list_snapshots, load_snapshot, save_snapshot
from pheasant.services import ServiceContext
from pheasant.services.errors import SnapshotDrifted, SnapshotNotFound

#: Manifest sections a pinned search compares. Deliberately not every section:
#: `evaluation` holds the digests of the evaluation plane's own cohorts and
#: proofs, which change when somebody records a judgement and have no effect on
#: what retrieval returns. Including it would make every proof submission look
#: like corpus drift and refuse searches that are entirely reproducible.
PINNED_SECTIONS = ("corpus", "graph", "retrieval", "memory", "security")


def _now() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class SealRequest:
    knowledge_base: str | None = None
    label: str | None = None
    sealed_by: str | None = None
    note: str | None = None
    effective_as_of: str | None = None


def seal(context: ServiceContext, request: SealRequest) -> dict[str, Any]:
    """Compute the current manifest, store it, and record the seal.

    Sealing twice over an unchanged region is a no-op that returns the same
    snapshot id, because the id is a digest of the state and the seal row's
    primary key is that id. That is the same idempotency `save_snapshot` has
    always had, and it is what lets a harness seal defensively at the start of
    every arm without accumulating a snapshot per attempt.
    """

    kb_id = context.knowledge_base(request.knowledge_base)
    manifest = build_snapshot(
        context.state,
        context.config,
        graph=context.graph,
        effective_as_of=request.effective_as_of,
    )
    save_snapshot(context.state, manifest)
    now = _now()
    context.state.execute(
        """INSERT INTO snapshot_seals(
            snapshot_id,kb_id,label,sealed_at,sealed_by,corpus_digest,manifest_digest,note
        )
        VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT (snapshot_id) DO NOTHING""",
        (
            manifest.snapshot_id,
            kb_id,
            request.label,
            now,
            request.sealed_by,
            _section_digest(manifest, "corpus"),
            _manifest_digest(manifest),
            request.note,
        ),
    )
    return describe(context, manifest.snapshot_id)


def describe(context: ServiceContext, snapshot_id: str) -> dict[str, Any]:
    """One sealed snapshot, with its manifest and its live drift status."""

    manifest = load_snapshot(context.state, snapshot_id)
    if manifest is None:
        raise SnapshotNotFound(snapshot_id)
    rows = context.state.rows("SELECT * FROM snapshot_seals WHERE snapshot_id=?", (snapshot_id,))
    seal_row = dict(rows[0]) if rows else {}
    return {
        "snapshot_id": snapshot_id,
        "kb_id": manifest.kb_id,
        "created_at": manifest.created_at,
        "effective_as_of": manifest.effective_as_of,
        "complete": manifest.complete,
        "incomplete": list(manifest.incomplete),
        "sealed": bool(seal_row),
        "sealed_at": seal_row.get("sealed_at"),
        "sealed_by": seal_row.get("sealed_by"),
        "label": seal_row.get("label"),
        "note": seal_row.get("note"),
        "manifest": manifest.as_dict(),
    }


def catalogue(
    context: ServiceContext, knowledge_base: str | None = None, *, limit: int = 50
) -> dict[str, Any]:
    """Every snapshot this region holds, saying which are sealed."""

    kb_id = context.knowledge_base(knowledge_base)
    sealed = {
        str(row["snapshot_id"]): dict(row)
        for row in context.state.rows(
            "SELECT * FROM snapshot_seals WHERE kb_id=? ORDER BY sealed_at DESC LIMIT ?",
            (kb_id, int(limit)),
        )
    }
    manifests = list_snapshots(context.state, kb_id, limit=limit)
    return {
        "knowledge_base": kb_id,
        "snapshots": [
            {
                "snapshot_id": manifest.snapshot_id,
                "created_at": manifest.created_at,
                "effective_as_of": manifest.effective_as_of,
                "complete": manifest.complete,
                "sealed": manifest.snapshot_id in sealed,
                "sealed_at": sealed.get(manifest.snapshot_id, {}).get("sealed_at"),
                "label": sealed.get(manifest.snapshot_id, {}).get("label"),
            }
            for manifest in manifests
        ],
    }


def verify(context: ServiceContext, snapshot_id: str) -> dict[str, Any]:
    """Whether the region still stands where this snapshot says it did.

    Reported rather than raised, because "has my corpus moved" is a question a
    harness asks *between* runs and the answer "yes" is not an error there.
    :func:`require_current` is the raising form, for the pinned search path
    where continuing would produce a result attributed to the wrong state.
    """

    manifest = load_snapshot(context.state, snapshot_id)
    if manifest is None:
        raise SnapshotNotFound(snapshot_id)
    live = build_snapshot(context.state, context.config, graph=context.graph)
    drifted = [
        name for name in manifest.differences(live) if name.split(".", 1)[0] in PINNED_SECTIONS
    ]
    return {
        "snapshot_id": snapshot_id,
        "current": not drifted,
        "live_snapshot_id": live.snapshot_id,
        "drifted": drifted,
        # What a caller does about drift depends entirely on which section
        # moved: `corpus` means somebody indexed, `retrieval` means somebody
        # applied a tuning bundle, `memory` means a record was written. Naming
        # the section is the difference between an actionable report and "the
        # snapshot changed".
        "drifted_sections": sorted({name.split(".", 1)[0] for name in drifted}),
    }


def require_current(context: ServiceContext, snapshot_id: str) -> dict[str, Any]:
    """Verify, and refuse if the region has moved.

    The one place the refusal is raised, so a pinned search on either surface
    fails the same way with the same text — and so that adding a third caller
    cannot accidentally introduce a fourth policy.
    """

    report = verify(context, snapshot_id)
    if not report["current"]:
        raise SnapshotDrifted(snapshot_id, report["drifted"])
    return report


def _section_digest(manifest: Any, section: str) -> str:
    values = manifest.sections().get(section) or {}
    return json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)[:64]


def _manifest_digest(manifest: Any) -> str:
    return manifest.snapshot_id
