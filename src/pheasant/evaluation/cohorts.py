"""Query cohorts, and the leakage controls that make them mean anything.

A cohort is a set of queries evaluated together *for a stated purpose*, and the
purposes are not interchangeable. Two of them exist purely to stop a single
honest-looking mistake:

**Learned is not generalization.** Replaying the queries whose interactions
created a memory, and reporting the improvement as "the memory helped", measures
recall of the exact experience the memory was minted from. It will always look
excellent. The temporal holdout -- queries asked *after* the intervention
existed, which contributed none of its evidence -- is the one that can
disappoint, which is what makes it worth running. They are separate cohorts
with separate metrics and the report never merges them.

**Control is how you find out what you broke.** An alias rule that improves its
own queries and quietly re-ranks a hundred unrelated ones is a net loss that
every treatment-only metric will report as a win. The control cohort is queries
the intervention should not touch at all, and a change there is a regression by
definition rather than by threshold.

Beyond that: the **anchor** cohort is frozen and replayed at every snapshot, so
a trend line compares like with like; the **rolling** cohort is recent traffic,
which moves for two reasons at once (the region changed, and so did what people
ask) and therefore must never be reported alone; and the **synthetic invariant**
cohort is deterministic cases for ACL, validity, supersession and abstention --
system properties rather than user utility, which is why they are gates rather
than scores.

Every cohort here is derived from state the region already holds. Nothing asks
a human to label a corpus, which is the assumption the specification is written
against.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pheasant.evaluation.contracts import (
    Cohort,
    CohortPurpose,
    EvaluatedQuery,
    digest,
    normalize_query,
    utc_now,
)
from pheasant.evaluation.contracts import (
    query_id as make_query_id,
)
from pheasant.evaluation.proof import partition_token

logger = logging.getLogger(__name__)

#: Words a query is stripped to before a control cohort asks whether a steering
#: rule could fire on it. Shared with nothing on purpose: the retrieval path has
#: its own expansion rules, and a control definition that drifted with them
#: would silently start excluding the queries it exists to include.
_TOKENS = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {token for token in _TOKENS.findall(normalize_query(text)) if len(token) > 1}


def _ledger_queries(
    state: Any,
    kb_id: str,
    *,
    since: str | None = None,
    until: str | None = None,
    min_results: int = 0,
    limit: int = 5000,
) -> list[EvaluatedQuery]:
    """Distinct questions from the ledger, most-asked first.

    Distinct *questions*, not rows: the same query asked forty times is one
    cohort member carrying ``asked: 40``. That is both a smaller cohort and the
    right weight for usage-weighted aggregation -- and it stops one chatty
    session from defining an anchor.
    """

    clauses = ["kb_id=?", "query_text IS NOT NULL", "query_text <> ''", "result_count >= ?"]
    params: list[Any] = [kb_id, int(min_results)]
    if since:
        clauses.append("started_at >= ?")
        params.append(since)
    if until:
        clauses.append("started_at <= ?")
        params.append(until)
    try:
        rows = state.rows(
            "SELECT query_text, started_at, principal, session_id "
            f"FROM interaction_events WHERE {' AND '.join(clauses)} "
            "ORDER BY started_at, id LIMIT ?",
            (*params, int(limit)),
        )
    except Exception:  # noqa: BLE001 - a region with observation off has no cohort
        logger.debug("evaluation: interaction ledger unavailable", exc_info=True)
        return []

    merged: dict[str, EvaluatedQuery] = {}
    counts: dict[str, int] = {}
    for row in rows:
        text = str(row["query_text"]).strip()
        if not text:
            continue
        qid = make_query_id(text)
        counts[qid] = counts.get(qid, 0) + 1
        # First occurrence wins for `occurred_at`: a holdout cohort asks "was
        # this query asked after the intervention existed", and the honest
        # answer is when it was *first* asked, not when it was last repeated.
        if qid not in merged:
            merged[qid] = EvaluatedQuery(
                query_id=qid,
                text=text,
                occurred_at=str(row["started_at"]),
                principal_partition=partition_token(kb_id, row["principal"], row["session_id"]),
            )
    return sorted(
        (
            EvaluatedQuery(
                query_id=q.query_id,
                text=q.text,
                occurred_at=q.occurred_at,
                principal_partition=q.principal_partition,
                asked=counts[q.query_id],
            )
            for q in merged.values()
        ),
        key=lambda q: (-q.asked, q.query_id),
    )


def _cohort(
    kb_id: str,
    name: str,
    purpose: str,
    queries: list[EvaluatedQuery],
    *,
    frozen: bool = False,
    window_start: str | None = None,
    window_end: str | None = None,
    eligibility: dict[str, Any] | None = None,
) -> Cohort:
    ordered = tuple(sorted(queries, key=lambda q: q.query_id))
    eligibility_digest = digest(eligibility or {})
    # The id digests the *membership*, so a frozen anchor that has not changed
    # re-derives its own id and `save_cohort`'s DO NOTHING makes re-materializing
    # it free. A changed anchor gets a new id rather than silently replacing
    # the one every past trend point was measured against.
    cohort_id = "cohort-" + digest(
        kb_id, name, purpose, [q.query_id for q in ordered], eligibility_digest
    )
    return Cohort(
        cohort_id=cohort_id,
        kb_id=kb_id,
        name=name,
        purpose=purpose,
        queries=ordered,
        created_at=utc_now(),
        frozen=frozen,
        window_start=window_start,
        window_end=window_end,
        eligibility_digest=eligibility_digest,
    )


def build_anchor(
    state: Any,
    kb_id: str,
    *,
    name: str = "anchor",
    minimum_queries: int = 20,
    maximum_queries: int = 200,
    existing: Cohort | None = None,
) -> Cohort | None:
    """The frozen cohort every later snapshot is compared against.

    Returns ``existing`` unchanged when one is already frozen. That is the
    freeze: an anchor rebuilt from current traffic on every run is a rolling
    cohort wearing an anchor's name, and every trend it produces mixes "the
    region changed" with "the questions changed" -- which is precisely the
    confound the anchor exists to remove.

    ``None`` when the ledger cannot yet supply ``minimum_queries``: an anchor
    of four questions is not a baseline, and freezing one now means being stuck
    with it.
    """

    if existing is not None and existing.frozen:
        return existing
    queries = _ledger_queries(state, kb_id, min_results=1, limit=maximum_queries * 5)
    if len(queries) < minimum_queries:
        return None
    return _cohort(
        kb_id,
        name,
        CohortPurpose.ANCHOR.value,
        queries[:maximum_queries],
        frozen=True,
        eligibility={"min_results": 1, "max": maximum_queries, "order": "asked_desc"},
    )


def build_rolling(
    state: Any,
    kb_id: str,
    *,
    name: str = "rolling",
    lookback_days: int = 30,
    maximum_queries: int = 200,
    now: str | None = None,
) -> Cohort:
    """Recent traffic. Reported beside the anchor, never instead of it.

    A rolling delta moves for two reasons at once and cannot separate them.
    That does not make it useless -- it is the only cohort that notices the
    region has started being asked something new -- but it makes it a
    companion measurement rather than a headline one.
    """

    from datetime import datetime, timedelta

    end = now or utc_now()
    try:
        parsed = datetime.fromisoformat(end.replace("Z", "+00:00"))
        start = (parsed - timedelta(days=max(1, int(lookback_days)))).isoformat()
    except ValueError:
        start = None
    queries = _ledger_queries(state, kb_id, since=start, limit=maximum_queries * 5)
    return _cohort(
        kb_id,
        name,
        CohortPurpose.ROLLING.value,
        queries[:maximum_queries],
        window_start=start,
        window_end=end,
        eligibility={"lookback_days": lookback_days, "max": maximum_queries},
    )


def _candidate_queries(state: Any, kb_id: str, *, statuses: tuple[str, ...]) -> dict[str, str]:
    """``{query_id: created_at}`` for queries that produced memory candidates.

    The evidence trail formation already records: a candidate names the rows it
    was derived from, and each of those names a query. That is what makes a
    learned cohort derivable rather than guessed.
    """

    out: dict[str, str] = {}
    for status in statuses:
        try:
            candidates = state.list_memory_candidates(status=status, limit=1000)
        except Exception:  # noqa: BLE001 - formation is optional
            continue
        for candidate in candidates:
            try:
                evidence = json.loads(candidate.get("evidence_json") or "{}")
            except (TypeError, ValueError):
                continue
            texts: list[str] = []
            if evidence.get("query"):
                texts.append(str(evidence["query"]))
            for item in evidence.get("queries") or []:
                texts.append(str(item))
            first_seen = str(candidate.get("first_seen") or "")
            for text in texts:
                qid = make_query_id(text)
                # Earliest wins: an intervention's creation time is when the
                # first evidence for it appeared, and a holdout drawn from a
                # later timestamp would admit queries that helped create it.
                if qid not in out or (first_seen and first_seen < out[qid]):
                    out[qid] = first_seen
    return out


def build_learned(
    state: Any,
    kb_id: str,
    *,
    name: str = "learned",
    maximum_queries: int = 200,
) -> Cohort:
    """Queries whose own interactions created or reinforced the memory.

    Labelled ``learned`` everywhere it appears, and the report says "recall of
    learned experience" rather than "gain". The distinction is not pedantry: a
    system that only ever measured this would promote memorization and call it
    learning, which is the specific self-rewarding loop the promotion gates
    exist to prevent.
    """

    creating = _candidate_queries(state, kb_id, statuses=("admitted", "pending"))
    queries = [q for q in _ledger_queries(state, kb_id, limit=5000) if q.query_id in creating]
    return _cohort(
        kb_id,
        name,
        CohortPurpose.LEARNED.value,
        queries[:maximum_queries],
        eligibility={"source": "memory_candidate_evidence"},
    )


def build_temporal_holdout(
    state: Any,
    kb_id: str,
    *,
    name: str = "temporal_holdout",
    minimum_separation_days: float = 0.0,
    maximum_queries: int = 200,
) -> Cohort:
    """Queries asked after the interventions existed, and independent of them.

    Two exclusions, both load-bearing. A query that contributed evidence to any
    candidate is out regardless of when it was asked -- being asked again later
    does not make it independent. And a query first asked before the earliest
    intervention is out, because the region's answer to it was not a forward
    prediction.

    ``minimum_separation_days`` widens the second exclusion for deployments
    that want a settling period. Zero by default: how long a holdout must
    remain independent is an open policy decision the specification flags, and
    a hidden non-zero default would be this package answering it silently.
    """

    creating = _candidate_queries(state, kb_id, statuses=("admitted", "pending"))
    if not creating:
        return _cohort(kb_id, name, CohortPurpose.TEMPORAL_HOLDOUT.value, [])
    stamps = [value for value in creating.values() if value]
    earliest = min(stamps) if stamps else ""
    cutoff = earliest
    if earliest and minimum_separation_days > 0:
        from datetime import datetime, timedelta

        try:
            cutoff = (
                datetime.fromisoformat(earliest.replace("Z", "+00:00"))
                + timedelta(days=float(minimum_separation_days))
            ).isoformat()
        except ValueError:
            cutoff = earliest

    eligible = [
        q
        for q in _ledger_queries(state, kb_id, limit=5000)
        if q.query_id not in creating and q.occurred_at and (not cutoff or q.occurred_at > cutoff)
    ]
    return _cohort(
        kb_id,
        name,
        CohortPurpose.TEMPORAL_HOLDOUT.value,
        eligible[:maximum_queries],
        window_start=cutoff or None,
        eligibility={
            "excludes": "queries contributing intervention evidence",
            "minimum_separation_days": minimum_separation_days,
        },
    )


def build_control(
    state: Any,
    kb_id: str,
    *,
    name: str = "control",
    maximum_queries: int = 200,
) -> Cohort:
    """Queries no steering rule can fire on.

    "Should have no effect" is made deterministic rather than intuitive: a
    query is a control when none of its tokens triggers any alias or preference
    rule, and no exclusion path fragment appears in it. Derived from the live
    steering records rather than a hand-written list, so a rule added tomorrow
    shrinks the control set tomorrow instead of quietly invalidating it.

    **The cohort is about steering, so what it controls must be steering too.**
    Two earlier attempts got this wrong from the other end. Comparing the
    full-memory variant against the corpus baseline here counted a memory
    record legitimately answering a control query as an unintended regression
    -- it was the treatment doing its job on a query this cohort had wrongly
    called untouched. Widening the cohort to exclude anything memory could
    answer then emptied it outright, because a formed *session digest* quotes
    the very queries the cohort is drawn from and is retrievable for all of
    them.

    Both failures came from pairing a steering-defined cohort with a treatment
    that also changes memory *content*. The fix is on the metric's side, not
    here: :func:`pheasant.evaluation.runner` measures control regression
    between ``B1`` (memory content, no steering) and ``B5`` (memory content
    plus every steering kind), which differ in steering alone. On a query no
    rule can fire on, those two must be identical -- and any difference is
    unintended by construction.
    """

    triggers: set[str] = set()
    try:
        from pheasant.artifacts.steering import load_steering_records, parse_rule

        for record in load_steering_records(state):
            parsed = parse_rule(str(record.get("kind") or ""), str(record.get("text") or ""))
            if parsed is None:
                continue
            name_, value = parsed
            if name_ == "alias":
                term, values = value
                triggers |= _tokens(term)
                for item in values:
                    triggers |= _tokens(item)
            elif name_ == "preference":
                trigger_terms, paths = value
                for item in (*trigger_terms, *paths):
                    triggers |= _tokens(item)
            else:
                for item in value:
                    triggers |= _tokens(item)
    except Exception:  # noqa: BLE001 - no steering means every query is a control
        logger.debug("evaluation: steering records unavailable", exc_info=True)

    queries = [
        q for q in _ledger_queries(state, kb_id, limit=5000) if not (_tokens(q.text) & triggers)
    ]
    return _cohort(
        kb_id,
        name,
        CohortPurpose.CONTROL.value,
        queries[:maximum_queries],
        eligibility={"rule": "no steering trigger token present", "triggers": len(triggers)},
    )


def build_synthetic_invariants(state: Any, kb_id: str, *, name: str = "invariants") -> Cohort:
    """Deterministic cases for the properties that are gates, not scores.

    Each case carries an ``expectation`` the gate evaluator checks rather than
    a relevance judgment, because these assert system behaviour: a superseded
    record must not be returned under ``current_only``; the same query under
    ``as_of`` must bring it back; a scoped record must not reach a principal
    who did not write it; a query about something the corpus has never
    contained must return nothing.

    Built from the region's own memory records, so the cases describe *this*
    region. A fixed case list would pass everywhere and mean nothing anywhere.
    """

    cases: list[EvaluatedQuery] = []
    try:
        # The superseded record's own `valid_from` comes back with the pair,
        # because the as-of case needs an instant at which the *old* record was
        # valid -- see the comment on the temporal case below.
        rows = state.rows(
            "SELECT new.record_id AS record_id, new.valid_from AS corrected_at, "
            "old.record_id AS superseded_id, old.valid_from AS superseded_from "
            "FROM memory_records new JOIN memory_records old ON old.record_id = new.supersedes "
            "WHERE new.supersedes IS NOT NULL AND new.supersedes <> '' "
            "ORDER BY new.record_id LIMIT 25"
        )
    except Exception:  # noqa: BLE001 - a region without memory has no invariants
        rows = []
    for row in rows:
        text = _record_text(state, str(row["record_id"]))
        if not text:
            continue
        cases.append(
            EvaluatedQuery(
                query_id=make_query_id(f"invariant:stale:{row['record_id']}"),
                text=text,
                occurred_at=str(row["corrected_at"] or ""),
                expectation={
                    "kind": "stale_current",
                    "forbidden_record_id": str(row["superseded_id"]),
                    "expected_record_id": str(row["record_id"]),
                },
            )
        )
        # The old record's validity ends *at* the correction's instant --
        # `effective_valid_until` takes the correction time, and `admits`
        # excludes on `valid_until <= instant`. So an as-of set to the
        # correction's own timestamp is the one instant at which the old record
        # is already gone, and a case built there would assert a failure the
        # validity model is right about. The old record's own `valid_from` is
        # the deterministic instant at which it certainly *was* current.
        superseded_from = str(row["superseded_from"] or "")
        corrected_at = str(row["corrected_at"] or "")
        # A record corrected within the same second it was asserted has an
        # **empty validity window**: `valid_from == valid_until`, so there is no
        # instant at which `as_of` could return it, and a case built here would
        # assert a failure against arithmetic rather than against behaviour.
        # Found on a live run, where a scripted write-then-correct produced
        # exactly that and failed the gate on a region behaving correctly.
        if not superseded_from or not corrected_at or superseded_from >= corrected_at:
            continue
        cases.append(
            EvaluatedQuery(
                query_id=make_query_id(f"invariant:temporal:{row['record_id']}"),
                text=text,
                occurred_at=superseded_from,
                expectation={
                    "kind": "temporal_as_of",
                    "as_of": superseded_from,
                    "expected_record_id": str(row["superseded_id"]),
                },
            )
        )

    try:
        scoped = state.rows(
            "SELECT record_id, scope, written_by FROM memory_records "
            "WHERE scope IN ('session','user') AND written_by IS NOT NULL AND written_by <> '' "
            "ORDER BY record_id LIMIT 25"
        )
    except Exception:  # noqa: BLE001
        scoped = []
    for row in scoped:
        text = _record_text(state, str(row["record_id"]))
        if not text:
            continue
        cases.append(
            EvaluatedQuery(
                query_id=make_query_id(f"invariant:acl:{row['record_id']}"),
                text=text,
                expectation={
                    "kind": "acl_isolation",
                    "forbidden_record_id": str(row["record_id"]),
                    # A principal that provably did not write it. Deterministic
                    # and impossible to collide with a real one.
                    "principal": f"evaluation-probe-{digest(row['written_by'])}",
                },
            )
        )

    # Abstention: a token sequence the corpus cannot contain, derived from the
    # kb id so it is stable per region and cannot accidentally match anything.
    cases.append(
        EvaluatedQuery(
            query_id=make_query_id(f"invariant:abstain:{kb_id}"),
            text=f"zzq{digest(kb_id, 'abstain')}xq unknowable subject",
            expectation={"kind": "abstention", "expected_results": 0},
        )
    )
    return _cohort(
        kb_id,
        name,
        CohortPurpose.SYNTHETIC.value,
        cases,
        eligibility={"derived_from": "memory_records", "version": 1},
    )


def _record_text(state: Any, record_id: str) -> str:
    """The record's own indexed text, or "".

    Read from the chunk the ordinary pipeline produced rather than from the
    file: what the evaluation replays has to be what retrieval can actually
    match, and a record whose file exists but whose chunk does not is exactly
    the state an invariant case would silently pass in.
    """

    try:
        rows = state.rows(
            "SELECT c.text AS text FROM chunks c "
            "JOIN memory_records m ON m.artifact_id = c.artifact_id "
            "WHERE m.record_id=? ORDER BY c.chunk_index LIMIT 1",
            (record_id,),
        )
    except Exception:  # noqa: BLE001
        return ""
    if not rows:
        return ""
    text = " ".join(str(rows[0]["text"] or "").split())
    return text[:200]
