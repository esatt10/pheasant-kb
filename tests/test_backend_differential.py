"""The two scaling primitives, replayed identically on both backends.

`tests/test_backend_parity.py` proves the two backends produce the same
*knowledge base* — same ids, same counts, comparable ranking. What it does not
touch is the pair of primitives the fleet is actually built on:

* the **per-source lease** (`sync/locks.py`), which is what lets more than one
  indexer exist without two of them writing one source, and
* the **durable index queue** (`sync/queue.py`), which is what lets a backlog
  survive a restart and a depth be scaled on.

Both are claim protocols. Both turn on the exact things the two dialects
disagree about — `ON CONFLICT … RETURNING`, `UPDATE … RETURNING`, a
conditional `WHERE` re-evaluated under READ COMMITTED, and a `cursor.rowcount`
that one backend used to discard. Every one of those has already produced a
bug here, and two of them produced the *quiet* direction: correct on Postgres,
wrong on SQLite, where the offline suite runs.

So this is a differential test rather than another pair of assertions. A
seeded generator produces one sequence of operations; the sequence is replayed
against both backends; every observable outcome — who won a claim, what a
depth read said, what came back from a queue claim — is recorded in order and
the two transcripts are diffed. A divergence names the operation index it
happened at, which is the thing a reader needs and an equality assertion over
final state cannot give.

Seeded rather than random-per-run: a failure has to be reproducible from the
report alone (pillar 3), and a test that fails on a different sequence each
time is a test nobody can act on. Widening coverage is a matter of adding
seeds to `SEEDS`, which is cheap and explicit.
"""

from __future__ import annotations

import os
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.persistence.paths import StatePaths
from pheasant.persistence.state_store import StateStore
from pheasant.sync.locks import SourceLease, release_stale_lease
from pheasant.sync.queue import DEAD, DONE, INFLIGHT, PENDING, IndexTask, LocalQueue

DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()

postgres = pytest.mark.skipif(
    not DSN,
    reason="set PHEASANT_TEST_POSTGRES_DSN to a throwaway database to run the differential",
)

#: Each seed is one generated operation sequence. More seeds is more coverage
#: at no maintenance cost; each is deterministic, so a failure reproduces.
SEEDS = (1, 7, 13, 42, 99)

#: Long enough that a generated sequence exercises re-claims, takeovers,
#: retries and dead-lettering rather than just the happy path.
OPERATIONS = 60

OWNERS = ("indexer-a", "indexer-b", "indexer-c")
SOURCES = ("docs", "code")


def _store(root: Path, backend: str) -> StateStore:
    data: dict[str, Any] = {
        "pheasant": {
            "name": "differential",
            "state_path": str(root / f"state-{backend}"),
            "workspace_root": str(root),
            "exports_path": str(root / "exports"),
        },
        "sources": [],
    }
    if backend == "postgres":
        data["storage"] = {"backend": "postgres", "dsn_env": "PHEASANT_TEST_POSTGRES_DSN"}
    config = PheasantConfig.model_validate(data)
    paths = StatePaths.from_config(config)
    paths.ensure()
    store = StateStore.from_config(config, paths.sqlite)
    store.migrate()
    return store


def _reset(dsn: str) -> None:
    from pheasant.persistence import pg_driver

    with pg_driver.connect(dsn, autocommit=True) as conn:
        conn.query("DROP SCHEMA public CASCADE")
        conn.query("CREATE SCHEMA public")


# ---------------------------------------------------------------------------
# The generated programs
# ---------------------------------------------------------------------------


def _lease_program(seed: int) -> list[tuple[str, ...]]:
    """A sequence of lease operations. Data, not calls — so both backends run
    exactly the same one and the sequence can be printed on a failure."""

    rng = random.Random(seed)
    program: list[tuple[str, ...]] = []
    for _ in range(OPERATIONS):
        verb = rng.choice(
            # Weighted toward claiming, because contention is the interesting
            # part; `expire` is what exercises the staleness clause that a
            # dialect could get wrong without any test noticing.
            ["acquire", "acquire", "acquire", "release", "expire", "read"]
        )
        program.append((verb, rng.choice(SOURCES), rng.choice(OWNERS)))
    return program


def _queue_program(seed: int) -> list[tuple[str, ...]]:
    rng = random.Random(seed + 1000)
    program: list[tuple[str, ...]] = []
    for _ in range(OPERATIONS):
        verb = rng.choice(
            ["publish", "publish", "claim", "claim", "ack", "nack", "depth", "requeue_dead"]
        )
        program.append((verb, rng.choice(SOURCES), rng.choice(OWNERS)))
    return program


# ---------------------------------------------------------------------------
# The interpreters. Each returns a transcript: one line per operation.
# ---------------------------------------------------------------------------


def _run_lease_program(store: StateStore, program: list[tuple[str, ...]]) -> list[str]:
    transcript: list[str] = []
    leases: dict[tuple[str, str], SourceLease] = {}

    def lease_for(source: str, owner: str) -> SourceLease:
        key = (source, owner)
        if key not in leases:
            # No heartbeat thread: a background writer would make the
            # transcript depend on timing, and this is a test of the SQL.
            leases[key] = SourceLease(store, source, owner=owner, heartbeat_interval_s=10_000)
        return leases[key]

    for index, (verb, source, owner) in enumerate(program):
        if verb == "acquire":
            won = lease_for(source, owner).try_acquire()
            transcript.append(f"{index}: acquire {source} by {owner} -> {won}")
        elif verb == "release":
            lease = leases.get((source, owner))
            if lease is None:
                transcript.append(f"{index}: release {source} by {owner} -> not-held")
            else:
                lease.release()
                transcript.append(f"{index}: release {source} by {owner} -> released")
        elif verb == "expire":
            # Age every heartbeat past the staleness window, the way a killed
            # container does — the clause the takeover path turns on, and one
            # an ISO-8601 string comparison has to get right on both engines.
            stale = (datetime.now(UTC) + timedelta(days=1)).isoformat()
            freed = release_stale_lease(store, source, stale_before=stale)
            transcript.append(f"{index}: expire {source} -> {freed}")
        else:
            rows = store.rows("SELECT owner FROM source_leases WHERE source_id=?", (source,))
            holder = str(rows[0]["owner"]) if rows else None
            transcript.append(f"{index}: read {source} -> {holder}")
    for lease in leases.values():
        lease.release()
    return transcript


def _run_queue_program(store: StateStore, program: list[tuple[str, ...]]) -> list[str]:
    queue = LocalQueue(store)
    transcript: list[str] = []
    claimed: dict[str, IndexTask] = {}

    for index, (verb, source, owner) in enumerate(program):
        if verb == "publish":
            task = queue.publish(IndexTask(id=f"task:{source}", source_id=source))
            transcript.append(f"{index}: publish {source} -> {task.id}")
        elif verb == "claim":
            task = queue.claim(owner, visibility_seconds=300)
            if task is None:
                transcript.append(f"{index}: claim by {owner} -> nothing")
            else:
                claimed[owner] = task
                transcript.append(
                    f"{index}: claim by {owner} -> {task.id} attempts={task.attempts}"
                )
        elif verb == "ack":
            task = claimed.pop(owner, None)
            if task is None:
                transcript.append(f"{index}: ack by {owner} -> nothing held")
            else:
                queue.ack(task)
                transcript.append(f"{index}: ack by {owner} -> {task.id}")
        elif verb == "nack":
            task = claimed.pop(owner, None)
            if task is None:
                transcript.append(f"{index}: nack by {owner} -> nothing held")
            else:
                # `retry_in_seconds=0` so the retry is immediately visible:
                # the point is the state transition, not the backoff.
                queue.nack(task, "generated failure", retry_in_seconds=0)
                transcript.append(f"{index}: nack by {owner} -> {task.id}")
        elif verb == "requeue_dead":
            transcript.append(f"{index}: requeue_dead -> {queue.requeue_dead()}")
        else:
            depth = queue.depth()
            counts = " ".join(
                f"{status}={depth.get(status, 0)}" for status in (PENDING, INFLIGHT, DONE, DEAD)
            )
            transcript.append(f"{index}: depth -> {counts}")
    return transcript


def _diff(sqlite: list[str], postgres_transcript: list[str]) -> str:
    """The first divergence, with its neighbourhood.

    A whole-transcript equality failure prints 120 lines and says nothing; the
    operation *index* where two backends first disagreed is the entire
    diagnosis.
    """

    for index, (left, right) in enumerate(zip(sqlite, postgres_transcript, strict=False)):
        if left != right:
            window = slice(max(0, index - 3), index + 1)
            return (
                f"backends diverged at operation {index}:\n"
                f"  sqlite:   {left}\n"
                f"  postgres: {right}\n"
                "context (sqlite):\n    " + "\n    ".join(sqlite[window])
            )
    if len(sqlite) != len(postgres_transcript):
        return (
            f"transcript lengths differ: sqlite={len(sqlite)} postgres={len(postgres_transcript)}"
        )
    return ""


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


@postgres
@pytest.mark.parametrize("seed", SEEDS)
def test_the_lease_protocol_is_identical_on_both_backends(tmp_path: Path, seed: int) -> None:
    """Who holds a source must not depend on which database is under it.

    The lease is the whole of the multi-indexer story: `try_acquire` is one
    `INSERT … ON CONFLICT DO UPDATE … WHERE … RETURNING`, and every clause in
    it is dialect-sensitive. A divergence here does not look like a bug — it
    looks like two indexers indexing one source.
    """

    _reset(DSN)
    program = _lease_program(seed)
    sqlite_store, postgres_store = _store(tmp_path, "sqlite"), _store(tmp_path, "postgres")
    try:
        left = _run_lease_program(sqlite_store, program)
        right = _run_lease_program(postgres_store, program)
    finally:
        sqlite_store.close()
        postgres_store.close()

    difference = _diff(left, right)
    assert not difference, difference


@postgres
@pytest.mark.parametrize("seed", SEEDS)
def test_the_queue_protocol_is_identical_on_both_backends(tmp_path: Path, seed: int) -> None:
    """Claim, ack, nack, dead-letter and depth, in the same order, twice.

    `claim` is an `UPDATE … RETURNING` whose outer `WHERE` has to be a
    predicate the winner's own write falsifies — the READ COMMITTED trap this
    codebase has already fallen into once. `depth` is what an autoscaler
    reads, so a divergence there is a fleet that scales differently on two
    backends while both report success.
    """

    _reset(DSN)
    program = _queue_program(seed)
    sqlite_store, postgres_store = _store(tmp_path, "sqlite"), _store(tmp_path, "postgres")
    try:
        left = _run_queue_program(sqlite_store, program)
        right = _run_queue_program(postgres_store, program)
    finally:
        sqlite_store.close()
        postgres_store.close()

    difference = _diff(left, right)
    assert not difference, difference


def test_the_generated_programs_are_deterministic() -> None:
    """Runs offline, and is the reason a failure above is actionable.

    A generator that produced a different sequence per run would report a
    divergence nobody could reproduce — which is worse than no test, because
    it would be quarantined rather than fixed.
    """

    for seed in SEEDS:
        assert _lease_program(seed) == _lease_program(seed)
        assert _queue_program(seed) == _queue_program(seed)
    # And the seeds are genuinely different programs, not one repeated.
    assert len({tuple(_lease_program(seed)) for seed in SEEDS}) == len(SEEDS)


def test_the_programs_exercise_more_than_the_happy_path() -> None:
    """A generated sequence that only ever publishes and acks would pass on
    any pair of backends, including two broken ones."""

    verbs = {verb for seed in SEEDS for verb, *_ in _queue_program(seed)}
    assert {"publish", "claim", "ack", "nack", "requeue_dead", "depth"} <= verbs

    lease_verbs = {verb for seed in SEEDS for verb, *_ in _lease_program(seed)}
    assert {"acquire", "release", "expire", "read"} <= lease_verbs

    # Contention is the point: the same source must be claimed by more than
    # one owner somewhere in the corpus of programs.
    contended = False
    for seed in SEEDS:
        by_source: dict[str, set[str]] = {}
        for verb, source, owner in _lease_program(seed):
            if verb == "acquire":
                by_source.setdefault(source, set()).add(owner)
        contended = contended or any(len(owners) > 1 for owners in by_source.values())
    assert contended, "no generated program ever has two owners contend for one source"
