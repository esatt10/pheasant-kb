"""The lexical arm on pg_textsearch's BM25 index (``storage.lexical_index``).

:class:`~pheasant.search.sqlite_store.SearchStore` builds its filters and the
structural prior once and hands them here when the region ranks with the
extension; everything else about a search — tokens, steering, the score
mapping, the LIKE fallback — stays where it is, so the three lexical
backends differ in exactly one statement. The index this queries is built by
:mod:`pheasant.persistence.lexical_index`, which owns the expression both
sides must spell identically.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pheasant.persistence.lexical_index import bm25_document_expression, bm25_index_name
from pheasant.search.ranking import RankingParameters

logger = logging.getLogger(__name__)

# How deep the BM25 index is read before the structural prior reorders it.
# The prior divides the score, so a deeper or test-file chunk can fall below
# one ranked a little lower by BM25 alone; reading past `max_results` is what
# lets the shallow file the prior favours come up from below the cut. The
# read is a Block-Max WAND top-k, which costs roughly what its `LIMIT` costs
# rather than what the corpus does, so the floor is cheap.
_BM25_CANDIDATE_FLOOR = 50
_BM25_CANDIDATE_FACTOR = 2
# How much deeper than the candidate depth the fenced scan reads when filters
# outside it will drop rows, how much each retry widens it, and how far it
# may widen before the search hands the question back to the planner (see
# `bm25_scan_limits`).
_BM25_SCAN_HEADROOM = 2
_BM25_SCAN_WIDEN = 8
_BM25_SCAN_CEILING = 8192

_bm25_fallback_warned = False
_bm25_weights_warned = False


def warn_bm25_unavailable() -> None:
    """Say once per process that pg_textsearch failed and tsvector answered.

    Once, because it fails the same way on every query until someone acts,
    and a warning per search would bury the first one.
    """

    global _bm25_fallback_warned
    if _bm25_fallback_warned:
        logger.debug("pg_textsearch query failed; ranked with tsvector", exc_info=True)
        return
    _bm25_fallback_warned = True
    logger.warning(
        "storage.lexical_index is 'pg_textsearch' but its query failed; ranking with "
        "the tsvector index until it works again. Restarting re-runs the migration "
        "that creates the BM25 index.",
        exc_info=True,
    )


def bm25_counts_for(state: Any, ranking: RankingParameters) -> tuple[int, ...] | None:
    """The index's repetition counts, if the ranking in force can use it.

    The column weights are inside the index expression, so the index answers
    only for the weights it was built on. A tuning bundle or trial that moves
    one ranks with ``tsvector``, which takes its weights per query, rather
    than being served a ranking that ignores the parameter it changed — that
    would be a bundle promoted on a parameter with no effect, the over-fetch
    trap over again.
    """

    global _bm25_weights_warned
    counts = getattr(state, "bm25_counts", None)
    if not counts:
        return None
    current = (
        ranking.title_weight,
        ranking.path_weight,
        ranking.heading_weight,
        ranking.text_weight,
    )
    if all(abs(float(w) - c) <= 1e-9 for w, c in zip(current, counts, strict=True)):
        return counts
    if not _bm25_weights_warned:
        _bm25_weights_warned = True
        logger.info(
            "Ranking column weights %s differ from the pg_textsearch index's %s; "
            "ranking with tsvector until the config's search.ranking matches",
            list(current),
            list(counts),
        )
    return None


def bm25_query_text(tokens: list[str]) -> str:
    """The query terms as pg_textsearch reads them, safe to inline as a literal.

    Split on the same punctuation the index expression flattens, so
    ``heap_insert`` asks for ``heap`` and ``insert`` exactly as the document
    side stored them, and de-duplicated, so a token that is also one of its
    own parts does not count twice. Nothing but ``[a-z0-9]`` survives, which
    is what makes inlining it in :func:`pg_textsearch_sql` safe: alias
    expansions come from memory records, and nothing from them may reach the
    statement as SQL.
    """

    terms = re.findall(r"[a-z0-9]+", " ".join(tokens).lower())
    return " ".join(dict.fromkeys(terms))


def bm25_candidate_depth(max_results: int) -> int:
    """How many BM25-ordered rows that pass every filter the prior reorders."""

    return max(_BM25_CANDIDATE_FLOOR, max_results * _BM25_CANDIDATE_FACTOR)


def bm25_scan_limits(max_results: int, *, filtered: bool) -> list[int]:
    """The fenced scan's depth for each attempt, shallowest first.

    Unfiltered, the scan's top rows *are* the candidates, so one read of
    exactly that depth answers. With filters outside the fence the scan reads
    ahead by ``_BM25_SCAN_HEADROOM`` and, when too few of its rows survive,
    widens by ``_BM25_SCAN_WIDEN`` up to ``_BM25_SCAN_CEILING``. A filter
    selective enough to exhaust every attempt is one that admits few rows,
    and the caller then runs the unfenced statement, whose sort plan scores
    only the rows that pass it.
    """

    depth = bm25_candidate_depth(max_results)
    if not filtered:
        return [depth]
    limits = [depth * _BM25_SCAN_HEADROOM]
    while limits[-1] < _BM25_SCAN_CEILING:
        limits.append(min(_BM25_SCAN_CEILING, limits[-1] * _BM25_SCAN_WIDEN))
    return limits


def pg_textsearch_sql(
    query_text: str,
    counts: tuple[int, ...],
    *,
    prior_sql: str,
    prior_params: list[object],
    memory_join: str,
    filter_where: str,
    filter_params: list[object],
    max_results: int,
    scan_where: str = "",
    scan_params: list[object] | None = None,
    scan_limit: int | None = None,
) -> tuple[str, list[object]]:
    """The lexical query on pg_textsearch's BM25 index, and its params.

    **The index scan sits behind a fence**: a subquery over ``chunks_fts``
    alone, an ordered index scan, Block-Max WAND
    top-k, reading ``scan_limit`` rows with only ``scan_where`` (filters on
    ``chunks_fts``'s own columns, the source) inside it. Every filter that
    needs a join — the memory policy, a folder scope, a section, a steering
    exclusion — is applied outside, to the rows the scan returned.

    That is not tidiness. With those joins in the same query as the
    ``ORDER BY … <@>`` the planner is free to drop the index entirely: it
    fetches every row that passes the filters and sorts them by ``<@>``,
    which scores each chunk *standalone* — re-flattening and re-tokenizing
    it — at ~170µs a row. Measured on a 53k-chunk region: the memory policy's
    ``LEFT JOIN`` (the default ``prefer`` policy, applied whenever a region
    holds one memory record) took **9.0 s** per search that way, a folder
    scope 558 ms, against 12 ms for the same query on the index. The SQL
    still said ``<@>``, so nothing looked wrong. Inside the fence there is
    no join to tempt it, and a ``LIMIT`` the planner cannot pull up.

    The fence is a ``FROM (… ORDER BY … LIMIT ?)`` subquery, not a CTE: a
    ``MATERIALIZED`` CTE keeps the index scan but loses the score reuse
    below, and re-scored all 800 rows of a 400-result search standalone
    (274 ms against 12).

    Outside the fence each scanned row carries ``admits`` (it matched and
    passes every filter), ``passed`` (how many scanned rows do) and
    ``scanned`` (how many the scan returned). Rows that do not pass are
    sorted last rather than dropped, so the counts arrive even when nothing
    passes; the caller keeps the admitted rows. It widens the scan while
    ``passed`` is short of the candidate depth and the scan was not
    exhausted (``bm25_scan_limits``), so the candidates are exactly the first
    ``depth`` BM25-ordered rows that pass every filter — the set the unfenced
    statement chose.

    **The score and the sort key are spelled differently on purpose.** Asked
    for in the select list, ``<@>`` re-scores the row on its own: it flattens
    and tokenizes the whole chunk again, 38 ms of a 39 ms query for 60
    candidates on a 20k-chunk corpus, against 0.7 ms for the index scan that
    had already computed the same number. pg_textsearch's planner hook swaps
    a select-list score for the scan's own (``bm25_get_current_score()``)
    when it is ``equal()`` to a *separate* ORDER BY entry, and Postgres merges
    two identically spelled expressions into one entry at parse time. So the
    select list says ``to_bm25query('…', 'idx')`` and the ORDER BY says
    ``'idx:…'::bm25query``: different trees when parsed, one constant once
    folded. That is also why the terms are a literal rather than a bound
    parameter: a generic plan does not fold parameters, and the text varying
    per query keeps psycopg from preparing one. A plan without the index
    scan (a small table) gets no swap and scores standalone, which is slower
    and gives the same numbers; ``tests/test_pg_textsearch.py`` holds both.

    ``<@>`` is negative-better like ``bm25()``, so the result loop's
    ``raw < 0`` mapping applies unchanged. One difference from the tsvector
    path: a source-scoped search ranks with the region's corpus statistics,
    because the index holds one set for the table.
    """

    expression = bm25_document_expression(counts, table="chunks_fts")
    index = bm25_index_name(counts)
    depth = bm25_candidate_depth(max_results)
    limit = scan_limit if scan_limit is not None else depth
    sql = f"""
    SELECT chunk_id, source_id, artifact_id, path, heading_path, text,
           start_line, end_line, absolute_path, relative_path,
           bm25_cost / prior AS rank_score, admits, passed, scanned
    FROM (
        SELECT *
        FROM (
            SELECT judged.*,
                   count(*) OVER () AS scanned,
                   count(*) FILTER (WHERE admits) OVER () AS passed
            FROM (
                SELECT chunks_fts.chunk_id, chunks_fts.source_id, chunks_fts.artifact_id,
                       chunks_fts.path, chunks_fts.heading_path, chunks.text,
                       chunks.start_line, chunks.end_line, artifacts.path AS absolute_path,
                       artifacts.relative_path, chunks_fts.bm25_cost,
                       {prior_sql} AS prior,
                       COALESCE(chunks_fts.bm25_cost < 0{filter_where}, FALSE) AS admits
                FROM (
                    SELECT chunks_fts.chunk_id, chunks_fts.source_id, chunks_fts.artifact_id,
                           chunks_fts.path, chunks_fts.heading_path,
                           ({expression}) <@> to_bm25query('{query_text}', '{index}') AS bm25_cost
                    FROM chunks_fts
                    WHERE TRUE{scan_where}
                    ORDER BY ({expression}) <@> '{index}:{query_text}'::bm25query
                    LIMIT ?
                ) AS chunks_fts
                JOIN chunks ON chunks.id = chunks_fts.chunk_id
                JOIN artifacts ON artifacts.id = chunks_fts.artifact_id
                {memory_join}
            ) AS judged
        ) AS counted
        ORDER BY admits DESC, bm25_cost, chunk_id
        LIMIT ?
    ) AS candidates
    ORDER BY admits DESC, rank_score, chunk_id
    LIMIT ?
    """
    return sql, [*prior_params, *filter_params, *(scan_params or []), limit, depth, max_results]


def pg_textsearch_unfenced_sql(
    query_text: str,
    counts: tuple[int, ...],
    *,
    prior_sql: str,
    prior_params: list[object],
    memory_join: str,
    filter_where: str,
    filter_params: list[object],
    max_results: int,
) -> tuple[str, list[object]]:
    """Every filter beside the ordered scan, the planner choosing the plan.

    The fenced statement's last resort, for a filter so selective that a
    scan widened to ``_BM25_SCAN_CEILING`` rows still found too few that
    pass it. Then few rows pass at all, and the plan the fence exists to
    prevent — score every passing row standalone, sort — is the cheap one.
    Same columns as the fenced statement, ``passed`` and ``scanned`` aside.
    """

    expression = bm25_document_expression(counts, table="chunks_fts")
    index = bm25_index_name(counts)
    depth = bm25_candidate_depth(max_results)
    sql = f"""
    SELECT chunk_id, source_id, artifact_id, path, heading_path, text,
           start_line, end_line, absolute_path, relative_path,
           bm25_cost / prior AS rank_score
    FROM (
        SELECT chunks_fts.chunk_id, chunks_fts.source_id, chunks_fts.artifact_id,
               chunks_fts.path, chunks_fts.heading_path, chunks.text,
               chunks.start_line, chunks.end_line, artifacts.path AS absolute_path,
               artifacts.relative_path,
               ({expression}) <@> to_bm25query('{query_text}', '{index}') AS bm25_cost,
               {prior_sql} AS prior
        FROM chunks_fts
        JOIN chunks ON chunks.id = chunks_fts.chunk_id
        JOIN artifacts ON artifacts.id = chunks_fts.artifact_id
        {memory_join}
        WHERE TRUE{filter_where}
        ORDER BY ({expression}) <@> '{index}:{query_text}'::bm25query
        LIMIT ?
    ) AS candidates
    WHERE bm25_cost < 0
    ORDER BY rank_score, chunk_id
    LIMIT ?
    """
    return sql, [*prior_params, *filter_params, depth, max_results]


def bm25_rows(
    state: Any,
    query_text: str,
    counts: tuple[int, ...],
    *,
    prior_sql: str,
    prior_params: list[object],
    memory_join: str,
    source_where: str,
    source_params: list[object],
    joined_where: str,
    joined_params: list[object],
    max_results: int,
) -> tuple[list[Any], str]:
    """The BM25 candidates, read through the fenced index scan, and how.

    ``SearchStore.search`` builds the prior and the filters; this decides how
    deep the scan reads, and is the only caller of the two statements below.

    The scan is widened while too few of its rows pass the filters
    outside it and it still had rows to give; the rows that come back
    are then exactly the first ``depth`` BM25-ordered rows that pass
    every filter (see ``pg_textsearch_sql``). A filter that leaves too
    few even at the widest scan admits few rows at all, so the unfenced
    statement answers, and its sort plan is cheap for exactly that shape.
    The second value names which of the three answered, for
    ``pheasant_retrieval_lexical_total``.
    """

    filtered = bool(joined_where or memory_join)
    depth = bm25_candidate_depth(max_results)
    for attempt, scan_limit in enumerate(bm25_scan_limits(max_results, filtered=filtered)):
        sql, params = pg_textsearch_sql(
            query_text,
            counts,
            prior_sql=prior_sql,
            prior_params=prior_params,
            memory_join=memory_join,
            filter_where=joined_where,
            filter_params=joined_params,
            max_results=max_results,
            scan_where=source_where,
            scan_params=source_params,
            scan_limit=scan_limit,
        )
        result = state.rows(sql, tuple(params))
        scanned = int(result[0]["scanned"]) if result else 0
        passed = int(result[0]["passed"]) if result else 0
        if passed >= depth or scanned < scan_limit:
            return [row for row in result if row["admits"]], ("widened" if attempt else "index")
    sql, params = pg_textsearch_unfenced_sql(
        query_text,
        counts,
        prior_sql=prior_sql,
        prior_params=prior_params,
        memory_join=memory_join,
        filter_where=source_where + joined_where,
        filter_params=[*source_params, *joined_params],
        max_results=max_results,
    )
    return state.rows(sql, tuple(params)), "unfenced"
