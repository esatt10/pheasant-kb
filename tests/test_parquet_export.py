"""Columnar (Parquet) exports of indexed state, and the SQL surface over them.

The export is a *derived* payload, so what these tests guard is not that it
exists but that it tells the truth: the same row counts as the state store,
every column the shared schema declares, and a manifest that describes the
directory rather than the last invocation.

Two of them are structural gates rather than behaviour tests, and they are the
ones most likely to earn their keep:

* ``test_exported_columns_match_the_live_table`` — the column list comes from
  parsing ``CORE_SCHEMA``, so a column added to a table but not to the parser's
  view of it would silently vanish from every export. Comparing against the
  live database is what makes that impossible.
* ``test_pagination_is_correct_at_every_batch_size`` — the export streams by
  keyset pagination, and an off-by-one there drops or duplicates rows only
  once the corpus is bigger than one page. Forcing tiny pages surfaces it at
  three rows instead of at a million.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.persistence.schema import columns_of, primary_key_of
from pheasant.sync.engine import SyncEngine

duckdb = pytest.importorskip("duckdb", reason="the analytics extra is not installed")

from pheasant import analytics  # noqa: E402

CORPUS = {
    "README.md": "# Gateway service\n\nThe deployment gateway rotates credentials nightly.\n",
    "runbook.md": "# Runbook\n\n## Restarts\n\nThe gateway restarts nightly at 03:00 UTC.\n",
    "notes.md": "# Notes\n\nGateway, gateway, gateway. Mentioned often, explained never.\n",
    "src/app.py": (
        "def rotate_credentials(gateway: str) -> bool:\n"
        '    """Rotate the gateway credentials."""\n'
        "    return True\n"
        "\n"
        "\n"
        "class Gateway:\n"
        "    def restart(self) -> None:\n"
        "        pass\n"
    ),
}


def _config(tmp_path: Path, *, memory: bool = False) -> PheasantConfig:
    workspace = tmp_path / "workspace"
    for name, body in CORPUS.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    sources: list[dict[str, Any]] = [
        {
            "name": "docs",
            "type": "repository",
            "path": str(workspace),
            "include": ["**/*.md", "**/*.py"],
        }
    ]
    if memory:
        sources.append({"name": "agent-memory", "type": "memory", "path": str(tmp_path / "memory")})
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "export-kb",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "sources": sources,
        }
    )


@pytest.fixture()
def indexed(tmp_path: Path) -> Any:
    """A synced engine, its state store and its graph."""

    config = _config(tmp_path)
    engine = SyncEngine(config)
    engine.sync_all("full")
    yield engine
    engine.close()


def _export(engine: Any, out: Path, **kwargs: Any) -> dict[str, Any]:
    return analytics.export_parquet(
        engine.state,
        out_dir=out,
        kb_id=engine.config.knowledge_base_id,
        graph=engine.graph_builder.graph,
        **kwargs,
    )


def _rows(out: Path, sql: str) -> list[tuple]:
    return analytics.query(out, sql)[1]


# --- what lands on disk ---------------------------------------------------


def test_export_writes_one_parquet_per_table_plus_a_manifest(indexed: Any, tmp_path: Path) -> None:
    out = tmp_path / "out"
    report = _export(indexed, out)

    files = {path.name for path in out.glob("*")}
    for table in analytics.DEFAULT_TABLES:
        assert f"{table}.parquet" in files
    assert analytics.MANIFEST_NAME in files
    # The scratch database is working state, not an export artifact.
    assert not list(out.glob("*.duckdb"))
    assert not list(out.glob("*.tmp"))

    manifest = json.loads((out / analytics.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["kb_id"] == "export-kb"
    assert manifest["state_backend"] == "postgres"
    assert sorted(manifest["exported"]) == sorted(analytics.DEFAULT_TABLES)
    assert report["tables"]


def test_row_counts_match_the_state_store(indexed: Any, tmp_path: Path) -> None:
    """The export is only worth having if it agrees with what it was made from."""

    out = tmp_path / "out"
    _export(indexed, out)

    for table in ("artifacts", "chunks", "symbols", "sources"):
        in_state = int(indexed.state.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"])
        exported = _rows(out, f"SELECT count(*) AS n FROM {table}")[0][0]
        assert exported == in_state > 0, table

    graph = indexed.graph_builder.graph
    assert _rows(out, "SELECT count(*) AS n FROM graph_nodes")[0][0] == graph.number_of_nodes()
    assert _rows(out, "SELECT count(*) AS n FROM graph_edges")[0][0] == graph.number_of_edges()


def test_exported_columns_match_the_live_table(indexed: Any, tmp_path: Path) -> None:
    """A column the DDL parser cannot see is a column that silently stops
    being exported. Compare against the database rather than against a list."""

    out = tmp_path / "out"
    _export(indexed, out, tables=["artifacts", "chunks", "symbols", "memory_records"])

    for table in ("artifacts", "chunks", "symbols", "memory_records"):
        live = indexed.state.backend.table_columns(table)
        exported = {row[0] for row in _rows(out, f"DESCRIBE {table}")}
        assert exported == live, table
        # And the parser's order is the declaration order, not set order.
        assert [name for name, _ in columns_of(table)][0] == (primary_key_of(table) or "")


def test_values_survive_the_round_trip(indexed: Any, tmp_path: Path) -> None:
    out = tmp_path / "out"
    _export(indexed, out)

    exported = {
        row[0]: (row[1], row[2])
        for row in _rows(out, "SELECT relative_path, sha256, size_bytes FROM artifacts")
    }
    for row in indexed.state.rows("SELECT relative_path, sha256, size_bytes FROM artifacts"):
        assert exported[row["relative_path"]] == (row["sha256"], row["size_bytes"])

    # Typed, not stringly typed: an analytics export whose numbers are text
    # makes `sum(size_bytes)` a cast at every call site.
    types = {row[0]: row[1] for row in _rows(out, "DESCRIBE artifacts")}
    assert types["size_bytes"] == "BIGINT"
    assert types["relative_path"] == "VARCHAR"


def test_graph_edges_carry_relation_types_and_attributes(indexed: Any, tmp_path: Path) -> None:
    out = tmp_path / "out"
    _export(indexed, out, tables=["graph_nodes", "graph_edges"])

    types = {row[0] for row in _rows(out, "SELECT DISTINCT type FROM graph_edges")}
    assert "has_chunk" in types
    assert "contains" in types

    # The attribute bag is JSON text, so a symbol's language is reachable
    # without a second export format.
    languages = _rows(
        out,
        "SELECT DISTINCT json_extract_string(attributes, '$.language') AS language "
        "FROM graph_nodes WHERE type = 'symbol'",
    )
    assert "python" in {row[0] for row in languages}


# --- streaming correctness -------------------------------------------------


@pytest.mark.parametrize("batch", [1, 2, 3, 1000])
def test_pagination_is_correct_at_every_batch_size(
    indexed: Any, tmp_path: Path, batch: int
) -> None:
    """Keyset pagination must neither drop nor duplicate a row at a boundary."""

    out = tmp_path / f"out-{batch}"
    _export(indexed, out, tables=["artifacts", "chunks"], batch=batch)

    for table in ("artifacts", "chunks"):
        expected = {row["id"] for row in indexed.state.rows(f"SELECT id FROM {table}")}
        exported = [row[0] for row in _rows(out, f"SELECT id FROM {table}")]
        assert len(exported) == len(expected), f"{table} at batch={batch}"
        assert set(exported) == expected


def test_re_exporting_unchanged_state_is_stable(indexed: Any, tmp_path: Path) -> None:
    out = tmp_path / "out"
    first = _export(indexed, out, tables=["artifacts", "chunks", "symbols"])
    second = _export(indexed, out, tables=["artifacts", "chunks", "symbols"])

    counts = {(entry["table"], entry["rows"]) for entry in first["tables"]}
    assert counts == {(entry["table"], entry["rows"]) for entry in second["tables"]}


def test_a_partial_export_still_describes_the_whole_directory(indexed: Any, tmp_path: Path) -> None:
    """`--table chunks` into a populated directory must not leave a manifest
    claiming the directory holds one table."""

    out = tmp_path / "out"
    _export(indexed, out)
    _export(indexed, out, tables=["chunks"])

    manifest = json.loads((out / analytics.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["exported"] == ["chunks"]
    listed = {entry["table"]: entry for entry in manifest["tables"]}
    assert sorted(listed) == sorted(analytics.DEFAULT_TABLES)
    assert listed["chunks"]["refreshed"] is True
    assert listed["artifacts"]["refreshed"] is False
    # Row counts for the files this run did not write come from the Parquet
    # footer, so they are real numbers rather than nulls.
    assert listed["artifacts"]["rows"] > 0


def test_staging_files_never_survive_the_export(indexed: Any, tmp_path: Path) -> None:
    """Rows reach DuckDB through a per-table NDJSON staging file. One left
    behind is a second, uncompressed copy of the corpus sitting in `/exports`
    under a name nothing ever cleans up."""

    out = tmp_path / "out"
    _export(indexed, out)

    assert not list(out.glob("*.jsonl*")), "staging file survived a successful export"
    assert not list(out.glob(".*")), "hidden working file survived a successful export"
    assert not list(out.glob("*.tmp"))
    # And nothing that is not an export artifact at all.
    assert {path.name for path in out.iterdir()} == {
        f"{table}.parquet" for table in analytics.DEFAULT_TABLES
    } | {analytics.MANIFEST_NAME}


def test_a_failed_export_leaves_no_debris(indexed: Any, tmp_path: Path) -> None:
    """A crash mid-COPY must not leave a partial `.parquet` that reads as real,
    nor the staging file that fed it."""

    out = tmp_path / "out"

    class Boom(Exception):
        pass

    real_export_one = analytics._export_one

    def fail_on_chunks(connection, state, graph, table, directory, **kwargs):
        if table == "chunks":
            raise Boom("COPY failed")
        return real_export_one(connection, state, graph, table, directory, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(analytics, "_export_one", fail_on_chunks)
        with pytest.raises(Boom):
            _export(indexed, out, tables=["artifacts", "chunks"])

    assert not list(out.glob("*.jsonl*"))
    assert not list(out.glob("*.tmp"))
    assert not (out / "chunks.parquet").exists()


def test_an_empty_table_still_gets_a_typed_file(indexed: Any, tmp_path: Path) -> None:
    """A reader joining against a file that does not exist gets an error; one
    with zero rows gets the right answer. So an empty table owes a file, and
    that file owes the right schema."""

    out = tmp_path / "out"
    _export(indexed, out, tables=["memory_records"])

    assert (out / "memory_records.parquet").exists()
    assert _rows(out, "SELECT count(*) FROM memory_records")[0][0] == 0
    declared = {name for name, _ in columns_of("memory_records")}
    assert {row[0] for row in _rows(out, "DESCRIBE memory_records")} >= declared
    types = {row[0]: row[1] for row in _rows(out, "DESCRIBE memory_records")}
    assert types["salience"] == "DOUBLE"
    assert types["uses"] == "BIGINT"


def test_text_with_quotes_newlines_and_unicode_survives(indexed: Any, tmp_path: Path) -> None:
    """The staging format has to carry chunk text intact. A CSV staging file
    would need a quoting scheme, and getting that subtly wrong corrupts the
    export silently — which is why the staging format is JSON."""

    hostile = "He said \"hi\" 'twice'\nthen \\ left — ünïcode, \ttabbed\r\n"
    artifact = indexed.state.rows("SELECT id, source_id FROM artifacts LIMIT 1")[0]
    indexed.state.rows(
        "UPDATE chunks SET text = ? WHERE artifact_id = ?", (hostile, artifact["id"])
    )
    indexed.state.backend.commit()

    out = tmp_path / "out"
    _export(indexed, out, tables=["chunks"])

    texts = {row[0] for row in _rows(out, "SELECT text FROM chunks")}
    assert hostile in texts


# --- the query surface -----------------------------------------------------


def test_query_registers_one_view_per_file_and_joins_across_them(
    indexed: Any, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    _export(indexed, out)

    columns, rows = analytics.query(
        out,
        "SELECT a.relative_path, count(c.id) AS chunks "
        "FROM artifacts a JOIN chunks c ON c.artifact_id = a.id "
        "GROUP BY 1 ORDER BY 1",
    )
    assert columns == ["relative_path", "chunks"]
    assert {row[0] for row in rows} == set(CORPUS)


def test_query_limit_wraps_the_caller_statement(indexed: Any, tmp_path: Path) -> None:
    out = tmp_path / "out"
    _export(indexed, out)

    _, rows = analytics.query(out, "SELECT * FROM chunks", limit=2)
    assert len(rows) == 2
    # A trailing semicolon is what everyone pastes; it must not break the wrap.
    _, rows = analytics.query(out, "SELECT * FROM chunks;", limit=1)
    assert len(rows) == 1


def test_query_without_an_export_says_what_to_run(indexed: Any, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="pheasant export parquet"):
        analytics.query(tmp_path / "never-exported", "SELECT 1")


def test_a_bad_query_is_reported_as_a_query_error(indexed: Any, tmp_path: Path) -> None:
    """Typed, so the CLI does not have to catch everything to catch a typo —
    which would report a bug in this module as though the user mistyped."""

    out = tmp_path / "out"
    _export(indexed, out, tables=["artifacts"])

    with pytest.raises(analytics.QueryError, match="nosuchcolumn"):
        analytics.query(out, "SELECT nosuchcolumn FROM artifacts")
    with pytest.raises(analytics.QueryError):
        analytics.query(out, "SELECT * FROM nosuchtable")


def test_unknown_table_names_are_rejected_rather_than_dropped() -> None:
    """Silently exporting six of seven requested tables is a report with an
    invisible hole in it."""

    with pytest.raises(ValueError, match="chunkz"):
        analytics.resolve_tables(["chunks", "chunkz"])
    assert analytics.resolve_tables(None) == list(analytics.DEFAULT_TABLES)
    # Deduplicated and ordered, so two spellings of one selection agree.
    assert analytics.resolve_tables(["chunks", "artifacts", "chunks"]) == ["artifacts", "chunks"]


def test_graph_tables_need_a_graph(indexed: Any, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="graph"):
        analytics.export_parquet(
            indexed.state,
            out_dir=tmp_path / "out",
            kb_id="export-kb",
            graph=None,
            tables=["graph_nodes"],
        )


def test_identity_and_operational_tables_are_not_exportable() -> None:
    """An export is a file people pass around. Group membership, audit trails
    and queue state are not that, and the omission is deliberate."""

    for table in (
        "idp_groups",
        "idp_sync_meta",
        "source_audit_events",
        "index_tasks",
        "source_leases",
        "source_checkpoints",
        "chunks_fts",
    ):
        assert table not in analytics.EXPORTABLE


def test_missing_duckdb_explains_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure a user without the extra actually sees."""

    import builtins

    real_import = builtins.__import__

    def fail(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "duckdb":
            raise ModuleNotFoundError("No module named 'duckdb'", name="duckdb")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail)
    with pytest.raises(analytics.AnalyticsUnavailable, match=r"pheasant-kb\[analytics\]"):
        analytics.duckdb_module()


# --- the CLI seam ----------------------------------------------------------


def _write_config(tmp_path: Path) -> Path:
    """The same corpus, as a YAML file the CLI can be pointed at."""

    import yaml

    config = _config(tmp_path)
    path = tmp_path / "pheasant.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "pheasant": {
                    "name": "export-kb",
                    "state_path": str(tmp_path / "state"),
                    "workspace_root": str(tmp_path / "workspace"),
                    "exports_path": str(tmp_path / "exports"),
                },
                "sources": [
                    {
                        "name": "docs",
                        "type": "repository",
                        "path": str(tmp_path / "workspace"),
                        "include": ["**/*.md", "**/*.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert config.knowledge_base_id
    return path


def test_cli_exports_and_queries_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from pheasant.cli import main

    config = _write_config(tmp_path)
    assert main(["sync", "--all", "-c", str(config)]) == 0
    capsys.readouterr()

    assert main(["export", "parquet", "-c", str(config)]) == 0
    written = capsys.readouterr().out
    assert "artifacts.parquet" in written
    assert "graph_edges.parquet" in written

    # The default output location is the one the docs name, and `query`
    # defaults to the same place — so the two commands compose without the
    # user having to carry a path between them.
    default_dir = analytics.export_dir_for(tmp_path / "exports", "export-kb")
    assert (default_dir / "artifacts.parquet").exists()

    assert main(["export", "query", "-c", str(config), "SELECT count(*) FROM artifacts"]) == 0
    assert "(1 row)" in capsys.readouterr().out

    assert (
        main(
            [
                "export",
                "query",
                "-c",
                str(config),
                "--format",
                "json",
                "SELECT relative_path FROM artifacts ORDER BY 1",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert {row["relative_path"] for row in payload} == set(CORPUS)


def test_cli_lists_exportable_tables(capsys: pytest.CaptureFixture) -> None:
    from pheasant.cli import main

    assert main(["export", "tables"]) == 0
    listing = capsys.readouterr().out
    for table in analytics.EXPORTABLE:
        assert table in listing


# --- opening state, on whichever backend ------------------------------------


@pytest.fixture()
def empty_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A database with no pheasant tables: up, and never synced."""

    import uuid

    from tests.conftest import _admin_execute, _drop_database, database_dsn

    name = f"pheasant_empty_{uuid.uuid4().hex[:12]}"
    _admin_execute(f'CREATE DATABASE "{name}"')
    monkeypatch.setenv("PHEASANT_DATABASE_URL", database_dsn(name))
    yield name
    _drop_database(name)


def test_cli_export_without_indexed_state_is_actionable(
    tmp_path: Path, capsys: pytest.CaptureFixture, empty_database: str
) -> None:
    """The honest answer to exporting an unindexed region is "run sync", not a
    missing-table error from inside DuckDB."""

    from pheasant.cli import main

    config = _write_config(tmp_path)
    assert main(["export", "parquet", "-c", str(config)]) == 1
    assert "run `pheasant sync` first" in capsys.readouterr().err.lower()


def test_open_state_rejects_an_empty_database(tmp_path: Path, empty_database: str) -> None:
    """A database that is up but holds no pheasant tables is "not synced yet",
    not "relation chunks does not exist" from inside the Parquet writer."""

    with pytest.raises(analytics.StateUnavailable, match="no pheasant tables yet"):
        analytics.open_state(_config(tmp_path))


def test_open_state_distinguishes_unreadable_from_unsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Cannot open this at all" must not be reported as "nothing indexed yet".

    `table_columns` swallows a driver error and answers with an empty set, so a
    probe built on it would turn an unreachable database into a confident,
    wrong diagnosis.
    """

    from tests.conftest import database_dsn

    monkeypatch.setenv("PHEASANT_DATABASE_URL", database_dsn("pheasant_does_not_exist"))
    with pytest.raises(analytics.StateUnavailable) as raised:
        analytics.open_state(_config(tmp_path))
    message = str(raised.value)
    assert "could not read the state database" in message
    assert "no pheasant tables yet" not in message


def test_open_state_explains_a_missing_postgres_dsn(tmp_path: Path) -> None:
    """The most common Postgres misconfiguration, and it must not arrive as a
    traceback four frames inside `resolve_dsn`. Runs without Postgres: no DSN
    is exactly the condition under test."""

    config = _config(tmp_path)
    config.storage.backend = "postgres"
    config.storage.dsn_env = "PHEASANT_TEST_DSN_THAT_IS_NOT_SET"
    with pytest.raises(analytics.StateUnavailable, match="PHEASANT_TEST_DSN_THAT_IS_NOT_SET"):
        analytics.open_state(config)


def test_open_state_returns_a_usable_store(indexed: Any, tmp_path: Path) -> None:
    state = analytics.open_state(indexed.config)
    try:
        assert state.rows("SELECT count(*) AS n FROM artifacts")[0]["n"] > 0
    finally:
        state.close()


# --- the schema is a published contract ------------------------------------


SCHEMA_DOC = Path(__file__).resolve().parents[1] / "docs" / "reference" / "export-schema.md"


def test_every_exported_table_and_column_is_documented(indexed: Any) -> None:
    """PR gate, in the shape `test_config_surface_freshness.py` uses.

    An export is consumed by systems that are not pheasant and cannot read the
    source. A column added to a state table therefore appears in exports —
    `export_columns` unions the live table — and is invisible to every reader
    until someone writes it down. This makes that a CI failure rather than a
    support question.
    """

    doc = SCHEMA_DOC.read_text(encoding="utf-8")
    missing: list[str] = []
    for table in analytics.EXPORTABLE:
        if f"`{table}.parquet`" not in doc and f"{table}.parquet" not in doc:
            missing.append(table)
        for column, _type in analytics.schema_of(table, indexed.state):
            if f"`{column}`" not in doc:
                missing.append(f"{table}.{column}")
    assert not missing, "docs/reference/export-schema.md does not document: " + ", ".join(
        sorted(set(missing))
    )


def test_documented_joins_hold_in_a_real_export(indexed: Any, tmp_path: Path) -> None:
    """Every foreign key the schema advertises must actually join.

    The export is a set of files, so nothing enforces referential integrity —
    a join documented wrongly fails silently, as an outside reader quietly
    losing rows. So assert it on real data instead of asserting the map
    against itself.
    """

    out = tmp_path / "out"
    _export(indexed, out)
    exported = set(analytics.parquet_files(out))

    checked = 0
    for (table, column), target in analytics.FOREIGN_KEYS.items():
        target_table, target_column = target.split(".")
        if table not in exported or target_table not in exported:
            continue
        dangling = _rows(
            out,
            f"SELECT count(*) FROM {table} WHERE {column} IS NOT NULL "
            f"AND {column} NOT IN (SELECT {target_column} FROM {target_table} "
            f"WHERE {target_column} IS NOT NULL)",
        )[0][0]
        assert dangling == 0, f"{table}.{column} -> {target} has {dangling} dangling row(s)"
        checked += 1
    assert checked >= 10, f"only {checked} joins were exercised; the fixture is too thin"


def test_the_manifest_declares_a_format_version(indexed: Any, tmp_path: Path) -> None:
    """An outside reader needs to know when the shape changed without diffing
    schemas, so the version is part of the payload, not of the release notes."""

    out = tmp_path / "out"
    report = _export(indexed, out, tables=["artifacts"])
    assert report["format_version"] == analytics.EXPORT_FORMAT_VERSION
    manifest = json.loads((out / analytics.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["format_version"] == analytics.EXPORT_FORMAT_VERSION


def test_schema_report_reflects_the_live_tables(indexed: Any) -> None:
    """`pheasant export tables --schema -c …` must describe what will actually
    be written, migration-added columns included."""

    live = analytics.export_schema(indexed.state)
    declared = analytics.export_schema()

    assert set(live) == set(analytics.EXPORTABLE)
    live_artifacts = {column["name"] for column in live["artifacts"]["columns"]}
    declared_artifacts = {column["name"] for column in declared["artifacts"]["columns"]}
    # `acl` arrives through a one-shot migration, so it is in the database and
    # not in CORE_SCHEMA. Exactly the drift the schema report has to survive.
    assert "acl" in live_artifacts
    assert "acl" not in declared_artifacts
    assert live["artifacts"]["primary_key"] == "id"
    assert live["memory_records"]["primary_key"] == "record_id"
    assert live["graph_edges"]["primary_key"] is None

    references = {column["name"]: column["references"] for column in live["chunks"]["columns"]}
    assert references["artifact_id"] == "artifacts.id"
    assert analytics.render_schema(live).startswith("sources.parquet")


def test_memory_text_is_recoverable_by_joining_chunks(tmp_path: Path) -> None:
    """The claim the schema doc makes about agent memory: the projection is in
    `memory_records`, the record's words are in `chunks`, and a correction
    leaves both versions in the export with the old one dated."""

    from datetime import UTC, datetime

    from pheasant.artifacts.store import MemoryStore

    config = _config(tmp_path, memory=True)
    store = MemoryStore(tmp_path / "memory")
    first, _ = store.append(
        "The rollout password is BANANAS.",
        scope="org",
        subject="rollout",
        now=datetime(2026, 7, 1, tzinfo=UTC),
    )
    store.append(
        "The rollout password is CHERRIES.",
        scope="org",
        subject="rollout",
        supersedes=first.record_id,
        now=datetime(2026, 8, 1, tzinfo=UTC),
    )
    engine = SyncEngine(config)
    try:
        engine.sync_all("full")
        out = tmp_path / "out"
        analytics.export_parquet(
            engine.state,
            out_dir=out,
            kb_id=config.knowledge_base_id,
            graph=engine.graph_builder.graph,
        )
    finally:
        engine.close()

    rows = _rows(
        out,
        "SELECT m.scope, m.valid_until IS NULL AS current, c.text "
        "FROM memory_records m JOIN chunks c ON c.artifact_id = m.artifact_id "
        "ORDER BY m.asserted_at",
    )
    assert len(rows) == 2
    assert [row[1] for row in rows] == [False, True], "the corrected record must be dated, not gone"
    assert "BANANAS" in rows[0][2] and "CHERRIES" in rows[1][2]
    # And the correction is in the graph as an edge, not only as a column.
    assert _rows(out, "SELECT count(*) FROM graph_edges WHERE type = 'supersedes'")[0][0] == 1
