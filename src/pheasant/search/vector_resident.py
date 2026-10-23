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

**No query waits for a load.** The first version loaded the table inside
whichever request arrived first -- 37.9 s for one question on the lab, after
every restart -- and reloaded the whole table on every new version, which an
indexing source produces every few seconds, so back-to-back reloads competed
on the api tier's one core with the queries they were meant to speed up. Now:

* a serving process calls :meth:`ResidentVectors.warm` at boot, which loads in
  the background and then keeps the copy current from a refresher thread;
* until a copy exists, ``search`` returns ``None`` and the store answers from
  LanceDB (its ANN index when one is configured) rather than blocking;
* a newer version starts a reload at most every :data:`RELOAD_MIN_SECONDS`,
  so an indexing burst costs one reload per window, not one per write;
* the decode is a zero-copy view and an in-place normalise (`_unit_rows`).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: The columns a resident copy holds. Never ``payload_json``'s parse: payloads
#: are ~180 bytes and only the top k are decoded per query.
COLUMNS = ["chunk_id", "payload_json", "vector"]

#: The shortest gap between two reloads. While a source indexes, the table
#: gains a version every few seconds; reloading per version spent the api
#: tier's CPU on copies nobody read before the next one replaced them.
RELOAD_MIN_SECONDS = 30.0

#: How often the refresher started by :meth:`ResidentVectors.warm` looks for
#: a newer version. One ``table.version`` read; the reload itself is throttled.
REFRESH_SECONDS = 10.0


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
        #: Monotonic start of the last load, for the reload throttle.
        self._last_load: float | None = None
        self._refresher: threading.Thread | None = None

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
            matrix = _unit_rows(arrow.column("vector").combine_chunks(), len(ids))
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
        now = time.monotonic()
        with self._lock:
            held = self._snapshot
            if held is not None and held.version == version:
                return held
            if self._oversized == version or self._loading is not None:
                return held
            if (
                self.background
                and held is not None
                and self._last_load is not None
                and now - self._last_load < RELOAD_MIN_SECONDS
            ):
                return held  # throttled; the refresher reloads once the window passes
            self._loading = version
            self._last_load = now
        if not self.background:
            self._load(table, version)
            with self._lock:
                return self._snapshot
        threading.Thread(
            target=self._load, args=(table, version), name="resident-vectors", daemon=True
        ).start()
        # Never the request's wait: with nothing held yet the store answers
        # from LanceDB, and otherwise from the held copy, until this lands.
        return held

    def warm(self, table_fn: Callable[[], Any]) -> None:
        """Load in the background now, and keep the copy current from then on.

        ``table_fn`` returns the table as it is now, or ``None`` while it does
        not exist; the refresher calls it off the request path. Started once
        per process, by a role that serves searches, at boot.
        """

        if not self.background or self._refresher is not None:
            return

        def refresh() -> None:
            while True:
                try:
                    table = table_fn()
                    if table is not None:
                        self._ensure(table)
                except Exception:  # noqa: BLE001 - a missed beat costs one window
                    logger.debug("resident vector refresh failed", exc_info=True)
                time.sleep(REFRESH_SECONDS)

        self._refresher = threading.Thread(
            target=refresh, name="resident-vectors-refresh", daemon=True
        )
        self._refresher.start()

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


def _unit_rows(flat: Any, rows: int) -> np.ndarray:
    """The vectors as one writable float32 matrix of unit rows.

    A zero-copy view of Arrow's buffer copied once into memory this process
    owns, then row norms by ``einsum`` and an in-place scale. Measured for
    52k x 1536 on one core: ``np.array(flat.values)`` took 4.3 s (it walks
    the values) and ``np.linalg.norm`` plus a divide took 7.3 s. A zero row
    stays zero.
    """

    width = flat.type.list_size
    matrix = np.array(flat.values.to_numpy(zero_copy_only=False), dtype=np.float32, copy=True)
    matrix = matrix.reshape(rows, width)
    norms = np.sqrt(np.einsum("ij,ij->i", matrix, matrix))
    norms[norms == 0.0] = 1.0
    matrix *= (1.0 / norms)[:, None]
    return matrix
