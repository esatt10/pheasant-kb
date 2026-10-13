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

from dataclasses import dataclass
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


def section_needle(section: str | None) -> str:
    """The comparable form of a requested section, or ``""`` for no filter.

    Lowercased and stripped, and that is the whole rule — the SQL above matches
    ``LOWER(heading_path) LIKE '%needle%'`` and :func:`section_matches` does
    ``needle in heading_path.lower()``, which are the same test. Keeping the
    normalization in one function is what stops the two from drifting apart.
    """
    return (section or "").strip().lower()


def section_matches(heading_path: object, section: str | None) -> bool:
    """Does a hit's breadcrumb sit inside the requested section?

    Substring, not equality, because people cite the section and not the whole
    path: ``§ 12.3``, ``Article IV`` and ``Termination`` should each reach
    ``MASTER SERVICES AGREEMENT > Article IV Term and Termination > § 12.3``.
    Matching the breadcrumb rather than only its last crumb is what makes
    asking for an Article return everything nested under it.
    """
    needle = section_needle(section)
    if not needle:
        return True
    return needle in str(heading_path or "").lower()


def path_scope(path: Any) -> str:
    """A file or folder inside a source, normalized: ``"/guide/setup/"`` → ``"guide/setup"``.

    ``""`` means no scope. ``.`` and ``..`` segments are dropped rather than
    followed, so a scope can only ever narrow, never climb out of a source.
    """

    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p and p not in {".", ".."}]
    return "/".join(parts)


def path_within(relative_path: Any, scope: Any) -> bool:
    """Is ``relative_path`` the scoped file, or anything under the scoped folder?

    Exact and case-sensitive, as the SQL form is (`path_scope_sql`): the
    scope is a path the index itself listed, so it is spelled as indexed, and
    a case-blind match here would agree with SQLite's ``LIKE`` and disagree
    with Postgres's.
    """

    wanted = path_scope(scope)
    if not wanted:
        return True
    have = str(relative_path or "").replace("\\", "/").strip("/")
    return have == wanted or have.startswith(f"{wanted}/")


@dataclass(frozen=True)
class Exclusion:
    """Documents left out of a search, by identity — what ``services.scope``
    resolved from the names a caller gave.

    Carried two ways because the arms identify a hit two ways: the text arm
    filters ``artifacts.id`` in SQL, and a vector or graph hit (a chunk, a
    symbol) is matched on the ``(source, relative_path)`` of its document.
    Empty is falsy, so "nothing excluded" costs nothing anywhere.
    """

    ids: frozenset[str] = frozenset()
    pairs: frozenset[tuple[str, str]] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.ids or self.pairs)

    def admits(self, hit: dict) -> bool:
        if str(hit.get("node_id") or "") in self.ids:
            return False
        return (source_of(hit), path_of(hit)) not in self.pairs


def path_scope_sql(
    scope: Any, exclude: Exclusion | None = None, column: str = "artifacts.relative_path"
) -> tuple[str, list[Any]]:
    """``(" AND …", params)`` keeping rows within ``scope`` and not excluded.

    ``substr(...) = ?`` rather than ``LIKE 'x/%'``: SQLite's ``LIKE`` folds
    ASCII case and Postgres's does not, so the obvious spelling would scope
    the two backends differently. Both dialects spell ``substr`` alike.
    """

    where, params = "", []
    wanted = path_scope(scope)
    if wanted:
        where = f" AND ({column} = ? OR substr({column}, 1, ?) = ?)"
        params = [wanted, len(wanted) + 1, f"{wanted}/"]
    if exclude and exclude.ids:
        ids = sorted(exclude.ids)
        table = column.rsplit(".", 1)[0] if "." in column else "artifacts"
        where += f" AND {table}.id NOT IN ({', '.join('?' for _ in ids)})"
        params.extend(ids)
    return where, params


def path_of(item: dict) -> str:
    """The relative path a search hit came from, whichever arm produced it."""

    provenance = item.get("provenance")
    if isinstance(provenance, dict) and provenance.get("relative_path"):
        return str(provenance["relative_path"])
    return str(item.get("relative_path") or "")


def scope_hits(
    scope: Any, *arms: list[dict], exclude: Exclusion | None = None
) -> tuple[list[dict], ...]:
    """Each arm's hits within ``scope`` and not excluded, for the arms SQL cannot narrow.

    The vector arm's hits carry their path. A graph node outside any file (an
    entity, a stub) carries none and cannot be "in" a folder, so it goes —
    the call ``section`` makes for a node with no breadcrumb.
    """

    return tuple(
        [
            hit
            for hit in arm
            if path_within(path_of(hit), scope) and (not exclude or exclude.admits(hit))
        ]
        for arm in arms
    )


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
