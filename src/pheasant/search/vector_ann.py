"""The optional approximate index on the LanceDB vector table.

Off by default (``search.vector_store.ann_index: none``), because it trades
recall for latency and a region should choose that knowingly. Measured on the
pheasant-lab stack (60,509 x 1536-d, one core), against an exact scan's 245 ms:

==========================  ========  ===========  ===========
index, query setting        build     k=40         k=300
==========================  ========  ===========  ===========
IVF_HNSW_SQ, nprobes 32     45 s      41 ms, 0.81  54 ms, 0.68
IVF_PQ, nprobes 40 refine   385 s     32 ms, 0.70  133 ms, 0.50
==========================  ========  ===========  ===========

(latency p50, recall against the exact top-k, for short text queries). Those
vectors came from an offline hashed bag-of-words embedder, which has far less
cluster structure than a real embedding model, and recall depends on the
queries as much as the corpus: the same HNSW index kept 0.92 of the top-k for
queries drawn from the corpus itself (what ``python -m
pheasant.search.vector_ann_benchmark`` measures) against 0.68-0.81 for short
questions. Measure a region's own before turning it on, and read the
benchmark as the in-distribution case. Where the vectors fit in memory,
``search.vector_store.resident`` is exact and, on the lab, faster too: 61 ms
for the vector arm against 91 ms with this index.

The writer owns the index: :func:`reconcile` runs at the end of a sync, on the
one process that writes the table, and builds, refreshes, replaces or drops
the index to match the config. Readers only shape their query
(:func:`shape_query`), and with ``none`` they bypass any index that a previous
configuration left behind, so switching it off takes effect on the next query
rather than on the next sync.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

logger = logging.getLogger(__name__)

#: ``ann_index`` values and the LanceDB ``index_type`` each builds.
INDEX_TYPES = {"ivf_hnsw_sq": "IVF_HNSW_SQ", "ivf_pq": "IVF_PQ"}
#: Rebuild once this share of rows arrived after the index was trained; until
#: then LanceDB scans the unindexed rows exactly and merges them in, which is
#: correct and only slower.
REFRESH_UNINDEXED_SHARE = 0.10


def configured(kind: str | None) -> str | None:
    """The normalized index kind, or None for ``none``; unknown kinds refuse."""

    name = (kind or "none").strip().lower()
    if name in {"", "none", "off"}:
        return None
    if name not in INDEX_TYPES:
        raise ValueError(
            f"Unsupported search.vector_store.ann_index {kind!r}; "
            f"expected 'none' or one of {sorted(INDEX_TYPES)}"
        )
    return name


def _vector_index(table: Any) -> Any | None:
    for index in table.list_indices():
        if "vector" in list(getattr(index, "columns", []) or []):
            return index
    return None


def _build_options(kind: str, rows: int, width: int) -> dict[str, Any]:
    if kind == "ivf_pq":
        # sqrt(rows) partitions and 16 dimensions per sub-vector: the shape
        # the table above was measured at.
        return {
            "index_type": "IVF_PQ",
            "num_partitions": max(1, round(math.sqrt(rows))),
            "num_sub_vectors": max(1, width // 16),
        }
    # About a thousand rows per partition, each an HNSW graph.
    return {"index_type": "IVF_HNSW_SQ", "num_partitions": max(1, rows // 1000)}


def reconcile(table: Any, settings: Any) -> str:
    """Make the table's vector index match ``settings``; what was done.

    Maintenance, never correctness: a failure is logged and reported, and
    queries keep answering (exactly) without an index.
    """

    kind = configured(getattr(settings, "ann_index", "none"))
    try:
        existing = _vector_index(table)
        if kind is None:
            if existing is None:
                return "none"
            table.drop_index(existing.name)
            logger.info("Dropped the vector index %s (ann_index: none)", existing.name)
            return "dropped"
        rows = int(table.count_rows())
        if rows < int(getattr(settings, "ann_min_rows", 0) or 0):
            return "below_min_rows"
        wanted = INDEX_TYPES[kind]
        current = str(getattr(existing, "index_type", "") or "").upper() if existing else None
        if existing is not None and current == wanted.replace("_", ""):
            current = wanted  # LanceDB spells some types without underscores
        if existing is not None and current == wanted:
            stats = table.index_stats(existing.name)
            unindexed = int(getattr(stats, "num_unindexed_rows", 0) or 0)
            if unindexed <= REFRESH_UNINDEXED_SHARE * max(1, rows):
                return "current"
        width = int(getattr(table.schema.field("vector").type, "list_size", 0) or 0)
        started = time.monotonic()
        table.create_index(
            metric="cosine",
            vector_column_name="vector",
            replace=True,
            **_build_options(kind, rows, width),
        )
        logger.info(
            "Built the %s vector index over %d rows in %.1fs",
            wanted,
            rows,
            time.monotonic() - started,
        )
        return "built"
    except Exception:  # noqa: BLE001 - an index is an optimisation
        logger.warning("Vector index maintenance failed; searching exactly", exc_info=True)
        return "failed"


def query_table(table: Any, query_vec: list[float], k: int, settings: Any) -> list[dict]:
    """One top-k query on disk: projected, and shaped by ``settings``.

    Projected because without ``select`` every hit carries its whole vector
    back into Python -- 1,536 floats a row -- to be discarded by the caller;
    ``_distance`` is named because LanceDB has announced it stops adding it
    unasked. A LanceDB too old for either (the ``[vector]`` extra allows 0.6)
    gets the unprojected query this always ran, which with no index is the
    same answer.
    """

    vector = [float(value) for value in query_vec]
    try:
        query = table.search(vector).distance_type("cosine").limit(k)
        query = query.select(["chunk_id", "payload_json", "_distance"])
        return shape_query(query, settings, k).to_list()
    except (AttributeError, TypeError, ValueError):
        return table.search(vector).distance_type("cosine").limit(k).to_list()


def shape_query(query: Any, settings: Any, k: int) -> Any:
    """Apply the configured query-time knobs to one LanceDB vector query."""

    kind = configured(getattr(settings, "ann_index", "none"))
    if kind is None:
        return query.bypass_vector_index()
    query = query.nprobes(int(getattr(settings, "ann_nprobes", 32) or 32))
    if kind == "ivf_hnsw_sq":
        return query.ef(max(int(getattr(settings, "ann_ef", 600) or 600), k))
    refine = getattr(settings, "ann_refine_factor", None)
    return query.refine_factor(int(refine)) if refine else query
