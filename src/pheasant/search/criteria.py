"""Per-call retrieval criteria, shared by every protocol (Step 33.6).

These filters lived inside `mcp_server/tools.py`, which meant an MCP caller
could scope a search away from a noisy source, floor the score or keep only
certain node types — and an HTTP caller could not, because `POST /search` went
straight to the searcher and never saw them. Same region, same index, two
different retrieval surfaces depending on which protocol you happened to hold.

Moving them here is what lets `/search` and `search_context` call one
implementation. `mcp_server.tools` re-exports `apply_retrieval_criteria` so
existing importers keep working.
"""

from __future__ import annotations

from typing import Any


def apply_retrieval_criteria(
    results: list[dict],
    *,
    exclude_sources: list[str] | None = None,
    node_types: list[str] | None = None,
    min_score: float | None = None,
    source_types: list[str] | None = None,
    exclude_source_types: list[str] | None = None,
) -> list[dict]:
    """Filter search hits by caller-supplied criteria. Order is preserved.

    Ranking is not recomputed: these are *post-filters* over an already-ranked
    list, so a criterion can only ever remove rows. Re-scoring here would make
    the same query answer differently depending on which filters were passed,
    which is exactly the surprise a retrieval-tuning surface must not have.
    """
    excluded = {str(name) for name in (exclude_sources or [])}
    wanted_types = {str(name) for name in (node_types or [])}
    # Source *type* (repository, gdrive, web_collection, ...) is a different axis from
    # node type (chunk, symbol, entity) and from source *name*. Scoping by name
    # needs you to know every source in the region; scoping by type is the
    # question an agent actually has — "only what came out of our wikis".
    wanted_source_types = {str(name) for name in (source_types or [])}
    excluded_source_types = {str(name) for name in (exclude_source_types or [])}
    kept: list[dict] = []
    for item in results:
        if excluded and source_of(item) in excluded:
            continue
        if wanted_types and str(item.get("type") or "") not in wanted_types:
            continue
        if wanted_source_types or excluded_source_types:
            source_type = type_of(item)
            if wanted_source_types and source_type not in wanted_source_types:
                continue
            if source_type and source_type in excluded_source_types:
                continue
        if min_score is not None:
            try:
                if float(item.get("score") or 0.0) < min_score:
                    continue
            except (TypeError, ValueError):
                continue
        kept.append(item)
    return kept


def source_of(item: dict) -> str:
    """The source a search hit came from.

    Hits do **not** carry a top-level ``source_id`` — it lives under
    ``provenance``, alongside the path. Reading the wrong key here meant
    ``exclude_sources`` matched nothing and silently returned unfiltered
    results, which is worse than refusing the filter outright. Both shapes are
    accepted so a caller passing already-flattened rows still works.
    """
    provenance = item.get("provenance")
    if isinstance(provenance, dict) and provenance.get("source_id"):
        return str(provenance["source_id"])
    return str(item.get("source_id") or item.get("source") or "")


def criteria_active(
    exclude_sources: list[str] | None,
    node_types: list[str] | None,
    min_score: float | None,
    source_types: list[str] | None = None,
    exclude_source_types: list[str] | None = None,
) -> bool:
    """Whether any post-filter will run, and therefore whether to over-fetch.

    Over-fetching matters: without it `max_results` quietly degrades from "give
    me this many" to "look at this many and return whatever survives".
    """
    return bool(
        exclude_sources
        or node_types
        or min_score is not None
        or source_types
        or exclude_source_types
    )


def criteria_dict(
    source_name: str | None,
    exclude_sources: list[str] | None,
    node_types: list[str] | None,
    min_score: float | None,
    memory: Any = None,
    source_types: list[str] | None = None,
    exclude_source_types: list[str] | None = None,
) -> dict[str, Any]:
    """The echo of what was applied, reported back on a filtered payload."""
    return {
        "source_name": source_name,
        "exclude_sources": exclude_sources,
        "node_types": node_types,
        "min_score": min_score,
        "memory": memory,
        "source_types": source_types,
        "exclude_source_types": exclude_source_types,
    }


def source_type_map(state: Any) -> dict[str, str]:
    """``{source name: configured type}`` for every registered source.

    One query per search rather than a join per arm: the vector and graph arms
    do not go through SQL at all, so a join would only ever have annotated the
    text arm — and a filter that silently applies to one third of a hybrid
    result set is worse than no filter. Failures return ``{}``, which makes
    the type simply absent rather than failing the search.
    """

    if state is None:
        return {}
    try:
        rows = state.rows("SELECT name, type FROM sources")
    except Exception:  # pragma: no cover - a store without the table yet
        return {}
    return {str(row["name"]): str(row["type"]) for row in rows if row["name"]}


def stamp_source_types(results: list[dict], types: dict[str, str]) -> None:
    """Record each hit's *source type* alongside its source id, in place.

    Provenance carried ``source_id`` — which source — but never what **kind**
    of source that was, so no caller could tell a hit from a git repository
    from one out of Slack without separately fetching the source list and
    joining by hand. Every modality reads provenance, so writing it here
    surfaces the type on MCP, HTTP, the UI and the assistant at once.
    """

    if not types:
        return
    for item in results:
        provenance = item.get("provenance")
        if not isinstance(provenance, dict):
            continue
        source_type = types.get(str(provenance.get("source_id") or ""))
        if source_type:
            provenance["source_type"] = source_type


def type_of(item: dict) -> str:
    """The configured type of the source a hit came from.

    Reads the same ``provenance`` block :func:`source_of` does, and tolerates
    an already-flattened row for the same reason.
    """

    provenance = item.get("provenance")
    if isinstance(provenance, dict) and provenance.get("source_type"):
        return str(provenance["source_type"])
    return str(item.get("source_type") or "")
