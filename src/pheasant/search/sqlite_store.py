from __future__ import annotations

import logging
import math
import re
import time
from typing import Any

from pheasant.persistence.state_store import StateStore
from pheasant.search.ranking import DEFAULT_RANKING, RankingParameters

logger = logging.getLogger(__name__)

# --- ranking -------------------------------------------------------------
#
# BM25 column weights, positionally matching the chunks_fts schema:
#   chunk_id, source_id, artifact_id (UNINDEXED), title, path, heading_path, text
#
# `title` holds the file's BASENAME (see StateStore._fts_title); `path` is the
# full relative path. Weighting them above `text` is what makes "readme" find
# the file *named* README rather than the file that happens to say "readme"
# most often in a short body — untuned, a filename match was worth exactly one
# body word, and BM25's length normalization then ranked by body brevity.
# The values now live on `RankingParameters`, whose defaults are these exact
# numbers; `bm25_weights` renders this same string. They stayed constants for
# as long as they were the output of a one-off tuning pass and stopped being
# constants when a plane appeared whose job is to propose better ones.
_BM25_WEIGHTS = DEFAULT_RANKING.bm25_weights

# The same four weights for Postgres. `ts_rank_cd` takes exactly four weight
# classes and its array is ordered {D, C, B, A} — the reverse of how the
# columns read above — so this is 1/2/3/8 written backwards and normalized onto
# ts_rank_cd's 0-1 scale (divide by 8, the title weight). Four BM25 columns
# mapping onto exactly four tsvector weight classes is luck, not design; it is
# what makes the port preserve the *ordering* of field importance rather than
# having to collapse two fields together.
_TS_RANK_WEIGHTS = DEFAULT_RANKING.ts_rank_weights

# ts_rank_cd's normalization flag. **32 = rank / (rank + 1)** — term-frequency
# *saturation*, applied per term.
#
# This is the second half of making Postgres rank like BM25, and it is not a
# tuning knob. BM25 scores a term as `IDF x tf/(tf + k1)`: the tenth occurrence
# of a word is worth almost nothing more than the third. `ts_rank_cd` grows
# roughly linearly with occurrences, so without saturation a long document that
# repeats a common query word outranks the short document actually about the
# rare one — even once IDF is applied, because linear growth outruns a 2.6x
# weight difference.
#
# Measured on the parity corpus, summing IDF-weighted per-term ranks for
# "gateway restarts nightly" (correct answer: runbook.md):
#
#   flag  0 (none)          notes.md 1.50   > runbook.md 0.375   WRONG
#   flag  1 (log length)    notes.md 0.199  > runbook.md 0.132   WRONG
#   flag 33 (1|32)          notes.md 0.145  > runbook.md 0.127   WRONG
#   flag 32 (saturation)    runbook.md 0.368 > notes.md 0.323    correct
#
# Length normalization is deliberately *not* combined in (33 above): saturation
# already defuses repetition, and dividing by document length on top penalizes
# the document that matched two of the three query terms.
_TS_RANK_NORMALIZATION = 32

# Postgres has to evaluate ``ts_rank_cd`` against the stored position list for
# every candidate row.  An OR query made from a planner's whole sentence can
# admit most of a code corpus ("main", framework names, and so on), then pay
# that cost once per query term.  Use the rarest terms to choose candidates
# while retaining a wider, bounded set for final IDF-weighted ranking.  Queries
# of four terms or fewer are unchanged.
_POSTGRES_MAX_MATCH_TERMS = 4
_POSTGRES_MAX_RANK_TERMS = 12

# Structural priors, applied as a DIVISOR on the (negative) BM25 cost, so they
# scale a match rather than displacing it: a strong deep hit still beats a weak
# shallow one, but ties break toward the more central file. An additive penalty
# was tried first and measurably hurt legitimately-deep code (a checkpoint
# implementation went from rank 34 to 43) while fixing the same document
# queries, so proportional it is.
#
#   depth  — 412 files in the demo corpus are named README.md, so their
#            basenames are textually identical and BM25 *cannot* separate them.
#            The signal that the repository's own README is the one you meant
#            is structural, not lexical: it sits at the root.
#   tests  — "where is X implemented" should not return X's test suite first.
#            Both the lexical and the vector arm ranked tests/ above the
#            implementation for every code query measured.
#   samples— same, one notch softer: sample code is often a legitimate answer.
#
# The three coefficients are `RankingParameters.depth_prior` / `test_prior` /
# `sample_prior`, whose defaults are 0.05 / 0.60 / 0.30 — the measured values
# this comment describes. `_structural_prior_sql` renders the expression;
# `tests/test_ranking_parameters.py` pins the default rendering against the
# literal string this used to be.


def _structural_prior_sql(ranking: RankingParameters) -> str:
    """The divisor expression, rendered from one parameter point.

    Interpolated into SQL rather than parameterized, and safely: every value
    is a `float` that has already been through `clamp`, so the rendering is
    `%g` of a bounded number and no caller-controlled text reaches the
    statement. A placeholder would be portable but would also add three
    positional parameters to an already order-sensitive assembly, in the one
    expression that appears before every `WHERE` clause.
    """

    return f"""(
    1.0
    + {ranking.depth_prior} * (length(artifacts.relative_path)
                        - length(replace(artifacts.relative_path, '/', '')))
    + CASE WHEN artifacts.relative_path LIKE '%/tests/%'
             OR artifacts.relative_path LIKE 'tests/%'
             OR artifacts.relative_path LIKE '%/test_%'
             OR artifacts.relative_path LIKE 'test_%'
             OR artifacts.relative_path LIKE '%_test.%'
           THEN {ranking.test_prior} ELSE 0.0 END
    + CASE WHEN artifacts.relative_path LIKE '%/samples/%'
             OR artifacts.relative_path LIKE 'samples/%'
             OR artifacts.relative_path LIKE '%/examples/%'
             OR artifacts.relative_path LIKE 'examples/%'
           THEN {ranking.sample_prior} ELSE 0.0 END
)"""


# How much a `preference` rule is worth is `RankingParameters.prefer_bonus`
# (0.35). The prior is a *divisor* on a negative BM25 cost, so subtracting
# from it makes a match rank better. Sized to outweigh roughly six levels of
# path depth (`depth_prior` 0.05) — enough that "prefer docs/" actually moves
# a deep document above a shallow one, small enough that it cannot promote an
# irrelevant match over a strong one.
#
# `prior_floor` (0.25) is the floor on the divisor. It must stay positive or
# the ranking inverts, and a caller with several preference rules could
# otherwise drive it to zero — which is why `ranking.BOUNDS` refuses to clamp
# it below 0.01 however a config file or a proposed bundle spells it.


def _structural_prior(
    steering: Any,
    tokens: list[str],
    *,
    postgres: bool = False,
    ranking: RankingParameters = DEFAULT_RANKING,
) -> tuple[str, list[object]]:
    """The ranking divisor, plus any `preference` bonus in force.

    Returns SQL and its params, because a preference is a per-query LIKE
    pattern and cannot live in the module-level constant.

    ``MAX(a, b)`` is a *scalar* two-argument function in SQLite and an
    *aggregate* in Postgres, where the scalar spelling is ``GREATEST``. Left
    untranslated this raises rather than silently mis-ranking, but only for
    callers who have a `preference` rule in force — which is a narrow enough
    path that it could sit broken for a long time unnoticed.
    """
    prior = _structural_prior_sql(ranking)
    if not steering:
        return prior, []
    prefixes = steering.prefers(tokens)
    if not prefixes:
        return prior, []
    cases = " ".join("WHEN artifacts.relative_path LIKE ? THEN 1 " for _ in prefixes)
    params: list[object] = [f"{prefix.rstrip('/*')}%" for prefix in prefixes]
    greatest = "GREATEST" if postgres else "MAX"
    return (
        f"{greatest}({ranking.prior_floor}, "
        f"{prior} - {ranking.prefer_bonus} * (CASE {cases} ELSE 0 END))",
        params,
    )


def _exclusion_sql(steering: Any) -> tuple[str, list[object]]:
    """`AND relative_path NOT LIKE ?` per `exclusion` rule."""
    if not steering or not steering.exclusions:
        return "", []
    clauses = "".join(
        " AND COALESCE(artifacts.relative_path, '') NOT LIKE ?" for _ in steering.exclusions
    )
    params: list[object] = [f"%{pattern.strip('*').strip('/')}%" for pattern in steering.exclusions]
    return clauses, params


def _postgres_document_frequencies(
    state: Any, tokens: list[str], source_name: str | None = None
) -> tuple[int, dict[str, int]]:
    """``(total chunks, {term: how many chunks contain it})`` in one round trip.

    Computed per query rather than maintained in a table. It is a GIN index
    probe per query term — a handful of terms, all indexed — and it is always
    correct, where a cached table is a second thing to invalidate on every
    write and a silent source of stale ranking when it drifts.
    """

    if not tokens:
        return 0, {}
    source_join = " AND f.source_id = ?" if source_name else ""
    params: tuple[object, ...] = (list(tokens), source_name) if source_name else (list(tokens),)
    rows = state.rows(
        "SELECT t.term AS term, count(f.chunk_id) AS df "
        "FROM unnest(?::text[]) AS t(term) "
        "LEFT JOIN chunks_fts f ON f.search_vector @@ to_tsquery('simple', t.term) "
        f"{source_join} "
        "GROUP BY t.term",
        params,
    )
    return _postgres_total_chunks(state, source_name), {
        str(row["term"]): int(row["df"]) for row in rows
    }


def _postgres_total_chunks(state: Any, source_name: str | None = None) -> int:
    """Corpus size for IDF — from the planner's estimate, not ``count(*)``.

    This was a correlated ``(SELECT count(*) FROM chunks_fts)`` inside the
    per-query document-frequency lookup, so **every text search** paid a full
    sequential scan of the largest table in the database. Postgres has no
    SQLite-style O(1) count, so the cost grew linearly with the corpus — on
    the exact backend chosen for corpora too big for SQLite.

    ``reltuples`` is what the planner itself uses, maintained by autovacuum. It
    is approximate, and that is fine *here specifically*: it feeds
    ``ln(1 + (N - df + 0.5) / (df + 0.5))``, where a few percent of drift moves
    every term's IDF by the same negligible amount and cannot reorder results.
    A negative value means the table has never been analyzed (``-1`` is the
    documented sentinel for "no statistics"), and a zero on a table with rows
    would zero out every score, so both fall back to the real count — paid once
    on a cold database rather than on every query forever.
    """

    # A source-filtered query is already routed to one logical shard. Its
    # btree-backed exact count is small and, importantly, prevents source-local
    # ranking from paying for or being skewed by every unrelated corpus.
    if source_name:
        exact = state.rows(
            "SELECT count(*) AS n FROM chunks_fts WHERE source_id = ?", (source_name,)
        )
        return int(exact[0]["n"]) if exact else 0

    rows = state.rows(
        "SELECT reltuples AS estimate FROM pg_class WHERE oid = 'chunks_fts'::regclass"
    )
    estimate = int(rows[0]["estimate"]) if rows and rows[0]["estimate"] is not None else -1
    if estimate > 0:
        return estimate
    exact = state.rows("SELECT count(*) AS n FROM chunks_fts")
    return int(exact[0]["n"]) if exact else 0


def _idf(total_docs: int, document_frequency: int) -> float:
    """BM25's inverse document frequency, the +1 variant.

    ``ln(1 + (N - df + 0.5) / (df + 0.5))`` — the form Lucene and SQLite's
    FTS5 both use, chosen over the classic ``ln((N - df + 0.5)/(df + 0.5))``
    because that one goes *negative* for a term appearing in more than half
    the corpus, which would make a common word actively push documents down
    the ranking rather than merely counting for little.
    """

    df = max(0, int(document_frequency))
    n = max(df, int(total_docs))
    return math.log(1.0 + (n - df + 0.5) / (df + 0.5))


def _postgres_rank_expression(
    state: Any,
    tokens: list[str],
    source_name: str | None = None,
    *,
    ranking: RankingParameters = DEFAULT_RANKING,
) -> tuple[str, list[object], list[str]]:
    """A BM25-shaped ranking expression for Postgres.

    ``ts_rank_cd`` has **no inverse document frequency**: it accumulates cover
    density, so a long document repeating a common query word outranks the
    short one that is actually about the rare term. That is not a tuning miss
    — no normalization flag fixes it, because normalization is about document
    *length* and the missing ingredient is corpus-wide *term rarity*. Measured
    directly: for "gateway restarts nightly", plain ``ts_rank_cd`` put a decoy
    repeating "gateway" six times above the one runbook containing "restarts".

    So the expression is built the way BM25 is: a **sum over query terms** of
    that term's rank times its IDF. The IDFs are computed here and inlined as
    float literals (they are ours, not user input); the terms themselves stay
    bound parameters.

    Falls back to a single un-weighted ``ts_rank_cd`` when the corpus is empty,
    where every IDF would be identical anyway.
    """

    unique = list(dict.fromkeys(tokens))
    weights = ranking.ts_rank_weights
    started = time.perf_counter()
    try:
        total, frequencies = _postgres_document_frequencies(state, unique, source_name)
    finally:
        from pheasant.request_budget import record_active_timing

        record_active_timing(
            "postgres_lexical_frequency",
            time.perf_counter() - started,
            query_terms=len(unique),
        )
    if not tokens or not total:
        return (
            f"ts_rank_cd('{{{weights}}}', chunks_fts.search_vector, "
            f"query_ts, {_TS_RANK_NORMALIZATION})",
            [],
            unique,
        )
    # A zero-frequency token cannot admit or score a row.  Among terms that
    # exist, lower document frequency means higher retrieval signal and a much
    # smaller GIN candidate set.  Lexical position is the deterministic tie
    # break because ``_query_tokens`` is sorted.
    ranked_tokens = sorted(
        (token for token in unique if frequencies.get(token, 0) > 0),
        key=lambda token: (frequencies.get(token, 0), token),
    )
    if not ranked_tokens:
        ranked_tokens = unique
    match_tokens = ranked_tokens[:_POSTGRES_MAX_MATCH_TERMS]
    score_tokens = ranked_tokens[:_POSTGRES_MAX_RANK_TERMS]
    terms: list[str] = []
    params: list[object] = []
    for token in score_tokens:
        weight = _idf(total, frequencies.get(token, 0))
        terms.append(
            f"{weight:.6f} * ts_rank_cd('{{{weights}}}', chunks_fts.search_vector, "
            f"to_tsquery('simple', ?), {_TS_RANK_NORMALIZATION})"
        )
        params.append(token)
    return "(" + " + ".join(terms) + ")", params, match_tokens


class SearchStore:
    def __init__(self, state: StateStore, ranking: Any = None):
        self.state = state
        # A `RankingResolver` (fleet parameters, refreshed on a TTL), a plain
        # `RankingParameters` (a tuning trial pinning one point), or None —
        # which is every existing caller, and gets the shipped defaults.
        self.ranking = ranking

    def ranking_parameters(self) -> RankingParameters:
        """The point this search ranks under."""

        resolver = self.ranking
        if resolver is None:
            return DEFAULT_RANKING
        current = getattr(resolver, "current", None)
        return current() if callable(current) else resolver

    def _memory_sql(self, policy: Any, now: str | None) -> tuple[str, str, list[object]]:
        """`(join, where_suffix, params)` enforcing an agent-memory policy.

        All three are empty unless the region has memory records *and* the
        policy asks for something — so the query a memory-less region runs is
        character-for-character the one it ran before Step 33.6.
        """
        if policy is None or now is None:
            return "", "", []
        from pheasant.memory.policy import sql_predicate

        condition, params = sql_predicate(policy, now=now, alias="memory_records")
        join = "LEFT JOIN memory_records ON memory_records.artifact_id = chunks.artifact_id"
        return join, f" AND {condition}", list(params)

    def search(
        self,
        query: str,
        source_name: str | None = None,
        max_results: int = 10,
        section: str | None = None,
        memory_policy: Any = None,
        memory_now: str | None = None,
        steering: Any = None,
    ) -> list[dict]:
        # Natural-language queries ("where does the X service run") must not
        # fall to FTS5's implicit-AND — a single unmatched stopword would
        # zero out the whole query (found by the Step-33.4 memory benchmark).
        # OR the sanitized tokens instead and let BM25 rank by token rarity;
        # quoting each token also neutralizes FTS5 query-syntax characters.
        tokens = _query_tokens(query)
        # Step 33.8 — alias expansion. Additive: the caller's own tokens always
        # survive, so a remembered synonym can widen a search and never narrow
        # one. This is the whole reason steering is worth having — it changes
        # queries that return no memory at all.
        if steering:
            tokens = steering.expand(tokens)
        match_expr = " OR ".join(f'"{token}"' for token in tokens) if tokens else query

        # Params are positional, so they must be assembled in the order the
        # placeholders appear in the final SQL: the ranking expression sits in
        # the SELECT, ahead of every WHERE clause.
        postgres = bool(getattr(self.state, "dialect", None) and self.state.dialect.is_postgres)
        ranking = self.ranking_parameters()
        prior_sql, prior_params = _structural_prior(
            steering, tokens, postgres=postgres, ranking=ranking
        )
        rank_sql, rank_params = "", []
        if postgres:
            rank_sql, rank_params, match_tokens = _postgres_rank_expression(
                self.state, tokens, source_name, ranking=ranking
            )
            match_expr = " | ".join(match_tokens) if match_tokens else query
        # Params are positional, so they must be assembled in the order the
        # placeholders appear in the final SQL. On Postgres the ranking
        # expression (`-{rank_sql}`) precedes the divisor (`/ {prior_sql}`),
        # which precedes the CROSS JOIN's tsquery, which precedes every WHERE
        # clause. On SQLite `bm25(...)` takes no parameters, so `prior_params`
        # is still first and this is the order it always was.
        params: list[object] = [*rank_params, *prior_params, match_expr]
        where = "search_vector @@ query_ts" if postgres else "chunks_fts MATCH ?"
        exclusion_where, exclusion_params = _exclusion_sql(steering)
        if source_name:
            # All three joined Postgres tables carry source_id. Leaving this
            # unqualified makes the primary query fail as ambiguous and used
            # to send every scoped search down the corpus-wide LIKE fallback.
            where += " AND chunks_fts.source_id = ?"
            params.append(source_name)
        # Restrict to one section of a document's taxonomy. Pushed into SQL
        # rather than filtered afterwards: a section is a narrow slice, and
        # post-filtering a globally-ranked page of hits can return nothing from
        # the requested section while its chunks sit just past the cut.
        if section_needle(section):
            where += " AND LOWER(COALESCE(chunks.heading_path, '')) LIKE ?"
            params.append(f"%{section_needle(section)}%")
        # Step 33.6 — agent-memory policy. Pushed into SQL for the same reason
        # as `section`: `mode="only"` is a very narrow slice, and post-filtering
        # a globally-ranked page would return an empty list while the matching
        # memories sat just past the cut. The join is added *only* when the
        # region actually has memory records, so a region without agent memory
        # runs the identical query it ran before this step.
        memory_join, memory_where, memory_params = self._memory_sql(memory_policy, memory_now)
        where += memory_where
        params.extend(memory_params)
        where += exclusion_where
        params.extend(exclusion_params)
        params.append(max_results)
        if postgres:
            # `ts_rank_cd` is positive-better; `bm25()` is negative-better and
            # everything downstream — the ORDER BY, and the `raw < 0` score
            # mapping below — is built around that. Negating here preserves
            # both, so the result-processing loop is shared rather than forked.
            #
            # The weight array is ordered {D,C,B,A}. It carries the measured
            # 8/3/2/1 column weights from the 2026-08-03 retrieval overhaul,
            # normalized onto ts_rank_cd's 0-1 scale: title A, path B,
            # heading_path C, text D. This preserves the *ordering* of the four
            # fields' importance. It does not reproduce BM25's arithmetic, and
            # `tests/test_backend_parity.py` gates on measured retrieval
            # quality rather than on the two backends agreeing on a number.
            sql = f"""
            SELECT chunks_fts.chunk_id, chunks_fts.source_id, chunks_fts.artifact_id,
                   chunks_fts.path, chunks_fts.heading_path, chunks.text,
                   chunks.start_line, chunks.end_line, artifacts.path AS absolute_path,
                   artifacts.relative_path,
                   -{rank_sql} / {prior_sql} AS rank_score
            FROM chunks_fts
            JOIN chunks ON chunks.id = chunks_fts.chunk_id
            JOIN artifacts ON artifacts.id = chunks_fts.artifact_id
            CROSS JOIN to_tsquery('simple', ?) AS query_ts
            {memory_join}
            WHERE {where}
            ORDER BY rank_score LIMIT ?
            """
        else:
            sql = f"""
            SELECT chunks_fts.chunk_id, chunks_fts.source_id, chunks_fts.artifact_id,
                   chunks_fts.path, chunks_fts.heading_path, chunks.text,
                   chunks.start_line, chunks.end_line, artifacts.path AS absolute_path,
                   artifacts.relative_path,
                   bm25(chunks_fts, {ranking.bm25_weights}) / {prior_sql} AS rank_score
            FROM chunks_fts
            JOIN chunks ON chunks.id = chunks_fts.chunk_id
            JOIN artifacts ON artifacts.id = chunks_fts.artifact_id
            {memory_join}
            WHERE {where}
            ORDER BY rank_score LIMIT ?
            """
        try:
            started = time.perf_counter()
            try:
                rows = self.state.rows(sql, tuple(params))
            finally:
                if postgres:
                    from pheasant.request_budget import record_active_timing

                    record_active_timing(
                        "postgres_lexical_rank",
                        time.perf_counter() - started,
                        query_terms=len(tokens),
                    )
        except Exception:
            fallback_where = "(chunks.text LIKE ? OR artifacts.relative_path LIKE ?)"
            fallback_params: list[object] = [f"%{query}%", f"%{query}%"]
            if source_name:
                fallback_where += " AND chunks.source_id = ?"
                fallback_params.append(source_name)
            if section_needle(section):
                fallback_where += " AND LOWER(COALESCE(chunks.heading_path, '')) LIKE ?"
                fallback_params.append(f"%{section_needle(section)}%")
            # The fallback has to enforce the same policy. Leaving it out would
            # make a corrected memory reappear on exactly the path taken when
            # FTS5 is unavailable — a leak that only shows up in the degraded
            # configuration, which is the worst place to have one.
            fallback_where += memory_where
            fallback_params.extend(memory_params)
            fallback_where += exclusion_where
            fallback_params.extend(exclusion_params)
            fallback_params.append(max_results)
            rows = self.state.rows(
                f"""SELECT chunks.id AS chunk_id, chunks.source_id, chunks.artifact_id,
                artifacts.relative_path AS path, chunks.heading_path, chunks.text,
                chunks.start_line, chunks.end_line, artifacts.path AS absolute_path,
                artifacts.relative_path, 0.0 AS rank_score
                FROM chunks JOIN artifacts ON artifacts.id=chunks.artifact_id
                {memory_join}
                WHERE {fallback_where} LIMIT ?""",
                tuple(fallback_params),
            )
        # No concept-term expansion pass. It used to top up short result sets
        # from `artifact_terms`, and it was measured dead: it only ran when FTS
        # returned FEWER than max_results, and on a real corpus every query
        # matched hundreds to thousands of chunks, so it never fired once. What
        # it did cost was an extra query per search and a table that had grown
        # to 1.27M rows / 554k distinct concepts over 2,132 files. Concept
        # nodes are kept — they still back the graph-facts panel and
        # `similar_to` edges, which is what they are actually good at — but
        # they are no longer a retrieval path.
        results = []
        for i, row in enumerate(rows, start=1):
            # FTS5 bm25() is a cost: more negative = better. Map it to a
            # monotone [0, 1) relevance so downstream merges (hybrid mode)
            # keep FTS's own ordering — the old 1/(1+|bm25|) *inverted* it
            # (found by the Step-33.4 memory benchmark). The LIKE-fallback
            # rows carry rank_score 0.0 and keep their historical 1.0.
            raw_rank = float(row["rank_score"] or 0.0)
            score = (-raw_rank / (1.0 - raw_rank)) if raw_rank < 0 else 1.0
            results.append(_row_result(row, i, score, "SQLite FTS/path match"))
        return results


def section_needle(section: str | None) -> str:
    """The comparable form of a requested section, or ``""`` for no filter.

    Lowercased and stripped, and that is the whole rule — the SQL above matches
    ``LOWER(heading_path) LIKE '%needle%'`` and :func:`section_matches` does
    ``needle in heading_path.lower()``, which are the same test. Keeping the
    normalization in one function is what stops the two from drifting apart.
    """
    return (section or "").strip().lower()


def section_matches(heading_path: object, section: str | None) -> bool:
    """Does a hit's breadcrumb sit inside the requested section?

    Substring, not equality, because people cite the section and not the whole
    path: ``§ 12.3``, ``Article IV`` and ``Termination`` should each reach
    ``MASTER SERVICES AGREEMENT > Article IV Term and Termination > § 12.3``.
    Matching the breadcrumb rather than only its last crumb is what makes
    asking for an Article return everything nested under it.
    """
    needle = section_needle(section)
    if not needle:
        return True
    return needle in str(heading_path or "").lower()


def _row_result(row, rank: int, score: float, reason: str) -> dict:
    # The section this chunk sits in, when the source extracts a taxonomy.
    # The SQL has always selected `heading_path`; it reached Python and was
    # dropped here, so a hit could never say *which* section matched. Added
    # only when non-empty, so a corpus without taxonomy returns the exact
    # payload it did before.
    heading_path = _row_value(row, "heading_path")
    chunk_detail = {
        "chunk_id": row["chunk_id"],
        "start_line": row["start_line"],
        "end_line": row["end_line"],
        "text_preview": (row["text"] or "")[:500],
    }
    provenance = {
        "source_id": row["source_id"],
        "path": row["absolute_path"],
        "relative_path": row["relative_path"],
    }
    if heading_path:
        chunk_detail["heading_path"] = heading_path
        provenance["heading_path"] = heading_path
    result = {
        "rank": rank,
        "node_id": row["artifact_id"],
        "chunk_id": row["chunk_id"],
        "type": "chunk",
        "title": row["relative_path"],
        "path": row["absolute_path"],
        "relative_path": row["relative_path"],
        "score": score,
        "reason": reason,
        "summary": (row["text"] or "")[:240],
        "chunks": [chunk_detail],
        "provenance": provenance,
    }
    if heading_path:
        result["heading_path"] = heading_path
    return result


def _row_value(row, key: str):
    """Read an optional column, tolerating a row that lacks it."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


# Words that carry no retrieval signal but wreck BM25 when they survive into
# the query. The OR-expansion above means every token contributes, and BM25
# weights a token by how *rare* it is — so an uncommon framing verb outscores
# the noun the question is actually about. Measured on the agent-framework
# corpus: "locate" appears in 15 chunks and "readme" in 724, so adding
# "locate" to `locate readme` pushed the repository's own README.md from rank
# 121 to 135 and put an unrelated `_compaction.py` into the top five. These
# are dropped from ranking, never from the user's intent — the answer step
# still sees the original question.
_STOPWORDS = frozenset(
    """
    a an the this that these those there here it its
    i we you me my our your us they them their
    is are was were be been being am do does did doing done
    have has had having can could should would will shall may might must
    of to in on at by for from with without into onto about across over under
    and or not but if then than as so such via per
    what which who whom whose when where why how
    find finds locate locating search searching show shows tell tells give gives
    get gets list lists explain explaining describe describing
    please help need needs want wants use uses using used
    me about above below more most some any all each every
    file files document documents thing things stuff
    """.split()
)

#: Applied when every token is a stopword ("what is it about") — a query with
#: no content words still has to return something, so the stopword filter
#: yields rather than emptying the query.
_MIN_CONTENT_TOKENS = 1


def _query_tokens(query: str, drop_stopwords: bool = True) -> list[str]:
    """Searchable tokens for a query: raw, underscore-split and camel-split.

    ``drop_stopwords`` filters framing words that only add BM25 noise. It
    yields when filtering would leave nothing to search for, so a question
    made entirely of function words degrades to the old behavior instead of
    matching nothing.
    """
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}", query):
        tokens.add(raw.lower())
        for part in re.split(r"[_\W]+", raw):
            if len(part) > 1:
                tokens.add(part.lower())
        camel = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw)
        for part in camel.split():
            if len(part) > 1:
                tokens.add(part.lower())
    if drop_stopwords:
        content = {token for token in tokens if token not in _STOPWORDS}
        if len(content) >= _MIN_CONTENT_TOKENS:
            return sorted(content)
    return sorted(tokens)


#: Terms too generic to describe a corpus, on top of the query stopwords.
_VOCAB_NOISE = frozenset(
    """
    self none true false null return def class import from print str int float bool
    list dict set tuple value values key keys name names type types data item items
    args kwargs param params result results test tests example examples new old
    http https www com org net html span div href src id class style
    """.split()
)


#: How many raw vocabulary terms to read for every one published.
#:
#: Deliberately **not** `search.ranking.filter_overfetch`, and named so the
#: difference is visible: that parameter is retrieval's, tunable, and
#: attributed to the `filters` stage of a *query*. This is contract
#: publication — it runs once per sync, has nothing to do with `max_results`,
#: and its post-filter is a fixed stopword/noise list rather than anything a
#: caller asked for. One name covering both behaviours is exactly the
#: confusion `tests/test_ranking_parameters.py` guards against.
VOCAB_OVERFETCH = 4


def corpus_vocabulary(state: StateStore, limit: int = 64) -> list[tuple[str, int]]:
    """The corpus's most widely-used content terms, as ``(term, doc_count)``.

    Read from ``chunks_vocab``, an ``fts5vocab`` view over the FTS index, so
    it costs no storage and cannot drift from what is actually searchable.

    This is what replaced concept extraction. "What is this corpus about" was
    being answered by materializing 141,529 concept nodes and 1.27M
    ``artifact_terms`` rows, when SQLite was already maintaining a term →
    document-frequency table for the index. Ordering by document frequency
    (not raw count) is deliberate: a term repeated 400 times in one file
    describes that file, while a term appearing once in 400 files describes
    the corpus.

    On Postgres ``chunks_vocab`` does not exist — ``fts5vocab`` is an SQLite
    virtual-table module — so the equivalent comes from ``ts_stat`` over the
    same ``search_vector`` the queries run against. Without that branch the
    ``except`` below swallowed a missing-relation error and every Postgres
    region published a contract with an **empty vocabulary**, which is the one
    field the router scores regions on: the region would have been silently
    unroutable while looking healthy. ``ts_stat`` scans the index rather than
    reading a maintained table, but this runs once per sync (contract
    publication), not per query.
    """
    postgres = bool(getattr(state, "dialect", None) and state.dialect.is_postgres)
    try:
        if postgres:
            rows = state.rows(
                "SELECT word AS term, ndoc AS doc FROM "
                "ts_stat('SELECT search_vector FROM chunks_fts') "
                "WHERE length(word) > 3 AND word !~ '[0-9]' "
                "ORDER BY ndoc DESC, word ASC LIMIT ?",
                (limit * VOCAB_OVERFETCH,),
            )
        else:
            rows = state.rows(
                "SELECT term, doc FROM chunks_vocab WHERE length(term) > 3 "
                "AND term NOT GLOB '*[0-9]*' ORDER BY doc DESC, term ASC LIMIT ?",
                (limit * VOCAB_OVERFETCH,),
            )
    except Exception:  # fts5vocab unavailable (older SQLite) — degrade quietly
        logger.warning("Corpus vocabulary unavailable; publishing an empty one", exc_info=True)
        return []
    out: list[tuple[str, int]] = []
    for row in rows:
        term = str(row["term"])
        if term in _STOPWORDS or term in _VOCAB_NOISE:
            continue
        out.append((term, int(row["doc"])))
        if len(out) >= limit:
            break
    return out
