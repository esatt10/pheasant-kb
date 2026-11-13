"""Where a tuning batch keeps things, and why it is two places.

The split is the same one the whole product is built on -- ``/state`` is
operational truth, ``/exports`` is regenerable payload -- applied to a workload
that would otherwise abuse the first.

**What goes in ``/state``**: an experiment, a trial's *scores*, a decision, a
bundle. Small rows, queried by the UI, the CLI, HTTP and MCP. A trial row stays
a few hundred bytes however large the cohort is.

**What goes in cold storage**: the per-query ranked lists behind those scores --
the stage captures, the arm candidates, the fused orders. On a 200-query cohort
with 400 trials that is 80,000 ranked lists, and they are *derivable*: given the
corpus, the snapshot and the parameters, the ranking can be recomputed. Keeping
them is worth doing (they are what makes a decision auditable months later) and
keeping them in an operational database is not.

Cold storage is ``<exports>/tuning/<kb_id>/<experiment>/*.jsonl.zst``:
zstd-compressed JSON lines, one object per query, written once and never
updated. Zstandard is already a core dependency, so this needs no extra;
JSONL because an audit reads it with ``zstdcat | jq`` and should not need this
package installed to do so.

Three properties the writes have to keep, each learned somewhere else in this
repository.

**A batch write must not lose good rows to one bad one.** A queued batch of
observations once rolled back in full because a single event carried a null
``trace_id``; the fix was to validate *before* the statement, since a
rolled-back transaction cannot drop one row and keep the rest. Trials are
validated by :meth:`Trial.validate` before they are offered to the database,
for exactly that reason.

**Cold storage is best-effort, and the row is not.** ``/exports`` can be a
read-only mount, a full volume, or absent entirely. A cold write that fails is
logged and the trial is still stored with an empty ``cold_ref`` -- losing the
audit detail is bad, losing the experiment because a volume filled is worse.

**At most one bundle is active, enforced in the write.** Applying a bundle
supersedes the incumbent in the same transaction that applies the new one. Two
active overlays would make two replicas rank differently while both reported
the fleet as converged.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from pheasant.evaluation.contracts import utc_now
from pheasant.tuning.contracts import (
    Decision,
    Diagnosis,
    Experiment,
    Trial,
    TuningBundle,
)

logger = logging.getLogger(__name__)

#: Subdirectory under ``exports_path``, alongside ``parquet/``.
COLD_SUBDIR = "tuning"

#: Statuses an experiment row can carry.
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
INTERRUPTED = "interrupted"
CANCELLED = "cancelled"


def _json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _write(state: Any, sql: str, params: tuple[Any, ...] = ()) -> int:
    """Run a write and return how many rows it changed.

    ``StateStore.execute`` returns nothing, and every claim in this module
    turns on whether a conditional ``UPDATE`` actually matched -- so the
    rowcount is the answer, not a diagnostic. Routed through
    ``backend.statement`` (which returns ``(rows, rowcount)``) and committed
    here for the same reason ``execute_returning`` commits: on a pooled backend
    a read may hand the connection back, and a later commit would then land on
    a different one and silently discard the write.
    """

    _rows, rowcount = state.backend.statement(sql, params)
    state.backend.commit()
    return int(rowcount or 0)


# --------------------------------------------------------------------------
# cold storage
# --------------------------------------------------------------------------


def cold_dir(exports_path: str | Path, kb_id: str, experiment_id: str) -> Path:
    return Path(exports_path) / COLD_SUBDIR / kb_id / experiment_id


def write_cold(
    exports_path: str | Path | None,
    kb_id: str,
    experiment_id: str,
    name: str,
    records: list[dict[str, Any]],
) -> str:
    """Write one compressed JSONL payload. Returns its ref, or "" on failure.

    Never raises. A cold write is an audit convenience; an experiment that
    could not write one still produced a valid result, and failing the batch
    because ``/exports`` is read-only would take a working feature away from
    every region that mounts it that way -- which is the arrangement
    ``docker-compose.scale.yml`` recommends.
    """

    if not exports_path or not records:
        return ""
    tmp: Path | None = None
    try:
        import zstandard

        target = cold_dir(exports_path, kb_id, experiment_id)
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{name}.jsonl.zst"
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.partial")
        body = "\n".join(_json(record) for record in records).encode("utf-8")
        compressor = zstandard.ZstdCompressor(level=10)
        # Written to a temporary name and renamed, so a reader never sees a
        # half-written file: `pheasant export query` and an operator with
        # `zstdcat` both point at this directory while a batch is running.
        #
        # The temp name is unique per writer. A fixed `.partial` collided when
        # two batches raced: both wrote the same path, the first rename took
        # it, and the second `os.replace` raised FileNotFoundError on a file
        # that had just been renamed out from under it.
        tmp.write_bytes(compressor.compress(body))
        os.replace(tmp, path)
        return str(path)
    except Exception:  # noqa: BLE001 - cold storage must never fail a batch
        logger.warning(
            "tuning: could not write cold payload %r for %s", name, experiment_id, exc_info=True
        )
        # A rename that failed leaves the temp file behind, and these are
        # unique per writer now — so without this a failing volume would
        # accumulate one orphan per attempt rather than one in total.
        try:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return ""


def read_cold(ref: str) -> list[dict[str, Any]]:
    """Read a cold payload back. Empty on any failure, including absence."""

    if not ref:
        return []
    try:
        import zstandard

        raw = zstandard.ZstdDecompressor().decompress(Path(ref).read_bytes())
        return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    except Exception:  # noqa: BLE001
        logger.warning("tuning: could not read cold payload %r", ref, exc_info=True)
        return []


# --------------------------------------------------------------------------
# experiments
# --------------------------------------------------------------------------


def open_experiment(
    state: Any,
    experiment: Experiment,
    *,
    owner: str,
    stale_before: str | None = None,
) -> bool:
    """Claim the experiment row. ``False`` means somebody else holds it.

    The claim is a conditional ``UPDATE`` whose ``WHERE`` the winner's own
    write falsifies -- not a read-then-write. Under Postgres READ COMMITTED
    only the *outer* ``WHERE`` is re-evaluated after a blocking update's winner
    commits, so the predicate has to be one the winner changed: here that is
    ``status``, which the winner sets to ``running``.
    """

    now = utc_now()
    existing = state.rows(
        "SELECT experiment_id, status, heartbeat_at, attempts FROM tuning_experiments "
        "WHERE experiment_id = ?",
        (experiment.experiment_id,),
    )
    if not existing:
        _write(
            state,
            """INSERT INTO tuning_experiments
               (experiment_id, kb_id, snapshot_id, cohort_id, holdout_cohort_id,
                control_cohort_id, space_digest, baseline_point_id, budget_json,
                started_at, status, phase, owner, attempts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)
               ON CONFLICT (experiment_id) DO NOTHING""",
            (
                experiment.experiment_id,
                experiment.kb_id,
                experiment.snapshot_id,
                experiment.cohort_id,
                experiment.holdout_cohort_id,
                experiment.control_cohort_id,
                experiment.space_digest,
                experiment.baseline_point.point_id,
                _json(experiment.budget),
                now,
                RUNNING,
                "starting",
                owner,
            ),
        )
        rows = state.rows(
            "SELECT owner FROM tuning_experiments WHERE experiment_id = ?",
            (experiment.experiment_id,),
        )
        return bool(rows) and str(rows[0]["owner"]) == owner

    row = existing[0]
    if str(row["status"]) == RUNNING:
        heartbeat = str(row["heartbeat_at"] or "")
        if not stale_before or (heartbeat and heartbeat > stale_before):
            return False
    changed = _write(
        state,
        """UPDATE tuning_experiments
              SET status = ?, owner = ?, started_at = ?, finished_at = NULL,
                  error = NULL, heartbeat_at = ?, attempts = attempts + 1,
                  phase = 'starting'
            WHERE experiment_id = ? AND status <> ?""",
        (RUNNING, owner, now, now, experiment.experiment_id, RUNNING),
    )
    if changed:
        return True
    # The row was already `running` and its heartbeat was stale: take it over
    # only if this process wins the race to do so.
    changed = _write(
        state,
        """UPDATE tuning_experiments
              SET owner = ?, started_at = ?, heartbeat_at = ?, attempts = attempts + 1,
                  phase = 'starting'
            WHERE experiment_id = ? AND status = ? AND COALESCE(heartbeat_at, '') <= ?""",
        (owner, now, now, experiment.experiment_id, RUNNING, stale_before or ""),
    )
    return bool(changed)


def publish_phase(
    state: Any,
    experiment_id: str,
    *,
    phase: str,
    detail: str = "",
    completed: int | None = None,
    total: int | None = None,
    searches: int | None = None,
) -> None:
    """Move the progress row. The durable half of "the UI can watch this"."""

    sets = ["phase = ?", "phase_detail = ?", "heartbeat_at = ?"]
    params: list[Any] = [phase, detail, utc_now()]
    if completed is not None:
        sets.append("completed_units = ?")
        params.append(int(completed))
    if total is not None:
        sets.append("total_units = ?")
        params.append(int(total))
    if searches is not None:
        sets.append("searches = ?")
        params.append(int(searches))
    params.append(experiment_id)
    _write(
        state,
        f"UPDATE tuning_experiments SET {', '.join(sets)} WHERE experiment_id = ?",
        tuple(params),
    )


def request_cancel(state: Any, kb_id: str, experiment_id: str, *, requested_by: str) -> bool:
    """Ask a running batch to stand down. ``False`` if there was none.

    Sets a column rather than signalling a thread, because the replica serving
    the cancel is usually not the replica running the batch. The runner reads
    this between units, so the batch stops at its next checkpoint with its
    trials already stored — which makes a cancel resumable rather than
    destructive, the same property standing down under index-queue pressure
    has.
    """

    changed = _write(
        state,
        """UPDATE tuning_experiments SET cancel_requested = 1, cancel_requested_by = ?
            WHERE experiment_id = ? AND kb_id = ? AND status = ?""",
        (requested_by, experiment_id, kb_id, RUNNING),
    )
    return bool(changed)


def cancel_requested(state: Any, experiment_id: str) -> str:
    """Who asked this batch to stop, or "" if nobody has.

    Read between units, so it is one indexed single-row select on the primary
    key — cheap enough to check often, which is what makes a cancel feel
    immediate rather than arriving whenever the batch happens to finish.
    """

    try:
        rows = state.rows(
            "SELECT cancel_requested, cancel_requested_by FROM tuning_experiments "
            "WHERE experiment_id = ?",
            (experiment_id,),
        )
    except Exception:  # noqa: BLE001 - a /state predating the column
        return ""
    if not rows or not int(rows[0]["cancel_requested"] or 0):
        return ""
    return str(rows[0]["cancel_requested_by"] or "a caller")


def prune_experiment(
    state: Any, kb_id: str, experiment_id: str, *, exports_path: str | Path | None = None
) -> dict[str, int]:
    """Delete one experiment and its trials. Returns what it removed.

    Bundles are deliberately **not** removed: a bundle may be the region's live
    overlay, and deleting the experiment that produced it must not leave the
    fleet serving a configuration whose provenance has been erased. The bundle
    keeps its `experiment_id`, which then names a row that is gone — which is
    honest ("this came from a pruned experiment") rather than misleading.
    """

    with state.conn:
        deleted = state.conn.execute(
            "DELETE FROM tuning_experiments WHERE experiment_id=? AND kb_id=? AND status<>? "
            "RETURNING experiment_id",
            (experiment_id, kb_id, RUNNING),
        ).fetchall()
        if not deleted:
            return {"experiments": 0, "trials": 0, "decisions": 0}
        experiments = len(deleted)
        trials = len(
            state.conn.execute(
                "DELETE FROM tuning_trials WHERE experiment_id=? RETURNING trial_id",
                (experiment_id,),
            ).fetchall()
        )
        decisions = len(
            state.conn.execute(
                "DELETE FROM tuning_decisions WHERE experiment_id=? AND kb_id=? "
                "RETURNING decision_id",
                (experiment_id, kb_id),
            ).fetchall()
        )
        if exports_path:
            import shutil

            root = (Path(exports_path) / COLD_SUBDIR).resolve()
            target = cold_dir(exports_path, kb_id, experiment_id).resolve()
            if target.is_relative_to(root) and target != root and target.is_dir():
                shutil.rmtree(target)
    return {"experiments": experiments, "trials": trials, "decisions": decisions}


def heartbeat(state: Any, experiment_id: str) -> None:
    _write(
        state,
        "UPDATE tuning_experiments SET heartbeat_at = ? WHERE experiment_id = ? AND status = ?",
        (utc_now(), experiment_id, RUNNING),
    )


def close_experiment(
    state: Any,
    experiment_id: str,
    *,
    status: str,
    report: dict[str, Any] | None = None,
    diagnosis: Diagnosis | None = None,
    error: str = "",
) -> None:
    _write(
        state,
        """UPDATE tuning_experiments
              SET status = ?, finished_at = ?, phase = ?, report_json = ?,
                  diagnosis_json = COALESCE(?, diagnosis_json), error = ?
            WHERE experiment_id = ?""",
        (
            status,
            utc_now(),
            status,
            _json(report) if report is not None else None,
            _json(diagnosis.as_dict()) if diagnosis is not None else None,
            error,
            experiment_id,
        ),
    )


def reclaim_stale_experiments(state: Any, kb_id: str, stale_before: str) -> list[str]:
    """Mark batches whose process died as ``interrupted``. Returns their ids.

    The staleness test lives *in* the ``UPDATE`` rather than in a read before
    it, so a legitimate successor that started between the read and the write
    survives. The evaluation plane learned this from a killed container whose
    run row and lease aged out on two different clocks.
    """

    rows = state.rows(
        """SELECT experiment_id FROM tuning_experiments
            WHERE kb_id = ? AND status = ? AND COALESCE(heartbeat_at, '') <= ?""",
        (kb_id, RUNNING, stale_before),
    )
    reclaimed: list[str] = []
    for row in rows:
        experiment_id = str(row["experiment_id"])
        changed = _write(
            state,
            """UPDATE tuning_experiments
                  SET status = ?, finished_at = ?, phase = ?,
                      error = 'the process running this batch stopped'
                WHERE experiment_id = ? AND status = ? AND COALESCE(heartbeat_at, '') <= ?""",
            (INTERRUPTED, utc_now(), INTERRUPTED, experiment_id, RUNNING, stale_before),
        )
        if changed:
            reclaimed.append(experiment_id)
    return reclaimed


def experiment_status(state: Any, experiment_id: str) -> dict[str, Any] | None:
    rows = state.rows("SELECT * FROM tuning_experiments WHERE experiment_id = ?", (experiment_id,))
    return _experiment_row(rows[0]) if rows else None


def active_experiment(state: Any, kb_id: str) -> dict[str, Any] | None:
    rows = state.rows(
        """SELECT * FROM tuning_experiments WHERE kb_id = ? AND status = ?
            ORDER BY started_at DESC""",
        (kb_id, RUNNING),
    )
    return _experiment_row(rows[0]) if rows else None


def latest_experiment(state: Any, kb_id: str) -> dict[str, Any] | None:
    rows = state.rows(
        "SELECT * FROM tuning_experiments WHERE kb_id = ? ORDER BY started_at DESC",
        (kb_id,),
    )
    return _experiment_row(rows[0]) if rows else None


def list_experiments(state: Any, kb_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = state.rows(
        "SELECT * FROM tuning_experiments WHERE kb_id = ? ORDER BY started_at DESC LIMIT ?",
        (kb_id, int(limit)),
    )
    return [_experiment_row(row) for row in rows]


def _experiment_row(row: Any) -> dict[str, Any]:
    def value(key: str, default: Any = None) -> Any:
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return default

    completed = int(value("completed_units", 0) or 0)
    total = int(value("total_units", 0) or 0)
    return {
        "experiment_id": str(value("experiment_id", "")),
        "kb_id": str(value("kb_id", "")),
        "snapshot_id": str(value("snapshot_id", "")),
        "cohort_id": str(value("cohort_id", "")),
        "status": str(value("status", "")),
        "phase": str(value("phase") or ""),
        "phase_detail": str(value("phase_detail") or ""),
        "completed_units": completed,
        "total_units": total,
        # Reported rather than left to each caller: the CLI, the UI and MCP
        # would otherwise each divide by a total that can legitimately be zero.
        "progress": (completed / total) if total else None,
        "searches": int(value("searches", 0) or 0),
        "started_at": str(value("started_at") or ""),
        "finished_at": str(value("finished_at") or ""),
        "heartbeat_at": str(value("heartbeat_at") or ""),
        "attempts": int(value("attempts", 0) or 0),
        "error": str(value("error") or ""),
        "diagnosis": json.loads(value("diagnosis_json") or "null"),
        "report": json.loads(value("report_json") or "null"),
    }


# --------------------------------------------------------------------------
# trials
# --------------------------------------------------------------------------


def save_trial(state: Any, trial: Trial, kb_id: str, *, cold_ref: str = "") -> list[str]:
    """Store one completed trial. Returns validation problems, if any.

    Validated **before** the statement, not inside the transaction: a batch of
    trials shares one, and a rolled-back transaction cannot drop the one bad
    row and keep the rest. That is not a hypothetical shape here -- the
    observation plane dead-lettered hundreds of good events over one null
    ``trace_id`` before validation moved ahead of the insert.
    """

    problems = trial.validate()
    if problems:
        logger.warning("tuning: refusing to store trial %s: %s", trial.trial_id, problems)
        return problems
    _write(
        state,
        """INSERT INTO tuning_trials
           (trial_id, experiment_id, kb_id, point_id, cohort_id, cohort_name,
            cost_class, motivating_stage, generation, created_at,
            evaluated_queries, excluded_queries, searches, duration_ms,
            primary_metric, point_json, proposal_json, metrics_json,
            histogram_json, cold_ref, failed)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (trial_id) DO NOTHING""",
        (
            trial.trial_id,
            trial.experiment_id,
            kb_id,
            trial.proposal.point.point_id,
            trial.cohort_id,
            trial.cohort_name,
            trial.proposal.cost_class,
            trial.proposal.motivating_stage,
            trial.proposal.generation,
            trial.created_at,
            trial.evaluated_queries,
            trial.excluded_queries,
            trial.searches,
            trial.duration_ms,
            trial.metrics.get(PRIMARY_METRIC),
            _json(trial.proposal.point.as_dict()),
            _json(trial.proposal.as_dict()),
            _json(trial.metrics),
            _json(trial.histogram),
            cold_ref,
            trial.failed,
        ),
    )
    return []


#: The metric a trial is ranked by. Named once, here, so the strategy, the
#: gates, the report and the SQL ``ORDER BY`` cannot disagree about what
#: "best" meant -- which they would, silently, the first time one of them was
#: changed.
PRIMARY_METRIC = "known_positive_reciprocal_rank"


def load_trials(state: Any, experiment_id: str) -> dict[str, dict[str, Any]]:
    """Completed trials, keyed ``(point_id, cohort_id)``.

    This is what makes a batch **resume** rather than restart: the experiment
    id is content-addressed, so a re-run derives the same id, loads these, and
    evaluates only the points that are missing.
    """

    rows = state.rows(
        "SELECT * FROM tuning_trials WHERE experiment_id = ?",
        (experiment_id,),
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f"{row['point_id']}::{row['cohort_id']}"
        out[key] = {
            "trial_id": str(row["trial_id"]),
            "point_id": str(row["point_id"]),
            "cohort_id": str(row["cohort_id"]),
            "cohort_name": str(row["cohort_name"]),
            "cost_class": str(row["cost_class"]),
            "motivating_stage": str(row["motivating_stage"]),
            "generation": int(row["generation"] or 0),
            "metrics": json.loads(row["metrics_json"] or "{}"),
            "histogram": json.loads(row["histogram_json"] or "{}"),
            "proposal": json.loads(row["proposal_json"] or "{}"),
            "point": json.loads(row["point_json"] or "{}"),
            "evaluated_queries": int(row["evaluated_queries"] or 0),
            "excluded_queries": int(row["excluded_queries"] or 0),
            "searches": int(row["searches"] or 0),
            "duration_ms": float(row["duration_ms"] or 0.0),
            "cold_ref": str(row["cold_ref"] or ""),
            "failed": str(row["failed"] or ""),
        }
    return out


# --------------------------------------------------------------------------
# decisions and bundles
# --------------------------------------------------------------------------


def save_decision(state: Any, decision: Decision, kb_id: str) -> None:
    _write(
        state,
        """INSERT INTO tuning_decisions
           (decision_id, experiment_id, kb_id, outcome, reason, winning_point_id,
            gates_passed, holdout_confirmed, control_regressed, created_at, payload_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (decision_id) DO NOTHING""",
        (
            decision.decision_id,
            decision.experiment_id,
            kb_id,
            decision.outcome,
            decision.reason,
            decision.winning_point_id,
            1 if decision.gates_passed else 0,
            1 if decision.holdout_confirmed else 0,
            1 if decision.control_regressed else 0,
            decision.created_at,
            _json(decision.as_dict()),
        ),
    )


def save_bundle(state: Any, bundle: TuningBundle) -> None:
    """Store a bundle as a *proposal*. Applying it is a separate act.

    Deliberately two steps. Producing a bundle is safe -- it is a file that
    describes a configuration -- and a batch can do it unattended. Changing
    what the fleet serves is not, and needs somebody or something to say so.
    """

    _write(
        state,
        """INSERT INTO tuning_bundles
           (bundle_id, kb_id, experiment_id, decision_id, snapshot_id, created_at,
            parameters_json, replaces_json, payload_json)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT (kb_id, bundle_id) DO NOTHING""",
        (
            bundle.bundle_id,
            bundle.kb_id,
            bundle.experiment_id,
            bundle.decision_id,
            bundle.snapshot_id,
            bundle.created_at,
            _json(bundle.parameters),
            _json(bundle.replaces),
            _json(bundle.as_dict()),
        ),
    )


def apply_bundle(state: Any, kb_id: str, bundle_id: str, *, applied_by: str) -> dict[str, Any]:
    """Make one bundle the region's active overlay.

    Supersedes the incumbent first and in the same call, so there is never a
    moment with two active rows: two replicas resolving different overlays
    would rank differently while both reported the fleet converged.

    Returns the applied bundle's payload, or raises ``KeyError`` if there is no
    such bundle -- an unknown id must be a refusal a caller can read, not a
    silent no-op that leaves ranking unchanged and reports success.
    """

    rows = state.rows(
        "SELECT payload_json, parameters_json FROM tuning_bundles "
        "WHERE kb_id = ? AND bundle_id = ?",
        (kb_id, bundle_id),
    )
    if not rows:
        raise KeyError(f"Unknown tuning bundle for {kb_id}: {bundle_id}")
    now = utc_now()
    payload = json.loads(rows[0]["payload_json"])
    payload.update(applied_at=now, applied_by=applied_by)
    from pheasant.artifacts.publications import publish

    with state.conn:
        if state.backend.dialect.is_postgres:
            state.conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(?))", ("pheasant:tuning:" + kb_id,)
            )
        else:
            # Acquire SQLite's writer slot before reading the incumbent.
            state.conn.execute(
                "UPDATE tuning_bundles SET applied_by=applied_by WHERE kb_id=? AND bundle_id=?",
                (kb_id, bundle_id),
            )
        previous = active_overlay(state, kb_id)
        state.conn.execute(
            "UPDATE tuning_bundles SET superseded_at=? "
            "WHERE kb_id=? AND applied_at IS NOT NULL AND superseded_at IS NULL",
            (now, kb_id),
        )
        state.conn.execute(
            "UPDATE tuning_bundles SET applied_at=?,applied_by=?,superseded_at=NULL,"
            "replaces_json=? "
            "WHERE kb_id=? AND bundle_id=?",
            (now, applied_by, _json((previous or {}).get("parameters") or {}), kb_id, bundle_id),
        )
        publish(
            state,
            kb_id,
            f"tuning:{bundle_id}",
            payload,
            receipt={
                "experiment_id": payload.get("experiment_id"),
                "decision_id": payload.get("decision_id"),
                "retrieval_eligible": False,
            },
            transaction=False,
        )
    return payload


def revert_bundle(
    state: Any, kb_id: str, *, applied_by: str, to: str = "base"
) -> dict[str, Any] | None:
    """Stand the active overlay down. Returns what was reverted, or ``None``.

    ``to`` is ``"base"`` (the default) or an earlier bundle id.

    **Base is the default, deliberately.** The configured parameters are the
    thing an operator can read in a file and reason about; an earlier bundle
    is a decision somebody made once and may not remember. A rollback that
    silently activated an older experiment's output would leave the region
    serving a configuration nobody chose twice over.

    But stepping back to a *named* earlier bundle is a real need — the last
    apply made things worse and the one before it was fine — and forcing that
    through "revert to base, then re-apply" loses the fact that it was a
    rollback. Naming a target records it as one.
    """

    active = active_overlay(state, kb_id)
    if not active:
        return None
    now = utc_now()
    _write(
        state,
        """UPDATE tuning_bundles SET superseded_at = ?, applied_by = ?
            WHERE kb_id = ? AND bundle_id = ? AND superseded_at IS NULL""",
        (now, applied_by, kb_id, active["bundle_id"]),
    )
    if to and to != "base":
        # Re-apply the named bundle. `apply_bundle` records what it replaced,
        # so the lineage reads as a step back rather than as a fresh apply
        # that happens to use old numbers.
        apply_bundle(state, kb_id, to, applied_by=f"{applied_by} (rollback)")
    return active


def lineage(state: Any, kb_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Every configuration this region has served, newest first.

    The answer to "what changed, when, and what were we serving before". Kept
    as history rather than derived from the active row, because the question
    people actually ask after a regression is about a *previous* state, and
    that is exactly the state the active row no longer holds.

    ``applied_at`` is second-resolution, so two applies inside one second tie
    — which is not exotic: it is what a rollback immediately after a bad apply
    looks like, and what a test does. Ties break on the active row first, then
    on the *later* supersession, which reconstructs the real order: the row
    still serving is newest, and among retired rows the one retired last was
    serving most recently.
    """

    rows = state.rows(
        """SELECT bundle_id, experiment_id, decision_id, created_at, applied_at,
                  applied_by, superseded_at, parameters_json, replaces_json
             FROM tuning_bundles
            WHERE kb_id = ? AND applied_at IS NOT NULL
            ORDER BY applied_at DESC,
                     CASE WHEN superseded_at IS NULL THEN 0 ELSE 1 END,
                     superseded_at DESC
            LIMIT ?""",
        (kb_id, int(limit)),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "bundle_id": str(row["bundle_id"]),
                "experiment_id": str(row["experiment_id"] or ""),
                "decision_id": str(row["decision_id"] or ""),
                "created_at": str(row["created_at"] or ""),
                "applied_at": str(row["applied_at"] or ""),
                "applied_by": str(row["applied_by"] or ""),
                "superseded_at": str(row["superseded_at"] or ""),
                "active": not row["superseded_at"],
                "parameters": json.loads(row["parameters_json"] or "{}"),
                # What this bundle displaced. An empty map means it replaced
                # the base configuration rather than another bundle.
                "replaced": json.loads(row["replaces_json"] or "{}"),
            }
        )
    return out


def active_overlay(state: Any, kb_id: str) -> dict[str, Any] | None:
    """The parameters this region is serving, or ``None`` for "the config".

    On the search path (through ``RankingResolver``), so it is one indexed
    single-row read and it must stay that way.
    """

    rows = state.rows(
        """SELECT bundle_id, parameters_json, applied_at, applied_by, experiment_id,
                  decision_id, replaces_json
             FROM tuning_bundles
            WHERE kb_id = ? AND applied_at IS NOT NULL AND superseded_at IS NULL
            ORDER BY applied_at DESC""",
        (kb_id,),
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "bundle_id": str(row["bundle_id"]),
        "parameters": json.loads(row["parameters_json"] or "{}"),
        "replaces": json.loads(row["replaces_json"] or "{}"),
        "applied_at": str(row["applied_at"] or ""),
        "applied_by": str(row["applied_by"] or ""),
        "experiment_id": str(row["experiment_id"] or ""),
        "decision_id": str(row["decision_id"] or ""),
    }


def load_bundle_row(state: Any, kb_id: str, bundle_id: str) -> dict[str, Any] | None:
    """One bundle by id, or ``None``. Used to refuse an unknown rollback target."""

    rows = state.rows(
        "SELECT payload_json FROM tuning_bundles WHERE kb_id = ? AND bundle_id = ?",
        (kb_id, bundle_id),
    )
    return json.loads(rows[0]["payload_json"]) if rows else None


def list_bundles(state: Any, kb_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = state.rows(
        "SELECT * FROM tuning_bundles WHERE kb_id = ? ORDER BY created_at DESC LIMIT ?",
        (kb_id, int(limit)),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload_json"] or "{}")
        payload["applied_at"] = str(row["applied_at"] or "")
        payload["applied_by"] = str(row["applied_by"] or "")
        payload["superseded_at"] = str(row["superseded_at"] or "")
        payload["active"] = bool(row["applied_at"] and not row["superseded_at"])
        out.append(payload)
    return out
