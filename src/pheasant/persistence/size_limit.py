"""How much a knowledge base holds, against the limit its operator set.

``storage.knowledge_base_limit_gb`` is a ceiling on *indexed content*: the sum
of the sizes of the documents the region holds. That is the measure a person
adding content can reason about, it is exact, and it falls the moment a
document is removed -- which a database's size on disk does not (Postgres
keeps the space a deleted row occupied), so a limit on stored bytes could be
reached and then never left.

It is not ``storage.max_state_size_gb``, which bounds the graph's snapshot
history and evicts the oldest snapshot when exceeded. Nothing is evicted
here: a knowledge base at its limit keeps everything it has and takes no
more.

Unset (the default), nothing in this module reads a table.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

BYTES_PER_GB = 1024**3
#: The sync modes that can add content. ``validate_only`` reads and ``repair``
#: reconciles what is already held, so a full knowledge base still runs both.
GROWING_MODES = frozenset({"incremental", "full"})


def limit_gb(config: Any) -> float | None:
    """The configured ceiling in GB, or ``None`` when there is none."""

    value = getattr(getattr(config, "storage", None), "knowledge_base_limit_gb", None)
    try:
        number = float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def indexed_bytes(state: Any) -> int:
    """The size of every indexed document, summed. ``0`` when it cannot be read."""

    try:
        rows = state.rows("SELECT COALESCE(SUM(size_bytes), 0) FROM artifacts")
        return int(next(iter(dict(rows[0]).values())) or 0) if rows else 0
    except Exception:  # noqa: BLE001 - a fresh region has no tables yet
        return 0


def describe(config: Any, state: Any, *, indexed: int | None = None) -> dict[str, Any]:
    """The limit and how much of it is used, for the surfaces that show it."""

    limit = limit_gb(config)
    held = indexed_bytes(state) if indexed is None else int(indexed)
    return {
        "indexed_bytes": held,
        "limit_gb": limit,
        "limit_used": round(held / (limit * BYTES_PER_GB), 4) if limit else None,
        "full": bool(limit) and held >= limit * BYTES_PER_GB,
    }


def reached(config: Any, state: Any) -> dict[str, Any] | None:
    """What a refusal reports when the knowledge base is at its limit, else ``None``."""

    limit = limit_gb(config)
    if limit is None:
        return None
    held = indexed_bytes(state)
    if held < limit * BYTES_PER_GB:
        return None
    return {"reason": "knowledge base full", "indexed_bytes": held, "limit_gb": limit}


def blocks(config: Any, state: Any, mode: str, source_name: str = "") -> dict[str, Any] | None:
    """The details of a sync skipped because the knowledge base is full.

    The one check every sync passes: asked for, queued, scheduled, at startup
    or started by a save. A sync already running finishes what it began, so
    the limit can be passed by one sync's worth and never by more.
    """

    if mode not in GROWING_MODES:
        return None
    full = reached(config, state)
    if full is not None:
        logger.warning(
            "%s: sync skipped, the knowledge base holds %.2f GB of its %.2f GB limit",
            source_name or "source",
            full["indexed_bytes"] / BYTES_PER_GB,
            full["limit_gb"],
        )
    return full
