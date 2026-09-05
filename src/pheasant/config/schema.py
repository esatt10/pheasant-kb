from __future__ import annotations

from dataclasses import asdict, dataclass, field
from dataclasses import fields as dataclass_fields
from enum import Enum, StrEnum
from functools import cache
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

#: Patterns that keep credentials out of the index. Unlike the rest of
#: ``DEFAULT_EXCLUDES``, these are **not** merely a default a caller can
#: replace: ``security.default_exclude_secrets`` (on by default) unions them
#: into every filesystem source's effective exclude list. That distinction
#: matters because pheasant supports indexing any readable path — pointing a
#: source at ``$HOME`` with ``include: ["**/*.json", "**/*.yaml"]`` otherwise
#: sweeps up ``~/.docker/config.json``, ``~/.config/gh/hosts.yml`` and
#: friends, and a caller that supplies its own ``exclude`` list used to drop
#: every one of these patterns silently.
SECRET_EXCLUDES = [
    # Environment and dotenv files
    "**/.env",
    "**/.env.*",
    "**/*.envrc",
    # Private keys and certificates
    "**/*id_rsa*",
    "**/*id_dsa*",
    "**/*id_ecdsa*",
    "**/*id_ed25519*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/*.jks",
    "**/*.keystore",
    "**/*.asc",
    "**/*.gpg",
    # Credential stores people keep in a home directory
    "**/.ssh/**",
    "**/.gnupg/**",
    "**/.aws/**",
    "**/.azure/**",
    "**/.kube/**",
    "**/.docker/config.json",
    "**/.config/gh/**",
    "**/.config/gcloud/**",
    "**/.netrc",
    "**/.npmrc",
    "**/.pypirc",
    "**/.git-credentials",
    "**/credentials",
    "**/credentials.json",
    "**/secrets.yaml",
    "**/secrets.yml",
    "**/*.kdbx",
    # Local keychains / browser profiles
    "**/Library/Keychains/**",
    "**/.mozilla/**",
    "**/.password-store/**",
]

#: Directories that are large, generated, and never worth indexing. Kept
#: separate from the secret list because these are about *cost*, not
#: disclosure, and an operator may legitimately want to drop them.
NOISE_EXCLUDES = [
    "**/.git/**",
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/.venv/**",
    "**/venv/**",
    "**/dist/**",
    "**/build/**",
    "**/target/**",
    "**/.next/**",
    "**/.cache/**",
    "**/.tox/**",
    "**/.gradle/**",
    "**/.terraform/**",
    "**/.mypy_cache/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
]

DEFAULT_EXCLUDES = [*NOISE_EXCLUDES, *SECRET_EXCLUDES]


class SourceType(StrEnum):
    repository = "repository"
    markdown_folder = "markdown_folder"
    obsidian_vault = "obsidian_vault"
    document_folder = "document_folder"
    web_collection = "web_collection"
    single_file = "single_file"
    s3 = "s3"
    api = "api"
    memory = "memory"


class PluginSourceType(str):
    """A connector-plugin source type outside the built-in enum (Step 31.1).

    Behaves as its plain string everywhere, plus a ``.value`` property so
    every existing ``source.type.value`` call site works unchanged. Whether
    a connector actually exists for the name is checked at dispatch time
    (``connector_for_source``), not at config load — config stays loadable
    on a machine that hasn't installed the plugin yet.
    """

    @property
    def value(self) -> str:
        return str(self)


# Source types whose ``path`` is a real local directory/file (as opposed to
# the URL/connector-backed web/api/s3 types). A relative ``path`` on one of
# these is anchored to ``pheasant.workspace_root`` at config-load time.
FILESYSTEM_SOURCE_TYPES = frozenset(
    {
        SourceType.repository,
        SourceType.markdown_folder,
        SourceType.obsidian_vault,
        SourceType.document_folder,
        SourceType.single_file,
        SourceType.memory,
    }
)


_TRUTHY = {"true", "yes", "on", "1"}
_FALSY = {"false", "no", "off", "0"}


@cache
def _field_types(dc: type) -> dict[str, Any]:
    """Resolved annotations for a config dataclass.

    ``from __future__ import annotations`` makes every annotation in this file
    a string, so the nesting has to be resolved rather than read. Cached
    because it is resolved once per class and then consulted on every config
    load, including the live-edit path.

    Resolution is against the defining module's namespace, so a config
    dataclass has to live at module level — which every one of them does, and
    which is the arrangement this whole scheme assumes. A section defined
    inside a function raises here rather than silently loading as its
    defaults, and that is the right direction to fail in: the silent version
    is the bug this replaced.
    """

    return get_type_hints(dc)


def _model_type(annotation: Any) -> type | None:
    """The nested config dataclass an annotation names, ignoring ``| None``.

    A container of them is **not** one: `get_args(list[SourceConfig])` is also
    `(SourceConfig,)`, so without the origin check a list field would be
    handed to the dataclass constructor as though it were a single section.
    `sources` is the only such field today and `model_validate` skips it by
    name, which is exactly why this needs to be right rather than incidentally
    unreached — the second one would be a silent misconstruction.
    """

    if get_origin(annotation) in {list, tuple, dict, set}:
        return None
    for candidate in get_args(annotation) or (annotation,):
        if isinstance(candidate, type) and issubclass(candidate, ModelMixin):
            return candidate
    return None


def _is_path(annotation: Any) -> bool:
    """``Path`` or ``Path | None`` — deliberately not ``list[Path]``.

    `get_args(list[Path])` is also `(Path,)`, so a union check alone matches
    the list annotation and hands the whole list to `Path()`. The container
    case is answered by :func:`_is_path_list` and excluded here.
    """

    if get_origin(annotation) is list:
        return False
    return any(candidate is Path for candidate in (get_args(annotation) or (annotation,)))


def _is_path_list(annotation: Any) -> bool:
    return get_origin(annotation) is list and get_args(annotation)[:1] == (Path,)


def _build(annotation: Any, raw: Any) -> Any:
    """One value of a config tree, from plain data.

    Recursive and derived: a nested section is built because its annotation
    says it is one, not because a branch upstream remembered to name it. Paths
    are coerced the same way, so a new ``Path`` field needs no edit either.

    ``None`` and non-mapping values pass through: a section absent from YAML
    gets the dataclass's own default, which is what makes every field's
    default the single source of truth the wizard already reads.
    """

    model = _model_type(annotation)
    if model is not None:
        if not isinstance(raw, dict):
            # Absent, or already built (the live-edit path hands over
            # instances). Either way the caller's default stands.
            return raw if isinstance(raw, model) else model()
        values = dict(raw)
        _coerce_scalar_fields(model, values)
        hints = _field_types(model)
        return model(
            **{
                name: _build(hints[name], value)
                for name, value in values.items()
                if name in model.__dataclass_fields__
            }
        )
    if raw is None:
        return None
    if _is_path(annotation):
        return Path(raw)
    if _is_path_list(annotation):
        return [Path(item) for item in raw or []]
    return raw


def _coerce_scalar_fields(dc: type, raw: dict[str, Any]) -> None:
    """Coerce int/float/bool fields in-place; raise ValueError on garbage.

    The dataclass constructors accept whatever they are given, so without
    this a config (or a UI payload) carrying e.g. ``max_chars: "oops"``
    validates "successfully" and only blows up mid-sync. Coercion keeps the
    friendly behaviours (``"8765"`` → 8765, ``"true"`` → True) while turning
    unusable values into an immediate, catchable error.
    """
    for f in dataclass_fields(dc):
        if f.name not in raw or raw[f.name] is None:
            continue
        annotation = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", "")
        base = annotation.replace(" ", "").removesuffix("|None")
        value = raw[f.name]
        try:
            if base == "int" and not isinstance(value, bool) and not isinstance(value, int):
                raw[f.name] = int(value)
            elif base == "float" and not isinstance(value, (int, float)):
                raw[f.name] = float(value)
            elif base == "bool" and not isinstance(value, bool):
                text = str(value).strip().lower()
                if text in _TRUTHY:
                    raw[f.name] = True
                elif text in _FALSY:
                    raw[f.name] = False
                else:
                    raise ValueError(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{dc.__name__}.{f.name} expects {base}, got {value!r}") from exc


@dataclass
class ModelMixin:
    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        def conv(v: Any) -> Any:
            if isinstance(v, Path):
                return str(v) if mode == "json" else v
            if isinstance(v, Enum):
                return v.value
            if isinstance(v, str) and type(v) is not str:
                # A *subclass* of str — `PluginSourceType` is the one that
                # occurs — is not plain data, and consumers that dispatch on
                # the exact type refuse it. PyYAML's representer is one:
                # `yaml.safe_dump` raises `RepresenterError("cannot represent
                # an object")` for it, so `config_hash` crashed for any config
                # using a connector plugin — which is every third-party
                # connector and all five first-party SaaS ones. A dump is
                # plain data in both modes; the loader re-creates the rich
                # type on the way back in.
                return str(v)
            if isinstance(v, ModelMixin):
                return v.model_dump(mode=mode)
            if isinstance(v, list):
                return [conv(i) for i in v]
            if isinstance(v, dict):
                return {k: conv(val) for k, val in v.items()}
            return v

        return {k: conv(v) for k, v in asdict(self).items()}


@dataclass
class PheasantSettings(ModelMixin):
    name: str = "local-pheasant"
    description: str = "Lightweight MCP knowledge graph and retrieval server"
    environment: str = "local"
    log_level: str = "INFO"
    state_path: Path = Path("/state")
    workspace_root: Path = Path("/workspace")
    exports_path: Path = Path("/exports")


@dataclass
class McpSettings(ModelMixin):
    enabled: bool = True
    transports: dict[str, bool] = field(
        default_factory=lambda: {"stdio": True, "streamable_http": True, "sse": False}
    )


@dataclass
class ApiSettings(ModelMixin):
    enabled: bool = True
    openapi: bool = True
    # Browser origins allowed to call this API. The API is unauthenticated by
    # design (a local-first tool behind the operator's own perimeter), which
    # is exactly why the origin list must not be "*": with a wildcard, any
    # page the user happens to visit can script the whole surface — read the
    # index, rewrite the config, point the embedder at an attacker's host and
    # ship a server-side API key with it. The defaults cover the UI sidecar
    # and local dev; add explicit origins for anything else.
    cors_origins: list[str] = field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8765",
            "http://127.0.0.1:8765",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    )
    # Escape hatch for deployments that genuinely front this with their own
    # authenticating ingress and need `*` back. Opt-in, never the default.
    cors_allow_all_origins: bool = False
    #: Requests allowed to be in flight at once before the surplus is refused
    #: with 429 + Retry-After. **0 disables it**, which is the pre-35.6
    #: behavior and the right answer for one container: a 429 to the only user
    #: is worse than making them wait. Set it on replicas, where shedding lets
    #: a load balancer retry elsewhere instead of every replica queueing.
    max_concurrent_requests: int = 0
    #: Seconds to keep serving after SIGTERM while `/ready` already reports
    #: 503, so a load balancer stops sending new work before the process
    #: stops accepting it. 0 disables the delay. Must be shorter than the
    #: orchestrator's termination grace period or the pod is killed mid-drain.
    drain_seconds: int = 0
    #: Seconds between checks for a graph written by another process, for the
    #: ``api`` role only. 30 by default because an api replica that never
    #: re-reads the graph answers graph queries from whatever the graph was
    #: when its pod started, forever and silently. 0 disables it; the other
    #: roles ignore it, because they index their own graph and reload it
    #: through the sync worker.
    graph_refresh_seconds: int = 30


@dataclass
class UiSettings(ModelMixin):
    enabled: bool = True
    graph_visualization: bool = True


@dataclass
class ServerSettings(ModelMixin):
    host: str = "0.0.0.0"
    port: int = 8765
    #: Which jobs this process takes on: ``all`` (the default and today's
    #: behavior), ``api`` (serve only — publishes index work instead of
    #: running it), ``indexer`` (watch, schedule and drain the queue),
    #: ``graph`` (serve the resident graph only), or ``worker`` (preparation
    #: only). `pheasant serve --role` overrides this.
    #: See :mod:`pheasant.deployment.roles`.
    role: str = "all"
    mcp: McpSettings = field(default_factory=McpSettings)
    api: ApiSettings = field(default_factory=ApiSettings)
    ui: UiSettings = field(default_factory=UiSettings)


@dataclass
class StorageSettings(ModelMixin):
    """On-disk state layout + graph snapshot/retention policy (Synapse 21.6A).

    - ``graph_snapshots`` toggles zstd-compressed timestamped graph snapshots
      written after a successful sync. Defaults **on** — a snapshot is additive
      history beside the (unchanged) ``graph.latest.json`` and is bounded by the
      retention cap below, so standalone behavior stays sane.
    - ``graph_snapshot_interval_seconds`` is the minimum spacing between
      snapshots; syncs closer together than this reuse the most recent snapshot.
    - ``compression`` selects the snapshot codec (``zstd`` only, for now).
    - ``max_state_size_gb`` caps total snapshot bytes per KB; oldest snapshots
      are evicted first when exceeded. ``graph.latest.json``, the SQLite db, and
      the contract are never evicted.
    - ``graph_checkpoint_seconds`` is the minimum spacing between mid-sync
      writes of ``graph.latest.json``. The graph used to reach disk only when a
      sync *completed*, so stopping the container during a first index threw
      that work away. 0 disables checkpointing (end-of-sync save only).
    """

    #: Where the published knowledge graph lives: ``rows`` (default) or
    #: ``node_link_json``.
    #:
    #: ``rows`` puts it in ``graph_nodes``/``graph_edges`` in the state
    #: database, beside the artifacts it describes. A commit then writes only
    #: what changed and a serving replica holds nothing: measured on this
    #: repo's own benchmark at 100k files, the commit after a one-file change
    #: went from 9.1s to 10ms and stopped growing with the graph, and the
    #: 1.5GB every query-answering process had to be given went away. The
    #: trade is disk — the rows and their two indexes are larger than the
    #: compressed file they replace — which is stated in
    #: ``pheasant.capacity`` and reported by ``pheasant scan``.
    #:
    #: ``node_link_json`` is the pre-35.10 single zstd file. Kept selectable,
    #: and kept working, so a region that hits trouble reverts with one line
    #: rather than a downgrade. A region switched to ``rows`` imports its
    #: existing file once at boot and keeps it as ``*.migrated``.
    graph_format: str = "rows"
    graph_snapshots: bool = True
    graph_snapshot_interval_seconds: int = 900
    graph_checkpoint_seconds: int = 60
    #: Expected seconds between interruptions — a container restart, a
    #: redeploy, an OOM kill. This is the one number an operator actually
    #: knows, and it is what sets the checkpoint interval (see
    #: ``optimal_checkpoint_interval``). 24h suits a normally-running
    #: deployment; lower it on spot/preemptible instances, which is exactly
    #: the case where more frequent checkpoints pay for themselves.
    checkpoint_mtbf_seconds: int = 86_400
    #: Ceiling on the derived interval, so worst-case rework stays bounded no
    #: matter what the formula returns. 30 minutes of lost indexing is
    #: recoverable; an unbounded interval on a very large graph is not.
    graph_checkpoint_max_seconds: int = 1_800
    compression: str = "zstd"
    sqlite_path: Path | None = None
    graph_path: Path | None = None
    manifest_path: Path | None = None
    max_state_size_gb: float = 10

    # -- Phase 35.2: where state actually lives -------------------------------
    #: ``sqlite`` (default) or ``postgres``. SQLite is a file and permits one
    #: writer process per knowledge base, which is pheasant's hard scaling
    #: ceiling; Postgres is what lets 35.4 give each *source* its own lease so
    #: several indexers can commit at once. Leaving this alone keeps a region
    #: byte-identical to pre-35.2 and needing no infrastructure at all.
    backend: str = "sqlite"
    #: Name of the environment variable holding the libpq DSN. A DSN carries a
    #: password, so — like every other credential in pheasant — only the
    #: variable *name* is ever written to YAML. There is deliberately no field
    #: to paste the DSN itself into.
    dsn_env: str = "PHEASANT_DATABASE_URL"
    #: Server-side connections this process may hold. Unlike a SQLite file
    #: handle a Postgres connection is a server process, so this is a real
    #: resource on the database, not just on the client.
    pool_size: int = 10


@dataclass
class EmbeddingsSettings(ModelMixin):
    """Optional embed-on-sync provider (Synapse 21.4). Off by default.

    ``provider`` is ``openai-spec`` (POST {base_url}/embeddings, the same
    wire format the pheasant-flock router uses) or ``stub``
    (deterministic, offline). The API key is read from the environment
    variable named by ``api_key_env`` and never stored in config/state.

    ``dimensions`` is unset (``None``) by default: the ``dimensions``
    parameter is simply omitted from the embedding request, so the
    provider returns the model's own native size (1536 for
    ``text-embedding-3-small``, 3072 for ``text-embedding-3-large``, ...) —
    changing ``model`` therefore changes the vector width without a second
    edit. Set an explicit number only to shrink vectors for storage
    (OpenAI's ``-3`` models support Matryoshka truncation) or to pin an
    exact size across a Synapse fleet.
    """

    enabled: bool = False
    provider: str = "openai-spec"
    model: str = "text-embedding-3-small"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    dimensions: int | None = None
    batch_size: int = 64
    #: Bounded retry on *transient* embedding failures (TLS blips, 429s, 5xx).
    #: Indexing a large corpus is hundreds of HTTPS calls, and without this a
    #: single flaky one aborts the whole sync — a real 12,667-file run died
    #: ~45 minutes in on an `SSLV3_ALERT_BAD_RECORD_MAC`. A wrong key or a
    #: malformed request is never retried.
    max_retries: int = 4
    retry_backoff_seconds: float = 1.0
    #: A 429 during bulk indexing is provider flow control, not a failed
    #: source.  Keep retrying within this cumulative wait budget before the
    #: error is allowed to escape and let the durable source queue retry it.
    #: Zero restores the ordinary ``max_retries`` behavior.
    rate_limit_max_wait_seconds: float = 300.0


@dataclass
class VectorStoreSettings(ModelMixin):
    """Vector index backend; vectors live under ``<path>/<kb_id>/``.

    ``provider`` is ``lancedb`` (default; optional ``[vector]`` extra) or
    ``numpy`` (always available flat file). ``path`` defaults to
    ``<state>/vectors``.
    """

    provider: str = "lancedb"
    path: Path | None = None


@dataclass
class SearchRankingSettings(ModelMixin):
    """The numbers ranking reads. Every default is the shipped constant.

    These were module constants in ``pheasant.search.sqlite_store`` and
    ``pheasant.search.hybrid`` until the tuning plane needed to address them:
    a parameter no surface can name is a parameter no experiment can change.
    Leaving this whole block unset reproduces the 2026-08-03 retrieval
    overhaul's measured values exactly, so a region that never opens it ranks
    as it always did.

    **Fleet-scoped by construction.** These apply to the region, and an
    applied tuning bundle overlays them for every replica reading that
    ``/state``. There is deliberately no per-request or per-principal
    override: retrieval parameters that varied by caller would make two agents
    disagree about what the region contains, and would make every number the
    evaluation plane publishes a measurement of whoever happened to ask.

    Bounds are enforced in :mod:`pheasant.search.ranking`, not here, because
    a proposed bundle has to be clamped by the same rule as a hand-edited
    config and there can only be one home for that rule.
    """

    #: BM25 / ``ts_rank_cd`` column weights. ``title`` is the file's basename.
    title_weight: float = 8.0
    path_weight: float = 3.0
    heading_weight: float = 2.0
    text_weight: float = 1.0
    #: Structural priors, applied as a divisor on the negative BM25 cost so
    #: they scale a match rather than displacing it.
    depth_prior: float = 0.05
    test_prior: float = 0.60
    sample_prior: float = 0.30
    prefer_bonus: float = 0.35
    prior_floor: float = 0.25
    #: Reciprocal-rank-fusion constant and the per-arm fusion weights.
    rrf_k: float = 60.0
    text_arm_weight: float = 1.0
    vector_arm_weight: float = 1.0
    graph_arm_weight: float = 1.0
    #: Over-fetch multiplier applied when a filter will drop candidates.
    filter_overfetch: float = 3.0


@dataclass
class SearchSettings(ModelMixin):
    default_mode: str = "hybrid"
    max_results_default: int = 10
    embeddings: EmbeddingsSettings = field(default_factory=EmbeddingsSettings)
    vector_store: VectorStoreSettings = field(default_factory=VectorStoreSettings)
    ranking: SearchRankingSettings = field(default_factory=SearchRankingSettings)
    # Synapse Step 34.5b: run search.graph_search._scan_edges through the
    # vendored WASM accelerator instead of pure Python. Default off — needs
    # the [wasm] extra; falls back to pure Python on any failure or if the
    # extra isn't installed. 34.4 benchmark: a consistent 2-8x win, growing
    # with edge count, at every scale tested (500-64,000 edges).
    wasm_relationship_search: bool = False


@dataclass
class CaptionerSettings(ModelMixin):
    """Image captioner for multi-modal ingestion (Synapse 25.4 session A).

    When an image source is configured, image files are captioned into text
    that flows through the normal chunk -> embed -> graph path (architecture
    §8). ``provider`` is ``stub`` (default; deterministic + offline, the only
    path used by tests) or ``openai-spec`` (POST {base_url}/chat/completions
    with an ``image_url`` content part — a vision-capable chat model). The API
    key is read from the environment variable named by ``api_key_env`` and
    never stored. A sidecar ``<image>.caption.txt`` always wins, providing an
    authored caption with no model or network.
    """

    provider: str = "stub"
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    prompt: str = "Describe this image in one concise sentence for search indexing."


@dataclass
class TranscriberSettings(ModelMixin):
    """Audio transcriber for multi-modal ingestion (Synapse 25.4 session B).

    When an audio source is configured, audio files are transcribed into text
    that flows through the normal chunk -> embed -> graph path (architecture
    §8). ``provider`` is ``stub`` (default; deterministic + offline, the only
    path used by tests — no audio library required) or ``openai-spec`` (POST
    {base_url}/audio/transcriptions, a multipart upload to a speech-to-text
    model). The API key is read from the environment variable named by
    ``api_key_env`` and never stored. A sidecar ``<audio>.transcript.txt``
    always wins, providing an authored transcript with no model or network.
    """

    provider: str = "stub"
    model: str = "whisper-1"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"


@dataclass
class ExtractorSettings(ModelMixin):
    """Document text extractor for PDF/DOCX (and optionally HTML) ingestion.

    Before this existed, ``.pdf`` and ``.docx`` were *accepted* by the
    ingestion pipeline and then produced no text at all: the artifact was
    discovered, hashed and given a graph node, but ``read_text`` returned
    ``""``, so it contributed zero chunks and was unfindable by content.

    Unlike the captioner/transcriber, no provider here makes a network call or
    uses a model — the text is already in the file — so every option is fully
    offline and deterministic.

    ``provider``:

    - ``auto`` (default) — ``pymupdf``/``python-docx`` when importable (both
      already core deps), falling back to the pure-stdlib builtin. Never
      raises into a sync.
    - ``native`` — prefer the third-party libraries (best fidelity on PDFs
      with CID/Type0 fonts or complex layout).
    - ``builtin`` — standard library only: ``zlib`` + content-stream scanning
      for PDF, ``zipfile`` + ``xml.etree`` for DOCX. No third-party imports.
    - ``sandboxed`` — the builtin PDF tokenizer inside the Phase-34 WASM
      sandbox (fuel + memory cap, zero host capabilities). For regions
      ingesting PDFs from untrusted connector sources; needs the ``[wasm]``
      extra and raises with a hint if it is missing rather than silently
      running unsandboxed.

    ``html_text`` strips markup from ``.html``/``.htm``/``.xhtml`` so their
    prose indexes instead of their tags. It defaults to **false** because
    those extensions have always been indexed as raw markup: turning it on
    changes the indexed text (and therefore chunk boundaries) of an existing
    knowledge base, so it is an explicit operator choice rather than a
    surprise on upgrade.

    A sidecar ``<file>.extract.txt`` always wins, providing authored text with
    no extractor at all (mirrors the caption/transcript sidecars).
    """

    provider: str = "auto"
    html_text: bool = False


@dataclass
class IngestionSettings(ModelMixin):
    """Ingestion-path enrichment knobs. Standalone-safe defaults.

    ``captioner`` only takes effect for sources that include image files,
    ``transcriber`` only for sources that include audio files, and
    ``extractor`` only for sources that include PDF/DOCX files (or when
    ``extractor.html_text`` is on); a text-only region never builds any of
    them (and the defaults need no extra dependency anyway).
    """

    captioner: CaptionerSettings = field(default_factory=CaptionerSettings)
    transcriber: TranscriberSettings = field(default_factory=TranscriberSettings)
    extractor: ExtractorSettings = field(default_factory=ExtractorSettings)


@dataclass
class WatcherSettings(ModelMixin):
    enabled: bool = True
    max_watch_paths: int = 100
    debounce_ms: int = 1500
    batch_window_ms: int = 5000


@dataclass
class GitSettings(ModelMixin):
    enabled: bool = True
    detect_commit_changes: bool = True
    detect_branch_switch: bool = True
    reindex_on_commit: bool = True


@dataclass
class SchedulerSettings(ModelMixin):
    enabled: bool = True
    interval_seconds: int = 900


@dataclass
class SyncLimitsSettings(ModelMixin):
    """Guardrails on how much one filesystem source may pull in.

    pheasant lets a source point at any readable path, which makes
    "accidentally indexed my home directory" a realistic mistake rather than
    a hypothetical one. These limits turn that mistake into a refusal with an
    actionable message instead of a process that consumes memory until it
    dies. They are checked *during* traversal, before any file is read.

    Any field set to ``None`` disables that particular limit. A sync that
    trips a limit indexes nothing — a partial index would be
    non-deterministic, and silently indexing the first N files of a home
    directory is a worse outcome than a clear stop. Pass ``full_scan`` on the
    sync surfaces (or raise the limits here) to index it anyway.
    """

    #: Matching files, after include/exclude. A large monorepo is ~100k.
    max_files: int | None = 50_000
    #: Skip any single file bigger than this — a 2 GB model checkpoint or
    #: database dump has no business in a text index and would be read whole.
    max_file_size_mb: int | None = 25
    #: Total matched content. Chunking and embedding both scale off this.
    max_total_mb: int | None = 4096
    #: Symlinks are not followed by default: a home directory routinely
    #: contains links that escape the source root or form loops.
    follow_symlinks: bool = False


@dataclass
class SyncConcurrencySettings(ModelMixin):
    """Bounded indexing concurrency, shared by every configuration surface.

    File workers only prepare immutable parse results. SQLite, graph,
    manifest, and vector-store mutations remain coordinated by the engine so
    increasing a cap cannot change stable IDs or persisted graph bytes.
    """

    max_parallel_sources: int = 4
    max_parallel_files: int = 8
    max_parallel_embeddings: int = 4
    file_executor: str = "thread"
    remote_worker_urls: list[str] = field(default_factory=list)
    remote_worker_enabled: bool = False
    remote_worker_token_env: str = "PHEASANT_INDEX_WORKER_TOKEN"
    remote_worker_timeout_seconds: int = 120
    #: Files per request to a remote worker. A batch amortizes the request
    #: overhead and carries one deadline for the group, but every task in it
    #: holds its file's bytes in memory on both sides — so this is a memory
    #: knob as much as a throughput one. Eight is small enough that the
    #: default 25 MB file limit cannot surprise a worker.
    remote_worker_batch_size: int = 8
    #: ``http`` (stdlib, no extra) or ``grpc`` (needs the ``[grpc]`` extra).
    #: Retry, failover, breakers and deadlines are transport-independent, so
    #: this changes bytes on the wire and nothing about durability.
    worker_transport: str = "http"
    lock_timeout_seconds: int = 120


@dataclass
class SyncQueueSettings(ModelMixin):
    """The durable index work queue (Phase 35.5).

    Off by default, and that is a design decision rather than caution: with
    it off, ``sync_all`` keeps its remaining sources in a Python list exactly
    as it always has, so a single container indexing a folder needs no queue
    to do it. Turning it on buys three things a list cannot give: a backlog
    that survives a restart, a depth other processes can read (and a
    scheduler can scale on), and a source that keeps failing being
    dead-lettered instead of retried forever.

    ``local`` is the state store itself — no broker to run, and it works on
    both SQLite and Postgres. ``nats`` is for a fleet that has outgrown one
    database; it does not buy correctness the local queue lacks.
    """

    enabled: bool = False
    backend: str = "local"
    #: Seconds a claimed task stays invisible to other workers. Heartbeats
    #: extend it, so this bounds *silence* from a claimer, not work.
    visibility_seconds: int = 300
    #: Attempts before a task is dead-lettered. A dead task is kept, never
    #: deleted, so it can be replayed once the cause is fixed.
    max_attempts: int = 3
    nats_servers: list[str] = field(default_factory=list)
    nats_stream: str = "PHEASANT_INDEX"
    nats_subject: str = "pheasant.index.tasks"
    nats_durable: str = "pheasant-indexers"
    #: Subject prefix for graph-commit announcements (the kb id is appended).
    #: Core NATS pub/sub rather than a JetStream stream, because every replica
    #: must hear it and a dropped one costs a poll interval — see
    #: :mod:`pheasant.sync.graph_events`.
    nats_graph_subject: str = "pheasant.graph.committed"


@dataclass
class SyncSettings(ModelMixin):
    watcher: WatcherSettings = field(default_factory=WatcherSettings)
    git: GitSettings = field(default_factory=GitSettings)
    scheduler: SchedulerSettings = field(default_factory=SchedulerSettings)
    limits: SyncLimitsSettings = field(default_factory=SyncLimitsSettings)
    concurrency: SyncConcurrencySettings = field(default_factory=SyncConcurrencySettings)
    queue: SyncQueueSettings = field(default_factory=SyncQueueSettings)


@dataclass
class GraphSettings(ModelMixin):
    """How much graph the knowledge graph should actually keep."""

    #: Optional internal graph-query service. When set, API and MCP serving
    #: processes keep no persisted graph in RAM and send graph reads to this
    #: endpoint instead. Standalone remains in-process when this is ``None``.
    query_service_url: str | None = None
    #: Bearer token environment variable shared by graph clients and the
    #: graph-service role. The secret itself never belongs in YAML.
    query_service_token_env: str = "PHEASANT_GRAPH_SERVICE_TOKEN"
    #: Per-call deadline. Graph/hybrid search fails explicitly at this bound;
    #: it never falls back to loading a full graph into an API replica.
    query_service_timeout_seconds: float = 30.0

    #: Wire agent-memory records into the graph (Step 33.7): `about` edges to
    #: what a record refers to, plus `supersedes` between corrections. A no-op
    #: without a memory source, so turning it off only matters to a region that
    #: has one and would rather keep memory out of its graph.
    memory_entity_bridging: bool = True
    # Synapse Step 34.5a: run graph.enrichment.resolve_cross_source_edges
    # through the vendored WASM accelerator instead of pure Python. Default
    # off — needs the [wasm] extra; falls back to pure Python on any
    # failure or if the extra isn't installed. 34.4 benchmark: a
    # conditional win — loses to Python below ~1,300-2,500 edges (today's
    # demo corpus sits at ~2,900, right at the breakeven point), wins
    # modestly above that. Opt in for large/growing multi-source graphs.
    wasm_cross_source_resolution: bool = False

    # -- Phase 35.3: the in-RAM graph has a measured ceiling ------------------
    #: Warn once per sync when the graph passes this many nodes. **Not a
    #: refusal** — unlike `sync.limits`, which stops a source before any work
    #: happens, by the time this trips the graph already exists and refusing
    #: would throw away a completed index.
    #:
    #: The default is derived from measurement, not taste. `graph/capacity.py`
    #: measured a real-shaped graph at four scales and found a flat ~2.4 KB of
    #: process RSS per node. The shipped container limit is 6 Gi, and the graph
    #: is roughly 60% of process RSS, so ~1.5M nodes (~240k files) is where a
    #: default container is genuinely full — past that it is OOM-killed
    #: mid-sync, which presents as an unexplained restart rather than as a
    #: capacity problem. Raise both together for a larger container; shard once
    #: raising them stops being comfortable. Set to None to disable.
    max_nodes: int | None = 1_500_000


@dataclass
class SynapseSettings(ModelMixin):
    """Synapse federation (Synapse 21.5). Standalone-safe: all router-facing
    behavior no-ops when ``router_url`` is unset and ``publish`` is false.

    - ``publish`` gates contract publication + the NDJSON event stream from the
      sync engine. Default off → a router-less pheasant behaves exactly as
      before. The local ``GET /contract`` serving path still works once a
      contract has been published.
    - ``router_url`` is the Synapse router base URL; when set, the engine POSTs
      the ``sync.completed`` event (with the inline contract) to
      ``<router_url>/v1/synapse/events`` (fail-soft).
    - ``fleet_id`` / ``endpoint`` are stamped into the contract so the router
      can pin the fleet embedding space and pull/route to this region.
    """

    publish: bool = False
    router_url: str | None = None
    fleet_id: str | None = None
    endpoint: str | None = None
    webhook_timeout_seconds: float = 5.0
    # Synapse 24.4: optional Ed25519 contract signing. ``signing_key_ref`` is a
    # secret *reference* (``env://NAME`` or a bare env-var name) resolving to a
    # base64 32-byte Ed25519 private seed — the plaintext key never lands in the
    # config or on disk. When unset (default) the contract's
    # ``integrity.signature`` stays ``null`` and a standalone pheasant is
    # unchanged. The router verifies the signature against an out-of-band public
    # key when its fleet sets ``require_signed: true``.
    signing_key_ref: str | None = None


@dataclass
class RetrievalSettings(ModelMixin):
    """How hard the answering workflows look before they answer.

    These knobs already existed — as untyped keys inside
    ``assistant.workflow_options``, documented only in a workflow module's
    ``DEFAULTS`` dict. That made them invisible to the config surface: not in
    the schema, not validated, not editable from the UI, and not something an
    MCP client could ask about. This block is their typed home.

    Precedence is deliberately *low*: these are merged **under**
    ``assistant.workflow_options``, which is merged under the per-request
    ``options``. So an existing config that tuned ``workflow_options`` keeps
    winning, and an agent overriding a criterion for one call still wins over
    both (see :func:`pheasant.assistant.workflows.resolve_options`).

    A field left at ``None`` is not merged at all — the workflow's own
    ``DEFAULTS`` apply, which is what makes this block additive rather than a
    second source of truth for values it does not care about.
    """

    #: plan → retrieve → grade turns before answering with what is in hand.
    max_rounds: int | None = 2
    #: Passages fetched per query per search mode.
    per_query_results: int | None = 6
    #: Total passages offered to the synthesis step.
    max_context_passages: int | None = 10
    #: Search modes to fan out over. "vector" is dropped automatically when
    #: no vector index is built, so leaving it on is safe.
    retrieval_modes: list[str] | None = field(default_factory=lambda: ["text", "vector"])
    #: Walk the graph out of the best hits for structurally-related material.
    expand_graph: bool | None = True
    expand_depth: int | None = 1
    expand_per_node: int | None = 3
    #: Ask the model to grade its own evidence before answering.
    grade_evidence: bool | None = True
    #: Drop [n] markers that do not resolve to a real citation.
    verify_citations: bool | None = True
    #: Graph facts surfaced alongside the answer.
    max_facts: int | None = 12

    def as_options(self) -> dict[str, Any]:
        """The subset that is actually set, as workflow-option keys."""
        return {key: value for key, value in self.model_dump().items() if value is not None}


@dataclass
class AssistantSettings(ModelMixin):
    """Grounded chat over the knowledge graph (query-time only).

    The assistant is a **retrieval-time** surface: it runs the ordinary
    hybrid self-search, assembles the hits into a grounded prompt, and asks
    a chat model to answer with citations. It is never part of the indexing
    path, so determinism there is untouched — and with no provider
    configured it falls back to a deterministic extractive answer, which is
    what the offline test suite exercises.

    ``provider: "auto"`` picks the first provider whose ``api_key_env`` is
    populated in the server environment (Anthropic, then OpenAI, then
    Gemini). ``allow_session_keys`` lets a UI user paste a key for the
    lifetime of a browser session: it is held in process memory only, keyed
    by an opaque token, and is never written to config, state, or logs.
    """

    enabled: bool = True
    provider: str = "auto"  # auto | anthropic | openai | gemini | none
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    allow_session_keys: bool = True
    session_key_ttl_minutes: int = 720
    max_context_chunks: int = 8
    max_output_tokens: int = 4096
    request_timeout_seconds: float = 90.0
    max_facts: int = 12
    # Which question-answering workflow runs. "auto" picks the LangGraph
    # agent when the [agent] extra is installed AND a model is reachable,
    # else the single-pass workflow. Any name registered through the
    # `pheasant.agent_workflows` entry-point group is also valid.
    workflow: str = "auto"
    # Per-workflow knobs, passed through untouched (see the workflow's
    # DEFAULTS for the keys it honors). Merged OVER `retrieval` below, so a
    # config that already tuned these keeps behaving exactly as it did.
    workflow_options: dict[str, Any] = field(default_factory=dict)
    # Typed retrieval criteria (rounds, depth, breadth). See RetrievalSettings.
    retrieval: RetrievalSettings = field(default_factory=RetrievalSettings)


@dataclass
class MemorySynthesisSettings(ModelMixin):
    """L3 compaction (Phase 4): abstractive merge of a near-duplicate
    cluster deterministic methods cannot resolve — complementary partial
    facts, progressive refinement, or genuine abstraction across records.
    See `docs/memory-system.md` §8 for what deterministic clustering (L1/L2)
    already handles and why this tier exists for what it cannot.

    Mirrors `AssistantSettings` field-for-field on purpose: an operator who
    has already configured the assistant's model recognizes every knob
    here, and the fields feed the exact same `assistant.llm`/
    `assistant.catalog` machinery — no second provider stack.

    **Off by default and never on an automatic beat.** Unlike consolidation
    or compaction (Phase 3), which are pure metadata operations, this makes
    a network call — CLAUDE.md rule 1 forbids an LLM on the indexing path,
    and the scheduler's maintenance beat is exactly that path. Synthesis
    runs only through the explicit `memory_synthesize` MCP tool /
    `POST /memory/synthesize`, never automatically, so `pytest` stays
    network-free by construction (the default `provider` resolves to
    nothing reachable) rather than by mocking.
    """

    enabled: bool = False
    provider: str = "auto"  # auto | anthropic | openai | gemini | none
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    max_output_tokens: int = 1024
    request_timeout_seconds: float = 60.0
    #: Hard cap on model calls in one pass. The content-addressed cache
    #: (a cluster's member-id set + model id + rule id, checked against the
    #: `memory_compactions` ledger before any call) already makes a repeat
    #: pass over unchanged clusters cost zero regardless — this bounds the
    #: *first* pass over a large store, or one after many new clusters
    #: appeared at once.
    max_calls_per_pass: int = 20
    #: Conservative char-based proxy for input size — no tokenizer
    #: dependency, matching the no-extra-dependency posture the rest of
    #: this module keeps. A cluster whose combined member text exceeds this
    #: is skipped rather than truncated silently into a partial merge.
    max_input_chars: int = 6000
    #: A cluster below this size is not a synthesis candidate — medoid
    #: promotion (L2, Phase 3) already resolves anything smaller cleanly
    #: and losslessly; synthesis is reserved for what that tier cannot.
    min_cluster_size: int = 3
    #: The other half of that gate, and the one that decides *what the model
    #: is asked to do* without a model. A cluster whose members are already
    #: near-identical (high Jaccard over normalized tokens) is what medoid
    #: promotion solves losslessly and for free; synthesis exists for the
    #: opposite shape — records about one subject that say genuinely
    #: *different* things (complementary partials, progressive refinement,
    #: abstraction across instances). Above this similarity a cluster is
    #: skipped, so spend goes only where deterministic compaction provably
    #: cannot help. Matters most when `compaction_enabled` is off (the
    #: default): nothing has been demoted, so without this gate every
    #: near-duplicate bucket would reach the model.
    max_jaccard: float = 0.55


@dataclass
class MemoryFormationSettings(ModelMixin):
    """Turning recorded interactions into memory candidates, deterministically.

    The observation plane (``observability.interactions``) records what was
    asked and what came back. This is the half that reads it and proposes
    records — and, deliberately, only *proposes*: a candidate becomes a
    record through :meth:`pheasant.memory.store.MemoryStore.append` like
    every other write, so nothing here is a second ingestion path.

    **No model runs in this path.** Rules are counting and string matching
    over recorded inputs, so a pass is reproducible and a candidate's id is
    a deterministic hash of what produced it. Every decision is ledgered
    with its ``rule_id`` and ``params_hash``, exactly as compaction's
    already is, so a repeat pass over unchanged observations under
    unchanged parameters writes nothing.

    See ``docs/memory-formation.md``.
    """

    #: Master switch. Off, nothing reads the observation plane and no
    #: candidate is ever minted — the region behaves exactly as it did
    #: before formation existed.
    enabled: bool = False
    #: Maintain one record per session, refined through dialog: scope
    #: ``session``, subject the session id, each refinement naming the
    #: previous one in ``supersedes``. `current_only` (on by default) then
    #: returns exactly one record per session and `as_of` reads its
    #: history — the validity model doing what it was built for, with no
    #: new primitive. Only meaningful when `enabled`.
    session_digest: bool = True
    #: Admit a candidate above threshold without review.
    #:
    #: **Off by default**, the same posture `compaction_enabled` and
    #: `supersede_retention_days` take: it changes what a *default* query
    #: returns, which is a decision an operator should make rather than
    #: inherit. Left off, candidates accumulate for review in the Memory
    #: tab and nothing is written until a person promotes one. An
    #: auto-admitted record carries the `formed` tag and its candidate row
    #: records `admitted_by`, so a machine-formed record is always
    #: distinguishable from a written one.
    auto_admit: bool = False
    #: How many times a pattern must be observed before it is a candidate.
    #: Counts run over a stream that is *sampled under load* (the log tier
    #: drops rather than blocks), so a busy region reaches these thresholds
    #: later — not incorrectly.
    min_observations: int = 3
    #: …and across how many distinct sessions. One session repeating itself
    #: is a habit; several sessions agreeing is a signal. Guards against a
    #: single loop minting steering that reorders results for everyone.
    min_sessions: int = 2
    #: Hard cap on candidates minted in one pass, bounding a first pass over
    #: a large ledger the way `synthesis.max_calls_per_pass` bounds its own.
    max_candidates_per_pass: int = 50
    #: A pending candidate nobody promoted expires after this many days.
    #: Rejections are remembered separately and are never re-proposed.
    candidate_ttl_days: int = 30
    #: Rules to run, by id. Each is versioned so a rule's decisions stay
    #: attributable after its logic changes: a new version is a new
    #: `rule_id`, never an edit to an existing one.
    rules: list[str] = field(
        default_factory=lambda: [
            "session-digest-v1",
            "alias-cooccurrence-v1",
            "path-affinity-v1",
            "retrieval-gap-v1",
        ]
    )


@dataclass
class MemorySettings(ModelMixin):
    """Agent-memory consolidation policy (Step 33.2).

    Consolidation archives superseded records (an explicit correction is the
    only default trigger); per-scope TTL decay is opt-in — ``None`` means the
    scope never expires. Archived record files are renamed in place
    (``.md.archived``) and never deleted.
    """

    consolidation_enabled: bool = True
    session_ttl_days: int | None = None
    user_ttl_days: int | None = None
    org_ttl_days: int | None = None
    #: Days a superseded or TTL-expired record stays indexed — hidden from
    #: default results by the existing `valid_until` query-time predicate,
    #: but still reachable via `as_of` / `current_only=False` — before
    #: consolidation actually archives its file (Phase 2). `0` (the
    #: default) reproduces the pre-Phase-2 behavior: archive the instant a
    #: record is no longer current.
    #:
    #: **Opt-in, not on by default, and that is a measured trade-off, not
    #: caution for its own sake.** Retaining a corrected record's near-
    #: duplicate text alongside its correction gives the hybrid RRF fusion
    #: two close competitors for one query instead of one — `stale_leak_rate`
    #: stays 0.0 (the query-time `valid_until` predicate does correctly
    #: exclude the old record from every result set), but `update_accuracy`
    #: was observed to swing 0.75-1.0 run to run on the same seed in
    #: `tests/test_memory_benchmark.py` at the default `retention=7`, from
    #: exactly this near-duplicate ranking competition. A region that wants
    #: the documented `as_of` guarantee — "what did we believe last week" —
    #: sets this explicitly and accepts that cost; one that does not is
    #: unaffected.
    supersede_retention_days: int = 0

    # --- retrieval (Steps 33.6-33.9) -------------------------------------
    #: How memory takes part in a search that does not say: ``auto`` (like any
    #: other source), ``off``, ``only`` or ``prefer``. A per-call ``memory``
    #: argument always wins over this.
    default_policy: str = "auto"
    #: Let ``alias``/``preference``/``exclusion`` records steer *ranking*, not
    #: just be retrievable (Step 33.8). Off by default: a memory that silently
    #: re-orders results is a surprise unless it was asked for.
    steering_enabled: bool = False
    #: Count which memories retrieval actually returns, so salience can reflect
    #: use (Step 33.9). Off by default — it is a write on the read path, and
    #: recording what a person looks up is a choice an operator should make.
    usage_tracking: bool = False
    #: Archive the least salient records once the store exceeds this many.
    #: ``None`` = unbounded, which is the pre-33.9 behavior. Runs as the
    #: **backstop** over whatever the per-scope/per-subject caps below leave
    #: behind (Phase 5) — those isolate their own pools first, this cap
    #: cleans up anything still over budget after that.
    max_records: int | None = None

    # --- per-scope budgets (Phase 5) ----------------------------------------
    #: Mirrors ``session_ttl_days``/``user_ttl_days``/``org_ttl_days`` above,
    #: but for count rather than age. ``max_records`` alone ranks the whole
    #: store as one pool, so with the default ``SCOPE_WEIGHT`` a session
    #: flood only ever *outranks* org facts by a fixed multiplier — it never
    #: fully isolates them. These three cap each scope's own pool
    #: independently, before the global backstop runs. ``None`` = that
    #: scope is unbounded (the pre-Phase-5 behavior).
    session_max_records: int | None = None
    user_max_records: int | None = None
    org_max_records: int | None = None
    #: Cap on live records sharing one ``subject`` (across scopes), grouped
    #: the same way graph bridging already groups by subject. Records with
    #: no ``subject`` are exempt — there is no single entity to cap them
    #: against. ``None`` = unbounded.
    max_records_per_subject: int | None = None

    #: Cap on `about` edges drawn per record by the graph bridge (Step 33.7).
    #: Total `about` edges stay bounded by this times the record count — the
    #: ceiling the retired concept layer never had.
    about_max_targets: int = 3

    # --- compaction (Phase 1) ---------------------------------------------
    #: L0 write-path admission: a write whose *normalized* text already
    #: matches a live record in the same (scope, subject, kind, ACL
    #: partition) bucket reinforces that record (bumps `observations`,
    #: records the surface form as a `variant`) instead of creating a new
    #: file — collapsing exact repeats and paraphrases alike, which today
    #: are indistinguishable "new" writes. **On by default**, unlike
    #: `usage_tracking` above: this is a write-path counter with none of
    #: that flag's read-path privacy argument (recording what an agent
    #: *wrote* is not recording what anyone *looked up*), and shipped off it
    #: would be exactly as inert as `uses` is while `usage_tracking` stays
    #: at its own default.
    reinforcement_enabled: bool = True

    # --- clustering (Phase 3) ----------------------------------------------
    #: L1/L2 near-duplicate clustering and medoid promotion, on the same
    #: consolidation pass as archival and capacity pruning. Off by default —
    #: unlike reinforcement, this changes what a *default* query sees
    #: (subsumed records drop to the cold tier and stop appearing in results
    #: a plain `current_only=True` query returns), so it is an explicit
    #: opt-in rather than an assumed-safe default, the same posture
    #: `supersede_retention_days` takes.
    compaction_enabled: bool = False
    #: Exact-Jaccard threshold (over normalized content tokens — see
    #: `pheasant.memory.normalize`) above which two records in the same
    #: (scope, subject, kind, ACL) bucket link into one cluster.
    compaction_similarity_threshold: float = 0.6
    #: A cluster below this size is left alone. `2` is the floor — anything
    #: less is not a cluster.
    compaction_min_cluster_size: int = 2

    # --- synthesis (Phase 4) -----------------------------------------------
    #: L3: abstractive merge for what L1/L2 cannot resolve deterministically.
    #: See `MemorySynthesisSettings` — off by default, never on an automatic
    #: beat.
    synthesis: MemorySynthesisSettings = field(default_factory=MemorySynthesisSettings)

    # --- formation (observation -> candidate -> record) --------------------
    #: Reading the observation plane and proposing records. Off by default;
    #: see `MemoryFormationSettings` and ``docs/memory-formation.md``.
    formation: MemoryFormationSettings = field(default_factory=MemoryFormationSettings)


@dataclass
class IdPSettings(ModelMixin):
    """External identity-provider group sync (Step 32.4).

    Disabled by default — the 32.2 config-mapped ``security.groups`` stays
    the deterministic core. When enabled, the region pulls a SCIM 2.0
    ``/Groups`` listing every ``sync_interval_minutes`` (scheduler beat, or
    ``POST /security/idp/sync``) into SQLite; the bearer token is read from
    the environment variable named by ``api_key_env`` and never stored.
    ``staleness_max_minutes`` is the SLA: a mapping older than this grants
    nothing (fail closed) until the next successful sync.
    """

    enabled: bool = False
    provider: str = "scim"
    base_url: str = ""
    api_key_env: str = "IDP_TOKEN"
    sync_interval_minutes: int = 60
    staleness_max_minutes: int = 1440


@dataclass
class ApiAuthSettings(ModelMixin):
    """First-party bearer auth for the HTTP/MCP surface (Phase 35.8).

    The HTTP API has no authentication of its own, and for one container on
    loopback that is the right trade: a local tool behind the operator's own
    perimeter. The fleet is a different deployment. There the API is a
    multi-replica Service that necessarily binds ``0.0.0.0``, and the only
    thing between it and the network is a port-publishing decision an
    operator can reasonably change — while the surface behind it can register
    a source over any allow-listed path and read what it finds.

    So this exists to make "behind an authenticating ingress" the *default*
    rather than the instruction. A static shared token is deliberately the
    whole feature: it is enough to close the gap, it needs no IdP, and it
    cannot rot into a half-built identity system. Anything richer belongs to
    the ingress, which is what ``behind_authenticating_proxy`` declares.

    Roles other than ``all`` refuse to serve a non-loopback bind with neither
    of the two set — see :func:`pheasant.deployment.roles.validate_role`.
    ``all`` is exempt because a single container is the local-first tool and
    must keep starting with no configuration at all.
    """

    #: Environment variable holding the shared bearer token. The secret itself
    #: never belongs in YAML, so — like every other credential here — only the
    #: variable *name* is written to config.
    token_env: str = "PHEASANT_API_TOKEN"
    #: "Something in front of this authenticates callers." Turns off the
    #: startup refusal without turning on a token. Opt-in and explicit,
    #: because the failure it suppresses is silent by construction.
    behind_authenticating_proxy: bool = False
    #: Paths answerable without a token. The probes must stay open or the
    #: orchestrator cannot tell a healthy pod from an unauthenticated one, and
    #: ``/metrics`` is scraped by a collector that holds no pheasant identity.
    #: ``/internal/*`` is deliberately absent: those routes enforce their own
    #: per-boundary tokens and are exempted structurally, not by this list.
    public_paths: list[str] = field(default_factory=lambda: ["/health", "/ready", "/metrics"])


@dataclass
class SecuritySettings(ModelMixin):
    allow_workspace_roots: list[Path] = field(
        default_factory=lambda: [Path("/workspace"), Path("/exports")]
    )
    # Step 32.2 — principal-aware retrieval. Off by default: a standalone /
    # single-user region behaves byte-identically to pre-32. When enforced,
    # un-ACL'd artifacts follow default_visibility ("public" keeps local
    # sources searchable; "private" requires an authenticated principal),
    # and `groups` maps principal ids to group identities (IdP sync = 32.4).
    acl_enforced: bool = False
    default_visibility: str = "public"
    groups: dict[str, list[str]] = field(default_factory=dict)
    idp: IdPSettings = field(default_factory=IdPSettings)
    api_auth: ApiAuthSettings = field(default_factory=ApiAuthSettings)
    allow_user_selected_source_paths: bool = True
    read_only_sources: bool = True
    deny_path_traversal: bool = True
    default_exclude_secrets: bool = True


@dataclass
class RepoSettings(ModelMixin):
    branch_policy: str = "current"
    include_uncommitted: bool = True
    commit_trigger: bool = True
    dependency_graph: dict[str, Any] = field(default_factory=dict)
    # Populated by URL quick-add. Local repository sources leave these unset.
    # Keeping the materialization recipe with the source is what lets every
    # later sync advance the managed checkout before it indexes it.
    clone_url: str | None = None
    clone_path: str | None = None
    clone_ref: str | None = None


@dataclass
class ChunkingSettings(ModelMixin):
    enabled: bool = True
    strategy: str = "semantic"
    max_chars: int = 4000
    overlap_chars: int = 400


@dataclass
class SourceSyncSettings(ModelMixin):
    on_startup: bool = True
    on_file_change: str | bool = "debounce"
    on_git_commit: bool = True
    interval_seconds: int | None = None


@dataclass
class SourceConnectorSettings(ModelMixin):
    allow_experimental: bool = False
    request_timeout_seconds: int = 10
    headers: dict[str, str] = field(default_factory=dict)
    # Name of the environment variable holding the connector's API token
    # (Step 31.2+ SaaS connectors). The secret itself never lands in config.
    api_key_env: str | None = None
    api_endpoint: str | None = None
    api_items_field: str = "items"
    api_content_field: str = "content"
    s3_bucket: str | None = None
    s3_prefix: str = ""
    # Synapse Step 34.1+: "native" (default, in-process trusted Python) or
    # "sandboxed" (fuel/memory-capped WASM guest via pheasant.sandbox).
    # Opt-in per source; unset is byte-identical to pre-34.1.
    runtime: str = "native"
    # Hostnames a sandboxed connector's guest may reach via host_fetch.
    # Empty (default) denies every fetch.
    allowed_hosts: list[str] = field(default_factory=list)
    # Path to a guest module (.wat text or compiled .wasm) for a sandboxed
    # connector. None uses the connector class's bundled reference guest.
    wasm_module_path: str | None = None


@dataclass
class TaxonomySettings(ModelMixin):
    """Structural taxonomy extraction for one source (books, procedures, legal).

    Highly structured documents carry their own outline — Part / Chapter /
    Article / Section / § / 1.2.3 / (a). With this on, that outline is
    detected per artifact and used three ways: each chunk is labelled with the
    section it falls inside (``chunks.heading_path``, which ``chunks_fts``
    already weights at 2.0 — double the body text), `heading` graph nodes and
    `has_heading` edges are emitted so the taxonomy is traversable, and
    ``GET /taxonomy`` can render the tree.

    Detection is rule-based, deterministic and offline — no model, no network.

    ``enabled`` defaults to **false**, and is a *per-source* switch rather than
    a global one, because the numbering rules are genuinely ambiguous on
    ordinary prose: ``1. Introduction`` in a standards document is a section,
    ``1. Buy milk`` in a note is a list item, and nothing in the line tells
    them apart. Turning it on per source is how the operator says "this
    source really is structured". Enabling it also changes what the FTS index
    holds for that source, so it wants a deliberate re-sync.

    ``detect`` narrows the rule set when a corpus only uses some conventions;
    an empty list means all rules. Valid names: ``markdown``, ``keyword``
    (Chapter/Article/Section/...), ``code`` (``§``), ``numbered``
    (``1.2.3``), ``lettered`` (``(a)``/``(iv)``), ``caps`` (ALL-CAPS lines —
    the noisiest, and the first one to drop if a corpus shouts).
    """

    enabled: bool = False
    max_depth: int = 6
    detect: list[str] = field(default_factory=list)
    #: Emit `heading` graph nodes + `has_heading` edges. Chunk labelling is
    #: independent of this: a source can populate `heading_path` for search
    #: without growing the graph.
    graph_nodes: bool = True
    #: Cut chunks at section boundaries so one chunk is one section (still
    #: subdivided when a section exceeds ``chunking.max_chars``).
    #:
    #: On by default *within* an enabled taxonomy, because it is what makes
    #: the feature useful rather than decorative. Without it a whole contract
    #: that fits in one 4000-char chunk gets a single ``heading_path`` — its
    #: first heading — and a chunk spanning several sections is labelled with
    #: only the section its first line falls in, which is actively
    #: misleading. With it, "what does § 12.3 say" retrieves § 12.3.
    #:
    #: It does change chunk boundaries for the source, but enabling taxonomy
    #: is already a deliberate re-index; set it false to keep the existing
    #: boundaries and accept coarser labels.
    split_on_sections: bool = True


@dataclass
class SourceConfig(ModelMixin):
    name: str
    type: SourceType | PluginSourceType
    path: Path
    description: str | None = None
    enabled: bool = True
    max_depth: int | None = None
    include: list[str] = field(
        default_factory=lambda: [
            "**/*.py",
            "**/*.md",
            "**/*.txt",
            "**/*.yaml",
            "**/*.yml",
            "**/*.toml",
            "**/*.json",
        ]
    )
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    repo: RepoSettings = field(default_factory=RepoSettings)
    chunking: ChunkingSettings = field(default_factory=ChunkingSettings)
    sync: SourceSyncSettings = field(default_factory=SourceSyncSettings)
    connector: SourceConnectorSettings = field(default_factory=SourceConnectorSettings)
    taxonomy: TaxonomySettings = field(default_factory=TaxonomySettings)
    urls: list[str] = field(default_factory=list)
    #: Per-source override of ``sync.limits``. ``None`` inherits the global
    #: block; set it to widen or tighten one source without touching others.
    limits: SyncLimitsSettings | None = None


@dataclass
class LogQueueSettings(ModelMixin):
    """The log tier's own durable queue — deliberately not the index queue.

    Request-rate churn in ``index_tasks`` would mean vacuum pressure on
    PostgreSQL and constant churn on the index claim path, which is exactly
    the burden this tier exists to avoid. The cost of separating is small:
    ``drain()`` is already task-agnostic and is reused verbatim, and the
    race-free conditional-``UPDATE`` claim stays one implementation
    parameterized by table.
    """

    #: Off, batches are written by whoever produced them, if `/state` is
    #: writable. On, they are published and a ``--role logger`` drains them.
    enabled: bool = False
    backend: str = "local"  # local | nats
    visibility_seconds: int = 120
    #: Lower than the index queue's 3. A log batch is best-effort by
    #: construction, and retrying a poisoned one three times costs more than
    #: the data is worth.
    max_attempts: int = 2
    nats_stream: str = "PHEASANT_LOGS"
    nats_subject: str = "pheasant.logs.batches"
    nats_durable: str = "pheasant-loggers"


@dataclass
class InteractionSettings(ModelMixin):
    """The observation plane: what was asked, on which surface, by whom, and
    what came back.

    **Off by default, because it records queries and principals.** An
    operator turning this on is choosing to keep that; `redact_text` exists
    for regions that want the shape of the traffic without its content.

    Observations are rows, never files. They are not chunked, not indexed,
    and never returned by ``search_context`` — a UI session's chat does not
    become knowledge because it was observed. See ``docs/memory-formation.md``.
    """

    enabled: bool = False
    #: Fraction of observed searches that carry a per-stage digest.
    #:
    #: The digest names what each retrieval stage did — arm candidate counts,
    #: what each filter removed, the fused depth, and the bundle the search
    #: ranked under. It is what lets a stage regression be traced to the
    #: configuration change that caused it, and it gives the tuning plane a
    #: *live* diagnosis source rather than one that is only as fresh as the
    #: last batch.
    #:
    #: Sampled rather than universal because the digest is a few hundred bytes
    #: on a row that is already a couple of kilobytes, and a ledger sized for
    #: search traffic should not be resized by a diagnostic. Sampling is
    #: deterministic on the trace id, so every hop of one call agrees about
    #: whether it is sampled — a per-hop random draw produces traces that
    #: cannot be joined. 0.0 disables it; the always-on Prometheus stage
    #: counters are unaffected either way.
    stage_sample_rate: float = 0.0
    #: Record no free text at all -- neither the question nor the answer.
    #:
    #: Named for what it does rather than for one of the two fields it
    #: covers: redacting a question while keeping the answer that quotes the
    #: corpus back at it would be incoherent, so this is deliberately not
    #: `redact_query_text`. Identity, modality, criteria, result ids and
    #: paths are still recorded, so `path-affinity-v1` and
    #: `retrieval-gap-v1` still work; only the lexical rule
    #: (`alias-cooccurrence-v1`) goes quiet.
    redact_text: bool = False
    #: Cap on a recorded answer, in characters. **`0` records no answers at
    #: all** -- the same "0 means off" shape `hot_retention_days` and
    #: `supersede_retention_days` already use.
    #:
    #: A cap rather than an unbounded field because an answer is model output
    #: and runs 10-50x a question's bytes; left unbounded, chat traffic would
    #: dominate the ledger's size on a corpus that barely changed. Truncation
    #: is honest: a truncated answer is marked in `attributes`.
    max_answer_chars: int = 4000

    # --- the request path ---------------------------------------------------
    #: Events held in memory before a flush. **This is a backpressure knob,
    #: not a throughput one**: the buffer is bounded and overflow drops the
    #: oldest event rather than blocking a request. A log tier falling
    #: behind must degrade to data loss, never to request latency — the same
    #: posture `bound_concurrency` takes when it answers 429 under
    #: saturation.
    buffer_size: int = 10_000
    flush_interval_seconds: float = 5.0
    #: Events per published batch. Batching is what keeps the ledger off the
    #: request path: one publish per N events rather than one write per
    #: request, mirroring the batched fan-out `sync.worker_pool` already
    #: does for file preparation.
    flush_batch_size: int = 500
    #: Stop publishing (and start dropping, counted separately) when the log
    #: queue is this deep. Without it a stalled log tier turns into unbounded
    #: queue growth, which is the same failure wearing a different hat.
    max_queue_depth: int = 50_000

    # --- storage tiers ------------------------------------------------------
    #: How long events stay queryable in `/state`.
    #:
    #: ``0`` is **cold-only mode**: batches go straight to Parquet and
    #: `/state` never grows at all. Formation then reads cold on its own
    #: pass — slower, batch-only, which is fine because formation is a beat,
    #: not a request.
    hot_retention_days: int = 7
    #: Roll hot rows past their retention into Parquet under
    #: ``<exports_path>/interactions/dt=YYYY-MM-DD/`` before deleting them.
    #: Off, they are simply deleted.
    #:
    #: This does not make DuckDB a storage backend (CLAUDE.md rule 12): the
    #: destination is `/exports`, the writer is `analytics.py`'s, the pass
    #: runs on the log tier rather than the sync path, and nothing
    #: operational lives there.
    cold_enabled: bool = False
    #: ``None`` keeps cold partitions forever. Set it and whole ``dt=``
    #: directories are dropped once past it — never individual rows.
    cold_retention_days: int | None = None
    #: Upper bound on rows one roll pass moves. Load-bearing in a single
    #: container, where the roll runs on the scheduler beat *under
    #: ``sync_lock``*: an unbounded roll there stalls incremental sync for
    #: every source. Same argument as `MEMORY_TARGETED_ARCHIVE_MAX`.
    max_rows_per_pass: int = 50_000
    #: Where an API replica spools batches when `/state` is read-only and no
    #: queue is configured — the degraded path for a custom SQLite
    #: multi-process deployment. ``None`` disables it, and such a replica
    #: then drops rather than spools. The shipped fleet needs none of this:
    #: it runs PostgreSQL, so every replica can write directly.
    spool_path: Path | None = None

    queue: LogQueueSettings = field(default_factory=LogQueueSettings)


@dataclass
class ObservabilitySettings(ModelMixin):
    """Tracing and the observation plane.

    Two independent things share this block because they share a source:
    one span per API/MCP call feeds both the operator's collector (if they
    run one) and the region's own interaction ledger (if they enable it).
    Neither requires the other, and both are off by default.

    ``pytest`` stays network-free by construction rather than by mocking:
    the OTLP exporter is attached only when `otlp_endpoint` is set, and the
    default is ``None``.
    """

    #: OTLP collector endpoint. ``None`` attaches no exporter at all — spans
    #: are still created and still feed the interaction ledger, they just go
    #: nowhere off-box.
    otlp_endpoint: str | None = None
    otlp_protocol: str = "http/protobuf"
    #: Environment variable holding ``key=value,key=value`` exporter headers.
    #: The **name**, never the value — same rule as `storage.dsn_env`.
    otlp_headers_env: str = "PHEASANT_OTLP_HEADERS"
    service_name: str = "pheasant"
    #: Head sampling ratio for exported spans. Does not affect the
    #: interaction ledger, which has its own bounded buffer: sampling out a
    #: span the operator's collector does not need should not also cost the
    #: region a data point it counts on.
    sample_ratio: float = 1.0

    interactions: InteractionSettings = field(default_factory=InteractionSettings)


@dataclass
class EvaluationProofSettings(ModelMixin):
    """How observed events become weighted evidence.

    Every default here is a refusal to over-claim, and each one has a failure
    mode attached:

    ``unknown_is_negative`` off -- an artifact served and neither selected nor
    rejected stays *unjudged*. Turned on, every metric below silently changes
    meaning: precision improves whenever the region returns fewer results,
    because the unshown items stop counting as failures.

    ``non_selection_is_negative`` off -- the reader may have found the answer
    at rank one and stopped. This is the single most tempting inference in the
    whole system and the one with no evidence behind it.

    ``temporal_decay_enabled`` off -- an operator who has not chosen a
    half-life should not discover that a year-old conclusive test result
    quietly stopped counting.

    The event weights are the specification's defaults. Renaming an event type
    orphans every proof row referencing it, so the *names* are stable API even
    though the numbers are configuration.
    """

    event_weights: dict[str, float] = field(
        default_factory=lambda: {
            "considered": 0.0,
            "served": 0.0,
            "included_in_context": 0.0,
            "cited": 0.25,
            "selected": 0.5,
            "explicit_accept": 1.0,
            "downstream_success": 1.0,
            "deterministic_validation_pass": 1.0,
            "explicit_reject": -1.0,
            "downstream_failure": -1.0,
            "deterministic_validation_fail": -1.0,
            "explicit_correction": -1.0,
            "superseded": -1.0,
            "immediate_reformulation": 0.0,
            "not_selected": 0.0,
        }
    )
    strength_multipliers: dict[str, float] = field(
        default_factory=lambda: {
            "weak": 0.25,
            "moderate": 0.5,
            "strong": 1.0,
            "conclusive": 1.0,
        }
    )
    unknown_is_negative: bool = False
    non_selection_is_negative: bool = False
    temporal_decay_enabled: bool = False
    temporal_half_life_days: float = 180.0
    #: Net weight below which a target is neither a known positive nor a known
    #: negative. Not zero: one weak citation is not a "known positive", and a
    #: metric named `known_positive_recall` that counted one would over-claim
    #: in its own name.
    positive_floor: float = 0.2
    minimum_eligible_queries: int = 10
    minimum_evidenced_queries: int = 5
    minimum_independent_interactions: int = 5
    maximum_single_query_proof_share: float = 0.5


@dataclass
class EvaluationCohortSettings(ModelMixin):
    """Which query cohorts a run materializes, and how large each may be.

    ``anchor_minimum_queries`` is a floor on *freezing*, not on running: an
    anchor of four questions is not a baseline, and freezing one means being
    stuck with it at every future snapshot.

    ``holdout_minimum_separation_days`` defaults to 0 because "how long must a
    temporal holdout remain independent" is an open policy decision the
    specification flags explicitly. A hidden non-zero default would be this
    package answering it on an operator's behalf.
    """

    anchor: bool = True
    anchor_minimum_queries: int = 20
    rolling: bool = True
    rolling_lookback_days: int = 30
    learned: bool = True
    temporal_holdout: bool = True
    holdout_minimum_separation_days: float = 0.0
    holdout_minimum_queries: int = 5
    control: bool = True
    control_minimum_queries: int = 10
    synthetic_invariants: bool = True
    maximum_queries_per_cohort: int = 200


@dataclass
class EvaluationVariantSettings(ModelMixin):
    """Which ablations run. ``B0`` (corpus baseline) is not optional.

    Every attribution number in the report is a paired difference against the
    corpus baseline. Without it the treatment numbers have nothing to subtract,
    and an absolute retrieval score published on its own gets read as accuracy.
    """

    memory_content: bool = True
    alias_only: bool = True
    preference_only: bool = True
    exclusion_only: bool = True
    full_memory: bool = True
    candidate_shadow: bool = True
    leave_one_memory_out: bool = False
    leave_one_cluster_out: bool = False


@dataclass
class EvaluationGateSettings(ModelMixin):
    """Hard invariants, checked before any score is aggregated.

    Zero tolerance on the first four is the point of having them: an ACL leak
    is not offset by good recall, and any arithmetic that lets it be is the
    arithmetic these exist to sit outside of.
    """

    acl_leak_maximum: int = 0
    stale_current_leak_maximum: int = 0
    temporal_invariant_failures_maximum: int = 0
    known_positive_exclusion_maximum: int = 0
    abstention_failures_maximum: int = 0
    control_regression_tolerance: float = 0.0
    negative_exposure_increase_tolerance: float = 0.0
    incomplete_snapshot_blocks_run: bool = True


@dataclass
class EvaluationPromotionSettings(ModelMixin):
    """When a validated candidate may reach production retrieval.

    Off by default, the same posture ``memory.formation.auto_admit`` takes and
    for the same reason: promotion changes what a *default* query returns.

    ``allow_originating_query_only_promotion`` off is the anti-self-reward
    rule. A candidate that improves only the query that created it has
    demonstrated recall of its own evidence and nothing else, and promoting on
    that basis is the feedback loop this whole plane is built to keep closed.
    """

    enabled: bool = False
    require_shadow_replay: bool = True
    require_all_hard_gates: bool = True
    minimum_independent_queries: int = 3
    minimum_temporal_holdout_queries: int = 1
    minimum_target_metric_gain: float = 0.0
    maximum_control_regression: float = 0.0
    maximum_negative_exposure_increase: float = 0.0
    allow_originating_query_only_promotion: bool = False
    #: What happens to a candidate the evidence cannot yet decide:
    #: ``retain_candidate`` (default) or ``reject``. Retention is right --
    #: a rejection is permanent by design, and rejecting for *absence* of
    #: evidence would make a review queue that forgets things nobody has
    #: had a chance to demonstrate yet.
    insufficient_evidence_action: str = "retain_candidate"


@dataclass
class EvaluationSettings(ModelMixin):
    """The evaluation plane: measuring whether this region is getting better.

    Off by default and read-only when on. A run replays cohorts through the
    real search path, computes evidence-bearing metrics, and writes rows to the
    ``evaluation_*`` tables -- which are never indexed, never chunked and never
    returned by a search, because a region must not retrieve its own
    measurements as knowledge.

    **Fleet-safe by construction.** A run takes the evaluation lease
    (``pheasant.evaluation.runner.EVALUATION_LEASE``), so several API replicas
    pointed at one ``/state`` produce one run rather than N. It never takes
    ``sync_lock``: the scheduler holds that across all its work, and a
    thousand-query replay inside it would stall incremental sync for every
    source in the region -- exactly the mistake the observation plane's
    hot-to-cold roll was moved outside the lock to avoid.

    See ``docs/knowledge-effectiveness.md``.
    """

    enabled: bool = False
    #: Fire a run on the scheduler beat when the snapshot manifest shows a
    #: material change. Off by default: a run costs one search per query per
    #: variant, which is real work to start doing on a timer without asking.
    on_material_snapshot: bool = False
    #: …and never more often than this, whatever the trigger says. A corpus
    #: under active indexing changes materially every beat.
    minimum_interval_seconds: int = 3600
    on_release_boundary: bool = True
    #: Historical reconstruction ("what could the region have known at t")
    #: alongside current-state replay ("how does it handle that question now").
    #: Both are supported; a report always says which it ran.
    historical_reconstruction: bool = True
    #: Results requested per query per variant. The k values the metrics use
    #: are capped by this, so raising the metric k without raising this
    #: measures a truncated list.
    max_results: int = 10
    #: Retrieval mode the replay uses. ``hybrid`` is what production serves.
    mode: str = "hybrid"
    #: Ceiling on one run: queries × variants searches. A run that outgrows
    #: this is truncated and says so, rather than becoming the region's
    #: dominant workload.
    maximum_queries_per_run: int = 500
    maximum_runtime_seconds: int = 900
    #: Per-query metric rows kept in the report. The audit trail an aggregate
    #: resolves to, bounded so a large rolling cohort cannot produce a
    #: document nothing can open.
    maximum_stored_per_query_results: int = 200
    #: How long a running batch may go without stamping its heartbeat before
    #: another process may declare it dead and mark it ``interrupted``.
    #:
    #: A knob rather than a constant because the right value depends on how
    #: long a single (cohort, variant) replay takes here, and that is a
    #: property of the corpus. Too low and a slow-but-healthy batch is
    #: reclaimed out from under itself; too high and a stopped container shows
    #: a spinner for minutes longer than it needs to. The default is six
    #: heartbeats -- a run that missed that many has almost certainly died
    #: with its process.
    run_stale_seconds: float = 90.0
    #: Optional packs, each independently enabled. Every one of them is
    #: labelled diagnostic or optional in the report; none may enter a
    #: factual-accuracy claim.
    retrieval_diagnostics: bool = False
    binary_preference: bool = False
    #: Weighted geometric mean over normalized components. Empty (the default)
    #: means no composite is published at all, which is the specification's
    #: "no default universal accuracy score".
    composite_weights: dict[str, float] = field(default_factory=dict)
    proof: EvaluationProofSettings = field(default_factory=EvaluationProofSettings)
    cohorts: EvaluationCohortSettings = field(default_factory=EvaluationCohortSettings)
    variants: EvaluationVariantSettings = field(default_factory=EvaluationVariantSettings)
    gates: EvaluationGateSettings = field(default_factory=EvaluationGateSettings)
    promotion: EvaluationPromotionSettings = field(default_factory=EvaluationPromotionSettings)


@dataclass
class TuningTrackingSettings(ModelMixin):
    """Where experiment tracking is mirrored. ``/state`` is always the truth.

    ``backend`` is ``off`` (default), ``state`` (an explicit spelling of the
    same thing) or ``mlflow``. The MLflow sink needs the ``[tuning]`` extra and
    defaults to a **local file store** under ``<exports>/tuning/mlruns`` -- no
    server, no network, no credentials -- so a region can get run comparison
    and a parameter-versus-metric plot without operating anything. Point
    ``tracking_uri`` at a real server if you have one.

    A tracking backend that is missing, down or misconfigured never fails a
    batch: the rows are in ``/state`` either way and the mirror is a
    projection of them.
    """

    backend: str = "off"
    tracking_uri: str = ""
    experiment_name: str = "pheasant-retrieval-tuning"


@dataclass
class TuningAutoSettings(ModelMixin):
    """When a batch starts by itself, and whether it may change ranking.

    ``enabled`` and ``apply`` are separate switches on purpose, and the gap
    between them is the safety property. Running a batch is read-only: it
    produces a diagnosis, some trials and at most a *proposed* bundle.
    Applying one changes what every replica in the fleet serves. A region that
    wants continuous measurement without unattended re-ranking sets the first
    and not the second, which is the default.

    ``on_material_snapshot`` fires only where the scheduler runs, so API
    replicas never start batches -- the same rule the evaluation plane's
    auto-trigger follows.
    """

    enabled: bool = False
    apply: bool = False
    on_material_snapshot: bool = False
    minimum_interval_seconds: int = 86400


@dataclass
class TuningObjectiveSettings(ModelMixin):
    """What "better" means for this region. See ``pheasant.tuning.objective``.

    ``metric`` names a built-in objective: ``reciprocal_rank`` (default),
    ``recall_at_5``, ``recall_at_10``, ``hit_rate`` or ``balanced``. Each one
    is a different product decision, and each publishes what it *trades away*
    as well as what it optimizes — an objective without a stated trade is a
    preference presented as an optimum.

    ``weights`` overrides the name entirely with a custom combination over
    collected metrics, normalized to sum to one. A caller who wrote weights has
    been more specific than one who picked a label, so weights win.

    Getting this wrong is not a small thing. A region whose agents read one
    result wants ``reciprocal_rank``; one whose agents read a page and
    synthesize wants ``recall_at_10``, and would be actively harmed by a
    parameter set that sharpens rank one at the cost of dropping a document
    out of the list. Both are legitimate and they are not the same objective.
    """

    metric: str = "reciprocal_rank"
    weights: dict[str, float] = field(default_factory=dict)
    higher_is_better: bool = True


@dataclass
class TuningSettings(ModelMixin):
    """The tuning plane: finding which retrieval stage is failing, and fixing it.

    Off by default, and read-only when on unless ``auto.apply`` is set or
    somebody applies a bundle. A batch replays a cohort through the real search
    path with stage capture, attributes every miss to the step that lost it,
    proposes parameters only for the stages actually to blame, and gates any
    winner against a held-out cohort before it may change the region.

    **Fleet-scoped.** What a batch produces is a bundle of region-wide
    retrieval parameters. There is no per-request and no per-principal tuning:
    parameters that varied by caller would make two agents disagree about what
    the region contains, and would make every number the evaluation plane
    publishes a measurement of whoever happened to ask.

    **It yields.** The executor holds one slot, takes the ``__tuning__`` lease
    rather than ``sync_lock``, and stands down while the index queue has work
    in it -- a batch is a measurement and indexing is somebody waiting.

    See ``docs/retrieval-tuning.md``.
    """

    enabled: bool = False
    #: Results per query during a trial. The k values the metrics use are
    #: capped by this, so raising a metric k without raising this measures a
    #: truncated list.
    max_results: int = 10
    mode: str = "hybrid"
    #: Points evaluated by re-fusion — the cheap family, which needs no
    #: retrieval at all and can therefore be generous.
    refusion_trials: int = 400
    #: Points needing a real search per query. This is the number that decides
    #: how long a batch runs.
    requery_trials: int = 24
    #: Backstop on total searches, whatever the trial counts imply.
    max_searches: int = 5000
    #: Paired queries required before a delta may decide anything.
    minimum_paired_queries: int = 20
    #: Parameters an operator has settled and does not want re-litigated. A
    #: pinned name is never proposed.
    pinned_parameters: list[str] = field(default_factory=list)
    #: Index-queue depth above which a running batch stands down.
    max_index_queue_depth: int = 1
    #: Stand down while any source sync holds its lease.
    yield_to_sync: bool = True
    #: How long a batch may go without a heartbeat before another process may
    #: declare it dead and mark it ``interrupted``.
    stale_seconds: float = 90.0
    objective: TuningObjectiveSettings = field(default_factory=TuningObjectiveSettings)
    tracking: TuningTrackingSettings = field(default_factory=TuningTrackingSettings)
    auto: TuningAutoSettings = field(default_factory=TuningAutoSettings)


@dataclass
class ReadinessSettings(ModelMixin):
    """Whether this region can be *measured by somebody else*, and its limits.

    A third plane, and the one the other two do not cover. The evaluation
    plane asks how well retrieval is doing; the tuning plane asks which stage
    is failing; this one asks whether an outside harness can trust either
    answer — whether every submitted write reconciles, whether a result names
    the exact place it came from, whether an isolation boundary holds, and
    whether the region will say so in a form a machine can read.

    Off by default and read-only when on, like both of its siblings. Turning
    it on adds a contract endpoint and a checkable gate; it does not change
    what a search returns, what an index holds, or what a sync does.

    The thresholds below exist because a performance gate cannot be evaluated
    against an unstated number. The specification this implements makes that
    the one *missing decision* rather than a missing capability: the defaults
    here are deliberately generous, because a threshold that is present and
    loose can be tightened from evidence, and one that is absent turns every
    latency observation into an argument.

    See ``docs/stress-test-readiness.md``.
    """

    enabled: bool = False
    #: Paths and filenames that may never enter the searchable corpus.
    #: fnmatch patterns, tested against both the item's relative path and its
    #: bare filename. Empty by default: a region with no benchmark to protect
    #: pays one truth test per submitted item and nothing else.
    #:
    #: This is enforcement, not a report. A check that benchmark artifacts are
    #: absent can only run after they have already been indexed, and by then
    #: they have been retrievable — so the write is refused instead.
    corpus_denylist: list[str] = field(default_factory=list)
    #: Search latency a readiness run will accept, end to end, at p95.
    #: Generous on purpose (see above); the gate reports the observed value
    #: beside it either way, so tightening it is a config edit rather than a
    #: code change.
    max_search_latency_ms: float = 5000.0
    #: How long after a submission a receipt may take to reach ``accepted``.
    max_ingest_ack_ms: float = 30000.0
    #: How long after acceptance an item may take to become searchable. The
    #: index barrier's budget, and the number that decides whether a harness
    #: that queries immediately is early or the region is late.
    max_index_lag_ms: float = 120000.0
    #: Searches a latency probe issues. Below this a p95 is a statement about
    #: one or two queries, so the gate publishes no rate rather than a
    #: confident one — the floor `tuning.health` already keeps.
    latency_probe_queries: int = 12
    #: Concurrent writers the swarm probe drives. The default is small enough
    #: to run in a laptop container and large enough to interleave.
    concurrency_probe_writers: int = 4
    #: Items each concurrent writer submits.
    concurrency_probe_items: int = 5


@dataclass
class PheasantConfig(ModelMixin):
    pheasant: PheasantSettings = field(default_factory=PheasantSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    search: SearchSettings = field(default_factory=SearchSettings)
    ingestion: IngestionSettings = field(default_factory=IngestionSettings)
    sync: SyncSettings = field(default_factory=SyncSettings)
    graph: GraphSettings = field(default_factory=GraphSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    synapse: SynapseSettings = field(default_factory=SynapseSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    observability: ObservabilitySettings = field(default_factory=ObservabilitySettings)
    assistant: AssistantSettings = field(default_factory=AssistantSettings)
    evaluation: EvaluationSettings = field(default_factory=EvaluationSettings)
    tuning: TuningSettings = field(default_factory=TuningSettings)
    readiness: ReadinessSettings = field(default_factory=ReadinessSettings)
    sources: list[SourceConfig] = field(default_factory=list)

    @classmethod
    def model_validate(cls, data: dict[str, Any]) -> PheasantConfig:
        """Build the config tree from plain data.

        Nesting is derived from the dataclasses rather than wired by hand.
        This used to be ~90 lines of `if dc is ServerSettings: if "mcp" in raw:
        raw["mcp"] = build(McpSettings, ...)`, one branch per nested section —
        a fourth edit that rule 11's freshness test did not cover, so a section
        added without it loaded silently as defaults. Every section in this
        file was one forgotten line away from being ignored, and two were added
        during this work with exactly that hazard.

        What stays hand-written is what is genuinely not derivable: the source
        list, whose `path` is anchored to `workspace_root` and whose `type` may
        be a plugin string outside the enum, and the three state paths defaulted
        from `pheasant.state_path` at the end.
        """

        cfg = cls(
            **{
                name: _build(annotation, data.get(name))
                for name, annotation in _field_types(cls).items()
                if name != "sources"
            }
        )
        cfg.sources = []
        for raw in data.get("sources", []) or []:
            raw = dict(raw)
            try:
                raw["type"] = SourceType(raw.get("type", "single_file"))
            except ValueError:
                raw["type"] = PluginSourceType(str(raw.get("type")))
            src_path = Path(raw["path"])
            # Anchor a relative filesystem source path to workspace_root so a
            # config written as `path: docs` means "<workspace_root>/docs", not
            # "<cwd>/docs" — the container CWD is /app, which would silently
            # resolve off the mounted workspace and index nothing.
            if not src_path.is_absolute() and raw["type"] in FILESYSTEM_SOURCE_TYPES:
                src_path = cfg.pheasant.workspace_root / src_path
            raw["path"] = src_path
            hints = _field_types(SourceConfig)
            for key, value in list(raw.items()):
                if key in {"type", "path"} or key not in hints:
                    continue
                nested = _model_type(hints[key])
                if nested is not None and isinstance(value, dict):
                    raw[key] = _build(hints[key], value)
            _coerce_scalar_fields(SourceConfig, raw)
            cfg.sources.append(
                SourceConfig(
                    **{k: v for k, v in raw.items() if k in SourceConfig.__dataclass_fields__}
                )
            )
        state = cfg.pheasant.state_path
        cfg.storage.sqlite_path = cfg.storage.sqlite_path or state / "pheasant.db"
        cfg.storage.graph_path = cfg.storage.graph_path or state / "graphs"
        cfg.storage.manifest_path = cfg.storage.manifest_path or state / "manifests"
        return cfg

    def effective_source(
        self,
        source: SourceConfig,
        *,
        max_depth: int | None = None,
        full_scan: bool = False,
    ) -> SourceConfig:
        """The source as the sync path should actually see it.

        Two deployment-wide policies are folded in here rather than at every
        call site, because the connector API takes only ``(source, state)``
        and third-party plugins must keep working unchanged:

        * ``security.default_exclude_secrets`` unions :data:`SECRET_EXCLUDES`
          into the exclude list. This has to happen *after* any caller-
          supplied ``exclude``, because supplying one replaces the field
          wholesale — which used to drop every credential pattern silently.
        * ``sync.limits`` fills in a per-source budget when the source does
          not carry its own.

        ``max_depth`` overrides the source's own depth for this call, and
        ``full_scan`` means "I know what I am asking for": no depth cap and
        no budget. Both are per-invocation, never persisted.
        """
        import copy

        resolved = copy.deepcopy(source)
        if self.security.default_exclude_secrets:
            existing = list(resolved.exclude or [])
            for pattern in SECRET_EXCLUDES:
                if pattern not in existing:
                    existing.append(pattern)
            resolved.exclude = existing
        if full_scan:
            inherited = resolved.limits or self.sync.limits
            resolved.max_depth = None
            resolved.limits = SyncLimitsSettings(
                max_files=None,
                max_file_size_mb=None,
                max_total_mb=None,
                follow_symlinks=bool(getattr(inherited, "follow_symlinks", False)),
            )
            return resolved
        if max_depth is not None:
            resolved.max_depth = max_depth
        if resolved.limits is None:
            resolved.limits = self.sync.limits
        return resolved

    @property
    def knowledge_base_id(self) -> str:
        return self.pheasant.name

    @property
    def state_path(self) -> Path:
        return self.pheasant.state_path
