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
) -> tuple[str, list[object]]:
    """The lexical query on pg_textsearch's BM25 index, and its params.

    The inner query is an ordered index scan, Block-Max WAND top-k, with
    every filter inside it so a narrow filter cannot leave the top-k empty
    (the extension widens a post-filtered scan until its ``LIMIT`` fills).
    The outer query divides by the structural prior and keeps what matched.

    That replaces both halves of the tsvector path's cost: the
    document-frequency round trip (the index keeps its own corpus statistics)
    and ``ts_rank_cd`` evaluated against every row the ``OR`` query admits,
    which grew with the corpus. It also ranks the way FTS5's ``bm25()`` does,
    which ``ts_rank_cd`` cannot (see ``bm25_repeat_counts``).

    **The score and the sort key are spelled differently on purpose.** Asked
    for in the select list, ``<@>`` re-scores the row on its own: it flattens
    and tokenizes the whole chunk again, 38 ms of a 39 ms query for 60
    candidates on a 20k-chunk corpus, against 0.7 ms for the index scan that
    had already computed the same number. pg_textsearch's planner hook swaps
    a select-list score for the scan's own (``bm25_get_current_score()``)
    when it ``equal()``\s a *separate* ORDER BY entry, and Postgres merges
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
    depth = max(_BM25_CANDIDATE_FLOOR, max_results * _BM25_CANDIDATE_FACTOR)
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
