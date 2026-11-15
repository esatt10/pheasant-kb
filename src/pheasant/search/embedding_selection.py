"""Which embedding model a region indexes and searches with, for every process.

``models.embeddings`` lists what an operator provisioned; exactly one is in
force at a time, because a query vector is only comparable with passage
vectors from the same model. The choice is one ``region_settings`` row
(section ``models``), laid over ``models.default_embedding``:

* a process applies it to ``config.search.embeddings`` when its engine is
  built (:func:`apply_active`), so everything downstream -- the embedder, the
  source fingerprint, the reconcile on startup -- reads one answer;
* a long-lived process re-reads the row at most every :data:`FOLLOW_SECONDS`
  (:func:`follow`) and rebuilds its vector stack when it moved;
* before a sync writes a vector, the indexer checks that the store is in the
  space the config now names (:func:`ensure_space`) and, when it is not,
  re-embeds every stored chunk under a lease so two sync children cannot both
  do it. Chunks are already in the state store: no source is re-read.

With no catalog (``models.embeddings`` empty) every function here returns at
once and ``search.embeddings`` is used as written: the standalone path, and
every region that predates this, are unchanged.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

SECTION = "models"
#: The lease the re-embed is held under (`sync/locks.SourceLease`).
EMBEDDING_LEASE = "__embeddings__"
#: How often a running process re-reads the selection.
FOLLOW_SECONDS = 30.0
#: How long a sync waits for another process's re-embed before going ahead
#: without vectors being reconciled by it (the holder is still doing it).
LEASE_WAIT_SECONDS = 6 * 3600.0

_FIELDS = ("provider", "model", "base_url", "api_key_env", "dimensions")
_FOLLOWED: dict[tuple[int, str], float] = {}
_LOCK = threading.Lock()


def catalog(config: Any) -> list[Any]:
    return list(getattr(getattr(config, "models", None), "embeddings", None) or [])


def stored_id(state: Any, kb_id: str) -> str | None:
    """The embedding an admin selected, or ``None`` (the configured default)."""

    try:
        rows = state.rows(
            "SELECT data_json FROM region_settings WHERE kb_id=? AND section=?", (kb_id, SECTION)
        )
    except Exception:  # noqa: BLE001 - a state store with no schema yet has no selection
        return None
    if not rows:
        return None
    value = json.loads(rows[0]["data_json"] or "{}").get("embedding")
    return str(value) if value else None


def active_id(config: Any, state: Any) -> str | None:
    """The catalog entry in force: the stored selection, else the default."""

    ids = [str(entry.id) for entry in catalog(config)]
    if not ids:
        return None
    chosen = stored_id(state, config.knowledge_base_id)
    if chosen in ids:
        return chosen
    default = getattr(config.models, "default_embedding", None)
    return default if default in ids else None


def select(state: Any, kb_id: str, embedding_id: str, actor: str | None) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    state.execute(
        "INSERT INTO region_settings(kb_id, section, data_json, updated_at, updated_by) "
        "VALUES(?,?,?,?,?) ON CONFLICT(kb_id, section) DO UPDATE SET "
        "data_json=excluded.data_json, updated_at=excluded.updated_at, "
        "updated_by=excluded.updated_by",
        (kb_id, SECTION, json.dumps({"embedding": embedding_id}), now, actor),
    )


def apply_active(config: Any, state: Any) -> str | None:
    """Point ``config.search.embeddings`` at the entry in force; its id."""

    chosen = active_id(config, state)
    if chosen is None:
        return None
    entry = next(e for e in catalog(config) if str(e.id) == chosen)
    settings = config.search.embeddings
    settings.enabled = True
    for name in _FIELDS:
        value = getattr(entry, name)
        if value is not None or name == "dimensions":
            setattr(settings, name, value)
    return chosen


def in_force(config: Any, embedder: Any) -> bool:
    """Whether a live embedder is the one ``config.search.embeddings`` now names."""

    settings = config.search.embeddings
    if embedder is None:
        return not settings.enabled
    if getattr(embedder, "model", None) != settings.model:
        return False
    width = getattr(embedder, "dimensions", None) or getattr(embedder, "dim", None)
    return settings.dimensions is None or width is None or int(width) == int(settings.dimensions)


def follow(config: Any, state: Any, embedder: Any, reload: Any, *, key: str = "") -> bool:
    """Re-read the selection (rate-limited per ``key``); ``reload()`` when the
    stack ``embedder()`` belongs to is not on it.

    Stateless on purpose: it compares what is *running* with what is
    selected, so it does not matter which part of a process noticed first --
    the indexer and the searcher of one process each ask for themselves.
    """

    if not catalog(config):
        return False
    now = time.monotonic()
    slot = (id(config), key)
    with _LOCK:
        if now - _FOLLOWED.get(slot, -FOLLOW_SECONDS) < FOLLOW_SECONDS:
            return False
        _FOLLOWED[slot] = now
    chosen = apply_active(config, state)
    if chosen is None or in_force(config, embedder()):
        return False
    logger.info("Embedding model is now %s; rebuilding the %s vector stack", chosen, key)
    reload()
    return True


def forget(config: Any) -> None:
    """Make every :func:`follow` on this config read at once (after a switch here)."""

    with _LOCK:
        for slot in [slot for slot in _FOLLOWED if slot[0] == id(config)]:
            del _FOLLOWED[slot]


def ensure_space(engine: Any) -> dict | None:
    """Before a sync embeds: bring the vector store into the configured space.

    A no-op read of one fingerprint row when nothing changed. When the store
    was built with another model, every stored chunk is re-embedded here,
    under :data:`EMBEDDING_LEASE`; a second process that arrives meanwhile
    waits for the first rather than repeating it.
    """

    config = engine.config
    if not catalog(config):
        return None
    follow(
        config,
        engine.state,
        lambda: getattr(getattr(engine, "vectors", None), "embedder", None),
        lambda: _rebuild_indexer(engine),
        key="indexing",
    )
    if getattr(engine, "vectors", None) is None:
        return None
    from pheasant.sync.fingerprint import EMBEDDING_SCOPE, embedding_fingerprint

    current = embedding_fingerprint(config)
    if engine.state.get_fingerprint(EMBEDDING_SCOPE) in {None, current}:
        return None
    from pheasant.sync.locks import SourceLease, SourceLeaseError

    lease = SourceLease(engine.state, EMBEDDING_LEASE)
    try:
        lease.acquire(wait_timeout_s=LEASE_WAIT_SECONDS)
    except SourceLeaseError:
        logger.warning("Another process is still re-embedding; continuing without it")
        return None
    try:
        if engine.state.get_fingerprint(EMBEDDING_SCOPE) == current:
            return None  # the holder we waited for did it
        return engine.reconcile_embeddings()
    finally:
        lease.release()


def _rebuild_indexer(engine: Any) -> None:
    from pheasant.search.vector_store import vector_indexer_from_config

    engine.vectors = vector_indexer_from_config(engine.config, background=True)


def status(config: Any, state: Any) -> dict[str, Any]:
    """Whether the vectors on disk are in the selected model's space."""

    from pheasant.sync.fingerprint import EMBEDDING_SCOPE, embedding_fingerprint

    recorded = state.get_fingerprint(EMBEDDING_SCOPE)
    current = embedding_fingerprint(config)
    if recorded == current:
        state_name = "ready"
    elif recorded is None:
        state_name = "not_indexed"
    else:
        state_name = "reindex_pending"
    return {"state": state_name, "ready": recorded == current}
