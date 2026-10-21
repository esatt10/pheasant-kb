"""An exact nearest-neighbour scan held in memory, for a LanceDB table that fits.

LanceDB with no ANN index answers a query by reading every vector from its
files: 280 ms p50 for 60,509 x 1536-d vectors on one core (the pheasant-kb
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

**A new version is a delta, not a reload** (`_delta`). Chunk ids are
content-addressed -- the text's hash is part of the id -- so an id that is in
both versions names the same text and therefore the same vector, and the only
changes between two versions are ids added and ids removed. A refresh reads the
id column alone, diffs it against the held copy, fetches just the added rows
and drops the removed ones: the same rows a full read produces, for a fraction
of it. A change too large to be a delta (a new model, a rebuilt source) falls
back to the full read, as does anything unexpected. The loading thread also
lowers its own priority (`_yield_cpu`), so on a small api tier a refresh takes
the CPU only when a request is not using it.
"""

from __future__ import annotations

import json
import logging
import os
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
RELOAD_MIN_SECONDS = 10.0

#: A delta larger than this share of the held rows is read in full instead:
#: past it, fetching the added rows by id costs about what one scan does.
DELTA_MAX_SHARE = 0.25

#: Ids per ``chunk_id IN (...)`` fetch of a delta's added rows.
DELTA_FETCH_BATCH = 400

#: The niceness a loading thread takes. Linux schedules each thread on its
#: own, so this ranks a refresh below the request threads within the
#: container's CPU quota, rather than splitting the quota with them.
LOAD_NICENESS = 19

#: How often the refresher started by :meth:`ResidentVectors.warm` looks for
#: a newer version. One ``table.version`` read; the reload itself is throttled.
REFRESH_SECONDS = 10.0


#: Spare rows a full load leaves after the live ones, as a share of them (and
#: at least SPARE_MIN_ROWS): what a delta appends into without copying.
SPARE_SHARE = 0.25
SPARE_MIN_ROWS = 4096

#: Dead rows -- removed by a delta, still occupying their slot -- past this
#: share of a snapshot make the next refresh a full load, which compacts.
DEAD_MAX_SHARE = 0.10


class _Snapshot:
    """One table version, decoded: unit-norm rows, ids and raw payloads.

    ``matrix`` is a buffer with spare capacity, of which rows ``[:n]`` belong to
    this snapshot; ``alive`` marks which of those are still in the table (None:
    all of them) and ``position`` maps each live id to its row. A delta appends
    into ``matrix[n:]`` and hands the next snapshot the same buffer, so a query
    still holding this one -- which reads only ``[:n]`` -- never sees a row it
    did not load, and no update copies the rows it keeps.
    """

    __slots__ = ("version", "ids", "payloads", "matrix", "n", "alive", "position")

    def __init__(
        self,
        version: int,
        ids: list[str],
        payloads: list[str],
        matrix: np.ndarray,
        n: int | None = None,
        alive: np.ndarray | None = None,
        position: dict[str, int] | None = None,
    ):
        self.version = version
        self.ids = ids
        self.payloads = payloads
        self.matrix = matrix
        self.n = len(ids) if n is None else n
        self.alive = alive
        self.position = (
            position if position is not None else {chunk_id: i for i, chunk_id in enumerate(ids)}
        )

    @property
    def live(self) -> int:
        return len(self.position)


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
        if self.background:
            _yield_cpu()
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
            with self._lock:
                held = self._snapshot
            snapshot = _delta(table, version, held) if held is not None else None
            how = "updated" if snapshot is not None else "loaded"
            if snapshot is None:
                snapshot = _full(table, version)
            with self._lock:
                self._snapshot = snapshot
            logger.info(
                "Holding %d vectors resident (v%s, %.0f MB), %s in %.2fs",
                snapshot.live,
                version,
                snapshot.matrix[: snapshot.n].nbytes / 1e6,
                how,
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
        if snapshot is None or not snapshot.live:
            return None
        rows = snapshot.matrix[: snapshot.n]
        query = np.asarray(query_vec, dtype=np.float32)
        if query.shape[0] != rows.shape[1]:
            return None  # a dimension change is mid-flight; the store's own scan knows
        norm = float(np.linalg.norm(query))
        if norm == 0.0:
            return []
        scores = rows @ (query / norm)
        if snapshot.alive is not None:
            scores[~snapshot.alive] = -np.inf
        k = min(k, snapshot.live)
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


def _full(table: Any, version: int) -> _Snapshot:
    arrow = table.search().select(COLUMNS).limit(None).to_arrow()
    ids = arrow.column("chunk_id").to_pylist()
    payloads = arrow.column("payload_json").to_pylist()
    spare = max(SPARE_MIN_ROWS, int(len(ids) * SPARE_SHARE))
    flat = arrow.column("vector").combine_chunks()
    matrix = _unit_rows(flat, len(ids), capacity=len(ids) + spare)
    return _Snapshot(version, ids, payloads, matrix)


def _delta(table: Any, version: int, held: _Snapshot) -> _Snapshot | None:
    """``held`` brought to ``version`` by its added and removed ids, or ``None``.

    Chunk ids are content-addressed, so an id in both versions is the same
    vector and the change is exactly the ids added and removed. The added rows
    are written into the spare capacity after ``held.n`` and the removed ones
    marked dead; nothing ``held`` keeps is copied. The first version compacted
    the whole matrix on every delta, and on the lab that copy alone -- 320 MB
    into freshly faulted pages, 83k of them -- was 2.6 to 10.6 s of a 5-6 s
    refresh, where appending a refresh's rows took 0.2 ms.

    ``None`` means "read it in full": the change is too large, the spare
    capacity is spent, dead rows have reached DEAD_MAX_SHARE (a full read
    compacts), the ids are not unique, a fetch did not return what the id
    column promised, or anything raised.
    """

    try:
        current = table.search().select(["chunk_id"]).limit(None).to_arrow()
        ids_now = current.column("chunk_id").to_pylist()
        now = set(ids_now)
        if len(now) != len(ids_now):
            return None
        removed = [chunk_id for chunk_id in held.position if chunk_id not in now]
        added = [chunk_id for chunk_id in ids_now if chunk_id not in held.position]
        if not removed and not added:
            return _Snapshot(
                version, held.ids, held.payloads, held.matrix, held.n, held.alive, held.position
            )
        if len(removed) + len(added) > DELTA_MAX_SHARE * max(1, held.live):
            return None
        if held.n + len(added) > held.matrix.shape[0]:
            return None  # the spare capacity is spent: a full read sizes a new buffer
        dead = held.n - held.live + len(removed)
        if dead > DEAD_MAX_SHARE * max(1, held.n + len(added)):
            return None
        new_ids: list[str] = []
        new_payloads: list[str] = []
        at = held.n
        for start in range(0, len(added), DELTA_FETCH_BATCH):
            batch = added[start : start + DELTA_FETCH_BATCH]
            quoted = ", ".join("'" + chunk_id.replace("'", "''") + "'" for chunk_id in batch)
            arrow = (
                table.search().where(f"chunk_id IN ({quoted})").select(COLUMNS).limit(None)
            ).to_arrow()
            rows = _unit_rows(arrow.column("vector").combine_chunks(), arrow.num_rows)
            # Beyond `held.n`: no reader of `held` looks here.
            held.matrix[at : at + arrow.num_rows] = rows
            at += arrow.num_rows
            new_ids.extend(arrow.column("chunk_id").to_pylist())
            new_payloads.extend(arrow.column("payload_json").to_pylist())
        if sorted(new_ids) != sorted(added):
            return None  # the table moved under the fetch; a full read settles it
        n = held.n + len(new_ids)
        alive = np.ones(n, dtype=bool)
        if held.alive is not None:
            alive[: held.n] = held.alive
        position = dict(held.position)
        for chunk_id in removed:
            alive[position.pop(chunk_id)] = False
        for offset, chunk_id in enumerate(new_ids):
            position[chunk_id] = held.n + offset
        return _Snapshot(
            version,
            held.ids + new_ids,
            held.payloads + new_payloads,
            held.matrix,
            n,
            alive if not alive.all() else None,
            position,
        )
    except Exception:  # noqa: BLE001 - the full read is always the answer
        logger.debug("resident vector delta failed; reading in full", exc_info=True)
        return None


def _yield_cpu() -> None:
    """Lower this thread's scheduling priority, where the platform allows it."""

    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), LOAD_NICENESS)
    except (AttributeError, OSError):  # not Linux, or not permitted: no matter
        pass


def _unit_rows(flat: Any, rows: int, *, capacity: int | None = None) -> np.ndarray:
    """The vectors as one writable float32 matrix of unit rows.

    With ``capacity``, a buffer of that many rows whose first ``rows`` are the
    vectors (the rest uninitialised spare, see `_Snapshot`). A zero-copy view
    of Arrow's buffer is copied once into memory this process owns, then
    normalised in place: ``np.array(flat.values)`` walked the values one by one
    (4.3 s for 52k x 1536 on one core) and ``np.linalg.norm`` plus a divide
    added 7.3 s. A zero row stays zero.
    """

    width = flat.type.list_size
    values = flat.values.to_numpy(zero_copy_only=False)
    buffer = np.empty((max(rows, capacity or rows), width), dtype=np.float32)
    matrix = buffer[:rows]
    matrix[...] = values.reshape(rows, width)
    norms = np.sqrt(np.einsum("ij,ij->i", matrix, matrix))
    norms[norms == 0.0] = 1.0
    matrix *= (1.0 / norms)[:, None]
    return buffer if capacity else matrix
