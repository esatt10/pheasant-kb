"""The limits that skip a sync which would add content: size and monthly spend.

The one check every sync passes (asked for, queued, scheduled, at startup or
started by a save), in ``SyncEngine._sync_source``. Each limit is a skip, not a
failure, so a queued task finishes instead of retrying, and a sync already
running finishes what it began. The limits themselves are
``persistence/size_limit.py`` and ``telemetry/spend.py``.
"""

from __future__ import annotations

from typing import Any

from pheasant.persistence import size_limit
from pheasant.telemetry import spend


def blocks(config: Any, state: Any, mode: str, source_name: str = "") -> dict[str, Any] | None:
    """Why a sync in ``mode`` is skipped now, or ``None`` to run it."""

    return size_limit.blocks(config, state, mode, source_name) or spend.blocks(
        config, mode, source_name
    )
