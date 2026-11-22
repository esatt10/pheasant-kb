from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from pheasant.graph.simple import SimpleMultiDiGraph
from pheasant.persistence.graph_rows import GraphRowStore

logger = logging.getLogger(__name__)

# Synapse step 21.6 (session A): compressed timestamped graph snapshots.
# A snapshot lives beside the (uncompressed, fast-load) ``graph.latest.json``
# as ``graph.<utc-ts>.json.zst`` so history accrues without bloating the hot
# load path. The timestamp is a filesystem-safe ISO-8601 form (``:`` → ``-``).
SNAPSHOT_PATTERN = re.compile(r"^graph\.(?P<ts>.+)\.json\.zst$")


def _ts_for_filename(utc_ts: str) -> str:
    """Filesystem-safe form of an ISO-8601 UTC timestamp."""

    return utc_ts.replace(":", "-")


def _tmp_for(path: Path) -> Path:
    """A temp sibling of ``path`` unique to the writer.

    A fixed ``*.tmp`` name is not safe once two threads can save at the same
    time — a sync pass and a ``DELETE /sources`` handler, say. Both wrote the
    same file, the first ``os.replace`` renamed it away and the second raised
    ``FileNotFoundError``; worse, their interleaved writes could leave a
    corrupt payload behind. Keying the name on pid+thread keeps tmp+rename
    atomic per writer, so concurrent saves are last-writer-wins over two
    individually-complete files.
    """

    return path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def generation_id(compressed: bytes) -> str:
    """Name one published graph, identically on every replica.

    Content-addressed, and deliberately not a counter or a clock. Two replicas
    must agree on the name without coordinating, an unchanged graph must keep
    its name across a re-save (pillar 1), and "which graph answered this
    query" has to be answerable from the answer itself. A digest of the bytes
    that were actually published is the only one of those three that a
    sequence number also gives.

    Sixteen hex characters: this names generations of one region's graph, not
    the contents of the internet, and it is meant to be readable in a log line
    and a `/health` payload.
    """

    return hashlib.sha256(compressed).hexdigest()[:16]


#: ``storage.graph_format`` values this store understands: ``rows``, the graph
#: in ``graph_nodes``/``graph_edges`` (see :mod:`pheasant.persistence.graph_rows`
#: for the measurements). The whole-file ``node_link_json`` backend was removed
#: in 2.0; a region still holding its file imports it once at boot
#: (:meth:`GraphStore.import_persisted_file`).
GRAPH_FORMATS = ("rows",)


class GraphStore:
    """Where the published graph lives: rows in the state database.

    A commit writes what changed and a reader queries; nothing holds the graph
    but the indexer, whose working set it is. The directory holds history
    snapshots (whole-graph files, interval-gated) and, on a region upgrading
    from 1.x, the graph file that is imported once and parked.

    ``state`` may be ``None`` for a store used only for its snapshot files;
    everything that reads or writes the published graph needs it.
    """

    def __init__(
        self,
        root: str | Path,
        state: Any = None,
        graph_format: str = "rows",
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if graph_format not in GRAPH_FORMATS:
            raise ValueError(
                f"storage.graph_format {graph_format!r} is not supported: the graph is "
                "stored as rows (`rows`). The whole-file node_link_json backend was "
                "removed in 2.0; delete the setting."
            )
        self.graph_format = graph_format
        self.state = state
        self.rows = GraphRowStore(state) if state is not None else None
        #: Called with (kb_id, publication record) after a generation is
        #: durable. One hook here rather than a call beside each of the five
        #: `save()` sites, which is how one of them would eventually be
        #: forgotten. The store stays ignorant of what is on the other end:
        #: the engine injects a notifier, and a region with no broker leaves
        #: this None and behaves exactly as it did.
        self.on_publish: Any = None
        # One store instance is shared by the sync thread and the API request
        # threads (``DELETE /sources`` saves the graph too), so writes are
        # serialized here: it keeps two threads from serializing the same
        # graph twice, and on Windows a concurrent ``os.replace`` onto a
        # destination another thread is replacing fails outright.
        self._write_lock = threading.Lock()

    def kb_dir(self, kb_id: str) -> Path:
        path = self.root / kb_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def graph_path(self, kb_id: str) -> Path:
        """Where the current graph lives: zstd-compressed node-link JSON.

        Uncompressed, a real index is enormous — 445k nodes and 1.5M edges
        pretty-printed came to 928MB, which every checkpoint then rewrote and
        every start re-parsed. The payload is highly repetitive JSON, so zstd
        takes it down by roughly an order of magnitude for a few percent of
        the CPU the write was already costing.
        """

        return self.kb_dir(kb_id) / "graph.latest.json.zst"

    def legacy_graph_path(self, kb_id: str) -> Path:
        """Pre-compression location, still read once so no state is stranded."""

        return self.kb_dir(kb_id) / "graph.latest.json"

    def _require_rows(self) -> GraphRowStore:
        if self.rows is None:
            raise ValueError("the published graph lives in the state database; pass state=")
        return self.rows

    def save(self, kb_id: str, graph: SimpleMultiDiGraph) -> None:
        """Publish the graph: write its delta to the rows and name the result."""

        self._require_rows()
        self._save_rows(kb_id, graph)

    def _save_rows(self, kb_id: str, graph: SimpleMultiDiGraph) -> None:
        """Write what changed, then name the result.

        The delta is read, written and only then cleared — never claimed up
        front. A delta claimed by a commit that then fails is a node the graph
        no longer believes is dirty and that nothing will ever write again;
        the search index can afford that (it is a cache and rebuilds), and the
        published graph cannot. Same reasoning, opposite conclusion, which is
        why ``take_index_delta`` and ``graph_delta`` are shaped differently.
        """

        delta = graph.graph_delta()
        self.rows.apply_delta(
            kb_id,
            node_upserts=delta["node_upserts"],
            node_removals=delta["node_removals"],
            edge_upserts=delta["edge_upserts"],
            edge_removals=delta["edge_removals"],
        )
        graph.clear_graph_delta(delta)
        metadata = self.rows.publish(kb_id)
        self._announce(kb_id, metadata)

    def publication_stamp(self, kb_id: str) -> Any:
        """Something that changes when a new generation is published, or None.

        The published id itself — content-addressed, so an unchanged graph
        re-published still compares equal and does not provoke a reload.
        """

        record = self._require_rows().published_generation(kb_id)
        return record["generation_id"] if record else None

    def serving_graph(self, kb_id: str) -> Any:
        """A graph to answer queries from, without holding one.

        A :class:`~pheasant.graph.sql.SqlGraph`: a bounded walk is a bounded
        query, so a replica that only serves holds nothing — 1.5 GB at 100k
        files, measured, was the residency this replaced.
        """

        self._require_rows()
        from pheasant.graph.sql import SqlGraph

        return SqlGraph(self.state, kb_id)

    def import_persisted_file(self, kb_id: str) -> dict[str, int] | None:
        """One-shot: move a graph file into rows, keeping the file.

        Runs when a graph file exists and the tables are empty — a region
        upgrading from the whole-file backend. Idempotent by that condition and
        by the write itself (the rows are content-addressed, so a repeat
        produces the same digests and the same generation id).

        The file is **renamed, never deleted** — ``/state`` is user data
        (CLAUDE.md rule 2), and the same ``.migrated`` suffix the
        pre-compression graph already used. Returns None when there was
        nothing to import, so a caller can tell "migrated" from "already
        there" rather than inferring it from row counts.
        """

        if self.rows is None:
            return None
        if self.rows.counts(kb_id)[0]:
            return None
        path, legacy = self.graph_path(kb_id), self.legacy_graph_path(kb_id)
        if not path.exists() and not legacy.exists():
            return None
        graph = self._load_file(kb_id)
        if not len(graph):
            return None
        graph.mark_all_pending()
        delta = graph.graph_delta()
        written = self.rows.apply_delta(
            kb_id,
            node_upserts=delta["node_upserts"],
            edge_upserts=delta["edge_upserts"],
        )
        self.rows.publish(kb_id)
        for original in (path, legacy):
            if original.exists():
                try:
                    original.rename(original.with_suffix(original.suffix + ".migrated"))
                except OSError:  # pragma: no cover - best effort, never fails a boot
                    logger.warning("Could not park the migrated graph file %s", original)
        logger.info(
            "Imported the persisted graph into %s: %d nodes, %d edges",
            "graph_nodes/graph_edges",
            written["nodes"],
            written["edges"],
        )
        return written

    def _announce(self, kb_id: str, metadata: dict[str, object]) -> None:
        """Tell whoever is listening, and never let it cost the commit.

        The graph is already durable by the time this runs. A broker that is
        down, slow or absent must therefore be unable to affect the outcome —
        the worst it can do is leave every replica to notice on its next poll,
        which is what they did before this existed.
        """

        hook = self.on_publish
        if hook is None:
            return
        try:
            hook(kb_id, dict(metadata))
        except Exception:  # noqa: BLE001 - a notification is not a commit
            logger.warning("Could not announce a graph generation", exc_info=True)

    def published_generation(self, kb_id: str) -> dict[str, object] | None:
        """The publication record for the published graph, or None (one row)."""

        return self._require_rows().published_generation(kb_id)

    def counts(self, kb_id: str) -> tuple[int, int]:
        """Published node and edge counts, maintained on write (never counted)."""

        return self._require_rows().counts(kb_id)

    @staticmethod
    def _sweep_stale_tmp(directory: Path, max_age_s: float = 3600.0) -> None:
        """Delete temp files no live writer could still own.

        A writer cleans up its own temp on failure, but a killed process (a
        container recreate mid-save) cannot — and those leftovers accumulate in
        /state at full graph size. Only ``*.tmp`` written by this store is
        touched, and only once it is far too old to belong to a running save.
        """

        cutoff = time.time() - max_age_s
        for stale in directory.glob("*.tmp"):
            try:
                if stale.stat().st_mtime < cutoff:
                    stale.unlink()
            except OSError:  # pragma: no cover - racing another writer is fine
                pass

    def load(self, kb_id: str) -> SimpleMultiDiGraph:
        """Materialize the whole graph into memory.

        The right call for the **indexer**, whose working set this is: the
        builder mutates it and the whole-graph enrichment passes walk it. A
        process that only answers queries uses :meth:`serving_graph` instead.
        """

        rows = self._require_rows()
        # Loading is not a change, so the graph comes back owing the store
        # nothing. Otherwise the first commit after a restart would rewrite
        # every row it had just read — an O(total) write, which is the exact
        # cost this backend exists to remove.
        return SimpleMultiDiGraph.from_stored(rows.iter_nodes(kb_id), rows.iter_edges(kb_id))

    def _load_file(self, kb_id: str) -> SimpleMultiDiGraph:
        """Read a 1.x graph file, for :meth:`import_persisted_file` only."""

        import zstandard as zstd

        path = self.graph_path(kb_id)
        if path.exists():
            raw = zstd.ZstdDecompressor().decompress(path.read_bytes())
            return SimpleMultiDiGraph.from_node_link(json.loads(raw))
        # Pre-compression state directory: read it once; the next save writes
        # the compressed form and retires this file.
        legacy = self.legacy_graph_path(kb_id)
        if legacy.exists():
            return SimpleMultiDiGraph.from_node_link(json.loads(legacy.read_bytes()))
        return SimpleMultiDiGraph()

    # --- snapshots (Synapse step 21.6, session A) ---------------------------

    def snapshot_path(self, kb_id: str, utc_ts: str) -> Path:
        return self.kb_dir(kb_id) / f"graph.{_ts_for_filename(utc_ts)}.json.zst"

    def write_snapshot(self, kb_id: str, graph: SimpleMultiDiGraph, utc_ts: str) -> Path:
        """Write a zstd-compressed, durably-fsynced graph snapshot.

        The current graph is serialized as ``graph.latest.json.zst`` is
        (compact, ``sort_keys=True``, zstd level 3), which is also what the file
        backend's :meth:`snapshot_current` copies. tmp+rename+fsync keeps a
        crash from leaving a torn ``.zst`` file.
        """

        return self.write_snapshot_payload(kb_id, graph.to_node_link(), utc_ts)

    def write_snapshot_payload(self, kb_id: str, node_link: dict, utc_ts: str) -> Path:
        """The bytes half of :meth:`write_snapshot`, given an already-built payload.

        Split out so a snapshot from rows and a snapshot from a resident graph
        are the same file written by the same code — a history format that
        differed by which backend produced it would be a history nobody could
        diff.

        Compact, not ``indent=2``: ``json`` runs its C encoder only when
        ``indent`` is ``None``, so the indented form went through the
        pure-Python one. Measured on a 123k-node / 761k-edge region, a 346 MB
        document: **123 s** to encode, inline in the sync that triggered it.
        Every reader is ``json.loads``, which never saw the whitespace. Level 3
        matches ``graph.latest.json.zst``: 8.3 s at level 10 against 1.0 s, for
        14 MB against 17 MB.
        """

        import zstandard as zstd

        path = self.snapshot_path(kb_id, utc_ts)
        tmp = _tmp_for(path)
        payload = json.dumps(node_link, sort_keys=True, separators=(",", ":")).encode("utf-8")
        compressed = zstd.ZstdCompressor(level=3).compress(payload)
        try:
            with self._write_lock:
                with tmp.open("wb") as fh:
                    fh.write(compressed)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self._fsync_dir(path.parent)
        return path

    def snapshot_current(
        self, kb_id: str, utc_ts: str, resident: SimpleMultiDiGraph | None = None
    ) -> Path:
        """Preserve the published generation as a history file.

        The snapshot is materialized. O(graph), and in the right place:
        snapshots are history, they are interval-gated rather than per-commit,
        and they are read whole or not at all.

        ``resident`` is the working set of a process that loaded this kb's
        graph from the store. It is serialized instead of reading the rows
        back, but only while it owes the store nothing
        (:meth:`SimpleMultiDiGraph.settled_node_link`): then it *is* the
        published generation. From a settled working set it is 16 s where
        reading the rows back is 87 s (123k nodes / 761k edges, Postgres),
        because the indexer has just paid for that read once already.
        """

        self._require_rows()
        node_link = resident.settled_node_link() if resident is not None else None
        if node_link is None:
            from pheasant.graph.sql import SqlGraph

            node_link = SqlGraph(self.state, kb_id).to_node_link()
        return self.write_snapshot_payload(kb_id, node_link, utc_ts)

    def read_snapshot(self, path: str | Path) -> SimpleMultiDiGraph:
        """Decompress + load a ``graph.<ts>.json.zst`` snapshot."""

        import zstandard as zstd

        with Path(path).open("rb") as raw, zstd.ZstdDecompressor().stream_reader(raw) as reader:
            return SimpleMultiDiGraph.from_node_link(json.load(reader))

    def list_snapshots(self, kb_id: str) -> list[Path]:
        """All snapshot files for a KB, oldest-first (by embedded timestamp)."""

        kb_dir = self.root / kb_id
        if not kb_dir.exists():
            return []
        # The current graph is `graph.latest.json.zst`, which matches the
        # snapshot pattern by shape. It is not history and must never be
        # offered to retention, which deletes what it is given.
        live = self.graph_path(kb_id).name
        snapshots = [
            p for p in kb_dir.iterdir() if p.name != live and SNAPSHOT_PATTERN.match(p.name)
        ]
        # Filenames embed a sortable ISO-8601 timestamp, so lexical sort on the
        # name is chronological; ties fall back to mtime for robustness.
        snapshots.sort(key=lambda p: (p.name, p.stat().st_mtime))
        return snapshots

    def enforce_retention(self, kb_id: str, max_bytes: int) -> list[Path]:
        """Delete oldest snapshots until total snapshot bytes ≤ ``max_bytes``.

        Only snapshot ``.zst`` files are ever evicted — never
        ``graph.latest.json``, the SQLite db, or the contract. Returns the list
        of evicted paths (oldest-first). A non-positive ``max_bytes`` disables
        retention (keep everything).
        """

        if max_bytes <= 0:
            return []
        snapshots = self.list_snapshots(kb_id)
        total = sum(p.stat().st_size for p in snapshots)
        evicted: list[Path] = []
        # Always keep the newest snapshot even if it alone exceeds the cap — a
        # region must retain at least one point-in-time graph history entry.
        for path in snapshots[:-1]:
            if total <= max_bytes:
                break
            size = path.stat().st_size
            path.unlink()
            total -= size
            evicted.append(path)
        if evicted:
            self._fsync_dir(self.root / kb_id)
        return evicted

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:  # pragma: no cover - platform-dependent best effort
            pass
