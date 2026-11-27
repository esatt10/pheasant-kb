"""Budgets on every dimension a run can spend, with room kept for finishing.

Usage is never a running total someone increments: it is the sum of what each
attempt reserved or spent, read from ``harness_attempts``. An attempt whose
result was discarded (late, stale, duplicate) still spent what it spent, and a
reservation is released by the same row that settles it, so there is nothing
to race.
"""

from __future__ import annotations

import json
from typing import Any

DIMENSIONS = (
    "wall_seconds",
    "model_calls",
    "model_tokens",
    "tool_calls",
    "bytes_scanned",
    "sandbox_seconds",
    "output_bytes",
    "spend_usd",
)

DEFAULT_LIMITS = {
    "wall_seconds": 900.0,
    "model_calls": 40,
    "model_tokens": 400_000,
    "tool_calls": 400,
    "bytes_scanned": 512 * 1024 * 1024,
    "sandbox_seconds": 600.0,
    "output_bytes": 8 * 1024 * 1024,
    "spend_usd": 5.0,
}

#: Kept back from exploration so a run that spent everything reading can still
#: verify and deliver what it has (§4: reserve room before exploring).
FINISHING_RESERVE = {"tool_calls": 12, "sandbox_seconds": 90.0, "output_bytes": 512 * 1024}

#: What one invocation of each tool is reserved at before it runs.
COSTS: dict[str, dict[str, float]] = {
    "resolve": {"tool_calls": 1},
    "discover": {"tool_calls": 1},
    "enumerate": {"tool_calls": 1},
    "grep": {"tool_calls": 1, "bytes_scanned": 4 * 1024 * 1024},
    "read": {"tool_calls": 1, "bytes_scanned": 128 * 1024},
    "inspect": {"tool_calls": 1, "bytes_scanned": 2 * 1024 * 1024},
    "follow": {"tool_calls": 1, "bytes_scanned": 4 * 1024 * 1024},
    "compare": {"tool_calls": 1},
    "materialize": {"tool_calls": 1, "sandbox_seconds": 2},
    "apply_patch": {"tool_calls": 1},
    "execute": {"tool_calls": 1, "sandbox_seconds": 30},
    "draft": {"tool_calls": 1, "output_bytes": 64 * 1024},
    "render": {"tool_calls": 1, "output_bytes": 64 * 1024},
    "plan": {"model_calls": 1, "model_tokens": 6000},
    "verify": {"tool_calls": 1, "sandbox_seconds": 30},
    "deliver": {"tool_calls": 1, "output_bytes": 64 * 1024},
}

FINISHING = {"verify", "deliver"}


def limits(requested: dict | None) -> dict[str, float]:
    merged = dict(DEFAULT_LIMITS)
    for key, value in (requested or {}).items():
        if key in merged and value is not None:
            merged[key] = value
    return merged


def usage(state: Any, run_id: str) -> dict[str, dict[str, float]]:
    rows = state.rows(
        "SELECT status, reserved_json, spent_json FROM harness_attempts WHERE run_id=?", (run_id,)
    )
    spent = dict.fromkeys(DIMENSIONS, 0.0)
    reserved = dict.fromkeys(DIMENSIONS, 0.0)
    for row in rows:
        if row["spent_json"]:
            for key, value in json.loads(row["spent_json"]).items():
                spent[key] = spent.get(key, 0.0) + float(value)
        elif row["status"] == "dispatched":
            for key, value in json.loads(row["reserved_json"]).items():
                reserved[key] = reserved.get(key, 0.0) + float(value)
    return {"spent": spent, "reserved": reserved}


def admit(
    limit: dict, current: dict, action: str, cost: dict | None = None
) -> tuple[bool, str | None]:
    """Whether ``action`` may reserve ``cost`` now. Exploration leaves the reserve alone."""

    need = cost or COSTS.get(action, {"tool_calls": 1})
    hold_back = {} if action in FINISHING else FINISHING_RESERVE
    for key, amount in need.items():
        used = current["spent"].get(key, 0) + current["reserved"].get(key, 0)
        ceiling = limit.get(key, float("inf")) - hold_back.get(key, 0)
        if used + amount > ceiling:
            return False, f"budget:{key}"
    return True, None


def remaining(limit: dict, current: dict) -> dict[str, float]:
    return {
        key: round(limit[key] - current["spent"].get(key, 0) - current["reserved"].get(key, 0), 3)
        for key in DIMENSIONS
    }
