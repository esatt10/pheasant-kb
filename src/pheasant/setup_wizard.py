"""``pheasant setup`` — the interactive, sectioned configuration wizard.

This replaces the prose "config wizard" that used to live in
``agent/config_wizard_prompt.md`` and be executed by a coding agent. That
approach had three problems this module exists to not have:

* **Determinism.** The same answers here always produce the same
  ``pheasant.yaml``. Everything else in the indexing path is deterministic on
  purpose (CLAUDE.md §4 rule 1); configuration had no business being the
  exception.
* **Reachability.** A user with a terminal and Docker can run this. They could
  not run a procedure that required a coding agent to interpret it.
* **Freshness.** Every default here is read off the live dataclasses in
  :mod:`pheasant.config.schema` via :func:`schema_default` — never copied. A
  field whose default changes changes here too, with no second edit and
  nothing to rot. :mod:`tests.test_config_surface_freshness` holds the
  remaining mechanical line: every config section must be *covered* by a
  section below.

Structure: a flat list of :class:`Section`, each with a blurb that explains
what the section is for before anything is asked, and a list of
:class:`Question` whose ``default`` is always a working answer — pressing
Enter through the entire wizard is a supported, sensible path.

Secrets never enter the YAML. A ``kind="secret"`` question stores the *name*
of an environment variable in the config and the *value* in a ``.env`` file
written with mode 0600, which is also checked against ``.gitignore``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pheasant.assistant.catalog import PROVIDERS, resolve_auto_provider
from pheasant.config.schema import PheasantConfig
from pheasant.mcp_client.vscode import DEFAULT_IMAGE

#: Where an interrupted run leaves its answers, so a second invocation can
#: pick up where it stopped rather than starting the interview again.
PROGRESS_FILENAME = ".pheasant-setup.json"

QuestionKind = str  # text | bool | int | float | choice | model | list | secret | path
QuestionValidator = Callable[[Any], Any]


def schema_default(dotted: str) -> Any:
    """The live default for a dotted config path, read off the dataclasses.

    ``schema_default("search.embeddings.model")`` instantiates a default
    :class:`~pheasant.config.schema.PheasantConfig` and walks to the field, so
    a default that changes in the schema changes in the wizard with no second
    edit. A typo raises immediately (at import time, via the module-level
    section list) rather than silently offering ``None``.
    """
    cursor: Any = PheasantConfig()
    for part in dotted.split("."):
        cursor = getattr(cursor, part)
    if isinstance(cursor, Path):
        return str(cursor)
    return cursor


@dataclass
class Choice:
    value: str
    label: str
    detail: str = ""


@dataclass
class Question:
    """One prompt. ``key`` is the dotted config path the answer is written to."""

    key: str
    prompt: str
    help: str = ""
    kind: QuestionKind = "text"
    default: Any = None
    choices: Sequence[Choice] = ()
    #: Only asked when this returns True for the answers gathered so far.
    when: Callable[[dict[str, Any]], bool] | None = None
    #: Asked only with ``--advanced``; otherwise the default is taken silently.
    advanced: bool = False
    #: ``kind="secret"`` only: dotted path of the field naming the env var.
    secret_env_key: str | None = None
    #: Optional semantic validation applied both while prompting and when
    #: answers supplied by ``--answers`` are rendered.
    validate: QuestionValidator | None = None

    def resolved_default(self) -> Any:
        if self.default is not None or self.kind == "secret":
            return self.default
        return schema_default(self.key)


@dataclass
class Section:
    id: str
    title: str
    blurb: str
    #: Top-level ``PheasantConfig`` fields this section configures. The
    #: freshness test asserts every one of them is claimed by some section.
    covers: tuple[str, ...]
    questions: list[Question] = field(default_factory=list)


@dataclass(frozen=True)
class Phase:
    """A user-facing chapter made up of the existing schema sections."""

    id: str
    title: str
    blurb: str
    section_ids: tuple[str, ...]


# --------------------------------------------------------------------------
# The interview
# --------------------------------------------------------------------------


def build_sections() -> list[Section]:
    """Every section, in the order they are asked.

    Deliberately a function rather than a module constant: it reads live
    schema defaults, and building it at import time would freeze them against
    a config object constructed before any test monkeypatching.
    """
    return [
        Section(
            id="identity",
            title="Knowledge base",
            blurb=(
                "What this knowledge base is called. The name is its ID everywhere "
                "else — in the graph root node, in MCP tool calls, and (if you ever "
                "join a Synapse fleet) in the contract it publishes. Changing it "
                "later means re-indexing, so pick something stable."
            ),
            covers=("pheasant",),
            questions=[
                Question(
                    key="pheasant.name",
                    prompt="Knowledge base name",
                    help="Lowercase, no spaces. Used as the stable ID.",
                ),
                Question(
                    key="pheasant.description",
                    prompt="One-line description",
                    help="Shown in the UI and published in the Synapse contract.",
                ),
                Question(
                    key="pheasant.log_level",
                    prompt="Log level",
                    kind="choice",
                    choices=[
                        Choice("INFO", "INFO", "Normal operation."),
                        Choice("DEBUG", "DEBUG", "Verbose; useful when a sync misbehaves."),
                        Choice("WARNING", "WARNING", "Quiet."),
                    ],
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="paths",
            title="Where things live on disk",
            blurb=(
                "pheasant splits its storage two ways: /state is operational truth "
                "(SQLite, the graph, manifests) and is user data — back it up; "
                "/exports holds regenerable payloads. The defaults are the container "
                "paths; running outside Docker you probably want local directories."
            ),
            covers=("storage",),
            questions=[
                Question(
                    key="pheasant.state_path",
                    prompt="State directory",
                    kind="path",
                    help="SQLite + graph + manifests. Treat as user data.",
                ),
                Question(
                    key="pheasant.exports_path",
                    prompt="Exports directory",
                    kind="path",
                    advanced=True,
                ),
                Question(
                    key="pheasant.workspace_root",
                    prompt="Workspace root",
                    kind="path",
                    help="A relative source path is resolved against this.",
                ),
                Question(
                    key="storage.graph_snapshots",
                    prompt="Keep compressed graph snapshots after each sync?",
                    kind="bool",
                    help="Timestamped zstd history beside the live graph. Cheap; "
                    "bounded by the size cap below.",
                    advanced=True,
                ),
                Question(
                    key="storage.max_state_size_gb",
                    prompt="Cap total snapshot size (GB)",
                    kind="float",
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="sources",
            title="Sources",
            blurb=(
                "What to index. Point this at anything readable: a folder, a git "
                "checkout or clone URL, an Obsidian vault, a single file, a list of "
                "web pages, or a connector (notion, slack, gdrive, confluence, imap). "
                "Paste one target per prompt; blank when you are done. You can add "
                "more later from the UI or with `pheasant up <target>`."
            ),
            covers=("sources",),
            questions=[],  # handled by _ask_sources
        ),
        Section(
            id="search",
            title="Search",
            blurb=(
                "How self-search answers a query. 'hybrid' merges keyword (BM25), "
                "vector and graph arms by reciprocal rank fusion; 'text' is keyword "
                "only and needs no model or extra dependency."
            ),
            covers=("search",),
            questions=[
                Question(
                    key="search.default_mode",
                    prompt="Default search mode",
                    kind="choice",
                    choices=[
                        Choice("hybrid", "hybrid", "Keyword + vector + graph, fused."),
                        Choice("text", "text", "Keyword only. No model, no extras."),
                        Choice("vector", "vector", "Semantic only. Needs embeddings on."),
                        Choice("graph", "graph", "Relationship traversal only."),
                    ],
                ),
                Question(
                    key="search.max_results_default",
                    prompt="Results per query by default",
                    kind="int",
                ),
            ],
        ),
        Section(
            id="embeddings",
            title="Semantic search (embeddings)",
            blurb=(
                "Optional. Off by default and pheasant is fully usable without it — "
                "keyword search needs no model, no network and no API key. Turn it on "
                "to match on meaning rather than words. The 'stub' provider is "
                "deterministic and offline, which is a real way to try the plumbing "
                "before spending anything."
            ),
            covers=(),  # part of `search`, claimed above
            questions=[
                Question(
                    key="search.embeddings.enabled",
                    prompt="Enable semantic search?",
                    kind="bool",
                ),
                Question(
                    key="search.embeddings.provider",
                    prompt="Embedding provider",
                    kind="choice",
                    choices=[
                        Choice(
                            "openai-spec",
                            "OpenAI-spec endpoint",
                            "OpenAI, or any gateway/self-hosted server speaking the same shape.",
                        ),
                        Choice("stub", "Deterministic stub", "Offline, no key, no network."),
                    ],
                    when=lambda a: bool(a.get("search.embeddings.enabled")),
                ),
                Question(
                    key="search.embeddings.model",
                    prompt="Search embedding model",
                    help="The model used to turn indexed text into vectors for semantic search.",
                    when=lambda a: a.get("search.embeddings.provider") == "openai-spec",
                ),
                Question(
                    key="search.embeddings.base_url",
                    prompt="Embeddings base URL",
                    when=lambda a: a.get("search.embeddings.provider") == "openai-spec",
                    advanced=True,
                ),
                Question(
                    key="search.embeddings.api_key_env",
                    prompt="Environment variable holding the API key",
                    help="Only the NAME goes in the config. The value goes to .env.",
                    validate=_validate_env_name,
                    when=lambda a: a.get("search.embeddings.provider") == "openai-spec",
                ),
                Question(
                    key="__secret__.embeddings",
                    prompt="Paste the embedding API key (blank to set it yourself later)",
                    kind="secret",
                    secret_env_key="search.embeddings.api_key_env",
                    when=lambda a: a.get("search.embeddings.provider") == "openai-spec",
                ),
                Question(
                    key="search.vector_store.provider",
                    prompt="Vector store backend",
                    kind="choice",
                    choices=[
                        Choice("numpy", "numpy", "Flat file. Always available, no extra."),
                        Choice("lancedb", "lancedb", "Needs the [vector] extra."),
                    ],
                    when=lambda a: bool(a.get("search.embeddings.enabled")),
                ),
            ],
        ),
        Section(
            id="assistant",
            title="Answering questions (the assistant)",
            blurb=(
                "Grounded chat over the index. This is query-time only — it never "
                "touches the indexing path. With no model configured, answers are "
                "extractive (the retrieved passages, attributed), which works offline "
                "and is what the test suite runs on. 'auto' picks whichever provider "
                "has a key in the environment."
            ),
            covers=("assistant",),
            questions=[
                Question(
                    key="assistant.enabled",
                    prompt="Enable the assistant?",
                    kind="bool",
                ),
                Question(
                    key="assistant.provider",
                    prompt="Chat provider",
                    kind="choice",
                    choices=[
                        Choice("auto", "auto", "First provider with a key in the env."),
                        Choice("anthropic", "Anthropic", "ANTHROPIC_API_KEY"),
                        Choice("openai", "OpenAI", "OPENAI_API_KEY"),
                        Choice("gemini", "Gemini", "GEMINI_API_KEY"),
                        Choice("none", "None", "Extractive answers only. No network."),
                    ],
                    when=lambda a: bool(a.get("assistant.enabled")),
                ),
                Question(
                    key="assistant.model",
                    prompt="Chat and agent workflow model",
                    kind="model",
                    help=(
                        "One model powers grounded chat and the agent workflow. "
                        "Choose the tested provider default or enter a custom model ID."
                    ),
                    when=lambda a: (
                        bool(a.get("assistant.enabled")) and a.get("assistant.provider") != "none"
                    ),
                ),
                Question(
                    key="__secret__.assistant",
                    prompt="Paste the chat API key (blank to set it yourself later)",
                    kind="secret",
                    secret_env_key="__provider_key_env__",
                    when=lambda a: a.get("assistant.provider") in {"anthropic", "openai", "gemini"},
                ),
                Question(
                    key="assistant.workflow",
                    prompt="Answering workflow",
                    kind="choice",
                    choices=[
                        Choice(
                            "auto",
                            "auto",
                            "Agentic when [agent] is installed and a "
                            "model is reachable, else single-pass.",
                        ),
                        Choice("agentic", "agentic", "Plan → retrieve → grade loop."),
                        Choice("simple", "simple", "One retrieval pass. Fastest."),
                    ],
                    when=lambda a: bool(a.get("assistant.enabled")),
                ),
            ],
        ),
        Section(
            id="retrieval",
            title="Retrieval tuning",
            blurb=(
                "How hard the assistant looks before it answers. More rounds and more "
                "passages means better answers on hard questions and slower, more "
                "expensive ones on easy questions. These are the same knobs an agent "
                "can override per-call over MCP, so you can test a setting before "
                "committing it here."
            ),
            covers=(),  # part of `assistant`, claimed above
            questions=[
                Question(
                    key="assistant.retrieval.max_rounds",
                    prompt="Retrieval rounds (plan → retrieve → grade turns)",
                    kind="int",
                    help="1 disables the re-plan loop.",
                ),
                Question(
                    key="assistant.retrieval.per_query_results",
                    prompt="Passages fetched per query per mode",
                    kind="int",
                ),
                Question(
                    key="assistant.retrieval.max_context_passages",
                    prompt="Passages offered to the answer step",
                    kind="int",
                ),
                Question(
                    key="assistant.retrieval.expand_graph",
                    prompt="Walk the knowledge graph out of the best hits?",
                    kind="bool",
                    help="Reaches documents that share no vocabulary with the "
                    "question but are connected structurally.",
                ),
                Question(
                    key="assistant.retrieval.expand_depth",
                    prompt="Graph expansion depth",
                    kind="int",
                    when=lambda a: bool(a.get("assistant.retrieval.expand_graph")),
                    advanced=True,
                ),
                Question(
                    key="assistant.retrieval.grade_evidence",
                    prompt="Ask the model to grade its evidence before answering?",
                    kind="bool",
                    advanced=True,
                ),
                Question(
                    key="assistant.retrieval.retrieval_modes",
                    prompt="Search modes to fan out over (comma-separated)",
                    kind="list",
                    help="text, vector, graph, hybrid. 'vector' is dropped "
                    "automatically when no vector index exists.",
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="sync",
            title="Keeping the index fresh",
            blurb=(
                "Re-indexing is incremental and idempotent: unchanged content is "
                "skipped by content hash, so a scheduled beat over a quiet source "
                "costs almost nothing. The watcher reacts to file changes; the "
                "scheduler is the backstop for anything the watcher cannot see."
            ),
            covers=("sync",),
            questions=[
                Question(
                    key="sync.watcher.enabled",
                    prompt="Watch source directories for changes?",
                    kind="bool",
                ),
                Question(
                    key="sync.scheduler.enabled",
                    prompt="Re-sync on a timer?",
                    kind="bool",
                ),
                Question(
                    key="sync.scheduler.interval_seconds",
                    prompt="Timer interval (seconds)",
                    kind="int",
                    when=lambda a: bool(a.get("sync.scheduler.enabled")),
                ),
                Question(
                    key="sync.limits.max_files",
                    prompt="Refuse a source with more files than",
                    kind="int",
                    help="A guardrail, not a target: pointing a source at a home "
                    "directory should stop with a message, not consume memory "
                    "until it dies.",
                    advanced=True,
                ),
                Question(
                    key="sync.limits.max_file_size_mb",
                    prompt="Skip any single file larger than (MB)",
                    kind="int",
                    advanced=True,
                ),
                Question(
                    key="sync.queue.enabled",
                    prompt="Keep the index backlog in a durable queue?",
                    kind="bool",
                    help="Off, a sync holds its remaining sources in memory: a "
                    "process killed nine sources into ten loses the tenth. On, "
                    "the backlog is rows, so a restart resumes, the depth is a "
                    "number a scaler can read, and a source that keeps failing "
                    "is set aside instead of retried forever.",
                    advanced=True,
                ),
                Question(
                    key="sync.queue.backend",
                    prompt="Queue backend (local | nats)",
                    kind="str",
                    help="'local' is this knowledge base's own database — no "
                    "broker to run. 'nats' needs the [queue] extra and is for a "
                    "fleet that has outgrown one database.",
                    when=lambda a: bool(a.get("sync.queue.enabled")),
                    advanced=True,
                ),
                Question(
                    key="sync.queue.max_attempts",
                    prompt="Attempts before a source is set aside",
                    kind="int",
                    when=lambda a: bool(a.get("sync.queue.enabled")),
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="graph",
            title="Knowledge graph",
            blurb=(
                "Artifacts, chunks, symbols, entities and headings, wired by "
                "contains / has_chunk / has_heading / mentions / imports / "
                "references edges. Memory bridging wires agent-memory records "
                "into that graph; turn it off if you would rather keep "
                "remembered facts out of it."
            ),
            covers=("graph",),
            questions=[
                Question(
                    key="graph.memory_entity_bridging",
                    prompt="Link agent-memory records into the knowledge graph",
                    kind="bool",
                ),
            ],
        ),
        Section(
            id="ingestion",
            title="Documents, images and audio",
            blurb=(
                "Text that cannot be reached by decoding the bytes. Documents "
                "(PDF, DOCX, PPTX, XLSX, DOC, RTF, EPUB) are extracted, images "
                "captioned and audio transcribed, all into text that flows through "
                "the ordinary chunk → graph path. An authored sidecar "
                "(<file>.extract.txt / .caption.txt / .transcript.txt) always wins, "
                "and none of the three is built unless a source's include globs "
                "admit those extensions. Without the extractor a document is "
                "findable by its path and invisible by its content. PDF extraction "
                "reads an existing text layer; OCR for image-only scans is not "
                "currently bundled, so use an .extract.txt sidecar for those."
            ),
            covers=("ingestion",),
            questions=[
                Question(
                    key="ingestion.extractor.provider",
                    prompt="Document text extraction",
                    kind="choice",
                    choices=[
                        Choice("auto", "Automatic", "pymupdf/python-docx, else stdlib."),
                        Choice("native", "Third-party readers", "Best fidelity."),
                        Choice("builtin", "Standard library only", "No third-party imports."),
                        Choice(
                            "sandboxed",
                            "WASM-sandboxed PDF",
                            "For untrusted PDFs; needs the [wasm] extra.",
                        ),
                    ],
                    advanced=True,
                ),
                Question(
                    key="ingestion.captioner.provider",
                    prompt="Image captioner",
                    kind="choice",
                    choices=[
                        Choice("stub", "Deterministic stub", "Offline, no key."),
                        Choice("openai-spec", "Vision model", "OpenAI-spec chat endpoint."),
                    ],
                    advanced=True,
                ),
                Question(
                    key="ingestion.transcriber.provider",
                    prompt="Audio transcriber",
                    kind="choice",
                    choices=[
                        Choice("stub", "Deterministic stub", "Offline, no audio library."),
                        Choice("openai-spec", "Speech-to-text", "OpenAI-spec audio endpoint."),
                    ],
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="server",
            title="Server and MCP",
            blurb=(
                "The HTTP API, the web UI and the MCP server all run on one port. "
                "The API is unauthenticated by design (a local-first tool behind your "
                "own perimeter), which is why the CORS origin list is explicit rather "
                "than a wildcard — with '*', any page in your browser can drive every "
                "route on it."
            ),
            covers=("server",),
            questions=[
                Question(key="server.port", prompt="Port", kind="int"),
                Question(
                    key="server.role",
                    prompt="Process role (all | api | indexer | graph | worker)",
                    kind="str",
                    help="'all' is right for one container. Split roles only in a "
                    "fleet, where several serving replicas must not each watch the "
                    "same directories and each index the same source. 'api' "
                    "publishes index work instead of running it, so it needs the "
                    "durable queue and an indexer draining it.",
                    advanced=True,
                ),
                Question(
                    key="server.host",
                    prompt="Bind address",
                    help="0.0.0.0 inside a container is correct; publish the port to "
                    "127.0.0.1 on the host.",
                    advanced=True,
                ),
                Question(
                    key="server.ui.enabled",
                    prompt="Serve the web UI?",
                    kind="bool",
                ),
                Question(
                    key="server.mcp.enabled",
                    prompt="Enable the MCP server (for coding agents)?",
                    kind="bool",
                ),
            ],
        ),
        Section(
            id="security",
            title="Security",
            blurb=(
                "Two things matter here. Secret exclusion unions a credential-pattern "
                "list into every filesystem source, so pointing a source at a home "
                "directory does not sweep up ~/.aws or ~/.ssh — leave it on. ACL "
                "enforcement is for multi-user deployments; a single-user region "
                "should leave it off."
            ),
            covers=("security",),
            questions=[
                Question(
                    key="security.default_exclude_secrets",
                    prompt="Always exclude credential files from indexing?",
                    kind="bool",
                ),
                Question(
                    key="security.allow_user_selected_source_paths",
                    prompt="Allow sources anywhere on the filesystem?",
                    kind="bool",
                    help="Off restricts sources to the allow-listed roots below.",
                ),
                Question(
                    key="security.acl_enforced",
                    prompt="Enforce per-principal ACLs on retrieval?",
                    kind="bool",
                    advanced=True,
                ),
                Question(
                    key="security.api_auth.behind_authenticating_proxy",
                    prompt="Is something in front of this API already authenticating callers?",
                    help=(
                        "Only matters for a fleet: every role but 'all' refuses to start "
                        "on an address other machines can reach unless either this is on "
                        "or PHEASANT_API_TOKEN holds a token. Answer yes only if an "
                        "ingress really does authenticate — the failure it suppresses is "
                        "an open API, and it is silent."
                    ),
                    kind="bool",
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="memory",
            title="Agent memory",
            blurb=(
                "Agents can write memories through MCP; they become ordinary indexed "
                "content, so recall is just search. Consolidation archives superseded "
                "records (renamed in place, never deleted). TTLs are opt-in — blank "
                "means a scope never expires. A corrected memory stops being returned "
                "immediately, without waiting for consolidation."
            ),
            covers=("memory",),
            questions=[
                Question(
                    key="memory.consolidation_enabled",
                    prompt="Consolidate superseded memories?",
                    kind="bool",
                    advanced=True,
                ),
                Question(
                    key="memory.default_policy",
                    prompt="How should memory take part in a search that does not say?",
                    help=(
                        "auto: like any other source. off: never in results. "
                        "only: memory and nothing else. prefer: memory keeps a share "
                        "of the slots. A per-call setting always wins over this."
                    ),
                    kind="choice",
                    choices=(
                        Choice("auto", "Like any other source"),
                        Choice("off", "Never in results"),
                        Choice("only", "Memory and nothing else"),
                        Choice("prefer", "Guaranteed a share of the slots"),
                    ),
                    advanced=True,
                ),
                Question(
                    key="memory.steering_enabled",
                    prompt="Let memories re-rank results (aliases, path preferences)?",
                    help=(
                        "Records written as kind alias/preference/exclusion become "
                        "retrieval rules — an alias makes a query for your word find "
                        "documents that only use the other one. Off by default because "
                        "a memory that silently re-orders searches is a surprise."
                    ),
                    kind="bool",
                    advanced=True,
                ),
                Question(
                    key="memory.usage_tracking",
                    prompt="Count which memories retrieval returns?",
                    help=(
                        "Feeds salience, which decides what is dropped first when the "
                        "store is bounded. It is a write on the read path, so it is "
                        "off unless you ask for it."
                    ),
                    kind="bool",
                    advanced=True,
                ),
                Question(
                    key="memory.max_records",
                    prompt="Maximum memory records to keep (blank = unbounded)",
                    help=(
                        "Past this many, the least salient are archived — renamed in "
                        "place, never deleted."
                    ),
                    kind="int",
                    advanced=True,
                ),
                Question(
                    key="memory.session_max_records",
                    prompt="Maximum session-scope records (blank = unbounded)",
                    help=(
                        "Caps session scratch notes on their own, so a busy agent "
                        "session cannot use its share of max_records to crowd out "
                        "org-wide facts — each scope is its own pool."
                    ),
                    kind="int",
                    advanced=True,
                ),
                Question(
                    key="memory.user_max_records",
                    prompt="Maximum per-user records (blank = unbounded)",
                    help="Same isolation, for one user's own memories.",
                    kind="int",
                    advanced=True,
                ),
                Question(
                    key="memory.org_max_records",
                    prompt="Maximum org-scope records (blank = unbounded)",
                    help="The shared, everyone-reads-it pool's own cap, isolated the same way.",
                    kind="int",
                    advanced=True,
                ),
                Question(
                    key="memory.max_records_per_subject",
                    prompt="Maximum records per subject, across scopes (blank = unbounded)",
                    help=(
                        "Caps how many records can accumulate about one named entity "
                        "(a service, a person, a project) before the least salient are "
                        "archived. Records with no subject are unaffected."
                    ),
                    kind="int",
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="observability",
            title="Tracing and the observation plane",
            blurb=(
                "Optional, and off by default. Every API and MCP call can emit a "
                "span — exported to your collector, recorded in the region's own "
                "interaction ledger, or both. The ledger is what lets memory "
                "formation learn from how the knowledge base is actually used. It "
                "records queries and principals, so it is a deliberate choice: "
                "observations are rows with a retention policy, never indexed "
                "content, and nothing observed becomes a memory without an "
                "admission."
            ),
            covers=("observability",),
            questions=[
                Question(
                    key="observability.otlp_endpoint",
                    prompt="OTLP collector endpoint (blank = export nothing)",
                    help=(
                        "Blank attaches no exporter at all. Spans are still created "
                        "and still feed the interaction ledger below; they just do "
                        "not leave the box. Needs the [otel] extra to export."
                    ),
                    default="",
                    advanced=True,
                ),
                Question(
                    key="observability.interactions.enabled",
                    prompt="Record interactions (queries, principals, results)?",
                    help=(
                        "The evidence memory formation reads. Rows in /state with a "
                        "retention policy — never files, never indexed, never "
                        "returned by a search. Off, the region behaves exactly as it "
                        "did before this existed."
                    ),
                    kind="bool",
                    advanced=True,
                ),
                Question(
                    key="observability.interactions.hot_retention_days",
                    prompt="Days to keep interactions queryable in /state (0 = cold only)",
                    help=(
                        "0 means batches go straight to Parquet and /state never "
                        "grows: you keep the audit trail and the evaluation corpus "
                        "without a query-time ledger."
                    ),
                    kind="int",
                    when=lambda a: bool(a.get("observability.interactions.enabled")),
                    advanced=True,
                ),
                Question(
                    key="observability.interactions.cold_enabled",
                    prompt="Roll expired interactions to Parquet under /exports?",
                    help=(
                        "Off, they are simply deleted when they age out. On, whole "
                        "days are written to <exports>/interactions/dt=... first, "
                        "which is what `pheasant eval bootstrap` reads. A Parquet "
                        "directory enforces no access control — put it on the "
                        "directory."
                    ),
                    kind="bool",
                    when=lambda a: bool(a.get("observability.interactions.enabled")),
                    advanced=True,
                ),
                Question(
                    key="observability.interactions.queue.enabled",
                    prompt="Hand log work to a dedicated tier?",
                    help=(
                        "Off, whoever produced a batch writes it. On, batches are "
                        "published to their own queue and a `serve --role logger` "
                        "drains them — so persistence, rolling and compaction never "
                        "touch a request or the indexer's sync lock. Worth it in a "
                        "fleet; pure overhead in one container."
                    ),
                    kind="bool",
                    when=lambda a: bool(a.get("observability.interactions.enabled")),
                    advanced=True,
                ),
                Question(
                    key="observability.interactions.queue.backend",
                    prompt="Log queue backend (local | nats)",
                    kind="str",
                    when=lambda a: bool(a.get("observability.interactions.queue.enabled")),
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="evaluation",
            title="Knowledge-effectiveness evaluation",
            blurb=(
                "Optional, off by default, and read-only when on. A run replays "
                "recorded queries against a corpus-only baseline and the memory "
                "system, and reports what changed with the evidence and the "
                "denominator attached. It never publishes a single 'accuracy' "
                "score: what it produces is a health vector plus hard gates for "
                "ACL leakage, stale-fact leakage and temporal correctness. "
                "Needs the interaction ledger above to have something to replay."
            ),
            covers=("evaluation",),
            questions=[
                Question(
                    key="evaluation.enabled",
                    prompt="Measure knowledge effectiveness over time?",
                    help=(
                        "Replays cohorts of real queries through the real search "
                        "path and writes evidence-bearing reports to /state. "
                        "Nothing it writes is ever indexed or retrievable as "
                        "knowledge."
                    ),
                    kind="bool",
                    advanced=True,
                ),
                Question(
                    key="evaluation.on_material_snapshot",
                    prompt="Run automatically when the knowledge base changes materially?",
                    help=(
                        "Off by default: a run costs one search per query per "
                        "variant, which is real work to start doing on a timer "
                        "without being asked. It never runs inside the sync lock."
                    ),
                    kind="bool",
                    when=lambda a: bool(a.get("evaluation.enabled")),
                    advanced=True,
                ),
                Question(
                    key="evaluation.max_results",
                    prompt="Results fetched per query per variant",
                    kind="int",
                    when=lambda a: bool(a.get("evaluation.enabled")),
                    advanced=True,
                ),
                Question(
                    key="evaluation.maximum_queries_per_run",
                    prompt="Ceiling on queries replayed in one run",
                    help=(
                        "A run past this is truncated and says which cohorts it "
                        "dropped, rather than quietly reporting a smaller "
                        "denominator as if it were the whole thing."
                    ),
                    kind="int",
                    when=lambda a: bool(a.get("evaluation.enabled")),
                    advanced=True,
                ),
                Question(
                    key="evaluation.run_stale_seconds",
                    prompt="Seconds a batch may go silent before it is called interrupted",
                    help=(
                        "A batch heartbeats while it works. Past this, another "
                        "process marks it interrupted and the next run resumes "
                        "from its checkpoints. Raise it if one replay here takes "
                        "a long time; lower it to notice a stopped container "
                        "sooner."
                    ),
                    kind="float",
                    when=lambda a: bool(a.get("evaluation.enabled")),
                    advanced=True,
                ),
                Question(
                    key="evaluation.promotion.enabled",
                    prompt="Let validated memory candidates be promoted automatically?",
                    help=(
                        "Off by default. On, a candidate is promoted only when "
                        "every hard gate passes AND it improves later queries it "
                        "was not derived from. Off, the same decisions are "
                        "computed and recorded and nothing is applied — which is "
                        "what to run first."
                    ),
                    kind="bool",
                    when=lambda a: bool(a.get("evaluation.enabled")),
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="tuning",
            title="Retrieval performance tuning",
            blurb=(
                "Optional, off by default, and read-only when on. A batch replays "
                "recorded queries with per-stage capture, works out which step of "
                "retrieval actually lost each document — the lexical arm, a filter, "
                "the fusion, the cut — and proposes parameters only for the stages "
                "to blame. Any winner has to improve a held-out cohort it was not "
                "selected on before it may change what the region serves. Needs "
                "evaluation above, because it tunes against the same cohorts and "
                "the same proof."
            ),
            covers=("tuning",),
            questions=[
                Question(
                    key="tuning.enabled",
                    prompt="Diagnose and tune retrieval parameters?",
                    help=(
                        "Produces a stage diagnosis and, where it helps, a proposed "
                        "configuration bundle. Producing one changes nothing: "
                        "applying it is a separate step."
                    ),
                    kind="bool",
                    advanced=True,
                ),
                Question(
                    key="tuning.auto.enabled",
                    prompt="Run a tuning batch automatically when the corpus changes materially?",
                    help=(
                        "Only where the scheduler runs, so API replicas never start "
                        "one. It stands down while the index queue has work in it."
                    ),
                    kind="bool",
                    when=lambda a: bool(a.get("tuning.enabled")),
                    advanced=True,
                ),
                Question(
                    key="tuning.auto.apply",
                    prompt="Let a passing bundle change the fleet's ranking without asking?",
                    help=(
                        "Off by default, and worth leaving off until you have read a "
                        "few reports. On, a bundle that passes every gate — including "
                        "confirmation on a cohort the search never saw — becomes the "
                        "region's live overlay for every replica. Off, the same work "
                        "happens and the bundle waits for `pheasant tune apply`."
                    ),
                    kind="bool",
                    when=lambda a: bool(a.get("tuning.enabled")),
                    advanced=True,
                ),
                Question(
                    key="tuning.objective.metric",
                    prompt=(
                        "What should tuning optimize? "
                        "(reciprocal_rank | recall_at_5 | recall_at_10 | hit_rate | balanced)"
                    ),
                    help=(
                        "Not a detail. A region whose agents read one result "
                        "wants reciprocal_rank; one whose agents read a page "
                        "and synthesize wants recall_at_10, and would be "
                        "actively harmed by a parameter set that sharpens "
                        "rank one at the cost of dropping a document out of "
                        "the list. Each objective publishes what it trades "
                        "away; `pheasant tune show` reports which is in force."
                    ),
                    kind="str",
                    when=lambda a: bool(a.get("tuning.enabled")),
                    advanced=True,
                ),
                Question(
                    key="tuning.requery_trials",
                    prompt="Points that may be tried with real searches",
                    help=(
                        "The number that decides how long a batch runs. Fusion "
                        "parameters are free — they are re-computed from cached "
                        "candidates — so this only bounds the family that changes "
                        "what the arms retrieve."
                    ),
                    kind="int",
                    when=lambda a: bool(a.get("tuning.enabled")),
                    advanced=True,
                ),
                Question(
                    key="tuning.max_searches",
                    prompt="Ceiling on searches in one batch",
                    kind="int",
                    when=lambda a: bool(a.get("tuning.enabled")),
                    advanced=True,
                ),
                Question(
                    key="tuning.tracking.backend",
                    prompt="Mirror experiments to a tracking backend? (off | mlflow)",
                    help=(
                        "/state is always the source of truth. `mlflow` adds a "
                        "mirror — needs the [tuning] extra, and with no tracking_uri "
                        "it writes a local file store under <exports>/tuning/mlruns "
                        "that `mlflow ui` opens later with nothing running in "
                        "between. A tracking backend being down never fails a batch."
                    ),
                    kind="str",
                    when=lambda a: bool(a.get("tuning.enabled")),
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="readiness",
            title="Stress-test readiness",
            blurb=(
                "Optional, off by default, and read-only when on. Publishes a "
                "machine-readable capability contract and runs the gates an "
                "outside harness needs before it treats this region's answers as "
                "evidence: every submitted write reconciles, every result names "
                "an exact source location, no forbidden content crosses an "
                "isolation boundary. Turn it on when something other than a "
                "person is going to measure this region."
            ),
            covers=("readiness",),
            questions=[
                Question(
                    key="readiness.enabled",
                    prompt="Publish a readiness contract and allow readiness checks?",
                    help=(
                        "Adds a contract endpoint and a checkable gate. It changes "
                        "nothing about what a search returns or what a sync does."
                    ),
                    kind="bool",
                    advanced=True,
                ),
                Question(
                    key="readiness.max_search_latency_ms",
                    prompt="Search latency budget (p95, ms)",
                    help=(
                        "A performance gate cannot pass or fail against an unstated "
                        "number, so this is deliberately present and generous rather "
                        "than absent. Tighten it once you have measured."
                    ),
                    kind="float",
                    when=lambda a: bool(a.get("readiness.enabled")),
                    advanced=True,
                ),
                Question(
                    key="readiness.max_index_lag_ms",
                    prompt="Index-lag budget (ms)",
                    help=(
                        "How long after acceptance an item may take to become "
                        "searchable. The number that decides whether a harness "
                        "querying immediately is early or the region is late."
                    ),
                    kind="float",
                    when=lambda a: bool(a.get("readiness.enabled")),
                    advanced=True,
                ),
            ],
        ),
        Section(
            id="synapse",
            title="Synapse federation",
            blurb=(
                "Optional. Joins this knowledge base to a Synapse fleet as a region: "
                "it publishes a semantic contract describing what it knows, and a "
                "router uses that to decide when to ask it. Everything here no-ops "
                "when publishing is off — a standalone pheasant is unaffected."
            ),
            covers=("synapse",),
            questions=[
                Question(
                    key="synapse.publish",
                    prompt="Publish a semantic contract to a Synapse router?",
                    kind="bool",
                ),
                Question(
                    key="synapse.router_url",
                    prompt="Router base URL",
                    default="",
                    when=lambda a: bool(a.get("synapse.publish")),
                ),
                Question(
                    key="synapse.fleet_id",
                    prompt="Fleet ID",
                    default="",
                    when=lambda a: bool(a.get("synapse.publish")),
                ),
                Question(
                    key="synapse.endpoint",
                    prompt="This region's endpoint, as the router will reach it",
                    default="",
                    when=lambda a: bool(a.get("synapse.publish")),
                ),
            ],
        ),
    ]


def build_phases(sections: list[Section] | None = None) -> list[Phase]:
    """Return the six concise chapters shown by the guided terminal flow.

    ``build_sections`` remains the canonical flat schema surface so existing
    answer files and freshness checks continue to work.
    """

    available = {section.id for section in (sections or build_sections())}
    definitions = (
        Phase(
            "foundation",
            "Foundation",
            "Identity and the directories pheasant owns.",
            ("identity", "paths"),
        ),
        Phase(
            "sources-content",
            "Sources & content",
            "What to index and how documents are extracted.",
            ("sources", "ingestion"),
        ),
        Phase(
            "search-graph",
            "Search & graph",
            "Keyword/vector search, embeddings, and relationships between artifacts.",
            ("search", "embeddings", "graph"),
        ),
        Phase(
            "assistant-memory",
            "Assistant & memory",
            "The single chat/agent model, retrieval behavior, and agent memory.",
            ("assistant", "retrieval", "memory"),
        ),
        Phase(
            "operations-security",
            "Operations & security",
            (
                "Synchronization, the server surface, tracing, effectiveness "
                "measurement, and filesystem safeguards."
            ),
            ("sync", "server", "observability", "evaluation", "security"),
        ),
        Phase(
            "federation",
            "Federation",
            "Optional Synapse publication settings.",
            ("synapse",),
        ),
    )
    return [phase for phase in definitions if any(item in available for item in phase.section_ids)]


# --------------------------------------------------------------------------
# Prompting
# --------------------------------------------------------------------------


class _LegacyPrompter:
    """Terminal I/O, isolated so the whole wizard is testable.

    :class:`ScriptedPrompter` below is the test double; nothing in the wizard
    reads ``input()`` or writes ``print()`` directly.
    """

    def __init__(self, *, color: bool | None = None) -> None:
        self.color = os.environ.get("NO_COLOR") is None if color is None else color

    def say(self, text: str = "") -> None:
        print(text)

    def rule(self, title: str) -> None:
        print()
        print(self._bold(f"── {title} " + "─" * max(0, 62 - len(title))))

    def ask(self, prompt: str, default_hint: str) -> str:
        suffix = f" [{default_hint}]" if default_hint else ""
        rendered = f"{prompt}{suffix}"
        if len(rendered) > self.width - 2:
            lines = _wrap(rendered, max(20, self.width - 2))
            for line in lines[:-1]:
                self.say(line)
            prompt = lines[-1]
            suffix = ""
        try:
            return input(f"{prompt}{suffix}: ").strip()
        except EOFError:  # piped stdin that ran out — take the defaults
            print()
            return ""

    def ask_secret(self, prompt: str) -> str:
        import getpass

        try:
            return getpass.getpass(f"{prompt}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return ""

    def _bold(self, text: str) -> str:
        return f"\033[1m{text}\033[0m" if self.color else text

    def styled(self, text: str, style: str) -> None:
        """Print a consistently styled status line when Rich is available."""

        if self._console is not None:
            self._console.print(text, style=style, markup=False, soft_wrap=True)
        else:
            self.say(text)

    def success(self, text: str) -> None:
        self.styled(text, "green")

    def warning(self, text: str) -> None:
        self.styled(text, "yellow")

    def error(self, text: str) -> None:
        self.styled(text, "bold red")


class _LegacyScriptedPrompter(_LegacyPrompter):
    """A prompter driven by a list of canned answers (tests, ``--answers``)."""

    def __init__(self, answers: Iterable[str]) -> None:
        super().__init__(color=False)
        self._answers = list(answers)
        self.transcript: list[str] = []

    def say(self, text: str = "") -> None:
        self.transcript.append(text)

    def rule(self, title: str) -> None:
        self.transcript.append(f"== {title}")

    def ask(self, prompt: str, default_hint: str) -> str:
        self.transcript.append(f"? {prompt} [{default_hint}]")
        return self._answers.pop(0).strip() if self._answers else ""

    def ask_secret(self, prompt: str) -> str:
        self.transcript.append(f"?? {prompt}")
        return self._answers.pop(0).strip() if self._answers else ""


class Prompter:
    """Responsive terminal renderer with a dependency-free plain fallback."""

    def __init__(
        self,
        *,
        color: bool | None = None,
        plain: bool = False,
        width: int | None = None,
        interactive: bool | None = None,
    ) -> None:
        detected = width or shutil.get_terminal_size(fallback=(80, 24)).columns
        self.width = max(40, min(100, detected))
        self.plain = (
            plain or os.environ.get("NO_COLOR") is not None or os.environ.get("TERM") == "dumb"
        )
        self.interactive = (
            bool(interactive)
            if interactive is not None
            else bool(
                getattr(sys.stdin, "isatty", lambda: False)()
                and getattr(sys.stdout, "isatty", lambda: False)()
            )
        )
        self.color = (not self.plain) if color is None else bool(color and not self.plain)
        self._console = None
        if self.color and self.interactive:
            try:
                from rich.console import Console

                self._console = Console(width=self.width, force_terminal=True, highlight=False)
            except ImportError:  # pragma: no cover - minimal installs
                pass

    def say(self, text: str = "") -> None:
        if self.plain:
            text = (
                text.replace("\u2014", "-")
                .replace("\u2192", "->")
                .replace("\u2190", "<-")
                .replace("\u2026", "...")
                .replace("\u2500", "-")
            )
            text = (
                text.replace("—", "-")
                .replace("→", "->")
                .replace("←", "<-")
                .replace("…", "...")
                .replace("─", "-")
                .encode("ascii", "replace")
                .decode("ascii")
            )
        if self._console is not None:
            self._console.print(text, markup=False, soft_wrap=True)
        else:
            lines = text.splitlines() or [""]
            for line in lines:
                if len(line) <= self.width:
                    print(line)
                else:
                    for wrapped in _wrap(line, self.width):
                        print(wrapped)

    def rule(self, title: str, *, progress: str | None = None) -> None:
        label = f"{progress}  {title}" if progress else title
        self.say()
        if self.width < 60:
            self.say(f"-- {label} --")
        else:
            self.say(f"{'─' * 2} {label} " + "─" * max(0, self.width - len(label) - 4))

    def show_choices(self, choices: Sequence[Choice], default: Any = None) -> None:
        if self.width < 60 or self._console is None:
            for choice in choices:
                marker = "*" if choice.value == default else " "
                detail = f" — {choice.detail}" if choice.detail else ""
                for line in _wrap(
                    f"{marker} {choice.value}: {choice.label}{detail}", self.width - 4
                ):
                    self.say(f"    {line}")
            return
        from rich.table import Table

        table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        table.add_column("", width=2)
        table.add_column("Choice", no_wrap=True)
        table.add_column("Description", overflow="fold")
        for choice in choices:
            marker = "*" if choice.value == default else " "
            detail = f"{choice.label} — {choice.detail}" if choice.detail else choice.label
            table.add_row(marker, choice.value, detail)
        self._console.print(table)

    def ask(self, prompt: str, default_hint: str) -> str:
        suffix = f" [{default_hint}]" if default_hint else ""
        rendered = f"{prompt}{suffix}"
        if len(rendered) > self.width - 2:
            lines = _wrap(rendered, max(20, self.width - 2))
            for line in lines[:-1]:
                self.say(line)
            prompt = lines[-1]
            suffix = ""
        try:
            return input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            self.say()
            return ""

    def ask_secret(self, prompt: str) -> str:
        import getpass

        try:
            return getpass.getpass(f"{prompt}: ").strip()
        except (EOFError, KeyboardInterrupt):
            self.say()
            return ""

    def _bold(self, text: str) -> str:
        return f"\033[1m{text}\033[0m" if self.color else text

    def styled(self, text: str, style: str) -> None:
        """Print a consistently styled status line when Rich is available."""

        if self._console is not None:
            self._console.print(text, style=style, markup=False, soft_wrap=True)
        else:
            self.say(text)

    def success(self, text: str) -> None:
        self.styled(text, "green")

    def warning(self, text: str) -> None:
        self.styled(text, "yellow")

    def error(self, text: str) -> None:
        self.styled(text, "bold red")


TerminalRenderer = Prompter


class ScriptedPrompter(Prompter):
    """A prompter driven by canned answers (tests and ``--answers``)."""

    def __init__(self, answers: Iterable[str], *, width: int = 80) -> None:
        super().__init__(color=False, plain=True, width=width, interactive=False)
        self._answers = list(answers)
        self.transcript: list[str] = []

    def say(self, text: str = "") -> None:
        self.transcript.append(text)

    def rule(self, title: str, *, progress: str | None = None) -> None:
        self.transcript.append(f"== {progress + ' ' if progress else ''}{title}")

    def ask(self, prompt: str, default_hint: str) -> str:
        self.transcript.append(f"? {prompt} [{default_hint}]")
        return self._answers.pop(0).strip() if self._answers else ""

    def ask_secret(self, prompt: str) -> str:
        self.transcript.append(f"?? {prompt}")
        return self._answers.pop(0).strip() if self._answers else ""


def _format_default(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if value is None:
        return ""
    return str(value)


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_env_name(value: Any) -> str:
    """Accept an environment-variable *name*, never a pasted credential."""

    text = str(value).strip()
    if not _ENV_NAME_RE.fullmatch(text):
        raise ValueError(
            "enter an environment-variable name such as OPENAI_API_KEY; do not paste the key itself"
        )
    return text


def _coerce(kind: QuestionKind, raw: str, default: Any) -> Any:
    if raw == "":
        return default
    if kind == "bool":
        lowered = raw.lower()
        if lowered in {"y", "yes", "true", "on", "1"}:
            return True
        if lowered in {"n", "no", "false", "off", "0"}:
            return False
        raise ValueError("answer yes or no")
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "list":
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


#: Provider id -> the env var its key is read from. Shared with the runtime
#: provider catalogue so setup labels and credential handling cannot drift.
PROVIDER_KEY_ENV = {name: spec.api_key_env for name, spec in PROVIDERS.items()}


class Wizard:
    """Runs the interview and renders its results."""

    def __init__(
        self,
        prompter: Prompter | None = None,
        *,
        advanced: bool = False,
        accept_defaults: bool = False,
        preset: dict[str, Any] | None = None,
    ) -> None:
        self.prompter = prompter or Prompter()
        self.advanced = advanced
        self.accept_defaults = accept_defaults
        self.answers: dict[str, Any] = dict(preset or {})
        self.secrets: dict[str, str] = {}
        self.sources: list[dict[str, Any]] = []

    # -- the interview ----------------------------------------------------

    def run(self, sections: list[Section] | None = None) -> dict[str, Any]:
        sections = sections or build_sections()
        p = self.prompter
        p.say()
        p.say("pheasant setup — this writes pheasant.yaml and, if you supply any")
        p.say("secrets, a .env file with mode 0600. Press Enter to accept a default;")
        p.say("every default is a working answer.")
        if not self.advanced:
            p.say("Re-run with --advanced to be asked about every option.")
        for section in sections:
            self._run_section(section)
        return self.answers

    def _run_section(self, section: Section) -> None:
        p = self.prompter
        p.rule(section.title)
        for line in _wrap(section.blurb):
            p.say(f"  {line}")
        p.say()
        if section.id == "sources":
            self._ask_sources()
            return
        for question in section.questions:
            self._ask(question)

    def _ask(self, question: Question) -> None:
        if question.when is not None and not question.when(self.answers):
            return
        default = question.resolved_default()
        if question.advanced and not self.advanced:
            if question.key not in self.answers and question.kind != "secret":
                self.answers[question.key] = default
            return
        if question.key in self.answers:
            return  # supplied by --answers or a resumed run
        if question.kind == "secret":
            self._ask_secret(question)
            return
        if self.accept_defaults:
            self.answers[question.key] = default
            return
        if question.help:
            for line in _wrap(question.help, width=72):
                self.prompter.say(f"    {line}")
        if question.kind == "choice":
            for choice in question.choices:
                marker = "*" if choice.value == default else " "
                detail = f" — {choice.detail}" if choice.detail else ""
                self.prompter.say(f"    {marker} {choice.value}: {choice.label}{detail}")
        while True:
            raw = self.prompter.ask(f"  {question.prompt}", _format_default(default))
            try:
                value = _coerce(question.kind, raw, default)
                if question.validate is not None:
                    value = question.validate(value)
            except ValueError as exc:
                self.prompter.say(f"    ! {exc}")
                continue
            if question.kind == "choice" and value not in {c.value for c in question.choices}:
                self.prompter.say(
                    "    ! pick one of: " + ", ".join(c.value for c in question.choices)
                )
                continue
            self.answers[question.key] = value
            return

    def _ask_secret(self, question: Question) -> None:
        env_name = self._secret_env_name(question)
        if not env_name:
            return
        if os.environ.get(env_name):
            self.prompter.say(f"    {env_name} is already set in this environment — skipping.")
            return
        if self.accept_defaults:
            return
        self.prompter.say(f"    Stored in .env as {env_name}, never in pheasant.yaml.")
        value = self.prompter.ask_secret(f"  {question.prompt}")
        if value:
            self.secrets[env_name] = value

    def _secret_env_name(self, question: Question) -> str | None:
        key = question.secret_env_key
        if key == "__provider_key_env__":
            return PROVIDER_KEY_ENV.get(str(self.answers.get("assistant.provider")))
        if not key:
            return None
        value = self.answers.get(key)
        if value is None:
            value = schema_default(key)
        return str(value) if value else None

    def _ask_sources(self) -> None:
        if self.sources:
            return
        if self.accept_defaults:
            return
        from pheasant.targets import TargetError, resolve_target

        p = self.prompter
        while True:
            raw = p.ask("  Target (path, git URL, https:// page, s3://, notion:…)", "done")
            if not raw or raw.lower() == "done":
                break
            try:
                target = resolve_target(
                    raw,
                    clone_root=Path(".pheasant/sources"),
                    workspace=Path(".pheasant/external"),
                )
            except TargetError as exc:
                p.say(f"    ! {exc}")
                continue
            payload = target.to_source_dict()
            existing = {source["name"] for source in self.sources}
            if payload["name"] in existing:
                payload["name"] = f"{payload['name']}-{len(self.sources) + 1}"
            self.sources.append(payload)
            p.say(f"    + {payload['name']} ({payload['type']}) ← {payload['path']}")
        if not self.sources:
            p.say("    No sources yet — add them later from the UI or `pheasant up <target>`.")

    # -- rendering --------------------------------------------------------

    def config_dict(self) -> dict[str, Any]:
        """Assemble the answers into a config mapping, validated before return."""
        # Answers supplied from a file or a resumed run bypass the interactive
        # prompt, so apply the same validators before serializing them.
        for section in build_sections():
            for question in section.questions:
                if question.validate is not None and question.key in self.answers:
                    self.answers[question.key] = question.validate(self.answers[question.key])
        data: dict[str, Any] = {}
        for key, value in self.answers.items():
            if key.startswith("__"):
                continue
            _set_in(data, key, value)
        if self.sources:
            data["sources"] = [dict(source) for source in self.sources]
        # Validate here rather than at write time: a wizard that writes a file
        # the loader then rejects has wasted the whole interview.
        PheasantConfig.model_validate(json.loads(json.dumps(data, default=str)))
        return data

    def env_values(self) -> dict[str, str]:
        return dict(self.secrets)


class SetupSaveAndExit(KeyboardInterrupt):
    """The review menu's explicit "Save progress and exit" action."""


class GuidedWizard(Wizard):
    """Six-phase responsive interview layered over the stable wizard API."""

    def run(self, sections: list[Section] | None = None) -> dict[str, Any]:
        p = self.prompter
        p.say()
        for line in _wrap("pheasant setup — a guided, scrollable configuration interview", p.width):
            p.say(line)
        for line in _wrap(
            "Press Enter to accept a default. Secrets are written only to .env (never YAML).",
            p.width,
        ):
            p.say(line)
        if not self.advanced:
            for line in _wrap(
                "Essentials are shown first; use --advanced for expert fields.", p.width
            ):
                p.say(line)

        if sections is not None:
            for section in sections:
                self._run_section(section)
        else:
            phases = build_phases()
            all_sections = {section.id: section for section in build_sections()}
            for index, phase in enumerate(phases, 1):
                p.rule(phase.title, progress=f"Phase {index}/{len(phases)}")
                for line in _wrap(phase.blurb, max(20, p.width - 4)):
                    p.say(f"  {line}")
                for section_id in phase.section_ids:
                    section = all_sections.get(section_id)
                    if section is None:
                        continue
                    self._run_section(section, nested=True)

        if p.interactive and not self.accept_defaults:
            self._review()
        return self.answers

    def _run_section(self, section: Section, *, nested: bool = False, force: bool = False) -> None:
        p = self.prompter
        if nested:
            p.say()
            p.say(f"  {section.title}")
        else:
            p.rule(section.title)
        for line in _wrap(section.blurb, max(20, p.width - 4)):
            p.say(f"    {line}")
        p.say()
        if section.id == "sources":
            self._ask_sources(force=force)
            return
        for question in section.questions:
            self._ask(question, force=force)

    def _ask(self, question: Question, *, force: bool = False) -> None:
        if question.when is not None and not question.when(self.answers):
            if question.kind == "model":
                self.answers[question.key] = None
            return
        default = question.resolved_default()
        if question.advanced and not self.advanced:
            if question.key not in self.answers and question.kind != "secret":
                self.answers[question.key] = default
            return
        if question.kind == "model" and self.answers.get("assistant.provider") == "auto":
            # A provider-specific ID is unsafe when this config moves between
            # environments, even if an answers file supplied one.
            self._ask_model(question)
            return
        if question.key in self.answers and not force:
            return
        if question.kind == "secret":
            self._ask_secret(question)
            return
        if question.kind == "model":
            self._ask_model(question)
            return
        if self.accept_defaults:
            self.answers[question.key] = default
            return
        if question.help:
            for line in _wrap(question.help, max(20, self.prompter.width - 8)):
                self.prompter.say(f"    {line}")
        if question.kind == "choice":
            self.prompter.show_choices(question.choices, default)
        while True:
            raw = self.prompter.ask(f"  {question.prompt}", _format_default(default))
            try:
                value = _coerce(question.kind, raw, default)
                if question.validate is not None:
                    value = question.validate(value)
            except (TypeError, ValueError) as exc:
                self.prompter.error(f"    ! {exc}")
                continue
            if question.kind == "choice" and value not in {c.value for c in question.choices}:
                self.prompter.error(
                    "    ! pick one of: " + ", ".join(c.value for c in question.choices)
                )
                continue
            self.answers[question.key] = value
            return

    def _ask_model(self, question: Question) -> None:
        provider = str(self.answers.get("assistant.provider") or "auto")
        if provider == "auto":
            resolved = resolve_auto_provider()
            if resolved:
                spec = PROVIDERS[resolved]
                self.prompter.say(
                    f"    Resolved provider: {spec.label} ({spec.api_key_env} available)"
                )
                self.prompter.say(f"    Runtime default: {spec.default_model}")
            else:
                self.prompter.warning("    No model currently available — extractive answers only.")
            self.answers[question.key] = None
            return
        spec = PROVIDERS.get(provider)
        if spec is None:
            self.answers[question.key] = None
            return
        default = spec.default_model
        choices = (
            Choice(
                default, f"{default} (Recommended)", "Tested runtime default for this provider."
            ),
            Choice("__custom__", "Custom model ID", "Enter an ID your account can access."),
        )
        if self.accept_defaults:
            self.answers[question.key] = default
            return
        self.prompter.say(f"    {spec.label} uses one model for chat and agent workflows.")
        self.prompter.show_choices(choices, default)
        while True:
            raw = self.prompter.ask(f"  {question.prompt}", default).strip()
            if not raw or raw in {"1", default}:
                self.answers[question.key] = default
                return
            if raw in {"2", "custom", "Custom model ID", "__custom__"}:
                custom = self.prompter.ask("  Custom model ID", default)
                if custom.strip():
                    self.answers[question.key] = custom.strip()
                    return
                self.prompter.say("    ! enter a model ID or choose the recommended default")
                continue
            self.answers[question.key] = raw
            return

    def _ask_sources(self, *, force: bool = False) -> None:
        if self.sources and not force:
            return
        if self.accept_defaults and not force:
            return
        from pheasant.targets import TargetError, resolve_target

        p = self.prompter
        if force and self.sources:
            p.say("    Existing sources:")
            for index, source in enumerate(self.sources, 1):
                p.say(f"      {index}. {source.get('name')} — {source.get('path')}")
            while True:
                action = p.ask("  Source action (list/add/remove/done)", "done").lower()
                if action in {"", "done", "d"}:
                    return
                if action in {"list", "l"}:
                    for index, source in enumerate(self.sources, 1):
                        p.say(f"      {index}. {source.get('name')} — {source.get('path')}")
                    continue
                if action in {"remove", "remove source", "r"}:
                    raw = p.ask("  Source number or name to remove", "")
                    removed = self._remove_source(raw)
                    p.say(f"    {'Removed ' + removed if removed else 'No matching source.'}")
                    continue
                if action in {"add", "add source", "a"}:
                    self._add_source(resolve_target, TargetError)
                    continue
                p.say("    ! choose list, add, remove, or done")
            return
        while True:
            raw = p.ask("  Target (path, git URL, https:// page, s3://, notion:…)", "done")
            if not raw or raw.lower() == "done":
                break
            self._add_source(resolve_target, TargetError, raw)
        if not self.sources:
            p.warning("    No sources yet — add them later with `pheasant up <target>`.")

    def _add_source(self, resolve_target: Any, target_error: Any, raw: str | None = None) -> None:
        p = self.prompter
        raw = raw or p.ask("  Target", "done")
        if not raw or raw.lower() == "done":
            return
        try:
            target = resolve_target(
                raw, clone_root=Path(".pheasant/sources"), workspace=Path(".pheasant/external")
            )
        except target_error as exc:
            p.error(f"    ! {exc}")
            return
        payload = target.to_source_dict()
        existing = {source["name"] for source in self.sources}
        if payload["name"] in existing:
            payload["name"] = f"{payload['name']}-{len(self.sources) + 1}"
        self.sources.append(payload)
        p.success(f"    + {payload['name']} ({payload['type']}) ← {payload['path']}")

    def _remove_source(self, raw: str) -> str | None:
        try:
            index = int(raw) - 1
            if 0 <= index < len(self.sources):
                return str(self.sources.pop(index).get("name"))
        except ValueError:
            pass
        for index, source in enumerate(self.sources):
            if source.get("name") == raw:
                return str(self.sources.pop(index).get("name"))
        return None

    def _review(self) -> None:
        p = self.prompter
        phases = build_phases()
        while True:
            p.rule("Review configuration")
            for line in self._review_lines():
                for wrapped in _wrap(line, max(20, p.width - 4)):
                    p.say(f"  {wrapped}")
            p.say()
            p.say("  [w] Write files   [e] Edit a phase   [s] Save progress and exit")
            action = p.ask("  Review action", "w").lower()
            if action in {"", "w", "write", "write files"}:
                return
            if action in {"s", "save", "save progress and exit", "q", "quit"}:
                raise SetupSaveAndExit()
            if action in {"e", "edit", "edit a phase"}:
                options = ", ".join(f"{i + 1} {phase.title}" for i, phase in enumerate(phases))
                choice = p.ask(f"  Phase ({options})", "1")
                try:
                    phase = phases[int(choice.split()[0]) - 1]
                except (ValueError, IndexError):
                    phase = next((item for item in phases if item.id == choice), None)
                if phase is None:
                    p.say("    ! choose a phase number")
                    continue
                sections = {section.id: section for section in build_sections()}
                for section_id in phase.section_ids:
                    if section_id in sections:
                        self._run_section(sections[section_id], nested=True, force=True)
                continue
            p.say("    ! choose write, edit, or save")

    def _review_lines(self) -> list[str]:
        data = self.config_dict()
        assistant = data.get("assistant", {})
        search = data.get("search", {})
        embeddings = search.get("embeddings", {})
        provider = assistant.get("provider", "auto")
        model = assistant.get("model")
        if provider == "auto":
            resolved = resolve_auto_provider()
            model_text = PROVIDERS[resolved].default_model if resolved else "none available"
            provider_text = f"auto → {resolved or 'none'} / {model_text}"
        elif provider in PROVIDERS:
            provider_text = f"{provider} / {model or PROVIDERS[provider].default_model}"
        else:
            provider_text = str(provider)
        pheasant = data.get("pheasant", {})
        server = data.get("server", {})
        synapse = data.get("synapse", {})
        embedding_model = (
            embeddings.get("model") or schema_default("search.embeddings.model")
            if embeddings.get("enabled")
            else "off"
        )
        federation = "on" if synapse.get("publish") else "off"
        output_path = getattr(self, "output_path", "pheasant.yaml")
        env_output_path = getattr(self, "env_output_path", ".env")
        lines = [
            f"Output: {output_path}; secrets: {env_output_path} (values hidden)",
            f"Paths: state={pheasant.get('state_path')}; exports={pheasant.get('exports_path')}",
            f"       workspace={pheasant.get('workspace_root')}",
            f"Sources: {len(self.sources)} configured",
            f"Search: {search.get('default_mode')} | embedding model: {embedding_model}",
            f"Assistant: {provider_text} | workflow={assistant.get('workflow')}",
            f"Server: {server.get('host')}:{server.get('port')} | federation: {federation}",
        ]
        for source in self.sources:
            lines.append(f"  - {source.get('name')} ({source.get('type')}): {source.get('path')}")
        credential_envs: list[str] = []
        if embeddings.get("api_key_env"):
            credential_envs.append(str(embeddings["api_key_env"]))
        if provider == "auto":
            credential_envs.extend(spec.api_key_env for spec in PROVIDERS.values())
        elif provider != "none" and provider in PROVIDERS:
            credential_envs.append(PROVIDERS[provider].api_key_env)
        for env_name in dict.fromkeys(credential_envs):
            status = (
                "available"
                if (os.environ.get(env_name) or self.secrets.get(env_name))
                else "not set"
            )
            lines.append(f"Credential {env_name}: {status}")
        return lines


# Keep the public class name and all existing imports stable.
Wizard = GuidedWizard


def _set_in(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cursor = data
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def _wrap(text: str, width: int = 74) -> list[str]:
    import textwrap

    return textwrap.wrap(" ".join(text.split()), width=width) or [""]


# --------------------------------------------------------------------------
# Output files
# --------------------------------------------------------------------------


def render_config_yaml(data: dict[str, Any]) -> str:
    """YAML for a config mapping, in the shape every pheasant writer emits."""
    from pheasant.config.loader import dump_config_yaml

    return dump_config_yaml(data)


_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def merge_env_file(path: Path, values: dict[str, str]) -> tuple[str, list[str], list[str]]:
    """Merge ``values`` into an existing .env, preserving everything else.

    Returns ``(text, added, updated)``. Comments, ordering and unrelated keys
    survive: a setup run is not permitted to silently drop a key someone put
    there by hand. An existing key with the *same* value is left alone so the
    file is not rewritten for nothing.
    """
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: dict[str, int] = {}
    for index, line in enumerate(existing_lines):
        match = _ENV_LINE.match(line)
        if match:
            seen[match.group(1)] = index

    added: list[str] = []
    updated: list[str] = []
    lines = list(existing_lines)
    for name, value in values.items():
        rendered = f"{name}={_quote_env(value)}"
        if name in seen:
            if lines[seen[name]] != rendered:
                lines[seen[name]] = rendered
                updated.append(name)
        else:
            added.append(name)
            lines.append(rendered)
    text = "\n".join(lines).rstrip("\n")
    return (text + "\n" if text else ""), added, updated


def _quote_env(value: str) -> str:
    if value and not any(char.isspace() or char in "#'\"$`\\" for char in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def write_env_file(path: Path, values: dict[str, str]) -> dict[str, Any]:
    """Write the .env with mode 0600, creating parents as needed.

    0600 before the content is written, not after: a secrets file that is
    world-readable for even the length of one write is a secrets file that was
    world-readable.
    """
    text, added, updated = merge_env_file(path, values)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - filesystems without POSIX modes
        pass
    path.write_text(text, encoding="utf-8")
    return {"path": str(path), "added": added, "updated": updated, "mode": "0600"}


def ensure_gitignored(env_path: Path, repo_root: Path | None = None) -> str | None:
    """Make sure the .env is ignored by git. Returns a note when it acted.

    Writing a secrets file into a git working tree without this is how keys end
    up in a public repository, and "the wizard told me to create it" is not a
    defence anyone gets to use.
    """
    root = repo_root or env_path.parent
    if not (root / ".git").exists():
        return None
    gitignore = root / ".gitignore"
    name = env_path.name
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    patterns = {line.strip() for line in existing.splitlines()}
    if name in patterns or ".env" in patterns or "*.env" in patterns:
        return None
    suffix = "" if existing.endswith("\n") or not existing else "\n"
    gitignore.write_text(f"{existing}{suffix}{name}\n", encoding="utf-8")
    return f"Added {name} to {gitignore}"


def startup_commands(config_path: Path, target: str, port: int) -> list[str]:
    """The exact commands to run next, for the chosen deployment target.

    The docker line pins the image to this package's version rather than
    naming ``:latest``. The config it mounts was written by *this* version's
    schema, and an image from a later release is the one arrangement where a
    freshly generated config can fail to load.
    """
    if target == "docker":
        return [
            f"docker run --rm -p {port}:8765 \\",
            f"  -v {config_path.resolve()}:/config/pheasant.yaml:ro \\",
            "  -v $PWD:/workspace:ro -v pheasant-state:/state \\",
            f"  {DEFAULT_IMAGE}",
        ]
    if target == "compose":
        return [
            f"pheasant compose-env {config_path} -o .env.compose",
            "docker compose up -d",
        ]
    return [
        'pip install -e ".[mcp]"',
        f"pheasant sync -c {config_path} --all",
        f"pheasant start -c {config_path}",
    ]


# --------------------------------------------------------------------------
# Progress checkpointing
# --------------------------------------------------------------------------


def save_progress(path: Path, wizard: Wizard) -> None:
    """Answers so far, minus every secret.

    Secrets are deliberately not checkpointed: the whole point of the .env is
    that the key exists in exactly one place with mode 0600, and a resume file
    holding a copy would quietly make that two.
    """
    payload = {"answers": wizard.answers, "sources": wizard.sources}
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def load_progress(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.exists():
        return {}, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}, []
    return dict(payload.get("answers") or {}), list(payload.get("sources") or [])
