from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pheasant.persistence.backends import SqliteBackend, StateBackend, open_backend
from pheasant.persistence.schema import schema_for
from pheasant.persistence.secrets import resolve_dsn
from pheasant.persistence.sql import SQLITE

logger = logging.getLogger(__name__)

#: How long a statement waits for a competing writer before giving up.
#:
#: Was 5 s, which is fine when the only writer is a short sync. It is **not**
#: fine during a multi-hour index of a large repository with the UI open: the
#: sync worker writes continuously in one process while the API server serves
#: `/overview`, `/jobs` and `/graph/slice` polls in another, and a 12,667-file
#: run of microsoft/vscode died on
#:
#:     sqlite3.OperationalError: database is locked
#:
#: after ~40 minutes. WAL lets readers and one writer coexist, but checkpoints
#: and the enrichment pass's multi-statement transactions still take the write
#: lock long enough to blow a 5-second budget under that load. Waiting a minute
#: costs nothing when the alternative is losing the whole index.
BUSY_TIMEOUT_MS = 60_000

#: The columns both write-path fold lookups read (Phase 1/3). One string so
#: `find_canonical_record` and `foldable_record` cannot disagree on the
#: empty-string-is-unset tier corner `MemoryPolicy.sql_predicate` documents.
_FOLD_COLUMNS = (
    "SELECT record_id, COALESCE(NULLIF(tier, ''), 'hot') AS tier, subsumed_by, valid_until "
    "FROM memory_records"
)


def _basename(path: str | None) -> str:
    """Final path segment, for the FTS ``title`` column.

    Deliberately not ``Path(...).name``: these are POSIX-style relative paths
    recorded at index time, and on Windows ``PurePath`` would also split on
    backslashes, so an indexed path containing one would produce a different
    title than the same corpus indexed on Linux.
    """
    text = str(path or "")
    return text.rsplit("/", 1)[-1] if "/" in text else text


#: Re-exported for callers that referenced ``state_store.SCHEMA``; the
#: definitions now live in :mod:`pheasant.persistence.schema`, per dialect.
SCHEMA = schema_for(SQLITE)

# Bump whenever the core DDL or an additive migration changes. Postgres fleet
# members use the marker to avoid replaying the complete schema at startup.
SCHEMA_VERSION = "1"


class StateStore:
    """SQLite-backed state, one connection per thread.

    WAL mode (set below) is SQLite's sanctioned way to let multiple threads
    read the same database truly concurrently — but only when each thread
    uses its *own* connection/cursor. An earlier version of this class shared
    one `sqlite3.Connection` across every thread: the search paths
    (text/vector/graph) run in parallel across threads (hybrid.py's per-mode
    pool, nested inside multi_search's per-query pool for the agentic
    workflow), and overlapping execute()+fetch calls on one shared connection
    interleaved cursor state, handing back a Row missing an expected column
    (reproduced as `row["chunk_id"]` raising IndexError from
    vector_store.search() under concurrent load). The fix at the time was a
    lock serializing every read through `rows()` — correct, but it throws
    away the concurrency WAL mode exists to provide: with four queries
    fanning out across three search modes, a global lock turned an
    embarrassingly-parallel retrieve step into a mostly-serial one (measured:
    the first, multi-query retrieve of an agentic run took 22-29s; a later
    single-query retrieve over the same data took 1.3-2s).

    Thread-local connections remove the shared state instead of locking
    around it: every thread gets its own connection and cursor, so there is
    nothing left to interleave, and reads run genuinely in parallel again.
    Writes still only ever happen from the sync worker process (a different
    process entirely — see sync/worker.py), so this process's connections are
    all readers as far as WAL's MVCC snapshot isolation is concerned.
    """

    def __init__(self, path: str | Path | None = None, *, backend: StateBackend | None = None):
        """Open the store.

        ``path`` is what every existing caller passes and still means "a
        SQLite file here". ``backend`` is the Phase-35.2 seam: pass a
        :class:`~pheasant.persistence.backends.StateBackend` (see
        :func:`from_config`) to run on Postgres instead. Exactly one is
        required.
        """

        if backend is None:
            if path is None:
                raise ValueError("StateStore needs either a path or a backend")
            backend = SqliteBackend(path)
        self.backend = backend
        # Kept for the many callers that read `store.path` (backups, logging,
        # the migration command). None on a backend that has no file.
        self.path = getattr(backend, "path", None)

    @classmethod
    def from_config(cls, config: Any, path: str | Path | None = None) -> StateStore:
        """Build the store the config asks for.

        Falls back to ``path`` for the SQLite default so a caller can keep
        passing the location it already computed from :class:`StatePaths`.
        """

        storage = getattr(config, "storage", None)
        backend_name = str(getattr(storage, "backend", "sqlite") or "sqlite")
        if backend_name.lower() != "postgres":
            return cls(path)
        return cls(
            backend=open_backend(
                path,
                backend="postgres",
                dsn=resolve_dsn(storage),
                pool_size=int(getattr(storage, "pool_size", 10) or 10),
            )
        )

    @property
    def dialect(self):
        return self.backend.dialect

    @property
    def conn(self):
        """The live connection.

        On SQLite this is the real ``sqlite3.Connection``, unchanged. On
        Postgres it is a connection-shaped adapter, so the 76 ``self.conn``
        uses below — including ten ``with self.conn:`` transaction blocks —
        work on both without being rewritten. Rewriting them would have put 76
        freshly edited lines on the default path to gain nothing.
        """

        return self.backend.conn

    def migrate(self) -> None:
        if self.backend.dialect.is_postgres:
            self._migrate_postgres()
            return
        self.backend.executescript(schema_for(self.backend.dialect))
        self._migrate_artifact_terms_node_lookup()
        # Step 32.1 — one-shot idempotent column add (additive; existing rows
        # keep acl NULL = "source expressed no ACL", the pre-32 semantics).
        if "acl" not in self.backend.table_columns("artifacts"):
            self.conn.execute("ALTER TABLE artifacts ADD COLUMN acl TEXT")
        # Phase 1 (agent-speed memory compaction) — one-shot idempotent
        # column adds. Existing rows keep canon_key NULL (never reinforced
        # against, since nothing can match a NULL) and observations 0/
        # last_seen NULL/variants NULL, which are exactly the pre-Phase-1
        # values for a record that predates reinforcement. `canon_key` is
        # recomputed for every row on the next projection rebuild regardless
        # (it is derived, not earned — see schema.py), so leaving it NULL
        # here is only ever a transient state until that next sync.
        memory_columns = self.backend.table_columns("memory_records")
        if memory_columns and "canon_key" not in memory_columns:
            self.conn.execute("ALTER TABLE memory_records ADD COLUMN canon_key TEXT")
        if memory_columns and "observations" not in memory_columns:
            self.conn.execute(
                "ALTER TABLE memory_records ADD COLUMN observations INTEGER NOT NULL DEFAULT 0"
            )
        if memory_columns and "last_seen" not in memory_columns:
            self.conn.execute("ALTER TABLE memory_records ADD COLUMN last_seen TEXT")
        if memory_columns and "variants" not in memory_columns:
            self.conn.execute("ALTER TABLE memory_records ADD COLUMN variants TEXT")
        # Only safe to create once the column above is guaranteed present —
        # either it was in this database's original CREATE TABLE, or the
        # ALTER just above added it. See the comment in schema.py.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_records_canon_key ON memory_records(canon_key)"
        )
        # Phase 3 — same shape. Existing rows keep tier='hot' (their default
        # is exactly the pre-Phase-3 state: a record nothing has clustered
        # yet is not demoted) and subsumed_by NULL.
        if memory_columns and "tier" not in memory_columns:
            self.conn.execute(
                "ALTER TABLE memory_records ADD COLUMN tier TEXT NOT NULL DEFAULT 'hot'"
            )
        if memory_columns and "subsumed_by" not in memory_columns:
            self.conn.execute("ALTER TABLE memory_records ADD COLUMN subsumed_by TEXT")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_records_tier ON memory_records(tier, scope)"
        )
        # The observation plane's free-text and result-path columns. Guarded
        # like every other additive column here: `executescript` creates the
        # table only when it is absent, so a /state written before these
        # existed keeps the old shape unless something adds them.
        interaction_columns = self.backend.table_columns("interaction_events")
        if interaction_columns and "answer_text" not in interaction_columns:
            self.conn.execute("ALTER TABLE interaction_events ADD COLUMN answer_text TEXT")
        if interaction_columns and "result_paths_json" not in interaction_columns:
            self.conn.execute("ALTER TABLE interaction_events ADD COLUMN result_paths_json TEXT")
        self._migrate_evaluation_progress()
        self._migrate_tuning_control()
        self.conn.commit()
        self._migrate_fts_titles()

    #: The evaluation run's durable progress columns, added after the table
    #: shipped. Guarded like every other additive column: `CREATE TABLE IF NOT
    #: EXISTS` cannot widen a table that already exists, so a `/state` written
    #: before these would otherwise fail every progress write with a missing
    #: column -- and progress is the one thing a watcher outside the process
    #: has to be able to read.
    _EVALUATION_RUN_COLUMNS: tuple[tuple[str, str], ...] = (
        ("phase", "TEXT"),
        ("phase_detail", "TEXT"),
        ("completed_units", "INTEGER NOT NULL DEFAULT 0"),
        ("total_units", "INTEGER NOT NULL DEFAULT 0"),
        ("heartbeat_at", "TEXT"),
        ("owner", "TEXT"),
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("error", "TEXT"),
    )

    #: Control columns on ``tuning_experiments``, added after the table
    #: shipped. Guarded for exactly the reason the evaluation ones are: a
    #: `/state` written before these existed keeps the old shape, because
    #: `CREATE TABLE IF NOT EXISTS` cannot widen a table that already exists.
    #:
    #: ``cancel_requested`` is a *column* rather than a process flag on
    #: purpose. A batch runs in a thread inside whichever replica started it,
    #: and the person cancelling is talking to whichever replica their browser
    #: reached — usually a different one. A cancel that only set an in-memory
    #: flag would silently do nothing in a fleet, which is the same lesson
    #: progress learned when it lived in a job registry.
    _TUNING_EXPERIMENT_COLUMNS: tuple[tuple[str, str], ...] = (
        ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
        ("cancel_requested_by", "TEXT"),
    )

    def _migrate_tuning_control(self) -> None:
        """Add the control columns to a pre-existing ``tuning_experiments``."""

        existing = self.backend.table_columns("tuning_experiments")
        if not existing:
            return
        for column, declaration in self._TUNING_EXPERIMENT_COLUMNS:
            if column not in existing:
                self.conn.execute(
                    f"ALTER TABLE tuning_experiments ADD COLUMN {column} {declaration}"
                )

    def _migrate_evaluation_progress(self) -> None:
        """Add the run-progress columns to a pre-existing ``evaluation_runs``.

        Said once here and called from both migration paths, because Postgres
        returns early from :meth:`migrate` and an additive column stated in only
        one place exists on exactly one backend -- a mistake this file has
        already made once, and the reason the comment below the Postgres
        ``required`` map exists.
        """

        existing = self.backend.table_columns("evaluation_runs")
        if not existing:
            return
        for column, declaration in self._EVALUATION_RUN_COLUMNS:
            if column not in existing:
                self.conn.execute(f"ALTER TABLE evaluation_runs ADD COLUMN {column} {declaration}")
        # Only safe once `heartbeat_at` is guaranteed present — either it was in
        # this database's original CREATE TABLE, or the ALTER above just added
        # it. Declaring it in the schema script instead fails the whole
        # migration with "no such column" on any /state written before the
        # progress columns existed, which is how this was found.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_evaluation_runs_live "
            "ON evaluation_runs(kb_id, status, heartbeat_at)"
        )

    def _migrate_artifact_terms_node_lookup(self) -> None:
        """Replace the legacy wide artifact-term B-tree index once.

        ``node_id`` is a parser-produced text identifier, not a bounded key.
        PostgreSQL rejects a B-tree row over one third of a page, which used to
        turn one unusually long heading or symbol into a failed whole-source
        sync. The old index serves only retired historical concept rollups;
        the replacement retains their type/artifact filter without indexing an
        unbounded value. This changes derived DDL only, never term rows.
        """

        marker = "artifact_terms_node_lookup_v2"
        applied = self.rows("SELECT value FROM pheasant_schema_meta WHERE key=?", (marker,))
        if applied:
            return
        self.conn.execute("DROP INDEX IF EXISTS idx_artifact_terms_node_lookup")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_artifact_terms_type_artifact "
            "ON artifact_terms(node_type, artifact_id)"
        )
        self.conn.execute(
            "INSERT INTO pheasant_schema_meta(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (marker, "1", datetime.now(UTC).isoformat()),
        )
        self.conn.commit()

    def _migrate_postgres(self) -> None:
        """Run DDL once across a concurrently starting fleet.

        db-init, API, indexer and short-lived sync children often start
        together. Replaying the full schema from all of them deadlocks
        PostgreSQL catalog locks against ordinary chunk writes. The backend's
        advisory lock elects one migrator; the marker makes later calls cheap.
        """

        with self.backend.schema_lock():
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS pheasant_schema_meta ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            self.conn.commit()
            version = self.rows("SELECT value FROM pheasant_schema_meta WHERE key=?", ("core",))
            required = {
                "artifacts": "acl",
                "index_tasks": "id",
                "source_leases": "source_id",
                "sync_fingerprints": "scope",
                "log_tasks": "id",
                # The *newest* column, not just the table: a marker written
                # before these existed would otherwise skip the DDL below and
                # leave every ledger insert failing on a missing column.
                "interaction_events": "answer_text",
                "memory_candidates": "id",
                # Same rule, newest column: a marker written before the
                # evaluation plane's progress columns existed must not let this
                # skip the DDL and leave every progress write failing.
                "evaluation_runs": "heartbeat_at",
                "evaluation_replays": "id",
            }
            schema_present = all(
                column in self.backend.table_columns(table) for table, column in required.items()
            )
            if version and str(version[0]["value"]) == SCHEMA_VERSION and schema_present:
                self._migrate_artifact_terms_node_lookup()
                return
            self.backend.executescript(schema_for(self.backend.dialect))
            if "acl" not in self.backend.table_columns("artifacts"):
                self.conn.execute("ALTER TABLE artifacts ADD COLUMN acl TEXT")
            # Postgres returns early from `migrate()`, so the guarded column
            # adds there never run here -- an additive column needs saying
            # twice or it exists on exactly one backend. `CREATE TABLE IF NOT
            # EXISTS` above cannot widen a table that already exists.
            interaction_columns = self.backend.table_columns("interaction_events")
            if interaction_columns and "answer_text" not in interaction_columns:
                self.conn.execute("ALTER TABLE interaction_events ADD COLUMN answer_text TEXT")
            if interaction_columns and "result_paths_json" not in interaction_columns:
                self.conn.execute(
                    "ALTER TABLE interaction_events ADD COLUMN result_paths_json TEXT"
                )
            self._migrate_evaluation_progress()
            # Both paths, for the reason `_migrate_evaluation_progress` states:
            # Postgres returns early from `migrate`, so an additive column
            # named in only one place exists on exactly one backend.
            self._migrate_tuning_control()
            self._migrate_artifact_terms_node_lookup()
            self.conn.execute(
                "INSERT INTO pheasant_schema_meta(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                ("core", SCHEMA_VERSION, datetime.now(UTC).isoformat()),
            )
            self.conn.commit()

    def _migrate_fts_titles(self) -> None:
        """Re-point ``chunks_fts.title`` at the file's basename.

        It used to hold the full relative path — the identical string already
        in ``path`` — so a filename carried no signal BM25 could weight
        separately, and searching "readme" ranked by body brevity instead of
        by name. Rebuilding is safe and needs no re-index: ``chunks_fts`` is a
        derived cache over ``chunks`` + ``artifacts``, exactly like the graph
        node index, so it can be regenerated from the tables that are the
        truth. No user data is touched.

        One-shot and idempotent: the presence of a '/' in any stored title is
        the old format, and after the rebuild there is nothing left to detect.
        """
        if self.backend.dialect.fulltext != "fts5":
            # Postgres derives its search vector from `chunks` directly, so
            # there is no separate FTS table whose titles could be stale.
            return
        try:
            stale = self.conn.execute(
                "SELECT 1 FROM chunks_fts WHERE title LIKE '%/%' LIMIT 1"
            ).fetchone()
        except sqlite3.Error:  # table absent or FTS5 unavailable — nothing to do
            return
        if stale is None:
            return
        with self.conn:
            self.conn.execute("DELETE FROM chunks_fts")
            self.conn.execute(
                """INSERT INTO chunks_fts(
                       chunk_id, source_id, artifact_id, title, path, heading_path, text)
                   SELECT chunks.id, chunks.source_id, chunks.artifact_id,
                          replace(
                              COALESCE(artifacts.relative_path, artifacts.path),
                              rtrim(COALESCE(artifacts.relative_path, artifacts.path),
                                    replace(COALESCE(artifacts.relative_path, artifacts.path),
                                            '/', '')),
                              ''),
                          COALESCE(artifacts.relative_path, artifacts.path),
                          COALESCE(chunks.heading_path, ''),
                          chunks.text
                   FROM chunks JOIN artifacts ON artifacts.id = chunks.artifact_id"""
            )

    def get_fingerprint(self, scope: str) -> str | None:
        """What this scope was last indexed with, or None if never recorded."""

        rows = self.rows("SELECT fingerprint FROM sync_fingerprints WHERE scope=?", (scope,))
        return str(rows[0]["fingerprint"]) if rows else None

    def set_fingerprint(self, scope: str, fingerprint: str, updated_at: str) -> None:
        """Record a scope's fingerprint after a successful pass over it."""

        with self.conn:
            self.conn.execute(
                "INSERT INTO sync_fingerprints (scope, fingerprint, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET fingerprint=excluded.fingerprint, "
                "updated_at=excluded.updated_at",
                (scope, fingerprint, updated_at),
            )

    def clear_fingerprint(self, scope: str) -> None:
        """Drop a scope's fingerprint row. A no-op if it was never set."""

        with self.conn:
            self.conn.execute("DELETE FROM sync_fingerprints WHERE scope=?", (scope,))

    def replace_idp_groups(self, mapping: dict[str, list[str]], synced_at: str) -> bool:
        """Persist one IdP sync pass (Step 32.4). Returns True when rows changed.

        The mapping replace is transactional; ``synced_at`` bumps on every
        call (an unchanged directory still counts as a fresh heartbeat —
        that heartbeat is the staleness-SLA clock).
        """
        desired = {(principal, group) for principal, groups in mapping.items() for group in groups}
        current = {
            (row[0], row[1]) for row in self.rows("SELECT principal, group_name FROM idp_groups")
        }
        changed = desired != current
        with self.conn:
            if changed:
                self.conn.execute("DELETE FROM idp_groups")
                self.conn.executemany(
                    "INSERT INTO idp_groups(principal, group_name) VALUES(?,?)",
                    sorted(desired),
                )
            self.conn.execute(
                """INSERT INTO idp_sync_meta(key, value) VALUES('synced_at', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (synced_at,),
            )
        return changed

    def idp_groups_for(self, principals: set[str]) -> set[str]:
        """Group names the IdP mapping grants any of these principal spellings.

        ``rows``, not ``conn.execute``: this runs on the request path, and see
        :meth:`artifact_acls` for what routing a read through the write path
        costs on Postgres.
        """
        if not principals:
            return set()
        ordered = sorted(principals)
        placeholders = ",".join("?" for _ in ordered)
        rows = self.rows(
            f"SELECT group_name FROM idp_groups WHERE principal IN ({placeholders})",
            tuple(ordered),
        )
        return {row[0] for row in rows}

    def idp_synced_at(self) -> str | None:
        rows = self.rows("SELECT value FROM idp_sync_meta WHERE key='synced_at'")
        return str(rows[0]["value"]) if rows else None

    def idp_principal_count(self) -> int:
        rows = self.rows("SELECT COUNT(DISTINCT principal) AS c FROM idp_groups")
        return int(rows[0]["c"]) if rows else 0

    def artifact_acls(self, artifact_ids: list[str]) -> dict[str, str | None]:
        """The stored ACL JSON (or None) for each artifact id (Step 32.2).

        ``rows``, not ``conn.execute``. Under Postgres those are two different
        paths: ``rows`` returns the pooled connection when the statement leaves
        nothing pending, while ``conn.execute`` goes through
        :meth:`~pheasant.persistence.backends.PostgresBackend.statement`, which
        deliberately holds the connection because it is the *write* path.

        This method is a read, and it runs on every search under
        ``security.acl_enforced``. Routing it through the write path marked the
        calling thread dirty with nothing to commit, so its connection was
        pinned for the life of that thread -- and Starlette serves sync
        endpoints from a 40-slot threadpool against a pool of ``pool_size``
        (10). The eleventh thread to serve a search blocked for 30s and got
        ``PoolTimeout``; every one after it did too. Exactly the failure
        ``PostgresBackend._conn`` records having fixed once, reached again
        through a read on the sqlite-shaped connection.
        """
        if not artifact_ids:
            return {}
        placeholders = ",".join("?" for _ in artifact_ids)
        rows = self.rows(
            f"SELECT id, acl FROM artifacts WHERE id IN ({placeholders})",
            tuple(artifact_ids),
        )
        return {row[0]: row[1] for row in rows}

    def upsert_knowledge_base(
        self,
        kb_id: str,
        name: str,
        description: str | None,
        cfg_hash: str,
        now: str,
    ) -> None:
        self.conn.execute(
            """INSERT INTO knowledge_bases(
                id,name,description,config_hash,created_at,updated_at
            )
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                config_hash=excluded.config_hash,
                updated_at=excluded.updated_at""",
            (kb_id, name, description, cfg_hash, now, now),
        )
        self.conn.commit()

    def upsert_source(
        self,
        source_id: str,
        kb_id: str,
        name: str,
        source_type: str,
        path: str,
        enabled: bool,
        config: dict[str, Any],
        status: str = "registered",
    ) -> None:
        self.conn.execute(
            """INSERT INTO sources(
                id,knowledge_base_id,name,type,path,enabled,config_json,last_status
            )
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                type=excluded.type,
                path=excluded.path,
                enabled=excluded.enabled,
                config_json=excluded.config_json,
                last_status=COALESCE(sources.last_status, excluded.last_status)""",
            (
                source_id,
                kb_id,
                name,
                source_type,
                path,
                int(enabled),
                json.dumps(config, default=str),
                status,
            ),
        )
        self.conn.commit()

    def replace_artifact_chunks(
        self,
        artifact: dict[str, Any],
        chunks: list[dict[str, Any]],
        *,
        fresh: bool = False,
    ) -> None:
        """Write one artifact and its chunks, replacing whatever was there.

        ``fresh`` says the caller has already cleared this source, so there is
        nothing to replace. It exists because of a measured O(N²), found by
        the Phase 35.7 capacity sweep: indexing 500 → 8,000 files took 1.9s →
        164.7s, tripling for every doubling.

        The cause is that ``chunks_fts.artifact_id`` is an **UNINDEXED** FTS5
        column, so ``DELETE FROM chunks_fts WHERE artifact_id=?`` is a full
        scan of a table that grows with the corpus — once per artifact. On a
        *full* sync `delete_source_artifacts` has already emptied the source's
        rows before the loop starts, so each of those scans searches an
        ever-larger table in order to delete **nothing**.

        Same class as the ``graph_nodes_fts`` scan recorded in CLAUDE.md, and
        the same fix: do not ask an unindexed column a question whose answer
        is already known.

        **SQLite only.** On Postgres ``chunks_fts`` is an ordinary table with
        ``idx_chunks_fts_artifact``, so the delete was always indexed — the
        bug is a property of FTS5's UNINDEXED columns, not of the query.

        A sweep of every remaining FTS5 write (2026-08-16) found no other
        unmitigated instance: ``delete_source_artifacts`` scans on
        ``source_id`` but runs **once per sync** rather than once per
        artifact and has to visit those rows to delete them anyway, and
        ``graph_nodes_fts``'s batched delete is bounded by
        ``SyncEngine._INDEX_REBUILD_THRESHOLD``, above which it rebuilds
        wholesale instead.
        """

        with self.conn:
            self.conn.execute(
                """INSERT INTO artifacts(
                    id,source_id,type,path,relative_path,mime_type,size_bytes,
                    sha256,mtime,git_branch,git_commit,last_indexed_at,status,acl
                )
                VALUES(
                    :id,:source_id,:type,:path,:relative_path,:mime_type,:size_bytes,
                    :sha256,:mtime,:git_branch,:git_commit,:last_indexed_at,:status,:acl
                )
                ON CONFLICT(id) DO UPDATE SET
                    type=excluded.type,
                    path=excluded.path,
                    relative_path=excluded.relative_path,
                    mime_type=excluded.mime_type,
                    size_bytes=excluded.size_bytes,
                    sha256=excluded.sha256,
                    mtime=excluded.mtime,
                    git_branch=excluded.git_branch,
                    git_commit=excluded.git_commit,
                    last_indexed_at=excluded.last_indexed_at,
                    status=excluded.status,
                    acl=excluded.acl""",
                {"acl": None, **artifact},
            )
            if not fresh:
                self.conn.execute("DELETE FROM chunks WHERE artifact_id=?", (artifact["id"],))
                self.conn.execute("DELETE FROM chunks_fts WHERE artifact_id=?", (artifact["id"],))
                self.conn.execute("DELETE FROM symbols WHERE artifact_id=?", (artifact["id"],))
                self.conn.execute(
                    "DELETE FROM artifact_terms WHERE artifact_id=?", (artifact["id"],)
                )
            for chunk in chunks:
                self.conn.execute(
                    """INSERT INTO chunks(
                        id,artifact_id,source_id,chunk_index,heading_path,start_line,
                        end_line,text,text_hash,summary,token_estimate
                    )
                    VALUES(
                        :id,:artifact_id,:source_id,:chunk_index,:heading_path,
                        :start_line,:end_line,:text,:text_hash,:summary,:token_estimate
                    )""",
                    chunk,
                )
                self.conn.execute(
                    """INSERT INTO chunks_fts(
                        chunk_id,source_id,artifact_id,title,path,heading_path,text
                    )
                    VALUES(?,?,?,?,?,?,?)""",
                    (
                        chunk["id"],
                        chunk["source_id"],
                        chunk["artifact_id"],
                        # title = basename, path = full relative path. Two
                        # distinct signals so BM25 can weight a filename match
                        # above a body match (see sqlite_store._BM25_WEIGHTS);
                        # storing the same string twice made them one.
                        _basename(artifact["relative_path"] or artifact["path"]),
                        artifact["relative_path"] or artifact["path"],
                        chunk.get("heading_path") or "",
                        chunk["text"],
                    ),
                )

    def replace_artifact_enrichment(
        self,
        artifact_id: str,
        source_id: str,
        terms: list[dict[str, Any]],
        symbols: list[dict[str, Any]],
    ) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM symbols WHERE artifact_id=?", (artifact_id,))
            self.conn.execute("DELETE FROM artifact_terms WHERE artifact_id=?", (artifact_id,))
            seen_terms: set[tuple[str, str, str]] = set()
            for index, term in enumerate(terms):
                key = (
                    term["node_id"],
                    term["node_type"],
                    term["normalized_term"],
                )
                if key in seen_terms:
                    continue
                seen_terms.add(key)
                self.conn.execute(
                    """INSERT INTO artifact_terms(
                        id,artifact_id,source_id,node_id,node_type,term,
                        normalized_term,weight,metadata_json
                    )
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        f"{artifact_id}:term:{index:04d}",
                        artifact_id,
                        source_id,
                        term["node_id"],
                        term["node_type"],
                        term["term"],
                        term["normalized_term"],
                        float(term.get("weight") or 1.0),
                        json.dumps(term.get("metadata") or {}, default=str),
                    ),
                )
            for symbol in symbols:
                self.conn.execute(
                    """INSERT INTO symbols(
                        id,artifact_id,source_id,language,symbol_type,name,
                        qualified_name,start_line,end_line,signature,docstring_summary
                    )
                    VALUES(
                        :id,:artifact_id,:source_id,:language,:symbol_type,:name,
                        :qualified_name,:start_line,:end_line,:signature,:docstring_summary
                    )""",
                    symbol,
                )

    def mark_source_indexed(self, source_id: str, now: str, status: str = "healthy") -> None:
        self.conn.execute(
            "UPDATE sources SET last_indexed_at=?, last_status=? WHERE id=?",
            (now, status, source_id),
        )
        self.conn.commit()

    def mark_source_status(self, source_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE sources SET last_status=? WHERE id=?",
            (status, source_id),
        )
        self.conn.commit()

    def set_source_enabled(self, source_id: str, enabled: bool, status: str | None = None) -> None:
        if status is None:
            self.conn.execute(
                "UPDATE sources SET enabled=? WHERE id=?",
                (int(enabled), source_id),
            )
        else:
            self.conn.execute(
                "UPDATE sources SET enabled=?, last_status=? WHERE id=?",
                (int(enabled), status, source_id),
            )
        self.conn.commit()

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        rows = self.rows(
            "SELECT * FROM sources WHERE id=? OR name=? LIMIT 1",
            (source_id, source_id),
        )
        return dict(rows[0]) if rows else None

    def get_source_checkpoint(self, source_id: str) -> dict[str, Any] | None:
        rows = self.rows(
            """SELECT source_id, connector_type, cursor_json, high_watermark_json,
                      updated_at, status
               FROM source_checkpoints WHERE source_id=?""",
            (source_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "source_id": row["source_id"],
            "connector_type": row["connector_type"],
            "cursor": json.loads(row["cursor_json"] or "{}"),
            "high_watermark": json.loads(row["high_watermark_json"] or "{}"),
            "updated_at": row["updated_at"],
            "status": row["status"],
        }

    def set_source_checkpoint(
        self,
        source_id: str,
        connector_type: str,
        cursor: dict[str, Any],
        high_watermark: dict[str, Any],
        updated_at: str,
        status: str = "healthy",
    ) -> None:
        self.conn.execute(
            """INSERT INTO source_checkpoints(
                source_id,connector_type,cursor_json,high_watermark_json,updated_at,status
            )
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET
                connector_type=excluded.connector_type,
                cursor_json=excluded.cursor_json,
                high_watermark_json=excluded.high_watermark_json,
                updated_at=excluded.updated_at,
                status=excluded.status""",
            (
                source_id,
                connector_type,
                json.dumps(cursor, default=str, sort_keys=True),
                json.dumps(high_watermark, default=str, sort_keys=True),
                updated_at,
                status,
            ),
        )
        self.conn.commit()

    def list_source_checkpoints(self) -> list[dict[str, Any]]:
        rows = self.rows(
            """SELECT source_id, connector_type, cursor_json, high_watermark_json,
                      updated_at, status
               FROM source_checkpoints ORDER BY source_id"""
        )
        return [
            {
                "source_id": row["source_id"],
                "connector_type": row["connector_type"],
                "cursor": json.loads(row["cursor_json"] or "{}"),
                "high_watermark": json.loads(row["high_watermark_json"] or "{}"),
                "updated_at": row["updated_at"],
                "status": row["status"],
            }
            for row in rows
        ]

    def replace_memory_records(self, source_id: str, records: list[dict[str, Any]]) -> int:
        """Rebuild one memory source's projection rows (Step 33.5).

        Transactional and wholesale: the table is a projection of the record
        files, so rebuilding it is always safe and always correct, and a
        record archived on disk disappears here without needing its own
        delete path. Returns the number of rows written.

        ``salience``/``uses``/``last_used_at`` are carried over from the
        existing row when one is present — they are earned by *use* (Step
        33.9) and are the one thing here that is not derivable from the file,
        so a re-sync must not silently reset them to zero.

        ``observations``/``last_seen``/``variants`` (Phase 1) are earned the
        same way, by a near-duplicate write reinforcing this record instead
        of creating its own file, and are carried over identically.
        ``canon_key`` is the opposite: a pure function of the record's own
        fields (see ``pheasant.memory.normalize``), so it is *not* carried
        over — ``records`` supplies a freshly computed one on every rebuild.

        ``tier``/``subsumed_by`` (Phase 3) are earned by a compaction pass
        choosing a medoid and demoting the rest — carried over exactly like
        ``salience``. A record a pass has never seen keeps the column
        defaults (``tier='hot'``, ``subsumed_by=NULL``), which is exactly
        the pre-Phase-3 state.
        """
        with self.conn:
            earned = {
                str(row["record_id"]): row
                for row in self.conn.execute(
                    "SELECT record_id, salience, uses, last_used_at, "
                    "observations, last_seen, variants, tier, subsumed_by "
                    "FROM memory_records WHERE source_id=?",
                    (source_id,),
                )
            }
            self.conn.execute("DELETE FROM memory_records WHERE source_id=?", (source_id,))
            for record in records:
                prior = earned.get(str(record.get("record_id")))
                self.conn.execute(
                    """INSERT INTO memory_records(
                        record_id,artifact_id,source_id,scope,subject,kind,asserted_at,
                        valid_from,valid_until,supersedes,tags,written_by,canon_key,
                        salience,uses,last_used_at,observations,last_seen,variants,
                        tier,subsumed_by,schema_version
                    )
                    VALUES(
                        :record_id,:artifact_id,:source_id,:scope,:subject,:kind,:asserted_at,
                        :valid_from,:valid_until,:supersedes,:tags,:written_by,:canon_key,
                        :salience,:uses,:last_used_at,:observations,:last_seen,:variants,
                        :tier,:subsumed_by,:schema_version
                    )""",
                    {
                        "subject": None,
                        "kind": "fact",
                        "valid_from": None,
                        "valid_until": None,
                        "supersedes": None,
                        "tags": None,
                        "written_by": None,
                        "canon_key": None,
                        "schema_version": 1,
                        **record,
                        "salience": float(prior["salience"]) if prior else 1.0,
                        "uses": int(prior["uses"]) if prior else 0,
                        "last_used_at": prior["last_used_at"] if prior else None,
                        "observations": int(prior["observations"]) if prior else 0,
                        "last_seen": prior["last_seen"] if prior else None,
                        "variants": prior["variants"] if prior else None,
                        "tier": str(prior["tier"]) if prior and prior["tier"] else "hot",
                        "subsumed_by": prior["subsumed_by"] if prior else None,
                    },
                )
        return len(records)

    def record_memory_use(self, record_ids: list[str], when: str) -> int:
        """Bump `uses`/`last_used_at` for records retrieval just returned.

        Best-effort and off the critical path (Step 33.9): a counter that fails
        to increment costs a little ranking signal, never a search. One
        statement for the whole batch, because this runs on the *read* path and
        a per-record round trip there would be a real cost for a soft benefit.
        """
        if not record_ids:
            return 0
        placeholders = ",".join("?" for _ in record_ids)
        try:
            with self.conn:
                cursor = self.conn.execute(
                    "UPDATE memory_records SET uses = uses + 1, last_used_at = ? "
                    f"WHERE record_id IN ({placeholders})",
                    (when, *record_ids),
                )
            return int(cursor.rowcount or 0)
        except Exception:  # pragma: no cover - never fail a query over a counter
            logger.debug("memory use counters not recorded", exc_info=True)
            return 0

    def find_canonical_record(self, canon_digest: str, *, now: str) -> str | None:
        """The record_id a write carrying this canonical key may fold into
        (Phase 1's write-path near-duplicate lookup), or None.

        **Only ever a record a default query could return**, and that is the
        whole point rather than a refinement. Reinforcement makes a write
        return `created=False` with `outcome="reinforced"` — the caller is
        told "we already hold this". If the row it folded into were one a
        plain `current_only=True` query filters out, that would be false:
        the assertion would be unreachable through every default read path
        while the writer believed it was recorded. Two ways a row gets into
        that state, both excluded here:

        The validity predicate is spelled exactly as
        `MemoryPolicy.sql_predicate` spells it —
        `COALESCE(valid_until,'')=''` rather than `valid_until IS NULL` —
        because an empty string is falsy to the Python half and is not NULL
        to SQL, the same corner that halves already documents. A fold that
        judged validity differently from the query that has to return the
        record would put the two out of step in exactly the way this method
        exists to prevent.

        * **Superseded or expired** (`valid_until` at or before `now`). A
          later record corrected this claim. Re-asserting the old claim is
          a *new* assertion about the present, not a restatement of history
          — it must create its own record so retrieval can see the conflict
          (and `supersedes` can resolve it), not vanish into the record it
          contradicts. `supersede_retention_days` (Phase 2) keeps such rows
          queryable for days, which is exactly how long this window would
          otherwise stay open.
        * **Demoted** (`tier='cold'`, from a Phase 3 compaction pass). Here
          the fold is still right, but the *target* is wrong: the cluster's
          canonical record is what a default query returns, so `subsumed_by`
          is followed to it and the observation credit lands there. The
          chain is walked (a canonical record can itself later be subsumed)
          with a hard step cap, so a cycle written by a future rule cannot
          hang a write.

        `ORDER BY record_id` makes ties deterministic — a canonical key can
        in principle match more than one live row only through a race
        between concurrent writers, and picking the lexicographically first
        id is arbitrary but always the *same* arbitrary choice, matching the
        tie-break `memory.salience.rank` already uses elsewhere.

        `COALESCE(NULLIF(tier,''),'hot')` rather than `COALESCE(tier,'hot')`
        — the empty-string-is-unset corner `MemoryPolicy.sql_predicate`
        already documents, kept identical here so the two agree on what
        "hot" means.
        """
        rows = self.rows(
            f"{_FOLD_COLUMNS} "
            "WHERE canon_key = ? AND (COALESCE(valid_until, '') = '' OR valid_until > ?) "
            "ORDER BY record_id LIMIT 1",
            (canon_digest, now),
        )
        return self._resolve_fold_target(rows, now) if rows else None

    #: Hard cap on `subsumed_by` hops. A cluster's canonical record can itself
    #: later be subsumed, so one hop is not always enough; bounded rather than
    #: "until it resolves" so a cycle written by a future rule cannot hang a
    #: write.
    _MAX_SUBSUMED_HOPS = 8

    def _resolve_fold_target(self, rows: list[Any], now: str) -> str | None:
        """The record a write may fold into, given a candidate row.

        Shared by :meth:`find_canonical_record` (matched on `canon_key`) and
        :meth:`foldable_record` (matched on `record_id`) so the two cannot
        drift on what "foldable" means. `rows` must already be filtered to
        current records; a hot row folds into itself, a demoted one into the
        canonical record `subsumed_by` names.
        """
        record_id = str(rows[0]["record_id"])
        if str(rows[0]["tier"]) == "hot":
            return record_id
        seen = {record_id}
        target = rows[0]["subsumed_by"]
        for _ in range(self._MAX_SUBSUMED_HOPS):
            if not target or str(target) in seen:
                return None
            seen.add(str(target))
            hop = self.rows(
                f"{_FOLD_COLUMNS} WHERE record_id = ? "
                "AND (COALESCE(valid_until, '') = '' OR valid_until > ?)",
                (str(target), now),
            )
            if not hop:
                return None
            if str(hop[0]["tier"]) == "hot":
                return str(hop[0]["record_id"])
            target = hop[0]["subsumed_by"]
        return None

    def foldable_record(self, record_id: str, *, now: str) -> str | None:
        """Where a write that byte-matches `record_id` on disk should fold.

        The exact-digest twin of :meth:`find_canonical_record`, and it exists
        because the two lookups answer the same question from different
        starting points: `MemoryStore.append` finds a byte-identical *file*
        by globbing its digest, which says nothing about whether that record
        is still current. Folding into a superseded one reports the write as
        `reinforced` while leaving the assertion unreachable by every default
        query — the same failure `find_canonical_record` documents, reached
        through the filesystem instead of through `canon_key`.

        Returns `record_id` itself when the projection has no row for it at
        all. That case is a cold or absent `/state`, not a judgement that the
        record is stale, and the pre-Phase-1 behavior (dedup on the file) is
        the right fallback: a missed *fold* costs a counter, where a missed
        *dedup* would write a duplicate file for a byte-identical assertion.
        """
        rows = self.rows(
            f"{_FOLD_COLUMNS} WHERE record_id = ?",
            (record_id,),
        )
        if not rows:
            return record_id
        valid_until = rows[0]["valid_until"]
        if valid_until and str(valid_until) <= now:
            return None
        return self._resolve_fold_target(rows, now)

    def reinforce_memory_record(
        self, record_id: str, submitted_text: str, when: str, *, max_variants: int = 8
    ) -> None:
        """Bump `observations`/`last_seen` for a record a new write folded
        into instead of creating its own file, and remember the submitted
        surface form as a `variant` if it is new.

        Best-effort and off the write's critical path, the same posture
        `record_memory_use` takes for `uses`: a counter that fails to update
        costs ranking signal, never the write the caller already has in
        hand. Read-then-write rather than a single UPDATE because merging a
        bounded, deduplicated JSON list needs the current value — an
        acceptable non-atomic window for a stats sidecar that only ever
        grows a *count*, not a decision anything else depends on mid-update.
        """
        try:
            with self.conn:
                row = self.conn.execute(
                    "SELECT variants FROM memory_records WHERE record_id = ?", (record_id,)
                ).fetchone()
                variants: list[str] = []
                if row is not None and row["variants"]:
                    try:
                        variants = json.loads(row["variants"])
                    except (TypeError, ValueError):
                        variants = []
                text = str(submitted_text or "").strip()
                if text and text not in variants and len(variants) < max_variants:
                    variants.append(text)
                self.conn.execute(
                    "UPDATE memory_records SET observations = observations + 1, "
                    "last_seen = ?, variants = ? WHERE record_id = ?",
                    (when, json.dumps(variants) if variants else None, record_id),
                )
        except Exception:  # pragma: no cover - never fail a write over a counter
            logger.debug("memory reinforcement not recorded", exc_info=True)

    def memory_salience_rows(self) -> list[dict[str, Any]]:
        """Everything the salience formula reads, for a pruning pass."""
        try:
            rows = self.rows(
                # `kind` is here so capacity pruning can tell a retrieval *rule*
                # from a recallable fact; see `memory.maintenance`. `last_seen`
                # and `observations` (Phase 1) are what let the formula
                # recognize a fact re-observed 10,000 times instead of
                # decaying it from its original `asserted_at` forever.
                # `subject` (Phase 5) is what lets a pruning pass group by
                # entity for `max_records_per_subject`.
                "SELECT record_id, scope, subject, kind, asserted_at, uses, last_used_at, "
                "salience, observations, last_seen "
                "FROM memory_records"
            )
        except Exception:  # pragma: no cover - state store older than 33.5
            return []
        return [dict(row) for row in rows]

    def memory_scope_tier_counts(self) -> list[dict[str, Any]]:
        """Live record counts grouped by (scope, tier) — Phase 5's
        `pheasant_memory_records{scope,tier}` gauge. `NULLIF` before
        `COALESCE`, not `COALESCE` alone: the same empty-string-vs-NULL
        corner `MemoryPolicy.sql_predicate` already documents — an empty
        string is falsy in Python's `tier or "hot"` but is not NULL to SQL,
        so a bare `COALESCE(tier, 'hot')` would undercount 'hot' whenever a
        row's `tier` was written as `''` rather than left NULL.
        """
        try:
            rows = self.rows(
                "SELECT scope, COALESCE(NULLIF(tier, ''), 'hot') AS tier, COUNT(*) AS n "
                "FROM memory_records GROUP BY scope, tier"
            )
        except Exception:  # pragma: no cover - state store older than Phase 2
            return []
        return [dict(row) for row in rows]

    def set_memory_salience(self, scores: dict[str, float]) -> None:
        """Persist computed salience so it is visible without recomputing."""
        if not scores:
            return
        with self.conn:
            self.conn.executemany(
                "UPDATE memory_records SET salience = ? WHERE record_id = ?",
                [(value, key) for key, value in sorted(scores.items())],
            )

    def memory_compaction_rows(self) -> list[dict[str, Any]]:
        """Everything a clustering pass needs beyond the record text itself
        (Phase 3) — record text lives only in the `.md` files, so the caller
        joins these by `record_id` against an already-parsed
        `MemoryStore.list_records()` (`memory.maintenance` parses the store
        once per pass, per Phase 0, and hands the same list to every stage).
        """
        try:
            rows = self.rows(
                "SELECT record_id, scope, subject, kind, written_by, tier, subsumed_by, "
                "observations "
                "FROM memory_records"
            )
        except Exception:  # pragma: no cover - state store older than Phase 3
            return []
        return [dict(row) for row in rows]

    def subsume_records(
        self,
        canonical_id: str,
        member_ids: list[str],
        *,
        absorbed_observations: int,
        now: str,
        rule_id: str,
        params_hash: str,
        op: str = "subsume",
    ) -> int:
        """Demote `member_ids` to `tier='cold'` pointing at `canonical_id`,
        credit the canonical record with the cluster's absorbed
        `observations`, and append one ledger row per member (Phase 3).

        `op` defaults to `"subsume"` (deterministic medoid promotion,
        Phase 3) but the mechanics are identical for `"synthesize"`
        (Phase 4's LLM-merged canonical record) — a member is redundant but
        still true either way, so the same tier/subsumed_by/ledger write
        applies; only what produced the canonical record differs, and that
        is exactly what `op` + `rule_id` distinguish.

        Idempotent: the ledger row id is a deterministic hash of
        `(op, member_id, canonical_id, params_hash)`, so a second pass over
        an unchanged cluster with unchanged parameters writes the same ids —
        `INSERT OR IGNORE` makes them no-ops — while the tier/subsumed_by
        UPDATEs are themselves idempotent (setting the same value twice).
        Returns the number of members actually demoted this call.
        """
        ids = [str(i) for i in member_ids if i and i != canonical_id]
        if not ids:
            return 0
        with self.conn:
            placeholders = ",".join("?" for _ in ids)
            cursor = self.conn.execute(
                f"UPDATE memory_records SET tier='cold', subsumed_by=? "
                f"WHERE record_id IN ({placeholders})",
                (canonical_id, *ids),
            )
            demoted = int(cursor.rowcount or 0)
            if absorbed_observations:
                self.conn.execute(
                    "UPDATE memory_records SET observations = observations + ? WHERE record_id = ?",
                    (absorbed_observations, canonical_id),
                )
            for member_id in ids:
                row_id = hashlib.blake2b(
                    f"{op}|{member_id}|{canonical_id}|{params_hash}".encode(),
                    digest_size=16,
                ).hexdigest()
                self.conn.execute(
                    # `ON CONFLICT (id) DO NOTHING`, not `INSERT OR IGNORE`
                    # — the latter is SQLite-only syntax with no Postgres
                    # equivalent `dialect.translate()` handles (a hard
                    # `SyntaxError`, caught by running this against a real
                    # Postgres server, CLAUDE.md rule 10). `ON CONFLICT` is
                    # supported identically by both, the same portable
                    # idiom every other upsert in this file already uses.
                    "INSERT INTO memory_compactions"
                    "(id, op, member_id, canonical_id, rule_id, params_hash, at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (id) DO NOTHING",
                    (row_id, op, member_id, canonical_id, rule_id, params_hash, now),
                )
        return demoted

    # -- memory candidates (formation) -------------------------------------

    def upsert_memory_candidate(self, candidate: dict[str, Any]) -> bool:
        """Record a proposal, or refresh the counters of one already open.

        Returns True when the row is (still) ``pending`` afterwards --- that
        is, when this proposal is live and awaiting a decision.

        The ``WHERE`` on the conflict branch is the load-bearing part: it only
        ever updates a row that is still pending, so **a rejected candidate is
        never re-proposed** and an admitted one is never reopened. Without it a
        rule would re-suggest on every beat the very thing a person just said
        no to, which is the fastest way to make a review queue worthless. Same
        shape `index_tasks` uses to keep a dead task dead.
        """

        rows = self.execute_returning(
            "INSERT INTO memory_candidates("
            "id, rule_id, params_hash, scope, subject, kind, text, written_by, "
            "evidence_json, observations, sessions, first_seen, last_seen, status"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "observations=excluded.observations, sessions=excluded.sessions, "
            "evidence_json=excluded.evidence_json, last_seen=excluded.last_seen "
            "WHERE memory_candidates.status='pending' "
            "RETURNING id",
            (
                str(candidate["id"]),
                str(candidate["rule_id"]),
                str(candidate["params_hash"]),
                str(candidate["scope"]),
                candidate.get("subject"),
                str(candidate.get("kind") or "fact"),
                str(candidate["text"]),
                candidate.get("written_by"),
                candidate.get("evidence_json"),
                int(candidate.get("observations") or 1),
                int(candidate.get("sessions") or 1),
                str(candidate["first_seen"]),
                str(candidate["last_seen"]),
                "pending",
            ),
        )
        return bool(rows)

    def list_memory_candidates(
        self,
        *,
        status: str | None = "pending",
        rule_id: str | None = None,
        principal: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Open proposals, most recently reinforced first.

        ``principal`` narrows to what one caller may see: a candidate carrying
        a writer is that principal's business alone, because the record it
        would become is scoped to them. One with no writer is region-wide and
        is visible to everybody --- the same rule `normalize_acl` applies to
        the records themselves.
        """

        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if rule_id:
            clauses.append("rule_id = ?")
            params.append(rule_id)
        if principal is not None:
            clauses.append("(written_by IS NULL OR written_by = ?)")
            params.append(principal)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        rows = self.rows(
            "SELECT id, rule_id, params_hash, scope, subject, kind, text, written_by, "
            "evidence_json, observations, sessions, first_seen, last_seen, status, "
            "admitted_by, record_id, decided_at "
            f"FROM memory_candidates{where} ORDER BY last_seen DESC, id LIMIT ?",
            tuple(params),
        )
        return [dict(row) for row in rows]

    def get_memory_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        rows = self.rows(
            "SELECT id, rule_id, params_hash, scope, subject, kind, text, written_by, "
            "evidence_json, observations, sessions, first_seen, last_seen, status, "
            "admitted_by, record_id, decided_at "
            "FROM memory_candidates WHERE id = ?",
            (candidate_id,),
        )
        return dict(rows[0]) if rows else None

    def decide_memory_candidate(
        self,
        candidate_id: str,
        *,
        status: str,
        admitted_by: str,
        record_id: str | None = None,
        when: str,
    ) -> bool:
        """Mark a candidate admitted or rejected. Returns False if already decided.

        Guarded on ``status='pending'`` so two reviewers racing on one
        candidate cannot both admit it --- the second gets False and can say
        so, instead of writing a second identical record.
        """

        rows = self.execute_returning(
            "UPDATE memory_candidates SET status=?, admitted_by=?, record_id=?, "
            "decided_at=? WHERE id=? AND status='pending' RETURNING id",
            (str(status), str(admitted_by), record_id, str(when), str(candidate_id)),
        )
        return bool(rows)

    def expire_memory_candidates(self, *, older_than: str) -> int:
        """Retire proposals nobody acted on. Rejections are never touched.

        A pending candidate that has gone stale is noise in a review queue; a
        rejection is a decision, and re-proposing what someone already declined
        is the thing the upsert guard exists to prevent.
        """

        rows = self.execute_returning(
            "UPDATE memory_candidates SET status='expired', decided_at=? "
            "WHERE status='pending' AND last_seen < ? RETURNING id",
            (str(older_than), str(older_than)),
        )
        return len(rows)

    def memory_candidate_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.rows(
            "SELECT status, COUNT(*) AS c FROM memory_candidates GROUP BY status", ()
        ):
            counts[str(row["status"])] = int(row["c"])
        return counts

    def interaction_events_by_id(self, event_ids: list[str]) -> list[dict[str, Any]]:
        """The ledger rows a proposal was derived from, oldest first.

        What turns a candidate from an assertion with a count attached into
        something a reviewer can check: the questions that produced it, what
        came back, and the spans that carried them.

        Rows may be absent --- the hot window is retention-bounded, so
        evidence can age out from under a proposal that is still pending. The
        caller gets what survives rather than an error, because a partial
        trail is more useful than none.
        """

        if not event_ids:
            return []
        placeholders = ",".join("?" for _ in event_ids)
        rows = self.rows(
            "SELECT id, trace_id, span_id, parent_span_id, modality, operation, "
            "principal, session_id, started_at, duration_ms, status, query_text, "
            "answer_text, criteria_json, result_ids_json, result_paths_json, "
            "result_count, top_score "
            f"FROM interaction_events WHERE id IN ({placeholders}) "
            "ORDER BY started_at, id",
            tuple(event_ids),
        )
        return [dict(row) for row in rows]

    def memory_compaction_ledger(
        self,
        *,
        canonical_id: str | None = None,
        member_id: str | None = None,
        params_hash: str | None = None,
    ) -> list[dict[str, Any]]:
        """Ledger rows, optionally filtered by canonical id, member id or
        params_hash — the audit trail answering "why is this record cold"
        (Phase 3). `params_hash` (Phase 4) is what a synthesis pass checks
        *before* calling a model: any row already means this exact cluster
        was already resolved under this exact model + member set, so the
        pass costs zero calls on a repeat run over unchanged content."""
        clauses: list[str] = []
        params: list[str] = []
        if canonical_id is not None:
            clauses.append("canonical_id = ?")
            params.append(canonical_id)
        if member_id is not None:
            clauses.append("member_id = ?")
            params.append(member_id)
        if params_hash is not None:
            clauses.append("params_hash = ?")
            params.append(params_hash)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            rows = self.rows(
                "SELECT id, op, member_id, canonical_id, rule_id, params_hash, at "
                f"FROM memory_compactions{where} ORDER BY at, id",
                tuple(params),
            )
        except Exception:  # pragma: no cover - state store older than Phase 3
            return []
        return [dict(row) for row in rows]

    def delete_source_artifacts(self, source_id: str) -> int:
        rows = self.rows("SELECT COUNT(*) AS c FROM artifacts WHERE source_id=?", (source_id,))
        count = int(rows[0]["c"]) if rows else 0
        with self.conn:
            self.conn.execute("DELETE FROM chunks_fts WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM symbols WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM artifact_terms WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM artifacts WHERE source_id=?", (source_id,))
        # Deliberately NOT clearing memory_records here. This runs on every
        # *full* sync (engine.py's rebuild path, which consolidation takes after
        # each archive), and `replace_memory_records` rebuilds the rows moments
        # later anyway — so dropping them buys nothing and costs the one thing
        # in that table that is earned rather than derived: `uses`, `salience`
        # and `last_used_at`. Wiping those on every consolidation pass would
        # quietly reset a memory's track record. Genuine removal goes through
        # `delete_source`, which does clear them.
        return count

    def delete_artifacts(self, artifact_ids: list[str]) -> int:
        """Remove specific artifacts (and their chunks/symbols/terms) by id.

        Phase 0: the targeted counterpart to `delete_source_artifacts`. That
        method's whole-source `DELETE ... WHERE source_id=?` is cheap because
        every table involved is indexed on `source_id`; this one is a
        per-artifact `WHERE id/artifact_id IN (...)` instead, which is the
        exact shape CLAUDE.md warns against for `chunks_fts` — its
        `artifact_id` column is UNINDEXED, so this degrades to a table scan
        per call. It exists anyway because a handful of archived memory
        records does not justify re-syncing (and re-parsing) an entire
        source; callers own picking a size where a full sync is cheaper
        instead (see `memory.maintenance.MEMORY_TARGETED_ARCHIVE_MAX`).

        Like `delete_source_artifacts`, `memory_records` rows are left alone
        — the caller (memory maintenance) rebuilds that table itself from the
        record files, which is what keeps `uses`/`salience`/`last_used_at`
        earned rather than reset.
        """
        ids = [str(i) for i in artifact_ids if i]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        params = tuple(ids)
        with self.conn:
            self.conn.execute(
                f"DELETE FROM chunks_fts WHERE artifact_id IN ({placeholders})", params
            )
            self.conn.execute(f"DELETE FROM chunks WHERE artifact_id IN ({placeholders})", params)
            self.conn.execute(f"DELETE FROM symbols WHERE artifact_id IN ({placeholders})", params)
            self.conn.execute(
                f"DELETE FROM artifact_terms WHERE artifact_id IN ({placeholders})", params
            )
            cursor = self.conn.execute(
                f"DELETE FROM artifacts WHERE id IN ({placeholders})", params
            )
            return int(cursor.rowcount or 0)

    def delete_source(self, source_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM chunks_fts WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM symbols WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM artifact_terms WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM memory_records WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM artifacts WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM source_checkpoints WHERE source_id=?", (source_id,))
            self.conn.execute("DELETE FROM sources WHERE id=?", (source_id,))

    def append_sync_event(
        self,
        event_id: str,
        source_id: str | None,
        event_type: str,
        status: str,
        started_at: str | None,
        finished_at: str | None,
        details: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO sync_events(
                id,source_id,event_type,status,started_at,finished_at,details_json,error_json
            )
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                event_id,
                source_id,
                event_type,
                status,
                started_at,
                finished_at,
                json.dumps(details or {}, default=str, sort_keys=True),
                json.dumps(error, default=str, sort_keys=True) if error else None,
            ),
        )
        self.conn.commit()

    def list_sync_events(
        self,
        source_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Most-recent-first sync events (rowid order is insertion order)."""
        where = ""
        params: list[Any] = []
        if source_id:
            where = "WHERE source_id=?"
            params.append(source_id)
        params.extend([limit, offset])
        rows = self.rows(
            f"""SELECT * FROM sync_events
                {where}
                ORDER BY rowid DESC
                LIMIT ? OFFSET ?""",
            tuple(params),
        )
        events = []
        for row in rows:
            event = dict(row)
            event["details"] = json.loads(event.pop("details_json") or "{}")
            error_json = event.pop("error_json")
            event["error"] = json.loads(error_json) if error_json else None
            events.append(event)
        return events

    def append_source_audit_event(
        self,
        event_id: str,
        source_id: str | None,
        action: str,
        actor: str | None,
        transport: str | None,
        client_id: str | None,
        created_at: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO source_audit_events(
                id,source_id,action,actor,transport,client_id,created_at,details_json
            )
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                event_id,
                source_id,
                action,
                actor,
                transport,
                client_id,
                created_at,
                json.dumps(details or {}, default=str, sort_keys=True),
            ),
        )
        self.conn.commit()

    def list_source_audit_events(
        self,
        source_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if source_id:
            where = "WHERE source_id=?"
            params.append(source_id)
        params.extend([limit, offset])
        rows = self.rows(
            f"""SELECT * FROM source_audit_events
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?""",
            tuple(params),
        )
        events = []
        for row in rows:
            event = dict(row)
            event["details"] = json.loads(event.pop("details_json") or "{}")
            events.append(event)
        return events

    def get_manifest(self, source_name: str) -> dict[str, Any] | None:
        rows = self.rows(
            "SELECT payload_json FROM manifests WHERE source_name=?",
            (source_name,),
        )
        if not rows:
            return None
        return json.loads(rows[0]["payload_json"])

    def set_manifest(self, source_name: str, payload: dict[str, Any], updated_at: str) -> None:
        self.conn.execute(
            """INSERT INTO manifests(source_name,payload_json,updated_at)
            VALUES(?,?,?)
            ON CONFLICT(source_name) DO UPDATE SET
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at""",
            (source_name, json.dumps(payload, sort_keys=True, default=str), updated_at),
        )
        self.conn.commit()

    def manifest_exists(self, source_name: str) -> bool:
        rows = self.rows(
            "SELECT 1 FROM manifests WHERE source_name=? LIMIT 1",
            (source_name,),
        )
        return bool(rows)

    def delete_manifest(self, source_name: str) -> None:
        self.conn.execute("DELETE FROM manifests WHERE source_name=?", (source_name,))
        self.conn.commit()

    def artifact_state(self, source_id: str, artifact_id: str) -> dict[str, Any] | None:
        rows = self.rows(
            """SELECT artifacts.id, artifacts.sha256, artifacts.status,
                      COUNT(chunks.id) AS chunk_count
               FROM artifacts
               LEFT JOIN chunks ON chunks.artifact_id=artifacts.id
               WHERE artifacts.source_id=? AND artifacts.id=?
               GROUP BY artifacts.id""",
            (source_id, artifact_id),
        )
        return dict(rows[0]) if rows else None

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        """Run raw SQL. The escape hatch 57 call sites across the codebase use.

        Routed through the backend so the statement is translated for the
        active dialect (``?`` → ``%s``, ``GROUP_CONCAT`` → ``string_agg``).
        On SQLite the translation is a no-op and this executes byte-identical
        SQL over the same connection it always did.
        """

        return self.backend.rows(sql, params)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Run a raw write and commit it (Phase 35.5).

        The write counterpart to :meth:`rows`, and it commits because it is
        used by the durable index queue, whose whole point is that a claim
        or an ack is visible to another process the moment it returns. A
        queue operation batched into someone else's open transaction would
        be a queue that only works inside one process.
        """

        self.backend.execute(sql, params)
        self.backend.commit()

    def execute_returning(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        """Run a write that returns rows (``UPDATE ... RETURNING``) and commit.

        Not a convenience over :meth:`rows`: routing a write through the read
        path leaves the implicit transaction **open**, so on SQLite the WAL
        write lock is held until the connection happens to commit something
        else. Every other thread then blocks for the full ``busy_timeout``,
        and — worse for a queue — the claim is not visible to another process
        at all, which is the one property the durable queue exists to have.
        """

        # `statement`, not `rows`: on a pooled backend a read may hand the
        # connection back, and the commit below would then land on a
        # different connection — silently discarding the UPDATE. `statement`
        # returns `(rows, rowcount)` (Phase 5 added the rowcount half for
        # `subsume_records`/`delete_artifacts`); this caller only ever wants
        # the `RETURNING` rows.
        result, _rowcount = self.backend.statement(sql, params)
        self.backend.commit()
        return result

    def close(self) -> None:
        self.backend.close()
