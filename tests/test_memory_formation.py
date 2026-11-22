"""Forming memory from observed interactions.

The boundary this rests on: **an observation is evidence, a record is memory,
and only an admission crosses.** These tests pin the crossing --- that it goes
through the ordinary write path, that it is deterministic enough for a repeat
pass to write nothing, and that a session's digest can never be read by a
principal who did not produce it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from pheasant.artifacts.formation import (
    MAX_PATHS,
    MAX_QUERIES,
    SESSION_DIGEST_RULE,
    SessionObservations,
    admit,
    collect_sessions,
    digest_text,
    reject,
    run_candidate_rules,
    run_session_digests,
)
from pheasant.artifacts.maintenance import run_memory_maintenance
from pheasant.artifacts.store import MemoryStore, memory_source
from pheasant.config.schema import PheasantConfig
from pheasant.sync.log_queue import write_events
from pheasant.telemetry.interactions import InteractionEvent


def _config(tmp_path: Path, **formation: Any) -> tuple[PheasantConfig, Path]:
    docs = tmp_path / "ws" / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "runbook.md").write_text(
        "# Kestrel Runbook\n\nThe filewatch daemon restarts nightly at 0300 UTC.\n",
        encoding="utf-8",
    )
    for name in ("state", "exports", "memory"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    raw = {
        "pheasant": {
            "name": "kb",
            "state_path": str(tmp_path / "state"),
            "workspace_root": str(tmp_path / "ws"),
            "exports_path": str(tmp_path / "exports"),
        },
        "storage": {"graph_snapshots": False},
        # Off: these tests put rows in the ledger directly, because what is
        # under test is what formation makes of them -- the surfaces that
        # produce them are covered in `test_interactions.py`. Leaving it on
        # would build a buffer this app never tears down (no TestClient, so no
        # lifespan) and leak the process-wide slot into whatever runs next.
        "observability": {"interactions": {"enabled": False}},
        "memory": {"formation": {"enabled": True, "min_observations": 3, **formation}},
        "sources": [
            {
                "name": "docs",
                "type": "markdown_folder",
                "path": str(docs),
                "include": ["**/*.md"],
                "sync": {"on_startup": False},
            },
            {
                "name": "agent-memory",
                "type": "memory",
                "path": str(tmp_path / "memory"),
                "sync": {"on_startup": False},
            },
        ],
    }
    path = tmp_path / "pheasant.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return PheasantConfig.model_validate(raw), path


def _engine(tmp_path: Path, **formation: Any) -> Any:
    from pheasant.api.app import create_app

    config, path = _config(tmp_path, **formation)
    app = create_app(config, config_path=str(path))
    return app.state.engine


def _observe(
    engine: Any,
    session: str,
    queries: list[str],
    *,
    principal: str | None = "user:ada",
    paths: list[str] | None = None,
    answered: bool = True,
    modality: str = "ui",
    start: int = 0,
) -> None:
    """Put interactions in the ledger without going through HTTP.

    The surfaces that produce these are covered in `test_interactions.py`; what
    is under test here is what formation makes of them.
    """

    write_events(
        engine.state,
        [
            InteractionEvent(
                kb_id="kb",
                operation="/search",
                modality=modality,
                principal=principal,
                session_id=session,
                trace_id=f"{start + index:032x}",
                span_id=f"{start + index:016x}",
                started_at=f"2026-01-01T00:00:{start + index:02d}.000000Z",
                status="ok",
                duration_ms=12.5,
                query_text=query,
                result_paths=list(paths or ["runbook.md"]) if answered else [],
                result_count=1 if answered else 0,
                top_score=0.8 if answered else None,
            )
            for index, query in enumerate(queries)
        ],
    )


# --------------------------------------------------------------------------
# Off unless asked for
# --------------------------------------------------------------------------


def test_formation_is_off_by_default() -> None:
    settings = PheasantConfig().memory.formation

    assert settings.enabled is False
    assert settings.auto_admit is False
    # `session_digest` is True but only meaningful when `enabled` is, so a
    # default install forms nothing at all.
    assert settings.session_digest is True


def test_nothing_is_formed_while_formation_is_disabled(tmp_path: Path) -> None:
    engine = _engine(tmp_path, enabled=False)
    _observe(engine, "sess-a", ["one", "two", "three"])

    assert run_session_digests(engine) == {}

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    assert store.list_records() == []


def test_the_digest_rule_can_be_switched_off_on_its_own(tmp_path: Path) -> None:
    engine = _engine(tmp_path, session_digest=False)
    _observe(engine, "sess-a", ["one", "two", "three"])

    assert run_session_digests(engine) == {}


def test_a_rule_absent_from_the_configured_list_does_not_run(tmp_path: Path) -> None:
    """`rules` is the list an operator edits; a rule missing from it is off
    even when the feature is on."""

    engine = _engine(tmp_path, rules=["retrieval-gap-v1"])
    _observe(engine, "sess-a", ["one", "two", "three"])

    assert run_session_digests(engine) == {}


# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------


def test_a_session_below_the_threshold_is_not_worth_a_record(tmp_path: Path) -> None:
    """One question is not a dialog, and a record per drive-by query is the
    unbounded growth the capacity rules exist to prevent."""

    engine = _engine(tmp_path, min_observations=3)
    _observe(engine, "sess-small", ["only one", "and two"])

    assert run_session_digests(engine) == {}


def test_a_session_at_the_threshold_gets_one(tmp_path: Path) -> None:
    engine = _engine(tmp_path, min_observations=3)
    _observe(engine, "sess-a", ["one", "two", "three"])

    report = run_session_digests(engine)

    assert report["rule_id"] == SESSION_DIGEST_RULE
    assert len(report["created"]) == 1


def test_a_pass_is_bounded(tmp_path: Path) -> None:
    """A first pass over a busy ledger must not write unboundedly; what it
    does not reach waits for the next beat."""

    engine = _engine(tmp_path, max_candidates_per_pass=2)
    for index in range(5):
        _observe(engine, f"sess-{index}", ["a", "b", "c"], start=index * 10)

    assert len(run_session_digests(engine)["created"]) == 2


# --------------------------------------------------------------------------
# One record per session, refined through dialog
# --------------------------------------------------------------------------


def test_a_refined_digest_supersedes_its_own_previous_version(tmp_path: Path) -> None:
    """ "A session has a single memory, refined through dialog" needs no new
    primitive: it is a supersession chain, so `current_only` returns exactly
    one record while `as_of` can still read what the session looked like
    earlier."""

    engine = _engine(tmp_path)
    store = MemoryStore(memory_source(engine.config, engine.state).path)
    _observe(engine, "sess-a", ["one", "two", "three"])
    first = run_session_digests(engine)["created"][0]

    _observe(engine, "sess-a", ["four"], start=50)
    second = run_session_digests(engine)["refined"][0]

    assert second != first
    current = store.list_records(current_only=True)
    assert len(current) == 1, "a session must have exactly one live memory"
    assert current[0].record_id == second
    assert current[0].supersedes == first
    # Nothing is destroyed: the earlier version is still on disk and still
    # reachable through `as_of`.
    assert len(store.list_records()) == 2


def test_a_repeat_pass_over_unchanged_evidence_writes_nothing(tmp_path: Path) -> None:
    """The determinism property, from the outside. Without it every beat would
    write a record superseding the last, and the chain would grow forever.

    Two guards hold this up and the assertion below is deliberately on the
    outcome rather than on either of them: the pass short-circuits on
    unchanged text (cheap), and the store dedups an id it has already written
    (sound). Deleting the short-circuit alone must not change what ends up on
    disk -- which is the property this asserts.
    """

    engine = _engine(tmp_path)
    store = MemoryStore(memory_source(engine.config, engine.state).path)
    _observe(engine, "sess-a", ["one", "two", "three"])
    run_session_digests(engine)
    before = [record.record_id for record in store.list_records()]

    second = run_session_digests(engine)

    assert second.get("created") is None
    assert second.get("refined") is None
    assert second["unchanged"] == 1
    # The chain did not grow, and the live record is the same file.
    assert [record.record_id for record in store.list_records()] == before
    assert len(store.list_records(current_only=True)) == 1


def test_the_same_evidence_produces_byte_identical_text() -> None:
    """A record's id is a digest of `scope|subject|text`, so the text has to be
    a pure function of the evidence -- every list truncated after an explicit
    sort, never left in storage order."""

    def build() -> SessionObservations:
        session = SessionObservations(
            session_id="s",
            principal="user:ada",
            modalities={"mcp", "ui"},
            queries=["b", "a"],
            observations=3,
            first_seen="2026-01-01T00:00:00Z",
            last_seen="2026-01-01T00:01:00Z",
        )
        # Equal counts: `Counter.most_common` alone would leave these in
        # insertion order, which is not stable across two collections.
        session.paths.update(["z.md", "a.md", "z.md", "a.md"])
        return session

    assert digest_text(build()) == digest_text(build())
    text = digest_text(build())
    # Ties break on the path, so `a.md` precedes `z.md` at the same count.
    assert text.index("a.md") < text.index("z.md")
    # Queries stay in the order the session asked them, which is dialog order.
    assert text.index("- b") < text.index("- a")


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------


def test_two_principals_claiming_one_session_get_separate_memories(
    tmp_path: Path,
) -> None:
    """A session id is caller-asserted, so two principals *can* claim one. A
    digest that mixed them would be readable by whichever writer owned the
    record -- an ACL leak reached through a field nobody authenticates."""

    engine = _engine(tmp_path)
    _observe(engine, "shared", ["a", "b", "c"], principal="user:ada")
    _observe(engine, "shared", ["x", "y", "z"], principal="user:bo", start=20)

    run_session_digests(engine)

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    records = store.list_records(current_only=True)
    assert len(records) == 2
    assert {record.written_by for record in records} == {"user:ada", "user:bo"}
    ada = next(r for r in records if r.written_by == "user:ada")
    assert "x" not in ada.text and "y" not in ada.text


def test_a_digest_is_scoped_to_its_session_and_its_writer(tmp_path: Path) -> None:
    """Which is what makes writing it automatically safe: it never becomes
    shared knowledge. Reaching user or org scope takes an explicit promotion."""

    from pheasant.security.acl import normalize_acl

    engine = _engine(tmp_path)
    _observe(engine, "sess-a", ["one", "two", "three"], principal="user:ada")
    run_session_digests(engine)

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    record = store.list_records(current_only=True)[0]
    assert record.scope == "session"
    assert record.subject == "sess-a"
    assert record.written_by == "user:ada"

    acl = normalize_acl("memory", {"scope": record.scope, "written_by": record.written_by})
    assert acl == {"allow": ["user:ada"], "public": False}


def test_a_formed_record_says_it_was_formed(tmp_path: Path) -> None:
    """A machine-authored record must always be distinguishable from a written
    one -- the posture `llm-synthesized` already establishes for synthesis."""

    engine = _engine(tmp_path)
    _observe(engine, "sess-a", ["one", "two", "three"])
    run_session_digests(engine)

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    tags = store.list_records(current_only=True)[0].tags
    assert SESSION_DIGEST_RULE in tags
    assert "formed" in tags


# --------------------------------------------------------------------------
# What the digest says
# --------------------------------------------------------------------------


def test_a_question_that_found_nothing_is_recorded_as_a_gap(tmp_path: Path) -> None:
    """The honest form of "usage expands the knowledge": usage cannot conjure
    facts the corpus lacks, but it can say which questions keep going
    unanswered."""

    engine = _engine(tmp_path)
    _observe(engine, "sess-a", ["answered one", "answered two"])
    _observe(engine, "sess-a", ["nobody knows this"], answered=False, start=30)

    run_session_digests(engine)

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    text = store.list_records(current_only=True)[0].text
    assert "Found nothing for:" in text
    assert "- nobody knows this" in text


def test_the_digest_is_bounded_however_long_the_session(tmp_path: Path) -> None:
    """A digest is a paragraph someone reads in the Memory tab. An unbounded
    one is a transcript, which is the thing this deliberately is not."""

    engine = _engine(tmp_path)
    _observe(engine, "sess-long", [f"question {index}" for index in range(40)])

    run_session_digests(engine)

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    text = store.list_records(current_only=True)[0].text
    assert text.count("\n- ") <= MAX_QUERIES + MAX_PATHS + 5
    # It still reports the true total, so the record does not understate the
    # session it describes.
    assert "40 interactions" in text


def test_redaction_leaves_a_digest_that_still_says_something(tmp_path: Path) -> None:
    """`redact_text` drops what anyone typed. The structural half survives, so
    a region can keep learning its own shape without keeping its content."""

    engine = _engine(tmp_path)
    write_events(
        engine.state,
        [
            InteractionEvent(
                kb_id="kb",
                operation="/search",
                modality="ui",
                principal="user:ada",
                session_id="sess-r",
                trace_id=f"{index:032x}",
                span_id=f"{index:016x}",
                started_at=f"2026-01-01T00:00:{index:02d}.000000Z",
                status="ok",
                query_text=None,  # redacted at write time
                result_paths=["runbook.md"],
                result_count=1,
                top_score=0.8,
            )
            for index in range(3)
        ],
    )

    run_session_digests(engine)

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    text = store.list_records(current_only=True)[0].text
    assert "Asked about:" not in text
    assert "Most-consulted:" in text
    assert "runbook.md" in text


def test_the_modality_a_session_used_is_recorded(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _observe(engine, "sess-a", ["one", "two"], modality="mcp")
    _observe(engine, "sess-a", ["three"], modality="ui", start=20)

    run_session_digests(engine)

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    assert "(mcp/ui)" in store.list_records(current_only=True)[0].text


# --------------------------------------------------------------------------
# On the beat, and reachable afterwards
# --------------------------------------------------------------------------


def test_formation_rides_the_maintenance_beat(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _observe(engine, "sess-a", ["one", "two", "three"])

    result = run_memory_maintenance(engine, now=datetime.now(UTC))

    assert result is not None
    assert len(result["formation"]["created"]) == 1


def test_the_beat_forms_nothing_when_formation_is_off(tmp_path: Path) -> None:
    """Rule 7's shape: a region that has not asked for this behaves exactly as
    it did before formation existed."""

    engine = _engine(tmp_path, enabled=False)
    _observe(engine, "sess-a", ["one", "two", "three"])

    result = run_memory_maintenance(engine, now=datetime.now(UTC))

    assert result is not None
    assert "formation" not in result


def test_a_formed_digest_is_an_ordinary_indexed_record(tmp_path: Path) -> None:
    """The crossing, end to end: a formed record goes through the same write
    path, gets indexed by the same pipeline, and is found by the same search as
    anything a caller wrote. No second ingestion path."""

    engine = _engine(tmp_path)
    _observe(engine, "sess-a", ["filewatch daemon nightly", "two", "three"])
    run_session_digests(engine)

    engine.sync_source("agent-memory", "full")
    rows = engine.state.rows(
        "SELECT scope, subject, kind FROM memory_records WHERE scope='session'", ()
    )
    assert len(rows) == 1
    assert rows[0]["subject"] == "sess-a"
    # A fact, not a steering rule: it is knowledge about a session, not syntax
    # that re-ranks queries.
    assert rows[0]["kind"] == "fact"


def test_an_unusable_session_id_is_skipped_never_raised(tmp_path: Path) -> None:
    """Session ids are caller data on an unauthenticated surface. A newline in
    one would forge frontmatter, which the store rejects -- and the beat must
    survive that rather than dying on one bad row."""

    engine = _engine(tmp_path)
    _observe(engine, "bad\nid", ["one", "two", "three"])
    _observe(engine, "good-id", ["one", "two", "three"], start=20)

    report = run_session_digests(engine)

    assert len(report.get("created") or []) == 1
    store = MemoryStore(memory_source(engine.config, engine.state).path)
    assert store.list_records(current_only=True)[0].subject == "good-id"


def test_collect_ignores_rows_with_no_session(tmp_path: Path) -> None:
    """Most traffic carries no session id at all, and a digest of "everything
    anonymous" would be one enormous record that changes on every request."""

    engine = _engine(tmp_path)
    write_events(
        engine.state,
        [
            InteractionEvent(
                kb_id="kb",
                operation="/search",
                modality="ui",
                session_id=None,
                trace_id=f"{index:032x}",
                span_id=f"{index:016x}",
                started_at=f"2026-01-01T00:00:{index:02d}.000000Z",
                status="ok",
                query_text="anonymous",
            )
            for index in range(5)
        ],
    )

    assert collect_sessions(engine.state, min_observations=1, max_sessions=10) == []


@pytest.mark.parametrize("bad", [None, ""])
def test_a_session_with_no_principal_still_forms(tmp_path: Path, bad: str | None) -> None:
    """Unattributed memory falls through to the region default rather than
    inventing an owner -- the rule `normalize_acl` already follows."""

    engine = _engine(tmp_path)
    _observe(engine, "sess-anon", ["one", "two", "three"], principal=bad)

    run_session_digests(engine)

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    assert store.list_records(current_only=True)[0].written_by is None


# --------------------------------------------------------------------------
# Candidates: the crossing, and the review gate in front of it
# --------------------------------------------------------------------------


def _corpus_engine(tmp_path: Path, **formation: Any) -> Any:
    """An engine over a corpus that says `pheasant-flock` where a team says
    `router` -- the shape every mining rule is looking for."""

    from pheasant.api.app import create_app

    config, path = _config(tmp_path, **formation)
    docs = tmp_path / "ws" / "docs"
    (docs / "deploy").mkdir(parents=True, exist_ok=True)
    (docs / "deploy" / "rollout.md").write_text(
        "# Rollout\n\nThe pheasant-flock service coordinates every rollout.\n", encoding="utf-8"
    )
    (docs / "deploy" / "canary.md").write_text(
        "# Canary\n\nCanary steps are driven by the pheasant-flock service.\n", encoding="utf-8"
    )
    app = create_app(config, config_path=str(path))
    app.state.engine.sync_source("docs", "full")
    return app.state.engine


def _ask(engine: Any, session: str, query: str, *, start: int, hits: list[str]) -> None:
    ids, paths = [], []
    for stable_id in hits:
        ids.append(f"file:docs:{stable_id}:branch=none")
        paths.append(stable_id)
    write_events(
        engine.state,
        [
            InteractionEvent(
                kb_id="kb",
                operation="/search",
                modality="ui",
                principal="user:ada",
                session_id=session,
                trace_id=f"{start:032x}",
                span_id=f"{start:016x}",
                started_at=f"2026-01-01T00:00:{start:02d}.000000Z",
                status="ok",
                # `observe()` always sets this; a fixture that leaves it None
                # would let an assertion about it pass for the wrong reason.
                duration_ms=12.5,
                query_text=query,
                result_ids=ids,
                result_paths=paths,
                result_count=len(hits),
                top_score=0.8 if hits else None,
            )
        ],
    )


def test_a_word_the_corpus_never_uses_becomes_an_alias_proposal(tmp_path: Path) -> None:
    """The rule that finds team vocabulary. Aliases are the one part of memory
    that improves queries returning no memory at all, and this finds them
    without anyone writing them by hand."""

    engine = _corpus_engine(tmp_path, min_observations=2, min_sessions=2)
    for index, session in enumerate(("s1", "s2")):
        _ask(engine, session, "router rollout", start=index * 10, hits=["deploy/rollout.md"])
        _ask(engine, session, "router canary", start=index * 10 + 1, hits=["deploy/canary.md"])

    run_candidate_rules(engine)

    aliases = engine.state.list_memory_candidates(rule_id="alias-cooccurrence-v1")
    assert [c["text"] for c in aliases] == ["router -> pheasant-flock"]
    assert aliases[0]["kind"] == "alias"
    assert aliases[0]["scope"] == "org"
    assert aliases[0]["sessions"] == 2


def test_a_different_inflection_is_not_proposed_as_an_alias(tmp_path: Path) -> None:
    """Without this guard the rule proposes things like `coordination ->
    check`: the word is literally absent from documents that say "coordinates",
    so the absence test passes and the rule reaches for whatever term they
    share. Found on a real fixture, not imagined."""

    engine = _corpus_engine(tmp_path, min_observations=2, min_sessions=2)
    for index, session in enumerate(("s1", "s2")):
        _ask(
            engine,
            session,
            "rollout coordination",
            start=index * 10,
            hits=["deploy/rollout.md", "deploy/canary.md"],
        )
        _ask(
            engine,
            session,
            "rollout coordination steps",
            start=index * 10 + 1,
            hits=["deploy/rollout.md", "deploy/canary.md"],
        )

    run_candidate_rules(engine)

    proposals = [c["text"] for c in engine.state.list_memory_candidates()]
    assert not any(text.startswith("coordination ->") for text in proposals)


def test_the_alias_target_is_the_corpus_s_own_word_not_a_common_one(
    tmp_path: Path,
) -> None:
    """Being shared by every retrieved document is not enough.

    The first version of this rule picked the alphabetically-first universal
    term and proposed `router -> before`, because "before traffic shifts" and
    "before promotion" both contain it. Alphabetical order is not a signal.
    The pick is by corpus document frequency now -- the same IDF the ranking
    arm scores with, so "distinctive" means the same thing to formation as it
    does to search.
    """

    engine = _corpus_engine(tmp_path, min_observations=2, min_sessions=2)
    for index, session in enumerate(("s1", "s2")):
        _ask(engine, session, "router rollout", start=index * 10, hits=["deploy/rollout.md"])
        _ask(engine, session, "router canary", start=index * 10 + 1, hits=["deploy/canary.md"])

    run_candidate_rules(engine)

    aliases = engine.state.list_memory_candidates(rule_id="alias-cooccurrence-v1")
    assert [c["text"] for c in aliases] == ["router -> pheasant-flock"]


def test_a_corpus_with_no_vocabulary_still_proposes_rather_than_crashing(
    tmp_path: Path,
) -> None:
    """An empty corpus, or an FTS build without `fts5vocab`, degrades to
    lexical order -- the behaviour before frequencies were consulted."""

    from pheasant.artifacts.formation import _dominant_term

    bodies = ["alpha zulu content here", "alpha zulu other content"]
    assert _dominant_term(bodies, exclude=set(), state=None) == "alpha"


def test_documents_that_are_simply_similar_yield_no_alias() -> None:
    """More than a handful of shared terms means the documents resemble each
    other, not that any one term is the alias."""

    from pheasant.artifacts.formation import MAX_UNIVERSAL_TERMS, _dominant_term

    shared = " ".join(f"term{index}" for index in range(MAX_UNIVERSAL_TERMS + 2))
    assert _dominant_term([shared, shared], exclude=set(), state=None) is None


def test_a_query_family_that_lands_in_one_directory_becomes_a_preference(
    tmp_path: Path,
) -> None:
    engine = _corpus_engine(tmp_path, min_observations=2, min_sessions=2)
    for index, session in enumerate(("s1", "s2")):
        _ask(engine, session, "router rollout", start=index * 10, hits=["deploy/rollout.md"])
        _ask(engine, session, "router canary", start=index * 10 + 1, hits=["deploy/canary.md"])

    run_candidate_rules(engine)

    prefs = engine.state.list_memory_candidates(rule_id="path-affinity-v1")
    assert "when: router -> prefer: deploy/" in [c["text"] for c in prefs]
    # A `preference` rule, parseable by the steering engine that will run it.
    from pheasant.artifacts.steering import parse_rule

    assert parse_rule("preference", prefs[0]["text"]) is not None


def test_a_prefix_is_cut_at_a_directory_boundary() -> None:
    """`docs/deploy.md` and `docs/deployment.md` share the characters
    `docs/deploy`, which is not a directory -- a preference rule matching it
    would match neither file the way an operator expects."""

    from pheasant.artifacts.formation import _path_prefix

    assert _path_prefix(["docs/deploy.md", "docs/deployment.md"]) == "docs/"
    assert _path_prefix(["a/b/one.md", "a/b/two.md"]) == "a/b/"
    # One path is a hit, not a pattern.
    assert _path_prefix(["a/b/one.md"]) is None
    # Nothing in common is not an affinity.
    assert _path_prefix(["a/one.md", "b/two.md"]) is None
    # A root-level file has no directory to prefer.
    assert _path_prefix(["one.md", "two.md"]) is None


def test_a_question_nothing_answers_becomes_a_gap_proposal(tmp_path: Path) -> None:
    """A gap is "no results at all", never "nothing scored well". Fused RRF
    scores are small positive numbers whose scale depends on how many arms
    contributed, so a score threshold would be a tuning knob pretending to be
    a fact."""

    engine = _corpus_engine(tmp_path, min_observations=2, min_sessions=2)
    for index, session in enumerate(("s1", "s2")):
        _ask(engine, session, "how do I rotate the vault seal", start=index * 10, hits=[])

    run_candidate_rules(engine)

    gaps = engine.state.list_memory_candidates(rule_id="retrieval-gap-v1")
    assert len(gaps) == 1
    assert "how do I rotate the vault seal" in gaps[0]["text"]
    # About the corpus, not about whoever hit it.
    assert gaps[0]["scope"] == "org"
    assert gaps[0]["written_by"] is None


def test_a_pattern_seen_in_one_session_only_is_not_proposed(tmp_path: Path) -> None:
    """One session repeating itself is a habit; several agreeing is a signal.
    Without `min_sessions` a single loop could mint steering that re-ranks
    results for everyone."""

    engine = _corpus_engine(tmp_path, min_observations=2, min_sessions=2)
    _ask(engine, "s1", "router rollout", start=0, hits=["deploy/rollout.md"])
    _ask(engine, "s1", "router canary", start=1, hits=["deploy/canary.md"])

    run_candidate_rules(engine)

    assert engine.state.list_memory_candidates() == []


def test_promoting_a_candidate_writes_an_ordinary_record(tmp_path: Path) -> None:
    """**The crossing.** Through `MemoryStore.append`, so the result is an
    ordinary file indexed by the ordinary pipeline -- no second ingestion
    path for a formed record any more than for a written one."""

    engine = _corpus_engine(tmp_path, min_observations=2, min_sessions=2)
    for index, session in enumerate(("s1", "s2")):
        _ask(engine, session, "router rollout", start=index * 10, hits=["deploy/rollout.md"])
        _ask(engine, session, "router canary", start=index * 10 + 1, hits=["deploy/canary.md"])
    run_candidate_rules(engine)
    candidate = engine.state.list_memory_candidates(rule_id="alias-cooccurrence-v1")[0]

    result = admit(engine, candidate["id"], admitted_by="user:ada")

    store = MemoryStore(memory_source(engine.config, engine.state).path)
    record = next(r for r in store.list_records() if r.record_id == result["record_id"])
    assert record.text == "router -> pheasant-flock"
    assert record.kind == "alias"
    assert "formed" in record.tags
    assert "alias-cooccurrence-v1" in record.tags
    # And the candidate now points at what it became.
    decided = engine.state.get_memory_candidate(candidate["id"])
    assert decided["status"] == "admitted"
    assert decided["record_id"] == result["record_id"]
    assert decided["admitted_by"] == "user:ada"


def test_a_rejected_proposal_is_never_proposed_again(tmp_path: Path) -> None:
    """Re-suggesting what somebody just declined is the fastest way to make a
    review queue worth ignoring."""

    engine = _corpus_engine(tmp_path, min_observations=2, min_sessions=2)
    for index, session in enumerate(("s1", "s2")):
        _ask(engine, session, "router rollout", start=index * 10, hits=["deploy/rollout.md"])
        _ask(engine, session, "router canary", start=index * 10 + 1, hits=["deploy/canary.md"])
    run_candidate_rules(engine)
    candidate = engine.state.list_memory_candidates(rule_id="alias-cooccurrence-v1")[0]
    reject(engine, candidate["id"], rejected_by="user:ada")

    # The evidence has not gone anywhere, so the rule re-derives the same
    # proposal -- and the upsert must refuse to reopen it.
    run_candidate_rules(engine)

    assert engine.state.get_memory_candidate(candidate["id"])["status"] == "rejected"
    assert engine.state.list_memory_candidates(rule_id="alias-cooccurrence-v1") == []


def test_a_decision_is_final(tmp_path: Path) -> None:
    """Two reviewers racing on one candidate must not both admit it, or the
    region gets two identical records."""

    engine = _corpus_engine(tmp_path, min_observations=2, min_sessions=2)
    for index, session in enumerate(("s1", "s2")):
        _ask(engine, session, "router rollout", start=index * 10, hits=["deploy/rollout.md"])
        _ask(engine, session, "router canary", start=index * 10 + 1, hits=["deploy/canary.md"])
    run_candidate_rules(engine)
    candidate = engine.state.list_memory_candidates()[0]
    admit(engine, candidate["id"], admitted_by="user:ada")

    with pytest.raises(ValueError, match="already admitted"):
        admit(engine, candidate["id"], admitted_by="user:bo")


def test_promoting_something_that_does_not_exist_says_so(tmp_path: Path) -> None:
    engine = _corpus_engine(tmp_path)

    with pytest.raises(KeyError, match="Unknown memory candidate"):
        admit(engine, "nope", admitted_by="user:ada")


def test_auto_admit_is_off_by_default_and_admits_when_on(tmp_path: Path) -> None:
    """Off for the same reason `compaction_enabled` is: it changes what a
    *default* query returns."""

    engine = _corpus_engine(tmp_path, min_observations=2, min_sessions=2)
    for index, session in enumerate(("s1", "s2")):
        _ask(engine, session, "router rollout", start=index * 10, hits=["deploy/rollout.md"])
        _ask(engine, session, "router canary", start=index * 10 + 1, hits=["deploy/canary.md"])

    report = run_candidate_rules(engine)
    assert "admitted" not in report
    assert engine.state.memory_candidate_counts().get("admitted") is None

    engine.config.memory.formation.auto_admit = True
    admitted = run_candidate_rules(engine)["admitted"]
    assert admitted
    store = MemoryStore(memory_source(engine.config, engine.state).path)
    formed = [r for r in store.list_records() if "formed" in r.tags]
    assert formed
    # Still distinguishable from something a person wrote.
    decided = engine.state.list_memory_candidates(status="admitted")
    assert all(c["admitted_by"].startswith("rule:") for c in decided)


def test_candidates_expire_but_rejections_do_not(tmp_path: Path) -> None:
    """A stale proposal is noise in a queue; a rejection is a decision."""

    from datetime import timedelta

    from pheasant.artifacts.formation import expire_candidates

    engine = _corpus_engine(tmp_path, min_observations=2, min_sessions=2, candidate_ttl_days=1)
    for index, session in enumerate(("s1", "s2")):
        _ask(engine, session, "router rollout", start=index * 10, hits=["deploy/rollout.md"])
        _ask(engine, session, "router canary", start=index * 10 + 1, hits=["deploy/canary.md"])
    run_candidate_rules(engine)
    rejected = engine.state.list_memory_candidates()[0]
    reject(engine, rejected["id"], rejected_by="user:ada")

    later = datetime.now(UTC) + timedelta(days=400)
    assert expire_candidates(engine, now=later) >= 1

    counts = engine.state.memory_candidate_counts()
    assert counts.get("rejected") == 1
    assert counts.get("pending") is None


def test_tightening_a_threshold_proposes_anew_rather_than_rewriting(tmp_path: Path) -> None:
    """`params_hash` is part of the candidate id, so a proposal a person has
    already seen is never silently re-evidenced under different parameters."""

    from pheasant.artifacts.formation import params_hash

    loose = _config(tmp_path, min_observations=2)[0].memory.formation
    tight = _config(tmp_path, min_observations=9)[0].memory.formation
    assert params_hash(loose) != params_hash(tight)


# --------------------------------------------------------------------------
# The review surfaces
# --------------------------------------------------------------------------


def test_the_http_surface_lists_promotes_and_rejects(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    config, path = _config(tmp_path, min_observations=2, min_sessions=2)
    docs = tmp_path / "ws" / "docs"
    (docs / "deploy").mkdir(parents=True, exist_ok=True)
    (docs / "deploy" / "rollout.md").write_text(
        "# Rollout\n\nThe pheasant-flock service coordinates every rollout.\n", encoding="utf-8"
    )
    (docs / "deploy" / "canary.md").write_text(
        "# Canary\n\nCanary steps are driven by the pheasant-flock service.\n", encoding="utf-8"
    )
    app = create_app(config, config_path=str(path))
    engine = app.state.engine
    engine.sync_source("docs", "full")
    for index, session in enumerate(("s1", "s2")):
        _ask(engine, session, "router rollout", start=index * 10, hits=["deploy/rollout.md"])
        _ask(engine, session, "router canary", start=index * 10 + 1, hits=["deploy/canary.md"])
    run_candidate_rules(engine)

    with TestClient(app) as client:
        listing = client.get("/artifacts/candidates")
        assert listing.status_code == 200
        candidates = listing.json()["candidates"]
        assert candidates, "the rules proposed nothing to review"

        promoted = client.post(f"/artifacts/candidates/{candidates[0]['id']}/promote")
        assert promoted.status_code == 200
        assert promoted.json()["record_id"].startswith("mem-")

        # A decision is final, and the surface says so rather than writing a
        # second identical record.
        again = client.post(f"/artifacts/candidates/{candidates[0]['id']}/promote")
        assert again.status_code == 409

        rejected = client.post(f"/artifacts/candidates/{candidates[1]['id']}/reject")
        assert rejected.status_code == 200

        assert client.post("/artifacts/candidates/nope/promote").status_code == 404
        assert client.post("/artifacts/candidates/nope/reject").status_code == 404

        remaining = client.get("/artifacts/candidates").json()
        assert all(
            c["id"] not in {candidates[0]["id"], candidates[1]["id"]}
            for c in remaining["candidates"]
        )
        assert remaining["counts"]["admitted"] == 1
        assert remaining["counts"]["rejected"] == 1


def test_the_mcp_surface_is_additive(tmp_path: Path) -> None:
    """Rule 8: the tool surface is public API. These are new names, and the
    tools that were there before still answer exactly as they did."""

    from pheasant.mcp_server.tools import PheasantTools

    config, _path = _config(tmp_path)
    tools = PheasantTools(config)
    try:
        for name in (
            "list_memory_candidates",
            "promote_memory_candidate",
            "reject_memory_candidate",
        ):
            assert callable(getattr(tools, name))
        # Nothing renamed or removed alongside them.
        for existing in ("memory_write", "memory_consolidate", "search_context"):
            assert callable(getattr(tools, existing))

        listed = tools.list_memory_candidates("kb")
        assert listed["candidates"] == []
    finally:
        tools.engine.close()


# --------------------------------------------------------------------------
# The evidence trail behind a proposal
# --------------------------------------------------------------------------


def test_a_proposal_names_the_calls_it_came_from(tmp_path: Path) -> None:
    """Without this a candidate is an assertion with a count attached. A
    reviewer looking at `router -> pheasant-flock` could see it was seen four
    times and nothing at all about what was asked or what came back."""

    engine = _corpus_engine(tmp_path, min_observations=2, min_sessions=2)
    for index, session in enumerate(("s1", "s2")):
        _ask(engine, session, "router rollout", start=index * 10, hits=["deploy/rollout.md"])
        _ask(engine, session, "router canary", start=index * 10 + 1, hits=["deploy/canary.md"])

    run_candidate_rules(engine)

    candidate = engine.state.list_memory_candidates(rule_id="alias-cooccurrence-v1")[0]
    evidence = json.loads(candidate["evidence_json"])
    assert len(evidence["event_ids"]) == 4

    # And every named id resolves to a row a reviewer can read.
    rows = engine.state.interaction_events_by_id(evidence["event_ids"])
    assert len(rows) == 4
    assert {row["query_text"] for row in rows} == {"router rollout", "router canary"}
    assert all(
        row["trace_id"] and row["span_id"] and row["duration_ms"] is not None for row in rows
    )


def test_the_named_calls_are_bounded(tmp_path: Path) -> None:
    """A token asked four hundred times must not carry four hundred ids in the
    candidate row. The counts beside it stay the true totals."""

    from pheasant.artifacts.formation import MAX_EVIDENCE_EVENTS

    engine = _corpus_engine(tmp_path, min_observations=2, min_sessions=2)
    for index in range(40):
        session = "s1" if index % 2 else "s2"
        _ask(engine, session, "router rollout", start=index, hits=["deploy/rollout.md"])

    run_candidate_rules(engine)

    candidate = engine.state.list_memory_candidates(rule_id="alias-cooccurrence-v1")[0]
    evidence = json.loads(candidate["evidence_json"])
    assert len(evidence["event_ids"]) == MAX_EVIDENCE_EVENTS
    assert candidate["observations"] == 40


def test_the_evidence_endpoint_serves_all_three_layers(tmp_path: Path) -> None:
    """What is claimed, on what basis, and how to check it — the three
    questions a reviewer asks in order."""

    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    config, path = _config(tmp_path, min_observations=2, min_sessions=2)
    docs = tmp_path / "ws" / "docs"
    (docs / "deploy").mkdir(parents=True, exist_ok=True)
    (docs / "deploy" / "rollout.md").write_text(
        "# Rollout\n\nThe pheasant-flock service coordinates every rollout.\n", encoding="utf-8"
    )
    (docs / "deploy" / "canary.md").write_text(
        "# Canary\n\nCanary steps are driven by the pheasant-flock service.\n", encoding="utf-8"
    )
    app = create_app(config, config_path=str(path))
    engine = app.state.engine
    engine.sync_source("docs", "full")
    for index, session in enumerate(("s1", "s2")):
        _ask(engine, session, "router rollout", start=index * 10, hits=["deploy/rollout.md"])
        _ask(engine, session, "router canary", start=index * 10 + 1, hits=["deploy/canary.md"])
    run_candidate_rules(engine)
    candidate = engine.state.list_memory_candidates(rule_id="alias-cooccurrence-v1")[0]

    with TestClient(app) as client:
        response = client.get(f"/artifacts/candidates/{candidate['id']}/evidence")
        assert response.status_code == 200
        payload = response.json()

        # 1. what is claimed
        assert payload["candidate"]["text"] == "router -> pheasant-flock"
        # 2. on what basis
        assert payload["named"] == payload["found"] == 4
        asked = {call["query_text"] for call in payload["interactions"]}
        assert asked == {"router rollout", "router canary"}
        assert any(json.loads(c["result_paths_json"] or "[]") for c in payload["interactions"])
        # 3. how to check it
        for call in payload["interactions"]:
            assert call["trace_id"] and call["span_id"]
            assert call["duration_ms"] is not None
            assert call["status"] == "ok"

        assert client.get("/artifacts/candidates/nope/evidence").status_code == 404


def test_evidence_that_aged_out_is_reported_not_hidden(tmp_path: Path) -> None:
    """The hot window is retention-bounded, so a pending proposal can outlive
    the rows behind it. A short list that looks like the whole story is worse
    than saying how much is missing."""

    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    config, path = _config(tmp_path, min_observations=2, min_sessions=2)
    docs = tmp_path / "ws" / "docs"
    (docs / "deploy").mkdir(parents=True, exist_ok=True)
    (docs / "deploy" / "rollout.md").write_text(
        "# Rollout\n\nThe pheasant-flock service coordinates every rollout.\n", encoding="utf-8"
    )
    (docs / "deploy" / "canary.md").write_text(
        "# Canary\n\nCanary steps are driven by the pheasant-flock service.\n", encoding="utf-8"
    )
    app = create_app(config, config_path=str(path))
    engine = app.state.engine
    engine.sync_source("docs", "full")
    for index, session in enumerate(("s1", "s2")):
        _ask(engine, session, "router rollout", start=index * 10, hits=["deploy/rollout.md"])
        _ask(engine, session, "router canary", start=index * 10 + 1, hits=["deploy/canary.md"])
    run_candidate_rules(engine)
    candidate = engine.state.list_memory_candidates(rule_id="alias-cooccurrence-v1")[0]

    # The roll takes the evidence, exactly as retention would.
    engine.state.execute("DELETE FROM interaction_events", ())

    with TestClient(app) as client:
        payload = client.get(f"/artifacts/candidates/{candidate['id']}/evidence").json()

    assert payload["named"] == 4
    assert payload["found"] == 0
    assert payload["interactions"] == []
    # The proposal itself is untouched: the counts remain what the rule saw.
    assert payload["candidate"]["observations"] == 4
