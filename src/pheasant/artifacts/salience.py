"""How much a memory has earned its place (Step 33.9, extended Phase 1).

Everything else about a record is derivable from its file. Salience is not: it
is the one thing memory learns from *being used*, and it answers the question a
growing store eventually forces — when there are more memories than anyone can
carry, which ones go?

Deterministic by construction. A documented formula over recorded inputs, no
model and no sampling, so the same store always ranks the same way and a
pruning pass is reproducible rather than a judgement call.

Four inputs, each earning its place:

* **recency** — how long ago the record was last *seen* (Phase 1: reinforced
  by an exact or near-duplicate write, or retrieved with `usage_tracking` on)
  — or, absent either, when it was asserted. Memory is a claim about the
  world and the world moves; an old, unconfirmed note is likelier to be
  wrong. Anchoring on `last_seen` rather than `asserted_at` alone is what
  stops a fact re-observed thousands of times from decaying as though it
  were only ever said once, on day one.
* **use** — how often retrieval actually returned it. The cheapest available
  evidence that a record answers real questions rather than merely existing.
* **observations** (Phase 1) — how often a write re-asserted it, exactly or
  as a paraphrase, instead of creating a new file. Distinct from `use`: this
  is *assertion* frequency, not *retrieval* frequency, and it is on by
  default (`memory.reinforcement_enabled`) where `use` is not
  (`memory.usage_tracking`), so a store with tracking off still has a real
  importance signal rather than the pure age-and-scope ordering it fell
  back to before Phase 1.
* **scope** — an `org` fact was written for everyone and a `session` note for
  one conversation, so they should not compete on equal terms for the last
  slot in a bounded store.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

#: Days after which recency has decayed by half. A quarter: long enough that a
#: fact asserted last month is still strong, short enough that a year-old note
#: loses to a fresh one.
HALF_LIFE_DAYS = 90.0

#: Diminishing returns on use. The gap between "never returned" and "returned
#: once" is the informative one; between 40 and 41 it is noise, and a linear
#: count would let one hot record dominate a whole store.
USE_WEIGHT = 0.5

#: Same diminishing-returns shape as USE_WEIGHT, for `observations` (Phase
#: 1). Kept as a separate constant rather than reusing USE_WEIGHT: the two
#: are different kinds of evidence (asserted-again vs. retrieved-again) and
#: nothing requires them to be weighted identically, even though they start
#: out equal.
OBSERVATION_WEIGHT = 0.5

#: What a scope is worth. `org` is a shared assertion, `session` is scratch.
SCOPE_WEIGHT = {"org": 1.25, "user": 1.0, "session": 0.6}


def _age_days(instant: str, now: datetime) -> float:
    if not instant:
        return 0.0
    try:
        moment = datetime.fromisoformat(str(instant).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (now - moment).total_seconds() / 86400.0)


def salience(record: Any, *, now: datetime | None = None) -> float:
    """Score a record. Higher is more worth keeping.

    `record` is a `memory_records` row (or anything with the same keys).
    """
    instant = (now or datetime.now(tz=UTC)).astimezone(UTC)
    get = record.get if hasattr(record, "get") else lambda key, default=None: default

    # Phase 1: recency anchors on the most recent of the three instants a
    # record can carry — asserted, reinforced (`last_seen`), retrieved
    # (`last_used_at`, only with usage_tracking on) — not "reinforcement
    # always wins": a record retrieved yesterday but reinforced last month
    # should still read as fresh. ISO-8601 in Z form sorts lexicographically,
    # the same property `admits`/`sql_predicate` already rely on. A record
    # untouched since it was written falls back to `asserted_at` alone,
    # exactly today's behavior.
    instants = [
        str(value) for value in (get("asserted_at"), get("last_seen"), get("last_used_at")) if value
    ]
    age = _age_days(max(instants) if instants else "", instant)
    recency = math.pow(0.5, age / HALF_LIFE_DAYS)

    uses = max(0, int(get("uses") or 0))
    observations = max(0, int(get("observations") or 0))
    # log1p, not the raw counts: see USE_WEIGHT / OBSERVATION_WEIGHT.
    evidence = 1.0 + USE_WEIGHT * math.log1p(uses) + OBSERVATION_WEIGHT * math.log1p(observations)

    weight = SCOPE_WEIGHT.get(str(get("scope") or ""), 1.0)
    return round(recency * evidence * weight, 6)


def rank(records: list[Any], *, now: datetime | None = None) -> list[Any]:
    """Records most worth keeping first.

    Ties break on `record_id` so the order is total and a pruning pass over an
    unchanged store always removes the same rows.
    """
    instant = (now or datetime.now(tz=UTC)).astimezone(UTC)
    return sorted(
        records,
        key=lambda record: (
            -salience(record, now=instant),
            str(record.get("record_id") if hasattr(record, "get") else ""),
        ),
    )


def over_capacity(records: list[Any], max_records: int | None, *, now: datetime | None = None):
    """The least salient records beyond `max_records`, or `[]`.

    Returns what to archive, and nothing else decides it: `None` means
    unbounded, which is the pre-33.9 behavior and the default.
    """
    if not max_records or max_records <= 0 or len(records) <= max_records:
        return []
    return rank(records, now=now)[max_records:]


def _field(record: Any, key: str) -> str:
    get = record.get if hasattr(record, "get") else lambda _key, default=None: default
    return str(get(key) or "")


def over_scope_capacity(
    records: list[Any], caps: dict[str, int | None], *, now: datetime | None = None
) -> list[Any]:
    """The least salient records beyond each scope's own cap (Phase 5).

    `over_capacity` alone ranks the whole store as one queue, so with the
    default `SCOPE_WEIGHT` a session flood only ever *outranks* org facts by
    a fixed multiplier — it never fully isolates them, and a big enough flood
    still eventually crowds an org fact out of a shared `max_records` budget.
    Grouping by scope first, then capping each group independently, is what
    actually isolates the pools. Scopes absent from `caps`, or mapped to a
    falsy cap, are left unbounded — same convention as `over_capacity`.
    """
    if not caps or not any(caps.values()):
        return []
    by_scope: dict[str, list[Any]] = {}
    for record in records:
        by_scope.setdefault(_field(record, "scope"), []).append(record)
    doomed: list[Any] = []
    for scope in sorted(by_scope):
        doomed.extend(over_capacity(by_scope[scope], caps.get(scope), now=now))
    return doomed


def over_subject_capacity(
    records: list[Any], max_per_subject: int | None, *, now: datetime | None = None
) -> list[Any]:
    """The least salient records beyond `max_per_subject` within one subject
    (Phase 5), across scopes. Records with no `subject` are exempt — grouping
    every untagged record together would cap "everything untagged" as if it
    were one entity, which is not what a per-subject cap means.
    """
    if not max_per_subject or max_per_subject <= 0:
        return []
    by_subject: dict[str, list[Any]] = {}
    for record in records:
        subject = _field(record, "subject")
        if not subject:
            continue
        by_subject.setdefault(subject, []).append(record)
    doomed: list[Any] = []
    for subject in sorted(by_subject):
        doomed.extend(over_capacity(by_subject[subject], max_per_subject, now=now))
    return doomed
