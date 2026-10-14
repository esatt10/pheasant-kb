"""An exact nearest-neighbour scan held in memory, for a LanceDB table that fits.

LanceDB with no ANN index answers a query by reading every vector from its
files: 280 ms p50 for 60,509 x 1536-d vectors on one core (the pheasant-lab
api tier), and compaction made it slower, not faster. The same top-k as one
float32 matrix-vector product is 34 ms with an identical result set, because
the matrix is already decoded and contiguous and BLAS reads it at memory
bandwidth. The price is residency -- 4 bytes x dimensions per vector, 372 MB
at that size -- which is why it is opt-in (``search.vector_store.resident``)
and capped (``resident_max_mb``): a region over the cap keeps the disk scan.

Exact, not approximate: the result is the same set LanceDB returns, so turning
this on changes latency and nothing else a caller can observe. The ANN index
(``vector_ann``) is the other trade, for a corpus too large to hold.

Freshness follows the table's version. A search on a newer version than the
one held starts a background reload and answers from the held copy until it
lands, so a write costs readers one stale answer window (seconds) rather than
a blocked request; the vector arm already skips hits whose chunk is gone.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: The columns a resident copy holds. Never ``payload_json``'s parse: payloads
#: are ~180 bytes and only the top k are decoded per query.
COLUMNS = ["chunk_id", "payload_json", "vector"]


class _Snapshot:
    """One table version, decoded: unit-norm rows, ids and raw payloads."""

    __slots__ = ("version", "ids", "payloads", "matrix")

    def __init__(self, version: int, ids: list[str], payloads: list[str], matrix: np.ndarray):
        self.version = version
        self.ids = ids
        self.payloads = payloads
        self.matrix = matrix


class ResidentVectors:
    """A per-process exact cosine top-k over one LanceDB table, or nothing.

    ``search`` returns ``None`` whenever it cannot answer -- disabled, over the
    cap, not loaded yet in a non-blocking caller -- and the store falls back to
    its own scan. It never answers from a table it has not read.
    """

    def __init__(self, max_bytes: int, *, background: bool = True):
        self.max_bytes = int(max_bytes)
        self.background = background
        self._snapshot: _Snapshot | None = None
        self._lock = threading.Lock()
        self._loading: int | None = None
        #: The version known to exceed the cap, so it is not re-read per query.
        self._oversized: int | None = None

    @staticmethod
    def _bytes_for(table: Any) -> int:
        width = getattr(table.schema.field("vector").type, "list_size", None) or 0
        return int(table.count_rows()) * int(width) * 4

    def _load(self, table: Any, version: int) -> None:
        started = time.monotonic()
        try:
            if self._bytes_for(table) > self.max_bytes:
                self._oversized = version
                with self._lock:
                    self._snapshot = None  # an older, smaller copy is not this table
                logger.info(
                    "Vector table v%s exceeds search.vector_store.resident_max_mb; "
                    "searching it on disk",
                    version,
                )
                return
            arrow = table.search().select(COLUMNS).limit(None).to_arrow()
            ids = arrow.column("chunk_id").to_pylist()
            payloads = arrow.column("payload_json").to_pylist()
            flat = arrow.column("vector").combine_chunks()
            width = flat.type.list_size
            matrix = np.array(flat.values, dtype=np.float32).reshape(len(ids), width)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0.0] = 1.0
            matrix /= norms
            snapshot = _Snapshot(version, ids, payloads, matrix)
            with self._lock:
                self._snapshot = snapshot
            logger.info(
                "Holding %d vectors resident (v%s, %.0f MB) in %.1fs",
                len(ids),
                version,
                matrix.nbytes / 1e6,
                time.monotonic() - started,
            )
        except Exception:  # noqa: BLE001 - an optimisation; the disk scan still answers
            logger.warning("Loading resident vectors failed; searching on disk", exc_info=True)
        finally:
            with self._lock:
                self._loading = None

    def _ensure(self, table: Any) -> _Snapshot | None:
        version = int(table.version)
        with self._lock:
            held = self._snapshot
            if held is not None and held.version == version:
                return held
            if self._oversized == version or self._loading == version:
                return held
            self._loading = version
        if held is None or not self.background:
            self._load(table, version)
            with self._lock:
                return self._snapshot
        threading.Thread(
            target=self._load, args=(table, version), name="resident-vectors", daemon=True
        ).start()
        return held  # one version behind until the reload lands

    def search(
        self, table: Any, query_vec: list[float], k: int
    ) -> list[tuple[str, float, dict[str, Any]]] | None:
        snapshot = self._ensure(table)
        if snapshot is None or not snapshot.ids:
            return None
        query = np.asarray(query_vec, dtype=np.float32)
        if query.shape[0] != snapshot.matrix.shape[1]:
            return None  # a dimension change is mid-flight; the store's own scan knows
        norm = float(np.linalg.norm(query))
        if norm == 0.0:
            return []
        scores = snapshot.matrix @ (query / norm)
        k = min(k, len(snapshot.ids))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top], kind="stable")]
        return [
            (
                snapshot.ids[i],
                float(scores[i]),
                json.loads(snapshot.payloads[i] or "{}"),
            )
            for i in top
        ]
