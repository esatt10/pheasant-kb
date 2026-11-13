"""Append-only persistence for the evaluation plane.

A thin, deliberate layer over :class:`~pheasant.persistence.state_store.StateStore`
rather than a second storage abstraction. Three properties it has to keep, each
of which has cost this codebase time somewhere else:

**Portable SQL only.** ``INSERT ... ON CONFLICT DO NOTHING``, never
``INSERT OR IGNORE``; ``?`` placeholders that the backend translates. The
SQLite-only spelling is a bug this repository has already shipped and fixed
once, and it did not surface in the offline suite -- it surfaced against a
real Postgres server. Every statement here is written the portable way from
the start.

**Writes are idempotent because ids are content digests.** Re-running a batch
over an unchanged snapshot with unchanged configuration re-derives the same
run id and the same metric row ids, so a retried or duplicated run is a no-op
rather than a second copy of the same numbers. That is the reproducibility
acceptance criterion made mechanical instead of aspirational.

**Nothing here is ever deleted.** Retention is a policy applied by pruning old
*runs*, not by rewriting them: the specification's audit requirement is that an
aggregate resolve to the per-query operands that produced it, and an
after-the-fact edit breaks that for every report already published.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pheasant.evaluation.contracts import (
    Cohort,
    EvaluatedQuery,
    MetricResult,
    Proof,
    SnapshotManifest,
    digest,
)

logger = logging.getLogger(__name__)


def _json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


# --------------------------------------------------------------------------
# snapshots


def save_snapshot(state: Any, manifest: SnapshotManifest) -> str:
    state.execute(
        "INSERT INTO evaluation_snapshots"
        "(snapshot_id, kb_id, created_at, effective_as_of, complete, manifest_json) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT (snapshot_id) DO NOTHING",
        (
            manifest.snapshot_id,
            manifest.kb_id,
            manifest.created_at,
            manifest.effective_as_of,
            1 if manifest.complete else 0,
            _json(manifest.as_dict()),
        ),
    )
    return manifest.snapshot_id


def load_snapshot(state: Any, snapshot_id: str) -> SnapshotManifest | None:
    rows = state.rows(
        "SELECT manifest_json FROM evaluation_snapshots WHERE snapshot_id=?",
        (snapshot_id,),
    )
    if not rows:
        return None
    return SnapshotManifest.from_dict(json.loads(rows[0]["manifest_json"]))


def list_snapshots(state: Any, kb_id: str, *, limit: int = 50) -> list[SnapshotManifest]:
    rows = state.rows(
        "SELECT manifest_json FROM evaluation_snapshots WHERE kb_id=? "
        "ORDER BY created_at DESC, snapshot_id DESC LIMIT ?",
        (kb_id, int(limit)),
    )
    return [SnapshotManifest.from_dict(json.loads(row["manifest_json"])) for row in rows]


def previous_snapshot(
    state: Any, kb_id: str, *, before: str, exclude: str = ""
) -> SnapshotManifest | None:
    """The most recent snapshot strictly older than ``before``.

    What longitudinal deltas and rank churn are measured against. Excluding
    the current snapshot by id as well as by time matters: two snapshots
    created within the same second are ordered by id, and comparing a snapshot
    to itself reports a churn of zero that means nothing.
    """

    rows = state.rows(
        "SELECT manifest_json FROM evaluation_snapshots "
        "WHERE kb_id=? AND created_at<=? AND snapshot_id<>? "
        "ORDER BY created_at DESC, snapshot_id DESC LIMIT 1",
        (kb_id, before, exclude),
    )
    if not rows:
        return None
    return SnapshotManifest.from_dict(json.loads(rows[0]["manifest_json"]))


# --------------------------------------------------------------------------
# proof


def save_proofs(state: Any, proofs: list[Proof]) -> int:
    """Insert proof rows, skipping any that already exist.

    Written one statement at a time rather than as one batch insert, and that
    is deliberate: a batch runs in a single transaction, so one malformed row
    rolls back every good row beside it. The interaction ledger learned this
    the expensive way (one null ``trace_id`` cost a batch of hundreds), and a
    proof arrives from an HTTP body, which is a strictly more hostile source
    than a spool file.
    """

    written = 0
    for proof in proofs:
        try:
            state.execute(
                "INSERT INTO evaluation_proofs(proof_id, kb_id, query_id, target_type, "
                "target_id, event_type, polarity, strength, weight, observed_at, "
                "interaction_id, snapshot_id, principal_partition, position, exposed, "
                "outcome_reference, supersedes_proof_id, reason_code, multipliers_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (proof_id) DO NOTHING",
                (
                    proof.proof_id,
                    proof.kb_id,
                    proof.query_id,
                    proof.target_type,
                    proof.target_id,
                    proof.event_type,
                    proof.polarity,
                    proof.strength,
                    float(proof.weight),
                    proof.observed_at,
                    proof.interaction_id,
                    proof.snapshot_id,
                    proof.principal_partition,
                    proof.position,
                    1 if proof.exposed else 0,
                    proof.outcome_reference,
                    proof.supersedes_proof_id,
                    proof.reason_code,
                    _json(proof.multipliers),
                ),
            )
            written += 1
        except Exception:  # noqa: BLE001 - one bad row must not cost the rest
            logger.warning("evaluation: rejected malformed proof %s", proof.proof_id, exc_info=True)
    return written


def load_proofs(
    state: Any,
    kb_id: str,
    *,
    query_ids: list[str] | None = None,
    before: str | None = None,
    limit: int = 100_000,
) -> list[Proof]:
    """Proof rows for a knowledge base, optionally narrowed and time-capped.

    ``before`` is the leakage control, and it is not optional in practice: a
    replay reconstructing what the region could have known at ``t`` must not
    read evidence recorded after ``t``. Filtering here rather than at the
    metric means every metric inherits the guarantee instead of each one
    having to remember it.
    """

    clauses = ["kb_id=?"]
    params: list[Any] = [kb_id]
    if before:
        clauses.append("observed_at<=?")
        params.append(before)
    if query_ids is not None:
        if not query_ids:
            return []
        # Chunked so a large cohort cannot exceed a driver's parameter limit
        # (SQLite's default ceiling is 999, and a rolling cohort can be
        # larger than that).
        out: list[Proof] = []
        for start in range(0, len(query_ids), 400):
            batch = query_ids[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = state.rows(
                f"SELECT * FROM evaluation_proofs WHERE {' AND '.join(clauses)} "
                f"AND query_id IN ({placeholders}) ORDER BY observed_at, proof_id LIMIT ?",
                (*params, *batch, int(limit)),
            )
            out.extend(Proof.from_row(row) for row in rows)
        return out
    rows = state.rows(
        f"SELECT * FROM evaluation_proofs WHERE {' AND '.join(clauses)} "
        "ORDER BY observed_at, proof_id LIMIT ?",
        (*params, int(limit)),
    )
    return [Proof.from_row(row) for row in rows]


# --------------------------------------------------------------------------
# cohorts


def save_cohort(state: Any, cohort: Cohort) -> str:
    """Persist a cohort. A frozen one is written once and never rewritten.

    The ``DO NOTHING`` is the freeze: an anchor cohort's id is a digest of its
    query set, so re-materializing an unchanged anchor is a no-op, and a
    *changed* anchor gets a new id rather than silently replacing the one every
    past trend point was measured against.
    """

    state.execute(
        "INSERT INTO evaluation_cohorts(cohort_id, kb_id, name, purpose, created_at, "
        "frozen, window_start, window_end, eligibility_digest, query_count, queries_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (cohort_id) DO NOTHING",
        (
            cohort.cohort_id,
            cohort.kb_id,
            cohort.name,
            cohort.purpose,
            cohort.created_at,
            1 if cohort.frozen else 0,
            cohort.window_start,
            cohort.window_end,
            cohort.eligibility_digest,
            cohort.query_count,
            _json([q.as_dict() for q in cohort.queries]),
        ),
    )
    return cohort.cohort_id


def _cohort_from_row(row: Any) -> Cohort:
    data = dict(row)
    queries = tuple(
        EvaluatedQuery(
            **{k: v for k, v in item.items() if k in EvaluatedQuery.__dataclass_fields__}
        )
        for item in json.loads(data.get("queries_json") or "[]")
    )
    return Cohort(
        cohort_id=data["cohort_id"],
        kb_id=data["kb_id"],
        name=data["name"],
        purpose=data["purpose"],
        queries=queries,
        created_at=data.get("created_at") or "",
        frozen=bool(data.get("frozen")),
        window_start=data.get("window_start"),
        window_end=data.get("window_end"),
        eligibility_digest=data.get("eligibility_digest") or "",
    )


def load_cohort(state: Any, cohort_id: str) -> Cohort | None:
    rows = state.rows("SELECT * FROM evaluation_cohorts WHERE cohort_id=?", (cohort_id,))
    return _cohort_from_row(rows[0]) if rows else None


def find_cohort(state: Any, kb_id: str, purpose: str, name: str) -> Cohort | None:
    """The newest cohort with this purpose and name.

    How a frozen anchor is *re-found* on the next run rather than rebuilt from
    a corpus that has moved on. Without this an "anchor" would be re-derived
    every run and would not be an anchor at all.
    """

    rows = state.rows(
        "SELECT * FROM evaluation_cohorts WHERE kb_id=? AND purpose=? AND name=? "
        "ORDER BY created_at DESC, cohort_id DESC LIMIT 1",
        (kb_id, purpose, name),
    )
    return _cohort_from_row(rows[0]) if rows else None


def list_cohorts(state: Any, kb_id: str, *, limit: int = 100) -> list[Cohort]:
    rows = state.rows(
        "SELECT * FROM evaluation_cohorts WHERE kb_id=? ORDER BY created_at DESC LIMIT ?",
        (kb_id, int(limit)),
    )
    return [_cohort_from_row(row) for row in rows]


# --------------------------------------------------------------------------
# runs and metrics


#: How often a running batch stamps its heartbeat, and how stale one must be
#: before another process may declare it dead. The gap is deliberate, and it is
#: the same shape :data:`pheasant.sync.locks.SOURCE_HEARTBEAT_SECONDS` uses: a
#: run that missed two beats has almost certainly died with its container,
#: while one that is merely slow gets three chances to say otherwise.
#:
#: Wider than the source lease's, because a single unit of evaluation work is a
#: whole cohort/variant replay -- hundreds of searches -- rather than one file.
RUN_HEARTBEAT_SECONDS = 15.0
RUN_STALE_SECONDS = 90.0

#: The margin between the two above, and the whole reason the pair means
#: anything: three missed beats is death, and a merely slow run gets three
#: chances to say otherwise.
RUN_HEARTBEAT_MARGIN = 3

#: Floors, so the margin survives an operator narrowing the window. A batch
#: never beats faster than once a second (the beat is a write, and one per
#: replay-second is telemetry becoming load), which is what makes
#: :data:`MINIMUM_RUN_STALE_SECONDS` the narrowest window that can still keep
#: the margin.
MINIMUM_RUN_HEARTBEAT_SECONDS = 1.0
MINIMUM_RUN_STALE_SECONDS = MINIMUM_RUN_HEARTBEAT_SECONDS * RUN_HEARTBEAT_MARGIN


def heartbeat_interval_for(stale_seconds: float | None) -> float:
    """How often a batch must beat for ``stale_seconds`` to mean what it says.

    ``evaluation.run_stale_seconds`` is operator-configurable and the beat was
    not, so the margin above held only at the default. Narrow the window and a
    *live* batch becomes reclaimable: a single cohort/variant replay is
    hundreds of searches and routinely outlasts a short window, so its row
    flips to ``interrupted`` while it is still working -- and
    :func:`~pheasant.evaluation.runner.reclaim_interrupted_runs` frees the
    ``__evaluation__`` lease on the strength of that, which is the one thing
    keeping N replicas to one run. Reproduced at a 20s window, which
    CLAUDE.md records a CI region using so its smoke test need not wait out 90.

    So the beat is derived from the window rather than fixed against it, and
    both scale together. The default (90s) yields exactly the 15s beat it
    always had -- six beats of margin, unchanged -- and every narrower window
    keeps at least three.
    """

    window = max(float(stale_seconds or RUN_STALE_SECONDS), MINIMUM_RUN_STALE_SECONDS)
    return max(
        MINIMUM_RUN_HEARTBEAT_SECONDS,
        min(RUN_HEARTBEAT_SECONDS, window / RUN_HEARTBEAT_MARGIN),
    )


#: Terminal states. Nothing rewrites a run in one of these.
TERMINAL_RUN_STATUSES = ("completed", "truncated", "invalid", "failed", "interrupted")

#: Terminal *and* settled: the batch reached an answer and published it.
#: :func:`open_run` refuses to re-open one of these, because the report is the
#: run's whole output and re-deriving it can only overwrite it with itself.
#:
#: ``failed`` and ``interrupted`` are terminal but deliberately absent: both
#: say "this batch did not finish", and re-running one is the resume path the
#: replay checkpoints exist for.
SETTLED_RUN_STATUSES = ("completed", "truncated", "invalid")


def open_run(
    state: Any,
    *,
    run_id: str,
    kb_id: str,
    snapshot_id: str,
    started_at: str,
    mode: str,
    config_digest: str,
    owner: str = "",
    total_units: int = 0,
) -> dict[str, Any]:
    """Claim a run row, resume the one already there, or decline it.

    Returns ``{"claimed": bool, "resumed": bool, "attempts": int,
    "previous_status": str}``.

    Resuming is the normal path after a container restart: the run id is
    content-addressed, so the same batch re-derives the same row rather than
    starting a second one, and ``attempts`` counts how many times it has been
    picked up. The insert and the claim are one statement each, and neither
    ever rewrites a *terminal* row -- a completed run is history.

    ``claimed`` is the caller's instruction, and it is separate from ``resumed``
    because there are three outcomes, not two: a fresh claim, a resumed claim,
    and a *refusal*. A run that already reached a settled terminal status is
    refused, and the caller must not proceed -- see the comment on that branch
    for what proceeding did.
    """

    # Read before writing, so "this row did not exist" and "this row is being
    # picked up again" stay distinguishable -- an insert that reports success
    # either way cannot tell a first attempt from a resume, and would have every
    # fresh run claiming to be a recovery.
    #
    # The read-then-write race is real and harmless here: the evaluation lease
    # already serializes batches per `/state`, the INSERT is
    # `ON CONFLICT DO NOTHING`, and the worst a lost race costs is an attempt
    # counter off by one. Correctness does not ride on it; the checkpoints do
    # that, and they are keyed by (run, cohort, variant).
    existing = state.rows("SELECT status, attempts FROM evaluation_runs WHERE run_id=?", (run_id,))
    if not existing:
        state.execute(
            "INSERT INTO evaluation_runs(run_id, kb_id, snapshot_id, started_at, status, "
            "mode, config_digest, phase, completed_units, total_units, heartbeat_at, owner, "
            "attempts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (run_id) DO NOTHING",
            (
                run_id,
                kb_id,
                snapshot_id,
                started_at,
                "running",
                mode,
                config_digest,
                "starting",
                0,
                int(total_units),
                started_at,
                owner,
                1,
            ),
        )
        return {"claimed": True, "resumed": False, "attempts": 1, "previous_status": ""}

    previous = str(existing[0]["status"] or "")
    attempts = int(existing[0]["attempts"] or 1)
    if previous in SETTLED_RUN_STATUSES:
        # A completed, truncated or invalid run is history: re-running it would
        # destroy the report it published, and its numbers are reproducible by
        # construction anyway.
        #
        # This branch used to only *decline to write the row* and let the caller
        # replay the whole matrix on top of it, which is not the same thing and
        # broke three ways at once. The row kept saying `completed` while a
        # batch was genuinely in flight, so every watcher -- the UI, `eval
        # status --watch`, `get_evaluation_status` -- read a live run as
        # finished, with a `fraction` that fell from 1.0 back to 0.02 and
        # climbed again; `active_run` (which filters `status='running'`) could
        # not see it at all; and if that process died the row stayed
        # `completed` forever with `phase='replay'` and a stale
        # `completed_units`, because `reclaim_stale_runs` filters on
        # `status='running'` too -- so nothing could ever reclaim it, and the
        # replay checkpoints it left behind were never loaded and never
        # cleared. On top of which it re-replayed every cohort and variant, on
        # every scheduled beat, to overwrite an identical report.
        #
        # `failed` and `interrupted` are terminal but not settled: both mean
        # "this batch did not finish", and picking one up again is exactly the
        # recovery the checkpoints exist for.
        return {
            "claimed": False,
            "resumed": False,
            "attempts": attempts,
            "previous_status": previous,
        }
    state.execute(
        "UPDATE evaluation_runs SET status=?, phase=?, heartbeat_at=?, owner=?, "
        "attempts=attempts+1, error=NULL, finished_at=NULL, total_units=? WHERE run_id=?",
        ("running", "starting", started_at, owner, int(total_units), run_id),
    )
    return {
        "claimed": True,
        "resumed": True,
        "attempts": attempts + 1,
        "previous_status": previous,
    }


def heartbeat_run(
    state: Any,
    *,
    run_id: str,
    now: str,
    phase: str | None = None,
    detail: str | None = None,
    completed_units: int | None = None,
    total_units: int | None = None,
) -> None:
    """Stamp progress. Fail-soft: a lost beat must never fail the batch.

    A progress write that could raise would make observability able to break
    the thing it observes -- the posture ``jobs.progress`` already takes for the
    indexing loop, and for the same reason.
    """

    sets = ["heartbeat_at=?"]
    params: list[Any] = [now]
    if phase is not None:
        sets.append("phase=?")
        params.append(phase)
    if detail is not None:
        sets.append("phase_detail=?")
        params.append(detail[:400])
    if completed_units is not None:
        sets.append("completed_units=?")
        params.append(int(completed_units))
    if total_units is not None:
        sets.append("total_units=?")
        params.append(int(total_units))
    try:
        state.execute(
            f"UPDATE evaluation_runs SET {', '.join(sets)} WHERE run_id=?",
            (*params, run_id),
        )
    except Exception:  # noqa: BLE001 - progress must not be able to fail a run
        logger.debug("evaluation: progress write failed for %s", run_id, exc_info=True)


def close_run(
    state: Any,
    *,
    run_id: str,
    finished_at: str,
    status: str,
    gates_passed: bool,
    report: dict[str, Any],
    error: str | None = None,
) -> None:
    state.execute(
        "UPDATE evaluation_runs SET finished_at=?, status=?, gates_passed=?, report_json=?, "
        "phase=?, phase_detail=NULL, heartbeat_at=?, error=? WHERE run_id=?",
        (
            finished_at,
            status,
            1 if gates_passed else 0,
            _json(report),
            status,
            finished_at,
            error,
            run_id,
        ),
    )
    if status in ("completed", "truncated", "invalid") and report:
        from pheasant.artifacts.publications import publish

        rows = state.rows("SELECT kb_id FROM evaluation_runs WHERE run_id=?", (run_id,))
        if rows:
            publish(
                state,
                rows[0]["kb_id"],
                f"evaluation:{run_id}",
                report,
                receipt={
                    "run_id": run_id,
                    "gates_passed": gates_passed,
                    "retrieval_eligible": False,
                },
            )


def reclaim_stale_runs(
    state: Any,
    kb_id: str,
    *,
    now: str,
    stale_before: str,
) -> list[str]:
    """Mark runs whose heartbeat died as ``interrupted``, and say which.

    This is the container-stopped case, and it is the reason progress is a row
    rather than a process. Without it a killed batch leaves ``running`` forever:
    the UI shows a spinner nobody will ever stop, and the next scheduled run
    sees work apparently in flight.

    ``interrupted`` rather than ``failed`` on purpose -- the run did not fail,
    it was cut off, and the distinction decides whether resuming it is sensible
    (it is: the checkpointed replays are still there).
    """

    stale = state.rows(
        "SELECT run_id FROM evaluation_runs WHERE kb_id=? AND status='running' "
        "AND (heartbeat_at IS NULL OR heartbeat_at < ?) ORDER BY started_at",
        (kb_id, stale_before),
    )
    reclaimed: list[str] = []
    for row in stale:
        run_id = str(row["run_id"])
        state.execute(
            "UPDATE evaluation_runs SET status='interrupted', phase='interrupted', "
            "phase_detail=?, finished_at=?, error=? WHERE run_id=? AND status='running'",
            (
                "the process running this batch stopped without finishing it",
                now,
                "heartbeat expired; the run was reclaimed",
                run_id,
            ),
        )
        reclaimed.append(run_id)
    if reclaimed:
        logger.info("evaluation: reclaimed %s stale run(s)", len(reclaimed))
    return reclaimed


def run_status(state: Any, run_id: str) -> dict[str, Any] | None:
    """One run's live progress, readable from any process at any time."""

    rows = state.rows(
        "SELECT run_id, kb_id, snapshot_id, started_at, finished_at, status, mode, "
        "gates_passed, phase, phase_detail, completed_units, total_units, heartbeat_at, "
        "owner, attempts, error FROM evaluation_runs WHERE run_id=?",
        (run_id,),
    )
    if not rows:
        return None
    payload = dict(rows[0])
    payload["gates_passed"] = bool(payload.get("gates_passed"))
    payload["completed_units"] = int(payload.get("completed_units") or 0)
    payload["total_units"] = int(payload.get("total_units") or 0)
    payload["attempts"] = int(payload.get("attempts") or 0)
    total = payload["total_units"]
    payload["fraction"] = round(min(1.0, payload["completed_units"] / total), 4) if total else None
    payload["active"] = payload["status"] == "running"
    return payload


def active_run(state: Any, kb_id: str) -> dict[str, Any] | None:
    """The batch currently in flight for this knowledge base, if any."""

    rows = state.rows(
        "SELECT run_id FROM evaluation_runs WHERE kb_id=? AND status='running' "
        "ORDER BY started_at DESC LIMIT 1",
        (kb_id,),
    )
    return run_status(state, str(rows[0]["run_id"])) if rows else None


# --------------------------------------------------------------------------
# replay checkpoints


def save_replay_checkpoint(
    state: Any,
    *,
    run_id: str,
    kb_id: str,
    cohort_id: str,
    cohort_name: str,
    variant_id: str,
    completed_at: str,
    query_count: int,
    failure_count: int,
    duration_ms: float,
    payload: dict[str, Any],
) -> None:
    """Record one finished (cohort, variant) replay so a restart need not redo it."""

    state.execute(
        "INSERT INTO evaluation_replays(id, run_id, kb_id, cohort_id, cohort_name, "
        "variant_id, completed_at, query_count, failure_count, duration_ms, results_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING",
        (
            digest(run_id, cohort_id, variant_id),
            run_id,
            kb_id,
            cohort_id,
            cohort_name,
            variant_id,
            completed_at,
            int(query_count),
            int(failure_count),
            float(duration_ms),
            _json(payload),
        ),
    )


def load_replay_checkpoints(state: Any, run_id: str) -> dict[tuple[str, str], dict[str, Any]]:
    """``{(cohort_name, variant_id): payload}`` for everything already replayed."""

    try:
        rows = state.rows(
            "SELECT cohort_name, variant_id, results_json FROM evaluation_replays WHERE run_id=?",
            (run_id,),
        )
    except Exception:  # noqa: BLE001 - a resume must not fail on a missing table
        logger.debug("evaluation: replay checkpoints unavailable", exc_info=True)
        return {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        try:
            out[(str(row["cohort_name"]), str(row["variant_id"]))] = json.loads(row["results_json"])
        except (TypeError, ValueError):
            continue
    return out


def clear_replay_checkpoints(state: Any, run_id: str) -> None:
    """Drop a finished run's scaffolding.

    The evidence a completed run leaves behind is its metric rows and its
    report; the checkpoints exist only to make a *restart* cheap. Keeping them
    would grow ``/state`` by a copy of every result list, forever, for no
    reader -- which is the growth the retention policies elsewhere exist to
    prevent.
    """

    try:
        state.execute("DELETE FROM evaluation_replays WHERE run_id=?", (run_id,))
    except Exception:  # noqa: BLE001 - cleanup must not fail a finished run
        logger.debug("evaluation: could not clear checkpoints for %s", run_id, exc_info=True)


def save_metrics(state: Any, run_id: str, kb_id: str, results: list[MetricResult]) -> int:
    written = 0
    for result in results:
        row_id = digest(
            run_id,
            result.metric_id,
            result.scope.cohort_id,
            result.scope.variant_id,
            result.scope.query_id,
        )
        try:
            state.execute(
                "INSERT INTO evaluation_metrics(id, run_id, kb_id, metric_id, metric_version, "
                "classification, snapshot_id, cohort_id, variant_id, query_id, value, "
                "numerator, denominator, status, payload_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING",
                (
                    row_id,
                    run_id,
                    kb_id,
                    result.metric_id,
                    result.metric_version,
                    result.classification,
                    result.scope.snapshot_id,
                    result.scope.cohort_id,
                    result.scope.variant_id,
                    result.scope.query_id,
                    result.value,
                    result.numerator,
                    result.denominator,
                    result.status,
                    _json(result.as_dict()),
                ),
            )
            written += 1
        except Exception:  # noqa: BLE001 - one metric must not cost a whole run
            logger.warning("evaluation: could not persist %s", result.metric_id, exc_info=True)
    return written


def load_report(state: Any, run_id: str) -> dict[str, Any] | None:
    rows = state.rows("SELECT report_json FROM evaluation_runs WHERE run_id=?", (run_id,))
    if not rows or not rows[0]["report_json"]:
        return None
    return json.loads(rows[0]["report_json"])


def latest_run(state: Any, kb_id: str) -> dict[str, Any] | None:
    rows = state.rows(
        "SELECT run_id, snapshot_id, started_at, finished_at, status, mode, gates_passed "
        "FROM evaluation_runs WHERE kb_id=? ORDER BY started_at DESC, run_id DESC LIMIT 1",
        (kb_id,),
    )
    return dict(rows[0]) if rows else None


def list_runs(state: Any, kb_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = state.rows(
        "SELECT run_id, snapshot_id, started_at, finished_at, status, mode, gates_passed "
        "FROM evaluation_runs WHERE kb_id=? ORDER BY started_at DESC, run_id DESC LIMIT ?",
        (kb_id, int(limit)),
    )
    return [dict(row) for row in rows]


def metric_rows(
    state: Any,
    run_id: str,
    *,
    metric_id: str | None = None,
    cohort_purpose: str | None = None,
    variant_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """The stored metric results for one run, filtered, with their full payloads.

    How a reader gets from an aggregate to the queries, ranks and proof events
    behind it. Per-query rows and aggregates share the table (an aggregate has
    a NULL ``query_id``), so this returns both and the caller decides which it
    wanted — which is what makes "resolve this 0.89 to the five queries behind
    it" one request rather than a download of the whole report.
    """

    clauses = ["m.run_id=?"]
    params: list[Any] = [run_id]
    if metric_id:
        clauses.append("m.metric_id=?")
        params.append(metric_id)
    if variant_id:
        clauses.append("m.variant_id=?")
        params.append(variant_id)
    if cohort_purpose:
        clauses.append("c.purpose=?")
        params.append(cohort_purpose)
    rows = state.rows(
        "SELECT m.metric_id AS metric_id, m.variant_id AS variant_id, "
        "m.query_id AS query_id, m.value AS value, m.status AS status, "
        "c.name AS cohort_name, c.purpose AS cohort_purpose, m.payload_json AS payload_json "
        "FROM evaluation_metrics m "
        "LEFT JOIN evaluation_cohorts c ON c.cohort_id = m.cohort_id "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY m.metric_id, m.variant_id, m.query_id LIMIT ?",
        (*params, int(limit)),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        raw = payload.pop("payload_json", None)
        try:
            payload["result"] = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            payload["result"] = None
        out.append(payload)
    return out


def metric_trend(
    state: Any,
    kb_id: str,
    metric_id: str,
    *,
    cohort_name: str | None = None,
    variant_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """One metric's history, newest last, for longitudinal reporting.

    Joined against ``evaluation_runs`` for the snapshot time rather than
    ordering on the metric row, which carries no clock of its own -- and
    against ``evaluation_cohorts`` when a cohort *name* is asked for, because
    a frozen anchor keeps one name across many cohort ids only when it never
    changes, and the trend must survive it changing.
    """

    clauses = ["m.kb_id=?", "m.metric_id=?", "m.query_id IS NULL"]
    params: list[Any] = [kb_id, metric_id]
    if variant_id:
        clauses.append("m.variant_id=?")
        params.append(variant_id)
    if cohort_name:
        clauses.append("c.name=?")
        params.append(cohort_name)
    rows = state.rows(
        "SELECT r.started_at AS started_at, m.snapshot_id AS snapshot_id, m.run_id AS run_id, "
        "m.value AS value, m.numerator AS numerator, m.denominator AS denominator, "
        "m.status AS status, m.variant_id AS variant_id, m.cohort_id AS cohort_id "
        "FROM evaluation_metrics m "
        "JOIN evaluation_runs r ON r.run_id=m.run_id "
        "LEFT JOIN evaluation_cohorts c ON c.cohort_id=m.cohort_id "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY r.started_at DESC, m.id DESC LIMIT ?",
        (*params, int(limit)),
    )
    return [dict(row) for row in reversed(rows)]
