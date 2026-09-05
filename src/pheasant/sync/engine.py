from __future__ import annotations

import inspect
import json
import logging
import math
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from pheasant.capacity import GRAPH_SHARE_OF_RSS, RSS_BYTES_PER_NODE
from pheasant.capacity import project as project_capacity
from pheasant.config.schema import FILESYSTEM_SOURCE_TYPES, PheasantConfig, SourceConfig
from pheasant.graph.builder import GraphBuilder
from pheasant.ingestion.captioner import captioner_from_config, source_includes_images
from pheasant.ingestion.content_types import DOCUMENT_EXTENSIONS, TEXT_EXTENSIONS
from pheasant.ingestion.extractor import extractor_from_config, source_includes_documents
from pheasant.ingestion.pipeline import (
    ParsedArtifact,
    git_state,
    parse_connector_payload,
    sha256_bytes,
    utc_now,
)
from pheasant.ingestion.transcriber import source_includes_audio, transcriber_from_config
from pheasant.ingestion.walk import (
    SyncBudgetExceeded,
    WalkBudget,
    aggregate_depth_counts,
    walk_source,
)
from pheasant.persistence.graph_store import GraphStore
from pheasant.persistence.manifest import ManifestStore
from pheasant.persistence.paths import StatePaths
from pheasant.persistence.state_store import StateStore
from pheasant.registry.source_registry import SourceRegistry
from pheasant.search.node_index import NodeIndex
from pheasant.search.vector_store import (
    VectorSearcher,
    vector_indexer_from_config,
)
from pheasant.security import corpus_policy
from pheasant.synapse.events import EventStream, RouterWebhook
from pheasant.synapse.publisher import ContractPublisher
from pheasant.sync.connectors import (
    ConnectorItem,
    ConnectorPayload,
    ItemNotModified,
    connector_for_source,
)
from pheasant.sync.fingerprint import (
    EMBEDDING_SCOPE,
    SOURCE_SCOPE,
    embedding_change,
    embedding_fingerprint,
    source_fingerprint,
)
from pheasant.sync.graph_events import notifier_from_config as graph_notifier_from_config
from pheasant.sync.locks import EngineLease, SourceLease, source_lock
from pheasant.sync.pacing import serve_yield
from pheasant.sync.queue import queue_from_config
from pheasant.sync.saturation import CommitAuthorityMeter

logger = logging.getLogger(__name__)


def _published_timestamp(value: object) -> float | None:
    """A publication record's ISO-8601 instant as a unix timestamp, or None."""

    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


SyncMode = Literal["incremental", "full", "validate_only", "repair"]
SYNC_MODES = {"incremental", "full", "validate_only", "repair"}
FULL_RESUME_SCOPE = "source:{name}:full_sync_phase"

#: ``(phase, current, total, detail)``, optionally followed by a ``meta``
#: mapping (Phase 35.1) carrying ``source`` plus whatever counters the
#: emitting pass has — ``indexed``, ``skipped``, ``bytes_done``.
#: ``total`` is None until the connector has finished listing, because it is
#: not known before then and a made-up denominator is worse than none.
#: Four-argument callbacks stay supported: :func:`_hook_accepts_meta` decides
#: by signature which form to call, so an existing caller is unaffected.
ProgressHook = Callable[..., None]


def _hook_accepts_meta(hook: ProgressHook) -> bool:
    """Does this callback take the Phase-35.1 ``meta`` argument?

    Determined once per wrap, by signature, rather than by catching
    ``TypeError`` around the call — a ``TypeError`` raised *inside* a
    four-argument hook would otherwise be misread as "wrong arity" and the
    hook silently re-invoked with different arguments.
    """

    try:
        signature = inspect.signature(hook)
    except (TypeError, ValueError):  # builtins, C callables
        return False
    positional = 0
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
    return positional >= 5


def _progress_reporter(hook: ProgressHook | None, source_name: str):
    """Wrap a progress hook so it can never fail the sync that calls it.

    This fires from inside the per-artifact loop. A caller's bad callback —
    a closed queue, an evicted job, a typo — must cost a log line, not an
    index. Returns a no-op when there is no hook, so the un-instrumented path
    stays exactly as cheap as it was.

    Every event carries its ``source`` in ``meta`` (Phase 35.1). Before this,
    ``sync_all`` prefixed the source name into the human-readable ``detail``
    string and nothing could tell the eight sources of one job apart without
    parsing prose back out of it.
    """

    if hook is None:
        return lambda *_args, **_kwargs: None

    wants_meta = _hook_accepts_meta(hook)

    def report(
        phase: str,
        current: int = 0,
        total: int | None = None,
        detail: str = "",
        **stats: Any,
    ) -> None:
        try:
            if wants_meta:
                hook(phase, current, total, detail, {"source": source_name, **stats})
            else:
                hook(phase, current, total, detail)
        except Exception:  # pragma: no cover - progress is never load-bearing
            logger.debug("progress hook failed for %s", source_name, exc_info=True)

    return report


def optimal_checkpoint_interval(
    save_seconds: float,
    *,
    mtbf_seconds: float,
    floor_seconds: float,
    ceiling_seconds: float,
) -> float:
    """How long to wait between graph checkpoints — Young's formula.

    The previous rule was ``max(60s, last_save * 10)``: space checkpoints at
    ten times what one costs, holding overhead near **10%**. That is stable
    and far too expensive. Checkpointing is a solved problem in HPC and ML
    training, and the standard result is Young's (later Daly's) formula:

        T_opt = sqrt(2 * C * MTBF)

    where ``C`` is what a checkpoint costs and ``MTBF`` is the expected time
    between interruptions. It minimizes *total* expected time — the sum of
    checkpoint overhead and the work redone after a crash — rather than
    either one alone, which is why it is not simply "checkpoint less often".

    On pheasant's measured save costs, against a 24h MTBF:

        corpus        C        old interval / overhead    Young / overhead
        2,000        0.13s      60s   /  0.2%             150s  / 0.1%
        20,000       1.32s      60s   /  2.2%             478s  / 0.3%
        100,000      6.71s      67s   / 10.0%            1077s  / 0.6%
        250,000     19.91s     199s   / 10.0%            1855s  / 1.1%

    An order of magnitude less overhead, and the cost is bounded rework: at
    most one interval of indexing is lost, which the ceiling caps at 30
    minutes by default. On a sync that runs for hours at those sizes, that is
    the right trade — and it is the trade the formula is choosing, not a
    guess.

    Degrades sensibly: with no measured save yet (the first checkpoint of a
    fresh graph) or no MTBF configured, it returns the floor, which is the
    pre-35.4 behaviour.
    """

    if save_seconds <= 0 or mtbf_seconds <= 0:
        return floor_seconds
    interval = math.sqrt(2.0 * save_seconds * mtbf_seconds)
    if ceiling_seconds > 0:
        interval = min(interval, ceiling_seconds)
    return max(floor_seconds, interval)


def _restore_iso_ts(filename_ts: str) -> str:
    """Restore an ISO-8601 timestamp from its filesystem-safe snapshot form.

    ``write_snapshot`` builds filenames as ``utc_ts.replace(":", "-")`` over a
    ``YYYY-MM-DDTHH:MM:SSZ`` timestamp. The date hyphens must stay; only the
    time portion's ``-`` separators become ``:`` again. The trailing ``Z`` is
    dropped so ``datetime.fromisoformat`` parses it on Python 3.11.
    """

    ts = filename_ts.rstrip("Z")
    if "T" in ts:
        date_part, _, time_part = ts.partition("T")
        return f"{date_part}T{time_part.replace('-', ':')}"
    return ts


@dataclass(frozen=True)
class SyncResult:
    source_id: str
    indexed_artifacts: int
    skipped_artifacts: int
    graph_nodes: int
    graph_edges: int
    status: str = "healthy"
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _PreparedItem:
    """Immutable worker output consumed by the single coordinated writer."""

    position: int
    item: ConnectorItem
    previous: dict[str, Any] | None
    parsed: ParsedArtifact | None = None
    fetched: bool = False
    skipped: bool = False
    transfer_skipped: bool = False
    #: The `readiness.corpus_denylist` pattern that refused this item. Separate
    #: from `skipped` because an unchanged file and a refused one are both "not
    #: indexed" and nothing like each other — a control folded into a routine
    #: counter is one nobody can see working.
    refused_by: str | None = None


_PROCESS_SAFE_TEXT_EXTENSIONS = TEXT_EXTENSIONS - {".html"}


def _process_safe_text_path(path: str) -> bool:
    candidate = Path(path)
    return (
        candidate.suffix.lower() in _PROCESS_SAFE_TEXT_EXTENSIONS
        or candidate.name.lower() == "dockerfile"
    )


def _process_cpu_capacity() -> int:
    """CPU count visible to this process (affinity/cgroup aware where available)."""

    process_cpu_count = getattr(os, "process_cpu_count", None)
    if process_cpu_count is not None:
        return max(1, int(process_cpu_count() or 1))
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:
        return max(1, int(os.cpu_count() or 1))


def _prepare_filesystem_item_process(
    source: SourceConfig,
    mode: SyncMode,
    git_metadata: tuple[str | None, str | None, bool] | None,
    position: int,
    item: ConnectorItem,
    previous: dict[str, Any] | None,
) -> _PreparedItem:
    """Stateless process-worker entry point for ordinary filesystem text."""

    if (
        mode == "incremental"
        and previous is not None
        and item.sha256 is not None
        and previous.get("sha256") == item.sha256
    ):
        return _PreparedItem(
            position,
            item,
            previous,
            skipped=True,
            transfer_skipped=True,
        )
    path = Path(str(item.metadata["path"]))
    content = path.read_bytes()
    content_hash = sha256_bytes(content)
    if mode == "incremental" and previous and previous.get("sha256") == content_hash:
        return _PreparedItem(
            position,
            item,
            previous,
            fetched=True,
            skipped=True,
        )
    payload = ConnectorPayload(
        item=item,
        content=content,
        mime_type=item.mime_type,
        size_bytes=item.size_bytes,
        sha256=content_hash,
        mtime=item.mtime,
        metadata={"path": str(path)},
    )
    parsed = parse_connector_payload(source, item, payload, git_metadata)
    if parsed is None:
        return _PreparedItem(position, item, previous, fetched=True)
    if mode == "incremental" and previous and previous.get("sha256") == parsed.sha256:
        return _PreparedItem(
            position,
            item,
            previous,
            parsed=parsed,
            fetched=True,
            skipped=True,
        )
    return _PreparedItem(position, item, previous, parsed=parsed, fetched=True)


class SyncEngine:
    def __init__(
        self,
        config: PheasantConfig,
        paths: StatePaths | None = None,
        state: StateStore | None = None,
        lease: EngineLease | None = None,
        *,
        load_persisted_graph: bool = True,
        defer_persisted_graph_load: bool = False,
        initialize_indexing_components: bool = True,
    ):
        self.config = config
        self.paths = paths or StatePaths.from_config(config)
        self.paths.ensure()
        self.state = state or StateStore.from_config(self.config, self.paths.sqlite)
        self.state.migrate()
        # Single-writer lease (Synapse 21.2): acquired lazily on the first
        # sync so read-only engines (e.g. docker-exec MCP stdio beside a
        # running server) keep working against a served state directory.
        self.lease = lease or EngineLease(self.paths.state)
        # In-process serialization across all writers (watcher, scheduler,
        # API startup executor, HTTP /sync). Preparation may run concurrently;
        # only state/graph/vector commits take this mutex.
        self._sync_mutex = threading.RLock()
        self._lease_mutex = threading.Lock()
        SourceRegistry(self.config, self.state).initialize()
        self.manifests = ManifestStore(self.paths.manifests, self.state)
        self.graph_store = GraphStore(
            self.paths.graphs, state=self.state, graph_format=config.storage.graph_format
        )
        # A region upgrading into the row backend still has its graph file.
        # Import it once, keep it, and let every later commit be a delta.
        self.graph_store.import_persisted_file(config.knowledge_base_id)
        # Announce every committed generation on the fleet's broker, so a
        # serving replica reloads at commit latency instead of on its own poll
        # (Phase 35.8). `notifier_from_config` returns the null notifier
        # unless `sync.queue` is on the NATS backend, which is what keeps the
        # standalone path byte-identical: no broker, no publish, no change.
        self._graph_notifier = graph_notifier_from_config(config)
        #: How full this process's commit stream is. Meaningful only where
        #: this process is the commit authority — an api replica publishes
        #: index work rather than running it, so its meter never opens a span
        #: and `/metrics` omits the series rather than reporting a confident
        #: zero from a process that could not saturate if it tried.
        self.commit_meter = CommitAuthorityMeter()
        # One hook for both jobs, covering every save site by construction —
        # the same argument that put the announcement here rather than beside
        # each `graph_store.save`.
        self.graph_store.on_publish = self._on_graph_published
        self.graph_builder = GraphBuilder(config)
        self._loads_persisted_graph = bool(load_persisted_graph)
        self._graph_loaded = not self._loads_persisted_graph
        #: Which published generation this process is actually serving, or
        #: None for a process that holds no graph. "Stale" was previously
        #: undetectable rather than merely unmeasured: a replica that missed a
        #: reload answered from an old graph, correctly-looking, forever.
        self._loaded_graph_generation: str | None = None
        #: TTL on the resolved generation for a process that serves from the
        #: store rather than from a copy. Same 2s as `RemoteGraph.stats`, and
        #: for the same reason: the honest answer needs a read, and the read
        #: must not land on every search.
        self._generation_ttl = 2.0
        self._generation_at = 0.0
        #: When that generation was published, as a unix timestamp. Kept
        #: beside the id so the age gauge measures the graph this process is
        #: serving rather than whatever has since been written to /state.
        self.loaded_graph_published_at: float | None = None
        if self._loads_persisted_graph and not defer_persisted_graph_load:
            existing = self.graph_store.load(config.knowledge_base_id)
            if len(existing):
                self.graph_builder.graph = existing
            self._graph_loaded = True
            self._note_loaded_generation()
        # Optional embed-on-sync (Synapse 21.4): None when
        # search.embeddings.enabled is false, leaving sync byte-identical
        # to pre-21.4 behavior (no vector dir, no embedder calls).
        self.vectors = (
            vector_indexer_from_config(config) if initialize_indexing_components else None
        )
        # Multi-modal image ingestion (Synapse 25.4 session A): None unless a
        # source's include globs admit image extensions, so a text-only region
        # is byte-identical to pre-25.4 behavior (no captioner built, no
        # captioning network call possible). Default provider is the offline
        # deterministic stub.
        self.captioner = captioner_from_config(config) if initialize_indexing_components else None
        # Multi-modal audio ingestion (Synapse 25.4 session B): None unless a
        # source's include globs admit audio extensions — same opt-in,
        # zero-network-when-absent contract as the image captioner above.
        self.transcriber = (
            transcriber_from_config(config) if initialize_indexing_components else None
        )
        # Document text extraction (PDF/DOCX, optionally HTML): None unless a
        # source's include globs admit a document extension (or html_text is
        # on), so a text-only region behaves exactly as it did when .pdf/.docx
        # were accepted and then silently produced no text. Fully offline and
        # deterministic — no model, no network on any provider.
        self.extractor = extractor_from_config(config) if initialize_indexing_components else None
        # Synapse 21.5: contract publisher + NDJSON event stream. The event
        # log is always written (local, useful standalone); contract
        # publication + the router webhook are gated by synapse.publish /
        # synapse.router_url so a router-less region is unchanged.
        self.events = EventStream(self.paths.state)
        self.router_webhook = RouterWebhook(
            config.synapse.router_url,
            timeout=config.synapse.webhook_timeout_seconds,
        )
        # Mid-sync graph checkpointing (see _maybe_checkpoint_graph).
        self._graph_dirty = False
        self._last_checkpoint = time.monotonic()
        self._last_save_seconds = 0.0
        # Derived full-text index over graph nodes, so graph search does not
        # have to scan the graph. Built from the graph, never authoritative.
        self.node_index = NodeIndex(self.state)

    def close(self) -> None:
        """Persist in-flight graph work, release the lease, close the store.

        Called from the server's shutdown path, so `docker compose stop`
        keeps whatever the running sync had built instead of discarding it.
        """
        try:
            self.checkpoint_graph()
        except Exception as exc:  # pragma: no cover - shutdown must not raise
            logger.warning("Could not checkpoint graph on shutdown: %s", exc)
        if self.vectors is not None:
            try:
                self.vectors.flush()
            except Exception as exc:  # pragma: no cover - shutdown must not raise
                logger.warning("Could not flush vector store on shutdown: %s", exc)
        self.lease.release()
        try:
            self._graph_notifier.close()
        except Exception as exc:  # pragma: no cover - shutdown must not raise
            logger.warning("Could not close the graph event client: %s", exc)
        self.state.close()

    def checkpoint_graph(
        self,
        source_name: str | None = None,
        manifest: dict | None = None,
    ) -> bool:
        """Persist in-flight sync work now. True if anything was written.

        The manifest goes with the graph: it is what an incremental pass reads
        to decide a file is unchanged, so checkpointing one without the other
        would still make a resumed sync re-read everything.
        """

        if not self._graph_dirty:
            return False
        self.flush_node_index()
        started = time.monotonic()
        self.graph_store.save(self.config.knowledge_base_id, self.graph_builder.graph)
        # Publish the content-addressed graph generation before advancing the
        # source manifest.  A crash in between causes harmless reprocessing;
        # the inverse order could make an incremental scan skip content whose
        # graph generation was never published.
        if source_name and manifest is not None:
            self.manifests.save(source_name, manifest)
            resume_scope = FULL_RESUME_SCOPE.format(name=source_name)
            if self.state.get_fingerprint(resume_scope) in {"starting", "cleared"}:
                self.state.set_fingerprint(resume_scope, "checkpointed", utc_now())
        self._last_save_seconds = time.monotonic() - started
        self._graph_dirty = False
        self._last_checkpoint = time.monotonic()
        return True

    def reload_graph(self) -> int:
        """Re-read the graph from /state, e.g. after a worker process synced.

        The server does not index (see :mod:`pheasant.sync.worker`), so this is
        how a freshly-indexed graph reaches the process that is serving it.
        Reading a compressed graph is cheap; the swap is atomic from a
        reader's point of view because the new graph replaces the reference in
        one assignment.
        """

        if not self._loads_persisted_graph:
            # Nothing private to reload. On the row backend that is the normal
            # case for a serving replica — it reads the graph out of the shared
            # database, so it is never behind — and the generation it reports
            # still has to keep up, or `/ready` would claim a staleness that
            # cannot happen. See `serving_graph`.
            if self.graph_store.rows is not None:
                self._note_loaded_generation()
            return 0
        loaded = self.graph_store.load(self.config.knowledge_base_id)
        if not len(loaded):
            return 0
        self.graph_builder.graph = loaded
        self._graph_loaded = True
        # The index on disk was written by the worker for this same graph, so
        # nothing is owed; drop the delta the load itself generated.
        loaded.take_index_delta()
        self.node_index._populated = False
        self._note_loaded_generation()
        logger.info(
            "Reloaded graph from state: %d nodes (generation %s)",
            loaded.number_of_nodes(),
            self.loaded_graph_generation or "unlabelled",
        )
        return loaded.number_of_nodes()

    @property
    def loaded_graph_generation(self) -> str | None:
        """Which published generation this process is actually serving.

        For a process holding a **copy** — every process on the whole-file
        backend, and any process that indexes — this is a remembered label,
        set when the graph was loaded or committed. It can lag the published
        one, and the gap between them is exactly the staleness `/ready`
        exists to expose.

        For a process serving **from the store** there is no copy and no gap:
        it reads the rows the last commit wrote. Reporting a remembered label
        there would invent a staleness that cannot happen, and would raise a
        false alarm on the deployment shape the row backend exists to enable —
        so it resolves the published id instead, behind a short TTL so the
        read does not land on every search.
        """

        if self._loads_persisted_graph or self.graph_store.rows is None:
            return self._loaded_graph_generation
        now = time.monotonic()
        if now - self._generation_at > self._generation_ttl:
            record = self.graph_store.published_generation(self.config.knowledge_base_id)
            self._loaded_graph_generation = str(record["generation_id"]) if record else None
            self._generation_at = now
        return self._loaded_graph_generation

    @loaded_graph_generation.setter
    def loaded_graph_generation(self, value: str | None) -> None:
        self._loaded_graph_generation = value

    def serving_graph(self) -> Any:
        """The graph this process answers queries from.

        Two different objects for two different jobs. A process that *builds*
        the graph serves its own working set, which is current by construction
        and is what `role: all` — every standalone container — uses. A process
        that only serves gets whatever the store offers: on ``rows`` a
        :class:`~pheasant.graph.sql.SqlGraph` holding nothing, on the file
        backend the resident copy it loaded, because there is no third option
        there.

        That asymmetry is the residency win stated in one place: the file
        backend has to give a graph to everyone who reads one, and the row
        backend only to whoever writes one.
        """

        if self._loads_persisted_graph or self.graph_store.rows is None:
            return self.graph_builder.graph
        return self.graph_store.serving_graph(self.config.knowledge_base_id)

    def _on_graph_published(self, kb_id: str, record: dict) -> None:
        """This process just committed a generation: adopt it, then announce it.

        Adopting it matters more than it looks. A process that indexes *is*
        serving the graph it just wrote — its in-RAM graph is those bytes — but
        `loaded_graph_generation` was only ever set when a graph was **read**.
        So the single container, which is `role: all` and indexes in-process,
        reported `graph_generation: null` on `/health`, on `/ready` and on
        every search response, for its whole life. The staleness comparison
        that exists to make a stale replica detectable was inert on the
        deployment most people run.

        Found by the two-surface conformance matrix: the HTTP app had loaded a
        graph at construction and had an id, the MCP facade had indexed one and
        did not, and the same operation returned different answers.
        """

        self._loaded_graph_generation = str(record.get("generation_id") or "") or None
        self.loaded_graph_published_at = _published_timestamp(record.get("published_at"))
        if self._graph_notifier.enabled:
            self._graph_notifier.publish(kb_id, record)

    def _note_loaded_generation(self) -> None:
        """Record which generation the in-RAM graph came from.

        Read from the publication record *after* the load, so the label
        describes the bytes that were read rather than whatever has been
        published since. A state directory written before generations existed
        has no id to record and reports None — a missing label, never a
        missing graph.
        """

        try:
            record = self.graph_store.published_generation(self.config.knowledge_base_id)
        except Exception:  # noqa: BLE001 - a label must not break a load
            record = None
        self._loaded_graph_generation = str(record["generation_id"]) if record else None
        self.loaded_graph_published_at = (
            _published_timestamp(record.get("published_at")) if record else None
        )

    def _ensure_persisted_graph_loaded(self) -> None:
        """Materialize the published graph only when a task will mutate it.

        Incremental crawls can decide that every content-addressed item is
        unchanged from the manifest without consulting the graph.  Deferring
        this load turns that common path from a whole-corpus deserialize into
        a directory/metadata delta scan.
        """

        if self._graph_loaded or not self._loads_persisted_graph:
            return
        with self._sync_mutex:
            if self._graph_loaded:
                return
            loaded = self.graph_store.load(self.config.knowledge_base_id)
            if len(loaded):
                self.graph_builder.graph = loaded
                loaded.take_index_delta()
            self._graph_loaded = True
            self._note_loaded_generation()

    def _graph_counts(self) -> tuple[int, int]:
        if self._graph_loaded or not self._loads_persisted_graph:
            graph = self.graph_builder.graph
            return graph.number_of_nodes(), graph.number_of_edges()
        return self.graph_store.counts(self.config.knowledge_base_id)

    #: Above this many changed nodes, rebuild the FTS index wholesale instead
    #: of deleting each stale row individually. `node_id` on graph_nodes_fts
    #: is UNINDEXED (it's read-only lookup data, not searched), so a batched
    #: `DELETE ... WHERE node_id IN (...)` against it has no index to use and
    #: degrades to a table scan per batch. Measured: concept-node
    #: reconciliation removing ~387k of 543k nodes stalled for 10+ minutes
    #: going through the per-row delete path; `replace_all` (one unfiltered
    #: DELETE + bulk INSERT) is the cheap way to apply a change this size.
    _INDEX_REBUILD_THRESHOLD = 20_000

    def flush_node_index(self) -> int:
        """Push pending node writes into the search index. Never fatal.

        The index is derived from the graph, so a failure here costs query
        speed until the next flush or rebuild — never correctness, and never
        a failed sync.
        """

        try:
            upserts, removals = self.graph_builder.graph.take_index_delta()
            if not upserts and not removals:
                return 0
            if len(upserts) + len(removals) > self._INDEX_REBUILD_THRESHOLD:
                return self.rebuild_node_index()
            return self.node_index.apply(upserts, removals)
        except Exception as exc:  # pragma: no cover - cache maintenance
            logger.warning("Could not update the graph node index: %s", exc)
            return 0

    def rebuild_node_index(self) -> int:
        """Rebuild the whole index from the in-memory graph."""

        graph = self.graph_builder.graph
        rows = graph.index_rows()
        written = self.node_index.replace_all(rows)
        # Everything pending is now represented.
        graph.take_index_delta()
        logger.info("Graph node index rebuilt: %d nodes", written)
        return written

    def ensure_node_index(self) -> int:
        """Build the index if it is missing, e.g. after an upgrade.

        Cheap when it already exists: one COUNT.
        """

        if self.node_index.count() > 0:
            if self._graph_loaded:
                self.flush_node_index()
            return 0
        self._ensure_persisted_graph_loaded()
        graph = self.graph_builder.graph
        if graph.number_of_nodes() <= 1:
            return 0
        return self.rebuild_node_index()

    def _maybe_checkpoint(self, source_name: str, manifest: dict) -> None:
        """Checkpoint at most once per configured interval during a sync.

        Time-based rather than every-N-artifacts: the cost of a save scales
        with graph size, not with how many artifacts went into it, so a fixed
        count would checkpoint far too often on a large graph.
        """

        self._graph_dirty = True
        storage = self.config.storage
        floor = float(getattr(storage, "graph_checkpoint_seconds", 0) or 0)
        if floor <= 0:
            return
        interval = optimal_checkpoint_interval(
            self._last_save_seconds,
            mtbf_seconds=float(getattr(storage, "checkpoint_mtbf_seconds", 0) or 0),
            floor_seconds=floor,
            ceiling_seconds=float(getattr(storage, "graph_checkpoint_max_seconds", 0) or 0),
        )
        if time.monotonic() - self._last_checkpoint < interval:
            return
        try:
            self.checkpoint_graph(source_name, manifest)
        except Exception as exc:  # a checkpoint hiccup must not fail the sync
            logger.warning("Checkpoint failed (continuing): %s", exc)

    def startup(self) -> list[SyncResult]:
        """Bring a restarted container back to a serving state.

        The expensive work (reading files, chunking, embedding) has already
        happened and lives in /state, so a restart over unchanged content and
        an unchanged config is close to free. Only two things can be stale,
        and each is repaired the cheap way:

        * the vector space, if the embedding config changed — re-embedded from
          the chunks already in SQLite, without re-reading a single file;
        * the graph, if a sync was interrupted before its last checkpoint —
          healed by a repair pass that re-reads only the artifacts actually
          missing from it.
        """

        self.paths.ensure()
        self.state.migrate()
        SourceRegistry(self.config, self.state).initialize()
        self.reconcile_embeddings()
        self.ensure_node_index()
        results = []
        for source in self.enabled_sources():
            if source.enabled and source.sync.on_startup:
                mode: SyncMode = "incremental"
                # Unconditional: a graph missing artifacts SQLite already has
                # is never a state anyone wants served, and repair is the
                # cheap fix (re-reads only what is missing, re-embeds nothing).
                if self._graph_gap(source.name):
                    logger.info(
                        "Source %s has indexed artifacts missing from the graph "
                        "(interrupted sync?) — repairing instead of re-indexing",
                        source.name,
                    )
                    mode = "repair"
                results.append(self.sync_source(source.name, mode))
        return results

    def reconcile_embeddings(self) -> dict:
        """Align the vector store with the current embedding config.

        Returns a small report; a no-op transition reports ``embedded: 0`` and
        makes no embedder call, so an unchanged restart costs nothing.
        """

        current = embedding_fingerprint(self.config)
        previous = self.state.get_fingerprint(EMBEDDING_SCOPE)
        change = embedding_change(previous, current)
        report = {"change": change, "embedded": 0, "dropped": False}

        if change in {"none", "disabled"} or self.vectors is None:
            # "disabled" still records the transition, so turning embeddings
            # back on later is recognised as a return to a known space rather
            # than a fresh introduction.
            if change != "none":
                self.state.set_fingerprint(EMBEDDING_SCOPE, current, utc_now())
            return report

        # "invalidated" = a different model/dimension/store: the old vectors
        # are not comparable with new ones and must go. "introduced" = chunks
        # exist but were never embedded; keep whatever is there and fill in.
        drop_existing = change == "invalidated"
        logger.info(
            "Embedding config %s — re-embedding from stored chunks (drop_existing=%s)",
            change,
            drop_existing,
        )
        report["dropped"] = drop_existing
        report["embedded"] = self.rebuild_vectors(drop_existing=drop_existing)
        self.state.set_fingerprint(EMBEDDING_SCOPE, current, utc_now())
        return report

    def rebuild_vectors(self, drop_existing: bool = False) -> int:
        """Embed everything already indexed, without re-reading any source.

        The text is already in SQLite and chunk ids are content-addressed, so
        this is both cheaper than a re-scan and idempotent: a second run makes
        zero embedder calls.
        """

        if self.vectors is None:
            return 0
        if drop_existing:
            # LanceDB's vector width lives in the table schema. Deleting rows
            # leaves that schema behind, so a model dimension change must
            # reset the store itself before the first new vector is inserted.
            self.vectors.reset()
        embedded = 0
        for artifact in self.state.rows("SELECT id, source_id FROM artifacts ORDER BY id"):
            chunks = self.state.rows(
                "SELECT id, text, text_hash FROM chunks WHERE artifact_id=? ORDER BY chunk_index",
                (artifact["id"],),
            )
            if not chunks:
                continue
            embedded += self.vectors.index_artifact(
                str(artifact["source_id"]),
                str(artifact["id"]),
                [dict(chunk) for chunk in chunks],
            )
        self.vectors.flush()
        return embedded

    def _graph_gap(self, source_name: str) -> bool:
        """True when SQLite has artifacts the loaded graph does not know about."""

        graph = self.graph_builder.graph
        for row in self.state.rows(
            "SELECT id FROM artifacts WHERE source_id=? LIMIT 2000", (source_name,)
        ):
            if not graph.has_node(str(row["id"])):
                return True
        return False

    def sync_all(
        self,
        mode: SyncMode = "incremental",
        *,
        max_depth: int | None = None,
        full_scan: bool = False,
        on_progress: ProgressHook | None = None,
    ) -> list[SyncResult]:
        sources = self.enabled_sources()
        if not sources:
            return []
        workers = min(
            len(sources),
            max(1, int(self.config.sync.concurrency.max_parallel_sources or 1)),
        )
        queue = queue_from_config(self.config, self.state)
        if queue is not None:
            try:
                return self._sync_all_queued(
                    queue,
                    sources,
                    mode,
                    workers=workers,
                    max_depth=max_depth,
                    full_scan=full_scan,
                    on_progress=on_progress,
                )
            finally:
                # This call built the queue, so this call closes it. A no-op
                # for the local queue; for a broker it is the difference
                # between one connection per sync and a leak per sync.
                queue.close()
        if workers == 1:
            return [
                self.sync_source(
                    source.name,
                    mode,
                    max_depth=max_depth,
                    full_scan=full_scan,
                    on_progress=on_progress,
                )
                for source in sources
            ]

        # A shared callback may write NDJSON or mutate a job record; serialize
        # calls while still allowing the source pipelines themselves to overlap.
        progress_lock = threading.Lock()

        forward_meta = on_progress is not None and _hook_accepts_meta(on_progress)

        def progress_for(source_name: str) -> ProgressHook:
            def source_progress(
                phase: str,
                current: int,
                total: int | None,
                detail: str,
                meta: dict[str, Any] | None = None,
            ) -> None:
                # Respect the caller's arity. Forwarding five positional
                # arguments unconditionally defeated `_hook_accepts_meta`: a
                # four-argument hook — the documented supported form, and what
                # `sync/worker.py:_as_engine_hook` builds for the in-process
                # path — raised TypeError on every update, which
                # `_progress_reporter` swallows at debug level. The result was
                # a job whose progress bar never moved whenever
                # `max_parallel_sources > 1`, with nothing in the logs.
                if on_progress is None:
                    return
                with progress_lock:
                    if forward_meta:
                        on_progress(phase, current, total, detail, meta)
                    else:
                        on_progress(phase, current, total, detail)

            return source_progress

        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="pheasant-source",
        ) as executor:
            futures = [
                executor.submit(
                    self.sync_source,
                    source.name,
                    mode,
                    max_depth=max_depth,
                    full_scan=full_scan,
                    on_progress=progress_for(source.name),
                )
                for source in sources
            ]
            # Preserve configured source order in the public result even when
            # a later source finishes first.
            return [future.result() for future in futures]

    def _sync_all_queued(
        self,
        queue: Any,
        sources: list[SourceConfig],
        mode: SyncMode,
        *,
        workers: int,
        max_depth: int | None,
        full_scan: bool,
        on_progress: ProgressHook | None,
    ) -> list[SyncResult]:
        """Publish one task per source, then drain.

        The difference from the in-memory path is what survives a kill: a
        process stopped nine sources into ten leaves the tenth *queued*, so
        the next run finishes it instead of starting over. Logical task ids
        are stable for observability. The local queue re-arms a completed
        logical task, while NATS assigns each invocation a distinct broker
        publish id so its duplicate window cannot suppress a real rapid re-run.

        Ordering is by enqueue time, so with one worker the results come back
        in configured source order exactly as before. With several the
        interleaving is the same non-determinism the thread pool already had:
        commit order *within* a source is what stable IDs and deterministic
        graph bytes depend on, and that is untouched (rule 3, rule 4).
        """

        import hashlib

        from pheasant.sync.queue import IndexTask, drain, owner_id

        settings = self.config.sync.queue
        progress_lock = threading.Lock()

        forward_meta = on_progress is not None and _hook_accepts_meta(on_progress)

        def progress_for(source_name: str) -> ProgressHook:
            def source_progress(
                phase: str,
                current: int,
                total: int | None,
                detail: str,
                meta: dict[str, Any] | None = None,
            ) -> None:
                # Respect the caller's arity. Forwarding five positional
                # arguments unconditionally defeated `_hook_accepts_meta`: a
                # four-argument hook — the documented supported form, and what
                # `sync/worker.py:_as_engine_hook` builds for the in-process
                # path — raised TypeError on every update, which
                # `_progress_reporter` swallows at debug level. The result was
                # a job whose progress bar never moved whenever
                # `max_parallel_sources > 1`, with nothing in the logs.
                if on_progress is None:
                    return
                with progress_lock:
                    if forward_meta:
                        on_progress(phase, current, total, detail, meta)
                    else:
                        on_progress(phase, current, total, detail)

            return source_progress

        published = 0
        for source in sources:
            digest = hashlib.sha256(
                f"{self.config.knowledge_base_id}\0{source.name}\0{mode}".encode()
            ).hexdigest()[:24]
            task = IndexTask(
                id=f"idx-{digest}",
                source_id=source.name,
                mode=str(mode),
                payload={"max_depth": max_depth, "full_scan": bool(full_scan)},
                max_attempts=max(1, int(settings.max_attempts or 1)),
            )
            try:
                queue.publish(task)
                published += 1
            except Exception as exc:  # noqa: BLE001 - an existing task is not an error
                logger.debug("Index task for %s already queued (%s)", source.name, exc)
        logger.info("Queued %d of %d source(s) for indexing", published, len(sources))

        def run(task: Any) -> SyncResult:
            return self.sync_source(
                task.source_id,
                task.mode,  # type: ignore[arg-type]
                max_depth=task.max_depth,
                full_scan=task.full_scan,
                on_progress=progress_for(task.source_id),
            )

        visibility = float(settings.visibility_seconds or 300)
        if workers <= 1:
            results = drain(queue, run, visibility_seconds=visibility)
        else:
            # Each drain loop is its own claimant, so two never hold the same
            # task — the visibility timeout is doing the mutual exclusion that
            # the thread pool got for free from having one list.
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="pheasant-source"
            ) as executor:
                futures = [
                    executor.submit(
                        drain, queue, run, owner=owner_id(), visibility_seconds=visibility
                    )
                    for _ in range(workers)
                ]
                results = [item for future in futures for item in future.result()]
        order = {source.name: position for position, source in enumerate(sources)}
        return sorted(results, key=lambda result: order.get(result.source_id, len(order)))

    def _prepare_item(
        self,
        connector: Any,
        source: SourceConfig,
        mode: SyncMode,
        artifacts: dict[str, Any],
        git_metadata: tuple[str | None, str | None, bool] | None,
        position: int,
        item: ConnectorItem,
    ) -> _PreparedItem:
        """Read and parse one item without mutating authoritative state."""

        previous = artifacts.get(item.relative_path)
        # Before the sha256 skip and before any bytes are read: a denylisted
        # path is not content this region declines to *re*-index, it is content
        # this region must never hold. Read live rather than cached, so adding
        # a pattern applies without a restart. See `security/corpus_policy.py`.
        denylist = corpus_policy.denylist_of(self.config)
        refused = corpus_policy.denied_by(item.relative_path, denylist)
        if refused:
            return _PreparedItem(
                position,
                item,
                previous,
                skipped=True,
                transfer_skipped=True,
                refused_by=refused,
            )
        if self._can_skip_before_read(mode, previous, item):
            return _PreparedItem(
                position,
                item,
                previous,
                skipped=True,
                transfer_skipped=True,
            )
        try:
            payload = connector.read_item(item)
        except ItemNotModified:
            return _PreparedItem(
                position,
                item,
                previous,
                skipped=True,
                transfer_skipped=True,
            )
        content_hash = payload.sha256 or item.sha256 or sha256_bytes(payload.content)
        if mode == "incremental" and previous and previous.get("sha256") == content_hash:
            return _PreparedItem(
                position,
                item,
                previous,
                fetched=True,
                skipped=True,
            )
        if payload.sha256 != content_hash:
            payload = replace(payload, sha256=content_hash)
        parsed = parse_connector_payload(
            source,
            item,
            payload,
            git_metadata,
            captioner=self.captioner,
            transcriber=self.transcriber,
            extractor=self.extractor,
        )
        if parsed is None:
            return _PreparedItem(position, item, previous, fetched=True)
        if mode == "incremental" and previous and previous.get("sha256") == parsed.sha256:
            return _PreparedItem(
                position,
                item,
                previous,
                parsed=parsed,
                fetched=True,
                skipped=True,
            )
        if mode == "repair" and not self._needs_repair(
            source.name,
            parsed.id,
            parsed.sha256,
            previous,
            len(parsed.chunks),
        ):
            return _PreparedItem(
                position,
                item,
                previous,
                parsed=parsed,
                fetched=True,
                skipped=True,
            )
        return _PreparedItem(
            position,
            item,
            previous,
            parsed=parsed,
            fetched=True,
        )

    def _remote_precheck(
        self,
        connector: Any,
        source: SourceConfig,
        mode: SyncMode,
        artifacts: dict[str, Any],
        position: int,
        item: ConnectorItem,
    ) -> tuple[_PreparedItem | None, Any]:
        """Everything that must happen locally before a task can be dispatched.

        Returns ``(finished_item, payload)``: exactly one is not ``None``. The
        skips are decided coordinator-side on purpose — an unchanged file
        never becomes a request at all, which is what keeps a re-sync of a
        large source close to zero remote work.
        """

        previous = artifacts.get(item.relative_path)
        if self._can_skip_before_read(mode, previous, item):
            return _PreparedItem(
                position, item, previous, skipped=True, transfer_skipped=True
            ), None
        try:
            payload = connector.read_item(item)
        except ItemNotModified:
            return _PreparedItem(
                position, item, previous, skipped=True, transfer_skipped=True
            ), None
        content_hash = payload.sha256 or item.sha256 or sha256_bytes(payload.content)
        if mode == "incremental" and previous and previous.get("sha256") == content_hash:
            return _PreparedItem(position, item, previous, fetched=True, skipped=True), None
        if payload.sha256 != content_hash:
            payload = replace(payload, sha256=content_hash)
        return None, payload

    def _finish_remote(
        self,
        source: SourceConfig,
        mode: SyncMode,
        artifacts: dict[str, Any],
        position: int,
        item: ConnectorItem,
        payload: Any,
        parsed: Any,
    ) -> _PreparedItem:
        """Validate a worker's answer and turn it into a prepared item.

        The two identity checks are not defensive padding: a worker is another
        process reached over the network, and an answer for the wrong file
        would be committed under this item's stable ID. Content-addressing the
        request is what makes them checkable at all.
        """

        previous = artifacts.get(item.relative_path)
        if parsed is None:
            return _PreparedItem(position, item, previous, fetched=True)
        if parsed.source_id != source.name or parsed.relative_path != item.relative_path:
            raise ValueError(
                "Remote worker returned an artifact for a different source/item: "
                f"{parsed.source_id}/{parsed.relative_path}"
            )
        expected_hash = payload.sha256 or item.sha256
        if expected_hash is not None and parsed.sha256 != expected_hash:
            raise ValueError(
                f"Remote worker returned the wrong content hash for {item.relative_path}"
            )
        if mode == "incremental" and previous and previous.get("sha256") == parsed.sha256:
            return _PreparedItem(
                position, item, previous, parsed=parsed, fetched=True, skipped=True
            )
        return _PreparedItem(position, item, previous, parsed=parsed, fetched=True)

    def _prepare_batch_remote(
        self,
        pool: Any,
        connector: Any,
        source: SourceConfig,
        mode: SyncMode,
        artifacts: dict[str, Any],
        git_metadata: tuple[str | None, str | None, bool] | None,
        batch: list[tuple[int, ConnectorItem]],
    ) -> list[_PreparedItem]:
        """Prepare one batch remotely, falling back to local preparation.

        Remote preparation is a throughput optimization, so it must never be
        able to fail a sync. When the fleet cannot answer — every endpoint
        down, a rolling deploy, a deadline — the bytes are already in hand and
        parsing them here costs one CPU-bound parse, which is exactly what the
        non-remote executors do all the time. The same policy Phase 34.5
        applied to the WASM accelerators.
        """

        from pheasant.sync.remote_worker import task_payload
        from pheasant.sync.worker_pool import AllWorkersFailed, TaskRejected

        finished: dict[int, _PreparedItem] = {}
        pending: list[tuple[int, ConnectorItem, Any]] = []
        for position, item in batch:
            done, payload = self._remote_precheck(
                connector, source, mode, artifacts, position, item
            )
            if done is not None:
                finished[position] = done
            else:
                pending.append((position, item, payload))

        if pending:
            tasks = [
                task_payload(
                    source,
                    item,
                    payload,
                    git_metadata,
                    self.config.ingestion.extractor,
                )
                for _position, item, payload in pending
            ]
            timeout = float(self.config.sync.concurrency.remote_worker_timeout_seconds or 120)
            try:
                parsed_results = pool.prepare_batch(tasks, deadline=time.monotonic() + timeout)
            except (AllWorkersFailed, TaskRejected) as exc:
                logger.warning(
                    "Preparing %d file(s) of source %s locally: %s",
                    len(pending),
                    source.name,
                    exc,
                )
                parsed_results = [
                    parse_connector_payload(
                        source,
                        item,
                        payload,
                        git_metadata,
                        captioner=self.captioner,
                        transcriber=self.transcriber,
                        extractor=self.extractor,
                    )
                    for _position, item, payload in pending
                ]
            for (position, item, payload), parsed in zip(pending, parsed_results, strict=True):
                finished[position] = self._finish_remote(
                    source, mode, artifacts, position, item, payload, parsed
                )

        return [finished[position] for position, _item in batch]

    def _prepared_items(
        self,
        connector: Any,
        source: SourceConfig,
        mode: SyncMode,
        artifacts: dict[str, Any],
        git_metadata: tuple[str | None, str | None, bool] | None,
        items: list[ConnectorItem],
    ):
        """Yield prepared items in discovery order with bounded look-ahead.

        Workers never touch manifests, SQLite, vectors, or the graph. Keeping
        at most two work windows in flight avoids turning a 50k-file source
        into a second in-memory copy of the repository.
        """

        if not items:
            return
        workers = min(
            len(items),
            max(1, int(self.config.sync.concurrency.max_parallel_files or 1)),
        )
        requested_executor = str(self.config.sync.concurrency.file_executor or "thread").lower()
        if workers <= 1 and requested_executor == "thread":
            for position, item in enumerate(items, start=1):
                yield self._prepare_item(
                    connector,
                    source,
                    mode,
                    artifacts,
                    git_metadata,
                    position,
                    item,
                )
            return

        plain_text_source = (
            connector.connector_type == "filesystem"
            and mode != "repair"
            and not bool(getattr(source.taxonomy, "enabled", False))
            and all(_process_safe_text_path(item.relative_path) for item in items)
        )
        process_safe = requested_executor == "process" and plain_text_source
        remote_urls = list(self.config.sync.concurrency.remote_worker_urls or [])
        if requested_executor == "remote":
            from pheasant.sync.remote_worker import _remote_preparation_path

            remote_content_safe = (
                connector.connector_type == "filesystem"
                and mode != "repair"
                and not bool(getattr(source.taxonomy, "enabled", False))
                and all(
                    _remote_preparation_path(
                        item.relative_path,
                        html_text=bool(self.config.ingestion.extractor.html_text),
                    )
                    for item in items
                )
            )
        else:
            remote_content_safe = False
        remote_safe = requested_executor == "remote" and remote_content_safe and bool(remote_urls)
        remote_token = ""
        if remote_safe:
            from pheasant.sync.remote_worker import configured_token

            remote_token = configured_token(self.config.sync.concurrency.remote_worker_token_env)
        if requested_executor not in {"thread", "process", "remote"}:
            logger.warning(
                "Unknown sync.concurrency.file_executor=%r; using thread workers",
                requested_executor,
            )
        elif requested_executor == "process" and not process_safe:
            logger.info(
                "Source %s needs connector/modal/repair state; using thread workers",
                source.name,
            )
        elif requested_executor == "remote" and not remote_safe:
            logger.warning(
                "Source %s cannot use remote workers (requires URLs and supported text/document "
                "content without taxonomy/repair); using thread workers",
                source.name,
            )
        if remote_safe:
            yield from self._prepared_items_remote(
                connector,
                source,
                mode,
                artifacts,
                git_metadata,
                items,
                remote_urls,
                remote_token,
                workers,
            )
            return

        executor_type = ProcessPoolExecutor if process_safe else ThreadPoolExecutor
        if process_safe:
            workers = min(workers, _process_cpu_capacity())

        with executor_type(
            max_workers=workers,
            **({} if process_safe else {"thread_name_prefix": f"pheasant-file-{source.name}"}),
        ) as executor:
            pending: deque[Future[_PreparedItem]] = deque()
            iterator = iter(enumerate(items, start=1))

            def submit(position: int, item: ConnectorItem) -> Future[_PreparedItem]:
                if process_safe:
                    return executor.submit(
                        _prepare_filesystem_item_process,
                        source,
                        mode,
                        git_metadata,
                        position,
                        item,
                        artifacts.get(item.relative_path),
                    )
                return executor.submit(
                    self._prepare_item,
                    connector,
                    source,
                    mode,
                    artifacts,
                    git_metadata,
                    position,
                    item,
                )

            for _ in range(min(len(items), workers * 2)):
                position, item = next(iterator)
                pending.append(submit(position, item))
            while pending:
                yield pending.popleft().result()
                try:
                    position, item = next(iterator)
                except StopIteration:
                    continue
                pending.append(submit(position, item))

    def _prepared_items_remote(
        self,
        connector: Any,
        source: SourceConfig,
        mode: SyncMode,
        artifacts: dict[str, Any],
        git_metadata: tuple[str | None, str | None, bool] | None,
        items: list[ConnectorItem],
        remote_urls: list[str],
        remote_token: str,
        workers: int,
    ):
        """Yield prepared items in discovery order, prepared by a worker fleet.

        A separate generator from the thread/process path rather than another
        branch inside it, because the unit of work is different: a *batch*, so
        that a request carries several files, one deadline and one connection.
        Discovery order is preserved exactly as before — batches are contiguous
        runs of positions and the look-ahead window is FIFO — so committing
        stays deterministic and stable IDs are unaffected (rule 3).
        """

        from pheasant.sync.worker_pool import WorkerPool

        concurrency = self.config.sync.concurrency
        batch_size = max(1, int(concurrency.remote_worker_batch_size or 1))
        # Enumerated once, then sliced. Building the full `list(enumerate(...))`
        # inside the comprehension rebuilt it per batch — O(files²/batch_size)
        # allocations, which on a 250k-file source is millions of tuples for a
        # list that never changes. Identical output, same discovery order.
        numbered = list(enumerate(items, start=1))
        # A count-only batch is unsafe for documents: sixteen 25 MiB PDFs
        # become >500 MiB once the coordinator and worker each hold bytes plus
        # base64/JSON copies.  Keep ordinary source-code batching fast, while
        # splitting large payloads before they cross the process boundary.
        batch_byte_budget = 16 * 1024 * 1024
        batches: list[list[tuple[int, ConnectorItem]]] = []
        current_batch: list[tuple[int, ConnectorItem]] = []
        current_bytes = 0
        for numbered_item in numbered:
            item = numbered_item[1]
            estimated_bytes = max(1, int(item.size_bytes or 0))
            is_document = Path(item.relative_path).suffix.lower() in DOCUMENT_EXTENSIONS
            would_overflow = current_batch and current_bytes + estimated_bytes > batch_byte_budget
            document_boundary = current_batch and is_document
            if len(current_batch) >= batch_size or would_overflow or document_boundary:
                batches.append(current_batch)
                current_batch = []
                current_bytes = 0
            current_batch.append(numbered_item)
            current_bytes += estimated_bytes
            # A document is its own task envelope.  This bounds duplicate byte
            # copies and lets another replica start work immediately.
            if is_document:
                batches.append(current_batch)
                current_batch = []
                current_bytes = 0
        if current_batch:
            batches.append(current_batch)
        pool = WorkerPool(
            remote_urls,
            remote_token,
            timeout=float(concurrency.remote_worker_timeout_seconds or 120),
            transport_name=str(concurrency.worker_transport or "http"),
        )
        try:
            pool.publish_health()
            remote_workers = max(1, min(workers, len(batches), len(remote_urls) * 2))
            with ThreadPoolExecutor(
                max_workers=remote_workers,
                thread_name_prefix=f"pheasant-remote-{source.name}",
            ) as executor:
                pending: deque[Future[list[_PreparedItem]]] = deque()
                iterator = iter(batches)

                def submit(batch: list[tuple[int, ConnectorItem]]) -> Future[list[_PreparedItem]]:
                    return executor.submit(
                        self._prepare_batch_remote,
                        pool,
                        connector,
                        source,
                        mode,
                        artifacts,
                        git_metadata,
                        batch,
                    )

                for _ in range(min(len(batches), remote_workers * 2)):
                    try:
                        pending.append(submit(next(iterator)))
                    except StopIteration:
                        break
                while pending:
                    yield from pending.popleft().result()
                    try:
                        batch = next(iterator)
                    except StopIteration:
                        continue
                    pending.append(submit(batch))
        finally:
            # Health is published again on the way out so the gauge reflects
            # what this sync actually observed, not what it assumed at the
            # start — a breaker that tripped mid-run is the interesting case.
            pool.publish_health()
            for entry in pool.stats():
                if entry["failures"]:
                    logger.info(
                        "Remote worker %s: %d ok, %d failed%s",
                        entry["endpoint"],
                        entry["successes"],
                        entry["failures"],
                        " (circuit open)" if entry["circuit_open"] else "",
                    )
            pool.close()

    def _ensure_modal_handlers(self, source: Any) -> None:
        """Build the caption/transcribe/extract handlers this source needs.

        The three are built once in ``__init__`` from whether *any* configured
        source admits their extensions. That misses the source registered at
        **runtime** — `POST /sources`, `POST /sources/quick-add`, the UI's
        add-source flow — which is how most sources actually arrive: the engine
        was constructed before that source existed, so no handler was built,
        and its documents were indexed with **no text at all**. Exactly the
        silent-empty-content failure document extraction exists to fix, on the
        path most people use.

        Topping the handlers up here, where the source is known, also makes the
        test more precise than the constructor's: it asks whether *this* source
        needs a handler rather than whether any source does. Idempotent, and it
        never discards a handler that already exists — a second sync of an
        unchanged source does no work here.
        """
        if self.extractor is None and source_includes_documents(source):
            self.extractor = extractor_from_config(self.config, source=source)
        if self.captioner is None and source_includes_images(source):
            self.captioner = captioner_from_config(self.config, source=source)
        if self.transcriber is None and source_includes_audio(source):
            self.transcriber = transcriber_from_config(self.config, source=source)

    @staticmethod
    def _memory_acl(path: Any) -> dict[str, Any] | None:
        """`{scope, written_by}` read from the record file (Step 33.11).

        Read from the file rather than the `memory_records` projection because
        this runs *inside* the artifact loop, before the projection for this
        sync exists. Fail-soft: an unreadable record simply expresses no ACL
        and falls back to the region default, which is what it did before 33.11.
        """
        try:
            from pheasant.memory.store import MemoryStore

            record = MemoryStore(Path(path).parent).load(Path(path))
        except Exception:
            return None
        return {"scope": record.scope, "written_by": record.written_by}

    def _bridge_memory(self) -> None:
        """Emit memory `supersedes` + `about` edges (Step 33.7).

        Runs on the global-enrichment beat rather than per source, because a
        record can be about content that lives in a *different* source and the
        bridge can only see it once that source is indexed too.

        Fail-soft, matching every other global pass here: a bridge that cannot
        be derived costs some edges, never the sync that produced the content.
        """
        if not self.config.graph.memory_entity_bridging:
            return
        try:
            report = self.graph_builder.add_memory_edges(
                self.state, self.config.memory.about_max_targets
            )
        except Exception:
            logger.warning("memory graph bridge failed; edges not updated", exc_info=True)
            return
        if report.get("records"):
            logger.info(
                "Memory bridged: %s records, %s about edges %s, %s supersedes, %s unbridged",
                report["records"],
                report["about"],
                report.get("by_signal") or {},
                report["supersedes"],
                len(report.get("unbridged") or []),
            )

    def _project_memory_records(self, source: Any) -> None:
        """Rebuild ``memory_records`` for a ``type: memory`` source (Step 33.5).

        A no-op for every other source type, so a region without agent memory
        is untouched. Rebuilding wholesale — rather than diffing — is what
        keeps the table a *projection*: it cannot drift from the files, an
        archived record disappears without needing its own delete path, and a
        second sync over unchanged records writes identical rows.

        Fail-soft, the same posture document extraction takes: one unreadable
        record must not abort a sync that indexed everything else. The rows
        from the previous pass survive (the write is a single transaction that
        never runs), and the warning says so.
        """
        if getattr(source.type, "value", source.type) != "memory":
            return
        try:
            from pheasant.memory.projection import project
            from pheasant.memory.store import MemoryStore

            records = MemoryStore(source.path).list_records()
            self.state.replace_memory_records(source.name, project(source.name, records))
        except Exception:
            logger.warning(
                "memory projection failed for %s; keeping the previous rows",
                source.name,
                exc_info=True,
            )

    #: Scope key for the `sync_fingerprints` KV table (Phase 0). Presence of a
    #: row means "an `enrich=deferred` write landed here and the whole-graph
    #: enrichment block has not run since." Reused rather than adding a table:
    #: the same scope->value KV already backs config-change detection, and a
    #: dirty flag is exactly that shape — one value, read-then-clear, no
    #: history. Persisted (not in-process) so it survives a restart and is
    #: visible across the indexer/API/worker role split.
    _ENRICH_DIRTY_SCOPE = "memory-enrich-dirty:{name}"

    def _finalize_index_state(
        self,
        *,
        source: SourceConfig,
        connector: Any,
        mode: SyncMode,
        manifest: dict[str, Any],
        indexed: int,
        embedded_chunks: int,
        changed_ids: set[str],
        total_items: int,
        report: Callable[[str, int, int | None, str], None],
        embedding_progress: Callable[[int], None],
        enrich: str = "now",
    ) -> int:
        """Finalize one source while holding the coordinated writer mutex."""

        with self._sync_mutex:
            pruned_vectors = 0
            if self.vectors is not None:
                live_chunk_ids = {
                    str(row["id"])
                    for row in self.state.rows(
                        "SELECT id FROM chunks WHERE source_id=?", (source.name,)
                    )
                }
                pruned_vectors = self.vectors.prune_source(source.name, live_chunk_ids)

            self._project_memory_records(source)
            dirty_scope = self._ENRICH_DIRTY_SCOPE.format(name=source.name)
            if enrich == "deferred":
                # The whole-graph walk (similarity + cross-source + memory
                # bridge) never runs on a deferred call, regardless of any
                # prior dirty state — that is the entire point of deferring.
                # Only mark dirty when this call actually indexed something;
                # a no-op memory write (an exact-duplicate `created=False`)
                # has nothing new for enrichment to pick up.
                if indexed:
                    self.state.set_fingerprint(dirty_scope, "1", utc_now())
            else:
                pending_enrich = bool(self.state.get_fingerprint(dirty_scope))
                if indexed or mode == "full" or pending_enrich:
                    report("enriching", total_items, total_items, "linking the graph")
                    self.graph_builder.add_similarity_edges(
                        source.name,
                        changed_ids=None if mode == "full" else changed_ids,
                    )
                    self.graph_builder.add_cross_source_edges()
                    self._bridge_memory()
                    if pending_enrich:
                        self.state.clear_fingerprint(dirty_scope)

            if self.vectors is not None:
                # Finish provider work before advertising a saved, completed
                # source. Network batches run concurrently inside this call;
                # the vector-store upsert itself remains ordered.
                self.vectors.flush(on_progress=embedding_progress)
                report(
                    "embedding",
                    embedded_chunks,
                    embedded_chunks,
                    f"embedded {embedded_chunks} changed chunk(s)",
                )
            graph_changed = bool(indexed or mode == "full")
            report(
                "saving",
                total_items,
                total_items,
                "writing the graph and index" if graph_changed else "recording source checkpoint",
            )
            manifest["last_indexed_at"] = utc_now()
            manifest["connector"] = {"type": connector.connector_type}
            if graph_changed:
                self.flush_node_index()
                self.graph_store.save(self.config.knowledge_base_id, self.graph_builder.graph)
                self._graph_dirty = False
                self._last_checkpoint = time.monotonic()
            else:
                logger.debug("Skipping unchanged graph save for source %s", source.name)
            # See checkpoint_graph: manifests may lag a published generation,
            # but must never claim a graph generation that did not commit.
            self.manifests.save(source.name, manifest)
            return pruned_vectors

    def apply_index_task(self, task: Any, *, on_progress: ProgressHook | None = None) -> SyncResult:
        """Apply one durable data-plane or control-plane queue task.

        Fleet API replicas mount graph state read-only and intentionally never
        become writers.  Source replacement/removal therefore travels over
        the same ordered, retryable queue as indexing and is committed by the
        isolated single-writer child.
        """

        payload = getattr(task, "payload", None) or {}
        # Run the whole task inside the trace of the request that queued it.
        # `POST /sync` is a traced request; the indexer that claims the task is
        # a different process, possibly minutes later, and without this the
        # chain breaks at the queue -- one thing a person asked for shows up as
        # two unrelated traces. Adopting it here also means every hop the
        # indexer makes onward (remote preparation, the graph service) carries
        # it, because those inject from the ambient trace. A no-op when the
        # task carries none, which is every task a scheduler enqueued.
        from pheasant.telemetry.interactions import adopt_trace

        with adopt_trace(payload.get("traceparent")):
            return self._apply_index_task(task, on_progress=on_progress)

    def _apply_index_task(
        self, task: Any, *, on_progress: ProgressHook | None = None
    ) -> SyncResult:
        operation = str((getattr(task, "payload", None) or {}).get("operation") or "")
        if operation == "delete_source":
            return self.remove_source(str(task.source_id), on_progress=on_progress)
        if operation == "replace_source":
            payload = (getattr(task, "payload", None) or {}).get("source")
            if not isinstance(payload, dict):
                raise ValueError("replace_source task is missing its source configuration")
            return self.replace_source(payload, on_progress=on_progress)
        requested_mode = str(task.mode)
        resume_full = bool(
            requested_mode == "full"
            and int(getattr(task, "attempts", 0) or 0) > 1
            and self.state.get_fingerprint(FULL_RESUME_SCOPE.format(name=task.source_id))
            == "checkpointed"
        )
        if resume_full:
            logger.info(
                "Resuming interrupted full sync for %s as a content-addressed delta pass",
                task.source_id,
            )
        result = self.sync_source(
            str(task.source_id),
            "incremental" if resume_full else requested_mode,
            max_depth=task.max_depth,
            full_scan=task.full_scan,
            on_progress=on_progress,
            _resume_interrupted_full=resume_full,
        )
        if resume_full:
            self.state.execute(
                "DELETE FROM sync_fingerprints WHERE scope=?",
                (FULL_RESUME_SCOPE.format(name=task.source_id),),
            )
            return replace(
                result,
                details={
                    **result.details,
                    "requested_mode": "full",
                    "resumed_interrupted_full": True,
                },
            )
        return result

    def remove_source(
        self, source_name: str, *, on_progress: ProgressHook | None = None
    ) -> SyncResult:
        """Remove one source through the graph's single-writer publication path."""

        report = _progress_reporter(on_progress, source_name)
        report("claimed", 0, 1, f"removing {source_name}")
        with self._source_write_lock(source_name):
            with self._sync_mutex:
                self._ensure_persisted_graph_loaded()
                self.graph_builder.remove_source_content(source_name)
                self.flush_node_index()
                # Publish graph removal before deleting the registry entry. If
                # killed here, redelivery is idempotent and completes cleanup.
                self.graph_store.save(
                    self.config.knowledge_base_id,
                    self.graph_builder.graph,
                )
                self.manifests.delete(source_name)
                self.state.delete_source(source_name)
                self.config.sources = [s for s in self.config.sources if s.name != source_name]
        nodes, edges = self._graph_counts()
        report("saving", 1, 1, f"removed {source_name}")
        return SyncResult(source_name, 0, 0, nodes, edges, "removed")

    def replace_source(
        self,
        source_payload: dict[str, Any],
        *,
        on_progress: ProgressHook | None = None,
    ) -> SyncResult:
        """Atomically clear old indexed content and install a runtime source config."""

        updated = PheasantConfig.model_validate({"sources": [source_payload]}).sources[0]
        report = _progress_reporter(on_progress, updated.name)
        report("claimed", 0, 1, f"updating {updated.name}")
        with self._source_write_lock(updated.name):
            with self._sync_mutex:
                self._ensure_persisted_graph_loaded()
                self.graph_builder.remove_source_content(updated.name)
                self.flush_node_index()
                self.graph_store.save(
                    self.config.knowledge_base_id,
                    self.graph_builder.graph,
                )
                self.manifests.delete(updated.name)
                self.state.delete_source_artifacts(updated.name)
                SourceRegistry(self.config, self.state).register_source(updated)
                self.config.sources = [s for s in self.config.sources if s.name != updated.name]
                self.config.sources.append(updated)
        nodes, edges = self._graph_counts()
        report("saving", 1, 1, f"updated {updated.name}")
        return SyncResult(updated.name, 0, 0, nodes, edges, "updated")

    def sync_source(
        self,
        source_name: str,
        mode: SyncMode = "incremental",
        *,
        max_depth: int | None = None,
        full_scan: bool = False,
        on_progress: ProgressHook | None = None,
        _resume_interrupted_full: bool = False,
        enrich: str = "now",
    ) -> SyncResult:
        """Sync one source.

        ``max_depth`` caps directory depth for this run only, and
        ``full_scan`` lifts both the depth cap and the size budget — the
        explicit "yes, index all of it" switch. Neither is persisted to the
        source config, so a one-off wide sync cannot silently become the
        standing behavior of a scheduled one.

        ``on_progress`` is called as the pass advances so a caller can show
        real progress rather than a spinner. It is best-effort in the strongest
        sense: it rides the per-artifact yield point that already exists, and
        an exception raised inside it can never fail the sync (see
        :func:`_report_progress`).

        ``enrich="deferred"`` (used by memory writes) skips the whole-graph
        enrichment block in :meth:`_finalize_index_state` — similarity edges,
        cross-source edges, memory bridging — and marks the source dirty
        instead. The next ``enrich="now"`` pass over the source (the
        scheduler beat, a watcher-triggered sync, or an explicit `full`
        sync — every caller but the memory write path uses the "now"
        default) runs enrichment and clears the flag, even if that pass
        itself finds nothing new to index. Never used for `full` syncs: a
        full pass always enriches.
        """
        report = _progress_reporter(on_progress, source_name)
        if mode not in SYNC_MODES:
            raise ValueError(f"Unsupported sync mode: {mode}")
        source = self.config.effective_source(
            self._source(source_name), max_depth=max_depth, full_scan=full_scan
        )
        self._ensure_modal_handlers(source)
        # A container restart must never re-index by itself, but a config edit
        # that changes what the content pipeline produces must. Compare what
        # this source was last indexed with against what it is configured with
        # now; only a real difference escalates to a full pass.
        fingerprint = source_fingerprint(source)
        previous_fingerprint = self.state.get_fingerprint(SOURCE_SCOPE.format(name=source.name))
        config_changed = previous_fingerprint is not None and previous_fingerprint != fingerprint
        if config_changed and mode in {"incremental", "repair"} and not _resume_interrupted_full:
            logger.info(
                "Source %s config changed since it was indexed (globs/chunking) — "
                "escalating %s sync to full",
                source.name,
                mode,
            )
            mode = "full"
        # Test-only hook (Synapse 21.2 crash-safety tests): widens the window
        # between per-artifact writes so a kill -9 can land mid-sync.
        slow_sync_s = float(os.environ.get("PHEASANT_TEST_SLOW_SYNC_MS", "0") or 0) / 1000.0
        # The commit-authority meter opens here rather than at the top of
        # the method: everything above is argument resolution, and a
        # saturation gauge that counted it would report a busy region for
        # work that never touched the commit stream.
        with self.commit_meter.busy(), self._source_write_lock(source.name):
            remote_state: dict[str, Any] | None = None
            if source.type.value == "repository":
                # URL quick-add repositories are managed checkouts. Updating
                # them happens under the same per-source lock as indexing, so
                # the commit cannot change between fetch and the artifact
                # pass. Local-path repositories have no clone metadata and
                # remain byte-for-byte on the old path.
                from pheasant.targets import TargetError, refresh_managed_repository

                report("fetching", 0, None, f"checking the remote for {source.name}")
                try:
                    remote_state = refresh_managed_repository(source)
                except TargetError as exc:
                    self.state.mark_source_status(source.name, "remote_error")
                    logger.error("Remote update failed for %s: %s", source.name, exc)
                    raise
            connector = connector_for_source(source, self.state)
            if mode == "validate_only":
                health = connector.validate()
                status = "validated" if health.ok else health.status
                self.state.mark_source_status(source.name, status)
                graph_nodes, graph_edges = self._graph_counts()
                return SyncResult(
                    source.name,
                    0,
                    0,
                    graph_nodes,
                    graph_edges,
                    status,
                    {
                        "connector_type": connector.connector_type,
                        "item_count": health.item_count,
                        "checked_items": health.checked_items,
                        "errors": health.errors,
                        "checkpoint": health.checkpoint,
                        **({"repository": remote_state} if remote_state else {}),
                    },
                )
            started_at = utc_now()
            details_extra: dict = {"repository": remote_state} if remote_state is not None else {}
            manifest = self.manifests.load(source.name)
            artifacts = manifest.setdefault("artifacts", {})
            # Synapse 21.3: thread the previous checkpoint into the connector
            # so non-filesystem sources can skip unchanged remote items.
            connector.begin_sync(mode)
            # A filesystem source whose root is absent (typically: the path is
            # not mounted into this container) would otherwise index 0 files
            # silently and report success. Surface it as a first-class status so
            # the operator sees *why* nothing was indexed instead of a clean
            # zero. (validate() only runs in validate_only mode.)
            if source.type in FILESYSTEM_SOURCE_TYPES and not source.path.exists():
                self.state.mark_source_status(source.name, "path_missing")
                graph_nodes, graph_edges = self._graph_counts()
                return SyncResult(
                    source.name,
                    0,
                    0,
                    graph_nodes,
                    graph_edges,
                    "path_missing",
                    {
                        "connector_type": connector.connector_type,
                        "error": (
                            f"source path does not exist in this environment: "
                            f"{source.path}. If pheasant runs in a container, "
                            f"ensure this path is mounted into the container."
                        ),
                    },
                )
            report("listing", 0, None, f"listing {source.name}")
            try:
                items = connector.list_items()
            except SyncBudgetExceeded as exc:
                # The source is bigger than its budget. Refuse the whole sync
                # rather than indexing a prefix of it: a partial index is
                # non-deterministic, and the operator needs to make a choice
                # (narrow it, or ask for it explicitly with full_scan).
                self.state.mark_source_status(source.name, "limit_exceeded")
                graph_nodes, graph_edges = self._graph_counts()
                return SyncResult(
                    source.name,
                    0,
                    0,
                    graph_nodes,
                    graph_edges,
                    "limit_exceeded",
                    {
                        "connector_type": connector.connector_type,
                        "error": str(exc),
                        "scan": exc.report.as_dict(),
                    },
                )
            # A full sync empties the source here, so every artifact written
            # below is known to be absent. That matters more than it sounds:
            # `chunks_fts.artifact_id` is UNINDEXED, so the per-artifact
            # DELETE that would otherwise run is a full scan of a table
            # growing with the corpus, once per file — a measured O(N²) (Phase
            # 35.7: 500 → 8,000 files went 1.9s → 164.7s). Telling the writer
            # the rows cannot exist skips the question entirely.
            cleared = mode == "full"
            if cleared:
                with self._sync_mutex:
                    resume_scope = FULL_RESUME_SCOPE.format(name=source.name)
                    self.state.set_fingerprint(resume_scope, "starting", utc_now())
                    self._ensure_persisted_graph_loaded()
                    self.state.delete_source_artifacts(source.name)
                    self.graph_builder.remove_source_content(source.name)
                    self.state.set_fingerprint(resume_scope, "cleared", utc_now())
                manifest = {"source_id": source.name, "artifacts": {}}
                artifacts = manifest["artifacts"]
            indexed = 0
            skipped = 0
            fetched = 0
            transfer_skipped = 0
            refused: list[tuple[str, str]] = []
            embedded_chunks = 0
            indexed_bytes = 0
            changed_ids: set[str] = set()
            git_metadata = git_state(source.path) if source.type.value == "repository" else None
            if remote_state is not None:
                indexed_commit = git_metadata[1] if git_metadata else None
                remote_state["indexed_commit"] = indexed_commit
                remote_state["fresh"] = bool(
                    indexed_commit
                    and indexed_commit == remote_state.get("local_commit")
                    and indexed_commit == remote_state.get("remote_commit")
                )
            # The denominator only exists now — listing is what produced it.
            total_items = len(items)
            report("preparing", 0, total_items, f"queued {total_items} item(s)")
            embedding_completed = 0

            def embedding_progress(completed: int) -> None:
                nonlocal embedding_completed
                embedding_completed += completed
                report(
                    "embedding",
                    embedding_completed,
                    None,
                    f"embedded {embedding_completed} changed chunk(s)",
                )

            prepared_items = self._prepared_items(
                connector,
                source,
                mode,
                dict(artifacts),
                git_metadata,
                items,
            )
            for prepared in prepared_items:
                position = prepared.position
                item = prepared.item
                fetched += int(prepared.fetched)
                skipped += int(prepared.skipped)
                transfer_skipped += int(prepared.transfer_skipped)
                if prepared.refused_by:
                    # Never silent: a control whose only evidence is an absence
                    # cannot be told from a typo in the pattern.
                    refused.append((item.relative_path, prepared.refused_by))
                    logger.warning(
                        "%s: refused %s — matches readiness.corpus_denylist pattern %r",
                        source.name,
                        item.relative_path,
                        prepared.refused_by,
                    )
                report(
                    "preparing",
                    position,
                    total_items,
                    item.relative_path,
                    indexed=indexed,
                    skipped=skipped,
                    bytes_done=indexed_bytes,
                )
                parsed = prepared.parsed
                if prepared.skipped or parsed is None:
                    continue
                now = utc_now()
                from pheasant.security.acl import normalize_acl

                acl_raw = (item.metadata or {}).get("acl")
                acl_kind = connector.connector_type
                if getattr(source.type, "value", source.type) == "memory":
                    # Step 33.11 — a memory record is served by the ordinary
                    # filesystem connector, so its connector type says nothing
                    # about who may read it. Its *scope* does, and that lives
                    # in the record, not in the file's metadata.
                    acl_kind = "memory"
                    acl_raw = self._memory_acl(parsed.path)
                acl_doc = normalize_acl(acl_kind, acl_raw)
                artifact_row = {
                    "acl": json.dumps(acl_doc, sort_keys=True) if acl_doc else None,
                    "id": parsed.id,
                    "source_id": parsed.source_id,
                    "type": parsed.type,
                    "path": str(parsed.path),
                    "relative_path": parsed.relative_path,
                    "mime_type": parsed.mime_type,
                    "size_bytes": parsed.size_bytes,
                    "sha256": parsed.sha256,
                    "mtime": parsed.mtime,
                    "git_branch": parsed.git_branch,
                    "git_commit": parsed.git_commit,
                    "last_indexed_at": now,
                    "status": "indexed",
                }
                chunk_rows = [
                    {
                        "id": (
                            f"chunk:{source.name}:{parsed.relative_path}:"
                            f"sha256={chunk.text_hash}:chunk={chunk.index:04d}"
                        ),
                        "artifact_id": parsed.id,
                        "source_id": source.name,
                        "chunk_index": chunk.index,
                        "heading_path": chunk.heading_path,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "text": chunk.text,
                        "text_hash": chunk.text_hash,
                        "summary": chunk.text[:240],
                        "token_estimate": chunk.token_estimate,
                    }
                    for chunk in parsed.chunks
                ]
                with self._sync_mutex:
                    self._ensure_persisted_graph_loaded()
                    self.state.replace_artifact_chunks(artifact_row, chunk_rows, fresh=cleared)
                    if self.vectors is not None:
                        # Chunk ids are content-addressed (text_hash in the id),
                        # so only new/changed chunk text reaches the embedder.
                        embedded_chunks += self.vectors.index_artifact(
                            source.name,
                            parsed.id,
                            chunk_rows,
                            on_progress=embedding_progress,
                        )
                    enrichment = self.graph_builder.add_artifact(source, parsed)
                    self.state.replace_artifact_enrichment(
                        parsed.id,
                        parsed.source_id,
                        enrichment.terms,
                        enrichment.symbols,
                    )
                    artifacts[parsed.relative_path] = {
                        "sha256": parsed.sha256,
                        "artifact_id": parsed.id,
                        "last_indexed_at": now,
                        "git_branch": parsed.git_branch,
                        "git_commit": parsed.git_commit,
                    }
                    changed_ids.add(parsed.id)
                    self._maybe_checkpoint(source.name, manifest)
                indexed += 1
                # Compatibility phase retained for existing CLI/API/MCP
                # progress consumers; ``committing`` is the more precise new
                # phase for callers that understand the staged pipeline.
                indexed_bytes += int(parsed.size_bytes or 0)
                report(
                    "indexing",
                    position,
                    total_items,
                    parsed.relative_path,
                    indexed=indexed,
                    skipped=skipped,
                    bytes_done=indexed_bytes,
                )
                report(
                    "committing",
                    position,
                    total_items,
                    parsed.relative_path,
                    indexed=indexed,
                    skipped=skipped,
                    bytes_done=indexed_bytes,
                )
                # Let anything waiting on the API get a turn before the next
                # file. Without this a long index makes the UI unusable.
                serve_yield()
                # Checkpoint mid-run. Without this the graph and the manifest
                # only reach /state when a sync *finishes*, so stopping the
                # container an hour into a first index threw that hour away:
                # the next start re-read every file (stale manifest) and
                # rebuilt the whole graph.
                if slow_sync_s:
                    time.sleep(slow_sync_s)
            pruned_vectors = self._finalize_index_state(
                source=source,
                connector=connector,
                mode=mode,
                manifest=manifest,
                indexed=indexed,
                embedded_chunks=embedded_chunks,
                changed_ids=changed_ids,
                total_items=total_items,
                report=report,
                embedding_progress=embedding_progress,
                enrich=enrich,
            )
            # Recorded only after the pass succeeded, so a sync that dies
            # halfway is retried rather than being remembered as "done with
            # this config".
            self.state.set_fingerprint(
                SOURCE_SCOPE.format(name=source.name), fingerprint, utc_now()
            )
            if mode == "full":
                self.state.execute(
                    "DELETE FROM sync_fingerprints WHERE scope=?",
                    (FULL_RESUME_SCOPE.format(name=source.name),),
                )
            # Synapse 21.6A: compressed timestamped graph snapshots + retention.
            # Additive history beside graph.latest.json, throttled by the
            # configured interval and bounded by max_state_size_gb. Fail-soft so
            # a snapshot/retention hiccup never fails the sync.
            if indexed or mode == "full":
                report(
                    "snapshotting",
                    total_items,
                    total_items,
                    "preserving the published graph generation",
                )
                self._snapshot_after_sync()
            now = utc_now()
            cursor, high_watermark = connector.checkpoint_from_items(items)
            high_watermark.update({"indexed_artifacts": indexed, "skipped_artifacts": skipped})
            if remote_state is not None:
                high_watermark["repository"] = remote_state
            connector.set_checkpoint(cursor, high_watermark, "healthy")
            self.state.mark_source_indexed(source.name, now)
            if self.vectors is not None:
                # Synapse 21.4: additive keys, present only when embeddings
                # are enabled so default sync_events stay byte-identical.
                details_extra.update(
                    {
                        "embedded_chunks": embedded_chunks,
                        "pruned_vectors": pruned_vectors,
                    }
                )
            # Synapse 21.3: record skipped-vs-fetched transfer counts per sync.
            self.state.append_sync_event(
                uuid.uuid4().hex,
                source.name,
                "sync.completed",
                "healthy",
                started_at,
                now,
                {
                    "mode": mode,
                    "connector_type": connector.connector_type,
                    "fetched": fetched,
                    "skipped": transfer_skipped,
                    "indexed_artifacts": indexed,
                    "skipped_artifacts": skipped,
                    **details_extra,
                },
            )
            # Synapse 21.5: (re)publish the contract + emit/POST sync.completed.
            # Still inside the writer lock + per-source lock; fail-soft so a
            # publish/webhook hiccup never fails the sync.
            self._publish_after_sync(source.name, mode, started_at, now, indexed, skipped)
            capacity = self._graph_capacity_notice()
            if capacity:
                details_extra = {**details_extra, "capacity": capacity}
            graph_nodes, graph_edges = self._graph_counts()
            return SyncResult(
                source.name,
                indexed,
                skipped,
                graph_nodes,
                graph_edges,
                "healthy",
                {
                    "connector_type": connector.connector_type,
                    "fetched": fetched,
                    "skipped": transfer_skipped,
                    "checkpoint": self.state.get_source_checkpoint(source.name),
                    **corpus_policy.refusal_details(refused),
                    **details_extra,
                },
            )

    @contextmanager
    def _source_write_lock(self, source_name: str):
        """Hold everything needed to write one source, then release it.

        Exclusion is per **source** on a shared backend and per **process** on
        SQLite. Postgres arbitrates concurrent writers itself, so two indexers
        can commit two different sources at once — which is the point of the
        35.2 seam. SQLite genuinely permits one writer per file, so the
        whole-state :class:`EngineLease` there is an accurate model rather than
        a limitation to route around.

        The on-disk ``source_lock`` is held in both cases: it excludes two
        *threads* of one process, which no database lease addresses.
        """

        timeout = float(self.config.sync.concurrency.lock_timeout_seconds or 0)
        lease = None
        if self.state.dialect.is_postgres:
            lease = SourceLease(self.state, source_name)
            lease.acquire(wait_timeout_s=timeout)
        else:
            with self._lease_mutex:
                self.lease.acquire()
        try:
            with source_lock(self.paths.locks, source_name, timeout_s=timeout):
                yield
        finally:
            if lease is not None:
                lease.release()

    def _graph_capacity_notice(self) -> dict[str, Any] | None:
        """Say something once a graph outgrows one container (Phase 35.3).

        Deliberately a *notice*, not a refusal. `sync.limits` can refuse
        because it checks before any work happens; by the time this can fire,
        the index exists and throwing it away would help nobody.

        The threshold is measured, not guessed — see `graph/capacity.py`. The
        binding constraint is not RAM but checkpoint serialization: at 1.58M
        nodes one `graph.latest.json.zst` write takes ~20s against a default
        60s `storage.graph_checkpoint_seconds`, so a third of a long sync goes
        on writing the graph out, and it gets worse linearly.
        """

        limit = getattr(self.config.graph, "max_nodes", None)
        if not limit:
            return None
        nodes, _edges = self._graph_counts()
        if nodes <= int(limit):
            return None
        # ~2.4 KB of process RSS per node, flat across four measured scales —
        # divided by the graph's share of RSS, because what an operator has to
        # provision is the whole process, not the graph in isolation. Omitting
        # that divisor under-reported by 40% against the identical projection
        # `pheasant scan` and `pheasant shard plan` print, so the same corpus
        # got two different answers depending on which surface asked.
        projected_bytes = int(nodes * RSS_BYTES_PER_NODE / GRAPH_SHARE_OF_RSS)
        projected_gb = projected_bytes / 1e9
        notice = {
            "nodes": nodes,
            "max_nodes": int(limit),
            # Exact bytes as well as the rounded figure: at a low threshold the
            # GB value rounds to 0.0, and a capacity notice reporting "0.0 GB"
            # reads as a bug rather than as a small graph.
            "projected_rss_bytes": projected_bytes,
            "projected_rss_gb": round(projected_gb, 2),
            "advice": (
                "This graph has outgrown what one region holds comfortably. Split the "
                "corpus across several pheasant regions and let the router fan out "
                "(see docs/how-to/capacity-planning.md); raising graph.max_nodes "
                "silences this without changing the cost."
            ),
        }
        logger.warning(
            "Graph is %d nodes, past graph.max_nodes=%d (~%.1f GB RSS). %s",
            nodes,
            int(limit),
            projected_gb,
            notice["advice"],
        )
        return notice

    @property
    def stats(self) -> dict[str, int]:
        nodes, edges = self._graph_counts()
        return {
            "node_count": nodes,
            "edge_count": edges,
            "artifact_count": int(self.state.rows("SELECT COUNT(*) AS c FROM artifacts")[0]["c"]),
            "chunk_count": int(self.state.rows("SELECT COUNT(*) AS c FROM chunks")[0]["c"]),
        }

    def vector_searcher(self) -> VectorSearcher | None:
        """Query-time vector searcher sharing this engine's embedder/store."""

        if self.vectors is None:
            return None
        return VectorSearcher(self.vectors.embedder, self.vectors.store, self.state)

    def search_context(self, query: str, max_results: int = 10) -> dict:
        from pheasant.search.hybrid import HybridSearch
        from pheasant.search.ranking import resolver_for
        from pheasant.search.sqlite_store import SearchStore

        return HybridSearch(
            SearchStore(self.state, ranking=resolver_for(self.config, self.state)),
            vector=self.vector_searcher(),
            wasm_relationship_search=self.config.search.wasm_relationship_search,
        ).search_context(
            self.config.knowledge_base_id,
            query,
            max_results=max_results,
        )

    def _snapshot_after_sync(self) -> None:
        """Write a compressed graph snapshot (interval-throttled) + prune.

        Gated by ``storage.graph_snapshots`` (default on). At most one snapshot
        per ``graph_snapshot_interval_seconds``: if the newest existing snapshot
        is younger than the interval, no new snapshot is written. Retention then
        evicts oldest snapshots beyond ``max_state_size_gb``. Every step is
        fail-soft — the snapshot is additive history, never load-bearing for a
        sync.
        """

        storage = self.config.storage
        if not storage.graph_snapshots:
            return
        kb_id = self.config.knowledge_base_id
        try:
            now = utc_now()
            if self._snapshot_due(kb_id, now, storage.graph_snapshot_interval_seconds):
                self.graph_store.snapshot_current(kb_id, now)
            max_bytes = int(float(storage.max_state_size_gb) * 1024**3)
            self.graph_store.enforce_retention(kb_id, max_bytes)
        except Exception as exc:  # noqa: BLE001 - snapshots must not fail sync
            import logging

            logging.getLogger(__name__).warning(
                "Graph snapshot/retention failed for %s: %s", kb_id, exc
            )

    def _snapshot_due(self, kb_id: str, now: str, interval_seconds: int) -> bool:
        """True when no snapshot exists yet or the newest is older than interval."""

        if interval_seconds <= 0:
            return True
        snapshots = self.graph_store.list_snapshots(kb_id)
        if not snapshots:
            return True
        from datetime import datetime

        from pheasant.persistence.graph_store import SNAPSHOT_PATTERN

        match = SNAPSHOT_PATTERN.match(snapshots[-1].name)
        if not match:
            return True
        # Filename ts is the ISO-8601 form with ':' -> '-'; restore the time
        # separators (positions of the two ':' in HH:MM:SS) for parsing.
        raw = match.group("ts")
        iso = _restore_iso_ts(raw)
        try:
            last = datetime.fromisoformat(iso)
            current = datetime.fromisoformat(now.rstrip("Z"))
        except ValueError:
            return True
        return (current - last).total_seconds() >= interval_seconds

    def _publish_after_sync(
        self,
        source_name: str,
        mode: str,
        started_at: str,
        finished_at: str,
        indexed: int,
        skipped: int,
    ) -> None:
        """Publish the contract + emit/POST the sync.completed event.

        Entirely gated by ``synapse.publish`` (default off) so a router-less
        standalone region is byte-for-byte unchanged. When on, (re)derives +
        writes ``<state>/contract.latest.json``, appends the local NDJSON
        ``sync.completed`` event, and — when ``synapse.router_url`` is set —
        POSTs the event (with the inline contract) to the router. Every step is
        fail-soft: a publish/webhook hiccup never fails the sync.
        """

        if not self.config.synapse.publish:
            return
        contract: dict | None = None
        try:
            store = self.vectors.store if self.vectors is not None else None
            contract = ContractPublisher(self.config, self.state, vector_store=store).publish(
                generated_at=finished_at
            )
        except Exception as exc:  # noqa: BLE001 - publication must not fail sync
            import logging

            logging.getLogger(__name__).warning(
                "Synapse contract publication failed for %s: %s", source_name, exc
            )
        event: dict = {
            "type": "sync.completed",
            "kb_id": self.config.knowledge_base_id,
            "source_id": source_name,
            "mode": mode,
            "started_at": started_at,
            "finished_at": finished_at,
            "indexed_artifacts": indexed,
            "skipped_artifacts": skipped,
        }
        if self.config.synapse.fleet_id:
            event["fleet_id"] = self.config.synapse.fleet_id
        if self.config.synapse.endpoint:
            event["endpoint"] = self.config.synapse.endpoint
        self.events.append(event, now=finished_at)
        if self.router_webhook.enabled:
            payload = dict(event)
            if contract is not None:
                payload["contract"] = contract
            self.router_webhook.post_event(payload)

    def _emit_source_changed(self, source_name: str, now: str | None = None) -> None:
        """Emit a local ``source.changed`` event (used by the watcher)."""

        self.events.append(
            {
                "type": "source.changed",
                "kb_id": self.config.knowledge_base_id,
                "source_id": source_name,
            },
            now=now,
        )

    def scan_source(
        self,
        source_name: str,
        *,
        max_depth: int | None = None,
        full_scan: bool = False,
    ) -> dict:
        """Estimate what a source would index, without indexing anything.

        The pre-flight for pointing a source at something large: it reports
        the file count, total size, where the weight actually sits, and — via
        ``depth_options`` — how many files each depth cap would admit, so a
        depth can be chosen from evidence instead of guessed. Reads no file
        content and writes nothing.

        Runs with the budget *disabled* on purpose: the whole point is to
        learn the real size, including when it is over the limit. The
        absolute entry ceiling in the walker still applies.
        """
        source = self.config.effective_source(
            self._source(source_name), max_depth=max_depth, full_scan=full_scan
        )
        configured = WalkBudget.from_settings(source.limits)
        if source.type not in FILESYSTEM_SOURCE_TYPES:
            return {
                "source_id": source.name,
                "type": source.type.value,
                "scannable": False,
                "reason": "scan estimates filesystem sources; this source pulls from a service.",
            }
        if not source.path.exists():
            return {
                "source_id": source.name,
                "type": source.type.value,
                "scannable": False,
                "reason": f"source path does not exist in this environment: {source.path}",
            }
        report = walk_source(
            source.path,
            include=source.include,
            exclude=source.exclude,
            max_depth=source.max_depth,
            # Size the *real* tree, then report which limits it would trip.
            budget=WalkBudget(
                max_files=None,
                max_file_size_bytes=configured.max_file_size_bytes,
                max_total_bytes=None,
            ),
            follow_symlinks=bool(getattr(source.limits, "follow_symlinks", False)),
        )
        would_exceed = []
        if configured.max_files is not None and report.file_count > configured.max_files:
            would_exceed.append(f"max_files ({report.file_count} > {configured.max_files})")
        over_total = (
            configured.max_total_bytes is not None
            and report.total_bytes > configured.max_total_bytes
        )
        if over_total:
            would_exceed.append(
                f"max_total_mb ({report.total_bytes // (1024 * 1024)} > "
                f"{configured.max_total_bytes // (1024 * 1024)})"
            )
        return {
            "source_id": source.name,
            "type": source.type.value,
            "path": str(source.path),
            "scannable": True,
            **report.as_dict(),
            "depth_options": aggregate_depth_counts(report),
            "limits": {
                "max_files": configured.max_files,
                "max_file_size_mb": (
                    configured.max_file_size_bytes // (1024 * 1024)
                    if configured.max_file_size_bytes
                    else None
                ),
                "max_total_mb": (
                    configured.max_total_bytes // (1024 * 1024)
                    if configured.max_total_bytes
                    else None
                ),
            },
            "would_exceed": would_exceed,
            "would_sync": not would_exceed,
            # Phase 35.7: the scan already has the two inputs the capacity
            # model needs, so the projection costs nothing here — and this is
            # the moment it matters, before anyone commits to a first index
            # and learns the answer from an OOM kill mid-sync.
            "projection": project_capacity(
                report.file_count,
                report.total_bytes,
                embedding_dimensions=(
                    self.config.search.embeddings.dimensions
                    if self.config.search.embeddings.enabled
                    else 0
                ),
                max_nodes_per_shard=self.config.graph.max_nodes or 1_500_000,
            ).as_dict(),
            # The evaluation plane scales on a *different axis* from the
            # corpus -- cohort size times the ablation matrix, not file count
            # -- so it gets its own projection rather than a share of the one
            # above. Present only when evaluation is on: an operator who has
            # not enabled it does not need a volume estimate for it.
            "evaluation_projection": (
                self._evaluation_projection() if self.config.evaluation.enabled else None
            ),
        }

    def _evaluation_projection(self) -> dict[str, Any]:
        """Size an evaluation batch from the region's own configuration.

        Reads the cohort cap, the enabled variants and the storage cap off the
        live config rather than taking the model's defaults, so the answer
        describes *this* region: a deployment that trimmed the ablation matrix
        or lowered `maximum_stored_per_query_results` should see the smaller
        number it actually earned.
        """

        from pheasant.capacity import project_evaluation

        settings = self.config.evaluation
        variant_flags = (
            settings.variants.memory_content,
            settings.variants.alias_only,
            settings.variants.preference_only,
            settings.variants.exclusion_only,
            settings.variants.full_memory,
        )
        # +1 for B0, which is not optional: every attribution number is a
        # paired difference against it.
        variants = 1 + sum(1 for enabled in variant_flags if enabled)
        cohort_flags = (
            settings.cohorts.anchor,
            settings.cohorts.rolling,
            settings.cohorts.learned,
            settings.cohorts.temporal_holdout,
            settings.cohorts.control,
            settings.cohorts.synthetic_invariants,
        )
        cohorts = max(1, sum(1 for enabled in cohort_flags if enabled))
        # A material-change trigger on an hourly-indexing region runs hourly,
        # bounded by `minimum_interval_seconds`. Off, an operator runs it by
        # hand, and once a day is the honest default for that.
        runs_per_day = (
            86400.0 / max(1.0, float(settings.minimum_interval_seconds))
            if settings.on_material_snapshot
            else 1.0
        )
        return project_evaluation(
            min(
                int(settings.cohorts.maximum_queries_per_cohort),
                int(settings.maximum_queries_per_run),
            ),
            variants=variants,
            cohorts=cohorts,
            runs_per_day=runs_per_day,
            embeddings_enabled=bool(self.config.search.embeddings.enabled),
            max_stored_per_query_results=int(settings.maximum_stored_per_query_results),
        ).as_dict()

    def _source(self, name: str) -> SourceConfig:
        for source in self.config.sources:
            if source.name == name:
                return source
        # Sources added at runtime (the UI's quick-add, POST /sources) live in
        # the state registry, not the config file. Reading them back is what
        # lets a *different process* — the sync worker, or the CLI — index a
        # source that was never written to YAML.
        row = self.state.get_source(name)
        if row and row.get("config_json"):
            payload = json.loads(row["config_json"])
            resolved = PheasantConfig.model_validate({"sources": [payload]}).sources[0]
            self.config.sources.append(resolved)
            return resolved
        raise KeyError(f"Unknown source: {name}")

    def enabled_sources(self) -> list[SourceConfig]:
        """Every enabled source, including UI/API registrations in state.

        Runtime registrations are durable precisely so another process can
        index them. Enumerating only the YAML list made `sync_all`, scheduler
        beats and post-restart startup silently forget those sources even
        though source-specific sync still worked through :meth:`_source`.
        Configured order is preserved, followed by registry-only names in the
        registry's stable alphabetical order.
        """

        sources = [source for source in self.config.sources if source.enabled]
        seen = {source.name for source in sources}
        registry = SourceRegistry(self.config, self.state)
        for row in registry.list_sources(enabled=True, limit=100_000):
            name = str(row.get("id") or row.get("name") or "")
            if not name or name in seen:
                continue
            try:
                source = self._source(name)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                logger.warning("Ignoring invalid registered source %s", name, exc_info=True)
                continue
            if source.enabled:
                sources.append(source)
                seen.add(source.name)
        return sources

    def _can_skip_before_read(
        self,
        mode: SyncMode,
        previous: dict | None,
        item: ConnectorItem,
    ) -> bool:
        return (
            mode == "incremental"
            and previous is not None
            and item.sha256 is not None
            and previous.get("sha256") == item.sha256
        )

    def _needs_repair(
        self,
        source_id: str,
        artifact_id: str,
        sha256: str,
        previous: dict | None,
        expected_chunks: int,
    ) -> bool:
        if not previous or previous.get("sha256") != sha256:
            return True
        state = self.state.artifact_state(source_id, artifact_id)
        if state is None:
            return True
        # A node missing from the graph is as broken as a missing chunk row —
        # it is the case an interrupted sync leaves behind, and it is what
        # makes "repair" cheaper than "index it all again".
        self._ensure_persisted_graph_loaded()
        if not self.graph_builder.graph.has_node(artifact_id):
            return True
        return (
            state.get("sha256") != sha256
            or state.get("status") != "indexed"
            or int(state.get("chunk_count") or 0) < expected_chunks
        )
