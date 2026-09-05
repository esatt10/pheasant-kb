from __future__ import annotations

import hmac
import json
import logging
import os
import socket
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from pheasant.api.readiness_routes import register_readiness_routes
from pheasant.assistant.credentials import SessionKeyStore
from pheasant.config.loader import (
    ConfigError,
    dump_config_yaml,
    effective_config_dict,
    load_config,
    validate_source_paths,
)
from pheasant.config.profiles import profile_names
from pheasant.config.schema import PheasantConfig, SourceConfig, SourceType
from pheasant.deployment.roles import Role, resolve_role, validate_role
from pheasant.deployment.roles import describe as describe_role
from pheasant.deployment.serving import RETRY_AFTER_SECONDS, ConcurrencyLimiter, DrainState
from pheasant.graph.exporter import cytoscape, node_link
from pheasant.graph.query_service import graph_for_config
from pheasant.graph.simple import SimpleMultiDiGraph
from pheasant.ingestion.pipeline import read_text, utc_now
from pheasant.jobs import JobRegistry
from pheasant.persistence.paths import StatePaths
from pheasant.persistence.state_store import StateStore
from pheasant.registry.knowledge_base_registry import KnowledgeBaseRegistry
from pheasant.registry.source_registry import SourceRegistry
from pheasant.search.hybrid import HybridSearch
from pheasant.search.ranking import resolver_for
from pheasant.search.sqlite_store import SearchStore
from pheasant.security.path_policy import (
    PathPolicyError,
    resolve_config_write_target,
    resolve_under,
)
from pheasant.services import ServiceContext, ServiceError
from pheasant.services import assistant as assistant_service
from pheasant.services import graph as graph_service
from pheasant.services import retrieval as retrieval_service
from pheasant.sync.engine import SyncEngine
from pheasant.sync.fingerprint import EMBEDDING_SCOPE, embedding_fingerprint
from pheasant.sync.remote_worker import ResultCache
from pheasant.telemetry import metrics
from pheasant.version import __version__

logger = logging.getLogger(__name__)

#: Stand-in for the schema's mandatory ``path`` on source types that pull from
#: a service rather than the filesystem. Nothing ever opens it.
PLUGIN_PLACEHOLDER_PATH = "/unused"

#: ``(id, label, description, path_role)`` for the built-in source types, in
#: the order a picker should show them. ``path_role`` is ``"required"`` when
#: the connector actually reads that path and ``"unused"`` when the schema
#: demands the field but the connector gets its content elsewhere.
#: ``SourceType.memory`` is deliberately absent: agent-memory sources are
#: created and owned by the memory store, not registered by hand.
BUILTIN_SOURCE_TYPES: tuple[tuple[str, str, str, str], ...] = (
    (
        "document_folder",
        "Folder of documents",
        "Mixed files — Markdown, PDF, DOCX, HTML, code, configs.",
        "required",
    ),
    (
        "repository",
        "Git repository",
        "A git checkout, with branch and commit metadata on every artifact.",
        "required",
    ),
    (
        "obsidian_vault",
        "Obsidian vault",
        "Notes plus wikilinks, tags and frontmatter as graph structure.",
        "required",
    ),
    (
        "markdown_folder",
        "Folder of Markdown",
        "Markdown only — the lighter version of a document folder.",
        "required",
    ),
    ("single_file", "Single file", "One file, indexed on its own.", "required"),
    (
        "web_collection",
        "Web pages",
        "A list of URLs fetched over HTTP, with ETag-based incremental sync.",
        "unused",
    ),
    ("api", "HTTP API", "A JSON endpoint paged with a cursor (experimental).", "unused"),
    ("s3", "S3 bucket", "An S3-compatible bucket prefix (experimental).", "unused"),
)


class SearchRequest(BaseModel):
    # Step 32.2 — optional caller identity; enforced only when
    # security.acl_enforced is on. The caller (router / deployment
    # perimeter) authenticates; the region enforces visibility.
    principal: str | None = None
    principal_groups: list[str] = []
    knowledge_base: str | None = None
    query: str
    mode: str = "hybrid"
    max_results: int = 10
    source_name: str | None = None
    # Restrict to one part of a document's extracted taxonomy, matched against
    # the heading breadcrumb. Only meaningful for sources with taxonomy on.
    section: str | None = None
    # Step 33.6 — the same retrieval criteria the MCP tool has always had.
    # They lived only on the MCP surface, so the same region answered a query
    # differently depending on which protocol asked; the router, which reaches
    # this region over HTTP, could not scope a search at all.
    exclude_sources: list[str] | None = None
    node_types: list[str] | None = None
    min_score: float | None = None
    # Scope by the *kind* of source (repository, notion, slack, ...) rather
    # than by name. A caller that does not already know every source in the
    # region can still say "only our wikis" or "nothing from git". Each hit
    # reports its own under `provenance.source_type`.
    source_types: list[str] | None = None
    exclude_source_types: list[str] | None = None
    # How this region's agent memory takes part: "auto" (default), "off",
    # "only", "prefer", or an object with scopes/subject/current_only/as_of.
    memory: dict | str | None = None
    # Pin this search to a sealed snapshot. The region verifies it still
    # stands there and refuses with SNAPSHOT_DRIFTED if it does not — it holds
    # one version of its corpus, so the guarantee is that two runs naming one
    # snapshot cannot silently have seen different corpora.
    snapshot_id: str | None = None
    # The instant memory validity is evaluated at, echoed into the lineage
    # even where the region holds no memory — an arm that ran with memory off
    # has to be able to record that it did.
    as_of: str | None = None
    # The caller's correlation id, echoed so a result joins to the ledger row
    # and the span that produced it.
    trace_id: str | None = None


class MemoryEnableRequest(BaseModel):
    """Provision the agent-memory source.

    ``path`` defaults to ``<state_path>/memory`` rather than a workspace-
    relative folder, because memory is the **only** source type pheasant
    writes to. Every containerised deployment mounts the corpus read-only
    (`/workspace:ro` in both the standard and demo compose files), so
    anchoring memory there registers a source that can never be written —
    found by a live run, where the first write returned a bare 500.
    """

    name: str = "agent-memory"
    path: str | None = None


class MemoryWriteRequest(BaseModel):
    text: str
    scope: str = "user"
    subject: str | None = None
    supersedes: str | None = None
    tags: list[str] = []
    sync: bool = True
    # Step 33.5, all optional and defaulting to the pre-33.5 behavior.
    # `principal` is who asserted this; it is part of the record id, so two
    # callers writing the same sentence in the same second get two records
    # rather than silently sharing one.
    kind: str = "fact"
    principal: str | None = None
    valid_until: str | None = None


class SyncRequest(BaseModel):
    knowledge_base: str | None = None
    source_name: str | None = None
    mode: str = "incremental"
    # Per-run traversal controls. `depth` caps directory depth for this run;
    # `full_scan` lifts both the depth cap and sync.limits. Neither persists.
    depth: int | None = None
    full_scan: bool = False
    # Default True preserves the documented, tested contract: block until
    # the sync finishes and return its full result. A caller that would
    # rather not hold a connection open for a possibly-long sync (the UI's
    # "sync now" button) sets this false and polls `GET /sources`'
    # `syncing`/`sync_error` fields instead.
    wait: bool = True


class RegisterSourceRequest(BaseModel):
    name: str
    type: str = "document_folder"
    path: str
    description: str | None = None
    enabled: bool = True
    max_depth: int | None = None
    include: list[str] | None = None
    exclude: list[str] | None = None
    repo: dict | None = None
    chunking: dict | None = None
    sync: dict | None = None
    connector: dict | None = None
    # Structural taxonomy extraction (chapters/sections/§ codes). The toggle
    # belongs at registration because it is a property of the *source* — "this
    # corpus is structured documentation" — not of the instance. Omitted or
    # `{"enabled": false}` leaves the source byte-identical to pre-taxonomy.
    taxonomy: dict | None = None
    urls: list[str] | None = None
    sync_now: bool = False
    sync_mode: str = "incremental"
    # See SyncRequest.wait — default True keeps register-then-block the
    # existing behavior; the UI sets this false so registering a source
    # never holds the connection open for however long the first sync
    # takes.
    wait: bool = True


class UpdateSourceRequest(BaseModel):
    type: str | None = None
    path: str | None = None
    description: str | None = None
    enabled: bool | None = None
    max_depth: int | None = None
    include: list[str] | None = None
    exclude: list[str] | None = None
    taxonomy: dict | None = None
    repo: dict | None = None
    chunking: dict | None = None
    sync: dict | None = None
    connector: dict | None = None
    urls: list[str] | None = None


class PromoteSourceRequest(BaseModel):
    config_path: str | None = None
    write: bool = False


class ConfigWriteRequest(BaseModel):
    config: dict | None = None
    yaml_text: str | None = None


class ChatRequest(BaseModel):
    question: str
    # Opaque handle for a key the user pasted this session. Never the key.
    session_id: str | None = None
    mode: str = "hybrid"
    max_results: int | None = None
    source_name: str | None = None
    # Scope the answer to (or away from) kinds of source. Same axis as
    # `POST /search`'s, applied to every retrieval the answering loop runs.
    source_types: list[str] | None = None
    exclude_source_types: list[str] | None = None
    principal: str | None = None
    principal_groups: list[str] = []
    # Override assistant.workflow for this one question ("simple",
    # "agentic", or any registered plugin name).
    workflow: str | None = None
    # Per-request workflow knobs, merged over assistant.workflow_options.
    options: dict | None = None
    # How this region's agent memory takes part in the answer: "auto", "off",
    # "only", "prefer", or the full policy object (Step 33.10).
    memory: dict | str | None = None


class EmbeddingsRequest(BaseModel):
    """Turn semantic search on/off and configure the provider."""

    enabled: bool | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    dimensions: int | None = None
    batch_size: int | None = None
    store_provider: str | None = None
    # Persist to the config file as well as the live process.
    persist: bool = True
    # Build vectors for already-indexed content straight away.
    reindex: bool = False


class AssistantKeyRequest(BaseModel):
    provider: str
    api_key: str
    model: str | None = None
    base_url: str | None = None


class RetrievalRequest(BaseModel):
    """Tune the typed retrieval criteria (``assistant.retrieval``).

    Every field is optional and ``None`` means "leave it alone" — a PUT that
    sets one knob must not silently reset the other nine to their defaults.
    """

    max_rounds: int | None = None
    per_query_results: int | None = None
    max_context_passages: int | None = None
    retrieval_modes: list[str] | None = None
    expand_graph: bool | None = None
    expand_depth: int | None = None
    expand_per_node: int | None = None
    grade_evidence: bool | None = None
    verify_citations: bool | None = None
    max_facts: int | None = None
    # Write the change to the config file as well as the live process.
    persist: bool = True


class ConfigSectionRequest(BaseModel):
    """One config section's new value, plus what to do with it."""

    values: dict
    persist: bool = True


class EvidenceRequest(BaseModel):
    """One typed observation about one retrieved thing.

    ``event_type`` is required and drawn from the taxonomy rather than being a
    boolean "useful": *cited*, *selected*, *accepted* and *validated* are four
    claims of very different strength, and an API that let a caller skip the
    distinction would collapse them at the one point where the information
    still exists.
    """

    query: str
    target_id: str
    event_type: str
    target_type: str = "artifact"
    interaction_id: str | None = None
    principal: str | None = None
    session_id: str | None = None
    position: int | None = None
    outcome_reference: str | None = None
    reason_code: str = ""


class EvaluationRunRequest(BaseModel):
    """Start one evaluation batch.

    ``mode`` picks between "how does the region answer that historical
    question now" and "what could it have known at ``as_of``". A report always
    names which ran; a longitudinal chart that mixed them would be comparing
    two different questions.
    """

    mode: str = "current_state"
    as_of: str | None = None
    force: bool = False


class TuningRunRequest(BaseModel):
    """Start one tuning batch.

    ``apply`` is separate from ``force`` on purpose. ``force`` runs a batch a
    region has not enabled -- read-only, and safe. ``apply`` lets the winner
    change what every replica serves, and no amount of "I meant to run it"
    should imply that.
    """

    force: bool = False
    apply: bool = False
    diagnose_only: bool = False


class TuningApplyRequest(BaseModel):
    """Make one bundle the region's live retrieval overlay."""

    bundle_id: str
    applied_by: str = "api"


class TuningCancelRequest(BaseModel):
    """Ask the running batch to stand down."""

    experiment_id: str | None = None
    requested_by: str = "ui"


class TuningRollbackRequest(BaseModel):
    """Stand the overlay down, to the base or to an earlier bundle.

    ``to`` defaults to ``"base"`` — the configured values, which are the thing
    an operator can read in a file. An earlier bundle id steps back to a
    decision somebody made before, and is recorded as a rollback rather than
    as a fresh apply that happens to use old numbers.
    """

    to: str = "base"


class TuningPinRequest(BaseModel):
    """Parameters an operator has settled and does not want re-litigated.

    Replaces the whole list rather than adding to it, so the request describes
    the state the caller wants rather than a delta against one they may not
    have read.
    """

    pinned: list[str]


class KnowledgeBaseRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    persist: bool = True


class QuickAddRequest(BaseModel):
    """One-field source creation: paste a path or URL and go."""

    target: str
    name: str | None = None
    split: bool = False
    #: Extract a structural taxonomy (chapters/sections/§ codes) from this
    #: source's documents. Off by default, matching the per-source config.
    taxonomy: bool = False
    sync_now: bool = True
    sync_mode: str = "incremental"
    # See SyncRequest.wait — default True keeps the existing, tested
    # register-then-block-on-sync contract; the UI's quick-add sets this
    # false so pasting a large repo's URL never blocks the form (and the
    # reverse proxy in front of it) on however long the first index takes.
    wait: bool = True


class RemotePrepareRequest(BaseModel):
    """Immutable coordinator task accepted by an opt-in indexing worker."""

    source: dict
    item: dict
    payload: dict
    git_metadata: list[str | bool | None] | None = None


#: Tasks a worker will accept in one batch. Every task holds its file's bytes
#: in memory, so this is a memory bound, not a politeness limit: at the
#: 25 MB-per-file default a 64-task batch is already a 1.6 GB worst case, and
#: the coordinator's own default batch is far smaller.
MAX_PREPARE_BATCH = 64

#: Entries in the per-worker idempotency cache.
PREPARE_CACHE_SIZE = 256


class RemotePrepareBatchRequest(BaseModel):
    """Several preparation tasks in one request (Phase 35.5).

    ``idempotency_keys`` is content-addressed by the coordinator, so a retry
    after a timeout carries the same keys and is answered from cache instead
    of re-parsed. ``deadline_seconds`` is the caller's remaining budget: the
    worker stops rather than finishing a batch nobody is waiting for.
    """

    tasks: list[RemotePrepareRequest]
    idempotency_keys: list[str] | None = None
    deadline_seconds: float | None = None


class GraphQueryRequest(BaseModel):
    """One bounded operation on the internal graph-query service."""

    operation: str
    parameters: dict[str, Any] = {}


#: Re-exported: the text is the same on both surfaces, so it lives with the
#: operation rather than in whichever transport happened to define it first.
RETRIEVAL_FIELD_HELP = assistant_service.RETRIEVAL_FIELD_HELP


#: Config sections that can be changed on a running server, and what changing
#: one actually costs. ``True`` means the live process picks it up; ``False``
#: means it is written to the file and needs a restart. Being honest about the
#: difference is the whole point — a UI that says "saved" for a setting the
#: process is still ignoring has lied to the user.
def _state_is_writable(config: PheasantConfig, state: object) -> bool:
    """Can this process actually write the hot store?

    Under PostgreSQL, yes, from every replica -- which is why the shipped
    fleet needs no spool at all. Under SQLite the answer is the filesystem's:
    `docker-compose.scale.yml` mounts `/state:ro` on API replicas so the
    indexer is the only writer, and probing beats asking an operator to
    configure per-role what the mount already decided.

    Verified against the two forms that mount can take, because they are not
    equivalent. A read-only **mount** -- what `:ro` actually is -- is reported
    unwritable even to uid 0, since the kernel checks `MS_RDONLY` before it
    checks anything else. Read-only **permission bits** are not: root bypasses
    them, so this returns True and the first insert raises instead. That
    failure is caught and counted as a dropped batch rather than surfacing to
    a caller, so the worst case is a degraded ledger, not a broken request --
    and the manifests express it as a mount, which is the case that works.
    """

    backend = str(getattr(config.storage, "backend", "sqlite") or "sqlite").lower()
    if backend != "sqlite":
        return True
    path = getattr(config.storage, "sqlite_path", None)
    if path is None:
        return True
    return os.access(Path(path).parent, os.W_OK)


def _build_interaction_buffer(
    config: PheasantConfig,
    state: object,
    app: FastAPI,
    role_policy: object,
) -> object | None:
    """Wire the observation plane, or return ``None`` when it is off.

    Never raises. Telemetry that can break startup is worse than no telemetry,
    and this runs before anything has served a request.
    """

    settings = getattr(getattr(config, "observability", None), "interactions", None)
    if settings is None or not getattr(settings, "enabled", False):
        return None
    try:
        from pheasant.sync.log_queue import log_queue_from_config
        from pheasant.telemetry.interactions import (
            InteractionBuffer,
            configure_tracing,
            resolve_sink,
            set_process_buffer,
        )

        configure_tracing(config.observability)
        queue = log_queue_from_config(config, state)
        app.state.log_queue = queue
        sink = resolve_sink(
            settings,
            state=state,
            queue=queue,
            kb_id=config.pheasant.name,
            state_writable=_state_is_writable(config, state),
            owner=f"{socket.gethostname()}-{os.getpid()}",
        )
        buffer = InteractionBuffer(
            sink,
            capacity=int(getattr(settings, "buffer_size", 10_000)),
            batch_size=int(getattr(settings, "flush_batch_size", 500)),
            interval_seconds=float(getattr(settings, "flush_interval_seconds", 5.0)),
        )
        buffer.start()
        # The mounted MCP app observes through this same buffer rather than
        # opening a second one -- see `interactions.set_process_buffer`.
        set_process_buffer(buffer)
        logger.info(
            "Observation plane on (sink=%s, role=%s). Interactions are rows with a "
            "retention policy, never indexed content.",
            buffer.sink_name,
            getattr(role_policy, "name", "?"),
        )
        return buffer
    except Exception:  # noqa: BLE001 - telemetry must never break startup
        logger.warning(
            "Could not start the observation plane; continuing without it", exc_info=True
        )
        return None


def observed(request: Any) -> Any:
    """The live interaction event for this request, or ``None``.

    The seam between the middleware, which knows the timing and the status,
    and a handler, which is the only thing that has seen the request body and
    the results. A handler enriches through this and never has to know whether
    observation is on -- with it off there is no event and this returns None.

    Free text set here is capped and redacted by the middleware afterwards, so
    a handler never has to remember to do either.
    """

    return getattr(getattr(request, "state", None), "interaction", None)


def record_retrieval(
    request: Any,
    *,
    query: str | None,
    payload: Any,
    criteria: dict[str, Any] | None = None,
    answer: str | None = None,
    event: Any = None,
) -> None:
    """Fill in what only a handler knows: the question, the hits, the answer.

    A no-op when observation is off, so a route calls it unconditionally and
    never branches on telemetry. Free text set here is capped and redacted by
    the middleware on the way out, so a handler cannot forget to do either.
    """

    event = event if event is not None else observed(request)
    if event is None:
        return
    try:
        from pheasant.telemetry.interactions import extract_results

        event.query_text = query
        if criteria:
            event.criteria = criteria
        if answer is not None:
            event.answer_text = answer
        ids, paths, top, count = extract_results(payload)
        event.result_ids, event.result_paths = ids, paths
        event.top_score = top
        event.result_count = count
    except Exception:  # noqa: BLE001 - an observation must never fail a request
        logger.debug("could not record retrieval detail", exc_info=True)


def _observe_shed(
    buffer: object | None, request: object, path: str, config: PheasantConfig
) -> None:
    """Record a 429 without going through `observe`.

    Separate because the shed path has no handler to wrap: the response is
    built and returned before anything else runs.
    """

    if buffer is None:
        return
    try:
        from pheasant.telemetry.interactions import InteractionContext, InteractionEvent, utc_now

        headers = request.headers  # type: ignore[attr-defined]
        context = InteractionContext.create(
            "ui",
            principal=headers.get("x-pheasant-principal"),
            session_id=headers.get("x-pheasant-session"),
            client_id=headers.get("x-pheasant-client"),
            traceparent=headers.get("traceparent"),
        )
        buffer.record(  # type: ignore[attr-defined]
            InteractionEvent(
                kb_id=config.pheasant.name,
                operation=path,
                modality=str(context.modality),
                principal=context.principal,
                session_id=context.session_id,
                client_id=context.client_id,
                trace_id=context.trace_id,
                span_id=context.span_id,
                parent_span_id=context.parent_span_id,
                started_at=utc_now().isoformat(timespec="microseconds").replace("+00:00", "Z"),
                status="shed",
            )
        )
    except Exception:  # noqa: BLE001 - an observation must never fail a request
        logger.debug("could not observe a shed request", exc_info=True)


#: Routes the observation plane ignores.
#:
#: Liveness and scrape endpoints are machine chatter, not interactions: no
#: query, no session, no principal, and nothing a formation rule could ever
#: read. Recording them is not merely useless, it is expensive --- a container
#: health check every 3s is 28,800 rows per replica per day, about 26 MB, so a
#: three-replica fleet holding a week of history would carry half a gigabyte
#: of probes. Measured on the CI log-tier run, where the row count visibly
#: climbed between assertions with no traffic but the probes.
#:
#: `/mcp` is here for a different reason: it is observed one level down, by
#: the decorator that wraps every tool, which knows the tool name and its
#: criteria. Counting it twice would double every formation threshold.
UNOBSERVED_PATHS = frozenset({"/health", "/ready", "/metrics", "/mcp"})

LIVE_APPLICABLE_SECTIONS: dict[str, bool] = {
    "search": True,
    "assistant": True,
    "graph": True,
    "memory": True,
    "ingestion": False,  # captioner/transcriber are wired at engine construction
    "sync": False,  # watcher/scheduler services are started at boot
    # Path policy is read per request, but ACL wiring is not -- and since
    # 35.8 neither is `api_auth`: the token is resolved once at construction
    # and the middleware is only installed when one exists, so turning auth on
    # or rotating the token is a restart.
    "security": False,
    "synapse": True,
    # The tracer provider, the exporter and the buffer's flusher thread are
    # all wired once at app construction, and the log tier's queue binding
    # with them. Same reason `sync` is False: these are services, not
    # values read per request.
    "observability": False,
    # Read per run, not wired into a service: the runner reads
    # `config.evaluation` when a batch starts, so a change applies to the next
    # run without a restart. The exception is nothing — even the lease is
    # claimed per run rather than held.
    "evaluation": True,
    # Read per batch, not wired into a service: the runner reads `config.tuning`
    # when a batch starts, so a change applies to the next one. The applied
    # *bundle* is a different thing entirely — it lives in /state, not in this
    # config, and every replica picks it up on its own TTL without a restart.
    "tuning": True,
    # Read per check, not wired into a service: a readiness run reads
    # `config.readiness` when it starts. The one part that is *not* per-check
    # is `corpus_denylist`, which the ingestion service reads on every
    # submission — also per call, so also live. Nothing here is a service
    # started at boot, which is the question this table actually asks.
    "readiness": True,
    "storage": False,
    "server": False,
    "pheasant": False,
}


def _tuning_sweeps(trials: list[dict], metric: str) -> dict[str, list[dict]]:
    """Trials grouped by the parameter each one moved, for plotting.

    Grouped here rather than in the browser because the answer is already in
    the trial: a proposal moves exactly one coordinate, and its delta names
    which. Re-deriving that per render would put the strategy's invariant
    (one coordinate at a time) into the UI, where a later change to the
    strategy could not reach it.

    Each series carries the baseline value too, so a sweep renders as "what
    happens as this parameter moves away from what we serve" rather than as an
    unanchored scatter.
    """

    sweeps: dict[str, list[dict]] = {}
    for trial in trials:
        delta = (trial.get("point") or {}).get("delta") or {}
        for name, pair in delta.items():
            before, after = (list(pair) + [None, None])[:2]
            sweeps.setdefault(name, []).append(
                {
                    "trial_id": trial.get("trial_id", ""),
                    "value": after,
                    "baseline_value": before,
                    "metric": (trial.get("metrics") or {}).get(metric),
                    "stage": trial.get("motivating_stage", ""),
                    "cost_class": trial.get("cost_class", ""),
                }
            )
    for series in sweeps.values():
        series.sort(key=lambda point: (point["value"] is None, point["value"]))
    return sweeps


def _allowed_roots(config: PheasantConfig) -> list[Path]:
    """Roots a UI may browse / register sources under.

    Mirrors the allowlist used by the MCP register tool plus the exports path.
    """
    roots = [
        config.pheasant.workspace_root,
        config.pheasant.exports_path,
        *config.security.allow_workspace_roots,
    ]
    if config.security.allow_user_selected_source_paths:
        roots.append(Path("/"))
    seen: list[Path] = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved not in seen and resolved.exists():
            seen.append(resolved)
    return seen


def _configured_roots(config: PheasantConfig) -> list[Path]:
    """Browse roots for the UI, *including* configured-but-unmounted ones.

    Unlike ``_allowed_roots`` (which drops non-existent roots because they can
    never contain a selectable path), this preserves every configured root so
    the browser can render a root the operator added to
    ``security.allow_workspace_roots`` but forgot to mount — flagged
    ``mounted: false`` — instead of silently hiding it.
    """
    roots = [
        config.pheasant.workspace_root,
        config.pheasant.exports_path,
        *config.security.allow_workspace_roots,
    ]
    if config.security.allow_user_selected_source_paths:
        roots.append(Path("/"))
    seen: list[Path] = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved not in seen:
            seen.append(resolved)
    return seen


def _config_write_roots(config: PheasantConfig) -> list[Path]:
    """Roots a *config file* may be written into.

    Deliberately not ``_allowed_roots``: that one honors
    ``security.allow_user_selected_source_paths``, which widens the list to
    ``/`` so a user can index any folder they like. Choosing what content to
    index is not the same permission as choosing where the server writes
    YAML, and conflating them turns source promotion into an arbitrary file
    write. Only the explicitly configured roots count here.
    """
    roots = [
        config.pheasant.workspace_root,
        config.pheasant.exports_path,
        *config.security.allow_workspace_roots,
    ]
    seen: list[Path] = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved not in seen:
            seen.append(resolved)
    return seen


def _resolve_source_path(path: str, config: PheasantConfig) -> Path:
    if config.security.allow_user_selected_source_paths:
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            raise PathPolicyError(f"Path does not exist: {resolved}")
        return resolved
    return resolve_under(path, _allowed_roots(config))


def _check_source_type(type_name: str) -> bool:
    """Validate a ``sources[].type`` string; return whether it is built-in.

    YAML accepts any type a connector plugin claims (Step 31.1), so the API
    has to as well — otherwise the same source is configurable in a file and
    rejected through the UI.
    """
    from pheasant.sync.connector_registry import list_connector_types

    if type_name in {member.value for member in SourceType}:
        return True
    plugins = list_connector_types()
    if type_name in plugins:
        return False
    raise HTTPException(
        status_code=400,
        detail=f"Unknown source type: {type_name}. Built-in types: "
        + ", ".join(sorted(member.value for member in SourceType))
        + (
            f". Installed connector plugins: {', '.join(plugins)}"
            if plugins
            else ". No connector plugins are installed."
        ),
    )


def _source_from_payload(payload: dict) -> SourceConfig:
    return PheasantConfig.model_validate({"sources": [payload]}).sources[0]


def _source_payload(
    req: RegisterSourceRequest | UpdateSourceRequest,
    resolved_path: Path,
    existing: SourceConfig | None = None,
) -> dict:
    payload = existing.model_dump(mode="json") if existing else {}
    if isinstance(req, RegisterSourceRequest):
        payload.update({"name": req.name, "type": req.type})
        updates = req.model_dump(exclude={"sync_now", "sync_mode"}, exclude_none=True)
    else:
        updates = req.model_dump(exclude_unset=True)
    updates["path"] = str(resolved_path)
    payload.update(updates)
    return payload


#: Re-exported from the service layer, which now owns the traversal. Kept as
#: module-level names here because `graph_neighbors` and `graph_slice` are
#: imported from this module by name in several places, and because the HTTP
#: routes below read better calling them unqualified. The implementation moved;
#: the spelling did not.
graph_neighbors = graph_service.neighbors
graph_slice = graph_service.slice_


def _shortest_path(
    graph: SimpleMultiDiGraph,
    source: str,
    target: str,
    *,
    max_depth: int = 8,
    max_visited: int = 200_000,
) -> list[str] | None:
    """Fewest hops between two nodes, or None.

    Edges are followed in **both** directions on purpose. "How are these two
    related?" is a question about connectivity, not about which way an import
    happens to point — a file and the concept it mentions are related whether
    you walk `mentions` forwards or backwards, and a direction-respecting
    search reports "no path" for pairs a human would call obviously connected.

    Bidirectional BFS: two frontiers meeting in the middle explore
    O(b^(d/2)) instead of O(b^d), which is the difference between a usable
    answer and a graph walk on a hub-heavy index. ``max_visited`` bounds the
    work on a miss so a bad pair cannot pin the server.
    """
    if source == target:
        return [source]
    with graph.reading():
        adjacency: dict[str, set[str]] = {}
        for (edge_source, edge_target), _edge_map in graph.iter_edges():
            adjacency.setdefault(edge_source, set()).add(edge_target)
            adjacency.setdefault(edge_target, set()).add(edge_source)

    # Parent maps double as visited sets; walking them back at the meeting
    # point is what reconstructs the path.
    forward: dict[str, str | None] = {source: None}
    backward: dict[str, str | None] = {target: None}
    front, back = [source], [target]
    for depth in range(max_depth):
        if not front or not back:
            return None
        if len(forward) + len(backward) > max_visited:
            return None
        # Always expand the smaller frontier: it is what keeps the
        # bidirectional saving rather than degenerating to one deep search.
        expand_forward = len(front) <= len(back)
        frontier, seen, other = (
            (front, forward, backward) if expand_forward else (back, backward, forward)
        )
        next_frontier: list[str] = []
        for node in frontier:
            for neighbour in adjacency.get(node, ()):
                if neighbour in seen:
                    continue
                seen[neighbour] = node
                if neighbour in other:
                    return _join_paths(neighbour, forward, backward)
                next_frontier.append(neighbour)
        if expand_forward:
            front = next_frontier
        else:
            back = next_frontier
        if depth + 1 >= max_depth:
            break
    return None


def _join_paths(
    meeting: str,
    forward: dict[str, str | None],
    backward: dict[str, str | None],
) -> list[str]:
    head: list[str] = []
    cursor: str | None = meeting
    while cursor is not None:
        head.append(cursor)
        cursor = forward.get(cursor)
    head.reverse()
    tail: list[str] = []
    cursor = backward.get(meeting)
    while cursor is not None:
        tail.append(cursor)
        cursor = backward.get(cursor)
    return head + tail


def _edge_type_set(value: str | None) -> set[str] | None:
    items = {item.strip() for item in (value or "").split(",") if item.strip()}
    return items or None


def _resolved_api_token(config: PheasantConfig) -> str:
    """The shared bearer token for this region's API, or "" when there is none.

    Read once at startup rather than per request: it is a process-wide secret,
    and rotating it is a restart — the same contract as every other credential
    here, none of which is ever read out of YAML.
    """

    auth = getattr(getattr(config, "security", None), "api_auth", None)
    name = str(getattr(auth, "token_env", "") or "").strip()
    return (os.environ.get(name, "") or "").strip() if name else ""


def create_app(
    config: PheasantConfig | None = None,
    config_path: str | Path | None = None,
    role: str | None = None,
) -> FastAPI:
    resolved_config_path = (
        str(config_path)
        if config_path
        else os.environ.get("PHEASANT_CONFIG", "/config/pheasant.yaml")
    )
    if config is None:
        config = load_config(resolved_config_path)
    # Phase 35.6: which jobs this process takes on. `all` is the default and
    # is byte-identical to pre-roles behavior; the one that changes anything
    # here is `api`, which publishes index work instead of running it.
    role_policy = resolve_role(config, role)
    validate_role(role_policy, config)
    paths = StatePaths.from_config(config)
    paths.ensure()
    state = StateStore.from_config(config, paths.sqlite)
    state.migrate()
    SourceRegistry(config, state).initialize()
    # Only a standalone process, the dedicated graph service, or a legacy API
    # without a remote graph endpoint keeps the snapshot resident. Indexers
    # coordinate child commits; workers prepare immutable files; remote APIs
    # query the graph service. None of those needs a second graph copy.
    remote_graph = bool(str(config.graph.query_service_url or "").strip())
    # On the row backend only a process that *builds* the graph holds one: a
    # bounded walk is a bounded query there, so an api or graph replica serves
    # from `graph_nodes`/`graph_edges` and never materializes 1.5GB (measured
    # at 100k files) to answer a three-hop neighbourhood. On the whole-file
    # backend there is no such option, and this resolves exactly as it did.
    stores_graph = config.storage.graph_format == "rows"
    builds_graph = role_policy.role in {Role.ALL, Role.INDEXER}
    loads_graph = (
        role_policy.role in {Role.ALL, Role.GRAPH}
        or (role_policy.role is Role.API and not remote_graph)
    ) and (builds_graph or not stores_graph)
    engine = SyncEngine(
        config,
        paths,
        state,
        load_persisted_graph=loads_graph,
        initialize_indexing_components=role_policy.role not in {Role.INDEXER, Role.GRAPH},
    )
    serving_graph = graph_for_config(
        config,
        engine.serving_graph,
        force_local=role_policy.role is not Role.API,
    )
    # One resolver for the whole process: the searcher ranks with it, and
    # applying a bundle invalidates it here so this replica converges at once
    # rather than on its own TTL. Every *other* replica converges on theirs,
    # which is the point of a polled overlay.
    ranking_resolver = resolver_for(config, state)
    search = HybridSearch(
        SearchStore(state, ranking=ranking_resolver),
        vector=engine.vector_searcher(),
        node_index=engine.node_index,
        wasm_relationship_search=config.search.wasm_relationship_search,
        steering_enabled=config.memory.steering_enabled,
        default_memory_policy=config.memory.default_policy,
        usage_tracking=config.memory.usage_tracking,
        stage_sample_rate=config.observability.interactions.stage_sample_rate,
    )

    # The application layer's collaborators, assembled once. Every operation
    # below that has an MCP counterpart takes this and nothing else, which is
    # what makes "the same operation" a fact rather than a convention.
    services = ServiceContext(
        config=config,
        state=state,
        searcher=search,
        graph=serving_graph,
        engine=engine,
    )

    # Built before the lifespan so the lifespan closure can start its session
    # manager: the SDK's streamable-http app is useless without its own
    # lifespan running ("Task group is not initialized"), and a plain
    # app.mount() does not run a sub-app's lifespan.
    mcp_asgi_app = _mcp_asgi_app(config) if role_policy.serves_ui else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio

        import anyio.to_thread

        # `max_concurrent_requests` bounds how many requests
        # `ConcurrencyLimiter` admits before shedding with a 429 (Phase
        # 35.6); anyio's shared worker-thread pool — what every sync `def`
        # HTTP route and every `/mcp` tool call actually runs on — is a
        # *separate* budget, defaulted to 40 by anyio and never sized
        # against this one. Admitting more requests than the pool has
        # tokens re-admits exactly the silent queuing the limiter exists to
        # replace with a fast, explicit 429. Only ever raised, never
        # lowered: this field's job is "shed above N", not "run with fewer
        # worker threads than anyio's own default".
        max_concurrent = cors_settings.max_concurrent_requests
        if max_concurrent > 0:
            pool = anyio.to_thread.current_default_thread_limiter()
            pool.total_tokens = max(pool.total_tokens, max_concurrent)

        # Drain-on-SIGTERM is installed *here*, not before `uvicorn.run()`.
        # uvicorn installs its own graceful-shutdown handler inside `run()`,
        # so a handler installed earlier is simply replaced and never fires.
        # Lifespan startup runs after `capture_signals()`, which is what lets
        # this one chain to uvicorn's rather than clobber it.
        drain_seconds = float(getattr(config.server.api, "drain_seconds", 0) or 0)
        if drain_seconds > 0:
            from pheasant.deployment.serving import install_drain_handler

            try:
                install_drain_handler(app.state.drain, drain_seconds)
            except ValueError:  # pragma: no cover - not the main thread (TestClient)
                logger.debug("drain handler not installed: not running in the main thread")

        # Close out any evaluation batch this process (or a previous one, in a
        # container that was stopped) left in flight. Done at boot rather than
        # only on the scheduler beat because `--role api` never runs the beat,
        # and the API is exactly where somebody is watching a progress bar that
        # would otherwise spin forever for work nobody is doing.
        if config.evaluation.enabled:
            try:
                from pheasant.evaluation.runner import reclaim_interrupted_runs

                reclaimed = reclaim_interrupted_runs(
                    state,
                    config.knowledge_base_id,
                    stale_after_seconds=config.evaluation.run_stale_seconds,
                )
                if reclaimed:
                    logger.info(
                        "Reclaimed %s interrupted evaluation run(s): %s",
                        len(reclaimed),
                        ", ".join(reclaimed),
                    )
            except Exception:  # noqa: BLE001 - recovery must never block a boot
                logger.debug("Evaluation reclamation failed at startup", exc_info=True)

        startup_sources = [
            source.name for source in engine.enabled_sources() if source.sync.on_startup
        ]
        # A Postgres indexer runs this from its elected leader's promotion
        # callback. Doing it here would let every hot standby index before it
        # has won the orchestration lease.
        leader_managed_startup = getattr(app.state, "orchestration", None) is not None
        if startup_sources and role_policy.indexes_locally and not leader_managed_startup:
            loop = asyncio.get_running_loop()

            def _run_startup() -> None:
                from contextlib import nullcontext

                from pheasant.sync.worker import WorkerBackedEngine

                logger.info("Running startup sync for sources: %s", ", ".join(startup_sources))
                # Indexing happens in a child process so it cannot starve the
                # requests this server exists to answer.
                sync_lock = getattr(app.state, "sync_lock", None)
                lock_context = sync_lock if sync_lock is not None else nullcontext()
                with lock_context:
                    results = WorkerBackedEngine(engine, app.state.config_path).startup()
                app.state.startup_sync_results = results
                indexed = sum(result.indexed_artifacts for result in results)
                skipped = sum(result.skipped_artifacts for result in results)
                logger.info(
                    "Startup sync complete: sources=%s indexed=%s skipped=%s",
                    len(results),
                    indexed,
                    skipped,
                )

            loop.run_in_executor(None, _run_startup)

        # Warm the agent framework off the request path. Importing langgraph
        # costs ~4s, and it used to be paid, lazily, by whoever asked the first
        # question after a restart — which read as "the planner is slow" when
        # nothing was planning yet. Best-effort and in the background: a
        # missing [agent] extra just means the simple workflow answers.
        def _warm_workflows() -> None:
            from pheasant.assistant.workflows.agentic import warm

            if warm():
                logger.info("Agent workflow ready (langgraph imported and graph compiled)")

        if role_policy.serves_ui:
            threading.Thread(target=_warm_workflows, name="pheasant-warm", daemon=True).start()
        try:
            if mcp_asgi_app is None:
                yield
            else:
                async with mcp_asgi_app.router.lifespan_context(app):
                    yield
        finally:
            buffer = getattr(app.state, "interaction_buffer", None)
            if buffer is not None:
                app.state.interaction_buffer = None
                try:
                    from pheasant.telemetry.interactions import set_process_buffer

                    set_process_buffer(None)
                    # Drains what is buffered, with a bounded join. A shutdown
                    # that hangs on the log tier is worse than one that loses a
                    # handful of observations.
                    buffer.stop()
                except Exception:  # pragma: no cover - shutdown must not raise
                    logger.debug("could not stop the interaction buffer", exc_info=True)
            held = getattr(app.state, "metrics_queue", None)
            if held is not None:
                app.state.metrics_queue = None
                try:
                    held.close()
                except Exception:  # pragma: no cover - shutdown must not raise
                    logger.debug("could not close the metrics queue handle", exc_info=True)

    app = FastAPI(title="pheasant", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.state = state
    app.state.engine = engine
    app.state.serving_graph = serving_graph
    app.state.config_path = resolved_config_path
    # Chat API keys a user pastes in the browser live here and nowhere else:
    # process memory, TTL'd, gone on restart.
    app.state.session_keys = SessionKeyStore(config.assistant.session_key_ttl_minutes)
    # Background work (`wait=false` on quick-add/register/sync/upload) is
    # tracked in one job registry: in-memory, process lifetime, with a phase,
    # a counter and a terminal outcome per job. The `syncing_sources` /
    # `sync_outcomes` dicts this replaced could only say "true" for however
    # many minutes a first index took, which is indistinguishable from a hang.
    app.state.sync_lock = threading.Lock()
    app.state.metrics_queue = None
    app.state.metrics_queue_lock = threading.Lock()
    # Indexer roles publish atomic progress snapshots here. API replicas mount
    # /state read-only and merge those snapshots into the same HTTP/UI surface
    # as their process-local jobs.
    app.state.jobs = JobRegistry(shared_path=paths.state / "jobs")
    jobs = app.state.jobs
    # Content-addressed results for `/internal/indexing/prepare-batch`, so a
    # coordinator's retry after a timeout is a lookup rather than a second
    # parse. Bounded and LRU: a worker serving a 50k-file source must not
    # accumulate 50k parse results.
    app.state.prepare_cache = ResultCache(PREPARE_CACHE_SIZE)
    metrics.register_default_metrics(__version__)

    # The web UI is a separate workload that talks to this API over HTTP, so the
    # browser origin differs in development — but this API is unauthenticated,
    # so a wildcard here means any page in the user's browser can drive every
    # route on it (read the whole index, rewrite the config, register a source
    # over a sensitive directory). Allow the UI's origins, not the web.
    cors_settings = config.server.api
    if cors_settings.cors_allow_all_origins:
        logger.warning(
            "server.api.cors_allow_all_origins is on: every browser origin may call this "
            "unauthenticated API. Only do this behind an authenticating ingress."
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if cors_settings.cors_allow_all_origins else cors_settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Phase 35.6: shed rather than queue, and drain before dying. Both are
    # replica behaviors — the limiter is off by default because with one
    # process there is nowhere for a shed request to go, so waiting is the
    # best available answer.
    app.state.limiter = ConcurrencyLimiter(cors_settings.max_concurrent_requests)
    app.state.drain = DrainState()
    limiter: ConcurrencyLimiter = app.state.limiter
    drain_state: DrainState = app.state.drain

    # The observation plane (off by default). Built here rather than in the
    # lifespan so a TestClient sees the same wiring a served process does, and
    # so `interaction_buffer` is a stable attribute a route can read without
    # caring whether observation is on.
    app.state.log_queue = None
    app.state.interaction_buffer = _build_interaction_buffer(config, state, app, role_policy)
    interaction_buffer = app.state.interaction_buffer

    @app.middleware("http")
    async def bound_concurrency(request, call_next):  # type: ignore[no-untyped-def]
        """Admit or refuse immediately; never block.

        Blocking is the behavior being replaced. A fast 429 is what lets a
        load balancer try another replica while the client is still waiting;
        a slow one is just a timeout with extra steps.
        """

        path = request.url.path
        if not limiter.acquire(path):
            metrics.REGISTRY.inc("pheasant_requests_shed_total", path=_metric_path(path))
            # A shed request is still an observation: it is exactly the signal
            # that says a formation threshold is being reached more slowly than
            # the traffic suggests, and losing it would hide that.
            _observe_shed(interaction_buffer, request, _metric_path(path), config)
            return JSONResponse(
                status_code=429,
                content={
                    "detail": (
                        f"this replica is at its {limiter.limit}-request concurrency limit; "
                        "retry, ideally against another replica"
                    )
                },
                headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
            )
        try:
            if interaction_buffer is None or _metric_path(path) in UNOBSERVED_PATHS:
                return await call_next(request)
            # One bounded append per request and nothing else. Everything the
            # ledger costs beyond this happens on the flusher thread or on the
            # log tier — see `pheasant.telemetry.interactions`.
            from pheasant.telemetry.interactions import (
                InteractionContext,
                cap_answer,
                observe,
                redact,
            )

            context = InteractionContext.create(
                _request_modality(request),
                principal=request.headers.get("x-pheasant-principal"),
                session_id=request.headers.get("x-pheasant-session"),
                client_id=request.headers.get("x-pheasant-client"),
                traceparent=request.headers.get("traceparent"),
            )
            settings = config.observability.interactions
            with observe(
                interaction_buffer,
                context,
                kb_id=config.pheasant.name,
                operation=_metric_path(path),
            ) as event:
                # The middleware cannot see a request body without consuming
                # the stream, and it cannot see a response body at all. So it
                # publishes the live event and the handlers that know what was
                # asked and what came back fill it in -- see `observed()`.
                # Without this, every UI search recorded a path and a status
                # and no content, and three of the four formation rules would
                # have worked for MCP agents only.
                request.state.interaction = event
                response = await call_next(request)
                event.status = "ok" if response.status_code < 400 else "error"
                event.attributes["http_status"] = response.status_code
                event.attributes["method"] = request.method
                cap_answer(event, max_chars=int(getattr(settings, "max_answer_chars", 4000)))
                redact(event, enabled=bool(getattr(settings, "redact_text", False)))
                return response
        finally:
            limiter.release(path)

    # -- Phase 35.8: the two guards a fleet needs and a container does not ----
    #
    # Both are registered *after* `bound_concurrency`, which in Starlette means
    # they run *before* it: a caller with no token must not take a concurrency
    # slot, and a request to a surface this process does not serve must not
    # become an observation.

    #: What a process with `server.api.enabled: false` still answers. The
    #: probes, so an orchestrator can tell a healthy pod from a dead one;
    #: `/metrics`, so it is still a scrape target; and `/internal/*`, which is
    #: the entire point of such a process — those routes enforce their own
    #: per-boundary bearer tokens and 404 when their feature is off.
    RESTRICTED_PATHS: frozenset[str] = frozenset({"/health", "/ready", "/metrics"})
    RESTRICTED_PREFIX = "/internal/"
    serves_public_api = bool(getattr(config.server.api, "enabled", True))

    if not serves_public_api:

        @app.middleware("http")
        async def restricted_surface(request, call_next):  # type: ignore[no-untyped-def]
            """Make `server.api.enabled: false` mean what it says.

            `deploy/compose/worker.yaml` has declared `api.enabled: false`
            since the worker tier existed, and nothing read it: a preparation
            worker served the whole knowledge-base API — register a source
            over any allow-listed path, rewrite the config, read what it finds
            — on every pod of the tier designed to hold nothing and scale
            hardest. The flag was documentation.

            A 404 rather than a 403 because that is the truth: on this process
            the route does not exist.
            """

            path = request.url.path
            if path not in RESTRICTED_PATHS and not path.startswith(RESTRICTED_PREFIX):
                return JSONResponse(
                    status_code=404,
                    content={
                        "detail": (
                            "this process serves no knowledge-base API "
                            "(server.api.enabled is false); it answers the probes, "
                            "/metrics and its own /internal endpoints"
                        )
                    },
                )
            return await call_next(request)

    api_token = _resolved_api_token(config)
    if api_token:
        expected_token = api_token.encode("utf-8")
        auth_public_paths = frozenset(config.security.api_auth.public_paths or ())

        @app.middleware("http")
        async def require_api_token(request, call_next):  # type: ignore[no-untyped-def]
            """A static shared bearer token, when one is configured.

            Deliberately the whole feature. It is enough to make "behind an
            authenticating ingress" the default rather than the instruction,
            it needs no identity provider, and it cannot rot into a half-built
            one. Anything richer belongs to the ingress —
            `security.api_auth.behind_authenticating_proxy` says so and turns
            this off.

            `/internal/*` is exempt structurally rather than by the configured
            list: each of those routes already enforces the token for its own
            trust boundary (the worker token, the graph token), and requiring
            a second one would mean handing the region's API credential to
            every worker — the shape of the bug this phase removed from the
            Compose file.
            """

            path = request.url.path
            if path.startswith("/internal/") or path in auth_public_paths:
                return await call_next(request)
            scheme, _, supplied = (request.headers.get("authorization") or "").partition(" ")
            # Compared as bytes: Starlette decodes headers as latin-1, so a
            # single byte above 127 in the header makes `compare_digest` raise
            # TypeError on str operands -- a 500 from the auth guard, on input
            # any caller can send.
            if scheme.lower() != "bearer" or not hmac.compare_digest(
                supplied.encode("utf-8", "surrogateescape"), expected_token
            ):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "this region requires a bearer token"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return await call_next(request)

    def _record_stream_answer(parent: Any, req: Any, answer: Any, started: Any) -> None:
        """Observe a streamed answer as a child of the request that opened it.

        The request's own event was buffered the moment this route returned
        its `StreamingResponse`, which is long before there is an answer to
        record -- so this is a second event rather than a late mutation of a
        first, and the trace is what joins them.
        """

        if parent is None or interaction_buffer is None:
            return
        try:
            from pheasant.telemetry.interactions import cap_answer, child_event, redact

            settings = config.observability.interactions
            event = child_event(parent, "/assistant/chat/stream")
            # Timed from the child's own start, not the request's: this span
            # exists precisely to say how long generating the answer took,
            # which is the slowest thing in the region and the only part the
            # request's own span cannot see.
            event.duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
            record_retrieval(
                None,
                query=req.question,
                payload=answer,
                criteria={"workflow": req.workflow} if req.workflow else None,
                answer=str(answer.get("answer") or "") or None,
                event=event,
            )
            cap_answer(event, max_chars=int(getattr(settings, "max_answer_chars", 4000)))
            redact(event, enabled=bool(getattr(settings, "redact_text", False)))
            interaction_buffer.record(event)
        except Exception:  # noqa: BLE001 - an observation must never fail a stream
            logger.debug("could not observe a streamed answer", exc_info=True)

    def _request_modality(request) -> str:  # type: ignore[no-untyped-def]
        """Which surface this call arrived on.

        An explicit header wins; otherwise the `/mcp` mount is the only thing
        that can be told apart from a UI or script call by its path, and
        everything else is `ui` — which is what the existing `audit()` closure
        has always hardcoded. Making it a guess with a header override is
        strictly better than making it a constant, and honest about being a
        guess: nothing here is authenticated.
        """

        declared = request.headers.get("x-pheasant-modality")
        if declared:
            return declared
        return "mcp" if request.url.path.startswith("/mcp") else "ui"

    def _metric_path(path: str) -> str:
        """Collapse to the first segment: a label per artifact id is a leak.

        Prometheus cardinality is the failure mode here — `/nodes/content` is
        a useful label, `/nodes/content?id=<one of 600k>` is a memory leak in
        the scrape target.
        """

        head = path.strip("/").split("/", 1)[0]
        return f"/{head}" if head else "/"

    def audit(source_id: str | None, action: str, details: dict | None = None) -> None:
        created_at = utc_now()
        ordinal = len(state.list_source_audit_events(source_id, limit=10000))
        import hashlib
        import json

        digest = hashlib.sha256(
            json.dumps(
                {"source_id": source_id, "action": action, "created_at": created_at},
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:16]
        state.append_source_audit_event(
            f"audit:{digest}:{ordinal}",
            source_id,
            action,
            "ui",
            "http",
            None,
            created_at,
            details,
        )

    def _loaded_generation() -> dict[str, object]:
        """Which graph this process is answering from. No I/O.

        `loaded` is None on a process that holds no graph — an indexer
        coordinator, a worker, or an API replica pointed at the graph service,
        which is the deployment where the graph service's own probe is the one
        to read.
        """

        payload: dict[str, object] = {"loaded": getattr(engine, "loaded_graph_generation", None)}
        if remote_graph and role_policy.role is Role.API:
            payload["source"] = "graph-service"
        return payload

    def _graph_generation() -> dict[str, object]:
        """The loaded generation *and* what is published, which reads `/state`.

        Two ids rather than one, because one of them cannot say anything on
        its own. A replica that missed a reload serves an old graph correctly
        and silently; publishing the loaded id beside the published id is what
        turns "this region disagrees with itself" from an inference into a
        comparison anyone can make from two probe responses.

        Only `/ready` calls this, and only off the event loop. Reading the
        publication record is two syscalls on a small file — cheap, and not
        free, and `/health` is the liveness probe: a handler that does no I/O
        at all is the reason it answers in 0.05s under a saturated thread pool
        (see its docstring). Staleness belongs on the readiness probe anyway,
        since that is the one that decides whether a replica should be
        receiving traffic.
        """

        published: str | None = None
        try:
            record = engine.graph_store.published_generation(config.knowledge_base_id)
            published = str(record["generation_id"]) if record else None
        except Exception:  # noqa: BLE001 - a probe must never fail on a label
            published = None
        payload = _loaded_generation()
        payload["published"] = published
        loaded = payload.get("loaded")
        if loaded is not None and published is not None:
            payload["current"] = loaded == published
        return payload

    @app.exception_handler(ServiceError)
    async def service_error(_request, exc: ServiceError):  # type: ignore[no-untyped-def]
        """Translate an application-layer refusal into HTTP.

        One handler rather than a `try` in every adapter: the service layer
        raises the same refusal whichever transport called it, and each
        transport decides once how to say it. The MCP side does the mirror of
        this at the SDK boundary in `mcp_server/server.py`.

        The *text* is passed through unchanged, because it is the contract —
        "Unknown source: notes" is what a caller acts on, and an agent told
        only that something failed will retry the same call. The conformance
        test asserts both surfaces produce it identically.

        ``code`` and ``retryable`` ride *beside* the text rather than inside
        it, which is the one place the two surfaces deliberately differ. The
        MCP SDK's `ToolError` carries a string and nothing else, so a code
        spelled into the message would be the only way to get it across — and
        that would change the refusal text on both surfaces to serve one of
        them. Instead the readiness contract publishes the code table, an MCP
        client maps the text it already has, and an HTTP client reads the field.
        The asymmetry is here, in an adapter, where a reader can see it.
        """

        payload = exc.as_dict()
        return JSONResponse(
            status_code=exc.status,
            content={
                "detail": payload["error"],
                "code": payload["code"],
                "retryable": payload["retryable"],
            },
        )

    @app.get("/health")
    async def health() -> dict:
        """Liveness: is this process running at all.

        Carries the role so a pod can be identified from a probe response —
        "which of these five containers is the indexer" is otherwise a
        question you answer by reading manifests.

        ``async def`` — deliberately, though this handler does no I/O at
        all — because a sync ``def`` route is dispatched through anyio's
        shared worker-thread pool exactly like every other sync route, and
        that pool has nothing to do with the shed-exemption below. This is
        the liveness probe: a busy pod that fails it gets restarted by the
        thing meant to protect it. Measured: with the pool's forty tokens
        held by other sync handlers, a sync ``def /health`` took 4.29s to
        answer; this one, unaffected by the pool, took 0.05s.
        """

        payload: dict[str, object] = {
            "status": "ok",
            "service": "pheasant",
            "role": role_policy.name,
            "graph_generation": _loaded_generation(),
        }
        orchestration = getattr(app.state, "orchestration", None)
        if orchestration is not None:
            payload["leader"] = bool(orchestration.leader)
        return payload

    @app.get("/ready")
    async def ready() -> dict:
        """Readiness: can this process do its role's job.

        Distinct from `/health` on purpose, because Kubernetes uses them for
        different things: a failing liveness probe restarts the pod, a failing
        readiness probe takes it out of the Service. A process whose state
        store has gone away should stop receiving traffic, not be restarted
        into the same problem.

        Deliberately **not** gated on the index being populated. An empty
        knowledge base still answers searches correctly (with nothing), and a
        replica that stayed unready through a multi-hour first index would
        take the whole Service down for that time — which is the opposite of
        what the readiness probe is for.

        ``async def`` for the same reason as `/health`: this route is
        exempt from shedding (`deployment/serving.py`'s ``ALWAYS_ALLOWED``),
        and that exemption is worthless if the route still queues for an
        anyio thread-pool token like a non-exempt one would. The one blocking
        call left (`state.rows`) is offloaded explicitly below rather than
        left to make the whole route sync, so a saturated pool can still
        delay this probe briefly on that one call — but never on FastAPI's
        own dispatch, which no longer costs a token at all.
        """

        payload: dict[str, object] = {
            "status": "ready",
            "knowledge_base": config.knowledge_base_id,
            **describe_role(role_policy),
        }
        # ``Role.API`` can refresh a legacy local snapshot, but a remote API
        # deliberately has no local snapshot or refresher. Report the work the
        # running process actually performs rather than the role's capability.
        if role_policy.role is Role.API and remote_graph:
            payload["refreshes_graph"] = False
        if role_policy.role is Role.GRAPH:
            token_env = config.graph.query_service_token_env
            if not os.environ.get(token_env or "", ""):
                payload["status"] = "not_ready"
                payload["reason"] = "graph query service token is not configured"
                return JSONResponse(status_code=503, content=payload)  # type: ignore[return-value]
        if drain_state.draining:
            # SIGTERM has arrived. Reporting not-ready *before* the process
            # stops accepting work is the entire drain mechanism: Kubernetes
            # removes endpoints and sends SIGTERM concurrently, and endpoint
            # propagation is not instant, so a process that exits promptly
            # drops whatever was routed to it in the gap.
            payload["status"] = "draining"
            payload["draining_for_seconds"] = round(drain_state.draining_for, 1)
            return JSONResponse(status_code=503, content=payload)  # type: ignore[return-value]
        orchestration = getattr(app.state, "orchestration", None)
        if orchestration is not None:
            payload["leader"] = bool(orchestration.leader)
            if not orchestration.leader:
                payload["status"] = "standby"
                return JSONResponse(status_code=503, content=payload)  # type: ignore[return-value]
        try:
            # Starlette sends synchronous routes through one bounded AnyIO
            # threadpool. During the stress run that pool saturated while the
            # event loop still accepted TCP, so even exempt probes waited for
            # a worker. Use the event loop's executor with a hard deadline;
            # liveness above never leaves the event loop at all.
            import asyncio

            await asyncio.wait_for(
                asyncio.to_thread(state.rows, "SELECT 1 AS ok", ()),
                timeout=2.0,
            )
        except Exception as exc:  # noqa: BLE001 - any failure means not ready
            logger.warning("readiness probe failed: %s", exc)
            payload["status"] = "not_ready"
            payload["reason"] = "state store unreachable"
            return JSONResponse(status_code=503, content=payload)  # type: ignore[return-value]
        if getattr(serving_graph, "is_remote_graph", False):
            try:
                await asyncio.wait_for(asyncio.to_thread(serving_graph.ping), timeout=2.0)
            except Exception as exc:  # noqa: BLE001 - any failure means not ready
                logger.warning("graph-service readiness probe failed: %s", exc)
                payload["status"] = "not_ready"
                payload["reason"] = "graph query service unreachable"
                return JSONResponse(status_code=503, content=payload)  # type: ignore[return-value]
        # Last, and off the loop like the two probes above: reading the
        # publication record is two syscalls, and a saturated replica must not
        # pay them on the event loop just to label itself. A failure here is
        # not an unready pod -- it is a missing label -- so it degrades to the
        # in-memory half rather than to a 503.
        try:
            payload["graph_generation"] = await asyncio.wait_for(
                asyncio.to_thread(_graph_generation), timeout=2.0
            )
        except Exception:  # noqa: BLE001 - a label must never fail a probe
            payload["graph_generation"] = _loaded_generation()
        return payload

    def _authorize_worker(authorization: str | None) -> None:
        """Gate + constant-time token check shared by both worker routes."""

        concurrency = config.sync.concurrency
        if not concurrency.remote_worker_enabled:
            raise HTTPException(status_code=404, detail="remote indexing worker is disabled")
        import hmac

        from pheasant.sync.remote_worker import RemoteWorkerError, configured_token

        try:
            expected = configured_token(concurrency.remote_worker_token_env)
        except RemoteWorkerError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="invalid indexing worker token")

    def _check_task_size(payload: dict) -> None:
        max_mb = config.sync.limits.max_file_size_mb
        encoded = str(payload.get("content_base64") or "")
        if max_mb is not None and len(encoded) > int(max_mb) * 1024 * 1024 * 4 // 3 + 4:
            raise HTTPException(status_code=413, detail="indexing task exceeds max_file_size_mb")

    @app.post("/internal/indexing/prepare")
    def remote_prepare(
        req: RemotePrepareRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Stateless authenticated parse/chunk worker for remote coordinators."""

        from pheasant.sync.remote_worker import RemoteWorkerError, prepare_task

        _authorize_worker(authorization)
        _check_task_size(req.payload)
        try:
            parsed = prepare_task(req.model_dump())
        except (KeyError, TypeError, ValueError, RemoteWorkerError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"parsed": parsed}

    @app.post("/internal/indexing/prepare-batch")
    def remote_prepare_batch(
        req: RemotePrepareBatchRequest,
        authorization: Annotated[str | None, Header()] = None,
        x_pheasant_deadline_seconds: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Prepare several tasks in one request (Phase 35.5).

        The value is not the round trips saved — though on a large source that
        is most of the transport cost. It is that a batch carries the caller's
        remaining deadline and a content address per task, so a worker can
        decline work whose caller has given up and answer a duplicate from
        cache. Those two together are what make at-least-once retry cheap
        enough to actually do.
        """

        import time

        from pheasant.sync.remote_worker import (
            DeadlineExceeded,
            RemoteWorkerError,
            prepare_batch_tasks,
        )

        _authorize_worker(authorization)
        if not req.tasks:
            return {"results": []}
        if len(req.tasks) > MAX_PREPARE_BATCH:
            raise HTTPException(
                status_code=413,
                detail=f"batch of {len(req.tasks)} exceeds the {MAX_PREPARE_BATCH}-task limit",
            )
        for task in req.tasks:
            _check_task_size(task.payload)

        budget = req.deadline_seconds
        if budget is None:
            try:
                budget = float(x_pheasant_deadline_seconds or "")
            except ValueError:
                budget = None
        if budget is not None and budget <= 0:
            raise HTTPException(status_code=408, detail="caller deadline already passed")
        started = time.monotonic()

        def remaining() -> float | None:
            return None if budget is None else budget - (time.monotonic() - started)

        try:
            results = prepare_batch_tasks(
                [task.model_dump() for task in req.tasks],
                list(req.idempotency_keys or []),
                cache=app.state.prepare_cache,
                deadline=remaining,
            )
        except DeadlineExceeded as exc:
            raise HTTPException(status_code=408, detail=str(exc)) from exc
        except (KeyError, TypeError, ValueError, RemoteWorkerError) as exc:
            # One bad task fails the batch: the coordinator's fallback is to
            # prepare locally, which handles every task the remote path
            # refuses, so isolating the offender would buy nothing.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        cache = app.state.prepare_cache
        return {
            "results": results,
            "cache": {"hits": cache.hits, "misses": cache.misses, "size": len(cache)},
        }

    def _metrics_queue():
        """The scrape's queue handle, created once and reused.

        Under a lock: two concurrent scrapes would otherwise each build one
        and the loser's handle would leak — on `nats` that is a live
        connection nothing ever closes.
        """

        with app.state.metrics_queue_lock:
            queue = getattr(app.state, "metrics_queue", None)
            if queue is None:
                from pheasant.sync.queue import queue_from_config

                queue = queue_from_config(config, state)
                app.state.metrics_queue = queue
            return queue

    @app.get("/metrics")
    async def metrics_endpoint() -> PlainTextResponse:
        """Prometheus exposition text (Phase 35.1).

        Named ``metrics_endpoint``, not ``metrics``: every route in this module
        is a closure over ``create_app``, so a local named ``metrics`` would
        shadow the imported :mod:`pheasant.telemetry.metrics` for *every other
        route in the file* — the search handler would raise ``AttributeError``
        on ``metrics.REGISTRY`` and only at request time.

        Graph size and job state are sampled here rather than tracked
        incrementally: both are cheap to read, and a gauge updated from a
        write path drifts the moment any path forgets to update it.

        ``async def`` for the same reason as `/health` and `/ready`: this
        route is shed-exempt and that exemption means nothing if it still
        queues for an anyio thread-pool token. Only `queue.depth()` is
        genuinely blocking (a SQL query, or on the ``nats`` backend a broker
        round trip through `sync/queue.py`'s own event-loop bridge), so only
        that call is offloaded; the graph and job reads above it are
        in-memory and stay on the loop.
        """

        import anyio.to_thread

        graph = serving_graph
        pool = anyio.to_thread.current_default_thread_limiter()
        sample: dict[str, object] = {
            "pheasant_graph_nodes": graph.number_of_nodes(),
            "pheasant_graph_edges": graph.number_of_edges(),
            "pheasant_requests_inflight": limiter.inflight,
            "pheasant_requests_capacity_remaining": limiter.capacity_remaining,
            "pheasant_draining": 1.0 if drain_state.draining else 0.0,
            # The budget every sync HTTP route and every /mcp tool call
            # shares (see docs/configuration.md's "Serving durability"
            # section). Sits next to pheasant_requests_inflight because a
            # pool that's the actual bottleneck should be visible next to
            # the request-admission count, not just inferred from it.
            "pheasant_threadpool_tokens_total": pool.total_tokens,
            "pheasant_threadpool_tokens_available": pool.available_tokens,
        }
        # Only where this process is the commit authority. On an api replica
        # the meter would report a confident 0.0 from a process that publishes
        # index work rather than running it — a reading that looks like "there
        # is headroom" and means "this is not the tier with the ceiling".
        if role_policy.indexes_locally:
            saturation = engine.commit_meter.saturation()
            if saturation is not None:
                sample["pheasant_commit_authority_saturation"] = saturation
        published_at = getattr(engine, "loaded_graph_published_at", None)
        if published_at is not None:
            sample["pheasant_graph_generation_age_seconds"] = max(
                0.0, time.time() - float(published_at)
            )
        sample.update(jobs.metrics_sample())
        # With the durable queue on, the backlog outlives this process, so
        # the in-memory job registry is no longer the whole truth — an HPA
        # reading only it would see zero while a restarted fleet's rows sit
        # waiting. The queue's own depth wins where it exists (Phase 35.5).
        try:
            from pheasant.sync.queue import DEAD, INFLIGHT, PENDING

            # Held open across scrapes rather than built and closed per
            # request. Prometheus scrapes every 15s by default, per replica,
            # and on the `nats` backend building one meant a full TCP +
            # JetStream connect and teardown each time — a broker connection
            # storm proportional to fleet size, paid to read three integers.
            # Closed by the lifespan's `finally`.
            queue = _metrics_queue()
            if queue is not None:
                depth = await anyio.to_thread.run_sync(queue.depth)
                sample["pheasant_index_queue_depth"] = depth.get(PENDING, 0)
                sample["pheasant_index_inflight"] = depth.get(INFLIGHT, 0)
                sample["pheasant_index_dead_letters"] = depth.get(DEAD, 0)
        except Exception:  # pragma: no cover - a scrape must never 500
            logger.debug("could not sample index queue depth", exc_info=True)
        return PlainTextResponse(
            metrics.render_with(sample),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # The readiness plane's routes register from their own module. `app.py` is
    # the module ratchet's headline, and its docstring names the fix: one
    # router per plane, easier once the service layer exists. It does now for
    # these operations, so this plane starts on the right side of that line
    # rather than adding to the pile.
    register_readiness_routes(app, config=config, services=services, engine=engine)

    @app.get("/contract")
    def contract() -> dict:
        """Return this region's published semantic contract (Synapse 21.5).

        404 until a contract has been published (a sync with
        ``synapse.publish`` on). Standalone regions that never enable
        publishing simply always 404 here.
        """
        from pheasant.synapse.publisher import load_contract_text

        text = load_contract_text(config.pheasant.state_path)
        if text is None:
            raise HTTPException(status_code=404, detail="No contract published yet")
        import json as _json

        return _json.loads(text)

    @app.get("/knowledge-bases")
    def knowledge_bases() -> dict:
        return {"knowledge_bases": KnowledgeBaseRegistry(state).list()}

    def _with_sync_state(records: list[dict]) -> list[dict]:
        """Overlay live job state onto persisted source rows.

        Every route that lists sources carries this, so a client polling any
        of them sees what is running and — via `sync_error` — why the last
        background pass failed. `syncing` alone flickers to nothing the
        instant a job completes, which is exactly when a caller most wants to
        know whether it succeeded. `job` adds the phase and counter behind
        that boolean, which is what turns a spinner into a progress bar.

        `progress` (Phase 35.1) narrows that to *this* source's slice of the
        job. `job` is the whole run, so under `sync_all` every source in the
        list showed the same aggregate counter and the one source that was
        stuck looked exactly like the seven that were fine.
        """
        for record in records:
            name = record.get("name")
            if not name:
                continue
            active = jobs.active_snapshot_for(name)
            last = jobs.last_outcome_snapshot_for(name)
            record["syncing"] = active is not None
            record["sync_error"] = last.get("error") if last else None
            record["job"] = active
            record["progress"] = jobs.source_progress(name)
        return records

    @app.get("/sources")
    def sources() -> list[dict]:
        return _with_sync_state(SourceRegistry(config, state).list_sources())

    @app.get("/sources/types")
    def source_types() -> dict:
        """Every source type this deployment can register, built-in or plugin.

        The form that creates sources has to offer exactly what YAML accepts,
        which on a machine with connector plugins installed is more than the
        built-in enum. ``path_role`` tells a caller whether the schema's
        mandatory ``path`` is real for that type or just a placeholder the
        connector never reads.
        """
        from pheasant.sync.connector_registry import list_connector_types

        types = [
            {
                "id": type_id,
                "label": label,
                "description": description,
                "path_role": path_role,
                "builtin": True,
            }
            for type_id, label, description, path_role in BUILTIN_SOURCE_TYPES
        ]
        types.extend(
            {
                "id": name,
                "label": name,
                "description": "Connector plugin (pheasant.connectors entry point).",
                # A plugin pulls from its own service; the path field is
                # schema ceremony unless the plugin documents otherwise.
                "path_role": "unused",
                "builtin": False,
            }
            for name in list_connector_types()
        )
        return {"types": types, "placeholder_path": PLUGIN_PLACEHOLDER_PATH}

    @app.post("/sources")
    def register_source(req: RegisterSourceRequest) -> dict:
        is_builtin = _check_source_type(req.type)
        # A plugin connector reads from its own service, not the filesystem,
        # so the schema's mandatory path is a placeholder — don't make the
        # caller invent a real directory to register a Notion or Slack
        # source. A path that *is* supplied still goes through path policy.
        if not is_builtin and req.path.strip() in {"", PLUGIN_PLACEHOLDER_PATH}:
            resolved = Path(PLUGIN_PLACEHOLDER_PATH)
        else:
            try:
                resolved = _resolve_source_path(req.path, config)
            except PathPolicyError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            source = _source_from_payload(_source_payload(req, resolved))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid source: {exc}") from exc
        SourceRegistry(config, state).register_source(source)
        config.sources = [s for s in config.sources if s.name != source.name]
        config.sources.append(source)
        audit(source.name, "register_source", {"source": source.model_dump(mode="json")})
        result = None
        syncing = False
        job_id = None
        queued: list[str] = []
        if req.sync_now:
            if req.wait:
                try:
                    result = _index(source.name, req.sync_mode)["results"][0]
                except (KeyError, ValueError) as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            else:
                job_id, queued = _start_background_sync(source.name, req.sync_mode)
                syncing = True
        return {
            "status": "registered",
            "knowledge_base": config.knowledge_base_id,
            "source": source.model_dump(mode="json"),
            "sync_result": result,
            "syncing": syncing,
            "job_id": job_id,
            "queued_tasks": queued,
            "config_update_required": True,
        }

    @app.put("/sources/{source_id}")
    def update_source(source_id: str, req: UpdateSourceRequest) -> dict:
        source = next((s for s in config.sources if s.name == source_id), None)
        if source is None:
            row = state.get_source(source_id)
            if not row:
                raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
            import json

            source = _source_from_payload(json.loads(row["config_json"]))
        # An edit that doesn't touch the type must not re-validate it: a
        # plugin that got uninstalled shouldn't lock its source out of the
        # form. Only a type the caller actually sends is checked.
        is_builtin = (
            _check_source_type(req.type)
            if req.type is not None
            else str(source.type) in {member.value for member in SourceType}
        )
        if not is_builtin and (req.path or "").strip() in {"", PLUGIN_PLACEHOLDER_PATH}:
            resolved = Path(PLUGIN_PLACEHOLDER_PATH)
        else:
            try:
                resolved = _resolve_source_path(req.path, config) if req.path else source.path
            except PathPolicyError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            updated = _source_from_payload(_source_payload(req, resolved, existing=source))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid source: {exc}") from exc
        config.sources = [s for s in config.sources if s.name != source_id]
        config.sources.append(updated)
        queued: list[str] = []
        if not role_policy.is_default and config.sync.queue.enabled:
            # The API/indexer coordinator graph mount is read-only or not
            # loaded. Carry the replacement config with the durable operation
            # so a fresh child does not fall back to stale static YAML.
            SourceRegistry(config, state).register_source(updated)
            queued = _publish_background_sync(
                [source_id],
                "incremental",
                task_payload={
                    "operation": "replace_source",
                    "source": updated.model_dump(mode="json"),
                },
            )
        else:
            engine.replace_source(updated.model_dump(mode="json"))
        audit(source_id, "update_source", {"source": updated.model_dump(mode="json")})
        return {
            "status": "update_queued" if queued else "updated",
            "source": updated.model_dump(mode="json"),
            "queued_tasks": queued,
            "config_update_required": True,
        }

    @app.post("/sources/{source_id}/disable")
    def disable_source(source_id: str) -> dict:
        if not state.get_source(source_id):
            raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
        state.set_source_enabled(source_id, False, "disabled")
        for source in config.sources:
            if source.name == source_id:
                source.enabled = False
        audit(source_id, "disable_source")
        return {"status": "disabled", "source_name": source_id}

    @app.delete("/sources/{source_id}")
    def remove_source(source_id: str) -> dict:
        if not state.get_source(source_id):
            raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
        queued: list[str] = []
        if not role_policy.is_default and config.sync.queue.enabled:
            # Disable immediately so no scheduler can enqueue more work while
            # the ordered writer task removes the published generation.
            state.set_source_enabled(source_id, False, "removal_queued")
            queued = _publish_background_sync(
                [source_id],
                "incremental",
                task_payload={"operation": "delete_source"},
            )
        else:
            engine.remove_source(source_id)
        config.sources = [s for s in config.sources if s.name != source_id]
        audit(source_id, "remove_source")
        return {
            "status": "removal_queued" if queued else "removed",
            "source_name": source_id,
            "queued_tasks": queued,
            "config_update_required": True,
        }

    @app.post("/sources/{source_id}/promote")
    def promote_source(source_id: str, req: PromoteSourceRequest | None = None) -> dict:
        req = req or PromoteSourceRequest()
        source = next((s for s in config.sources if s.name == source_id), None)
        if source is None:
            row = state.get_source(source_id)
            if not row:
                raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
            import json

            payload = json.loads(row["config_json"])
            source = PheasantConfig.model_validate({"sources": [payload]}).sources[0]
        source_payload = source.model_dump(mode="json")
        yaml_patch = dump_config_yaml({"sources": [source_payload]})
        wrote = False
        # `config_path` is caller-supplied: constrain it to this server's own
        # config (or an allowed root) so promotion cannot be used to write a
        # YAML file anywhere the process can reach.
        try:
            path = resolve_config_write_target(
                req.config_path,
                server_config_path=app.state.config_path,
                allowed_roots=_config_write_roots(config),
            )
        except PathPolicyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        target = str(path)
        if req.write:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(data, dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"Refusing to overwrite {path}: it is not a pheasant config mapping",
                )
            existing = [
                item for item in data.get("sources", []) or [] if item.get("name") != source.name
            ]
            data["sources"] = [*existing, source_payload]
            path.write_text(dump_config_yaml(data), encoding="utf-8")
            wrote = True
        audit(source_id, "promote_runtime_source_to_config", {"write": wrote, "path": target})
        return {
            "status": "promoted" if wrote else "patch_generated",
            "source_name": source_id,
            "yaml_patch": yaml_patch,
            "wrote_config": wrote,
            "config_path": target,
        }

    @app.get("/sources/{source_id}/repo-map")
    def repo_map(source_id: str) -> dict:
        """Transport adapter. The operation is `services.graph.repo_map`."""

        return graph_service.repo_map(services, source_id)

    @app.get("/sources/{source_id}/history")
    def source_history(source_id: str, limit: int = 100, offset: int = 0) -> dict:
        return {
            "events": state.list_source_audit_events(source_id, limit, offset),
            "pagination": {"limit": limit, "offset": offset},
        }

    def _index(
        source_name: str | None,
        mode: str,
        depth: int | None = None,
        full_scan: bool = False,
    ) -> dict:
        """Index in a worker process, then pick up the result.

        The server deliberately does not index in-process: that work is
        CPU-bound Python and, under the GIL, it starves the very requests this
        API exists to answer. See :mod:`pheasant.sync.worker`.

        A role that does not index refuses here rather than silently doing it
        anyway. `wait: true` asks this process to run a sync and return the
        result, and an api replica cannot honour that — it can only publish,
        which is a different promise. Saying so with a 409 and the fix is
        better than either lying or quietly changing the contract.
        """

        if not role_policy.indexes_locally:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"role '{role_policy.name}' does not index; retry with wait=false to "
                    "queue this sync for an indexer to run"
                ),
            )

        from pheasant.sync.worker import WorkerBackedEngine

        worker = WorkerBackedEngine(engine, app.state.config_path)
        if source_name:
            results = [worker.sync_source(source_name, mode, max_depth=depth, full_scan=full_scan)]
        else:
            results = worker.sync_all(mode, max_depth=depth, full_scan=full_scan)
        failed = [r for r in results if r.status in {"failed", "timeout"}]
        if failed:
            raise HTTPException(
                status_code=500,
                detail=failed[0].details.get("error") or f"sync {failed[0].status}",
            )
        return {"results": [r.__dict__ for r in results]}

    def _run_background_sync(source_name: str | None, mode: str, job_id: str) -> None:
        """The `wait=false` path: same worker-subprocess sync as `_index`, off
        the request thread, reporting into the job registry as it goes.

        The job is guaranteed to reach a terminal state on every exit path —
        success, sync failure, or an unexpected exception — so a source can
        never get stuck showing "syncing" forever.
        """
        from pheasant.sync.worker import WorkerBackedEngine

        def forward(event: dict) -> None:
            meta = event.get("meta") or {}
            jobs.progress(
                job_id,
                phase=event.get("phase"),
                current=event.get("current"),
                total=event.get("total"),
                detail=event.get("detail"),
                source=meta.get("source") or None,
                stats=meta,
            )

        try:
            jobs.progress(
                job_id,
                phase="waiting_for_indexer",
                detail="Queued for the index writer",
            )
            worker = WorkerBackedEngine(engine, app.state.config_path)
            if source_name:
                results = [worker.sync_source(source_name, mode, on_progress=forward)]
            else:
                results = worker.sync_all(mode, on_progress=forward)
        except Exception as exc:  # background thread — never propagate, just record
            logger.exception("background sync failed")
            jobs.finish(job_id, "failed", error=str(exc))
            return
        failed = [r for r in results if r.status in {"failed", "timeout", "limit_exceeded"}]
        jobs.finish(
            job_id,
            "failed" if failed else "succeeded",
            error=(failed[0].details.get("error") or failed[0].status) if failed else None,
            result={
                "results": [
                    {
                        "source_id": r.source_id,
                        "indexed_artifacts": r.indexed_artifacts,
                        "skipped_artifacts": r.skipped_artifacts,
                        "status": r.status,
                    }
                    for r in results
                ]
            },
        )

    def _background_status(job_id: str | None, queued: list[str]) -> str:
        """Three outcomes, three words — `syncing` used to cover all of them.

        `queued` is not `syncing`: nothing is indexing yet, an indexer has to
        claim the task first, and there is no local job to watch.
        """

        if job_id:
            return "syncing"
        return "queued" if queued else "already_syncing"

    def _start_background_sync(source_name: str | None, mode: str) -> tuple[str | None, list[str]]:
        """Start a background sync. Returns ``(job_id, queued_task_ids)``.

        ``job_id`` is None when one is already running over the same source,
        **and** when this process does not index: an ``api`` replica publishes
        to the queue, and a queue task is not a job. Returning its id in the
        ``job_id`` field made every caller poll ``GET /jobs/<task id>``, which
        404s — the registry is in-process and the task belongs to an indexer
        in another pod. The ids are reported separately, under their own name,
        so the response says what actually happened instead of implying
        progress the api role cannot observe.

        Refusing the overlap matters: found live, a source with
        `sync.on_startup: true` and no checkpoint yet (so every attempt does a
        full, expensive pass) got resynced by both a container-startup trigger
        and a manual `wait: false` trigger landing close together — two
        concurrent embedding-heavy syncs over the same source, which tripped
        the embeddings endpoint's rate limit and multiplied disk and CPU work
        for no benefit. The check-and-claim happens under one lock acquisition
        here, not inside the thread, so it is atomic against a second caller
        racing in before the first thread has even started.
        """
        names = [source_name] if source_name else [s.name for s in engine.enabled_sources()]
        if not role_policy.indexes_locally:
            return None, _publish_background_sync(names, mode)
        with app.state.sync_lock:
            to_start = [name for name in names if jobs.active_for(name) is None]
            if not to_start:
                return None, []
            job = jobs.create(
                "sync",
                f"Indexing {source_name}" if source_name else "Indexing all sources",
                to_start,
            )
        threading.Thread(
            target=_run_background_sync,
            args=(source_name, mode, job.id),
            name=f"pheasant-bgsync-{source_name or 'all'}",
            daemon=True,
        ).start()
        return job.id, []

    def _publish_background_sync(
        names: list[str],
        mode: str,
        *,
        task_payload: dict[str, Any] | None = None,
    ) -> list[str]:
        """Hand the work to an indexer instead of running it here.

        This is what the ``api`` role *is*. Three api replicas that each
        indexed on request would put three processes on one source; publishing
        means an available indexer claims each task, and the caller still gets
        an id to poll. Source leases make at-least-once broker delivery safe.

        Logical task ids are content-addressed on (knowledge base, source,
        mode), the same rule ``sync_all`` uses. The local queue suppresses an
        outstanding duplicate; JetStream suppresses network retries of one
        publish invocation, while source leases remain the final guard if two
        API replicas intentionally submit distinct invocations.
        """

        import hashlib

        from pheasant.sync.queue import IndexTask, queue_from_config
        from pheasant.telemetry.interactions import traceparent_header

        queue = queue_from_config(config, state)
        if queue is None:  # pragma: no cover - validate_role refuses this at startup
            raise HTTPException(
                status_code=503,
                detail="this process does not index and no queue is configured",
            )
        published: list[str] = []
        try:
            for name in names:
                payload = {
                    "max_depth": None,
                    "full_scan": False,
                    **dict(task_payload or {}),
                }
                digest = hashlib.sha256(
                    (
                        f"{config.knowledge_base_id}\0{name}\0{mode}\0"
                        + json.dumps(payload, sort_keys=True, separators=(",", ":"))
                    ).encode()
                ).hexdigest()[:24]
                # Attached *after* the digest, never before: the task id is
                # content-addressed over the payload so two replicas answering
                # one double-click enqueue one task, and a per-request value in
                # there would defeat that entirely. A task already pending
                # keeps the first requester's trace, which is honest -- that is
                # the request the run belongs to.
                traceparent = traceparent_header()
                if traceparent:
                    payload = {**payload, "traceparent": traceparent}
                task = IndexTask(
                    id=f"idx-{digest}",
                    source_id=name,
                    mode=str(mode),
                    payload=payload,
                    max_attempts=max(1, int(config.sync.queue.max_attempts or 1)),
                )
                # A broker failure is not a duplicate and must reach the API
                # caller. Pretending this publish succeeded creates a job that
                # remains "running" forever with no message behind it.
                queue.publish(task)
                published.append(task.id)
        finally:
            queue.close()
        return published

    @app.post("/sync")
    def sync_all(req: SyncRequest) -> dict:
        if not req.wait:
            job_id, queued = _start_background_sync(None, req.mode)
            return {
                "status": _background_status(job_id, queued),
                "job_id": job_id,
                "queued_tasks": queued,
                "sources": [s.name for s in engine.enabled_sources()],
            }
        try:
            return _index(req.source_name, req.mode, req.depth, req.full_scan)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/sync/{source_id}")
    def sync_source(source_id: str, req: SyncRequest | None = None) -> dict:
        mode = req.mode if req else "incremental"
        if req is not None and not req.wait:
            # A source registered at runtime (quick-add, POST /sources) lives
            # in the state registry, not necessarily yet in `config.sources`
            # — SyncEngine._source lazily pulls it in from there on first
            # sync. Validating against `config.sources` alone would 404 a
            # perfectly syncable source (e.g. right after a restart, before
            # anything has re-triggered that lazy load).
            known = any(s.name == source_id for s in config.sources) or bool(
                state.get_source(source_id)
            )
            if not known:
                raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
            job_id, queued = _start_background_sync(source_id, mode)
            return {
                "status": _background_status(job_id, queued),
                "job_id": job_id,
                "queued_tasks": queued,
                "source_id": source_id,
            }
        try:
            report = _index(
                source_id,
                mode,
                req.depth if req else None,
                req.full_scan if req else False,
            )
            # This route has always returned one result object, not a list.
            results = report.get("results") or []
            return results[0] if results else report
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/sources/upload")
    async def upload_documents(
        files: Annotated[list[UploadFile], File()],
        source_name: Annotated[str, Form()] = "uploads",
        sync_now: Annotated[bool, Form()] = True,
        wait: Annotated[bool, Form()] = False,
    ) -> dict:
        """Index documents dropped into the UI, with no filesystem setup.

        The files land in a directory under ``/state/uploads`` which is
        registered as an ordinary ``document_folder`` source — so they flow
        through the same connector → chunk → graph pipeline as everything
        else, get the same idempotent re-sync, and can be removed by deleting
        the source. There is deliberately no second ingestion path.

        Uploading again into the same source name adds to it rather than
        replacing it, which is what "drop a few more files in" should mean.
        """
        import anyio.to_thread

        from pheasant.api.uploads import safe_filename, store_upload, upload_root

        if not files:
            raise HTTPException(status_code=400, detail="No files uploaded")
        name = safe_filename(source_name or "uploads", fallback="uploads")
        directory = upload_root(Path(config.pheasant.state_path), name)
        limits = config.sync.limits
        max_bytes = (limits.max_file_size_mb or 0) * 1024 * 1024 or None

        # Reading each upload's body is genuine async I/O and stays on the
        # event loop. Everything after it — the disk write, the source
        # registry, the audit log, and (wait=True) the whole sync pipeline —
        # is blocking, synchronous work, so it moves onto a worker thread
        # below. Left on the loop, one slow upload stalls every other
        # request this process is serving: measured, a single wait=true
        # upload delayed a *concurrently issued* GET /ready by the same ~5s
        # the upload itself took.
        pairs: list[tuple[str | None, bytes]] = [
            (upload.filename, await upload.read()) for upload in files
        ]

        def _finish_upload() -> dict:
            stored: list[dict] = []
            rejected: list[dict] = []
            for filename, data in pairs:
                try:
                    record = store_upload(
                        directory,
                        filename or "upload",
                        data,
                        max_bytes=max_bytes,
                    )
                except ValueError as exc:
                    # One bad file must not lose the good ones in the same drop.
                    rejected.append({"filename": filename, "error": str(exc)})
                    continue
                stored.append(record.__dict__)
            if not stored:
                raise HTTPException(
                    status_code=400,
                    detail="; ".join(item["error"] for item in rejected) or "Nothing stored",
                )

            registry = SourceRegistry(config, state)
            existing = next((s for s in config.sources if s.name == name), None)
            if existing is None:
                source = _source_from_payload(
                    {
                        "name": name,
                        "type": "document_folder",
                        "path": str(directory),
                        "description": f"Documents uploaded through the UI ({name})",
                        # Uploads are arbitrary documents, not a code tree: the
                        # default include list is code-shaped and would silently
                        # drop a dropped PDF or .docx.
                        "include": ["**/*"],
                    }
                )
                registry.register_source(source)
                config.sources = [s for s in config.sources if s.name != name]
                config.sources.append(source)
            audit(name, "upload_documents", {"files": [item["filename"] for item in stored]})

            syncing = False
            job_id = None
            queued: list[str] = []
            sync_result = None
            if sync_now:
                if wait:
                    try:
                        sync_result = _index(name, "incremental")["results"][0]
                    except (KeyError, ValueError) as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
                else:
                    job_id, queued = _start_background_sync(name, "incremental")
                    syncing = job_id is not None or bool(queued)
            return {
                "status": "stored",
                "source_name": name,
                "path": str(directory),
                "stored": stored,
                "rejected": rejected,
                "syncing": syncing,
                "job_id": job_id,
                "queued_tasks": queued,
                "sync_result": sync_result,
            }

        return await anyio.to_thread.run_sync(_finish_upload)

    @app.get("/fs/host-path")
    def fs_host_path(path: str) -> dict:
        """Can pheasant see this path — and if not, exactly how to fix it.

        The most common first-run failure is a real host path that simply is
        not mounted into the container. Answering "does not exist" there is
        true and useless; this returns the bind mount, the ``docker run``
        flag and the ``allow_workspace_roots`` entry needed to make it work.
        """
        from pheasant.deployment.mounts import in_container, resolve_host_path

        report = resolve_host_path(path)
        report["in_container"] = in_container()
        report["allowed"] = False
        if report.get("container_path"):
            try:
                _resolve_source_path(report["container_path"], config)
                report["allowed"] = True
            except PathPolicyError as exc:
                report["policy_error"] = str(exc)
        return report

    @app.post("/sources/{source_id}/scan")
    def scan_source(source_id: str, depth: int | None = None) -> dict:
        """Estimate what a source would index, without indexing it.

        The pre-flight for the "point it at anything" workflow: file count,
        size, where the weight sits, how many files each depth cap admits,
        and whether the configured limits would refuse the sync.
        """
        try:
            return engine.scan_source(source_id, max_depth=depth)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/memory")
    def memory_write(req: MemoryWriteRequest) -> dict:
        from pheasant.memory.reinforcement import StateReinforcementIndex
        from pheasant.memory.store import MemoryStore, memory_source

        source = memory_source(config, state)
        if source is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "no enabled memory source configured; add a `type: memory` "
                    "source to pheasant.yaml"
                ),
            )
        # Phase 1: see the matching comment in mcp_server/tools.py:memory_write.
        reinforcement = (
            StateReinforcementIndex(state) if config.memory.reinforcement_enabled else None
        )
        try:
            store = MemoryStore(source.path)
            record, created = store.append(
                req.text,
                scope=req.scope,
                subject=req.subject,
                supersedes=req.supersedes,
                tags=req.tags,
                kind=req.kind,
                written_by=req.principal,
                valid_until=req.valid_until,
                reinforcement=reinforcement,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            # A memory source pointed at somewhere unwritable — most often a
            # read-only corpus mount — used to surface as a bare 500 with the
            # traceback only in the container logs. The caller can act on this.
            raise HTTPException(
                status_code=500,
                detail=(
                    f"could not write the memory record to {source.path}: {exc}. "
                    "The memory source needs a writable location; a read-only mount "
                    "will not do."
                ),
            ) from exc
        outcome = store.last_outcome or ("created" if created else "duplicate")
        metrics.record_memory_write(outcome, store.last_fold)
        payload: dict = {
            "record": record.as_dict(),
            "created": created,
            "source": source.name,
            "outcome": outcome,
        }
        submitted = (req.text or "").strip()
        if not created and submitted and submitted != record.text:
            payload["submitted_text"] = submitted
        # The record is already durably on disk; this sync only makes it
        # *searchable now*. Failing the whole request when it cannot run — most
        # often because another writer holds the engine lease, which a live run
        # hit immediately — reports "your memory was not saved" when it was.
        # Degrade to deferred indexing instead: the scheduler and the next sync
        # both pick it up.
        if req.sync and created:
            if role_policy.indexes_locally:
                try:
                    # Interactive writes skip whole-graph enrichment and leave
                    # a durable dirty marker for the next ordinary sync.
                    payload["sync"] = engine.sync_source(
                        source.name, "incremental", enrich="deferred"
                    ).__dict__
                except Exception as exc:
                    logger.warning("memory write indexed later: %s", exc)
                    payload["sync_deferred"] = str(exc)
            else:
                _job_id, queued = _start_background_sync(source.name, "incremental")
                payload["queued_tasks"] = queued
        # Writing a memory is a mutation of what this region will later assert
        # as fact, which is exactly what the audit log is for. The MCP path has
        # always recorded it; this one silently did not, so the same action was
        # traceable or not depending on which protocol the caller happened to
        # use.
        audit(
            source.name,
            "memory_write",
            {
                "record_id": record.record_id,
                "scope": record.scope,
                "kind": record.kind,
                "created": created,
            },
        )
        return payload

    @app.get("/memory")
    def memory_list(scope: str | None = None, current_only: bool = False) -> dict:
        from pheasant.memory.store import MemoryStore, memory_source

        source = memory_source(config, state)
        if source is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "no enabled memory source configured; add a `type: memory` "
                    "source to pheasant.yaml"
                ),
            )
        try:
            records = MemoryStore(source.path).list_records(scope, current_only=current_only)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Phase 3: see the matching comment in mcp_server/tools.py:memory_list.
        try:
            compaction = {str(row["record_id"]): row for row in state.memory_compaction_rows()}
        except Exception:
            compaction = {}
        out = []
        for record in records:
            payload = record.as_dict()
            row = compaction.get(record.record_id)
            payload["tier"] = str(row["tier"]) if row and row.get("tier") else "hot"
            payload["subsumed_by"] = row.get("subsumed_by") if row else None
            out.append(payload)
        return {"source": source.name, "records": out}

    @app.post("/memory/enable")
    def memory_enable(req: MemoryEnableRequest) -> dict:
        """Provision the agent-memory source (Step 33.11).

        `SourceType.memory` is deliberately absent from `BUILTIN_SOURCE_TYPES`
        — memory sources are owned by the store, not hand-registered through
        the generic source picker, and letting someone point one at an
        arbitrary folder full of unrelated Markdown would make every file in it
        look like something an agent asserted.

        But "not in the picker" had come to mean **unreachable**: a person
        could not turn memory on from the UI at all. A dedicated action keeps
        the invariant (one well-known layout, created by us) while closing that
        gap. Idempotent — enabling twice returns the existing source.
        """
        from pheasant.memory.store import memory_source

        existing = memory_source(config, state)
        if existing is not None:
            return {"status": "already-enabled", "source": existing.model_dump(mode="json")}

        if req.path is None:
            # `/state` is the one location pheasant owns and can always write.
            path = Path(config.pheasant.state_path) / "memory"
        else:
            # An explicit relative path anchors to `pheasant.workspace_root`,
            # exactly as config load anchors any FILESYSTEM_SOURCE_TYPE. Going
            # through `_resolve_source_path` instead resolves against the
            # *process CWD*, which put the folder wherever the server happened
            # to be started from — during development that was the repo
            # checkout, and a record was written into it.
            requested = Path(req.path).expanduser()
            path = (
                requested
                if requested.is_absolute()
                else Path(config.pheasant.workspace_root) / requested
            )
            if not config.security.allow_user_selected_source_paths:
                try:
                    path = resolve_under(str(path), _allowed_roots(config))
                except PathPolicyError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Prove it is writable *before* registering. Otherwise enabling
        # "succeeds" and every later write fails — which is exactly what a
        # read-only corpus mount produced on a live run.
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".pheasant-write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"cannot write memory records to {path}: {exc}. Memory is the one "
                    "source pheasant writes to, so it needs a writable location — a "
                    "read-only corpus mount will not do. Pass an explicit `path`, or "
                    "mount a writable volume."
                ),
            ) from exc

        try:
            source = _source_from_payload(
                {"name": req.name, "type": "memory", "path": str(path), "enabled": True}
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid source: {exc}") from exc

        SourceRegistry(config, state).register_source(source)
        # Same step every other registration takes: the engine reads sources off
        # the live config object, so a source that only reached the registry is
        # invisible to sync.
        config.sources = [s for s in config.sources if s.name != source.name]
        config.sources.append(source)
        audit(source.name, "memory_enable", {"path": str(path)})
        return {"status": "enabled", "source": source.model_dump(mode="json")}

    @app.get("/memory/candidates")
    def memory_candidates(
        status: str = "pending",
        rule_id: str | None = None,
        principal: str | None = None,
        limit: int = 100,
    ) -> dict:
        """Proposals formation has made, awaiting a decision.

        Evidence, not memory: nothing listed here is retrievable, and nothing
        becomes retrievable until it is promoted. `principal` narrows to what
        one caller may see -- a candidate carrying a writer is that principal's
        business, because the record it would become is scoped to them.
        """

        return {
            "candidates": state.list_memory_candidates(
                status=status or None,
                rule_id=rule_id,
                principal=principal,
                limit=max(1, min(int(limit), 500)),
            ),
            "counts": state.memory_candidate_counts(),
        }

    @app.get("/memory/candidates/{candidate_id}/evidence")
    def memory_candidate_evidence(candidate_id: str) -> dict:
        """Why this was proposed: the calls behind it, and their spans.

        Three layers, because a reviewer asks three questions in order. *What
        is being claimed* is the candidate itself. *On what basis* is the set
        of interactions — what was asked, what came back. *How do I check it*
        is the trace: each call's span, its parent, when it ran and how long
        it took, which is what lets somebody follow one proposal back through
        the collector to the request that produced it.

        Evidence can age out from under a pending proposal — the hot window is
        retention-bounded — so this reports what survives and says how much is
        missing rather than failing.
        """

        candidate = state.get_memory_candidate(candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail=f"Unknown memory candidate: {candidate_id}")
        try:
            evidence = json.loads(candidate.get("evidence_json") or "{}")
        except (TypeError, ValueError):
            evidence = {}
        event_ids = [str(item) for item in (evidence.get("event_ids") or [])]
        interactions = state.interaction_events_by_id(event_ids)
        return {
            "candidate": candidate,
            "evidence": evidence,
            "interactions": interactions,
            # The head of the id list is what was recorded; `named` vs
            # `found` is how a reviewer knows the trail is partial rather
            # than assuming the rule only ever saw this much.
            "named": len(event_ids),
            "found": len(interactions),
        }

    @app.post("/memory/candidates/{candidate_id}/promote")
    def memory_candidate_promote(candidate_id: str, principal: str | None = None) -> dict:
        """Admit one proposal. **This is the crossing** from evidence to memory.

        It writes through the ordinary `MemoryStore.append`, so the result is
        an ordinary record in an ordinary file, indexed by the ordinary
        pipeline -- there is no second ingestion path here any more than there
        is for a record a person typed.
        """

        from pheasant.memory.formation import admit

        try:
            return admit(engine, candidate_id, admitted_by=principal or "ui")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/memory/candidates/{candidate_id}/reject")
    def memory_candidate_reject(candidate_id: str, principal: str | None = None) -> dict:
        """Decline one proposal, permanently.

        A rejection is a decision: the rule that proposed it will not suggest
        it again. Re-proposing what somebody already declined is the fastest
        way to make a review queue worth ignoring.
        """

        from pheasant.memory.formation import reject

        try:
            return reject(engine, candidate_id, rejected_by=principal or "ui")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc

    @app.post("/evaluation/evidence")
    def evaluation_evidence(req: EvidenceRequest) -> dict:
        """Record one typed piece of interaction evidence.

        The only way proof enters the system. Everything else the evaluation
        plane knows is *derived* -- exposure from the interaction ledger, an
        explicit accept from an admitted memory candidate -- and derivation
        can only ever say what the region did, never whether it helped.
        """

        import pheasant.evaluation as evaluation

        try:
            return evaluation.record_evidence(
                state,
                config,
                query=req.query,
                target_id=req.target_id,
                target_type=req.target_type,
                event_type=req.event_type,
                interaction_id=req.interaction_id,
                principal=req.principal,
                session_id=req.session_id,
                position=req.position,
                outcome_reference=req.outcome_reference,
                reason_code=req.reason_code,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/evaluation/taxonomy")
    def evaluation_taxonomy() -> dict:
        """Every event type, its polarity and strength, and what it licenses.

        Served so a caller instrumenting a surface can pick the honest event
        rather than guessing, and so the two defaults that matter most --
        exposure is not success, non-selection is not a negative -- are stated
        where an integrator will actually read them.
        """

        from pheasant.evaluation.proof import DEFAULT_TAXONOMY

        return {
            "events": [
                {
                    "event_type": kind.event_type,
                    "polarity": str(kind.polarity),
                    "strength": str(kind.strength),
                    "note": kind.note,
                }
                for kind in DEFAULT_TAXONOMY.values()
            ],
            "defaults": {
                "unknown_is_negative": bool(config.evaluation.proof.unknown_is_negative),
                "non_selection_is_negative": bool(
                    config.evaluation.proof.non_selection_is_negative
                ),
            },
        }

    @app.post("/evaluation/run")
    def evaluation_run(req: EvaluationRunRequest) -> dict:
        """Start a batch as a background job and return its id.

        A run replays every cohort through the real search path once per
        variant, which is minutes of work on a real corpus -- far past what a
        request should hold open. It goes through the same job registry a first
        index uses, so the UI, the CLI and an agent all watch it the same way.

        The run takes the evaluation lease, so several API replicas pointed at
        one `/state` produce one run rather than N. It never takes `sync_lock`:
        a replay inside the scheduler's lock would stall incremental sync for
        every source in the region.
        """

        import pheasant.evaluation as evaluation

        if not config.evaluation.enabled and not req.force:
            raise HTTPException(
                status_code=400,
                detail=(
                    "evaluation.enabled is false. Enable it in pheasant.yaml, or pass "
                    "force=true for a one-off run."
                ),
            )
        job = app.state.jobs.create(
            "evaluation", f"evaluation ({req.mode})", targets=["evaluation"]
        )

        def _run() -> None:
            try:
                outcome = evaluation.run(
                    engine,
                    mode=req.mode,
                    effective_as_of=req.as_of,
                    force=bool(req.force),
                    on_progress=lambda phase, detail: app.state.jobs.progress(
                        job.id, phase=phase, detail=detail or phase, source="evaluation"
                    ),
                )
                app.state.jobs.finish(
                    job.id,
                    status="succeeded" if outcome.status != "invalid" else "failed",
                    result={
                        "run_id": outcome.run_id,
                        "snapshot_id": outcome.snapshot_id,
                        "status": outcome.status,
                        "gates_passed": outcome.gates_passed,
                        "skipped_reason": outcome.skipped_reason,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - a job records its own failure
                app.state.jobs.finish(job.id, status="failed", error=str(exc))

        threading.Thread(target=_run, name="pheasant-evaluation", daemon=True).start()
        return {"job_id": job.id, "status": "running", "mode": req.mode}

    @app.get("/evaluation/report")
    def evaluation_report(run: str | None = None) -> dict:
        """The most recent report, or a named one."""

        import pheasant.evaluation as evaluation
        from pheasant.evaluation import store as evaluation_store

        report = (
            evaluation_store.load_report(state, run)
            if run
            else evaluation.latest_report(state, config.knowledge_base_id)
        )
        if report is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No evaluation report for {run}"
                    if run
                    else "No evaluation run has completed yet"
                ),
            )
        return report

    @app.get("/evaluation/runs")
    def evaluation_runs(limit: int = 20) -> dict:
        from pheasant.evaluation import store as evaluation_store

        return {
            "runs": evaluation_store.list_runs(
                state, config.knowledge_base_id, limit=max(1, min(int(limit), 200))
            )
        }

    # ---- the tuning plane ------------------------------------------------
    #
    # Shaped after the evaluation routes above, and for the same reasons: a
    # batch is minutes of work so starting one returns a job id, and every
    # read comes off `/state` rather than an in-process registry so a browser
    # talking to a replica that did not start the batch still sees it.

    @app.post("/tuning/run")
    def tuning_run(req: TuningRunRequest) -> dict:
        """Start a tuning batch as a background job.

        Read-only unless ``apply`` is set. The batch takes the ``__tuning__``
        lease, so several replicas pointed at one `/state` produce one batch;
        it never takes ``sync_lock``; and it stands down while the index queue
        has work in it, because indexing is somebody waiting and this is a
        measurement.
        """

        import pheasant.tuning as tuning

        if not config.tuning.enabled and not req.force:
            raise HTTPException(
                status_code=400,
                detail=(
                    "tuning.enabled is false. Enable it in pheasant.yaml, or pass "
                    "force=true for a one-off batch."
                ),
            )
        label = "tuning (diagnose)" if req.diagnose_only else "tuning"
        job = app.state.jobs.create("tuning", label, targets=["tuning"])

        def _run() -> None:
            try:
                kwargs: dict = {"force": True}
                if req.diagnose_only:
                    from pheasant.tuning.strategy import Budget

                    # A diagnosis is a batch with no trial budget: the same
                    # code path, stopped after the first movement.
                    kwargs["budget"] = Budget(
                        refusion_trials=0, requery_trials=0, max_searches=10_000
                    )
                if req.apply:
                    engine.config.tuning.auto.apply = True
                try:
                    outcome = tuning.run(
                        engine,
                        on_progress=lambda phase, detail: app.state.jobs.progress(
                            job.id, phase=phase, detail=detail or phase, source="tuning"
                        ),
                        **kwargs,
                    )
                finally:
                    if req.apply:
                        # Restored whatever the outcome: a request-scoped
                        # override must not silently become the region's
                        # standing policy for every later batch.
                        engine.config.tuning.auto.apply = bool(config.tuning.auto.apply)
                if outcome.applied:
                    ranking_resolver.invalidate()
                app.state.jobs.finish(
                    job.id,
                    status="succeeded" if outcome.status != "failed" else "failed",
                    result={
                        "experiment_id": outcome.experiment_id,
                        "status": outcome.status,
                        "outcome": outcome.decision.outcome if outcome.decision else "",
                        "bundle_id": outcome.bundle_id,
                        "applied": outcome.applied,
                        "gates_passed": outcome.gates_passed,
                        "skipped_reason": outcome.skipped_reason,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - a job records its own failure
                app.state.jobs.finish(job.id, status="failed", error=str(exc))

        threading.Thread(target=_run, name="pheasant-tuning", daemon=True).start()
        return {"job_id": job.id, "status": "running"}

    @app.get("/tuning/status")
    def tuning_status(experiment: str | None = None) -> dict:
        """What a tuning batch is doing right now, read from `/state`."""

        import pheasant.tuning as tuning

        payload = tuning.progress(state, config.knowledge_base_id, experiment)
        payload["enabled"] = bool(config.tuning.enabled)
        payload["auto_enabled"] = bool(config.tuning.auto.enabled)
        payload["auto_apply"] = bool(config.tuning.auto.apply)
        payload["tracking_backend"] = str(config.tuning.tracking.backend)
        return payload

    @app.get("/tuning/report")
    def tuning_report(experiment: str | None = None) -> dict:
        """The most recent tuning report, or a named one."""

        from pheasant.tuning import store as tuning_store

        row = (
            tuning_store.experiment_status(state, experiment)
            if experiment
            else tuning_store.latest_experiment(state, config.knowledge_base_id)
        )
        report = (row or {}).get("report")
        if not report:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No tuning report for {experiment}"
                    if experiment
                    else "No tuning batch has completed yet"
                ),
            )
        return report

    @app.get("/tuning/experiments")
    def tuning_experiments(limit: int = 20) -> dict:
        from pheasant.tuning import store as tuning_store

        return {
            "experiments": tuning_store.list_experiments(
                state, config.knowledge_base_id, limit=max(1, min(int(limit), 200))
            )
        }

    @app.get("/tuning/parameters")
    def tuning_parameters() -> dict:
        """What this region is ranking with, where it came from, and what is tunable.

        The first question an operator looking at an unexpected ranking has to
        answer, and it must be answerable without running anything.
        """

        import pheasant.tuning as tuning
        from pheasant.tuning import glossary
        from pheasant.tuning import objective as objective_module
        from pheasant.tuning.bundle import as_config_fragment
        from pheasant.tuning.space import ParameterSpace

        active = tuning.active_parameters(state, config.knowledge_base_id, config)
        space = ParameterSpace(pinned=frozenset(config.tuning.pinned_parameters or ()))
        return {
            "active": active,
            "space": space.as_dict(),
            "config_fragment": as_config_fragment(active),
            "objective": objective_module.resolve(config.tuning.objective).as_dict(),
            # Shipped with the values rather than behind a second request, so a
            # surface rendering a parameter always has its meaning in hand.
            "explanations": {entry["term"]: entry for entry in glossary.catalog()["parameters"]},
        }

    @app.get("/tuning/bundles")
    def tuning_bundles(limit: int = 20) -> dict:
        from pheasant.tuning import store as tuning_store

        return {
            "bundles": tuning_store.list_bundles(
                state, config.knowledge_base_id, limit=max(1, min(int(limit), 200))
            )
        }

    @app.post("/tuning/bundles/apply")
    def tuning_apply(req: TuningApplyRequest) -> dict:
        """Make a bundle the fleet's live retrieval overlay.

        Fleet-scoped: one row, and every replica reading this `/state` resolves
        it within its refresh window. There is deliberately no per-principal or
        per-request variant of this endpoint — retrieval parameters that varied
        by caller would make two agents disagree about what the region holds.
        """

        from pheasant.tuning import store as tuning_store

        try:
            payload = tuning_store.apply_bundle(
                state,
                config.knowledge_base_id,
                req.bundle_id,
                applied_by=req.applied_by or "api",
            )
        except KeyError as exc:
            # 404 rather than a silent success: an unknown id must reach the
            # caller as a refusal they can read, not a no-op that leaves
            # ranking unchanged and reports that it changed.
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        ranking_resolver.invalidate()
        return {"applied": True, "bundle": payload}

    @app.post("/tuning/bundles/rollback")
    def tuning_rollback(req: TuningRollbackRequest | None = None) -> dict:
        """Stand the active overlay down, to the base or an earlier bundle."""

        from pheasant.tuning import store as tuning_store

        target = (req.to if req else "base") or "base"
        if target != "base" and not tuning_store.load_bundle_row(
            state, config.knowledge_base_id, target
        ):
            raise HTTPException(status_code=404, detail=f"Unknown bundle: {target}")
        reverted = tuning_store.revert_bundle(
            state, config.knowledge_base_id, applied_by="api", to=target
        )
        ranking_resolver.invalidate()
        return {"reverted": reverted is not None, "bundle": reverted, "to": target}

    @app.get("/tuning/lineage")
    def tuning_lineage(limit: int = 50) -> dict:
        """Every configuration this region has served, newest first.

        The answer to "what changed, when, and what were we serving before" —
        which is the question people ask after a regression, at exactly the
        moment the active row no longer holds it.
        """

        from pheasant.tuning import store as tuning_store

        return {
            "lineage": tuning_store.lineage(
                state, config.knowledge_base_id, limit=max(1, min(int(limit), 200))
            ),
            "base_explanation": (
                "The oldest state is the configured base — `search.ranking` in "
                "the mounted pheasant.yaml. Every entry below it is a bundle "
                "that was promoted over it, and rolling back returns to the "
                "base or to any earlier entry."
            ),
        }

    @app.get("/tuning/glossary")
    def tuning_glossary(term: str | None = None) -> dict:
        """What every measure on this surface means, and what it does not.

        Served rather than documented because documentation a reader has to go
        and find arrives after the mistake. Each entry says what the number
        is, what to do if it moves, and — the field that is usually missing —
        the misreading it invites.
        """

        from pheasant.tuning import glossary

        if term:
            entry = glossary.lookup(term)
            if entry is None:
                raise HTTPException(status_code=404, detail=f"No explanation for {term!r}")
            return entry
        return glossary.catalog()

    @app.get("/tuning/objectives")
    def tuning_objectives() -> dict:
        """The definitions of "better" this region can tune for.

        Every entry states what it *trades away* as well as what it
        optimizes: an objective without a stated trade is a preference
        presented as an optimum, and picking one by its name alone is how a
        region ends up optimizing for a caller it does not have.
        """

        from pheasant.tuning import objective as objective_module

        return {
            "active": objective_module.resolve(config.tuning.objective).as_dict(),
            "available": objective_module.catalog(),
            "configured_by": "tuning.objective.metric, or tuning.objective.weights",
        }

    @app.get("/tuning/health")
    def tuning_health(since: str | None = None, limit: int = 5000) -> dict:
        """Pipeline behaviour on live traffic, between batches.

        Read from the sampled stage digests on the interaction ledger, so it
        needs no cohort, no replay and no proof — and consequently says what
        the pipeline *did*, never whether an answer was correct. Its value is
        as a change detector: an empty-rate that moves after a bundle is
        applied is a fact about the bundle, visible without waiting for the
        next batch.
        """

        from pheasant.tuning.health import stage_health

        payload = stage_health(
            state,
            config.knowledge_base_id,
            since=since,
            limit=max(1, min(int(limit), 50_000)),
        )
        from pheasant.tuning import glossary

        payload["sample_rate"] = config.observability.interactions.stage_sample_rate
        payload["observation_enabled"] = bool(config.observability.interactions.enabled)
        # Every rate arrives with what it means and, more usefully, the
        # misreading it invites — a truncation rate of 42% looks alarming and
        # is normal, and that is the single most common misreading here.
        payload["explanations"] = {
            entry["term"]: entry for entry in (*glossary.HEALTH, *glossary.STAGES)
        }
        return payload

    @app.get("/tuning/trials")
    def tuning_trials(experiment: str | None = None, limit: int = 500) -> dict:
        """Every trial in an experiment, for charting.

        This is what makes experiment observability native rather than a link
        to another system: `/state` already holds the parameter point, the
        score, the motivating stage and the rationale for every trial, so the
        sweep an operator wants to look at is a query, not an export.
        """

        from pheasant.tuning import store as tuning_store

        experiment_id = experiment or (
            (tuning_store.latest_experiment(state, config.knowledge_base_id) or {}).get(
                "experiment_id"
            )
        )
        if not experiment_id:
            return {"experiment_id": "", "trials": [], "sweeps": {}, "primary_metric": ""}
        trials = tuning_store.load_trials(state, experiment_id)
        rows = sorted(
            (t for t in trials.values() if not t["failed"]),
            key=lambda t: -(t["metrics"].get(tuning_store.PRIMARY_METRIC) or 0.0),
        )
        return {
            "experiment_id": experiment_id,
            "primary_metric": tuning_store.PRIMARY_METRIC,
            "trials": rows[: max(1, min(int(limit), 2000))],
            # Pre-grouped per parameter so the UI plots a sweep without having
            # to know which parameter each trial moved — that is in the delta,
            # and re-deriving it per render is work the server does once.
            "sweeps": _tuning_sweeps(rows, tuning_store.PRIMARY_METRIC),
        }

    @app.post("/tuning/cancel")
    def tuning_cancel(req: TuningCancelRequest) -> dict:
        """Ask the running batch to stand down at its next checkpoint.

        Lands as a column, not a signal to a thread: the replica serving this
        request is usually not the one running the batch. The batch stops with
        its trials already stored, so a cancel is resumable rather than
        destructive.
        """

        import pheasant.tuning as tuning
        from pheasant.tuning import store as tuning_store

        experiment_id = req.experiment_id or (
            tuning.progress(state, config.knowledge_base_id) or {}
        ).get("experiment_id")
        if not experiment_id:
            raise HTTPException(status_code=404, detail="No tuning batch is running")
        cancelled = tuning_store.request_cancel(
            state,
            config.knowledge_base_id,
            experiment_id,
            requested_by=req.requested_by or "api",
        )
        if not cancelled:
            raise HTTPException(
                status_code=409,
                detail=f"{experiment_id} is not running, so there is nothing to cancel",
            )
        return {"cancelled": True, "experiment_id": experiment_id}

    @app.patch("/tuning/pinned")
    def tuning_pin(req: TuningPinRequest) -> dict:
        """Pin parameters so no batch proposes them.

        Applies to the running process and is persisted with the rest of the
        config, so it survives a restart — a pin that evaporated on redeploy
        would be re-litigated by the next scheduled batch, which is the thing
        pinning exists to prevent.
        """

        from pheasant.search.ranking import PARAMETER_STAGES

        unknown = sorted(set(req.pinned) - set(PARAMETER_STAGES))
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Not ranking parameters: {', '.join(unknown)}",
            )
        config.tuning.pinned_parameters = sorted(set(req.pinned))
        # Persisted as well as applied. A pin that evaporated on the next
        # redeploy would be silently re-litigated by the following scheduled
        # batch, which is precisely what pinning exists to prevent.
        wrote = _merge_into_config_file(
            {"tuning": {"pinned_parameters": config.tuning.pinned_parameters}}
        )
        return {"pinned": config.tuning.pinned_parameters, "persisted": wrote}

    @app.delete("/tuning/experiments/{experiment_id}")
    def tuning_prune(experiment_id: str) -> dict:
        """Delete a finished experiment and its trials.

        Bundles survive on purpose: one of them may be the live overlay, and
        erasing the provenance of the configuration a fleet is serving is a
        worse outcome than keeping a row that points at a pruned experiment.
        """

        from pheasant.tuning import store as tuning_store

        removed = tuning_store.prune_experiment(state, config.knowledge_base_id, experiment_id)
        if not removed["experiments"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{experiment_id} is unknown or still running; a running batch must be "
                    "cancelled before it can be pruned"
                ),
            )
        return {"pruned": experiment_id, "removed": removed}

    @app.get("/evaluation/status")
    def evaluation_status(run: str | None = None) -> dict:
        """What a batch is doing right now — read from `/state`, not a process.

        This is the endpoint a progress bar polls, and the reason it reads the
        database rather than the in-process job registry is the two cases that
        actually happen: a browser talking to an API replica that did not start
        the run, and a watcher reconnecting after the container that *was*
        running it stopped. An in-memory registry answers neither.

        A batch whose heartbeat expired reports `interrupted`, with what it had
        finished and how far it got — never a spinner nobody will ever stop.
        """

        import pheasant.evaluation as evaluation

        payload = evaluation.progress(state, config.knowledge_base_id, run)
        payload["enabled"] = bool(config.evaluation.enabled)
        payload["promotion_enabled"] = bool(config.evaluation.promotion.enabled)
        payload["auto_trigger"] = bool(config.evaluation.on_material_snapshot)
        return payload

    @app.get("/evaluation/cohorts")
    def evaluation_cohorts(limit: int = 20) -> dict:
        """The query sets the last runs used, and what each one is for.

        Served because a reader looking at a number needs to know which
        questions produced it — and because a cohort that is empty (no anchor
        yet, no holdout yet) explains an `insufficient_evidence` far better
        than the metric can.
        """

        from pheasant.evaluation import store as evaluation_store

        cohorts = evaluation_store.list_cohorts(
            state, config.knowledge_base_id, limit=max(1, min(int(limit), 100))
        )
        return {
            "cohorts": [
                {
                    "cohort_id": cohort.cohort_id,
                    "name": cohort.name,
                    "purpose": cohort.purpose,
                    "query_count": cohort.query_count,
                    "frozen": cohort.frozen,
                    "created_at": cohort.created_at,
                    "window_start": cohort.window_start,
                    "window_end": cohort.window_end,
                }
                for cohort in cohorts
            ]
        }

    @app.get("/evaluation/metrics")
    def evaluation_metrics(
        run: str | None = None,
        metric: str | None = None,
        cohort: str | None = None,
        variant: str | None = None,
        limit: int = 200,
    ) -> dict:
        """Per-query rows behind an aggregate: the audit trail, on demand.

        The report carries the aggregates; this is how a reader gets from one of
        them to the queries, ranks and proof events that produced it without
        downloading every per-query row in the document.
        """

        import pheasant.evaluation as evaluation
        from pheasant.evaluation import store as evaluation_store

        resolved = run
        if not resolved:
            latest = evaluation.latest_run(state, config.knowledge_base_id)
            resolved = str(latest["run_id"]) if latest else None
        if not resolved:
            raise HTTPException(status_code=404, detail="No evaluation run has completed yet")
        return {
            "run_id": resolved,
            "results": evaluation_store.metric_rows(
                state,
                resolved,
                metric_id=metric,
                cohort_purpose=cohort,
                variant_id=variant,
                limit=max(1, min(int(limit), 1000)),
            ),
        }

    @app.get("/evaluation/trend")
    def evaluation_trend(
        metric: str = "known_positive_reciprocal_rank",
        cohort: str = "anchor",
        variant: str = "B5",
        limit: int = 50,
    ) -> dict:
        """One metric's history across snapshots, oldest first.

        Defaults to the anchor cohort because it is the only one whose
        membership is frozen: a rolling trend moves for two reasons at once and
        cannot separate "the region changed" from "the questions changed".
        """

        import pheasant.evaluation as evaluation

        return {
            "metric_id": metric,
            "cohort": cohort,
            "variant": variant,
            "points": evaluation.trend(
                state,
                config.knowledge_base_id,
                metric,
                cohort_name=cohort,
                variant_id=variant,
                limit=max(1, min(int(limit), 500)),
            ),
        }

    @app.post("/memory/consolidate")
    def memory_consolidate() -> dict:
        from pheasant.memory.maintenance import run_memory_maintenance

        result = run_memory_maintenance(engine)
        if result is None:
            # 200 with a reason, matching the MCP tool exactly. This used to be
            # a 400, which made the same condition an error over HTTP and a
            # normal outcome over MCP — and it is not a client error: the
            # request was well-formed and the server declined because of its
            # own configuration. A scheduler polling this endpoint should not
            # have to treat "consolidation is switched off" as a failure.
            return {
                "skipped": (
                    "memory consolidation is disabled or no `type: memory` source is configured"
                )
            }
        audit(result["source"], "memory_consolidate", result["report"])
        return result

    @app.post("/memory/synthesize")
    def memory_synthesize() -> dict:
        """LLM-merge a near-duplicate memory cluster deterministic
        compaction could not resolve into one canonical record. Off by
        default (`memory.synthesis.enabled`) and never automatic — see
        `MemorySynthesisSettings`. 200 with `{"skipped": reason}` when
        synthesis is disabled, no memory source is configured, or no model
        is reachable — matching `POST /memory/consolidate`'s posture: this
        is a well-formed request the server declined by its own
        configuration, not a client error.
        """
        from pheasant.memory.store import MemoryStore, memory_source
        from pheasant.memory.synthesis import run_synthesis

        source = memory_source(config, state)
        if source is None:
            return {"skipped": "no `type: memory` source is configured"}
        records = MemoryStore(source.path).list_records()
        result = run_synthesis(engine, records, config.memory.synthesis, source)
        audit(source.name, "memory_synthesize", result)
        return result

    @app.post("/security/idp/sync")
    def idp_sync() -> dict:
        from pheasant.security.idp import run_idp_sync

        try:
            return run_idp_sync(config, state)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/security/idp/status")
    def idp_sync_status() -> dict:
        from pheasant.security.idp import idp_status

        return idp_status(config, state)

    # ------------------------------------------------------------------
    # Jobs — everything running in the background, with real progress.
    # ------------------------------------------------------------------
    @app.get("/jobs")
    def list_jobs(active: bool = False, limit: int = 50) -> dict:
        records = jobs.list(active_only=active, limit=limit)
        return {
            "jobs": records,
            "active_count": sum(1 for job in records if job["active"]),
        }

    # Registered BEFORE /jobs/{job_id}: FastAPI matches routes in declaration
    # order, so the parameterised path would otherwise capture "stream" as a
    # job id and answer this with a 404.
    @app.get("/jobs/stream")
    def stream_jobs():
        """Server-sent events, one per job update.

        The alternative — polling `/jobs` on a timer — is the wrong shape for
        something that changes hundreds of times over a few minutes and then
        not at all for an hour.

        Deliberately an **async** generator polling snapshots, not a sync
        generator blocking on ``queue.get(timeout=...)``. In fleet mode the
        writer is another process, so an in-process subscriber can never see
        its updates; the shared atomic snapshots are the source of truth for
        both local and remote jobs. Cancellation occurs at the next sleep.
        """
        import asyncio
        import json as json_module

        from starlette.responses import StreamingResponse

        poll_seconds = 1.0
        ping_every = 4.0

        async def publish():
            seen: dict[str, str] = {}
            since_ping = ping_every
            while True:
                changed = False
                for job in jobs.list(limit=50):
                    signature = json_module.dumps(job, sort_keys=True)
                    if seen.get(job["id"]) == signature:
                        continue
                    seen[job["id"]] = signature
                    changed = True
                    yield f"data: {json_module.dumps({'type': 'job', 'job': job})}\n\n"
                if changed:
                    since_ping = 0.0
                elif since_ping >= ping_every:
                    since_ping = 0.0
                    yield ": ping\n\n"
                await asyncio.sleep(poll_seconds)
                since_ping += poll_seconds

        return StreamingResponse(
            publish(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.delete("/jobs")
    def clear_finished_jobs() -> dict:
        return {"cleared": jobs.clear()}

    @app.delete("/jobs/{job_id}")
    def clear_job(job_id: str) -> dict:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
        if job["active"]:
            raise HTTPException(status_code=409, detail="A running job cannot be cleared")
        return {"cleared": jobs.clear(job_id)}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
        return job

    @app.get("/sync/status")
    def sync_status() -> dict:
        return {
            "sources": SourceRegistry(config, state).list_sources(),
            "checkpoints": state.list_source_checkpoints(),
        }

    @app.post("/search")
    def search_context(req: SearchRequest, request: Request) -> dict:
        """Transport adapter. The operation is `services.retrieval.search`.

        Everything that decides *what* a search returns — over-fetch, criteria,
        the memory policy, the metric, the graph generation — lives there, so
        the MCP tool of the same name cannot answer differently. What is left
        here is the two things that are genuinely HTTP's: turning a request
        model into the service's request, and recording the interaction against
        the `Request` that carried it.
        """

        payload = retrieval_service.search(
            services,
            retrieval_service.SearchRequest(
                query=req.query,
                knowledge_base=req.knowledge_base,
                mode=req.mode,
                max_results=req.max_results,
                source_name=req.source_name,
                section=req.section,
                principal=req.principal,
                principal_groups=req.principal_groups,
                memory=req.memory,
                exclude_sources=req.exclude_sources,
                node_types=req.node_types,
                min_score=req.min_score,
                source_types=req.source_types,
                exclude_source_types=req.exclude_source_types,
                snapshot_id=req.snapshot_id,
                as_of=req.as_of,
                trace_id=req.trace_id,
            ),
        )
        record_retrieval(
            request,
            query=req.query,
            payload=payload,
            criteria={"mode": req.mode, **(payload.get("criteria") or {})},
        )
        return payload

    @app.post("/relevant-files")
    def relevant_files(req: SearchRequest, request: Request) -> dict:
        """Transport adapter. The operation is `services.retrieval.relevant_files`."""

        answer = retrieval_service.relevant_files(
            services,
            retrieval_service.FilesRequest(
                task=req.query,
                knowledge_base=req.knowledge_base,
                max_files=req.max_results,
                source_name=req.source_name,
                section=req.section,
                principal=req.principal,
                principal_groups=req.principal_groups,
                memory=req.memory,
            ),
        )
        record_retrieval(request, query=req.query, payload=answer, criteria={"mode": "hybrid"})
        return answer

    def _acl_guard(
        artifact_id: str | None,
        principal: str | None,
        principal_groups: list[str] | None = None,
    ) -> None:
        """Adapter for `services.graph.require_readable`.

        The rule itself moved to the service layer so the MCP surface enforces
        it too. This wrapper stays only because the remaining `/nodes/content`
        callers are HTTP-only operations not yet extracted; the refusal it
        raises is translated by the handler above, like every other.
        """

        graph_service.require_readable(services, artifact_id, principal, principal_groups)

    @app.get("/files/summary")
    def file_summary(
        path: str,
        source_name: str | None = None,
        principal: str | None = None,
    ) -> dict:
        """Transport adapter. The operation is `services.graph.file_summary`.

        The ACL check moved with it: it was an HTTP-only guard, so the MCP tool
        of the same name served artifact rows without one.
        """

        return graph_service.file_summary(services, path, source_name, principal)

    @app.get("/nodes/content")
    def node_content(node_id: str, principal: str | None = None) -> dict:
        graph = serving_graph
        attrs = graph.nodes.get(node_id)
        if attrs is None:
            raise HTTPException(status_code=404, detail=f"Unknown node: {node_id}")
        node = dict(attrs)
        if node.get("type") == "chunk":
            rows = state.rows(
                "SELECT artifact_id, text FROM chunks WHERE id=? LIMIT 1",
                (node_id,),
            )
            if rows:
                _acl_guard(str(rows[0]["artifact_id"]), principal)
            return {"node_id": node_id, "content": rows[0]["text"] if rows else None}
        _acl_guard(node_id, principal)
        artifact_rows = state.rows("SELECT path FROM artifacts WHERE id=? LIMIT 1", (node_id,))
        if artifact_rows:
            path = Path(artifact_rows[0]["path"])
            if path.exists() and path.is_file():
                content = read_text(path)
                if content:
                    return {"node_id": node_id, "content": content}
        # GROUP_CONCAT ignores a trailing ORDER BY, so order the rows in a
        # subquery to keep the reassembled file content in chunk order.
        rows = state.rows(
            "SELECT GROUP_CONCAT(text, '\n\n') AS content FROM "
            "(SELECT text FROM chunks WHERE artifact_id=? ORDER BY chunk_index)",
            (node_id,),
        )
        content = rows[0]["content"] if rows else None
        return {"node_id": node_id, "content": content}

    @app.get("/taxonomy")
    def taxonomy(
        source: str | None = None,
        path: str | None = None,
        max_nodes: int = 2000,
    ) -> dict:
        """The structural outline of taxonomy-enabled documents.

        Reads the `heading` nodes the sync emitted (it does not re-parse), so
        the tree served here is exactly what the graph and the chunks'
        `heading_path` agree on. Nests by the `level` each heading carries,
        which is the same nesting the graph's `contains` edges encode —
        rebuilt here rather than traversed so one document's outline is one
        response, in document order.

        `path` filters to a single document; `source` to one source. A source
        with taxonomy disabled simply has no heading nodes, so it returns an
        empty tree rather than an error.
        """
        from pheasant.ingestion.taxonomy import (
            Ordinal,
            SectionHeading,
            reconcile_issues,
            taxonomy_tree,
        )

        graph_obj = serving_graph
        remote = getattr(graph_obj, "remote_taxonomy", None)
        if callable(remote):
            return remote(source=source, path=path, max_nodes=max_nodes)
        by_document: dict[str, list[SectionHeading]] = {}
        seen = 0
        for _node_id, data in graph_obj.node_map().items():
            if data.get("type") != "heading":
                continue
            if source and data.get("source_id") != source:
                continue
            relative = str(data.get("relative_path") or "")
            if path and relative != path:
                continue
            seen += 1
            if seen > max(1, max_nodes):
                break
            parts = tuple(int(p) for p in (data.get("ordinal_parts") or []))
            ordinal = (
                Ordinal(
                    parts=parts,
                    series=str(data.get("ordinal_series") or ""),
                    raw=str(data.get("number") or ""),
                    relative=bool(data.get("ordinal_relative")),
                    suffix=str(data.get("ordinal_suffix") or ""),
                )
                if parts
                else None
            )
            by_document.setdefault(relative, []).append(
                SectionHeading(
                    line=int(data.get("start_line") or 0),
                    level=int(data.get("level") or 1),
                    number=data.get("number"),
                    title=str(data.get("title") or ""),
                    kind=str(data.get("kind") or ""),
                    path=str(data.get("heading_path") or ""),
                    pattern_level=int(data.get("pattern_level") or 0),
                    ordinal=ordinal,
                )
            )

        documents = []
        for relative in sorted(by_document):
            headings = sorted(by_document[relative], key=lambda h: h.line)
            issues = reconcile_issues(headings)
            documents.append(
                {
                    "relative_path": relative,
                    "heading_count": len(headings),
                    "tree": taxonomy_tree(headings),
                    # Gaps / duplicates / out-of-order numbering. For contracts
                    # and procedures "is anything missing?" is the question
                    # people actually ask of a document, and once ordinals are
                    # parsed it is nearly free to answer.
                    "issues": issues,
                }
            )
        return {
            "documents": documents,
            "heading_count": sum(d["heading_count"] for d in documents),
            "issue_count": sum(len(d["issues"]) for d in documents),
            "truncated": seen > max(1, max_nodes),
        }

    @app.get("/graph")
    def graph(
        limit: int | None = None,
        link_limit: int | None = None,
        types: str | None = None,
        exclude_types: str | None = None,
        source: str | None = None,
    ) -> dict:
        def _set(value: str | None) -> set[str] | None:
            items = {t.strip() for t in (value or "").split(",") if t.strip()}
            return items or None

        return node_link(
            serving_graph,
            node_limit=limit,
            link_limit=link_limit,
            node_types=_set(types),
            exclude_node_types=_set(exclude_types),
            source_id=source,
        )

    @app.get("/graph/export/node-link-json")
    def graph_node_link() -> dict:
        return node_link(serving_graph)

    @app.get("/graph/export/cytoscape-json")
    def graph_cyto() -> dict:
        return cytoscape(serving_graph)

    @app.get("/graph/neighbors")
    def graph_neighbors_route(
        node_id: str,
        depth: int = 1,
        edge_types: str | None = None,
        exclude_edge_types: str | None = None,
    ) -> dict:
        types = [t for t in edge_types.split(",") if t] if edge_types else None
        return graph_neighbors(
            serving_graph,
            node_id,
            depth,
            types,
            exclude_edge_types=_edge_type_set(exclude_edge_types),
        )

    @app.get("/graph/slice")
    def graph_slice_route(
        node_id: str,
        depth: int = 1,
        limit: int = 100,
        edge_types: str | None = None,
        exclude_edge_types: str | None = None,
        exclude_types: str | None = None,
    ) -> dict:
        """A bounded sub-graph around a node.

        ``exclude_edge_types`` drops edges from the *traversal*, not just the
        output — the canvas uses it to leave out `indexes`, the source→artifact
        shortcut that otherwise flattens the directory tree out of any bounded
        view of the graph.
        """

        types = [t for t in edge_types.split(",") if t] if edge_types else None
        return graph_slice(
            serving_graph,
            node_id,
            depth,
            types,
            limit,
            exclude_edge_types=_edge_type_set(exclude_edge_types),
            exclude_node_types=_edge_type_set(exclude_types),
        )

    @app.get("/graph/diagnostics")
    def graph_diagnostics(top: int = 20) -> dict:
        """Structural health of the graph, for the full-screen workspace.

        Answers the questions a picture cannot: which nodes are hubs, how much
        of the graph is disconnected, which edge types actually carry weight,
        and how the sources compare. One pinned read of the graph — this walks
        it, so it must not be on a hot path the UI polls.
        """
        graph_obj = serving_graph
        remote = getattr(graph_obj, "remote_diagnostics", None)
        if callable(remote):
            return remote(top=top)
        # `iter_edges`/`node_map` hand back the live mappings instead of
        # copying 850k edges per call; both require holding `reading()` for
        # the whole walk, which is exactly what this block does.
        with graph_obj.reading():
            node_types = graph_obj.type_counts()
            total_nodes = graph_obj.number_of_nodes()
            total_links = graph_obj.number_of_edges()
            nodes = graph_obj.node_map()
            degree: dict[str, int] = {}
            edge_types: dict[str, int] = {}
            for (source_id, target_id), edge_map in graph_obj.iter_edges():
                count = len(edge_map)
                degree[source_id] = degree.get(source_id, 0) + count
                degree[target_id] = degree.get(target_id, 0) + count
                for data in edge_map.values():
                    kind = str(data.get("type") or "unknown")
                    edge_types[kind] = edge_types.get(kind, 0) + 1
            hubs = sorted(degree.items(), key=lambda item: (-item[1], item[0]))[: max(1, top)]
            # An orphan is a node no edge touches. On a healthy index this is
            # near zero; a large number means enrichment did not run, or a
            # source indexed content that nothing links to.
            orphans = [node_id for node_id in nodes if node_id not in degree]
            hub_rows = [
                {
                    "node_id": node_id,
                    "degree": count,
                    "label": (nodes.get(node_id) or {}).get("label"),
                    "type": (nodes.get(node_id) or {}).get("type"),
                }
                for node_id, count in hubs
            ]
        return {
            "total_nodes": total_nodes,
            "total_links": total_links,
            "node_types": node_types,
            "edge_types": dict(sorted(edge_types.items(), key=lambda kv: -kv[1])),
            "orphan_count": len(orphans),
            "orphan_sample": orphans[:20],
            "density": round(total_links / total_nodes, 3) if total_nodes else 0.0,
            "hubs": hub_rows,
        }

    @app.get("/graph/path")
    def graph_path(source: str, target: str, max_depth: int = 8) -> dict:
        """Shortest path between two nodes, as a navigable chain.

        "How are these two things related?" is the question a graph is
        uniquely able to answer and the canvas alone cannot — two nodes six
        hops apart are never on screen together. Bidirectional BFS so a miss
        on a large graph costs two shallow frontiers rather than one deep one.
        """
        graph_obj = serving_graph
        remote = getattr(graph_obj, "remote_path", None)
        if callable(remote):
            payload = remote(source=source, target=target, max_depth=max_depth)
            missing = payload.pop("missing", None)
            if missing:
                raise HTTPException(status_code=404, detail=f"Unknown node: {missing}")
            return payload
        if source not in graph_obj:
            raise HTTPException(status_code=404, detail=f"Unknown node: {source}")
        if target not in graph_obj:
            raise HTTPException(status_code=404, detail=f"Unknown node: {target}")
        path = _shortest_path(graph_obj, source, target, max_depth=max_depth)
        return {
            "source": source,
            "target": target,
            "found": path is not None,
            "hops": len(path) - 1 if path else None,
            "path": [
                {"node_id": node_id, **dict(graph_obj.nodes.get(node_id) or {})}
                for node_id in (path or [])
            ],
        }

    @app.get("/nodes/explain")
    def explain_node(node_id: str) -> dict:
        """Transport adapter. The operation is `services.graph.explain_node`."""

        return graph_service.explain_node(services, node_id)

    def _authorize_graph_query(authorization: str | None) -> None:
        """Keep the graph service private even on a flat container network."""

        if role_policy.role is not Role.GRAPH:
            raise HTTPException(status_code=404, detail="graph query service is not enabled")
        import hmac

        env_name = config.graph.query_service_token_env
        expected = os.environ.get(env_name or "", "")
        if not expected:
            raise HTTPException(
                status_code=503,
                detail=f"graph query token environment variable {env_name!r} is empty",
            )
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="invalid graph query token")

    @app.post("/internal/graph/query")
    def internal_graph_query(
        req: GraphQueryRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Execute one graph operation where the snapshot is resident.

        The operation surface is intentionally query-shaped. There is no
        generic graph dump hidden behind search/neighbors, so API replicas
        cannot accidentally reconstruct and retain the full graph.
        """

        _authorize_graph_query(authorization)
        parameters = dict(req.parameters or {})
        # Through the serving graph, not `graph_builder.graph`. This endpoint
        # *is* the graph service, and on the row backend a graph replica holds
        # no working set at all — reaching for the builder's copy answered
        # every query from an empty graph, with a plausible-looking zero.
        # `force_local` above already guarantees this cannot proxy to itself.
        graph_obj = serving_graph
        operation = req.operation.strip().lower()

        if operation == "stats":
            with graph_obj.reading():
                result = {
                    "total_nodes": graph_obj.number_of_nodes(),
                    "total_links": graph_obj.number_of_edges(),
                    "node_types": graph_obj.type_counts(),
                }
        elif operation == "node":
            attrs = graph_obj.nodes.get(str(parameters.get("node_id") or ""))
            result = dict(attrs) if attrs is not None else None
        elif operation == "search":
            from pheasant.search.graph_search import search_graph

            result = search_graph(
                graph_obj,
                str(parameters.get("query") or ""),
                int(parameters.get("max_results") or 10),
                parameters.get("source_name"),
                bool(parameters.get("include_relationships", True)),
                node_index=engine.node_index,
                wasm_relationship_search=bool(parameters.get("wasm_relationship_search", False)),
            )
        elif operation == "neighbors":
            result = graph_neighbors(
                graph_obj,
                str(parameters.get("node_id") or ""),
                int(parameters.get("depth") or 1),
                parameters.get("edge_types"),
                parameters.get("max_nodes"),
                set(parameters.get("exclude_edge_types") or []) or None,
                set(parameters.get("exclude_node_types") or []) or None,
            )
        elif operation == "slice":
            result = graph_slice(
                graph_obj,
                str(parameters.get("node_id") or ""),
                int(parameters.get("depth") or 1),
                parameters.get("edge_types"),
                int(parameters.get("limit") or 100),
                set(parameters.get("exclude_edge_types") or []) or None,
                set(parameters.get("exclude_node_types") or []) or None,
            )
        elif operation == "node_link":
            result = node_link(
                graph_obj,
                node_limit=parameters.get("node_limit"),
                link_limit=parameters.get("link_limit"),
                node_types=set(parameters.get("node_types") or []) or None,
                exclude_node_types=set(parameters.get("exclude_node_types") or []) or None,
                source_id=parameters.get("source_id"),
            )
        elif operation == "cytoscape":
            result = cytoscape(graph_obj)
        elif operation == "diagnostics":
            result = graph_diagnostics(int(parameters.get("top") or 20))
        elif operation == "taxonomy":
            result = taxonomy(
                source=parameters.get("source"),
                path=parameters.get("path"),
                max_nodes=int(parameters.get("max_nodes") or 2000),
            )
        elif operation == "path":
            source_id = str(parameters.get("source") or "")
            target_id = str(parameters.get("target") or "")
            missing = source_id if source_id not in graph_obj else None
            if missing is None and target_id not in graph_obj:
                missing = target_id
            if missing is not None:
                result = {"source": source_id, "target": target_id, "missing": missing}
            else:
                path_nodes = _shortest_path(
                    graph_obj,
                    source_id,
                    target_id,
                    max_depth=int(parameters.get("max_depth") or 8),
                )
                result = {
                    "source": source_id,
                    "target": target_id,
                    "found": path_nodes is not None,
                    "hops": len(path_nodes) - 1 if path_nodes else None,
                    "path": [
                        {"node_id": node_id, **dict(graph_obj.nodes.get(node_id) or {})}
                        for node_id in (path_nodes or [])
                    ],
                }
        elif operation == "facts":
            from pheasant.assistant.chat import collect_facts

            result = collect_facts(
                graph_obj,
                [str(item) for item in parameters.get("node_ids") or []],
                int(parameters.get("limit") or 12),
            )
        elif operation == "memory_coverage":
            from pheasant.memory.bridge import ABOUT_EDGE

            artifacts = {str(item) for item in parameters.get("artifact_ids") or []}
            bridged: set[str] = set()
            by_signal: dict[str, int] = {}
            with graph_obj.reading():
                for (source_id, _target), edge_map in graph_obj.iter_edges():
                    if source_id not in artifacts:
                        continue
                    for data in edge_map.values():
                        if data.get("type") == ABOUT_EDGE:
                            bridged.add(source_id)
                            signal = str(data.get("match_signal") or "unknown")
                            by_signal[signal] = by_signal.get(signal, 0) + 1
            result = {
                "records": len(artifacts),
                "bridged": len(bridged),
                "unbridged": len(artifacts) - len(bridged),
                "by_signal": by_signal,
            }
        else:
            raise HTTPException(status_code=400, detail=f"unknown graph operation: {operation}")
        return {"result": result}

    @app.get("/fs/list")
    def fs_list(path: str | None = None) -> dict:
        roots = _allowed_roots(config)
        if not path:
            browse_roots = _configured_roots(config)
            return {
                "path": None,
                "parent": None,
                "roots": [str(root) for root in browse_roots],
                "entries": [
                    {
                        "name": root.name or str(root),
                        "path": str(root),
                        "is_dir": True,
                        "mounted": root.exists(),
                    }
                    for root in browse_roots
                ],
            }
        try:
            resolved = _resolve_source_path(path, config)
        except PathPolicyError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not resolved.exists() or not resolved.is_dir():
            raise HTTPException(status_code=404, detail=f"Not a directory: {resolved}")
        entries = []
        for child in sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith("."):
                continue
            entries.append({"name": child.name, "path": str(child), "is_dir": child.is_dir()})
        parent = resolved.parent
        parent_allowed = config.security.allow_user_selected_source_paths or any(
            parent == r or r in parent.parents or parent == r for r in roots
        )
        return {
            "path": str(resolved),
            "parent": str(parent) if parent_allowed and parent != resolved else None,
            "roots": [str(root) for root in roots],
            "entries": entries,
        }

    @app.get("/config")
    def get_config() -> dict:
        raw_yaml = None
        path = Path(app.state.config_path)
        if path.exists():
            raw_yaml = path.read_text(encoding="utf-8")
        return {
            "path": str(path),
            "effective": config.model_dump(mode="json"),
            "raw_yaml": raw_yaml,
            "profiles": profile_names(),
        }

    @app.put("/config")
    def put_config(req: ConfigWriteRequest) -> dict:
        if req.yaml_text is not None:
            try:
                data = yaml.safe_load(req.yaml_text) or {}
            except yaml.YAMLError as exc:
                raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}") from exc
        elif req.config is not None:
            data = req.config
        else:
            raise HTTPException(status_code=400, detail="Provide config or yaml_text")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Config root must be a mapping")
        try:
            candidate = PheasantConfig.model_validate(data)
        except (ConfigError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid config: {exc}") from exc
        errors = validate_source_paths(candidate, require_exists=False)
        if errors:
            raise HTTPException(status_code=400, detail="; ".join(errors))
        path = Path(app.state.config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = req.yaml_text if req.yaml_text is not None else dump_config_yaml(data)
        path.write_text(rendered, encoding="utf-8")
        audit(None, "write_config", {"path": str(path)})
        return {
            "status": "written",
            "path": str(path),
            "restart_required": True,
            "effective": candidate.model_dump(mode="json"),
        }

    # ------------------------------------------------------------------
    # Semantic search (embeddings). Everything `search.embeddings` does in
    # YAML, reachable from the UI — including building vectors for content
    # that was indexed before embeddings were switched on.
    # ------------------------------------------------------------------
    # Last vector-backend probe failure, so a store that cannot actually run
    # is reported as such instead of surfacing later as a 500 on reindex.
    store_probe_error: dict[str, str | None] = {"error": None}

    def _reload_vector_stack() -> None:
        """Rebuild the embed-on-sync indexer and query-time searcher in place.

        Embeddings are wired at engine construction, so flipping the config
        without this leaves a live process that has agreed to embed and then
        doesn't — the reindex would report success having written nothing.

        Vector backends import lazily, so constructing an indexer proves
        nothing about the backend being usable: a LanceDB store without the
        ``[vector]`` extra builds fine and only fails on first touch. Probe
        it here so enabling embeddings fails at the moment the user asks for
        it, rather than reporting ``active: true`` and 500ing on reindex.
        """
        from pheasant.search.vector_store import vector_indexer_from_config

        indexer = vector_indexer_from_config(config)
        if indexer is not None:
            try:
                indexer.store.count()
            except Exception as exc:
                store_probe_error["error"] = str(exc)
                engine.vectors = None
                search.vector = None
                raise
        store_probe_error["error"] = None
        engine.vectors = indexer
        search.vector = engine.vector_searcher()

    def _embeddings_status() -> dict:
        from pheasant.search.vector_store import (
            VECTOR_STORE_PROVIDERS,
            vector_store_available,
        )

        settings = config.search.embeddings
        vector_count = 0
        dimensions_on_disk: int | None = None
        store_error: str | None = store_probe_error["error"]
        # An indexer exists but that only means it was *constructed*; the
        # backend imports lazily. Touching the store is what proves it works,
        # so a store that raises here is reported inactive rather than
        # advertising a semantic search that would fail on first use.
        active = engine.vectors is not None and store_error is None
        if engine.vectors is not None:
            try:
                vector_count = int(engine.vectors.store.count())
                dimensions = getattr(engine.vectors.store, "dimensions", None)
                if callable(dimensions):
                    dimensions_on_disk = dimensions()
            except Exception as exc:  # a broken store must still render a page
                store_error = str(exc)
                active = False
        chunk_rows = state.rows("SELECT COUNT(*) AS n FROM chunks")
        chunk_count = int(chunk_rows[0]["n"]) if chunk_rows else 0
        return {
            "enabled": settings.enabled,
            "active": active,
            "provider": settings.provider,
            "model": settings.model,
            "base_url": settings.base_url,
            "api_key_env": settings.api_key_env,
            "api_key_present": bool(os.environ.get(settings.api_key_env)),
            "dimensions": settings.dimensions,
            "batch_size": settings.batch_size,
            "store_provider": config.search.vector_store.provider,
            "store_path": str(config.search.vector_store.path or ""),
            "vector_count": vector_count,
            "chunk_count": chunk_count,
            # How much of the index is actually searchable semantically.
            "coverage": round(vector_count / chunk_count, 4) if chunk_count else 0.0,
            "dimensions_on_disk": dimensions_on_disk,
            "store_error": store_error,
            "providers": [
                {
                    "id": "stub",
                    "label": "Deterministic stub (offline)",
                    "needs_key": False,
                    "description": "No network, no model. Useful for trying semantic "
                    "search out and for tests.",
                },
                {
                    "id": "openai-spec",
                    "label": "OpenAI-spec endpoint",
                    "needs_key": True,
                    "description": "POST {base_url}/embeddings — OpenAI, or any "
                    "gateway or self-hosted server speaking the same shape.",
                },
            ],
            "store_providers": [
                {
                    "id": name,
                    "label": label,
                    "available": vector_store_available(name),
                    "hint": hint,
                }
                for name, label, hint in VECTOR_STORE_PROVIDERS
            ],
        }

    @app.get("/search/embeddings")
    def embeddings_status() -> dict:
        return _embeddings_status()

    @app.put("/search/embeddings")
    def embeddings_update(req: EmbeddingsRequest) -> dict:
        from pheasant.search.vector_store import (
            VECTOR_STORE_PROVIDERS,
            vector_store_available,
        )

        settings = config.search.embeddings
        # Refuse a backend this deployment cannot run *before* touching the
        # config, so a bad choice leaves the running process exactly as it was
        # instead of half-applied and inert.
        target_store = req.store_provider or config.search.vector_store.provider
        wants_on = settings.enabled if req.enabled is None else req.enabled
        if wants_on and not vector_store_available(target_store):
            hint = next(
                (hint for name, _, hint in VECTOR_STORE_PROVIDERS if name == target_store),
                None,
            )
            raise HTTPException(
                status_code=400,
                detail=f"Vector store {target_store!r} is not available in this deployment"
                + (f" — {hint}" if hint else "")
                + ". The 'numpy' store needs no extra dependencies.",
            )
        requested = {
            "enabled": req.enabled,
            "provider": req.provider,
            "model": req.model,
            "base_url": req.base_url,
            "api_key_env": req.api_key_env,
            "dimensions": req.dimensions,
            "batch_size": req.batch_size,
        }
        changes = {
            key: value
            for key, value in requested.items()
            if value is not None or (key == "dimensions" and key in req.model_fields_set)
        }
        # Provider, endpoint, model and dimensions together define the vector
        # space. A backend change can also uncover an older index in a
        # different space. Any of them invalidates every existing vector.
        vector_space_change = any(
            key in changes and changes[key] != getattr(settings, key)
            for key in ("provider", "base_url", "model", "dimensions")
        ) or (
            req.store_provider is not None
            and req.store_provider != config.search.vector_store.provider
        )

        for key, value in changes.items():
            setattr(settings, key, value)
        if req.store_provider is not None:
            config.search.vector_store.provider = req.store_provider

        try:
            _reload_vector_stack()
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"Could not enable embeddings: {exc}"
            ) from exc

        # Clear incompatible vectors immediately, even when the caller wants
        # to rebuild later. Leaving them queryable lets the new embedder send
        # (for example) 3,072-wide queries to a 1,536-wide Lance table. Reset
        # drops Lance's Arrow schema as well as its rows; row deletion alone
        # cannot change a FixedSizeList width.
        dropped_vectors = 0
        if vector_space_change and engine.vectors is not None:
            dropped_vectors = engine.vectors.reset()

        wrote_config = False
        if req.persist:
            payload = {
                "search": {
                    "embeddings": settings.model_dump(mode="json"),
                    "vector_store": config.search.vector_store.model_dump(mode="json"),
                }
            }
            wrote_config = _merge_into_config_file(payload)
        audit(None, "update_embeddings", {"changes": list(changes), "persisted": wrote_config})

        result = {
            "status": "updated",
            "wrote_config": wrote_config,
            "vectors_invalidated": vector_space_change,
            "vectors_dropped": dropped_vectors,
            **_embeddings_status(),
        }
        if req.reindex and engine.vectors is not None:
            # A changed vector space was reset above; unchanged settings keep
            # their existing vectors and fill only missing chunk ids.
            result["reindex"] = _rebuild_vectors(drop_existing=False)
            # Record the space we just embedded into, so the next restart sees
            # a matching fingerprint and does not drop and re-embed all over
            # again for a change that has already been applied.
            state.set_fingerprint(EMBEDDING_SCOPE, embedding_fingerprint(config), utc_now())
        return result

    def _rebuild_vectors(drop_existing: bool = False) -> dict:
        """Embed everything already indexed, without re-reading the sources.

        Vectors are keyed by content-addressed chunk id, so this is
        idempotent: chunks already embedded are skipped and a second run
        makes zero embedder calls. The engine owns the same operation (it
        runs it automatically when the embedding config changes under a
        restart); this wrapper adds the HTTP-facing error shape.
        """
        if engine.vectors is None:
            raise HTTPException(
                status_code=400,
                detail=store_probe_error["error"]
                or "Embeddings are not enabled — turn them on first.",
            )
        embedded = 0
        artifacts = state.rows("SELECT id, source_id FROM artifacts ORDER BY id")
        try:
            if drop_existing:
                # A changed model/dimension means the old vectors are garbage.
                for source_id in {
                    str(row["source_id"])
                    for row in state.rows("SELECT DISTINCT source_id FROM artifacts")
                }:
                    engine.vectors.prune_source(source_id, set())

            for artifact in artifacts:
                chunks = state.rows(
                    "SELECT id, text, text_hash FROM chunks WHERE artifact_id=? "
                    "ORDER BY chunk_index",
                    (artifact["id"],),
                )
                if not chunks:
                    continue
                embedded += engine.vectors.index_artifact(
                    str(artifact["source_id"]),
                    str(artifact["id"]),
                    [dict(chunk) for chunk in chunks],
                )
            # index_artifact queues rather than embeds immediately (batches
            # across artifacts so this loop isn't one embedder call per
            # artifact) — flush before reading the count, or a remainder
            # smaller than the queue threshold would be silently unembedded
            # and undercounted.
            engine.vectors.flush()
            vector_count = int(engine.vectors.store.count())
        except (ModuleNotFoundError, ValueError) as exc:
            # A missing optional extra or a mismatched embedding space is a
            # configuration problem the caller can act on, not a server fault.
            store_probe_error["error"] = str(exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(None, "rebuild_vectors", {"embedded_chunks": embedded})
        return {
            "embedded_chunks": embedded,
            "artifacts_scanned": len(artifacts),
            "vector_count": vector_count,
        }

    @app.post("/search/embeddings/reindex")
    def embeddings_reindex(drop_existing: bool = False) -> dict:
        return {**_rebuild_vectors(drop_existing=drop_existing), **_embeddings_status()}

    def _merge_into_config_file(patch: dict) -> bool:
        """Deep-merge ``patch`` into the config file, preserving everything else."""
        from pheasant.config.loader import deep_merge

        path = Path(app.state.config_path)
        if not path.exists():
            return False
        yaml_error = getattr(yaml, "YAMLError", ValueError)
        try:
            existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Configuration file cannot be read for persistence: {path}",
            ) from exc
        except yaml_error:
            return False
        if not isinstance(existing, dict):
            return False
        merged = deep_merge(existing, patch)
        try:
            path.write_text(dump_config_yaml(merged), encoding="utf-8")
        except OSError as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Configuration file is not writable: {path}. "
                    "Mount it read-write or save with persistence disabled."
                ),
            ) from exc
        return True

    # ------------------------------------------------------------------
    # Editing the live knowledge base, one section at a time. `PUT /config`
    # replaces the whole file and always demands a restart; these routes let
    # the UI change one thing and be told honestly what happened to it.
    # ------------------------------------------------------------------
    @app.get("/config/sections")
    def config_sections() -> dict:
        effective = config.model_dump(mode="json")
        return {
            "sections": [
                {
                    "id": name,
                    "values": effective.get(name, {}),
                    "live_applicable": live,
                }
                for name, live in LIVE_APPLICABLE_SECTIONS.items()
            ]
        }

    @app.patch("/config/section/{section}")
    def patch_config_section(section: str, req: ConfigSectionRequest) -> dict:
        """Validate, persist and (where safe) hot-apply one config section.

        Validation goes through the *whole* config rather than the section
        alone: sections are not independent (``storage`` paths derive from
        ``pheasant.state_path``), and validating a fragment in isolation
        accepts combinations the loader would reject.
        """
        if section not in LIVE_APPLICABLE_SECTIONS:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown config section: {section}. Known sections: "
                + ", ".join(sorted(LIVE_APPLICABLE_SECTIONS)),
            )
        if not isinstance(req.values, dict):
            raise HTTPException(status_code=400, detail="values must be a mapping")

        from pheasant.config.loader import deep_merge

        current = config.model_dump(mode="json")
        candidate_data = deep_merge(current, {section: req.values})
        try:
            candidate = PheasantConfig.model_validate(candidate_data)
        except (ConfigError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid {section}: {exc}") from exc

        live = LIVE_APPLICABLE_SECTIONS[section]
        applied = False
        if live:
            # Swap the whole settings object rather than patching fields one by
            # one: a partial swap leaves the process in a state that is neither
            # the old config nor the new one, which is the worst of both.
            setattr(config, section, getattr(candidate, section))
            applied = True
            if section == "search":
                try:
                    _reload_vector_stack()
                except Exception as exc:  # embeddings may be misconfigured
                    logger.warning("search section applied but vectors not reloaded: %s", exc)
        wrote = False
        if req.persist:
            wrote = _merge_into_config_file({section: req.values})
        audit(None, "patch_config_section", {"section": section, "persisted": wrote})
        return {
            "status": "updated",
            "section": section,
            "applied": applied,
            "restart_required": not live,
            "wrote_config": wrote,
            "values": candidate.model_dump(mode="json").get(section, {}),
        }

    @app.get("/knowledge-base")
    def knowledge_base() -> dict:
        return {
            "id": config.knowledge_base_id,
            "name": config.pheasant.name,
            "description": config.pheasant.description,
            "environment": config.pheasant.environment,
            "version": __version__,
            "state_path": str(config.pheasant.state_path),
            "config_path": str(app.state.config_path),
        }

    @app.put("/knowledge-base")
    def update_knowledge_base(req: KnowledgeBaseRequest) -> dict:
        """Edit this knowledge base's identity.

        ``description`` is free to change. ``name`` is not cosmetic — it *is*
        ``kb_id``: the graph's root node id, the key every stable artifact ID
        hangs off, and the identity a Synapse router routes to (CLAUDE.md §4
        rule 3). Renaming is allowed, but it is reported as what it is: the
        existing graph belongs to the old name and a full re-index is needed
        to rebuild it under the new one. Silently accepting the rename and
        leaving an orphaned graph behind would be the actual bug.
        """
        changes: dict = {}
        if req.description is not None:
            config.pheasant.description = req.description
            changes["description"] = req.description
        rename = None
        if req.name is not None and req.name != config.pheasant.name:
            previous = config.pheasant.name
            if not req.name.strip():
                raise HTTPException(status_code=400, detail="name must not be empty")
            changes["name"] = req.name
            rename = {
                "previous": previous,
                "current": req.name,
                "reindex_required": True,
                "detail": (
                    f"The indexed graph is stored under {previous!r}. Run a full "
                    f"sync (`pheasant sync --all --mode full`) to rebuild it under "
                    f"{req.name!r}; until then this knowledge base will look empty."
                ),
            }
        if not changes:
            return {"status": "unchanged", **knowledge_base()}
        wrote = False
        if req.persist:
            wrote = _merge_into_config_file({"pheasant": changes})
        if rename:
            # Applied to the file only. Swapping kb_id in the live process
            # would repoint every subsequent write at a graph this process has
            # not loaded, which is a worse outcome than requiring a restart.
            config.pheasant.name = rename["previous"]
        audit(None, "update_knowledge_base", {"changes": sorted(changes), "persisted": wrote})
        return {
            "status": "updated",
            "changed": sorted(changes),
            "wrote_config": wrote,
            "restart_required": rename is not None,
            "rename": rename,
            **knowledge_base(),
        }

    @app.get("/config/effective")
    def config_effective(profile: str = "quickstart") -> dict:
        try:
            return effective_config_dict(app.state.config_path, profile, {})
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # Overview — one call that tells the UI whether there is anything to
    # show yet, and what shape it is. Drives the onboarding empty state.
    # ------------------------------------------------------------------
    @app.get("/overview")
    def overview() -> dict:
        graph_obj = serving_graph
        # One pinned read of aggregates the graph maintains on write. This
        # used to walk every node to tally types on an endpoint the UI hits on
        # every page load — seconds of latency for numbers already known.
        with graph_obj.reading():
            counts = graph_obj.type_counts()
            total_nodes = graph_obj.number_of_nodes()
            total_links = graph_obj.number_of_edges()
        registered = _with_sync_state(SourceRegistry(config, state).list_sources())
        artifacts = state.rows("SELECT COUNT(*) AS n FROM artifacts")
        chunks = state.rows("SELECT COUNT(*) AS n FROM chunks")
        indexed = int(artifacts[0]["n"]) if artifacts else 0
        # "Content" excludes the knowledge-base root and its source nodes:
        # a freshly-created KB has those and nothing else, and showing a
        # lone star node as if it were a graph is exactly the confusion
        # this field exists to prevent.
        structural = counts.get("knowledge_base", 0) + counts.get("source", 0)
        return {
            "knowledge_base": config.knowledge_base_id,
            "name": config.pheasant.name,
            "description": config.pheasant.description,
            "version": __version__,
            "sources": registered,
            "source_count": len(registered),
            "indexed_artifacts": indexed,
            "chunk_count": int(chunks[0]["n"]) if chunks else 0,
            "node_counts": counts,
            "total_nodes": total_nodes,
            "total_links": total_links,
            "has_content": total_nodes > structural and indexed > 0,
            "config_path": str(app.state.config_path),
        }

    # ------------------------------------------------------------------
    # Assistant — grounded chat over the index. Never in the sync path.
    # ------------------------------------------------------------------
    @app.get("/assistant/status")
    def assistant_status(session_id: str | None = None) -> dict:
        from pheasant.assistant.providers import PROVIDERS, resolve_auto_provider

        settings = config.assistant
        providers = [
            {
                "id": spec.id,
                "label": spec.label,
                "default_model": spec.default_model,
                "api_key_env": spec.api_key_env,
                "key_hint": spec.key_hint,
                # Whether the *server* already holds a key for this provider.
                "env_key_present": bool(os.environ.get(spec.api_key_env)),
            }
            for spec in PROVIDERS.values()
        ]
        from pheasant.assistant.chat import resolve_provider

        credential = app.state.session_keys.get(session_id)
        configured = settings.provider
        if configured == "auto":
            configured = resolve_auto_provider(dict(os.environ))
        # `ready` must mean "a credential actually resolves", not "a provider
        # name is configured" — otherwise a config naming a provider whose key
        # env var is unset reports "model connected" while every answer comes
        # back extractive. Resolve exactly the way /assistant/chat does.
        selected = resolve_provider(config, credential, dict(os.environ))
        return {
            "enabled": settings.enabled,
            "providers": providers,
            "configured_provider": configured,
            "configured_model": settings.model,
            "allow_session_keys": settings.allow_session_keys,
            "session": credential.redacted() if credential else None,
            # False means answers will be extractive rather than synthesized.
            "ready": selected is not None,
            "credential_source": selected.get("source") if selected else None,
        }

    @app.post("/assistant/key")
    def assistant_key(req: AssistantKeyRequest) -> dict:
        """Hold a user-supplied key in memory for this session only.

        The key never leaves this process: not to disk, not to the config,
        not into any response body. The caller gets an opaque session id.
        """
        from pheasant.assistant.providers import PROVIDERS

        if not config.assistant.allow_session_keys:
            raise HTTPException(
                status_code=403,
                detail="Session-supplied API keys are disabled (assistant.allow_session_keys)",
            )
        if req.provider not in PROVIDERS:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {req.provider}")
        if not req.api_key.strip():
            raise HTTPException(status_code=400, detail="API key must not be empty")
        token, credential = app.state.session_keys.put(
            req.provider,
            req.api_key.strip(),
            model=req.model,
            base_url=req.base_url,
        )
        return {"session_id": token, "session": credential.redacted()}

    @app.delete("/assistant/key")
    def assistant_key_revoke(session_id: str | None = None) -> dict:
        return {"revoked": app.state.session_keys.revoke(session_id)}

    @app.post("/assistant/chat")
    def assistant_chat(req: ChatRequest, request: Request) -> dict:
        from pheasant.assistant.chat import answer_question

        if not config.assistant.enabled:
            raise HTTPException(status_code=403, detail="The assistant is disabled")
        if not req.question.strip():
            raise HTTPException(status_code=400, detail="question must not be empty")
        answer = answer_question(
            req.question,
            search=search,
            knowledge_base=config.knowledge_base_id,
            config=config,
            graph=serving_graph,
            state=state,
            credential=app.state.session_keys.get(req.session_id),
            env=dict(os.environ),
            mode=req.mode,
            max_results=req.max_results,
            source_name=req.source_name,
            source_types=req.source_types,
            exclude_source_types=req.exclude_source_types,
            principal=req.principal,
            principal_groups=req.principal_groups,
            workflow=req.workflow,
            options=req.options,
            memory=req.memory,
        )
        # A chat turn is the richest evidence the ledger gets: the question, the
        # passages that answered it, and the answer itself. `citations` is what
        # `extract_results` reads for ids and paths.
        record_retrieval(
            request,
            query=req.question,
            payload=answer,
            criteria={"mode": req.mode, "workflow": req.workflow} if req.mode else None,
            answer=str(answer.get("answer") or "") or None,
        )
        return answer

    @app.post("/assistant/chat/stream")
    def assistant_chat_stream(req: ChatRequest, request: Request):
        """The same answer as ``/assistant/chat``, with progress as it happens.

        Server-sent events: one ``step`` per workflow stage the moment it
        finishes, then a single ``answer`` (or ``error``) and the stream
        closes. The agent loop can take a while over a large index, and a
        client that shows "planning… retrieved 35 passages… grading" is
        waiting rather than wondering. The work runs in a worker thread and
        the steps arrive through a queue, so a slow reader can never stall the
        workflow itself.

        ``publish`` below is deliberately an **async** generator polling the
        queue with ``get_nowait``, not a sync generator blocking on
        ``events.get()`` — the same reasoning as ``/jobs/stream`` above.
        Starlette runs a sync generator in the anyio thread pool and cannot
        interrupt it, so a client that disconnects (or just an agentic
        workflow that runs long) leaves that worker thread — and its
        thread-pool token — held until ``run()``'s ``finally`` finally puts
        the ``None`` sentinel. Measured: ten abandoned streams pinned ten of
        the pool's forty tokens for the whole ~8s a stubbed workflow took to
        finish. An async generator never touches the pool at all.
        """

        import asyncio
        import json as json_module
        import queue as queue_module
        import threading

        from starlette.responses import StreamingResponse

        from pheasant.assistant.chat import answer_question

        if not config.assistant.enabled:
            raise HTTPException(status_code=403, detail="The assistant is disabled")
        if not req.question.strip():
            raise HTTPException(status_code=400, detail="question must not be empty")

        events: queue_module.Queue = queue_module.Queue()
        credential = app.state.session_keys.get(req.session_id)
        environ = dict(os.environ)
        # Captured here, in the request, because `run()` executes on a worker
        # thread after this route has already returned its response object.
        parent_event = observed(request)
        # Monotonic, like every other duration here: a wall-clock delta across a
        # generation that can take a minute is exactly where an NTP step shows up.
        answer_started = time.perf_counter()

        def run() -> None:
            try:
                answer = answer_question(
                    req.question,
                    search=search,
                    knowledge_base=config.knowledge_base_id,
                    config=config,
                    graph=serving_graph,
                    state=state,
                    credential=credential,
                    env=environ,
                    mode=req.mode,
                    max_results=req.max_results,
                    source_name=req.source_name,
                    source_types=req.source_types,
                    exclude_source_types=req.exclude_source_types,
                    principal=req.principal,
                    principal_groups=req.principal_groups,
                    workflow=req.workflow,
                    options=req.options,
                    on_step=lambda step: events.put(
                        {
                            "type": "step",
                            "name": step.name,
                            "detail": step.detail,
                            "passages": step.passages,
                        }
                    ),
                )
                _record_stream_answer(parent_event, req, answer, answer_started)
                events.put({"type": "answer", "answer": answer})
            except Exception as exc:  # surfaced to the client, never a 500 mid-stream
                logger.exception("streaming chat failed")
                events.put({"type": "error", "error": str(exc)})
            finally:
                events.put(None)

        threading.Thread(target=run, name="pheasant-chat-stream", daemon=True).start()

        poll_seconds = 0.25

        async def publish():
            while True:
                try:
                    item = events.get_nowait()
                except queue_module.Empty:
                    await asyncio.sleep(poll_seconds)
                    continue
                if item is None:
                    return
                yield f"data: {json_module.dumps(item)}\n\n"

        return StreamingResponse(
            publish(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # nginx sits in front of this in the compose stack and would
                # otherwise buffer the whole stream into one write.
                "X-Accel-Buffering": "no",
            },
        )

    # ------------------------------------------------------------------
    # Retrieval criteria. The same knobs an MCP client can override per
    # call (`preview_retrieval`), reachable as standing configuration.
    # ------------------------------------------------------------------
    def _retrieval_payload() -> dict:
        from pheasant.assistant.workflows.agentic import DEFAULTS as AGENTIC_DEFAULTS

        settings = config.assistant.retrieval
        return {
            "retrieval": settings.model_dump(mode="json"),
            # What actually reaches the workflow once workflow_options is
            # layered on top — the honest answer to "what is it doing", which
            # is not the same as "what did I set" whenever both are in play.
            "effective": {
                **settings.as_options(),
                **{
                    key: value
                    for key, value in (config.assistant.workflow_options or {}).items()
                    if not isinstance(value, dict)
                },
            },
            "workflow_options": dict(config.assistant.workflow_options or {}),
            "defaults": AGENTIC_DEFAULTS,
            "field_help": RETRIEVAL_FIELD_HELP,
        }

    @app.get("/assistant/retrieval")
    def assistant_retrieval() -> dict:
        return _retrieval_payload()

    @app.put("/assistant/retrieval")
    def assistant_retrieval_update(req: RetrievalRequest) -> dict:
        settings = config.assistant.retrieval
        # `exclude_none` on purpose: a PUT that sets one knob must not reset
        # the other nine to their schema defaults.
        changes = req.model_dump(exclude={"persist"}, exclude_none=True)
        previous = {key: getattr(settings, key) for key in changes}
        for key, value in changes.items():
            setattr(settings, key, value)
        wrote = False
        try:
            if req.persist and changes:
                wrote = _merge_into_config_file(
                    {"assistant": {"retrieval": settings.model_dump(mode="json")}}
                )
        except HTTPException:
            # A failed disk write must not leave a request that reported an
            # error active only in memory until the next restart.
            for key, value in previous.items():
                setattr(settings, key, value)
            raise
        audit(None, "update_retrieval", {"changes": sorted(changes), "persisted": wrote})
        return {
            "status": "updated",
            "changed": sorted(changes),
            "wrote_config": wrote,
            # Retrieval is query-time only, so this takes effect on the next
            # question — no restart, nothing to re-index.
            "applied": True,
            **_retrieval_payload(),
        }

    @app.get("/assistant/workflows")
    def assistant_workflows() -> dict:
        """Every question-answering workflow this deployment can run."""
        from pheasant.assistant.chat import resolve_provider
        from pheasant.assistant.workflows import (
            langgraph_available,
            list_workflows,
            resolve_workflow_name,
        )
        from pheasant.assistant.workflows.agentic import DEFAULTS as AGENTIC_DEFAULTS

        selected = resolve_provider(config, None, dict(os.environ))
        return {
            "workflows": list_workflows(),
            "configured": config.assistant.workflow,
            "active": resolve_workflow_name(
                config.assistant.workflow, has_llm=selected is not None
            ),
            "agent_extra_installed": langgraph_available(),
            "options": dict(config.assistant.workflow_options or {}),
            "option_defaults": {"agentic": AGENTIC_DEFAULTS},
        }

    # ------------------------------------------------------------------
    # MCP connection details, so the UI can hand an agent a working config.
    # ------------------------------------------------------------------
    @app.get("/mcp/info")
    def mcp_info() -> dict:
        from pheasant.mcp_client.agents import agent_mcp_config, render_agent_mcp_json

        transports = dict(config.server.mcp.transports)
        base = f"http://{config.server.host}:{config.server.port}"
        clients = {}
        for client_name in ("claude-code", "cursor"):
            try:
                clients[client_name] = render_agent_mcp_json(
                    agent_mcp_config("local", config_path=str(app.state.config_path))
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("could not render %s mcp config: %s", client_name, exc)
        return {
            "enabled": config.server.mcp.enabled,
            "transports": transports,
            "streamable_http_url": f"{base}/mcp" if transports.get("streamable_http") else None,
            "stdio_command": ["pheasant", "mcp", "--transport", "stdio"],
            "config_path": str(app.state.config_path),
            "tools": _mcp_tool_summaries(),
            "client_configs": clients,
        }

    # ------------------------------------------------------------------
    # Quick add — the UI counterpart to `pheasant up <anything>`.
    # ------------------------------------------------------------------
    @app.post("/sources/quick-add")
    def quick_add(req: QuickAddRequest) -> dict:
        from pheasant.targets import TargetError, fetch_target, resolve_targets

        state_dir = Path(config.pheasant.state_path)
        # In a role-split fleet the API owns registration, not source
        # materialization. Its /state and /workspace mounts are deliberately
        # read-only; the queued indexer owns the writable workspace and will
        # clone/fetch a managed repository under the source write lease.
        # Single-process deployments keep the eager clone that makes
        # `quick-add(wait=true)` immediately syncable.
        materialize_here = role_policy.indexes_locally
        if materialize_here:
            clone_root = state_dir / "sources"
            external_workspace = state_dir / "external"
        else:
            workspace_root = Path(config.pheasant.workspace_root)
            clone_root = workspace_root / "sources"
            external_workspace = workspace_root / "external"
        try:
            targets = resolve_targets(
                [req.target],
                clone_root=clone_root,
                workspace=external_workspace,
                split=req.split,
                name=req.name,
            )
            if materialize_here:
                for target in targets:
                    fetch_target(target)
        except TargetError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        registry = SourceRegistry(config, state)
        created: list[dict] = []
        for target in targets:
            payload = target.to_source_dict()
            if req.taxonomy:
                payload["taxonomy"] = {"enabled": True}
            if target.local:
                try:
                    if target.clone_url and not materialize_here:
                        # The managed checkout intentionally does not exist
                        # yet. Enforce the allowlist without the local-path
                        # existence check; the indexer creates it while
                        # processing the queued sync.
                        payload["path"] = str(
                            resolve_under(payload["path"], _allowed_roots(config))
                        )
                    else:
                        payload["path"] = str(_resolve_source_path(payload["path"], config))
                except PathPolicyError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            try:
                source = _source_from_payload(payload)
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=f"Invalid source: {exc}") from exc
            registry.register_source(source)
            # The engine resolves sources off the live config object, so a
            # freshly registered source is invisible to sync until it lands
            # there too (same step the /sources route takes).
            config.sources = [s for s in config.sources if s.name != source.name]
            config.sources.append(source)
            audit(source.name, "quick_add", {"target": req.target, "type": target.type})
            created.append(payload)

        results = []
        syncing: list[str] = []
        job_ids: list[str] = []
        queued_tasks: list[str] = []
        if req.sync_now:
            if req.wait:
                for entry in created:
                    try:
                        results.extend(_index(entry["name"], req.sync_mode)["results"])
                    except Exception as exc:  # surfaced per-source; others still run
                        results.append(
                            {"source_id": entry["name"], "status": "error", "error": str(exc)}
                        )
            else:
                # Cloning (above) still happens on this request — it's the
                # bounded part. Indexing is the unbounded part (a big repo's
                # first full parse+chunk+embed pass can run well past any
                # reverse-proxy timeout), so that's what moves to the
                # background; the caller gets sources back already
                # registered and polls GET /sources' `syncing` field.
                for entry in created:
                    job_id, queued = _start_background_sync(entry["name"], req.sync_mode)
                    if job_id:
                        job_ids.append(job_id)
                    queued_tasks.extend(queued)
                    syncing.append(entry["name"])
        return {
            "status": "registered",
            "sources": created,
            "sync_results": results,
            "syncing": syncing,
            "job_ids": job_ids,
            "queued_tasks": queued_tasks,
        }

    # Mounted *before* the UI so the SPA catch-all cannot swallow /mcp.
    if mcp_asgi_app is not None:

        @app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
        async def _mcp_no_slash():
            """Send the slash-less form to the mount, preserving method + body.

            Starlette's ``Mount`` only matches paths that continue past the
            prefix, so the ``/mcp`` we advertise would 405 while ``/mcp/``
            worked. 307 (not 302) is what keeps a POST a POST and carries the
            JSON-RPC body with it.
            """
            from starlette.responses import RedirectResponse

            return RedirectResponse(url="/mcp/", status_code=307)

        app.mount("/mcp", mcp_asgi_app)
    _mount_ui(app, config)
    return app


def _mcp_tool_summaries() -> list[dict]:
    """Name + one-line description for each public MCP tool.

    Read off ``PheasantTools`` rather than hand-maintained, so a tool added
    there shows up in the UI without a second edit (rule 8: the tool
    surface is public API, and this is one of its shop windows).
    """
    from pheasant.mcp_server.tools import PheasantTools

    summaries = []
    for name in sorted(dir(PheasantTools)):
        if name.startswith("_"):
            continue
        member = getattr(PheasantTools, name, None)
        if not callable(member):
            continue
        doc = (member.__doc__ or "").strip().splitlines()
        summaries.append({"name": name, "description": doc[0] if doc else ""})
    return summaries


def _mcp_asgi_app(config: PheasantConfig):
    """The MCP streamable-HTTP ASGI app to mount at ``/mcp``, or None.

    ``GET /mcp/info`` has always advertised ``streamable_http_url:
    <base>/mcp``, but ``pheasant serve`` only ever built the FastAPI app — so
    that URL 404'd, and a client POSTing to it got a 405. Found by pointing a
    real MCP client at a live container: the server was telling every agent to
    connect somewhere it did not listen.

    Returns None when MCP or the transport is disabled, or when the installed
    ``mcp`` package cannot build the app — the caller then mounts nothing and
    the API behaves exactly as it did before.
    """
    try:
        from pheasant.mcp_server.server import streamable_http_app

        return streamable_http_app(config)
    except Exception:  # pragma: no cover - depends on the optional [mcp] extra
        logger.warning("MCP streamable-http app unavailable; /mcp not mounted", exc_info=True)
        return None


def _mount_ui(app: FastAPI, config: PheasantConfig) -> None:
    """Optionally serve a prebuilt UI bundle (Option B in the design doc).

    This is additive and off unless a built bundle exists; the indexing
    container image does not build the UI, so by default nothing is mounted.
    """
    # Recorded either way so callers (the CLI's startup banner) can tell the
    # user whether the UI is actually being served, instead of leaving them to
    # guess why the port shows JSON.
    app.state.ui_dist = None
    if not config.server.ui.enabled:
        return
    dist = os.environ.get("PHEASANT_UI_DIST")
    candidates = [Path(dist)] if dist else []
    candidates.append(Path(__file__).resolve().parents[3] / "ui" / "dist")
    target = next((c for c in candidates if c.exists() and c.is_dir()), None)
    if target is None:
        return
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(target), html=True), name="ui")
    app.state.ui_dist = str(target)

    index = target / "index.html"

    @app.exception_handler(404)
    async def _spa_fallback(request: Request, exc: Any) -> Response:
        """Serve the UI shell for client-side routes.

        ``StaticFiles(html=True)`` serves ``index.html`` at ``/`` and 404s
        everything else, so every deep link into the single-page app —
        ``/evaluation``, ``/memory``, ``/tuning`` — returned raw JSON to a
        browser. That is not only a bookmark being broken: it is what a *hard
        refresh* does, so anyone who reloaded the page they were looking at got
        an error and had to navigate back in from the root.

        Handled at 404 rather than as a catch-all route, which matters: this
        runs only after real routing has failed, so it cannot shadow an API
        endpoint, the ``/mcp`` mount, or a static asset that exists.

        Three conditions, each excluding a case where HTML would be the wrong
        answer:

        * ``GET`` only — a mistyped ``POST`` deserves its 404, not a page.
        * the client must actually accept HTML, so an API client or a ``curl``
          still gets JSON and a missing endpoint still reads as missing.
        * the path must not look like a file. A missing ``.js`` served as HTML
          becomes a MIME-type error in the console, which is a much harder
          thing to diagnose than a 404.
        """

        accepts_html = "text/html" in request.headers.get("accept", "")
        looks_like_a_file = "." in request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET" and accepts_html and not looks_like_a_file:
            return FileResponse(index)
        return JSONResponse({"detail": getattr(exc, "detail", "Not Found")}, status_code=404)

    logger.info("Serving pheasant UI bundle from %s", target)
