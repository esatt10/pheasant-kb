"""Which defaults a config is built on: pheasant-kb's, or standalone's.

pheasant's default deployment is pheasant-kb (`compose.yaml`): PostgreSQL,
NATS JetStream, a graph service, gRPC preparation workers, LanceDB, OpenAI
models, memory, the interaction ledger and the evaluation, tuning and
readiness planes. Its configuration is the code's default, so a config that
does not set a value gets pheasant-kb's: :data:`FLAGSHIP_DEFAULTS` is laid
under every config :meth:`PheasantConfig.model_validate` builds.

The values written into the dataclasses in ``schema.py`` are the other table,
**standalone**: SQLite, no queue, one process, offline stubs, nothing to stand
up and no key. A config selects it with ``pheasant.defaults: standalone``, or
the environment does with ``PHEASANT_DEFAULTS=standalone`` (the single
container, ``pip install``, stdio MCP and the offline test suite). A config's
own value wins over the environment, and the resolved table is recorded on
``pheasant.defaults``, so a dumped config reloads on the table it was built on.

This table is pinned to ``deploy/compose/pheasant-kb.yaml`` by
``tests/test_flagship_defaults.py``: an empty config built on it equals the
shipped pheasant-kb configuration, key for key.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

FLAGSHIP = "flagship"
STANDALONE = "standalone"
DEFAULTS_PROFILES = (FLAGSHIP, STANDALONE)
#: The environment variable that selects a table when a config names none.
DEFAULTS_ENV = "PHEASANT_DEFAULTS"

#: Set while something builds configs on a table it chose (the setup wizard,
#: answering from an answer file that names one).
_SELECTED: ContextVar[str | None] = ContextVar("pheasant_defaults", default=None)


def resolve_defaults(explicit: Any = None) -> str:
    """The table to build on: the config's own choice, then a selection in
    force, then ``PHEASANT_DEFAULTS``, then the flagship."""

    for candidate in (explicit, _SELECTED.get(), os.environ.get(DEFAULTS_ENV)):
        if candidate in (None, ""):
            continue
        value = str(candidate).strip().lower()
        if value not in DEFAULTS_PROFILES:
            raise ValueError(
                f"defaults must be one of {', '.join(DEFAULTS_PROFILES)}, not {candidate!r}"
                f" (set by pheasant.defaults or {DEFAULTS_ENV})"
            )
        return value
    return FLAGSHIP


@contextmanager
def using_defaults(profile: str | None) -> Iterator[None]:
    """Build configs on ``profile`` inside the block; ``None`` changes nothing."""

    token = _SELECTED.set(resolve_defaults(profile) if profile else _SELECTED.get())
    try:
        yield
    finally:
        _SELECTED.reset(token)


def with_table_defaults(data: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """``data`` ready to build, and the table it is built on.

    The hook ``PheasantConfig.model_validate`` calls first: the config's own
    ``pheasant.defaults`` wins, then a selection in force, then the
    environment, then the flagship. On the flagship ``data`` is laid over
    :data:`FLAGSHIP_DEFAULTS`; on standalone it is used as it is, because the
    standalone values are the dataclasses' own.
    """

    identity = data.get("pheasant") if isinstance(data.get("pheasant"), dict) else {}
    table = resolve_defaults(identity.get("defaults"))
    return (with_flagship_defaults(data) if table == FLAGSHIP else data), table


def default_config(table: str | None = None) -> Any:
    """The default configuration on ``table`` (or the one in force).

    ``PheasantConfig()`` cannot be it: a dataclass constructor only knows the
    values written on its fields, which are the standalone table.
    """

    from pheasant.config.schema import PheasantConfig

    with using_defaults(table):
        return PheasantConfig.model_validate({})


def default_config_data(table: str | None = None) -> dict[str, Any]:
    """:func:`default_config` as plain data, for a caller that will change it
    and build again (``pheasant up``, ``pheasant init``, the layered loader).

    The three storage paths ``model_validate`` derives from
    ``pheasant.state_path`` are left unset, as they are in a config nobody has
    built yet: dumped, they would pin ``/state/pheasant.db`` under a config
    whose ``state_path`` the caller is about to move, and every write would
    land in the old place.
    """

    data = default_config(table).model_dump(mode="json")
    for key in ("sqlite_path", "graph_path", "manifest_path"):
        data["storage"][key] = None
    return data


def with_flagship_defaults(data: dict[str, Any]) -> dict[str, Any]:
    """``data`` laid over :data:`FLAGSHIP_DEFAULTS`.

    Mappings merge key by key; anything else the config sets replaces the
    default whole, lists included, so ``sources: []`` means no sources and
    ``key: null`` means null.
    """

    return _merge(copy.deepcopy(FLAGSHIP_DEFAULTS), data)


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge(base[key], value)
        else:
            base[key] = value
    return base


#: pheasant-kb's configuration, as the values that differ from standalone.
#: Generated from deploy/compose/pheasant-kb.yaml; keep the two in step (the
#: test fails when they drift).
FLAGSHIP_DEFAULTS: dict[str, Any] = {
    "pheasant": {
        "name": "pheasant-kb",
        "description": "pheasant-kb: the flagship stack. PostgreSQL, NATS "
        "JetStream, a graph service, gRPC preparation workers, "
        "LanceDB, GPT-6 Luna, taxonomy and durable memory",
        "environment": "compose",
    },
    "server": {
        "mcp": {
            "max_request_body_size_mb": 1024,
            "transports": {"stdio": False, "streamable_http": True, "sse": False},
        },
        "api": {
            "max_concurrent_requests": 32,
            "drain_seconds": 20,
            "cors_origins": [
                "http://localhost:5173",
                "http://127.0.0.1:5173",
                "http://localhost:8765",
                "http://127.0.0.1:8765",
                "http://localhost:8080",
                "http://127.0.0.1:8080",
                "http://localhost:3000",
                "http://127.0.0.1:3000",
                "http://api:8765",
                "http://host.docker.internal:8765",
            ],
        },
    },
    "storage": {
        "backend": "postgres",
        "pool_size": 16,
        "graph_checkpoint_seconds": 120,
        "graph_checkpoint_max_seconds": 300,
        "max_state_size_gb": 250,
    },
    "search": {
        "max_results_default": 100,
        "embeddings": {
            "enabled": True,
            "dimensions": 1536,
            "batch_size": 128,
            "query_timeout_seconds": 1.0,
            "query_max_retries": 0,
            "query_rate_limit_max_wait_seconds": 0.0,
            "max_retries": 10,
            "retry_backoff_seconds": 2.0,
            "rate_limit_max_wait_seconds": 900.0,
        },
        "vector_store": {"path": "/state/vectors", "resident": True, "ann_index": "ivf_hnsw_sq"},
        "wasm_relationship_search": True,
    },
    "ingestion": {
        "captioner": {"provider": "openai-spec", "model": "gpt-6-luna"},
        "transcriber": {"provider": "openai-spec"},
        "extractor": {"html_text": True},
        "landing_service_url": "http://indexer:8765",
    },
    "sync": {
        "watcher": {"max_watch_paths": 20000, "debounce_ms": 250, "batch_window_ms": 1000},
        "limits": {"max_files": None, "max_total_mb": None},
        "source_processing": {
            "chunk_max_chars": 2000,
            "taxonomy_enabled": True,
            "chunk_strategy": "auto",
        },
        "concurrency": {
            "max_parallel_files": 16,
            "max_parallel_embeddings": 8,
            "file_executor": "remote",
            "remote_worker_urls": ["grpc://worker:8766"],
            "remote_worker_enabled": True,
            "remote_worker_batch_size": 16,
            "remote_worker_max_inflight_batches": 8,
            "worker_transport": "grpc",
            "lock_timeout_seconds": 300,
        },
        "queue": {
            "enabled": True,
            "backend": "nats",
            "max_attempts": 5,
            "nats_servers": ["nats://nats:4222"],
        },
    },
    "graph": {
        "wasm_cross_source_resolution": True,
        "query_service_url": "http://graph:8765",
        "max_nodes": None,
    },
    "security": {
        "allow_workspace_roots": ["/workspace", "/memory", "/state", "/exports", "/data"],
        "allow_user_selected_source_paths": False,
        "acl_enforced": True,
    },
    "memory": {
        "default_policy": "prefer",
        "steering_enabled": True,
        "usage_tracking": True,
        "about_max_targets": 12,
    },
    "assistant": {
        "provider": "openai",
        "model": "gpt-6-luna",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "allow_session_keys": False,
        "max_context_chunks": 16,
        "max_output_tokens": 16384,
        "request_timeout_seconds": 180.0,
        "max_facts": 16,
        "workflow": "agentic",
        "workflow_options": {
            "agentic": {
                "staged_retrieval": True,
                "combine_grade_and_answer": True,
                "defer_graph_until_insufficient": True,
                "output_tokens_by_depth": {"short": 512, "medium": 1024},
                "answer_max_words": {"short": 150, "medium": 400},
            }
        },
        "latency": {"max_concurrent_answers": 4},
        "retrieval": {
            "max_rounds": 2,
            "per_query_results": 10,
            "max_context_passages": 16,
            "retrieval_modes": ["hybrid", "graph", "vector"],
            "grader_model": "gpt-6-luna",
            "max_facts": 16,
        },
    },
    "evaluation": {"enabled": True},
    "tuning": {"enabled": True},
    "observability": {
        "interactions": {"enabled": True, "queue": {"enabled": True, "backend": "nats"}},
        "otlp_endpoint": "",
    },
    "readiness": {"enabled": True, "corpus_denylist": ["benchmark/*", "*.answers.json"]},
    "sources": [
        {
            "name": "agent-memory",
            "type": "memory",
            "path": "/memory",
            "description": "Durable agent memory",
            "include": ["**/*.md"],
            "exclude": ["**/*.archived"],
            "sync": {
                "on_startup": True,
                "on_file_change": "debounce",
                "on_git_commit": False,
                "interval_seconds": 60,
            },
        },
        {
            "name": "uploads",
            "type": "document_folder",
            "path": "/state/uploads/uploads",
            "enabled": True,
            "include": ["**/*"],
            "sync": {
                "on_startup": True,
                "on_file_change": "debounce",
                "on_git_commit": True,
                "interval_seconds": None,
            },
        },
    ],
}


def defaults_table() -> str:
    """The Markdown table in ``docs/configuration.md``: every key whose
    pheasant-kb default differs from its standalone one. Generated, and held
    to the code by ``tests/test_flagship_defaults.py``."""

    import json

    from pheasant.config.schema import PheasantConfig

    standalone = PheasantConfig().model_dump(mode="json")

    def leaves(data: dict[str, Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
        for key, value in data.items():
            dotted = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict) and value and not dotted.endswith("transports"):
                yield from leaves(value, dotted)
            else:
                yield dotted, value

    def lookup(dotted: str) -> Any:
        cursor: Any = standalone
        for part in dotted.split("."):
            cursor = cursor.get(part) if isinstance(cursor, dict) else None
        return cursor

    def cell(value: Any) -> str:
        if value is None:
            return "unset"
        text = json.dumps(value) if not isinstance(value, str) else value
        return f"`{text}`".replace("|", "\\|")

    rows = ["| Key | pheasant-kb (default) | standalone |", "|---|---|---|"]
    for dotted, value in leaves({k: v for k, v in FLAGSHIP_DEFAULTS.items() if k != "sources"}):
        rows.append(f"| `{dotted}` | {cell(value)} | {cell(lookup(dotted))} |")
    names = ", ".join(f"`{source['name']}`" for source in FLAGSHIP_DEFAULTS["sources"])
    rows.append(f"| `sources` | {names} | none |")
    return "\n".join(rows) + "\n"
