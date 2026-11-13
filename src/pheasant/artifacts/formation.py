"""Turning recorded interactions into memory, deterministically.

The observation plane records what was asked, on which surface, by whom, and
what came back. This is the half that reads it -- and the boundary it must not
cross is the whole design: **an observation is evidence, a record is memory,
and only an admission crosses.** Admission goes through
:meth:`pheasant.artifacts.store.MemoryStore.append` like every other write, so
memory's first invariant ("records are files, no second ingestion path, no
direct index writes") never bends here.

**No model runs in this path.** Rules are counting and string matching over
recorded rows, so a pass is reproducible: the same ledger and the same
parameters produce byte-identical text, and therefore -- because a record's id
is a digest of ``scope|subject|text`` -- the same record id. That is what makes
a repeat pass free rather than merely idempotent-by-luck: an unchanged session
re-derives the record it already wrote and the store dedups it on the file.

## The session digest

The first rule, and the one plan 2 was written about: *a session has a single
memory, refined through dialog.* That needs no new primitive. It is a
supersession chain --- scope ``session``, subject the session id, each
refinement naming the previous one in ``supersedes`` --- so:

* ``current_only`` (on by default) returns **exactly one** record per session;
* ``as_of`` reads the session's history, which is the point of invalidating
  rather than overwriting;
* consolidation archives the chain on the ordinary beat;
* ``session_ttl_days`` decays it like any other session-scope memory.

**Why this is written automatically while everything else is proposed.** A
session digest is `scope: session`, subject that session, written by that
principal --- so under ``security.acl_enforced`` only its own writer can read
it, and it decays with the session. It never becomes shared knowledge: reaching
``user`` or ``org`` scope takes an explicit promotion, which is exactly the
"nothing persists into the knowledge base unless a person adds it" the design
asks for. What is automatic here is a session's memory of itself.

A digest is written only for a session with at least
``memory.formation.min_observations`` recorded interactions. One question is
not a dialog, and a record per drive-by query would be the unbounded growth the
capacity rules exist to prevent.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import blake2b
from typing import Any

from pheasant.artifacts.store import MemoryRecord, MemoryStore, memory_source

logger = logging.getLogger(__name__)

#: Versioned, and a new version is a **new id** rather than an edit to this
#: one: a record's provenance has to stay attributable after the logic that
#: produced it changes.
SESSION_DIGEST_RULE = "session-digest-v1"

#: The tag every formed record carries, so a machine-authored record is always
#: distinguishable from a written one -- the posture `llm-synthesized` already
#: establishes for synthesis.
FORMED_TAG = "formed"

#: How much of a session one digest states. Caps, not thresholds: a digest is
#: a paragraph a person reads in the Memory tab, and an unbounded one is a
#: transcript, which is the thing this deliberately is not.
MAX_QUERIES = 10
MAX_PATHS = 10
MAX_GAPS = 5


#: What counts as unanswered: **no results at all**.
#:
#: Deliberately not a score threshold. Fused RRF scores are small positive
#: numbers whose scale depends on how many arms contributed, so "below 0.05"
#: is a tuning knob pretending to be a fact --- and with any threshold at all,
#: a query that matched three irrelevant documents on a stopword reads as
#: answered. An empty result set is unambiguous on every backend and in every
#: mode, and it is the only thing a person could act on anyway.
def _answered(result_count: Any) -> bool:
    return int(result_count or 0) > 0


@dataclass
class Interaction:
    """One observed call, as the mining rules see it.

    Carries its own ``event_id`` so a proposal can name the rows it was
    derived from. Without that a candidate is an assertion with a count
    attached and no way to check it --- a reviewer looking at
    ``router -> pheasant-flock`` can see that it was seen four times and
    nothing at all about what was asked or what came back.
    """

    event_id: str
    query: str
    node_ids: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    started_at: str = ""


@dataclass
class SessionObservations:
    """One principal's interactions within one session.

    Keyed by ``(session_id, principal)`` rather than by session alone. A
    session id is caller-asserted, so two principals *can* claim one, and a
    digest that mixed them would be readable by whichever writer happened to
    own the record --- an ACL leak reached through a field nobody
    authenticates. Splitting them is the conservative reading and costs
    nothing when, as normally, a session has one principal.
    """

    session_id: str
    principal: str | None
    modalities: set[str] = field(default_factory=set)
    queries: list[str] = field(default_factory=list)
    paths: Counter[str] = field(default_factory=Counter)
    gaps: list[str] = field(default_factory=list)
    #: One entry per observed call, in dialog order.
    #:
    #: Per *interaction*, not per session, because the mining rules ask
    #: questions of the form "what did **this** query retrieve" --- an alias is
    #: a claim about one word and the documents that word found, and rolling a
    #: session's hits together would attribute every document to every word
    #: anyone typed in that session.
    interactions: list[Interaction] = field(default_factory=list)
    observations: int = 0
    first_seen: str = ""
    last_seen: str = ""


def _candidate_sessions(state: Any, *, min_observations: int, limit: int) -> list[str]:
    """Session ids worth digesting, most recently active first.

    A separate, aggregate-only query so the pass is bounded *before* any row
    bodies are read. Selecting rows first and truncating them would cut a
    session in half, and a half-digest changes on the next pass and supersedes
    itself forever --- churn that looks like activity.

    Most recently active first because a backlog should drain from the live
    end: a session someone is still in is worth more than one that ended
    yesterday and will be identical whenever it is reached.
    """

    rows = state.rows(
        "SELECT session_id, MAX(started_at) AS last_seen FROM interaction_events "
        "WHERE session_id IS NOT NULL AND session_id <> '' "
        "GROUP BY session_id HAVING COUNT(*) >= ? "
        "ORDER BY MAX(started_at) DESC, session_id LIMIT ?",
        (int(min_observations), int(limit)),
    )
    return [str(row["session_id"]) for row in rows]


def _rows(state: Any, session_ids: list[str]) -> list[Any]:
    """Every ledger row for the named sessions, in dialog order.

    ``ORDER BY started_at`` is what makes the digest's "asked about" list the
    order the session actually asked in --- and deterministic, which the record
    id depends on.
    """

    if not session_ids:
        return []
    placeholders = ",".join("?" for _ in session_ids)
    return state.rows(
        "SELECT id, session_id, principal, modality, query_text, result_ids_json, "
        "result_paths_json, result_count, top_score, started_at "
        f"FROM interaction_events WHERE session_id IN ({placeholders}) "
        "ORDER BY started_at, id",
        tuple(session_ids),
    )


def collect_sessions(
    state: Any, *, min_observations: int, max_sessions: int
) -> list[SessionObservations]:
    """Group ledger rows into per-(session, principal) evidence.

    Grouped in Python rather than SQL because the interesting parts --- the
    distinct queries in order, the path frequencies, which calls answered
    nothing --- are list-shaped, and expressing them as portable SQL across
    SQLite and Postgres would cost more than it saves on a hot window bounded
    to days.
    """

    candidates = _candidate_sessions(state, min_observations=min_observations, limit=max_sessions)
    grouped: dict[tuple[str, str | None], SessionObservations] = {}
    for row in _rows(state, candidates):
        session_id = str(row["session_id"])
        principal = row["principal"] or None
        key = (session_id, principal)
        entry = grouped.get(key)
        if entry is None:
            entry = SessionObservations(session_id=session_id, principal=principal)
            grouped[key] = entry
        entry.observations += 1
        entry.modalities.add(str(row["modality"] or ""))
        started = str(row["started_at"] or "")
        entry.first_seen = entry.first_seen or started
        entry.last_seen = max(entry.last_seen, started)

        query = (row["query_text"] or "").strip()
        if query and query not in entry.queries:
            entry.queries.append(query)
        paths: list[str] = []
        node_ids: list[str] = []
        try:
            paths = [str(path) for path in json.loads(row["result_paths_json"] or "[]")]
            node_ids = [str(node) for node in json.loads(row["result_ids_json"] or "[]")]
        except (TypeError, ValueError):
            pass
        entry.paths.update(paths)
        if query:
            entry.interactions.append(
                Interaction(
                    event_id=str(row["id"]),
                    query=query,
                    node_ids=node_ids,
                    paths=paths,
                    started_at=started,
                )
            )
        # A question that returned nothing is the most useful thing a session
        # can record: it is what the region could not answer.
        if query and not _answered(row["result_count"]) and query not in entry.gaps:
            entry.gaps.append(query)

    ready = [entry for entry in grouped.values() if entry.observations >= min_observations]
    # Most recently active first, then by id so a tie is not storage order.
    ready.sort(key=lambda item: (item.last_seen, item.session_id), reverse=True)
    return ready[:max_sessions]


def digest_text(session: SessionObservations) -> str:
    """The record's body: a fixed template over sorted inputs.

    Deterministic by construction --- every list is truncated after an explicit
    sort, never left in insertion or storage order --- because the record id is
    a digest of this string. Two passes over an unchanged session must produce
    the same bytes, or every beat would write a new record superseding the last
    and the chain would grow without bound.

    Written to be read by a person in the Memory tab, and by a model as
    *recorded assertion* rather than instruction, which is what the answering
    prompt already says of any remembered passage.
    """

    modality = "/".join(sorted(m for m in session.modalities if m)) or "unknown"
    lines = [
        f"Session {session.session_id} ({modality}): "
        f"{session.observations} interactions "
        f"from {session.first_seen or 'unknown'} to {session.last_seen or 'unknown'}."
    ]

    if session.queries:
        lines.append("")
        lines.append("Asked about:")
        # Insertion order is the order of the session's own dialog, which is
        # the meaningful order here and is already deterministic: the rows are
        # read `ORDER BY started_at`.
        lines.extend(f"- {query}" for query in session.queries[:MAX_QUERIES])

    if session.paths:
        lines.append("")
        lines.append("Most-consulted:")
        # Count descending, then path ascending: `Counter.most_common` alone
        # leaves ties in insertion order.
        ranked = sorted(session.paths.items(), key=lambda item: (-item[1], item[0]))
        lines.extend(f"- {path} ({count})" for path, count in ranked[:MAX_PATHS])

    if session.gaps:
        lines.append("")
        lines.append("Found nothing for:")
        lines.extend(f"- {gap}" for gap in session.gaps[:MAX_GAPS])

    return "\n".join(lines)


def _current_digest(
    records: list[MemoryRecord], session: SessionObservations
) -> MemoryRecord | None:
    """The session's live digest, if it already has one.

    Matched on ``(scope, subject, written_by)`` --- the same triple the record
    id is derived from --- so a second principal claiming the same session id
    gets its own chain rather than superseding somebody else's.
    """

    for record in records:
        if (
            record.scope == "session"
            and record.subject == session.session_id
            and (record.written_by or None) == session.principal
            and SESSION_DIGEST_RULE in (record.tags or ())
        ):
            return record
    return None


def run_session_digests(
    engine: Any,
    *,
    now: datetime | None = None,
    records: list[MemoryRecord] | None = None,
) -> dict[str, Any]:
    """Write or refine one digest per session. Returns a report.

    Refinement is supersession: when a session's evidence has changed, the new
    digest names the old one, so ``current_only`` still returns exactly one
    record per session and ``as_of`` can read what the session looked like an
    hour ago.

    When nothing changed, nothing is written. **Two independent guards make
    that true**, and it is worth saying which does what:

    * this short-circuit, which is a *cost* guard --- the common case is a
      store full of sessions nobody has touched since the last pass, and a
      fold still costs a file stat and a projection read per session;
    * the store's own dedup, which is the *correctness* one. A record's id is
      a digest of ``scope|subject|text`` (plus kind/writer), and ``supersedes``
      is deliberately **not** part of it --- so an unchanged digest re-derives
      the id it already wrote and the append collapses onto the existing file.

    Recorded because mutation testing says so: deleting the short-circuit
    leaves every test here green, and correctly so. It is not dead code, it is
    the cheap guard in front of the sound one.
    """

    config = engine.config
    settings = getattr(getattr(config, "memory", None), "formation", None)
    if settings is None or not getattr(settings, "enabled", False):
        return {}
    if not getattr(settings, "session_digest", True):
        return {}
    if SESSION_DIGEST_RULE not in (getattr(settings, "rules", None) or []):
        return {}
    source = memory_source(config, getattr(engine, "state", None))
    if source is None:
        return {}

    state = engine.state
    sessions = collect_sessions(
        state,
        min_observations=max(1, int(getattr(settings, "min_observations", 3))),
        max_sessions=max(1, int(getattr(settings, "max_candidates_per_pass", 50))),
    )
    if not sessions:
        return {}

    store = MemoryStore(source.path)
    live = records if records is not None else store.list_records(current_only=True)
    # `records` may be the whole store when a caller already parsed it; the
    # digest lookup needs current records only, and re-filtering here is
    # cheaper than a second glob.
    superseded = MemoryStore.superseded_ids(live)
    current = [record for record in live if record.record_id not in superseded]

    written: list[str] = []
    refined: list[str] = []
    unchanged = 0
    for session in sessions:
        text = digest_text(session)
        existing = _current_digest(current, session)
        if existing is not None and existing.text.strip() == text.strip():
            unchanged += 1
            continue
        try:
            record, created = store.append(
                text,
                scope="session",
                subject=session.session_id,
                supersedes=existing.record_id if existing is not None else None,
                tags=(SESSION_DIGEST_RULE, FORMED_TAG),
                written_by=session.principal,
                now=now,
            )
        except ValueError:
            # A session id that cannot be a subject (a newline, say) is caller
            # data on an unauthenticated surface. Skip it; never fail the beat.
            logger.debug("Skipped a session digest with an unusable subject", exc_info=True)
            continue
        if not created:
            unchanged += 1
            continue
        (refined if existing is not None else written).append(record.record_id)

    report: dict[str, Any] = {"rule_id": SESSION_DIGEST_RULE, "sessions": len(sessions)}
    if written:
        report["created"] = written
    if refined:
        report["refined"] = refined
    if unchanged:
        report["unchanged"] = unchanged
    return report


# --------------------------------------------------------------------------
# Candidates: what the region proposes, before anything admits it
# --------------------------------------------------------------------------

ALIAS_RULE = "alias-cooccurrence-v1"
PATH_RULE = "path-affinity-v1"
GAP_RULE = "retrieval-gap-v1"

#: Rules that mint candidates, as opposed to the session digest, which writes
#: directly because its scope confines it. Ordered so a pass is reproducible.
CANDIDATE_RULES = (ALIAS_RULE, PATH_RULE, GAP_RULE)

#: How many contributing interactions a proposal names.
#:
#: A cap, because the id list rides in the candidate row and a token asked
#: four hundred times would otherwise carry four hundred ids. The head of the
#: list is what a reviewer reads; the counts beside it remain the true totals.
MAX_EVIDENCE_EVENTS = 20

#: A path prefix shorter than this is not an affinity, it is the corpus.
#: `preference` rules matching `/` would re-rank every query in the region.
MIN_PREFIX_SEGMENTS = 1

#: How much of a query a gap candidate quotes. A gap is a report a person
#: reads; the whole of a rambling question is not more informative than its
#: first line.
MAX_GAP_QUERY_CHARS = 200


def params_hash(settings: Any) -> str:
    """The parameters a candidate was proposed under.

    Part of the candidate id, so tightening a threshold proposes *new*
    candidates rather than silently rewriting the evidence behind ones a person
    has already seen --- the property `memory_compactions.params_hash` gives
    compaction, reached the same way.
    """

    payload = "|".join(
        str(getattr(settings, name, ""))
        for name in ("min_observations", "min_sessions", "max_candidates_per_pass")
    )
    return blake2b(payload.encode(), digest_size=8).hexdigest()


def candidate_id(
    rule_id: str, scope: str, subject: str | None, kind: str, text: str, digest: str
) -> str:
    """Content-addressed, so a re-derived proposal updates rather than piles up."""

    from pheasant.artifacts.normalize import normalized_text

    payload = f"{rule_id}|{scope}|{subject or ''}|{kind}|{normalized_text(text)}|{digest}"
    return blake2b(payload.encode(), digest_size=16).hexdigest()


@dataclass
class Candidate:
    """One proposal. Deliberately not a record: nothing here is memory yet."""

    rule_id: str
    scope: str
    kind: str
    text: str
    subject: str | None = None
    written_by: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    observations: int = 1
    sessions: int = 1
    first_seen: str = ""
    last_seen: str = ""

    def row(self, digest: str) -> dict[str, Any]:
        return {
            "id": candidate_id(
                self.rule_id, self.scope, self.subject, self.kind, self.text, digest
            ),
            "rule_id": self.rule_id,
            "params_hash": digest,
            "scope": self.scope,
            "subject": self.subject,
            "kind": self.kind,
            "text": self.text,
            "written_by": self.written_by,
            "evidence_json": json.dumps(self.evidence, sort_keys=True),
            "observations": self.observations,
            "sessions": self.sessions,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


def _tokens(query: str) -> list[str]:
    """The search path's own tokenizer.

    Not a second one: an alias rule minted from tokens that split differently
    from the way the query arm splits would produce a trigger that never fires,
    and `steering` matches on the same grammar.
    """

    from pheasant.search.sqlite_store import _query_tokens

    return _query_tokens(query)


def _path_prefix(paths: list[str]) -> str | None:
    """The longest directory prefix every path shares, or None.

    Cut at a separator, never mid-segment: `docs/deploy.md` and
    `docs/deployment.md` share the characters `docs/deploy`, which is not a
    directory and would produce a `preference` rule matching neither file the
    way an operator expects.
    """

    if len(paths) < 2:
        return None
    segments = [path.split("/")[:-1] for path in paths]
    if not all(segments):
        return None
    shared: list[str] = []
    for parts in zip(*segments, strict=False):
        if len(set(parts)) != 1:
            break
        shared.append(parts[0])
    if len(shared) < MIN_PREFIX_SEGMENTS:
        return None
    return "/".join(shared) + "/"


def _chunk_texts(state: Any, node_ids: list[str]) -> dict[str, str]:
    """Body text for retrieved hits, keyed by the id the ledger recorded.

    This is the join `result_ids` exists for: an alias rule cannot ask "did the
    document that matched actually contain this word" without the document.
    """

    if not node_ids:
        return {}
    placeholders = ",".join("?" for _ in node_ids)
    texts: dict[str, str] = {}
    for row in state.rows(
        f"SELECT artifact_id, text FROM chunks WHERE artifact_id IN ({placeholders})",
        tuple(node_ids),
    ):
        key = str(row["artifact_id"])
        texts[key] = (texts.get(key, "") + " " + str(row["text"] or "")).strip().lower()
    return texts


def mine_aliases(sessions: list[SessionObservations], state: Any, settings: Any) -> list[Candidate]:
    """Propose ``x -> y`` where a query word never appears in what it found.

    The signal: people keep asking with a word the corpus does not use. If a
    token retrieves documents and appears in *none* of them, the match came
    from the rest of the query --- so the token is the team's vocabulary and
    the documents' own frequent term is the corpus's. That pair is an alias.

    Aliases are the one part of memory that improves queries returning **no
    memory at all**, and the config-level ablation measured team-vocabulary
    queries moving 0.029 to 0.467 while control queries moved by 0.000. This is
    the rule that finds them without anyone writing them by hand.
    """

    min_observations = max(1, int(getattr(settings, "min_observations", 3)))
    min_sessions = max(1, int(getattr(settings, "min_sessions", 2)))

    # token -> the hits *its own queries* retrieved, and who asked.
    hits: dict[str, list[str]] = {}
    seen_in: dict[str, set[str]] = {}
    events: dict[str, list[str]] = {}
    asked: Counter[str] = Counter()
    for session in sessions:
        for call in session.interactions:
            for token in _tokens(call.query):
                asked[token] += 1
                seen_in.setdefault(token, set()).add(session.session_id)
                hits.setdefault(token, []).extend(call.node_ids)
                events.setdefault(token, []).append(call.event_id)
    if not asked:
        return []

    # One lookup covering every id any candidate token could need.
    texts = _chunk_texts(state, sorted({node for ids in hits.values() for node in ids}))

    out: list[Candidate] = []
    for token in sorted(asked):
        if asked[token] < min_observations or len(seen_in[token]) < min_sessions:
            continue
        bodies = [texts[node] for node in hits[token] if node in texts]
        if not bodies:
            continue
        # The token is absent from everything it retrieved: whatever matched,
        # it was not this word.
        if any(token in body for body in bodies):
            continue
        # Absent *and* not merely a different form of a word they do use.
        if _is_inflection(token, bodies):
            continue
        target = _dominant_term(bodies, exclude={token}, state=state)
        if target is None:
            continue
        out.append(
            Candidate(
                rule_id=ALIAS_RULE,
                scope="org",
                kind="alias",
                text=f"{token} -> {target}",
                evidence={
                    "token": token,
                    "target": target,
                    "asked": asked[token],
                    "sessions": sorted(seen_in[token]),
                    "event_ids": events[token][:MAX_EVIDENCE_EVENTS],
                },
                observations=asked[token],
                sessions=len(seen_in[token]),
                first_seen=min(s.first_seen for s in sessions if s.first_seen),
                last_seen=max(s.last_seen for s in sessions if s.last_seen),
            )
        )
    return out


def _dominant_term(bodies: list[str], *, exclude: set[str], state: Any = None) -> str | None:
    """The most *distinctive* term every retrieved document shares.

    Presence in **all** of them, not most: an alias is a claim that two words
    mean the same thing, and a term two documents out of three happen to share
    is a coincidence.

    Being universal is not enough on its own, though, and the first version of
    this proved it: on a two-document fixture it proposed ``router -> before``,
    because "before traffic shifts" and "before promotion" both contain the
    word and it sorted first alphabetically. Alphabetical order is not a
    signal.

    So the pick is by **corpus document frequency**, rarest first --- the same
    IDF the ranking arm scores with, read from the same term/document-frequency
    table (`corpus_vocabulary`), so "distinctive" means the same thing to
    formation as it does to search. A term the whole corpus uses is a function
    word; a term a handful of documents use is the corpus's own name for
    something, which is exactly what an alias should point at.

    Falls back to lexical order when no vocabulary is available (an empty
    corpus, or an FTS build without `fts5vocab`), which is the pre-existing
    behaviour rather than a crash.
    """

    from pheasant.search.sqlite_store import _STOPWORDS

    counts: Counter[str] = Counter()
    for body in bodies:
        seen = {
            token
            for token in re.findall(r"[a-z][a-z0-9_-]{2,}", body)
            if token not in _STOPWORDS and token not in exclude
        }
        counts.update(seen)
    universal = sorted(term for term, count in counts.items() if count == len(bodies))
    # More than a handful shared by everything means the documents are simply
    # similar, not that any one term is the alias.
    if not 1 <= len(universal) <= MAX_UNIVERSAL_TERMS:
        return None

    frequencies = _corpus_frequencies(state)
    if not frequencies:
        return universal[0]
    # Rarest in the corpus wins; an unlisted term is rarer than any listed one,
    # since `corpus_vocabulary` returns only the most widely used. Lexical
    # order breaks ties so the proposal is reproducible.
    return min(universal, key=lambda term: (frequencies.get(term, -1), term))


#: More universal terms than this and the documents are simply similar --- no
#: one of them is the alias.
MAX_UNIVERSAL_TERMS = 8


def _corpus_frequencies(state: Any) -> dict[str, int]:
    """``term -> document count`` for the corpus's most widely used terms."""

    if state is None:
        return {}
    try:
        from pheasant.search.sqlite_store import corpus_vocabulary

        return {term: doc for term, doc in corpus_vocabulary(state, limit=512)}
    except Exception:  # noqa: BLE001 - a missing vocabulary is not a failure
        logger.debug("Corpus vocabulary unavailable for alias ranking", exc_info=True)
        return {}


#: How much of a word two forms must share before they count as the same word.
#: Cheap stand-in for a stemmer, and it only ever *suppresses* a proposal, so
#: being approximate costs a missed alias rather than a wrong one.
_INFLECTION_PREFIX = 5


def _is_inflection(token: str, bodies: list[str]) -> bool:
    """Is the token simply a different form of a word the documents do use?

    Without this the rule proposes things like ``coordination -> check``:
    "coordination" is literally absent from documents that say "coordinates",
    so the absence test passes and the rule reaches for whatever term the
    documents happen to share. That is an inflection, not a vocabulary gap,
    and an alias claiming it would expand every such query with a word nobody
    meant.

    Measured on a fixture where exactly that proposal appeared, and disappears
    with this guard while ``router -> pheasant-flock`` --- a real alias, where
    the corpus genuinely uses another word --- survives.
    """

    if len(token) < _INFLECTION_PREFIX:
        return False
    stem = token[:_INFLECTION_PREFIX]
    for body in bodies:
        for term in re.findall(r"[a-z][a-z0-9_-]{2,}", body):
            if term != token and term.startswith(stem):
                return True
    return False


def mine_path_affinity(sessions: list[SessionObservations], settings: Any) -> list[Candidate]:
    """Propose ``when: <token> -> prefer: <dir>/`` for a query family that
    consistently lands in one place.

    A path prior an operator would otherwise have to notice and write. The
    prefix is cut at a directory boundary, and a single-path "affinity" is not
    one --- one document is a hit, not a pattern.
    """

    min_observations = max(1, int(getattr(settings, "min_observations", 3)))
    min_sessions = max(1, int(getattr(settings, "min_sessions", 2)))

    landed: dict[str, list[str]] = {}
    seen_in: dict[str, set[str]] = {}
    events: dict[str, list[str]] = {}
    asked: Counter[str] = Counter()
    for session in sessions:
        for call in session.interactions:
            if not call.paths:
                continue
            for token in _tokens(call.query):
                asked[token] += 1
                seen_in.setdefault(token, set()).add(session.session_id)
                landed.setdefault(token, []).extend(call.paths)
                events.setdefault(token, []).append(call.event_id)

    out: list[Candidate] = []
    for token in sorted(asked):
        if asked[token] < min_observations or len(seen_in[token]) < min_sessions:
            continue
        prefix = _path_prefix(sorted(set(landed[token])))
        if prefix is None:
            continue
        out.append(
            Candidate(
                rule_id=PATH_RULE,
                scope="org",
                kind="preference",
                text=f"when: {token} -> prefer: {prefix}",
                evidence={
                    "token": token,
                    "prefix": prefix,
                    "paths": sorted(set(landed[token]))[:MAX_PATHS],
                    "sessions": sorted(seen_in[token]),
                    "event_ids": events[token][:MAX_EVIDENCE_EVENTS],
                },
                observations=asked[token],
                sessions=len(seen_in[token]),
                first_seen=min(s.first_seen for s in sessions if s.first_seen),
                last_seen=max(s.last_seen for s in sessions if s.last_seen),
            )
        )
    return out


def mine_gaps(sessions: list[SessionObservations], settings: Any) -> list[Candidate]:
    """Propose a record naming a question the corpus keeps failing to answer.

    The honest form of "more usage expands the knowledge". Usage cannot conjure
    facts the corpus lacks; what it can do is say, with evidence, which
    questions keep going unanswered --- which is a thing worth remembering, and
    a thing an operator can act on.

    `org` scope and no writer: a gap is a property of the corpus, not of the
    person who happened to hit it.
    """

    min_observations = max(1, int(getattr(settings, "min_observations", 3)))
    min_sessions = max(1, int(getattr(settings, "min_sessions", 2)))

    asked: Counter[str] = Counter()
    seen_in: dict[str, set[str]] = {}
    events: dict[str, list[str]] = {}
    first: dict[str, str] = {}
    last: dict[str, str] = {}
    for session in sessions:
        by_query = {call.query: call.event_id for call in session.interactions}
        for gap in session.gaps:
            key = gap.strip()
            if not key:
                continue
            asked[key] += 1
            seen_in.setdefault(key, set()).add(session.session_id)
            if key in by_query:
                events.setdefault(key, []).append(by_query[key])
            first[key] = min(first.get(key, session.first_seen), session.first_seen)
            last[key] = max(last.get(key, ""), session.last_seen)

    out: list[Candidate] = []
    for query in sorted(asked):
        if asked[query] < min_observations or len(seen_in[query]) < min_sessions:
            continue
        quoted = query[:MAX_GAP_QUERY_CHARS]
        out.append(
            Candidate(
                rule_id=GAP_RULE,
                scope="org",
                kind="fact",
                subject="retrieval-gaps",
                text=(
                    f'Nothing indexed answers "{quoted}". '
                    f"Asked {asked[query]} times across {len(seen_in[query])} sessions "
                    f"with no result above the score threshold."
                ),
                evidence={
                    "query": quoted,
                    "asked": asked[query],
                    "sessions": sorted(seen_in[query]),
                    "event_ids": events.get(query, [])[:MAX_EVIDENCE_EVENTS],
                },
                observations=asked[query],
                sessions=len(seen_in[query]),
                first_seen=first[query],
                last_seen=last[query],
            )
        )
    return out


# --------------------------------------------------------------------------
# Admission: the one crossing from evidence into memory
# --------------------------------------------------------------------------


def run_candidate_rules(engine: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Mine the observation plane and record what the rules propose.

    Nothing here writes memory. Every rule produces *candidates*, and a
    candidate becomes a record only through :func:`admit`, which goes through
    ``MemoryStore.append`` like any other write.
    """

    config = engine.config
    settings = getattr(getattr(config, "memory", None), "formation", None)
    if settings is None or not getattr(settings, "enabled", False):
        return {}
    enabled = set(getattr(settings, "rules", None) or [])
    if not enabled & set(CANDIDATE_RULES):
        return {}

    state = engine.state
    sessions = collect_sessions(
        state,
        # Deliberately 1, not `min_observations`: that threshold is about how
        # often a *pattern* recurs, and a rule counts across sessions. Filtering
        # short sessions out here would hide the very cross-session agreement
        # `min_sessions` is asking for.
        min_observations=1,
        max_sessions=max(1, int(getattr(settings, "max_candidates_per_pass", 50)) * 10),
    )
    if not sessions:
        return {}

    digest = params_hash(settings)
    proposed: list[Candidate] = []
    if ALIAS_RULE in enabled:
        proposed.extend(mine_aliases(sessions, state, settings))
    if PATH_RULE in enabled:
        proposed.extend(mine_path_affinity(sessions, settings))
    if GAP_RULE in enabled:
        proposed.extend(mine_gaps(sessions, settings))

    # Strongest evidence first, then by id: a bounded pass should keep the
    # best proposals, not the ones that happened to be mined first.
    proposed.sort(key=lambda c: (-c.observations, -c.sessions, c.rule_id, c.text))
    limit = max(1, int(getattr(settings, "max_candidates_per_pass", 50)))

    opened: list[str] = []
    for candidate in proposed[:limit]:
        row = candidate.row(digest)
        try:
            if state.upsert_memory_candidate(row):
                opened.append(row["id"])
        except Exception:  # noqa: BLE001 - one bad proposal must not end the pass
            logger.debug("Could not record a memory candidate", exc_info=True)

    report: dict[str, Any] = {"proposed": len(proposed), "open": len(opened)}
    if getattr(settings, "auto_admit", False):
        report["admitted"] = auto_admit(engine, now=now)
    return report


def admit(
    engine: Any,
    candidate_id_: str,
    *,
    admitted_by: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Promote one candidate into a real memory record.

    **The crossing.** It goes through ``MemoryStore.append`` --- the same call a
    person or an agent makes --- so the record is an ordinary file indexed by
    the ordinary pipeline, and memory's "no second ingestion path" invariant
    holds for a formed record exactly as it does for a written one.

    The candidate is marked decided **after** the write, deliberately: a
    failed write has to leave it pending so it can be tried again, rather than
    consuming the proposal and leaving nothing behind.
    """

    state = engine.state
    candidate = state.get_memory_candidate(candidate_id_)
    if candidate is None:
        raise KeyError(f"Unknown memory candidate: {candidate_id_}")
    if candidate["status"] != "pending":
        raise ValueError(
            f"candidate {candidate_id_} is already {candidate['status']}; "
            "a decision is final, and re-deciding would write a second record"
        )
    source = memory_source(engine.config, state)
    if source is None:
        raise ValueError("no `type: memory` source is configured to admit into")

    store = MemoryStore(source.path)
    record, created = store.append(
        str(candidate["text"]),
        scope=str(candidate["scope"]),
        subject=candidate["subject"],
        kind=str(candidate["kind"] or "fact"),
        tags=(str(candidate["rule_id"]), FORMED_TAG),
        written_by=candidate["written_by"],
        now=now,
    )
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds").replace("+00:00", "Z")
    state.decide_memory_candidate(
        candidate_id_,
        status="admitted",
        admitted_by=admitted_by,
        record_id=record.record_id,
        when=stamp,
    )
    return {
        "candidate_id": candidate_id_,
        "record_id": record.record_id,
        "created": created,
        "admitted_by": admitted_by,
    }


def reject(
    engine: Any, candidate_id_: str, *, rejected_by: str, now: datetime | None = None
) -> dict[str, Any]:
    """Decline a proposal, permanently.

    A rejection is a decision, and the upsert guard means the rule that
    proposed it cannot re-suggest it on the next beat. Re-proposing what
    somebody has already declined is the fastest way to make a review queue
    worth ignoring.
    """

    state = engine.state
    candidate = state.get_memory_candidate(candidate_id_)
    if candidate is None:
        raise KeyError(f"Unknown memory candidate: {candidate_id_}")
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds").replace("+00:00", "Z")
    decided = state.decide_memory_candidate(
        candidate_id_, status="rejected", admitted_by=rejected_by, when=stamp
    )
    return {"candidate_id": candidate_id_, "rejected": decided}


def auto_admit(engine: Any, *, now: datetime | None = None) -> list[str]:
    """Admit every open candidate without review.

    Off by default, and the same posture `compaction_enabled` takes: this
    changes what a *default* query returns, which is a decision an operator
    should make rather than inherit. An auto-admitted record still carries the
    `formed` tag and its candidate still records `admitted_by`, so a
    machine-formed record is never indistinguishable from a written one.
    """

    state = engine.state
    admitted: list[str] = []
    for candidate in state.list_memory_candidates(status="pending"):
        try:
            result = admit(
                engine, candidate["id"], admitted_by=f"rule:{candidate['rule_id']}", now=now
            )
        except (KeyError, ValueError):
            continue
        admitted.append(result["record_id"])
    return admitted


def expire_candidates(engine: Any, *, now: datetime | None = None) -> int:
    """Retire proposals nobody acted on, per ``candidate_ttl_days``."""

    settings = getattr(getattr(engine.config, "memory", None), "formation", None)
    days = int(getattr(settings, "candidate_ttl_days", 30) or 0)
    if days <= 0:
        return 0
    from datetime import timedelta

    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    stamp = cutoff.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return engine.state.expire_memory_candidates(older_than=stamp)
