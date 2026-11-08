"""Whether the region is awake: what is still starting after a wake, and for how long.

A region that scales to zero wakes on its first request, and the parts wake
together but not equally fast: the api answers first (behind
`deployment/wake_gate.py` while the database starts), the database accepts
connections and then reloads its cache in the background (pg_prewarm), and
the graph service joins when it has started. Until now each of those was a
generic failure -- "graph query service unreachable", a bare timeout -- that
read like an outage to a person and gave an agent nothing to wait for.

One operation behind ``GET /region/status``, MCP ``get_region_status`` and
the web UI's banner. It reports each part as ``ready``, ``waking`` (not
answering yet, soon after this process started), ``warming`` (answering, but
cold: first answers may be slower) or ``unavailable`` (not answering long
after a wake should have finished), and the region's ``status`` is the worst
of them. Ready means there is nothing to show.
"""

from __future__ import annotations

import time
from typing import Any

from pheasant.services import ServiceContext

#: A part that does not answer this soon after the process started is waking,
#: because the whole region scales up together; later it is unavailable.
WAKE_WINDOW_SECONDS = 300.0

#: How long a wake from zero usually takes, end to end. Measured on the Azure
#: fleet: ~80 s from the first request to the UI, the database's image pull
#: the tail of it. Said as "usually", never as a promise.
TYPICAL_WAKE_SECONDS = 90

#: Each check gets this long; the status itself must answer while parts sleep.
CHECK_SECONDS = 2.0

#: When this process started, as near as a module can tell: it is imported
#: when the app is built.
PROCESS_STARTED = time.monotonic()

_ORDER = {"ready": 0, "warming": 1, "waking": 2, "unavailable": 3}


def region_status(context: ServiceContext) -> dict[str, Any]:
    """Every part's state, the region's, and one sentence saying what to expect."""

    elapsed = time.monotonic() - PROCESS_STARTED
    components = [_database(context, elapsed)]
    graph = context.graph
    if getattr(graph, "is_remote_graph", False):
        components.append(_graph_service(graph, elapsed))
    status = max((part["state"] for part in components), key=_ORDER.__getitem__)
    return {
        "status": status,
        "components": components,
        "elapsed_seconds": round(elapsed, 1),
        "typical_seconds": TYPICAL_WAKE_SECONDS,
        "message": _message(status, components),
        "served_by": "api",
    }


def _database(context: ServiceContext, elapsed: float) -> dict[str, Any]:
    from pheasant.request_budget import activate

    part: dict[str, Any] = {"name": "database", "label": "Database"}
    try:
        with activate(time.monotonic() + CHECK_SECONDS):
            context.state.rows("SELECT 1 AS ok", ())
    except Exception as exc:  # noqa: BLE001 - any failure is "not answering"
        return {**part, **_not_answering(elapsed, f"not accepting connections yet ({exc})")}
    if not getattr(getattr(context.state, "dialect", None), "is_postgres", False):
        return {**part, "state": "ready", "detail": "answering"}
    try:
        with activate(time.monotonic() + CHECK_SECONDS):
            row = context.state.rows(
                "SELECT EXTRACT(EPOCH FROM now() - pg_postmaster_start_time()) AS up,"
                " (SELECT count(*) FROM pg_stat_activity"
                "  WHERE backend_type = 'autoprewarm worker') AS prewarming",
                (),
            )[0]
    except Exception:  # noqa: BLE001 - the cache's state is a nicety, not a check
        return {**part, "state": "ready", "detail": "answering"}
    up = float(row["up"] or 0)
    part["up_seconds"] = round(up, 1)
    if int(row["prewarming"] or 0) > 0:
        return {
            **part,
            "state": "warming",
            "detail": f"woke {_duration(up)} ago and is reloading its cache; "
            "the first answers may be slower",
        }
    return {**part, "state": "ready", "detail": "answering"}


def _graph_service(graph: Any, elapsed: float) -> dict[str, Any]:
    from pheasant.request_budget import activate

    part: dict[str, Any] = {"name": "graph_service", "label": "Graph service"}
    try:
        with activate(time.monotonic() + CHECK_SECONDS):
            graph.ping()
    except Exception:  # noqa: BLE001 - any failure is "not answering"
        return {**part, **_not_answering(elapsed, "not answering yet")}
    return {**part, "state": "ready", "detail": "answering"}


def _not_answering(elapsed: float, detail: str) -> dict[str, Any]:
    if elapsed <= WAKE_WINDOW_SECONDS:
        return {"state": "waking", "detail": f"starting: {detail}"}
    return {"state": "unavailable", "detail": detail}


def _message(status: str, components: list[dict[str, Any]]) -> str:
    if status == "ready":
        return "Everything is awake."
    pending = [part for part in components if part["state"] == status]
    names = ", ".join(part["label"].lower() for part in pending)
    if status == "waking":
        return (
            f"Waking up after being idle: the {names} {'is' if len(pending) == 1 else 'are'} "
            f"still starting (usually about {TYPICAL_WAKE_SECONDS} s in all). "
            "Requests that need it may fail until then; retry shortly."
        )
    if status == "warming":
        return f"Awake. The {names} is still warming up, so the first answers may be slower."
    return (
        f"The {names} {'is' if len(pending) == 1 else 'are'} not answering, "
        "long after a wake should have finished."
    )


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds} s" if seconds < 90 else f"{seconds // 60} min {seconds % 60} s"
