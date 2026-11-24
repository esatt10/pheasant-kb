"""Read the tables a search reads into the database's cache, once, at wake.

A region that scales to zero wakes with a cold database. Postgres's own
``autoprewarm`` worker reloads what was in ``shared_buffers`` when it last
dumped -- 128 MB by default -- and every other page a search touches is a read
from the volume the first time any query touches it. On the Azure region that
volume is NFS-backed Azure Files, so the graph arm's candidate fetch (up to
2,000 node rows, each on its own page) took 3-7 s for every *new* query and
~100 ms once its pages were cached, and the first search after a wake took
11.9 s. The vector arm was never the cost: its matrix is resident before the
api accepts a request (`search/vector_resident.py`).

So a serving process reads the search path's relations -- every page, in
order, which a network volume serves at its sequential rate rather than per
page -- before people do (`search/keep_warm.py` calls this at boot, off the
request path). ``pg_prewarm`` does the reading: into ``shared_buffers`` when
the relations fit in half of it, otherwise into the operating system's cache
(``read`` mode), which holds them for as long as the container has memory.

Best-effort and bounded: without the extension (a managed server that does
not allow it) nothing happens and nothing fails; relations are read in the
order a search needs them and stop at :data:`MAX_BYTES`; a relation that does
not exist yet is skipped. Each statement carries :data:`MARKER`, so
``services/region_status.py`` reports the database as warming while one runs,
on any replica.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

#: In the order a search reads them: the lexical arm's index and chunk rows,
#: the graph arm's node index and nodes, then the edges walks and expansion
#: read. Each comes with its indexes.
SEARCH_RELATIONS: tuple[str, ...] = (
    "chunks_fts",
    "chunks",
    "graph_nodes_fts",
    "graph_nodes",
    "artifacts",
    "graph_edges",
)

#: Read no more than this. The search path of a 20k-chunk region is ~500 MB;
#: past this the cache would evict what it read first.
MAX_BYTES = 1536 * 1024 * 1024

#: In every prewarm statement, so a status check can see one running.
MARKER = "pheasant-prewarm"


def warm_search_relations(
    state: Any,
    *,
    relations: tuple[str, ...] = SEARCH_RELATIONS,
    max_bytes: int = MAX_BYTES,
) -> dict[str, Any]:
    """Read ``relations`` and their indexes into cache. Never raises.

    Returns what it did: ``{"available": False, "reason": ...}`` when it could
    not, else the mode, how many relations, bytes and blocks were read, what
    was left out over ``max_bytes``, and how long it took.
    """

    began = time.monotonic()
    try:
        if not _extension(state):
            return {"available": False, "reason": "pg_prewarm is not installed on this server"}
        targets = _targets(state, relations)
        shared = int(
            state.rows("SELECT pg_size_bytes(current_setting('shared_buffers')) AS b")[0]["b"]
        )
    except Exception as exc:  # noqa: BLE001 - a warm-up must never fail a boot
        logger.debug("prewarm: could not plan", exc_info=True)
        return {"available": False, "reason": str(exc)}
    chosen: list[tuple[str, int]] = []
    skipped: list[str] = []
    total = 0
    for name, size in targets:
        if size <= 0:
            continue  # empty: nothing to read
        if total + size > max_bytes:
            skipped.append(name)
            continue
        chosen.append((name, size))
        total += size
    mode = "buffer" if total <= shared // 2 else "read"
    blocks = 0
    failed: list[str] = []
    for name, _size in chosen:
        try:
            row = state.rows(
                f"SELECT /* {MARKER} */ pg_prewarm(CAST(? AS regclass), ?) AS blocks",
                (name, mode),
            )
            blocks += int(row[0]["blocks"] or 0)
        except Exception:  # noqa: BLE001 - one relation (dropped mid-way, say) is not all
            logger.debug("prewarm: %s failed", name, exc_info=True)
            failed.append(name)
    outcome = {
        "available": True,
        "mode": mode,
        "relations": len(chosen) - len(failed),
        "bytes": total,
        "blocks": blocks,
        "skipped": skipped,
        "failed": failed,
        "seconds": round(time.monotonic() - began, 2),
    }
    logger.info(
        "Warmed the search path's tables: %d relation(s), %.0f MB into %s cache in %.1fs",
        outcome["relations"],
        total / 1048576,
        "the database's" if mode == "buffer" else "the operating system's",
        outcome["seconds"],
    )
    return outcome


def _extension(state: Any) -> bool:
    """Whether ``pg_prewarm()`` can be called here, creating it if allowed."""

    if state.rows("SELECT 1 AS ok FROM pg_extension WHERE extname = 'pg_prewarm'"):
        return True
    if not state.rows("SELECT 1 AS ok FROM pg_available_extensions WHERE name = 'pg_prewarm'"):
        return False
    try:
        state.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
    except Exception:  # noqa: BLE001 - not allowed to (not a superuser): skip
        logger.debug("prewarm: cannot create the pg_prewarm extension", exc_info=True)
        return False
    return True


def _targets(state: Any, relations: tuple[str, ...]) -> list[tuple[str, int]]:
    """Each relation that exists, then its indexes, as ``(name, bytes)``."""

    out: list[tuple[str, int]] = []
    for relation in relations:
        rows = state.rows(
            "SELECT c.relname AS name, pg_relation_size(c.oid) AS size, 0 AS ord"
            " FROM pg_class c WHERE c.oid = to_regclass(?)"
            " UNION ALL"
            " SELECT i.relname, pg_relation_size(i.oid), 1"
            " FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid"
            " WHERE x.indrelid = to_regclass(?)"
            " ORDER BY ord, name",
            (relation, relation),
        )
        out.extend((str(row["name"]), int(row["size"] or 0)) for row in rows)
    return out
