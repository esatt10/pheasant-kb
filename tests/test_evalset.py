"""Building an evaluation case set from real traffic, without exporting people.

`analytics.py` excludes `source_audit_events` and `idp_groups` from Parquet
exports on principle: *"who a principal is, which groups they are in, and what
they did. An export is a file people pass around; identity and audit data is
not that."* An interaction ledger is exactly that category, so the interesting
assertions here are about what does **not** leave.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pheasant.evalset import EVALSET_FORMAT_VERSION, bootstrap, build_cases, write_cases
from pheasant.persistence.backends import PostgresBackend
from pheasant.persistence.state_store import StateStore
from pheasant.sync.log_queue import write_events
from pheasant.telemetry.interactions import InteractionEvent


def _state(tmp_path: Path, dsn: str | None = None) -> StateStore:
    store = StateStore(backend=PostgresBackend(dsn)) if dsn else StateStore()
    store.migrate()
    return store


def _ask(
    state: Any,
    query: str,
    *,
    index: int,
    session: str = "sess-1",
    principal: str | None = "user:ada",
    hits: int = 1,
) -> None:
    write_events(
        state,
        [
            InteractionEvent(
                kb_id="kb",
                operation="/search",
                modality="ui",
                principal=principal,
                session_id=session,
                trace_id=f"{index:032x}",
                span_id=f"{index:016x}",
                started_at=f"2026-01-01T00:00:{index:02d}.000000Z",
                status="ok",
                query_text=query,
                result_ids=[f"file:docs:{n}.md:branch=none" for n in range(hits)],
                result_paths=[f"{n}.md" for n in range(hits)],
                result_count=hits,
                top_score=0.8 if hits else None,
            )
        ],
    )


def test_a_repeated_question_is_one_case_weighted_by_how_often_it_was_asked(
    tmp_path: Path,
) -> None:
    """Distinct questions, not rows: the same query asked forty times is one
    case with `asked: 40` -- a smaller file and a better weight for a harness
    that wants what matters rather than what repeated."""

    state = _state(tmp_path)
    for index in range(5):
        _ask(state, "where is the watcher", index=index)
    _ask(state, "who owns billing", index=10)

    cases = build_cases(state)

    assert [case.query for case in cases] == ["where is the watcher", "who owns billing"]
    assert [case.asked for case in cases] == [5, 1]


def test_identity_leaves_as_a_pseudonym_never_as_itself(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _ask(state, "a question", index=1, session="sess-secret", principal="user:ada")

    case = build_cases(state)[0]

    assert case.principal not in (None, "user:ada")
    assert case.session not in ("", "sess-secret")
    # Opaque and fixed-width: a token, not a redaction that leaks its length.
    assert len(case.session) == 16
    assert len(case.principal) == 16


def test_two_exports_cannot_be_joined_to_re_identify_anyone(
    tmp_path: Path, fresh_database: Callable[[], str]
) -> None:
    """The salt is per-export and random, so intersecting two files gives an
    attacker nothing -- which is the difference between a pseudonym and a
    stable identifier wearing a hash."""

    state = _state(tmp_path)
    _ask(state, "a question", index=1, principal="user:ada")

    first = build_cases(state)[0]
    second = build_cases(state)[0]

    assert first.principal != second.principal
    assert first.session != second.session
    # But stable *within* one export, which is the property an eval needs.
    state2 = _state(tmp_path / "two", fresh_database())
    _ask(state2, "one", index=1, session="s", principal="p")
    _ask(state2, "two", index=2, session="s", principal="p")
    one, two = build_cases(state2, salt="fixed")
    assert one.session == two.session


def test_nothing_else_identifying_leaves(tmp_path: Path) -> None:
    """No trace ids, no client ids, no timings -- a case is a question and what
    came back, and nothing that answers "who, from where, when"."""

    state = _state(tmp_path)
    _ask(state, "a question", index=1)

    payload = json.dumps([case.__dict__ for case in build_cases(state)])

    for leaked in ("trace_id", "span_id", "client_id", "started_at", "duration_ms"):
        assert leaked not in payload


def test_a_case_carries_what_the_region_actually_returned(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _ask(state, "where is the watcher", index=1, hits=3)

    case = build_cases(state)[0]

    assert case.expected_ids == [f"file:docs:{n}.md:branch=none" for n in range(3)]
    assert case.expected_paths == ["0.md", "1.md", "2.md"]
    assert case.result_count == 3
    assert case.top_score == 0.8


def test_a_question_that_found_nothing_is_kept_and_counted(tmp_path: Path) -> None:
    """The most useful case in the set: it is the one the region fails."""

    state = _state(tmp_path)
    _ask(state, "answered", index=1, hits=1)
    _ask(state, "unanswered", index=2, hits=0)

    report = write_cases(build_cases(state), tmp_path / "cases.json")

    assert report["cases"] == 2
    assert report["unanswered"] == 1


def test_the_file_says_what_it_is(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _ask(state, "a question", index=1)

    bootstrap(state, tmp_path / "eval" / "cases.json")
    payload = json.loads((tmp_path / "eval" / "cases.json").read_text(encoding="utf-8"))

    assert payload["format_version"] == EVALSET_FORMAT_VERSION
    assert payload["generated_at"].endswith("Z")
    assert "pseudonyms" in payload["note"]
    assert payload["counts"]["cases"] == 1


def test_a_promoted_gap_becomes_an_answer_key(tmp_path: Path) -> None:
    """The nearest thing to ground truth the ledger can offer: somebody looked
    at the evidence behind this question and said "yes, remember that"."""

    state = _state(tmp_path)
    _ask(state, "how do I rotate the seal", index=1, hits=0)
    state.upsert_memory_candidate(
        {
            "id": "cand-1",
            "rule_id": "retrieval-gap-v1",
            "params_hash": "x",
            "scope": "org",
            "subject": "retrieval-gaps",
            "kind": "fact",
            "text": "Nothing indexed answers that.",
            "evidence_json": json.dumps({"query": "how do I rotate the seal"}),
            "first_seen": "2026-01-01T00:00:00Z",
            "last_seen": "2026-01-01T00:00:00Z",
        }
    )
    state.decide_memory_candidate(
        "cand-1",
        status="admitted",
        admitted_by="user:ada",
        record_id="mem-xyz",
        when="2026-01-02T00:00:00Z",
    )

    report = bootstrap(state, tmp_path / "cases.json")

    assert report["answered"] == 1
    payload = json.loads((tmp_path / "cases.json").read_text(encoding="utf-8"))
    assert payload["cases"][0]["answered_by_record"] == "mem-xyz"


def test_building_a_set_works_without_formation_ever_having_run(tmp_path: Path) -> None:
    """An eval set is useful on its own; it must not require the review queue
    to exist."""

    state = _state(tmp_path)
    _ask(state, "a question", index=1)

    assert bootstrap(state, tmp_path / "cases.json")["cases"] == 1


def test_an_empty_ledger_produces_an_empty_set_rather_than_a_crash(tmp_path: Path) -> None:
    state = _state(tmp_path)

    assert bootstrap(state, tmp_path / "cases.json")["cases"] == 0
