"""Bridges `MemoryStore.append`'s `ReinforcementIndex` protocol to a
`StateStore` (Phase 1/6).

Lives outside `memory.store`, which stays filesystem-only by design — it
has no state handle today and should not grow one, so the canonical-key
lookup happens here, in the callers (MCP tools, HTTP API) that already
hold `engine.state`, and is handed to `append()` as a small duck-typed
resolver.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


class StateReinforcementIndex:
    """Satisfies `memory.store.ReinforcementIndex` over a `StateStore`.

    Every method is best-effort and never raises: `find` returns `None` on
    any backend error (a cold or absent projection — fresh state dir, lost
    `/state` — degrades to "no match found", which is exactly today's
    behavior without Phase 1) and `reinforce` swallows its own failures,
    the same posture `record_memory_use` already takes for its counters. A
    missed dedup or a missed counter bump costs ranking signal, never a
    memory.
    """

    def __init__(self, state: Any):
        self.state = state

    def find(self, canon_digest: str, *, now: str) -> str | None:
        try:
            return self.state.find_canonical_record(canon_digest, now=now)
        except Exception:
            return None

    def foldable(self, record_id: str, *, now: str) -> str | None:
        try:
            return self.state.foldable_record(record_id, now=now)
        except Exception:
            # Same failure direction as `find`, but the safe answer is the
            # opposite one: a backend error here must not turn a
            # byte-identical re-write into a duplicate file, so fall back to
            # the record the caller already matched on disk.
            return record_id

    def reinforce(self, record_id: str, *, submitted_text: str, now: datetime) -> None:
        when = now.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        try:
            self.state.reinforce_memory_record(record_id, submitted_text, when)
        except Exception:
            pass
