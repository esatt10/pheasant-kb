"""The graph evidence an answer carries beside its passages, timed.

Facts and figures are read after retrieval, from the cited nodes, and each is
a graph round trip that can take as long as a search. A step whose duration
includes them owes a chip for each, or its chips do not add up to its total
and the time looks like it went nowhere. Both workflows call this one function.
"""

from __future__ import annotations

import time
from typing import Any


def gather(
    retriever: Any, citations: list[dict], max_facts: int | str
) -> tuple[list[dict], list[dict], list[dict[str, Any]]]:
    """Return ``(facts, figures, timings)`` for ``citations``.

    ``timings`` are fanout chips (``phase`` ``facts`` / ``figures``, mode
    ``graph``), one for each lookup that ran.
    """

    timings: list[dict[str, Any]] = []
    node_ids = [c["node_id"] for c in citations if c.get("node_id")]
    started = time.perf_counter()
    facts = retriever.facts(node_ids, int(max_facts))
    if node_ids:
        timings.append(_chip("facts", time.perf_counter() - started, len(facts)))
    collect = getattr(retriever, "figures", None)
    figures: list[dict] = []
    if callable(collect) and citations:
        started = time.perf_counter()
        figures = collect(citations)
        timings.append(_chip("figures", time.perf_counter() - started, len(figures)))
    return facts, figures, timings


def _chip(phase: str, seconds: float, items: int) -> dict[str, Any]:
    return {"mode": "graph", "phase": phase, "duration_seconds": seconds, "items": items}
