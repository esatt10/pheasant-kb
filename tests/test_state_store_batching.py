"""The artifact commit writes the same rows now that it batches them.

`replace_artifact_chunks` and `replace_artifact_enrichment` issue one
``executemany`` per table instead of one ``execute`` per row, because on
Postgres every row was its own round trip under the sole commit authority's
mutex (measured on loopback: 57ms → 25ms per artifact of 30 chunks, 40 terms
and 8 symbols). Batching is only an optimization if nothing it writes moves,
so these tests pin the rows themselves: ids, order, the FTS title/path split,
the term de-duplication and its index-based ids.

Both backends run where one is available; Postgres skips without
``PHEASANT_TEST_POSTGRES_DSN``, exactly as the parity suite does.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from pheasant.persistence.state_store import StateStore

DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()

postgres = pytest.mark.skipif(
    not DSN,
    reason="set PHEASANT_TEST_POSTGRES_DSN to a throwaway database to run the Postgres half",
)


def _reset(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


def _store(kind: str, tmp_path: Path, *, pool_size: int = 2) -> StateStore:
    if kind == "sqlite":
        store = StateStore(tmp_path / "state.sqlite")
    else:
        from pheasant.persistence.backends import PostgresBackend

        _reset(DSN)
        store = StateStore(backend=PostgresBackend(DSN, pool_size=pool_size))
    store.migrate()
    store.upsert_knowledge_base("kb", "kb", None, "hash", "now")
    store.upsert_source("docs", "kb", "docs", "markdown_folder", "/docs", True, {})
    return store


BACKENDS = ["sqlite", pytest.param("postgres", marks=postgres)]

ARTIFACT_ID = "file:docs:guide/setup.md:branch=main"


def _artifact() -> dict[str, Any]:
    return {
        "id": ARTIFACT_ID,
        "source_id": "docs",
        "type": "markdown",
        "path": "/docs/guide/setup.md",
        "relative_path": "guide/setup.md",
        "mime_type": "text/markdown",
        "size_bytes": 10,
        "sha256": "a" * 64,
        "mtime": 0.0,
        "git_branch": None,
        "git_commit": None,
        "last_indexed_at": "now",
        "status": "indexed",
    }


def _chunks(count: int, generation: str) -> list[dict[str, Any]]:
    return [
        {
            "id": f"chunk:docs:guide/setup.md:sha256={generation}{index}:chunk={index:04d}",
            "artifact_id": ARTIFACT_ID,
            "source_id": "docs",
            "chunk_index": index,
            "heading_path": f"Setup > Step {index}" if index else None,
            "start_line": index * 10,
            "end_line": index * 10 + 9,
            "text": f"step {index} of the {generation} setup",
            "text_hash": f"{generation}{index}",
            "summary": f"step {index}",
            "token_estimate": 7,
        }
        for index in range(count)
    ]


def _term(node: str, term: str) -> dict[str, Any]:
    return {
        "node_id": node,
        "node_type": "entity",
        "term": term,
        "normalized_term": term.lower(),
        "weight": 2.0,
        "metadata": {"from": "test"},
    }


def _symbol(name: str) -> dict[str, Any]:
    return {
        "id": f"{ARTIFACT_ID}:symbol:{name}",
        "artifact_id": ARTIFACT_ID,
        "source_id": "docs",
        "language": "python",
        "symbol_type": "function",
        "name": name,
        "qualified_name": f"setup.{name}",
        "start_line": 1,
        "end_line": 2,
        "signature": f"def {name}()",
        "docstring_summary": "",
    }


@pytest.mark.parametrize("kind", BACKENDS)
def test_a_replace_writes_exactly_the_rows_it_always_did(kind: str, tmp_path: Path) -> None:
    store = _store(kind, tmp_path)
    try:
        store.replace_artifact_chunks(_artifact(), _chunks(3, "old"))
        # Re-indexing replaces rather than appends: two chunks now, new ids.
        store.replace_artifact_chunks(_artifact(), _chunks(2, "new"))

        chunks = store.rows(
            "SELECT id, chunk_index, heading_path, text FROM chunks "
            "WHERE artifact_id=? ORDER BY chunk_index",
            (ARTIFACT_ID,),
        )
        assert [dict(row) for row in chunks] == [
            {
                "id": "chunk:docs:guide/setup.md:sha256=new0:chunk=0000",
                "chunk_index": 0,
                "heading_path": None,
                "text": "step 0 of the new setup",
            },
            {
                "id": "chunk:docs:guide/setup.md:sha256=new1:chunk=0001",
                "chunk_index": 1,
                "heading_path": "Setup > Step 1",
                "text": "step 1 of the new setup",
            },
        ]
        fts = store.rows(
            "SELECT chunk_id, title, path, heading_path, text FROM chunks_fts "
            "WHERE artifact_id=? ORDER BY chunk_id",
            (ARTIFACT_ID,),
        )
        assert [dict(row) for row in fts] == [
            {
                "chunk_id": "chunk:docs:guide/setup.md:sha256=new0:chunk=0000",
                # Basename and full path stay two distinct BM25 signals, and a
                # chunk with no heading indexes an empty string, not NULL.
                "title": "setup.md",
                "path": "guide/setup.md",
                "heading_path": "",
                "text": "step 0 of the new setup",
            },
            {
                "chunk_id": "chunk:docs:guide/setup.md:sha256=new1:chunk=0001",
                "title": "setup.md",
                "path": "guide/setup.md",
                "heading_path": "Setup > Step 1",
                "text": "step 1 of the new setup",
            },
        ]
    finally:
        store.close()


@pytest.mark.parametrize("kind", BACKENDS)
def test_enrichment_keeps_its_dedup_and_its_index_based_ids(kind: str, tmp_path: Path) -> None:
    store = _store(kind, tmp_path)
    try:
        store.replace_artifact_chunks(_artifact(), _chunks(1, "g"))
        terms = [_term("n1", "Alpha"), _term("n1", "ALPHA"), _term("n2", "Beta")]
        store.replace_artifact_enrichment(ARTIFACT_ID, "docs", terms, [_symbol("run")])
        # A second pass replaces both tables rather than accumulating.
        store.replace_artifact_enrichment(
            ARTIFACT_ID, "docs", terms, [_symbol("run"), _symbol("stop")]
        )

        rows = store.rows(
            "SELECT id, node_id, term, normalized_term, weight, metadata_json "
            "FROM artifact_terms WHERE artifact_id=? ORDER BY id",
            (ARTIFACT_ID,),
        )
        # The duplicate (same node, type and normalized term) is dropped, and
        # the survivor after it keeps its *original* index: 0000 and 0002.
        assert [dict(row) for row in rows] == [
            {
                "id": f"{ARTIFACT_ID}:term:0000",
                "node_id": "n1",
                "term": "Alpha",
                "normalized_term": "alpha",
                "weight": 2.0,
                "metadata_json": '{"from": "test"}',
            },
            {
                "id": f"{ARTIFACT_ID}:term:0002",
                "node_id": "n2",
                "term": "Beta",
                "normalized_term": "beta",
                "weight": 2.0,
                "metadata_json": '{"from": "test"}',
            },
        ]
        symbols = store.rows(
            "SELECT name, qualified_name FROM symbols WHERE artifact_id=? ORDER BY name",
            (ARTIFACT_ID,),
        )
        assert [dict(row) for row in symbols] == [
            {"name": "run", "qualified_name": "setup.run"},
            {"name": "stop", "qualified_name": "setup.stop"},
        ]
    finally:
        store.close()


@pytest.mark.parametrize("kind", BACKENDS)
def test_an_artifact_with_no_chunks_or_enrichment_is_still_written(
    kind: str, tmp_path: Path
) -> None:
    """`executemany` over nothing is skipped, and the artifact row still lands."""

    store = _store(kind, tmp_path)
    try:
        store.replace_artifact_chunks(_artifact(), _chunks(2, "g"))
        store.replace_artifact_chunks(_artifact(), [])
        store.replace_artifact_enrichment(ARTIFACT_ID, "docs", [], [])
        assert [row["id"] for row in store.rows("SELECT id FROM artifacts")] == [ARTIFACT_ID]
        for table in ("chunks", "chunks_fts", "artifact_terms", "symbols"):
            count = store.rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
            assert int(count) == 0, table
    finally:
        store.close()


@postgres
def test_a_standalone_read_leaves_the_write_path_transactional(tmp_path: Path) -> None:
    """Reads outside a transaction run in autocommit; writes must not.

    A standalone read used to cost `BEGIN` + the statement + `ROLLBACK`. It
    now runs with autocommit on, and the flag has to be back off before the
    next write, or `replace_artifact_chunks`' delete-then-insert would commit
    each half separately. Read-your-writes inside a pending transaction must
    also survive: that read is *not* standalone.
    """

    # One connection, so the write below is guaranteed to reuse the very
    # connection the read ran on. With two, the pool can hand back the other
    # one and the test passes whether or not the flag was restored -- which
    # is exactly what it did when the restore was deleted to check it.
    store = _store("postgres", tmp_path, pool_size=1)
    try:
        backend = store.backend
        assert store.rows("SELECT 1 AS one")[0]["one"] == 1
        assert backend._conn().autocommit is False
        backend.release()

        backend.execute(
            "INSERT INTO sync_fingerprints(scope, fingerprint, updated_at) VALUES(?,?,?)",
            ("probe", "uncommitted", "now"),
        )
        # Inside pending work: sees its own uncommitted row.
        seen = store.rows("SELECT fingerprint FROM sync_fingerprints WHERE scope=?", ("probe",))
        assert [row["fingerprint"] for row in seen] == ["uncommitted"]
        # Discarding the unit of work discards the row: it was never
        # autocommitted.
        backend._abort()
        assert (
            store.rows("SELECT fingerprint FROM sync_fingerprints WHERE scope=?", ("probe",)) == []
        )

        # And a failed standalone read returns the connection transactional.
        import psycopg

        with pytest.raises(psycopg.errors.UndefinedTable):
            store.rows("SELECT * FROM no_such_table")
        backend.execute(
            "INSERT INTO sync_fingerprints(scope, fingerprint, updated_at) VALUES(?,?,?)",
            ("probe", "kept", "now"),
        )
        backend.commit()
        assert [
            row["fingerprint"]
            for row in store.rows(
                "SELECT fingerprint FROM sync_fingerprints WHERE scope=?", ("probe",)
            )
        ] == ["kept"]
    finally:
        store.close()


@postgres
def test_a_standalone_read_sends_no_transaction_statements(tmp_path: Path) -> None:
    """The round trips themselves, where the server can count them."""

    import psycopg

    store = _store("postgres", tmp_path)
    try:
        with psycopg.connect(DSN, autocommit=True) as admin:
            try:
                admin.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
                admin.execute("SELECT pg_stat_statements_reset()")
            except psycopg.Error:
                pytest.skip("pg_stat_statements is not loaded on this server")
            for _ in range(20):
                store.rows("SELECT 7 AS standalone_read_probe")
            counts = {
                str(query).strip().upper(): int(calls)
                for query, calls in admin.execute(
                    "SELECT query, calls FROM pg_stat_statements "
                    "WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())"
                ).fetchall()
            }
        assert counts.get("SELECT $1 AS STANDALONE_READ_PROBE") == 20
        assert "BEGIN" not in counts
        assert "ROLLBACK" not in counts
    finally:
        store.close()
