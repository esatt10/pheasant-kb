"""Columnar exports of indexed state, and a SQL surface over them.

`/exports` is defined as regenerable payloads, and this is the regenerable
payload an analyst wants: one Parquet file per table, written into
``<exports_path>/parquet/<kb_id>/``, queryable by DuckDB, pandas, polars,
Spark, DuckDB-WASM in a browser — anything that reads Parquet — with no
pheasant process running and no Python dependency on this package.

**This module never writes to `/state` and never holds a lease.** It opens
the state store read-only-in-practice (it issues nothing but ``SELECT``) and
loads the graph the same way the server does, so exporting while a sync runs
is a WAL reader alongside the writer, not a second writer. That property is
the whole reason the export is safe to run on a schedule.

Why DuckDB, and why only here
-----------------------------
DuckDB is a Parquet writer and a query engine, and it is **not** a state
backend: pheasant's write path is per-artifact ``DELETE`` + re-``INSERT``,
conditional-``UPDATE`` lease claims and ``UPDATE … RETURNING`` queue claims,
which is the workload a bulk-columnar engine is worst at; its full-text index
cannot be maintained incrementally (it is rebuilt wholesale), which would
turn "a re-sync of an untouched corpus does no work" into "every sync
rebuilds the index"; and its single-writer *file lock* blocks other processes
from opening the database at all, where SQLite's WAL is what lets the scaled
compose file mount `/state:ro` on the API replicas while the indexer writes.
So DuckDB earns its place on the read side, off the sync path, owning
nothing — and it is an optional extra, because a region that never exports
should not carry a 20 MB wheel.

Rows are streamed by **keyset pagination on the primary key** rather than
read into a list. The table this exists for is ``chunks``, whose every row
carries the chunk's full text: materializing it means peak memory
proportional to the corpus, which is exactly the size that motivates
exporting in the first place. ``OFFSET`` paging would have been the other
obvious answer and is quadratic — the same shape as the O(N²) FTS delete this
codebase already paid for once. Keyset paging rides the primary-key index on
both backends and, as a side effect, makes row order deterministic.

Rows reach DuckDB as **newline-delimited JSON**, not as bound parameters, and
that is worth more than it looks: DuckDB's Python parameter binder costs about
100 microseconds *per value*, so a 13-column table ran at 1.9 ms per row —
40 seconds for 20,000 rows, and it made the export slower than the sync that
produced the data. Staging the same rows as NDJSON and letting ``read_json``
parse them measured **48 microseconds per row on identical output**, a 40x
difference that is entirely DuckDB's binding path and not the writes. JSON
rather than CSV because it carries real nulls, real numbers and correct
escaping for the quotes, newlines and unicode that live in chunk text — a CSV
staging file would need a quoting scheme, and getting that subtly wrong
corrupts the export silently.

Nothing is staged in a DuckDB *table*: the ``COPY`` reads the NDJSON and
writes the Parquet in one streaming statement, so there is no scratch
database, no spill directory, and no second copy of the corpus in RAM. The
staging file is written per table and deleted as soon as that table's Parquet
lands, so transient disk is bounded by the largest single table rather than by
all of them.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pheasant.persistence.schema import columns_of, primary_key_of

logger = logging.getLogger(__name__)

#: Rows per page. Matches :data:`pheasant.persistence.migrate.BATCH` — large
#: enough that a million-row table is not a million round trips, small enough
#: not to build a multi-GB parameter list.
BATCH = 1_000

#: The portable SQL types :data:`pheasant.persistence.schema.CORE_SCHEMA` uses,
#: mapped onto DuckDB's. Typed rather than all-``VARCHAR`` so ``sum(size_bytes)``
#: works on the exported file without a cast.
_DUCKDB_TYPES = {"TEXT": "VARCHAR", "INTEGER": "BIGINT", "REAL": "DOUBLE"}

#: Where an export lands under ``exports_path``, so it cannot collide with the
#: other things that live in `/exports`.
EXPORT_SUBDIR = "parquet"

#: The manifest written beside the Parquet files.
MANIFEST_NAME = "export.json"

#: Layout version of an export directory, recorded in the manifest.
#:
#: An export is consumed by systems that are not pheasant and do not upgrade
#: with it, so they need a way to notice that the shape changed without
#: diffing schemas. Bump it when a table is removed or renamed, a column is
#: removed or retyped, or an identifier grammar changes — not for an added
#: table or an added column, which a reader written against version 1 keeps
#: handling correctly.
EXPORT_FORMAT_VERSION = 1

#: Rows the Parquet writer buffers before flushing a row group.
#:
#: DuckDB's default is 122,880, which on ``chunks`` — whose rows carry the full
#: chunk text — is a third of a gigabyte of buffer per writer thread, and it is
#: **unspillable**: the memory limit below cannot page it out, so a large
#: default here is what turns a low limit into an out-of-memory error rather
#: than a spill. At 5,000 the buffer is ~13 MB per thread. Measured on a real
#: 16,000-file corpus, 5,000 beat 20,000 on peak RSS at every memory limit
#: (226-260 MB against 277-319 MB) and was never slower.
_ROW_GROUP_SIZE = 5_000

#: What DuckDB may hold before it spills to :data:`_TEMP_DIRNAME`.
#:
#: **This is what makes the export's memory flat instead of proportional.**
#: Left at DuckDB's default (80% of system RAM) the COPY grows with the table:
#: 696 MB at 100k rows, 1,238 MB at 400k, 1,805 MB at 1M, still climbing. With
#: this limit the same three sizes peak at 585 MB, 707 MB and 646 MB — no trend
#: across a 10x range — on a fixture whose every row is unique incompressible
#: text. On a real corpus, where chunks share vocabulary, a 16,000-file export
#: peaks at 258 MB. A container sized for the sync can therefore always run the
#: export, at any corpus size.
#:
#: 512 MB and not lower: 128 MB measured beautifully on a synthetic fixture and
#: then **failed outright on real data** with
#: ``OutOfMemoryException: failed to allocate 48.4 MiB (75.3/122.0 MiB used)``.
#: The fixture repeated one string, so dictionary encoding crushed the writer's
#: working set and understated it by roughly 3x. This codebase has the rule
#: already — a live run finds what the suite cannot — and this is the constant
#: that proves it. Re-measure on real content before touching it.
_MEMORY_LIMIT = "512MB"

#: Where DuckDB spills when it hits :data:`_MEMORY_LIMIT`. Inside the export
#: directory so the spill lands on the volume the operator sized for exports
#: rather than on whatever ``/tmp`` happens to be — which in a container is
#: often a small tmpfs, i.e. RAM, which would defeat the point.
_TEMP_DIRNAME = ".pheasant-export-tmp"

#: State tables that are exported, and what each one is for.
#:
#: Everything absent from this map is absent **on purpose**:
#:
#: * ``chunks_fts`` / ``chunks_vocab`` — derived caches, and dialect-specific
#:   (FTS5 virtual table vs. a ``tsvector`` column). The same argument
#:   :mod:`pheasant.persistence.migrate` makes for not copying them.
#: * ``idp_groups`` / ``idp_sync_meta`` / ``source_audit_events`` — who a
#:   principal is, which groups they are in, and what they did. An export is
#:   a file people pass around; identity and audit data is not that.
#: * ``source_checkpoints`` / ``source_leases`` / ``index_tasks`` /
#:   ``sync_fingerprints`` / ``manifests`` — operational state. Transient,
#:   opaque, and meaningless once detached from the region that wrote it.
#: * ``knowledge_bases`` — one row, reported in the manifest instead.
#: * ``interaction_events`` and the ``evaluation_*`` tables — the observation
#:   and evaluation planes. Both hold raw query text keyed to a principal
#:   partition, which is the identity category the three tables above are
#:   excluded for. ``pheasant eval bootstrap`` is the sanctioned way out of
#:   them, and it hashes principal and session with a per-export salt first.
SQL_TABLES: dict[str, str] = {
    "sources": "One row per configured source: type, path, enabled, last sync status.",
    "artifacts": "One row per indexed file: path, sha256, size, mime type, git branch/commit.",
    "chunks": "The indexed text itself, with heading path and line span. The big one.",
    "symbols": "Code symbols: language, kind, qualified name, signature, line span.",
    "memory_records": "Agent-memory records: scope, subject, validity window, supersedes.",
    "memory_compactions": "Compaction audit trail: each near-duplicate cluster's medoid promotion.",
    "sync_events": "Sync run history: what ran, when, and how it ended.",
    "saved_artifacts": (
        "Kept artifacts: who, when, which conversation and channel, archived or not; "
        "payload_json holds the files, citations and trace (confidence, steps)."
    ),
    "artifact_terms": "Legacy enrichment terms. Large, and mostly historical concept rows.",
}

#: Graph tables, projected out of the in-memory graph rather than out of SQL —
#: the graph is persisted as node-link JSON, not as rows.
#:
#: This is the part of the export that has no equivalent anywhere else:
#: ``graph.latest.json.zst`` has to be decompressed and parsed whole (445k
#: nodes / 1.5M edges came to 928 MB uncompressed) before you can ask it one
#: question. As Parquet, "how many `references` edges cross sources" is a
#: query against a column.
GRAPH_TABLES: dict[str, str] = {
    "graph_nodes": "One row per graph node: id, type, label, owning source, timestamps.",
    "graph_edges": "One row per graph edge: endpoints, relation type, confidence.",
}

EXPORTABLE: dict[str, str] = {**SQL_TABLES, **GRAPH_TABLES}

#: Exported unless ``--table`` says otherwise. ``artifact_terms`` is left out:
#: it reached 1.27M rows on a 2,132-file corpus and is retired as a retrieval
#: path, so paying for it by default would be paying for history.
DEFAULT_TABLES: tuple[str, ...] = (
    "sources",
    "artifacts",
    "chunks",
    "symbols",
    "memory_records",
    "memory_compactions",
    "sync_events",
    "saved_artifacts",
    "graph_nodes",
    "graph_edges",
)

#: Node columns promoted out of the attribute bag. Everything else on a node
#: goes to ``attributes`` as JSON text: node attributes are heterogeneous by
#: type (a symbol has a signature, a heading has a level) and Parquet wants one
#: schema per file.
_GRAPH_NODE_COLUMNS: list[tuple[str, str]] = [
    ("node_id", "TEXT"),
    ("type", "TEXT"),
    ("label", "TEXT"),
    ("source_id", "TEXT"),
    ("artifact_id", "TEXT"),
    ("created_at", "TEXT"),
    ("updated_at", "TEXT"),
    ("attributes", "TEXT"),
]

#: Edge columns. The endpoints are ``from_node``/``to_node`` rather than
#: node-link JSON's ``source``/``target``: in pheasant "source" already means a
#: *configured source*, and a column named ``source`` sitting next to
#: ``sources.parquet`` would be read wrongly by everyone, once each.
_GRAPH_EDGE_COLUMNS: list[tuple[str, str]] = [
    ("from_node", "TEXT"),
    ("to_node", "TEXT"),
    ("type", "TEXT"),
    ("confidence", "REAL"),
    ("created_at", "TEXT"),
    ("attributes", "TEXT"),
]

#: Attribute keys promoted to their own node column, so they are not repeated
#: inside the JSON bag as well.
_PROMOTED_NODE_KEYS = frozenset(
    {"id", "type", "label", "source_id", "artifact_id", "created_at", "updated_at"}
)
_PROMOTED_EDGE_KEYS = frozenset({"type", "confidence", "created_at"})


class AnalyticsUnavailable(RuntimeError):
    """DuckDB is not installed, so there is nothing to write Parquet with."""


class StateUnavailable(RuntimeError):
    """The configured state store could not be read, and why."""


class QueryError(RuntimeError):
    """The SQL handed to :func:`query` did not run.

    Its own type so the CLI can report a typo as a typo without wrapping the
    call in a blanket ``except Exception`` — which would report a genuine bug
    in this module as though the user had mistyped something.
    """


def duckdb_module() -> Any:
    """Import DuckDB, or explain how to get it.

    Lazy and local so importing :mod:`pheasant.analytics` — which the CLI does
    on any ``pheasant export`` invocation, including ``--help`` — costs
    nothing on a region that never exports.
    """

    try:
        import duckdb
    except ModuleNotFoundError as exc:  # pragma: no cover - dep-gated
        raise AnalyticsUnavailable(
            "Parquet export needs DuckDB. Install pheasant with the analytics "
            "extra: pip install 'pheasant-kb[analytics]'"
        ) from exc
    return duckdb


def open_state(config: Any, sqlite_path: Any) -> Any:
    """Open the configured state store for reading, or say why not.

    Exports run unattended — a cron entry, a CI step, a sidecar that ships
    `/exports` somewhere — so every failure has to arrive as a sentence an
    operator can act on rather than as a traceback from inside a driver.

    A Postgres-backed region has three failure modes SQLite does not, and all
    three surfaced raw before this existed: no DSN in the environment
    (``DsnUnavailable`` from four frames down), a database that is up but has
    never been synced (``psycopg.errors.UndefinedTable: relation "sources"
    does not exist``), and a database that is not reachable at all
    (``PoolTimeout`` after the pool's 30-second wait).

    The "has this been synced" probe is ``table_columns('sources')`` because
    it is the one existence check both backends already answer: SQLite swallows
    the error and returns an empty set, Postgres asks ``information_schema``.
    """

    from pheasant.persistence.secrets import DsnUnavailable
    from pheasant.persistence.state_store import StateStore

    backend = str(getattr(getattr(config, "storage", None), "backend", "sqlite") or "sqlite")
    if backend.lower() != "postgres" and not Path(sqlite_path).exists():
        # Name the path: on SQLite "which file did you mean" is the whole
        # question, and it is more use than "there are no tables".
        raise StateUnavailable(f"no indexed state at {sqlite_path}. Run `pheasant sync` first.")
    try:
        state = StateStore.from_config(config, sqlite_path)
    except DsnUnavailable as exc:
        raise StateUnavailable(str(exc)) from exc
    try:
        # `SELECT 1` before asking about tables, and the order is the point.
        # `table_columns` *swallows* a driver error and answers with an empty
        # set, so probing it first reports a database that cannot be opened at
        # all as one that has never been synced — a confident, wrong, and very
        # hard-to-argue-with diagnosis. This statement succeeds on any state
        # that can be read, so a failure here is unambiguous.
        state.backend.rows("SELECT 1 AS ok")
    except Exception as exc:
        state.close()
        raise StateUnavailable(_unreadable(backend, sqlite_path, exc)) from exc
    if not state.backend.table_columns("sources"):
        state.close()
        raise StateUnavailable(
            f"the configured {backend} state has no pheasant tables yet. Run `pheasant sync` first."
        )
    return state


def _unreadable(backend: str, sqlite_path: Any, exc: Exception) -> str:
    """Why the state could not be read, said usefully.

    The SQLite branch exists because of a failure that looks like a permissions
    bug and is not: **SQLite cannot read a WAL database on a read-only
    filesystem at all.** It needs to create the ``-wal`` and ``-shm`` sidecars
    even to read, so a `/state:ro` mount — exactly what
    ``deploy/compose/docker-compose.scale.yml`` gives the api replicas — fails with "unable to
    open database file" while the tables sit right there.

    ``?immutable=1`` does open such a file, and is deliberately not used: it
    asserts the database cannot change, and in the one topology where this
    arises the indexer is writing it through another mount. Trading a correct
    error for possibly-torn data is not a trade this export makes.
    """

    if backend.lower() == "postgres":
        # Refused, timed out, authentication rejected. The driver's own text
        # names which, so it is passed through rather than summarized.
        return f"could not read postgres state: {exc}"
    return (
        f"could not open the SQLite state at {sqlite_path}: {exc}. "
        "If /state is mounted read-only, that is the cause — SQLite needs to "
        "write alongside the database file even to read it. Run the export "
        "where /state is writable, or copy the state directory first."
    )


@dataclass(frozen=True)
class TableExport:
    """One written Parquet file."""

    table: str
    path: Path
    rows: int
    bytes_: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "file": self.path.name,
            "rows": self.rows,
            "bytes": self.bytes_,
        }


def resolve_tables(requested: list[str] | tuple[str, ...] | None) -> list[str]:
    """Validate a table selection, or hand back the default set.

    Raises rather than silently dropping an unknown name: a typo that exports
    six tables instead of seven is a report with a hole in it, and the hole is
    invisible until someone queries the file that is not there.
    """

    if not requested:
        return list(DEFAULT_TABLES)
    unknown = [name for name in requested if name not in EXPORTABLE]
    if unknown:
        raise ValueError(
            f"Unknown table(s): {', '.join(sorted(unknown))}. "
            f"Exportable: {', '.join(sorted(EXPORTABLE))}"
        )
    # Deduplicated, in the order EXPORTABLE declares, so two invocations that
    # name the same tables differently produce the same manifest.
    return [name for name in EXPORTABLE if name in set(requested)]


#: Where a column points, for the tables whose keys are not self-evident. The
#: export is a set of files rather than a database, so nothing in the Parquet
#: carries a foreign key — this is the join map an outside reader would
#: otherwise have to infer, and inferring it wrongly is silent.
FOREIGN_KEYS: dict[tuple[str, str], str] = {
    ("artifacts", "source_id"): "sources.id",
    ("chunks", "artifact_id"): "artifacts.id",
    ("chunks", "source_id"): "sources.id",
    ("symbols", "artifact_id"): "artifacts.id",
    ("symbols", "source_id"): "sources.id",
    ("memory_records", "artifact_id"): "artifacts.id",
    ("memory_records", "source_id"): "sources.id",
    ("memory_records", "supersedes"): "memory_records.record_id",
    ("memory_records", "subsumed_by"): "memory_records.record_id",
    ("memory_compactions", "member_id"): "memory_records.record_id",
    ("memory_compactions", "canonical_id"): "memory_records.record_id",
    ("artifact_terms", "artifact_id"): "artifacts.id",
    ("artifact_terms", "node_id"): "graph_nodes.node_id",
    ("sync_events", "source_id"): "sources.id",
    ("graph_nodes", "source_id"): "sources.id",
    ("graph_nodes", "artifact_id"): "artifacts.id",
    ("graph_edges", "from_node"): "graph_nodes.node_id",
    ("graph_edges", "to_node"): "graph_nodes.node_id",
}


def schema_of(table: str, state: Any = None) -> list[tuple[str, str]]:
    """``[(column, DuckDB type)]`` for one exportable table.

    With a ``state`` store this is the schema the export will actually
    produce, migration-added columns included; without one it is the schema
    the shared DDL declares, which is what a caller with no state directory
    (``pheasant export tables --schema`` with no config) can be told.
    """

    if table == "graph_nodes":
        columns = _GRAPH_NODE_COLUMNS
    elif table == "graph_edges":
        columns = _GRAPH_EDGE_COLUMNS
    elif state is not None:
        columns = export_columns(state, table)
    else:
        columns = columns_of(table)
    return [(name, _DUCKDB_TYPES[kind]) for name, kind in columns]


def export_schema(state: Any = None) -> dict[str, dict[str, Any]]:
    """The full export schema: every table, its key, its columns, its joins.

    Generated rather than written down, so it cannot drift from what the
    export produces. ``pheasant export tables --schema`` renders it, and
    ``tests/test_parquet_export.py`` gates the reference documentation against
    it — a column added to a table and not to the docs fails CI.
    """

    report: dict[str, dict[str, Any]] = {}
    for table, description in EXPORTABLE.items():
        columns = schema_of(table, state)
        report[table] = {
            "description": description,
            "file": f"{table}.parquet",
            "primary_key": primary_key_of(table) if table in SQL_TABLES else None,
            "columns": [
                {
                    "name": name,
                    "type": kind,
                    "references": FOREIGN_KEYS.get((table, name)),
                }
                for name, kind in columns
            ],
        }
    return report


def render_schema(report: dict[str, dict[str, Any]]) -> str:
    """The schema as aligned text, for the terminal."""

    lines: list[str] = []
    for spec in report.values():
        key = f"  (primary key: {spec['primary_key']})" if spec["primary_key"] else ""
        lines.append(f"{spec['file']}{key}")
        lines.append(f"  {spec['description']}")
        width = max(len(column["name"]) for column in spec["columns"])
        for column in spec["columns"]:
            arrow = f"  -> {column['references']}" if column["references"] else ""
            lines.append(f"    {column['name'].ljust(width)}  {column['type']:<8}{arrow}")
        lines.append("")
    return "\n".join(lines).rstrip()


def export_dir_for(exports_path: str | Path, kb_id: str) -> Path:
    """``<exports_path>/parquet/<kb_id>``."""

    return Path(exports_path) / EXPORT_SUBDIR / kb_id


def _sql_literal(value: str) -> str:
    """A single-quoted SQL string literal, with embedded quotes doubled."""

    return "'" + str(value).replace("'", "''") + "'"


def _quote(identifier: str) -> str:
    """A double-quoted identifier. Rejects the only character that could escape it."""

    if '"' in identifier:  # pragma: no cover - no such column exists
        raise ValueError(f"Refusing to quote identifier containing a double quote: {identifier!r}")
    return f'"{identifier}"'


def export_columns(state: Any, table: str) -> list[tuple[str, str]]:
    """The columns to export for one state table, in a stable order.

    :func:`~pheasant.persistence.schema.columns_of` reads the shared DDL,
    which is the right source for *order* and *type* — but it is not the whole
    truth. Columns also arrive through the one-shot additive migrations rule 2
    requires (``ALTER TABLE artifacts ADD COLUMN acl TEXT``), and those are
    invisible to the DDL. Exporting only what the DDL declares would have
    dropped ``acl`` from every export silently, which is precisely the failure
    an export cannot afford: nobody notices a column that was never there.

    So: declared columns first, in declaration order, intersected with what the
    database actually has (an older state directory can predate a schema
    addition, and asking for a column it lacks fails the whole export); then
    any live column the DDL does not know about, sorted, typed ``TEXT``. A
    migration that adds a non-text column will export it as text until it is
    added to ``CORE_SCHEMA`` — visibly wrong beats invisibly missing.
    """

    declared = columns_of(table)
    if not declared:  # pragma: no cover - EXPORTABLE only names core tables
        raise ValueError(f"{table} is not part of the shared schema")
    live = state.backend.table_columns(table)
    if not live:
        return declared
    known = {name for name, _ in declared}
    kept = [(name, kind) for name, kind in declared if name in live]
    kept.extend((name, "TEXT") for name in sorted(live - known))
    return kept


def _stream_table(
    state: Any, table: str, columns: list[tuple[str, str]], batch: int
) -> Iterator[list[tuple]]:
    """Yield pages of rows, paginated on the primary key.

    Goes through ``StateStore.rows`` — which is dialect-translated — rather
    than reaching for a driver cursor, so this works identically on SQLite and
    Postgres. ``migrate.py`` uses a raw SQLite cursor because it only ever
    reads SQLite; an export has to read whichever backend is configured.
    """

    key = primary_key_of(table)
    if key is None:  # pragma: no cover - every exportable table has one
        raise ValueError(f"{table} has no single-column primary key to paginate on")
    projection = ", ".join(_quote(name) for name, _ in columns)
    quoted_key = _quote(key)
    after: Any = None
    while True:
        if after is None:
            page = state.rows(
                f"SELECT {projection} FROM {_quote(table)} ORDER BY {quoted_key} LIMIT ?",
                (batch,),
            )
        else:
            page = state.rows(
                f"SELECT {projection} FROM {_quote(table)} WHERE {quoted_key} > ? "
                f"ORDER BY {quoted_key} LIMIT ?",
                (after, batch),
            )
        if not page:
            return
        yield [tuple(row[name] for name, _ in columns) for row in page]
        after = page[-1][key]
        if len(page) < batch:
            return


def _graph_node_pages(graph: Any, batch: int) -> Iterator[list[tuple]]:
    """Graph nodes as rows, in the graph's own insertion order."""

    page: list[tuple] = []
    for node_id, attrs in graph.nodes(data=True):
        extra = {k: v for k, v in attrs.items() if k not in _PROMOTED_NODE_KEYS}
        page.append(
            (
                str(node_id),
                _text(attrs.get("type")),
                _text(attrs.get("label")),
                _text(attrs.get("source_id")),
                _text(attrs.get("artifact_id")),
                _text(attrs.get("created_at")),
                _text(attrs.get("updated_at")),
                json.dumps(extra, sort_keys=True, default=str),
            )
        )
        if len(page) >= batch:
            yield page
            page = []
    if page:
        yield page


def _graph_edge_pages(graph: Any, batch: int) -> Iterator[list[tuple]]:
    """Graph edges as rows — one row per parallel edge, not per node pair."""

    page: list[tuple] = []
    for (from_node, to_node), edge_map in graph.edges():
        for data in edge_map.values():
            extra = {k: v for k, v in data.items() if k not in _PROMOTED_EDGE_KEYS}
            confidence = data.get("confidence")
            page.append(
                (
                    str(from_node),
                    str(to_node),
                    _text(data.get("type")),
                    float(confidence) if isinstance(confidence, (int, float)) else None,
                    _text(data.get("created_at")),
                    json.dumps(extra, sort_keys=True, default=str),
                )
            )
        if len(page) >= batch:
            yield page
            page = []
    if page:
        yield page


def _text(value: Any) -> str | None:
    """A nullable string column value. ``None`` stays NULL rather than ``"None"``."""

    return None if value is None else str(value)


def export_parquet(
    state: Any,
    *,
    out_dir: str | Path,
    kb_id: str,
    graph: Any = None,
    tables: list[str] | tuple[str, ...] | None = None,
    compression: str = "zstd",
    batch: int = BATCH,
    backend: str = "sqlite",
) -> dict[str, Any]:
    """Write one Parquet file per requested table, plus a manifest.

    ``graph`` is only needed for the ``graph_nodes``/``graph_edges`` tables and
    is otherwise ignored, so a caller that only wants SQL tables never pays for
    loading the graph.

    Each file is written to ``<table>.parquet.tmp`` and renamed into place, the
    same tmp+rename every other durable write in this codebase uses: a reader
    polling the directory sees either the previous export or the new one, never
    a half-written file.
    """

    duckdb = duckdb_module()
    selected = resolve_tables(tables)
    if any(name in GRAPH_TABLES for name in selected) and graph is None:
        raise ValueError(
            "graph_nodes/graph_edges need a loaded graph; pass graph= or deselect them"
        )
    if compression.lower() not in {"zstd", "snappy", "gzip", "uncompressed"}:
        raise ValueError(f"Unsupported Parquet compression: {compression}")

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)

    written: list[TableExport] = []
    # In-memory: the connection only parses NDJSON and writes Parquet, so it
    # holds no table and needs no file of its own.
    connection = duckdb.connect(":memory:")
    spill = directory / _TEMP_DIRNAME
    try:
        spill.mkdir(exist_ok=True)
        connection.execute(f"SET memory_limit = {_sql_literal(_MEMORY_LIMIT)}")
        connection.execute(f"SET temp_directory = {_sql_literal(str(spill))}")
        for table in selected:
            written.append(
                _export_one(
                    connection,
                    state,
                    graph,
                    table,
                    directory,
                    compression=compression,
                    batch=batch,
                )
            )
    finally:
        connection.close()
        # DuckDB clears its own spill on a clean close; this covers the crash
        # that did not get one.
        shutil.rmtree(spill, ignore_errors=True)

    manifest = {
        "format_version": EXPORT_FORMAT_VERSION,
        "kb_id": kb_id,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "pheasant_version": _version(),
        "state_backend": backend,
        "compression": compression,
        # What this run refreshed, as against what the directory holds. A
        # `--table chunks` export into a directory with eight files must not
        # leave a manifest claiming the directory has one.
        "exported": [item.table for item in written],
        "tables": _directory_manifest(directory, written),
    }
    manifest_path = directory / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    manifest["directory"] = str(directory)
    return manifest


def _directory_manifest(directory: Path, written: list[TableExport]) -> list[dict[str, Any]]:
    """Describe every Parquet file in the directory, not just this run's.

    Row counts for files this run did not write come from the Parquet footer
    (``parquet_file_metadata``), which is a metadata read rather than a scan.
    ``modified_at`` is what makes a stale file from an earlier export visible
    as stale instead of passing for current.
    """

    fresh = {item.table: item for item in written}
    entries: list[dict[str, Any]] = []
    for table, file in parquet_files(directory).items():
        item = fresh.get(table)
        rows = item.rows if item is not None else _parquet_rows(file)
        entries.append(
            {
                "table": table,
                "file": file.name,
                "rows": rows,
                "bytes": file.stat().st_size,
                "modified_at": datetime.fromtimestamp(file.stat().st_mtime, UTC).isoformat(
                    timespec="seconds"
                ),
                "refreshed": item is not None,
            }
        )
    return entries


def _parquet_rows(file: Path) -> int | None:
    """Row count from a Parquet footer, or ``None`` if it cannot be read."""

    try:
        duckdb = duckdb_module()
        connection = duckdb.connect(":memory:")
        try:
            found = connection.execute(
                f"SELECT num_rows FROM parquet_file_metadata({_sql_literal(str(file))})"
            ).fetchall()
        finally:
            connection.close()
        return int(sum(row[0] for row in found)) if found else None
    except Exception:  # pragma: no cover - a corrupt file should not fail the run
        logger.warning("Could not read Parquet metadata for %s", file, exc_info=True)
        return None


def _export_one(
    connection: Any,
    state: Any,
    graph: Any,
    table: str,
    directory: Path,
    *,
    compression: str,
    batch: int,
) -> TableExport:
    """Stage one table as NDJSON, then COPY it straight out as Parquet."""

    if table == "graph_nodes":
        columns = _GRAPH_NODE_COLUMNS
        pages: Iterator[list[tuple]] = _graph_node_pages(graph, batch)
    elif table == "graph_edges":
        columns = _GRAPH_EDGE_COLUMNS
        pages = _graph_edge_pages(graph, batch)
    else:
        columns = export_columns(state, table)
        pages = _stream_table(state, table, columns, batch)

    final = directory / f"{table}.parquet"
    tmp = final.with_suffix(".parquet.tmp")
    # Dot-prefixed so a directory glob for `*.parquet` — or a reader listing
    # the export — never sees the staging file even mid-run.
    stage = directory / f".{table}.jsonl.tmp"
    names = [name for name, _ in columns]
    rows = 0
    try:
        with stage.open("w", encoding="utf-8") as handle:
            for page in pages:
                # One write per page rather than per row: the join is cheap and
                # it keeps the syscall count proportional to pages, not rows.
                handle.write(
                    "".join(
                        json.dumps(
                            dict(zip(names, row, strict=True)),
                            ensure_ascii=False,
                            default=str,
                        )
                        + "\n"
                        for row in page
                    )
                )
                rows += len(page)
        connection.execute(
            f"COPY ({_source_select(stage, columns, rows)}) TO {_sql_literal(str(tmp))} "
            f"(FORMAT PARQUET, COMPRESSION {compression.upper()}, "
            f"ROW_GROUP_SIZE {_ROW_GROUP_SIZE})"
        )
        os.replace(tmp, final)
    except BaseException:
        # A partial `.parquet.tmp` is not a readable export and not a name
        # anything will overwrite on the next run; the same cleanup every other
        # tmp+rename write in this codebase does.
        tmp.unlink(missing_ok=True)
        raise
    finally:
        stage.unlink(missing_ok=True)
    logger.info("Exported %s: %d row(s) -> %s", table, rows, final)
    return TableExport(table=table, path=final, rows=rows, bytes_=final.stat().st_size)


def _source_select(stage: Path, columns: list[tuple[str, str]], rows: int) -> str:
    """The SELECT the Parquet COPY reads from.

    With rows, that is the staging file read under an explicit column spec —
    explicit so the Parquet schema is the schema pheasant declares rather than
    whatever DuckDB infers from the first few JSON objects, which for an
    all-null column would be a guess.

    With no rows it is a typed empty projection instead. An empty table still
    owes the export a file: a reader joining against a `memory_records.parquet`
    that does not exist gets an error, where one with zero rows gets the right
    answer.
    """

    if not rows:
        return (
            "SELECT "
            + ", ".join(
                f"CAST(NULL AS {_DUCKDB_TYPES[kind]}) AS {_quote(name)}" for name, kind in columns
            )
            + " WHERE false"
        )
    spec = ", ".join(
        f"{_sql_literal(name)}: {_sql_literal(_DUCKDB_TYPES[kind])}" for name, kind in columns
    )
    return (
        f"SELECT * FROM read_json({_sql_literal(str(stage))}, "
        f"columns = {{{spec}}}, format = 'newline_delimited')"
    )


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("pheasant-kb")
    except Exception:  # pragma: no cover - running from a checkout
        return "unknown"


# --- querying -------------------------------------------------------------


def parquet_files(directory: str | Path) -> dict[str, Path]:
    """``{view name: file}`` for every Parquet file in an export directory."""

    path = Path(directory)
    if not path.is_dir():
        return {}
    return {file.stem: file for file in sorted(path.glob("*.parquet"))}


def query(
    directory: str | Path,
    sql: str,
    *,
    limit: int | None = None,
) -> tuple[list[str], list[tuple]]:
    """Run ``sql`` against an export directory. Returns ``(columns, rows)``.

    Every Parquet file in the directory is registered as a view named after
    the file, so ``SELECT * FROM chunks`` works without a path — which is the
    difference between a documented query surface and a README that tells you
    to interpolate filenames yourself.

    ``limit`` wraps the statement rather than trusting the caller to have
    added one, because the first thing anyone types is ``SELECT * FROM
    chunks`` and the honest response to that on a real corpus is a page, not
    a million rows through a terminal.
    """

    duckdb = duckdb_module()
    views = parquet_files(directory)
    if not views:
        raise FileNotFoundError(
            f"No Parquet files in {directory}. Run `pheasant export parquet` first."
        )
    connection = duckdb.connect(":memory:")
    try:
        for name, file in views.items():
            connection.execute(
                f"CREATE VIEW {_quote(name)} AS "
                f"SELECT * FROM read_parquet({_sql_literal(str(file))})"
            )
        statement = sql.strip().rstrip(";")
        if limit is not None:
            statement = f"SELECT * FROM ({statement}) LIMIT {int(limit)}"
        try:
            cursor = connection.execute(statement)
        except duckdb.Error as exc:
            # DuckDB names the column or table it could not resolve, and even
            # points at the offending character. Repeating it verbatim is more
            # use than any summary this layer could write.
            raise QueryError(str(exc)) from exc
        columns = [description[0] for description in cursor.description or []]
        return columns, cursor.fetchall()
    finally:
        connection.close()


def format_rows(columns: list[str], rows: list[tuple], fmt: str = "table") -> str:
    """Render a result set as an aligned table, JSON or CSV."""

    if fmt == "json":
        return json.dumps(
            [dict(zip(columns, row, strict=True)) for row in rows], indent=2, default=str
        )
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        writer.writerows(rows)
        return buffer.getvalue().rstrip("\n")
    if not columns:
        return ""
    cells = [[_cell(value) for value in row] for row in rows]
    widths = [
        max(len(column), *(len(row[index]) for row in cells)) if cells else len(column)
        for index, column in enumerate(columns)
    ]
    lines = ["  ".join(column.ljust(widths[i]) for i, column in enumerate(columns))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in cells)
    lines.append(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
    return "\n".join(lines)


#: Chunk text is thousands of characters; one such value makes an aligned
#: table unreadable and wraps every other column off the screen.
_CELL_LIMIT = 60


def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    text = str(value).replace("\n", " ")
    return text if len(text) <= _CELL_LIMIT else text[: _CELL_LIMIT - 1] + "…"
