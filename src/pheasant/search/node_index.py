"""A searchable index of graph nodes, in SQLite.

Graph-mode search used to score every node in memory. That is O(nodes) per
query and it does not care how selective the query is: on a 500k-node graph a
single search cost 2.5s idle and over 10s while indexing ran, and the agent
loop issues several per question. Chunk text has never had this problem —
it lives in an FTS5 index — so graph nodes get the same treatment.

The index is a **cache derived from the graph**, never a second source of
truth. It can be deleted at any time and rebuilt from the in-memory graph, and
graph search falls back to the in-memory scan whenever it is empty or
unavailable. That keeps the graph the only thing that has to be correct.

One semantic change, deliberate: FTS matches tokens and prefixes where the old
scan matched arbitrary substrings, so a query for ``exec`` finds
``executor`` (token prefix) but no longer finds ``myexecutor`` (infix). This
is the same matching the text search over chunks has always used, and the
candidates it returns are still ranked by the original scorer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS graph_nodes_fts USING fts5(
  node_id UNINDEXED,
  source_id UNINDEXED,
  body,
  tokenize='unicode61'
);
"""

#: Postgres has no FTS5. Same table name and columns so every write below is
#: unchanged, plus a generated search vector and its GIN index. ``simple``
#: (not ``english``) matches FTS5's unicode61 tokenizer: no stemming, no
#: stopword removal — stemming here would silently change which graph nodes a
#: query reaches.
POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_nodes_fts (
  node_id TEXT PRIMARY KEY,
  source_id TEXT,
  body TEXT,
  search_vector tsvector GENERATED ALWAYS AS (
    to_tsvector('simple', coalesce(body, ''))
  ) STORED
);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_fts_vector
  ON graph_nodes_fts USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_fts_source ON graph_nodes_fts(source_id);
"""

#: FTS5 treats these as syntax; a query built from user text must not.
_UNSAFE = re.compile(r"[^\w\s]", re.UNICODE)


class NodeIndex:
    """Full-text index over graph node text, backed by the state database."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self._ready = False
        dialect = getattr(state, "dialect", None)
        self._postgres = bool(dialect is not None and dialect.is_postgres)
        # Sticky "there is something in here". A query only needs to know
        # empty-vs-not, and COUNT(*) over an FTS5 table is a full scan — on a
        # 577k-row index that was costing more than the search it guarded.
        # Only ever flips false→true, and writes set it directly.
        self._populated = False

    def ensure(self) -> bool:
        """Create the table if needed. False when FTS5 is unavailable."""

        if self._ready:
            return True
        try:
            if self._postgres:
                # Most processes only need to discover the cache another
                # process created. The re-check under the schema lock closes
                # the first-start race without replaying catalog DDL from
                # every API/indexer/sync child.
                if "node_id" not in self.state.backend.table_columns("graph_nodes_fts"):
                    with self.state.backend.schema_lock():
                        if "node_id" not in self.state.backend.table_columns("graph_nodes_fts"):
                            self.state.conn.executescript(POSTGRES_SCHEMA)
            else:
                self.state.conn.executescript(SCHEMA)
            self.state.conn.commit()
            self._ready = True
        except Exception as exc:  # pragma: no cover - SQLite without FTS5
            logger.warning("graph node index unavailable (%s); using the in-memory scan", exc)
            self._ready = False
        return self._ready

    def count(self) -> int:
        """Exact row count. Callers on the query path want :meth:`populated`."""

        if not self.ensure():
            return 0
        rows = self.state.rows("SELECT COUNT(*) AS n FROM graph_nodes_fts")
        total = int(rows[0]["n"]) if rows else 0
        self._populated = total > 0
        return total

    def populated(self) -> bool:
        """Is there anything to search? One row, not a count."""

        if self._populated:
            return True
        if not self.ensure():
            return False
        probe = "node_id" if self._postgres else "rowid"
        self._populated = bool(self.state.rows(f"SELECT {probe} FROM graph_nodes_fts LIMIT 1"))
        return self._populated

    def replace_all(self, nodes: Iterable[tuple[str, str, str]]) -> int:
        """Rebuild from scratch. ``nodes`` yields (node_id, source_id, body)."""

        if not self.ensure():
            return 0
        if self._postgres:
            return self._replace_all_postgres(nodes)
        written = 0
        with self.state.conn:
            self.state.conn.execute("DELETE FROM graph_nodes_fts")
            for batch in _batched(nodes, 2000):
                self.state.conn.executemany(
                    "INSERT INTO graph_nodes_fts (node_id, source_id, body) VALUES (?,?,?)",
                    batch,
                )
                written += len(batch)
        self._populated = written > 0
        return written

    def _replace_all_postgres(self, nodes: Iterable[tuple[str, str, str]]) -> int:
        """Reconcile through a temporary stage instead of delete/reinsert.

        The old wholesale DELETE made every live graph-index row dead on each
        rebuild. On the stress database that left 61k dead rows beside 60k
        live ones and made this derived cache larger than the chunk index.
        Staging deletes only vanished nodes and updates only changed text.
        """

        written = 0
        with self.state.conn:
            self.state.conn.execute(
                "CREATE TEMP TABLE pheasant_graph_nodes_stage ("
                "node_id TEXT PRIMARY KEY, source_id TEXT, body TEXT) ON COMMIT DROP"
            )
            for batch in _batched(nodes, 2000):
                self.state.conn.executemany(
                    "INSERT INTO pheasant_graph_nodes_stage(node_id, source_id, body) "
                    "VALUES(?,?,?)",
                    batch,
                )
                written += len(batch)
            self.state.conn.execute(
                "DELETE FROM graph_nodes_fts target WHERE NOT EXISTS ("
                "SELECT 1 FROM pheasant_graph_nodes_stage stage "
                "WHERE stage.node_id=target.node_id)"
            )
            self.state.conn.execute(
                "INSERT INTO graph_nodes_fts(node_id, source_id, body) "
                "SELECT node_id, source_id, body FROM pheasant_graph_nodes_stage "
                "ON CONFLICT(node_id) DO UPDATE SET "
                "source_id=excluded.source_id, body=excluded.body "
                "WHERE graph_nodes_fts.source_id IS DISTINCT FROM excluded.source_id "
                "OR graph_nodes_fts.body IS DISTINCT FROM excluded.body"
            )
        self._populated = written > 0
        return written

    def ids(self, source_id: str | None = None) -> set[str]:
        """The node ids the index holds, for one source or all of them."""

        if not self.ensure():
            return set()
        if source_id is None:
            rows = self.state.rows("SELECT node_id FROM graph_nodes_fts")
        else:
            rows = self.state.rows(
                "SELECT node_id FROM graph_nodes_fts WHERE source_id=?", (source_id,)
            )
        return {str(row["node_id"]) for row in rows}

    def has_saved_graph(self, kb_id: str) -> bool:
        """Whether the row backend holds any saved node for this knowledge base."""

        return bool(self.state.rows("SELECT 1 FROM graph_nodes WHERE kb_id=? LIMIT 1", (kb_id,)))

    def persisted_ids(self, kb_id: str, source_id: str) -> set[str]:
        """The node ids the saved graph holds for one source (``graph_nodes``).

        Empty on the whole-file graph backend, which keeps no such rows.
        """

        rows = self.state.rows(
            "SELECT node_id FROM graph_nodes WHERE kb_id=? AND source_id=?", (kb_id, source_id)
        )
        return {str(row["node_id"]) for row in rows}

    def apply(self, upserts: Iterable[tuple[str, str, str]], removals: Iterable[str]) -> int:
        """Incremental update: delete then re-insert the touched rows."""

        if not self.ensure():
            return 0
        upserts = list(upserts)
        if self._postgres:
            return self._apply_postgres(upserts, removals)
        touched = [node_id for node_id, _source, _body in upserts]
        touched.extend(removals)
        if not touched:
            return 0
        with self.state.conn:
            for batch in _batched(iter(touched), 500):
                placeholders = ",".join("?" for _ in batch)
                self.state.conn.execute(
                    f"DELETE FROM graph_nodes_fts WHERE node_id IN ({placeholders})",
                    tuple(batch),
                )
            for batch in _batched(iter(upserts), 2000):
                self.state.conn.executemany(
                    "INSERT INTO graph_nodes_fts (node_id, source_id, body) VALUES (?,?,?)",
                    batch,
                )
        if upserts:
            self._populated = True
        return len(upserts)

    def _apply_postgres(
        self,
        upserts: list[tuple[str, str, str]],
        removals: Iterable[str],
    ) -> int:
        """UPSERT changed nodes without a duplicate-key race or delete churn."""

        upsert_ids = {node_id for node_id, _source, _body in upserts}
        removed = [node_id for node_id in removals if node_id not in upsert_ids]
        if not upserts and not removed:
            return 0
        with self.state.conn:
            for batch in _batched(iter(removed), 500):
                placeholders = ",".join("?" for _ in batch)
                self.state.conn.execute(
                    f"DELETE FROM graph_nodes_fts WHERE node_id IN ({placeholders})",
                    tuple(batch),
                )
            for batch in _batched(iter(upserts), 2000):
                self.state.conn.executemany(
                    "INSERT INTO graph_nodes_fts(node_id, source_id, body) VALUES(?,?,?) "
                    "ON CONFLICT(node_id) DO UPDATE SET "
                    "source_id=excluded.source_id, body=excluded.body "
                    "WHERE graph_nodes_fts.source_id IS DISTINCT FROM excluded.source_id "
                    "OR graph_nodes_fts.body IS DISTINCT FROM excluded.body",
                    batch,
                )
        if upserts:
            self._populated = True
        return len(upserts)

    def candidate_query(
        self,
        tokens: list[str],
        source_name: str | None = None,
        limit: int = 2000,
    ) -> tuple[str, list[Any]] | None:
        """The candidate ``SELECT``, unexecuted. ``None`` = cannot answer.

        Split out from :meth:`candidates` so a caller holding the graph rows
        in the *same database* can run it as a subquery instead of shipping
        thousands of ids up to Python and straight back down as bind
        parameters — see
        :meth:`~pheasant.persistence.graph_rows.GraphRowStore.nodes_matching`.
        One definition of what a candidate is, two ways to spend it.
        """

        if not self.ensure():
            return None
        match = _match_expression(tokens)
        if not match:
            return None
        if not self.populated():
            return None
        if self._postgres:
            # tsquery, not MATCH. Prefix search is `word:*`, and the terms are
            # OR'd exactly as the FTS5 expression does — same recall, different
            # spelling. Building this from the already-sanitized tokens rather
            # than translating the FTS5 string keeps one source of truth for
            # what a term is.
            sql = (
                "SELECT node_id FROM graph_nodes_fts, to_tsquery('simple', ?) q "
                "WHERE search_vector @@ q"
            )
            params: list[Any] = [_tsquery_expression(tokens)]
        else:
            sql = "SELECT node_id FROM graph_nodes_fts WHERE graph_nodes_fts MATCH ?"
            params = [match]
        if source_name:
            sql += " AND source_id = ?"
            params.append(source_name)
        sql += " LIMIT ?"
        params.append(int(limit))
        return sql, params

    def still_populated(self) -> bool:
        """Re-probe rather than trust the sticky flag.

        A query that found nothing is ambiguous: either nothing matches, or
        the index was emptied behind our back (a wipe, a fresh state dir).
        ``_populated`` only ever flips false→true, so it has to be cleared
        before the probe can mean anything. Both readers of an empty result
        ask this, so the reset and the probe stay one step.
        """

        self._populated = False
        return self.populated()

    def candidates(
        self,
        tokens: list[str],
        source_name: str | None = None,
        limit: int = 2000,
    ) -> list[str] | None:
        """Node ids whose text matches any token. None = index cannot answer.

        ``None`` (rather than an empty list) when the index is unavailable or
        empty, so the caller can fall back to scanning instead of concluding
        that nothing matched.
        """

        built = self.candidate_query(tokens, source_name, limit)
        if built is None:
            return None
        sql, params = built
        try:
            found = [str(row["node_id"]) for row in self.state.rows(sql, tuple(params))]
        except Exception as exc:  # a malformed MATCH must not fail the search
            logger.debug("node index query failed (%s); falling back to scan", exc)
            return None
        if found:
            return found
        return found if self.still_populated() else None


def _match_expression(tokens: list[str]) -> str:
    """An OR of prefix terms, with FTS5 syntax characters stripped."""

    terms = []
    for token in tokens:
        cleaned = _UNSAFE.sub(" ", token).strip()
        for word in cleaned.split():
            if len(word) > 1:
                terms.append(f'"{word}"*')
    return " OR ".join(dict.fromkeys(terms))


def _batched(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _tsquery_expression(tokens: list[str]) -> str:
    """The Postgres twin of :func:`_match_expression`.

    Same tokens, same OR semantics, same prefix matching — spelled ``word:*``
    and joined with ``|`` instead of FTS5's ``"word"* OR``. Built from the
    tokens rather than by rewriting the FTS5 string so there is one definition
    of what counts as a term.
    """

    terms = []
    for token in tokens:
        cleaned = _UNSAFE.sub(" ", token).strip()
        for word in cleaned.split():
            if len(word) > 1:
                terms.append(f"{word}:*")
    return " | ".join(dict.fromkeys(terms))


def reconcile(
    index: NodeIndex,
    source_id: str,
    *,
    kb_id: str,
    graph: Callable[[], Any] | None,
    saved_rows: bool,
    missing: bool = False,
) -> tuple[int, int]:
    """Make the index agree with the graph for one source; ``(removed, added)``.

    The index is written from the graph's pending delta, which is claimed on
    flush and held in memory until then -- while the graph's rows are saved on
    their own schedule, mid-sync included. A process killed between the two
    left rows for nodes the saved graph no longer had (46 on the lab, after
    four OOM kills), and lost writes left graph nodes the index never got
    (4,573, an entire uploads source among them, with no crash anyone saw).

    **The truth has to be one the caller actually holds.** The first version
    judged rows against the in-memory working graph -- which a sync with
    nothing to change never loads, so to it every node looked absent, and it
    deleted every row of every source it touched. On the row backend
    (``saved_rows``) the saved ``graph_nodes`` are the truth instead: stale and
    missing are both a comparison of two id sets, and the working graph is
    loaded (``graph()``) only to build rows that need adding. A knowledge base
    with no saved rows at all is never read as "has no nodes". On the file
    backend, which saves no rows, ``graph`` is ``None`` unless the working
    graph is loaded, and then nothing is done; ``missing`` walks the whole of
    it, after a run is known to have died.

    Converges whatever is still pending: a stale row a later flush would have
    removed is removed now, and a missing row a later flush rewrites is
    written twice.
    """

    try:
        held = index.ids(source_id)
        added: list[tuple[str, str, str]] = []
        if saved_rows:
            if not index.has_saved_graph(kb_id):
                return 0, 0
            persisted = index.persisted_ids(kb_id, source_id)
            stale = sorted(held - persisted)
            absent = sorted(persisted - held)
            if absent and graph is not None:
                added = graph().index_rows_for(absent)
        else:
            if graph is None:
                return 0, 0
            working = graph()
            stale = [node_id for node_id in held if not working.has_node(node_id)]
            if missing:
                added = [
                    row
                    for row in working.index_rows()
                    if row[1] == source_id and row[0] not in held
                ]
        if stale or added:
            index.apply(added, stale)
            logger.warning(
                "Node index repaired for %s: %d stale row(s) removed, %d missing added",
                source_id,
                len(stale),
                len(added),
            )
        return len(stale), len(added)
    except Exception:  # noqa: BLE001 - cache maintenance is never a failed sync
        logger.warning("Could not reconcile the node index for %s", source_id, exc_info=True)
        return 0, 0
