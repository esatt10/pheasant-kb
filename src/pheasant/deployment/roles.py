"""Process roles (Phase 35.6).

One pheasant process does everything: serves search, serves the UI, watches
the filesystem, runs the scheduler, and indexes. That is exactly right for a
single container and exactly wrong for a fleet, for one reason that has
nothing to do with performance: **replicas.** Run three copies of today's
process against one knowledge base and all three watch the same directories,
all three fire the same scheduled sync, and all three try to index the same
source. The Phase 35.4 leases stop that from *corrupting* anything, but three
processes taking turns to do one process's work is not horizontal scale.

A role says which of those jobs this process has:

===========  ======  =========  =====  =========  ==============================
Role         Watch   Schedule   Drain  Index      Typical deployment
===========  ======  =========  =====  =========  ==============================
``all``      yes     yes        no     in-proc    one container (the default)
``api``      no      no         no     **never**  N replicas behind a Service
``indexer``  yes     yes        yes    in-proc    one per shard, a StatefulSet
``graph``    no      no         no     no         internal graph query service
``worker``   no      no         no     no         M replicas, autoscaled
``logger``   no      no         log    no         the observation tier
===========  ======  =========  =====  =========  ==============================

A role also says what a process may **hold**. ``validate_role`` refuses a
worker that resolves a database DSN, a model key or a source list, refuses one
secret spanning the worker and graph boundaries, and refuses an
unauthenticated API on an address other machines can reach. Those are
deployment invariants rather than process ones — none of them stops a process
working, which is exactly why they have to be refused at startup instead of
discovered later.

``all`` is the default and is **byte-identical to the behavior before roles
existed** — it does not drain a queue, because that would change what a
single container does the moment the queue is switched on for its crash
resumption alone.

The load-bearing cell is ``api``/Index/**never**. An api replica that indexed
would put every replica on the same source, which is the failure roles exist
to prevent; instead its sync requests are *published* and an indexer picks
them up. That makes the queue a hard requirement for that role, checked at
startup rather than discovered when a sync silently goes nowhere.

Routes are deliberately **not** hidden per role. The indexer coordinator does
not load the persisted graph in its parent process, however: sync children own
graph commits, and the Service selector keeps queries on API replicas. This
avoids holding two complete graph copies during every index while preserving
health, readiness, queue and control-plane diagnostics on the indexer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    ALL = "all"
    API = "api"
    INDEXER = "indexer"
    GRAPH = "graph"
    WORKER = "worker"
    LOGGER = "logger"


@dataclass(frozen=True)
class RolePolicy:
    """What a process with this role does without being asked."""

    role: Role
    #: Watch source directories for changes and re-index what changed.
    runs_watcher: bool
    #: Fire the interval re-sync beat (which also carries memory/IdP upkeep).
    runs_scheduler: bool
    #: Claim tasks from the durable index queue.
    drains_queue: bool
    #: Index in this process (or its own child) rather than publishing.
    indexes_locally: bool
    #: Serve the bundled UI. False on roles nobody points a browser at.
    serves_ui: bool
    #: Poll ``/state`` for a graph written by *another* process and reload it.
    #:
    #: Only ``api`` needs this, and it needs it badly: the graph is a file, a
    #: process loads it once at startup, and the only existing reload happens
    #: after a sync **this** process ran. An api replica never indexes, so
    #: without polling it would serve graph queries against whatever the graph
    #: was when the pod started — indefinitely, and silently, while text and
    #: vector search stayed current from the shared database.
    refreshes_graph: bool = False
    #: Claim batches from the **log** queue -- a different table with a
    #: different failure mode, deliberately not the index queue.
    #:
    #: Its own flag rather than a reuse of ``drains_queue`` because the two
    #: must be independently settable: an ``indexer`` drains index work and
    #: should not also be doing hot-to-cold Parquet rolls, which is exactly
    #: the coupling that would put a multi-million-row roll inside the
    #: scheduler's ``sync_lock``.
    drains_log_queue: bool = False

    @property
    def name(self) -> str:
        return self.role.value

    @property
    def is_default(self) -> bool:
        return self.role is Role.ALL


POLICIES: dict[Role, RolePolicy] = {
    Role.ALL: RolePolicy(
        role=Role.ALL,
        runs_watcher=True,
        runs_scheduler=True,
        # Deliberately False: `all` must not change behavior when the queue is
        # switched on. A single container turns the queue on for crash
        # resumption, not to become a fleet, and `sync_all` already drains
        # what it publishes.
        drains_queue=False,
        # False for the same reason, and it is the same reason: `all` must
        # behave identically whether or not a queue exists. A single container
        # rolls its own logs inline on the maintenance beat, bounded by
        # `max_rows_per_pass`, rather than growing a second worker.
        drains_log_queue=False,
        indexes_locally=True,
        serves_ui=True,
    ),
    Role.API: RolePolicy(
        role=Role.API,
        runs_watcher=False,
        runs_scheduler=False,
        drains_queue=False,
        indexes_locally=False,
        serves_ui=True,
        refreshes_graph=True,
    ),
    Role.INDEXER: RolePolicy(
        role=Role.INDEXER,
        runs_watcher=True,
        runs_scheduler=True,
        drains_queue=True,
        indexes_locally=True,
        serves_ui=False,
    ),
    Role.GRAPH: RolePolicy(
        role=Role.GRAPH,
        runs_watcher=False,
        runs_scheduler=False,
        drains_queue=False,
        indexes_locally=False,
        serves_ui=False,
        # The indexer commits snapshots on shared state. Only this service
        # reloads them when APIs use the remote graph boundary.
        refreshes_graph=True,
    ),
    Role.WORKER: RolePolicy(
        role=Role.WORKER,
        runs_watcher=False,
        runs_scheduler=False,
        drains_queue=False,
        indexes_locally=False,
        serves_ui=False,
    ),
    # The log tier. Everything else is False on purpose: this process serves
    # no traffic, indexes nothing, holds no sync lock and loads no graph. That
    # is the entire point -- persistence, rolling and cold compaction for a
    # request-rate stream, in a failure domain that shares nothing with
    # serving or ingest.
    Role.LOGGER: RolePolicy(
        role=Role.LOGGER,
        runs_watcher=False,
        runs_scheduler=False,
        drains_queue=False,
        indexes_locally=False,
        serves_ui=False,
        drains_log_queue=True,
    ),
}


class RoleConfigurationError(ValueError):
    """A role and a config that cannot do what the role promises."""


def resolve_role(config: object, override: str | None = None) -> RolePolicy:
    """CLI flag beats config beats the default.

    An unknown name raises rather than falling back to ``all``: a typo in a
    Deployment's args that silently produced a full-service pod would put an
    indexer's watcher on every api replica, which is the exact thing roles
    exist to prevent — and it would look like it worked.
    """

    server = getattr(config, "server", None)
    name = (override or getattr(server, "role", None) or Role.ALL.value).strip().lower()
    try:
        role = Role(name)
    except ValueError as exc:
        valid = ", ".join(item.value for item in Role)
        raise RoleConfigurationError(f"Unknown role {name!r}. Valid roles: {valid}") from exc
    return POLICIES[role]


# -- The per-role config allow-list (Phase 35.8) -----------------------------
#
# `worker.yaml` exists precisely to shrink a worker's surface -- no DSN, no
# keys, no sources, no MCP, no UI -- but it is the same 15-section schema
# every other tier validates, so nothing *structurally* prevented a worker
# config from carrying a database URL. The trust boundary between the indexer
# and the workers is real and well-reasoned, and it was enforced by what the
# operator put in a file rather than by what the process can express: a
# misapplied ConfigMap, or one shared `environment:` anchor in Compose, gives
# the least-trusted tier in the fleet credentials it was designed never to
# hold.
#
# So the convention becomes a startup invariant, in the style the two checks
# below already use: refuse, and name the field and the reason.

#: Provider keys a worker can never need. Drawn from the catalog rather than
#: from config alone, because ``assistant.provider: auto`` names no variable
#: and resolves whichever of these is present.
_MODEL_KEY_ENVS: tuple[str, ...] = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY")

#: Hosts that are not reachable from another machine. A bind outside this set
#: is a routable one, whatever the operator's firewall happens to say today.
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost", "ip6-localhost"})


def _env_value(name: str | None) -> str:
    """The resolved value of an environment variable named by config."""

    import os

    return (os.environ.get((name or "").strip(), "") or "").strip()


def _credential_envs(config: object) -> list[tuple[str, str]]:
    """(variable name, what holding it would mean) for every credential.

    Read off the live config rather than hardcoded, so a region that points a
    feature at its own variable is checked on *that* variable — the same
    reason nothing here ever reads a secret out of YAML.
    """

    def named(path: str, default: str | None = None) -> str | None:
        node: object = config
        for part in path.split("."):
            node = getattr(node, part, None)
            if node is None:
                return default
        return str(node) if isinstance(node, str) else default

    candidates: list[tuple[str | None, str]] = [
        (named("storage.dsn_env"), "the state database"),
        (named("search.embeddings.api_key_env"), "the embedding provider"),
        (named("ingestion.captioner.api_key_env"), "the captioner provider"),
        (named("ingestion.transcriber.api_key_env"), "the transcriber provider"),
        (named("assistant.api_key_env"), "the chat provider"),
        (named("security.idp.api_key_env"), "the identity provider"),
        (named("graph.query_service_token_env"), "the internal graph-query API"),
        (named("ingestion.landing_service_token_env"), "the internal landing service"),
    ]
    # `security.api_auth.token_env` is deliberately absent: that is the key to
    # this process's own front door, not a credential to somewhere else, and a
    # worker that serves an API at all needs it.
    candidates.extend((name, "a chat provider") for name in _MODEL_KEY_ENVS)
    seen: set[str] = set()
    ordered: list[tuple[str, str]] = []
    for name, reason in candidates:
        key = (name or "").strip()
        if key and key not in seen:
            seen.add(key)
            ordered.append((key, reason))
    return ordered


def _validate_worker_surface(config: object) -> None:
    """Refuse a worker that resolves anything a worker must never hold.

    A preparation worker parses bytes it is handed and returns chunks. It
    opens no database, calls no model, reads no source and serves no graph —
    which is exactly why it is the tier that scales, and the tier that may run
    somebody else's connector code. Every item below is a capability it has no
    use for, so holding one can only ever be a mistake or a compromise.
    """

    backend = str(getattr(getattr(config, "storage", None), "backend", "") or "").lower()
    if backend and backend != "sqlite":
        raise RoleConfigurationError(
            f"role 'worker' resolves storage.backend: {backend!r}. A preparation worker "
            "opens no state store — it is handed bytes and returns chunks — so a "
            "database backend here means this process is running the wrong config. "
            "Point it at worker.yaml (deploy/compose/worker.yaml)."
        )
    sources = getattr(config, "sources", None) or []
    if sources:
        names = ", ".join(str(getattr(item, "name", item)) for item in list(sources)[:3])
        raise RoleConfigurationError(
            f"role 'worker' resolves {len(sources)} source(s) ({names}...). A worker never "
            "lists or reads a source; the indexer does that and sends it the bytes. A "
            "non-empty sources list means this process is running the indexer's config."
        )
    for name, reason in _credential_envs(config):
        if _env_value(name):
            raise RoleConfigurationError(
                f"role 'worker' holds {name}, the credential for {reason}. A worker is the "
                "least-trusted tier in the fleet and needs none of it: give the worker "
                "service its own `environment:` block rather than the shared anchor (or its "
                "own Secret, in Kubernetes), and unset this variable for this process."
            )


def _validate_boundary_tokens(config: object) -> None:
    """Two trust boundaries must not share one secret.

    The worker token authenticates the *least*-trusted tier to the indexer.
    The graph token guards an internal API that serves the region's whole
    graph. Wiring both to one value — which the shipped Compose file did,
    ``PHEASANT_GRAPH_SERVICE_TOKEN: ${PHEASANT_INDEX_WORKER_TOKEN}`` — means
    compromising any worker also yields the graph credential, and the
    carefully drawn boundary is defeated by a convenience in a deployment
    file. Two variables in the manifests is the fix; this is what keeps it
    fixed.

    The landing token joined the same rule when manual ingestion stopped
    requiring a writable ``/state`` on the serving tier. It is a *write*
    credential — holding it means putting bytes into the corpus — which makes
    sharing it with the least-trusted tier worse than sharing the graph token,
    not better: a worker parses bytes it is handed by the indexer and has no
    business choosing what the region indexes.
    """

    concurrency = getattr(getattr(config, "sync", None), "concurrency", None)
    worker_env = str(getattr(concurrency, "remote_worker_token_env", "") or "")
    if not worker_env:
        return
    ingestion = getattr(config, "ingestion", None)
    # Every internal-service credential the *worker* tier must not also hold,
    # as (setting, variable, what holding it would let you do). A list rather
    # than a pair because there are two of these now and the next one should
    # cost a line here instead of a third copy of the argument below.
    guarded = [
        (
            "graph.query_service_token_env",
            str(getattr(getattr(config, "graph", None), "query_service_token_env", "") or ""),
            "the internal graph-query API, which serves the whole graph",
        ),
        (
            "ingestion.landing_service_token_env",
            str(getattr(ingestion, "landing_service_token_env", "") or ""),
            "the internal landing service, which puts bytes into the corpus",
        ),
    ]
    for setting, env_name, capability in guarded:
        if not env_name:
            continue
        if env_name.strip() == worker_env.strip():
            raise RoleConfigurationError(
                f"{setting} and sync.concurrency.remote_worker_token_env both name "
                f"{env_name!r}. Those are two trust boundaries — a worker holds the second by "
                f"necessity — so one variable means any worker also holds the credential for "
                f"{capability}. Give them separate names."
            )
        token, worker_token = _env_value(env_name), _env_value(worker_env)
        if token and worker_token and token == worker_token:
            raise RoleConfigurationError(
                f"{env_name} and {worker_env} resolve to the same value. Those are two trust "
                "boundaries: workers hold the indexing token by necessity, so sharing it hands "
                f"every worker the credential for {capability}. Generate a second random value "
                "for one of them."
            )


def _validate_serving_exposure(policy: RolePolicy, config: object) -> None:
    """Refuse an unauthenticated API on an address other machines can reach.

    The single-container posture — no authentication, published on loopback —
    is defensible and stays exactly as it is: ``all`` is exempt below, so a
    laptop, a ``pheasant up`` and every existing standalone container start
    with no configuration at all, which rule 7 requires.

    A role split is by definition a fleet. There the API is a multi-replica
    Service that must bind ``0.0.0.0``, ACL enforcement is off by default, and
    the surface behind it can register a source over any allow-listed path and
    read what it finds. The published control was a port-publishing decision
    an operator can reasonably change, and a warning in a comment. This turns
    the warning into the refusal it always described — the same shape as
    ``api`` without a queue.

    Two ways to satisfy it, because both are real deployments: set a token
    (``security.api_auth.token_env``), or declare the ingress that already
    authenticates (``security.api_auth.behind_authenticating_proxy``).
    """

    if policy.role is Role.ALL:
        return
    server = getattr(config, "server", None)
    host = str(getattr(server, "host", "") or "").strip()
    if not host or host in _LOOPBACK_HOSTS:
        return
    if not getattr(getattr(server, "api", None), "enabled", True):
        # No knowledge-base API to expose. `server.api.enabled: false` is
        # enforced rather than declared (see `create_app`'s restricted-surface
        # guard), so such a process answers the probes, `/metrics` and its own
        # `/internal` routes — each of which authenticates its own boundary.
        return
    auth = getattr(getattr(config, "security", None), "api_auth", None)
    if getattr(auth, "behind_authenticating_proxy", False):
        return
    token_env = str(getattr(auth, "token_env", "") or "")
    if _env_value(token_env):
        return
    raise RoleConfigurationError(
        f"role {policy.name!r} binds {host}, which other machines can reach, and nothing "
        "authenticates it: this API can register a source over any allow-listed path and "
        f"read what it finds. Set {token_env or 'security.api_auth.token_env'} to a random "
        "value on every serving replica, or — if an ingress already authenticates callers — "
        "set security.api_auth.behind_authenticating_proxy: true to say so. A process that "
        "serves no knowledge-base API at all (server.api.enabled: false, as the preparation "
        "workers use) needs neither."
    )


def validate_role(policy: RolePolicy, config: object, *, serves_http: bool = True) -> None:
    """Refuse a combination that cannot work, at startup.

    Each check earns its place by turning something silent into a refusal. An
    ``api`` replica publishes its syncs instead of running them, so without a
    queue a sync request would be accepted and then go **nowhere** — the
    caller gets a job id, the UI shows a job, and nothing ever indexes. A
    ``logger`` with nothing to drain is the same shape: a pod that starts
    healthy, reports ready, and does nothing forever.

    The rest are the deployment's invariants rather than the process's: a
    worker holding a credential it can never use, one secret spanning two
    trust boundaries, and an unauthenticated API on a routable address. None
    of those stops a process working, which is precisely why each needs to be
    refused here instead of discovered later.

    ``serves_http=False`` for a process that does not start the HTTP app at
    all — `pheasant worker --transport grpc` is the one, and it binds a gRPC
    port instead. The exposure check below is about a *reachable
    knowledge-base API*, so applying it to a process that serves none would
    demand a token for a surface that does not exist and refuse a working
    deployment. Everything else here still applies: what that process may
    *hold* has nothing to do with what it serves.
    """

    if policy.role is Role.API:
        queue = getattr(getattr(config, "sync", None), "queue", None)
        if not getattr(queue, "enabled", False):
            raise RoleConfigurationError(
                "role 'api' publishes index work instead of running it, so it needs "
                "sync.queue.enabled: true and an indexer draining the same queue. "
                "Without that, every sync request would be accepted and never run."
            )
    if policy.role is Role.LOGGER:
        interactions = getattr(getattr(config, "observability", None), "interactions", None)
        if not getattr(interactions, "enabled", False):
            raise RoleConfigurationError(
                "role 'logger' drains the observation log queue, so it needs "
                "observability.interactions.enabled: true. Without that there is "
                "nothing to drain and the process would idle forever while "
                "reporting itself healthy."
            )
        if not getattr(getattr(interactions, "queue", None), "enabled", False):
            raise RoleConfigurationError(
                "role 'logger' needs observability.interactions.queue.enabled: true. "
                "With the queue off, whichever process observed a call also writes "
                "it, so a separate log tier would have no work to claim."
            )
    if policy.role is Role.WORKER:
        _validate_worker_surface(config)
    else:
        # Skipped for the worker only because it cannot hold the graph token
        # at all: the surface check above already refused if it did.
        _validate_boundary_tokens(config)
    if serves_http:
        _validate_serving_exposure(policy, config)


def describe(policy: RolePolicy) -> dict[str, object]:
    """Role facts for ``/ready`` and ``/health``, so a pod can be identified."""

    return {
        "role": policy.name,
        "watcher": policy.runs_watcher,
        "scheduler": policy.runs_scheduler,
        "drains_queue": policy.drains_queue,
        "drains_log_queue": policy.drains_log_queue,
        "indexes_locally": policy.indexes_locally,
        "refreshes_graph": policy.refreshes_graph,
    }
