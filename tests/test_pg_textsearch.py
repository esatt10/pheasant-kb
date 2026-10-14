"""``storage.lexical_index: pg_textsearch`` — the BM25 extension as the lexical arm.

The Postgres default. Two halves. The offline half runs everywhere and holds
the parts that need no server: the setting's refusals, SQLite ignoring it, the
weights-to-repetition rule, the one shared index expression, and the literal
the query inlines. The live half needs a Postgres with pg_textsearch loaded,
and skips — loudly, with the reason — without one, unless
``PHEASANT_REQUIRE_PG_TEXTSEARCH=1`` makes that a failure, which is how CI's
backend-parity job stops it skipping silently.

    PHEASANT_TEST_POSTGRES_DSN=postgresql://user@127.0.0.1:5433/scratch \\
        pytest -q tests/test_pg_textsearch.py

The DSN is the same one the parity suite uses, and like it these tests drop the
``public`` schema of that database.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.persistence.lexical_index import (
    bm25_document_expression,
    bm25_index_name,
    bm25_repeat_counts,
    postgres_bm25_ddl,
)
from pheasant.persistence.state_store import StateStore
from pheasant.search import pg_textsearch
from pheasant.search.hybrid import HybridSearch
from pheasant.search.pg_textsearch import bm25_counts_for, bm25_query_text, pg_textsearch_sql
from pheasant.search.ranking import RankingParameters
from pheasant.search.sqlite_store import SearchStore
from pheasant.sync.engine import SyncEngine
from pheasant.telemetry import metrics
from tests.test_backend_parity import DSN, GOLD, _config, _corpus, _reset


def _lexical(index: str, path: str) -> float:
    # Registered at server start; idempotent, and keeps recorded values.
    metrics.register_default_metrics("test")
    return metrics.REGISTRY.value("pheasant_retrieval_lexical_total", index=index, path=path) or 0.0


DEFAULT_COUNTS = (8, 3, 2, 1)


# --- offline -------------------------------------------------------------


def test_postgres_ranks_with_pg_textsearch_by_default() -> None:
    config = PheasantConfig.model_validate({"pheasant": {"name": "kb"}})
    assert config.storage.lexical_index == "pg_textsearch"


def test_sqlite_searches_are_counted_as_fts5(tmp_path: Path) -> None:
    """The standalone default reports itself, so a dashboard never shows a gap."""

    store = StateStore(tmp_path / "state.db")
    store.migrate()
    before = _lexical("fts5", "sqlite")
    SearchStore(store).search("anything at all")
    assert _lexical("fts5", "sqlite") == before + 1


def test_sqlite_ranks_with_fts5_whatever_the_setting_says(tmp_path: Path) -> None:
    """Rule 7: the default must not change what a standalone region does.

    ``pg_textsearch`` is the setting's default, so SQLite has to accept it,
    and it must not reach the weights rule either — a standalone region with
    a fractional column weight ranks with FTS5, which takes any weight.
    """

    for value in ("pg_textsearch", "tsvector"):
        store = StateStore(tmp_path / f"{value}.db", lexical_index=value)
        assert store.bm25_counts is None
    fractional = StateStore(
        tmp_path / "f.db", lexical_index="pg_textsearch", lexical_weights=(8.5, 3, 2, 1)
    )
    assert fractional.bm25_counts is None
    with pytest.raises(ValueError, match="Unknown storage.lexical_index"):
        StateStore(tmp_path / "s.db", lexical_index="pg_search")


def test_the_standalone_default_config_opens_on_fts5(tmp_path: Path) -> None:
    config = PheasantConfig.model_validate(
        {"pheasant": {"name": "kb", "state_path": str(tmp_path / "state")}}
    )
    store = StateStore.from_config(config, tmp_path / "s.db")
    store.migrate()
    assert not store.dialect.is_postgres
    assert store.lexical_index == "pg_textsearch" and store.bm25_counts is None


def test_a_store_opened_without_config_never_manages_the_index(tmp_path: Path) -> None:
    """A migration tool or a test must not drop what the region's config built."""

    store = StateStore(tmp_path / "s.db")
    assert store.lexical_index is None
    assert store.bm25_counts is None


def test_weights_become_repetition_counts_or_are_refused() -> None:
    assert bm25_repeat_counts((8.0, 3.0, 2.0, 1.0)) == DEFAULT_COUNTS
    assert bm25_repeat_counts((4, 0, 0, 1)) == (4, 0, 0, 1)
    with pytest.raises(ValueError, match="storage.lexical_index: tsvector"):
        bm25_repeat_counts((8.5, 3.0, 2.0, 1.0))
    with pytest.raises(ValueError, match="non-zero"):
        bm25_repeat_counts((0.0, 0.0, 0.0, 0.0))


def test_the_index_and_the_query_spell_one_expression() -> None:
    """pg_textsearch uses an expression index only for the identical expression.

    Two spellings would leave every query on a standalone score over the whole
    table, with nothing failing, so both sides are asserted to read one helper.
    """

    ddl = postgres_bm25_ddl(DEFAULT_COUNTS)
    assert bm25_document_expression(DEFAULT_COUNTS) in ddl
    assert bm25_index_name(DEFAULT_COUNTS) == "idx_chunks_fts_bm25_w8_3_2_1"
    assert "text_config='simple'" in ddl

    sql, params = pg_textsearch_sql(
        "gateway restarts",
        DEFAULT_COUNTS,
        prior_sql="1.0",
        prior_params=[],
        memory_join="",
        filter_where=" AND chunks_fts.source_id = ?",
        filter_params=["docs"],
        max_results=10,
    )
    qualified = bm25_document_expression(DEFAULT_COUNTS, table="chunks_fts")
    assert sql.count(qualified) == 2
    # The score and the sort key must parse as different trees, or Postgres
    # merges them and the score is recomputed per row — see the docstring.
    assert "to_bm25query('gateway restarts', 'idx_chunks_fts_bm25_w8_3_2_1')" in sql
    assert "'idx_chunks_fts_bm25_w8_3_2_1:gateway restarts'::bm25query" in sql
    # Placeholder order is textual: the filters judged outside the scan come
    # before the scan's own limit, then the candidate depth and the cut.
    assert params == ["docs", 50, 50, 10]


def test_nothing_is_joined_beside_the_ordered_scan() -> None:
    """The BM25 scan is fenced off from every join, which is what keeps its index.

    With the memory policy's ``LEFT JOIN`` (or any filter needing a join) in
    the same query as ``ORDER BY … <@>``, the planner was free to read every
    passing row and sort them by ``<@>``, scoring each chunk standalone: 9 s
    a search on a 53k-chunk region, the index never touched, while the SQL
    still said BM25. So the subquery holding the ordered scan must name
    ``chunks_fts`` alone, with only the scan's own filter, and the joined
    filters must sit outside it.
    """

    sql, params = pg_textsearch_sql(
        "gateway",
        DEFAULT_COUNTS,
        prior_sql="1.0",
        prior_params=[],
        memory_join="LEFT JOIN memory_records ON memory_records.artifact_id = chunks.artifact_id",
        filter_where=" AND memory_records.record_id IS NULL AND artifacts.relative_path <> ?",
        filter_params=["notes.md"],
        max_results=10,
        scan_where=" AND chunks_fts.source_id = ?",
        scan_params=["docs"],
        scan_limit=200,
    )
    order_by = sql.index("::bm25query")
    scan = sql[sql.rindex("FROM chunks_fts", 0, order_by) : order_by]
    assert "JOIN" not in scan and "memory_records" not in scan and "artifacts" not in scan
    assert "chunks_fts.source_id = ?" in scan
    assert "WITH" not in sql  # a CTE fence loses the score reuse; see the docstring
    assert params == ["notes.md", "docs", 200, 50, 10]


def test_widening_the_scan_steps_up_to_its_ceiling() -> None:
    assert pg_textsearch.bm25_scan_limits(10, filtered=False) == [50]
    limits = pg_textsearch.bm25_scan_limits(10, filtered=True)
    assert limits[0] == 200 and limits[-1] == pg_textsearch._BM25_SCAN_CEILING
    assert limits == sorted(set(limits))


def test_a_zero_weight_column_is_left_out_of_the_document() -> None:
    expression = bm25_document_expression((4, 0, 0, 1))
    assert "title" in expression and "text" in expression
    assert "path" not in expression.replace("heading_path", "")
    assert "heading_path" not in expression


def test_only_plain_terms_reach_the_inlined_literal() -> None:
    """Alias expansions come from memory records, which anyone may write."""

    assert bm25_query_text(["heap_insert", "heap", "insert"]) == "heap insert"
    hostile = bm25_query_text(["x'); DROP TABLE chunks; --", "a?b", "50%", "idx:q"])
    assert set(hostile) <= set("abcdefghijklmnopqrstuvwxyz0123456789 ")
    assert bm25_query_text([]) == ""


def test_the_index_answers_only_for_the_weights_it_was_built_on() -> None:
    """A bundle that moves a column weight must move the ranking.

    The weights are inside the index expression, so a trial or bundle with
    different ones is ranked by tsvector (which takes them per query) rather
    than served a ranking that ignores the parameter it changed.
    """

    state = SimpleNamespace(bm25_counts=DEFAULT_COUNTS)
    assert bm25_counts_for(state, RankingParameters()) == DEFAULT_COUNTS
    assert bm25_counts_for(state, RankingParameters(title_weight=4.0)) is None
    assert bm25_counts_for(SimpleNamespace(bm25_counts=None), RankingParameters()) is None
    assert bm25_counts_for(object(), RankingParameters()) is None


# --- live ----------------------------------------------------------------


def _extension_available() -> str:
    """``""`` when the DSN's server can load pg_textsearch, else the reason."""

    if not DSN:
        return "set PHEASANT_TEST_POSTGRES_DSN to a throwaway database"
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        available = conn.execute(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'pg_textsearch'"
        ).fetchone()
        preload = str(conn.execute("SHOW shared_preload_libraries").fetchone()[0])
    if not available:
        return "the server at PHEASANT_TEST_POSTGRES_DSN has no pg_textsearch extension"
    if "pg_textsearch" not in preload:
        return "pg_textsearch is installed but not in shared_preload_libraries"
    return ""


@pytest.fixture(scope="module")
def require_extension() -> None:
    reason = _extension_available()
    if not reason:
        return
    if os.environ.get("PHEASANT_REQUIRE_PG_TEXTSEARCH") == "1":
        pytest.fail(f"PHEASANT_REQUIRE_PG_TEXTSEARCH=1 but {reason}")
    pytest.skip(reason)


def _engine(root: Path, workspace: Path, lexical_index: str, **ranking: float) -> SyncEngine:
    config = _config(root, workspace, "postgres")
    config.storage.lexical_index = lexical_index
    for name, value in ranking.items():
        setattr(config.search.ranking, name, value)
    return SyncEngine(config)


def _bm25_indexes() -> set[str]:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE indexname LIKE 'idx_chunks_fts_bm25_%'"
        ).fetchall()
    return {str(row[0]) for row in rows}


def _ranked(engine: SyncEngine, query: str) -> list[str]:
    search = HybridSearch(SearchStore(engine.state))
    hits = search.search_context(engine.config.knowledge_base_id, query, "text", 5)
    return [hit.get("relative_path") for hit in hits["results"]]


@pytest.fixture()
def indexed(tmp_path: Path, require_extension):
    _reset(DSN)
    workspace = _corpus(tmp_path)
    engine = _engine(tmp_path, workspace, "pg_textsearch")
    engine.sync_source("docs", "full")
    try:
        yield engine
    finally:
        engine.close()


def _capture_lexical_sql(monkeypatch, state) -> list[str]:
    seen: list[str] = []
    original = state.rows

    def spy(sql, params=()):
        seen.append(sql)
        return original(sql, params)

    monkeypatch.setattr(state, "rows", spy)
    return seen


def test_the_gold_set_ranks_correctly_including_the_tsvector_residual(indexed) -> None:
    """Every gold query, ``deploy gateway`` included.

    That one is ``POSTGRES_RANK_RESIDUAL`` in the parity suite: ``ts_rank_cd``
    has one length normalization for the whole weighted vector, so a filename
    match loses to a body that repeats the word. BM25 over the weighted
    document normalizes the way FTS5 does, and the residual closes on this
    backend — while staying pinned on the tsvector one.
    """

    for query, expected in GOLD:
        ranked = _ranked(indexed, query)
        assert ranked[:1] == [expected], f"{query!r} ranked {ranked}"


def test_the_bm25_index_is_what_answers(indexed, monkeypatch) -> None:
    seen = _capture_lexical_sql(monkeypatch, indexed.state)
    before = _lexical("pg_textsearch", "index")
    assert _ranked(indexed, "invoices generated monthly")[:1] == ["billing.md"]
    assert _lexical("pg_textsearch", "index") == before + 1
    assert any("<@>" in sql for sql in seen)
    # And not the tsvector path's document-frequency round trip.
    assert not any("to_tsquery" in sql for sql in seen)


def test_the_index_follows_the_config(tmp_path: Path, require_extension) -> None:
    """Built when selected, rebuilt when the weights move, dropped when not."""

    _reset(DSN)
    workspace = _corpus(tmp_path)
    engine = _engine(tmp_path, workspace, "pg_textsearch")
    engine.sync_source("docs", "full")
    engine.close()
    assert _bm25_indexes() == {"idx_chunks_fts_bm25_w8_3_2_1"}

    engine = _engine(tmp_path, workspace, "pg_textsearch", title_weight=4.0)
    try:
        assert _bm25_indexes() == {"idx_chunks_fts_bm25_w4_3_2_1"}
        # Built over the rows already there: no re-index was needed.
        assert _ranked(engine, "invoices generated monthly")[:1] == ["billing.md"]
    finally:
        engine.close()

    engine = _engine(tmp_path, workspace, "tsvector")
    engine.close()
    assert _bm25_indexes() == set()


def test_filters_reach_inside_the_index_scan(indexed, monkeypatch) -> None:
    store = SearchStore(indexed.state)
    seen = _capture_lexical_sql(monkeypatch, indexed.state)

    scoped = store.search("gateway", source_name="docs")
    assert scoped and all(hit["provenance"]["source_id"] == "docs" for hit in scoped)
    assert store.search("gateway", source_name="elsewhere") == []

    # The fixture indexes without a taxonomy, so give one chunk a section.
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            "UPDATE chunks SET heading_path = 'Kestrel Runbook > Schedule' "
            "WHERE artifact_id IN (SELECT id FROM artifacts WHERE relative_path = 'runbook.md')"
        )
    section = store.search("gateway", section="kestrel runbook")
    assert [hit["relative_path"] for hit in section] == ["runbook.md"]

    steering = SimpleNamespace(
        exclusions=["notes.md"], prefers=lambda tokens: [], expand=lambda tokens: tokens
    )
    excluded = store.search("gateway", steering=steering)
    assert excluded and "notes.md" not in {hit["relative_path"] for hit in excluded}
    # A preference rule puts its LIKE params in the prior, ahead of the
    # filters' in placeholder order; a swap would bind them to the wrong `?`.
    preferring = SimpleNamespace(
        exclusions=[], prefers=lambda tokens: ["runbook"], expand=lambda tokens: tokens
    )

    def runbook_score(hits: list[dict]) -> float:
        return next(hit["score"] for hit in hits if hit["relative_path"] == "runbook.md")

    plain = store.search("gateway", source_name="docs")
    preferred = store.search("gateway", source_name="docs", steering=preferring)
    assert runbook_score(preferred) > runbook_score(plain)
    # Every answer above came from the BM25 index. The tsvector fallback
    # applies the same filters, so without this a broken BM25 statement
    # would pass every assertion here while ranking on the slow path.
    assert any("<@>" in sql for sql in seen)
    assert not any("ts_rank_cd" in sql for sql in seen)


def test_the_reused_score_is_the_standalone_score(indexed) -> None:
    """The planner hook's score swap must not change the answer.

    With the index scan forced, the select-list score becomes the scan's own
    (``bm25_get_current_score()``); with index scans off it is computed
    standalone. Both must return the same chunks with the same scores — to
    within the index's own rounding: it stores each document's length as a
    one-byte fieldnorm, as Lucene does, so the scan's score differs from an
    exact-length standalone one by about a percent. A stale or foreign score
    from the swap would be off by far more, or attached to the wrong chunk.
    """

    import psycopg

    sql, params = pg_textsearch_sql(
        "gateway restarts nightly",
        DEFAULT_COUNTS,
        prior_sql="1.0",
        prior_params=[],
        memory_join="",
        filter_where="",
        filter_params=[],
        max_results=10,
    )
    statement = sql.replace("?", "%s")

    def run(setting: str) -> tuple[str, list[tuple[str, float]]]:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(setting)
            plan = "\n".join(
                str(row[0])
                for row in conn.execute("EXPLAIN (VERBOSE) " + statement, params).fetchall()
            )
            rows = conn.execute(statement, params).fetchall()
        # Column 11 is `admits`: the statement keeps rows that did not match
        # (sorted last, for its counts) and the caller drops them, as here.
        return plan, [(str(row[0]), float(row[10])) for row in rows if row[11]]

    reused_plan, reused = run("SET enable_seqscan = off")
    standalone_plan, standalone = run("SET enable_indexscan = off")
    assert "bm25_get_current_score" in reused_plan
    assert "bm25_get_current_score" not in standalone_plan
    assert reused and dict(reused).keys() == dict(standalone).keys()
    exact = dict(standalone)
    for chunk, score in reused:
        assert score == pytest.approx(exact[chunk], rel=0.03), chunk
    # Distinct documents score distinctly, so a swap that repeated one row's
    # score down the list would fail above rather than pass on equal numbers.
    assert len({round(score, 6) for _, score in reused}) > 1


def _unfenced(state, query: str, **kwargs) -> list[str]:
    """What the statement before the fence returned, for the same search."""

    from pheasant.search import sqlite_store

    def unfenced(state, text, counts, **parts):
        sql, params = pg_textsearch.pg_textsearch_unfenced_sql(
            text,
            counts,
            prior_sql=parts["prior_sql"],
            prior_params=parts["prior_params"],
            memory_join=parts["memory_join"],
            filter_where=parts["source_where"] + parts["joined_where"],
            filter_params=[*parts["source_params"], *parts["joined_params"]],
            max_results=parts["max_results"],
        )
        return state.rows(sql, tuple(params)), "unfenced"

    fenced = sqlite_store.bm25_rows
    sqlite_store.bm25_rows = unfenced
    try:
        return [hit["relative_path"] for hit in SearchStore(state).search(query, **kwargs)]
    finally:
        sqlite_store.bm25_rows = fenced


def test_the_fenced_scan_returns_what_the_unfenced_statement_did(indexed, monkeypatch) -> None:
    """Widening and the fallback change how the rows are found, not which.

    The depths are shrunk so this corpus exercises every branch: a filter
    the first read satisfies, one that needs the scan widened, and one so
    selective that the widest scan still falls short and the unfenced
    statement answers.
    """

    from pheasant.memory.policy import MemoryPolicy

    monkeypatch.setattr(pg_textsearch, "_BM25_CANDIDATE_FLOOR", 1)
    monkeypatch.setattr(pg_textsearch, "_BM25_CANDIDATE_FACTOR", 1)
    monkeypatch.setattr(pg_textsearch, "_BM25_SCAN_HEADROOM", 1)
    monkeypatch.setattr(pg_textsearch, "_BM25_SCAN_WIDEN", 2)
    monkeypatch.setattr(pg_textsearch, "_BM25_SCAN_CEILING", 2)
    steering = SimpleNamespace(
        exclusions=["notes.md"], prefers=lambda tokens: [], expand=lambda tokens: tokens
    )
    cases = [
        {"max_results": 2},
        {"max_results": 2, "source_name": "docs"},
        {"max_results": 1, "steering": steering},
        {"max_results": 1, "path_prefix": "runbook.md"},
        {"max_results": 1, "path_prefix": "billing.md"},
        {
            "max_results": 3,
            "memory_policy": MemoryPolicy.parse("prefer"),
            "memory_now": "2026-10-14T00:00:00Z",
        },
    ]
    store = SearchStore(indexed.state)
    # The document BM25 ranks second: scoped to it, a one-row first read
    # misses and the widened second read is what finds it.
    second = [hit["relative_path"] for hit in store.search("gateway", max_results=4)][1]
    cases.append({"max_results": 1, "path_prefix": second})
    seen = _capture_lexical_sql(monkeypatch, indexed.state)
    widened, unfenced = _lexical("pg_textsearch", "widened"), _lexical("pg_textsearch", "unfenced")
    statements: list[list[str]] = []
    for kwargs in cases:
        seen.clear()
        fenced = [hit["relative_path"] for hit in store.search("gateway", **kwargs)]
        statements.append([sql for sql in seen if "<@>" in sql])
        assert fenced == _unfenced(indexed.state, "gateway", **kwargs), kwargs
        assert not any("ts_rank_cd" in sql for sql in seen)
    # Every branch ran: answered by the first read, widened, and fell back to
    # the unfenced statement (the one without `admits`) — and said so.
    assert _lexical("pg_textsearch", "widened") > widened
    assert _lexical("pg_textsearch", "unfenced") > unfenced
    assert any(len(run) == 1 for run in statements)
    assert any(len(run) >= 2 for run in statements)
    assert any("admits" not in run[-1] for run in statements)


def test_a_changed_column_weight_ranks_with_tsvector(indexed, monkeypatch) -> None:
    seen = _capture_lexical_sql(monkeypatch, indexed.state)
    store = SearchStore(indexed.state, ranking=RankingParameters(title_weight=4.0))
    before = _lexical("tsvector", "weights_differ")
    assert store.search("invoices generated monthly")[0]["relative_path"] == "billing.md"
    # The slow path a tuning bundle can put a whole region on, made visible.
    assert _lexical("tsvector", "weights_differ") == before + 1
    assert not any("<@>" in sql for sql in seen)
    assert any("ts_rank_cd" in sql for sql in seen)


def test_a_missing_index_falls_back_to_tsvector_not_to_like(indexed, caplog) -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP INDEX idx_chunks_fts_bm25_w8_3_2_1")
    pg_textsearch._bm25_fallback_warned = False
    with caplog.at_level(logging.WARNING, logger="pheasant.search.pg_textsearch"):
        hits = SearchStore(indexed.state).search("invoices generated monthly")
    assert hits[0]["relative_path"] == "billing.md"
    # A ranked score, not the LIKE fallback's flat 1.0.
    assert hits[0]["score"] < 1.0
    assert "ranking with the tsvector index" in caplog.text


def test_an_edit_and_a_deletion_reach_the_index(tmp_path: Path, require_extension) -> None:
    """The index is maintained by the same per-artifact DELETE and INSERT."""

    _reset(DSN)
    workspace = _corpus(tmp_path)
    engine = _engine(tmp_path, workspace, "pg_textsearch")
    try:
        engine.sync_source("docs", "full")
        assert _ranked(engine, "quokka") == []
        (workspace / "billing.md").write_text("# Billing\n\nThe quokka audits invoices.\n")
        engine.sync_source("docs", "incremental")
        assert _ranked(engine, "quokka")[:1] == ["billing.md"]
        (workspace / "billing.md").unlink()
        engine.sync_source("docs", "incremental")
        assert _ranked(engine, "quokka") == []
    finally:
        engine.close()


def test_a_server_without_the_extension_is_a_refusal(
    tmp_path: Path, require_extension, monkeypatch
) -> None:
    """Not a quiet fallback that ranks differently from what the config says."""

    _reset(DSN)
    config = _config(tmp_path, _corpus(tmp_path), "postgres")
    config.storage.lexical_index = "pg_textsearch"
    store = StateStore.from_config(config)
    real = type(store).conn.fget

    class Refusing:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            if "CREATE EXTENSION" in sql:
                raise RuntimeError('extension "pg_textsearch" is not available')
            return self._conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(type(store), "conn", property(lambda self: Refusing(real(self))))
    with pytest.raises(RuntimeError, match="shared_preload_libraries"):
        store.migrate()
