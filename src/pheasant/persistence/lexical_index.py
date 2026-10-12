"""The ``pg_textsearch`` lexical index on Postgres (``storage.lexical_index``).

The DDL side: which index the setting asks for, what it is built over, and the
migration that makes the database match. The query side, which must spell the
same expression, is :mod:`pheasant.search.pg_textsearch`. Neither lives in
:mod:`pheasant.persistence.schema`, because nothing here is a table: the
index is derived from ``chunks_fts`` exactly as ``search_vector`` is, and
building or dropping it never changes a row.
"""

from __future__ import annotations

import logging
from typing import Any

from pheasant.persistence.sql import Dialect

logger = logging.getLogger(__name__)

#: ``storage.lexical_index`` values. ``tsvector`` is the generated column and
#: GIN index in :data:`~pheasant.persistence.schema.POSTGRES_EXTRAS`, and needs
#: nothing but stock Postgres.
LEXICAL_INDEXES = ("tsvector", "pg_textsearch")

#: The columns the ``pg_textsearch`` backend indexes, in ``chunks_fts`` order
#: and in the order ``RankingParameters`` weights them (8/3/2/1 by default).
BM25_COLUMNS = ("title", "path", "heading_path", "text")


def bm25_repeat_counts(weights: tuple[float, ...]) -> tuple[int, ...]:
    """The column weights as the whole repetition counts the index is built on.

    The index covers **one** document per chunk in which each column appears
    as many times as its weight. That is how FTS5's ``bm25()`` applies
    weights: it adds a column's weight to a term's frequency once per
    occurrence and saturates the weighted sum, so a title match counts for
    more but not eightfold more. A weighted sum of per-column BM25 scores was
    measured first and is the wrong shape: it applies the weight after
    saturation, and on the parity gold set a filename containing one query
    word outranked the only document containing all three.

    One difference remains: FTS5 normalizes by the unweighted length, and a
    repeated column also counts toward the length here. Titles and paths are a
    few tokens, so the effect is small; the parity gold set gates it.

    A fractional weight cannot be a repetition count, so it is refused rather
    than rounded into a different ranking than the one configured.
    """

    counts = tuple(int(round(float(weight))) for weight in weights)
    if any(abs(float(w) - c) > 1e-9 for w, c in zip(weights, counts, strict=True)):
        raise ValueError(
            "storage.lexical_index 'pg_textsearch' needs whole-number column weights "
            f"(search.ranking title/path/heading/text), got {list(weights)}"
        )
    if not any(counts):
        raise ValueError("storage.lexical_index 'pg_textsearch' needs a non-zero column weight")
    return counts


def bm25_index_name(counts: tuple[int, ...]) -> str:
    """Named for its weights, so a changed weight is a different index."""

    return "idx_chunks_fts_bm25_w" + "_".join(str(count) for count in counts)


def bm25_document_expression(counts: tuple[int, ...], *, table: str = "") -> str:
    """The indexed expression, shared by the DDL and the query.

    pg_textsearch matches an expression index only when the query repeats the
    expression exactly, so both sides call this rather than spelling it twice.
    The punctuation flattening is ``search_vector``'s, for the reason given
    above ``POSTGRES_EXTRAS`` in :mod:`pheasant.persistence.schema`:
    ``deploy-gateway.md`` has to tokenize as ``deploy gateway md`` on every
    backend.
    """

    parts = []
    for column, count in zip(BM25_COLUMNS, counts, strict=True):
        if count <= 0:
            continue
        qualified = f"{table}.{column}" if table else column
        flat = f"regexp_replace(coalesce({qualified}, ''), '[^a-zA-Z0-9]+', ' ', 'g')"
        parts.append(f"repeat({flat} || ' ', {count})")
    return " || ".join(parts)


def postgres_bm25_ddl(counts: tuple[int, ...]) -> str:
    """``CREATE INDEX`` for the ``pg_textsearch`` lexical backend.

    ``text_config='simple'`` is the dictionary ``search_vector`` uses: no
    stemming, no stopword list, so both Postgres backends and FTS5's
    ``unicode61`` see the same terms. ``k1``/``b`` stay at the extension's
    defaults (1.2/0.75), which are also FTS5's.
    """

    return (
        f"CREATE INDEX IF NOT EXISTS {bm25_index_name(counts)} ON chunks_fts "
        f"USING bm25 (({bm25_document_expression(counts)})) WITH (text_config='simple')"
    )


def validate_lexical_index(name: str, dialect: Dialect) -> str:
    """The setting, normalized, or a refusal naming what to change."""

    name = str(name).strip().lower()
    if name not in LEXICAL_INDEXES:
        raise ValueError(
            f"Unknown storage.lexical_index {name!r}; expected one of {', '.join(LEXICAL_INDEXES)}"
        )
    if name == "pg_textsearch" and not dialect.is_postgres:
        raise ValueError(
            "storage.lexical_index is 'pg_textsearch', which is a Postgres "
            "extension, but storage.backend is not 'postgres'. SQLite always "
            "ranks with FTS5's own bm25(); remove the setting or switch backend."
        )
    return name


def migrate_lexical_index(store: Any) -> None:
    """Make the ``pg_textsearch`` index match ``storage.lexical_index``.

    Runs under :meth:`StateStore._migrate_postgres`'s advisory lock, so a fleet
    starting together elects one process to build it. It is derived from
    ``chunks_fts`` like ``search_vector`` is, so building it over an
    existing corpus needs no re-index and dropping it loses nothing.

    Selected: create the extension if the server allows it, then the index
    for the configured weights. A server that cannot load the extension is
    a startup refusal rather than a quiet fallback to ``tsvector``, which
    would leave a region ranking differently from what its config says.

    Any other ``idx_chunks_fts_bm25_*`` index is dropped: one built for
    weights the config no longer names, or every one of them when the
    backend is set back to ``tsvector``. Each chunk write maintains them,
    so an index nothing queries is write cost on the commit path with no
    reader.
    """

    if store.lexical_index is None or not store.backend.dialect.is_postgres:
        return
    wanted = bm25_index_name(store.bm25_counts) if store.bm25_counts else None
    present = {
        str(row["indexname"])
        for row in store.rows(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'chunks_fts' "
            "AND indexname LIKE 'idx_chunks_fts_bm25_%'"
        )
    }
    stale = sorted(present - {wanted})
    if stale:
        logger.info("Dropping pg_textsearch indexes no longer configured: %s", stale)
        for name in stale:
            store.conn.execute(f"DROP INDEX IF EXISTS {name}")
        store.conn.commit()
    if wanted is None or wanted in present:
        return
    try:
        store.conn.execute("CREATE EXTENSION IF NOT EXISTS pg_textsearch")
        store.conn.commit()
    except Exception as exc:
        store.conn.rollback()
        raise RuntimeError(
            "storage.lexical_index is 'pg_textsearch' but the extension could not "
            f"be created ({exc}). It needs PostgreSQL 17 or newer with "
            "shared_preload_libraries = 'pg_textsearch', and CREATE EXTENSION "
            "pg_textsearch run once by a superuser if pheasant's role is not one. "
            "Set storage.lexical_index: tsvector to run without it."
        ) from exc
    logger.info("Building pg_textsearch BM25 index %s on chunks_fts", wanted)
    store.conn.execute(postgres_bm25_ddl(store.bm25_counts))
    store.conn.commit()
