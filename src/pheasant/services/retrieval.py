"""Retrieval, once — the operation that had already diverged furthest.

`search` and `relevant_files` existed on both surfaces. Each carried its own
over-fetch arithmetic (fixed separately: see `RankingParameters.overfetch`),
its own criteria assembly, and its own idea of which optional behaviours
applied. What that produced, measured before this module existed:

* **`relevant_files` ignored the memory policy over MCP.** The HTTP route
  passed ``memory=``; the tool did not. So an agent — the consumer memory
  exists for — could be handed a record the region *knew* had been superseded,
  which is precisely the stale-fact failure the memory plane was built to
  prevent.
* **`relevant_files` did not deduplicate over MCP.** HTTP returned one entry
  per file; the tool returned one per chunk, so an agent asking for eight
  files could receive eight chunks of two.
* **`relevant_files` ignored `section` over MCP.**
* **Search metrics were HTTP-only.** ``pheasant_search_total`` and
  ``pheasant_search_duration_seconds`` were incremented in the HTTP route, so
  every search an agent ran through MCP was invisible to the counters an
  operator sizes the region with — and to the capacity model that reads them.

None of those was a decision. Each is a line that landed on the surface whose
bug report arrived first.

Observation stays in the adapters on purpose: the ledger event carries the
HTTP request or the MCP session that opened it, which is transport context by
definition. What the adapters record — the query, the payload, the criteria —
is what this module returns, so the *content* is still decided once.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from typing import Any

from pheasant.search.criteria import apply_retrieval_criteria, criteria_active, criteria_dict
from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest
from pheasant.telemetry import metrics

#: Bounds on a search's graph expansion. Every hit's neighbourhood is a walk,
#: so these are what keep one search from becoming a whole-graph read: at most
#: ``MAX_EXPANSION_SEEDS`` distinct hits are expanded, each to at most
#: ``MAX_EXPANSION_NEIGHBORS`` nodes no more than ``MAX_EXPANSION_DEPTH`` hops
#: away. A caller wanting more walks from a hit with `get_graph_neighbors`.
MAX_EXPANSION_DEPTH = 3
MAX_EXPANSION_NEIGHBORS = 50
MAX_EXPANSION_SEEDS = 25

#: Edges an expansion skips unless the caller names its own. ``has_chunk``
#: leads from a file to its own passages — the hit already is one — and comes
#: first in a file's out-edges, so left in it spends the whole budget
#: restating the document. ``indexes`` is the source -> every-artifact
#: shortcut the canvas leaves out for the same reason.
DEFAULT_EXPANSION_EXCLUDES = ("has_chunk", "indexes")


@dataclass(frozen=True)
class GraphExpansion:
    """Walk the graph out from each hit and return the neighbourhood with it.

    This is the half of retrieval an agent cannot do from passages alone:
    *what is this connected to* — the file a symbol is defined in, the module
    an import resolves to, the record that superseded this one. It runs no
    model and makes no judgement about which neighbours matter, so a harness
    doing its own evaluation gets the structure and keeps the decision.
    """

    depth: int = 1
    max_neighbors: int = 8
    edge_types: tuple[str, ...] | None = None
    exclude_edge_types: tuple[str, ...] = DEFAULT_EXPANSION_EXCLUDES

    def block(self) -> dict[str, Any]:
        return {
            "depth": self.depth,
            "max_neighbors": self.max_neighbors,
            "edge_types": list(self.edge_types) if self.edge_types else None,
            "exclude_edge_types": list(self.exclude_edge_types),
        }


_EXPANSION_KEYS = ("depth", "max_neighbors", "edge_types", "exclude_edge_types")


def parse_expansion(value: Any) -> GraphExpansion | None:
    """``expand`` as either surface received it, or a refusal saying how to fix it.

    ``None``, ``False`` and ``0`` mean no expansion; ``True`` means the
    defaults; an integer is a depth; an object sets any of ``depth``,
    ``max_neighbors``, ``edge_types`` and ``exclude_edge_types``. Naming
    ``edge_types`` walks only those, so the default exclusions are dropped
    with it; naming ``exclude_edge_types`` replaces them (``[]`` for none).
    """

    if value is None or value is False:
        return None
    if isinstance(value, GraphExpansion):
        return value
    if value is True:
        return GraphExpansion()
    if isinstance(value, int):
        if value == 0:
            return None
        return GraphExpansion(depth=_expansion_depth(value))
    if not isinstance(value, dict):
        raise InvalidRequest(
            f"expand must be true, a depth (1-3), or an object with {', '.join(_EXPANSION_KEYS)}"
        )
    unknown = sorted(set(value) - set(_EXPANSION_KEYS))
    if unknown:
        raise InvalidRequest(
            f"expand does not take {', '.join(unknown)}; it takes {', '.join(_EXPANSION_KEYS)}"
        )
    edge_types = _edge_type_list(value.get("edge_types"), "edge_types")
    if "exclude_edge_types" in value:
        excludes = _edge_type_list(value.get("exclude_edge_types"), "exclude_edge_types") or ()
    else:
        excludes = () if edge_types else DEFAULT_EXPANSION_EXCLUDES
    max_neighbors = value.get("max_neighbors", GraphExpansion.max_neighbors)
    if (
        isinstance(max_neighbors, bool)
        or not isinstance(max_neighbors, int)
        or not 1 <= max_neighbors <= MAX_EXPANSION_NEIGHBORS
    ):
        raise InvalidRequest(
            f"expand.max_neighbors must be an integer from 1 to {MAX_EXPANSION_NEIGHBORS}"
        )
    return GraphExpansion(
        depth=_expansion_depth(value.get("depth", GraphExpansion.depth)),
        max_neighbors=max_neighbors,
        edge_types=edge_types,
        exclude_edge_types=excludes,
    )


def _expansion_depth(depth: Any) -> int:
    if (
        isinstance(depth, bool)
        or not isinstance(depth, int)
        or not 1 <= depth <= MAX_EXPANSION_DEPTH
    ):
        raise InvalidRequest(f"expand depth must be an integer from 1 to {MAX_EXPANSION_DEPTH}")
    return depth


def _edge_type_list(value: Any, name: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise InvalidRequest(f"expand.{name} must be a list of edge type names")
    if not all(isinstance(item, str) and item for item in value):
        raise InvalidRequest(f"expand.{name} must be a list of edge type names")
    return tuple(value)


@dataclass(frozen=True)
class SearchRequest:
    """One retrieval call, in the vocabulary both surfaces already speak.

    Typed rather than a bag of keyword arguments because this is the boundary
    the review named: an operation whose payload has no declared shape cannot
    be moved, and cannot be checked for agreement between two callers.
    """

    query: str
    knowledge_base: str | None = None
    mode: str = "hybrid"
    max_results: int = 10
    source_name: str | None = None
    section: str | None = None
    principal: str | None = None
    principal_groups: list[str] | None = None
    memory: Any = None
    #: Post-filters applied after the merge. Every one of these is a reason to
    #: over-fetch, which is why they travel together.
    exclude_sources: list[str] | None = None
    node_types: list[str] | None = None
    min_score: float | None = None
    source_types: list[str] | None = None
    exclude_source_types: list[str] | None = None
    #: Ask the arms to report what each stage did. The tuning plane's whole
    #: diagnosis is this block; see `search.explain`.
    explain: bool = False
    #: Pin this search to a sealed snapshot. The region verifies it still
    #: stands there and *refuses* if it does not — see `services.snapshots` for
    #: why that is a refusal rather than time travel.
    snapshot_id: str | None = None
    #: The instant memory validity is evaluated at. Carried here as well as on
    #: the memory policy so it reaches the lineage block even when the region
    #: holds no memory at all, which is the case where a caller most needs to
    #: be told that `as_of` did nothing.
    as_of: str | None = None
    #: The caller's correlation id, echoed into the lineage so a result can be
    #: joined to the ledger row and the span that produced it.
    trace_id: str | None = None
    #: Walk the graph out from each hit and attach the neighbourhood. Anything
    #: `parse_expansion` accepts; off by default, so a caller that does not ask
    #: receives the payload it always did.
    expand: Any = None

    @property
    def filtering(self) -> bool:
        """Whether a post-filter will drop rows, and the arms must over-fetch."""

        return criteria_active(
            self.exclude_sources,
            self.node_types,
            self.min_score,
            self.source_types,
            self.exclude_source_types,
        )

    def criteria_block(self) -> dict[str, Any]:
        return criteria_dict(
            self.source_name,
            self.exclude_sources,
            self.node_types,
            self.min_score,
            self.memory,
            self.source_types,
            self.exclude_source_types,
        )


@dataclass(frozen=True)
class FilesRequest:
    """`relevant_files`: the same retrieval, projected to files."""

    task: str
    knowledge_base: str | None = None
    max_files: int = 8
    source_name: str | None = None
    section: str | None = None
    principal: str | None = None
    principal_groups: list[str] | None = None
    memory: Any = None


def search(context: ServiceContext, request: SearchRequest) -> dict[str, Any]:
    """Hybrid retrieval with criteria, over-fetch, metrics and provenance.

    The order is load-bearing. Over-fetch is decided *before* the arms run,
    from the ranking parameters rather than a literal; the criteria filter runs
    *after* the merge and truncates to what the caller asked for; the metric is
    recorded around retrieval only, so criteria bookkeeping does not read as
    retrieval latency.
    """

    kb_id = context.knowledge_base(request.knowledge_base)
    # A malformed expansion is refused before anything is retrieved.
    request = replace(request, expand=parse_expansion(request.expand))
    # Before the arms run, not after. A pinned search whose corpus has moved
    # must not spend the retrieval and then discard it: the caller is going to
    # attribute whatever comes back to the snapshot it named, so the only safe
    # order is to establish that the name is still true first.
    snapshot = _require_snapshot(context, request.snapshot_id)
    return _search(context, request, kb_id, snapshot)


def _require_snapshot(context: ServiceContext, snapshot_id: str | None) -> Any:
    if not snapshot_id:
        return None
    from pheasant.services import snapshots as snapshot_service

    return snapshot_service.require_current(context, snapshot_id)


def _search(
    context: ServiceContext, request: SearchRequest, kb_id: str, snapshot: Any
) -> dict[str, Any]:
    """One retrieval, once the knowledge base and any snapshot pin are settled."""

    ranking = context.searcher.ranking_parameters()
    fetch = ranking.overfetch(request.max_results, filtering=request.filtering)
    started = time.perf_counter()
    try:
        payload = context.searcher.search_context(
            kb_id,
            request.query,
            request.mode,
            fetch,
            request.source_name,
            graph=context.graph,
            principal=request.principal,
            principal_groups=request.principal_groups,
            security=context.config.security,
            section=request.section,
            memory=request.memory,
            explain=request.explain,
        )
    except Exception:
        metrics.REGISTRY.inc("pheasant_search_total", mode=request.mode, outcome="error")
        raise
    finally:
        # Timed around retrieval only. Wrapping the post-filter too would fold
        # criteria bookkeeping into what reads as retrieval latency.
        metrics.REGISTRY.observe(
            "pheasant_search_duration_seconds", time.perf_counter() - started, mode=request.mode
        )
    metrics.REGISTRY.inc("pheasant_search_total", mode=request.mode, outcome="ok")

    if request.filtering:
        payload = dict(payload)
        payload["results"] = apply_retrieval_criteria(
            payload.get("results") or [],
            exclude_sources=request.exclude_sources,
            node_types=request.node_types,
            min_score=request.min_score,
            source_types=request.source_types,
            exclude_source_types=request.exclude_source_types,
        )[: request.max_results]
        payload["criteria"] = request.criteria_block()
    else:
        payload = dict(payload)

    # Which graph answered. A retrieval diagnosis that cannot name the
    # generation cannot tell "the document is not indexed" from "this replica
    # has not picked up the index that has it" — and those call for opposite
    # responses.
    payload["graph_generation"] = getattr(context.engine, "loaded_graph_generation", None)
    payload["lineage"] = _lineage(
        context,
        request,
        kb_id,
        ranking=ranking,
        snapshot=snapshot,
        payload=payload,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )
    if request.expand is not None:
        payload["results"], payload["expansion"] = _expand(
            context, request, payload.get("results") or []
        )
    return payload


def _expand(
    context: ServiceContext, request: SearchRequest, results: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach each hit's graph neighbourhood under ``graph``.

    After truncation, so only hits the caller receives are walked, and after
    the lineage, so expanding a search cannot change its query id: the
    retrieval is the same retrieval, with structure added to what it found.

    The seed is the hit's ``node_id`` — the artifact for a passage, the node
    itself for a graph hit — because a passage's own chunk node leads only
    back to its file. Under ACL enforcement neighbours pass the same artifact
    check the hits did, over-collected by the one over-fetch parameter so a
    filtered neighbourhood still fills its budget.
    """

    from pheasant.graph.traversal import expand
    from pheasant.services.graph import neighbor_filter

    expansion = request.expand
    seeds: list[str] = []
    for item in results:
        seed = str(item.get("node_id") or "")
        if seed and seed not in seeds:
            seeds.append(seed)
    walked = seeds[:MAX_EXPANSION_SEEDS]
    admit = neighbor_filter(context, request.principal, request.principal_groups)
    fetch = None
    if admit is not None:
        ranking = context.searcher.ranking_parameters()
        fetch = ranking.overfetch(expansion.max_neighbors, filtering=True)
    neighbourhoods = expand(
        context.graph,
        walked,
        depth=expansion.depth,
        edge_types=list(expansion.edge_types) if expansion.edge_types else None,
        exclude_edge_types=set(expansion.exclude_edge_types) or None,
        max_neighbors=expansion.max_neighbors,
        fetch=fetch,
        admit=admit,
    )
    expanded = []
    for item in results:
        seed = str(item.get("node_id") or "")
        neighbourhood = neighbourhoods.get(seed)
        if neighbourhood is None:
            expanded.append(item)
            continue
        expanded.append({**item, "graph": {"seed": seed, **neighbourhood}})
    block = {
        **expansion.block(),
        "seeds": len(walked),
        # Distinct hits past the seed ceiling carry no `graph` block; walk
        # from them with get_graph_neighbors.
        "seeds_skipped": len(seeds) - len(walked),
        "nodes": sum(len(n["neighbors"]) for n in neighbourhoods.values()),
    }
    return expanded, block


def relevant_files(context: ServiceContext, request: FilesRequest) -> dict[str, Any]:
    """The same retrieval as :func:`search`, projected to distinct files.

    No ``graph=``: this answers with *files*, and graph nodes (concepts,
    symbols) carry no ``relative_path``, so admitting them would crowd the file
    hits out of the merge and return an empty list.

    It runs under the same ACL enforcement and the same memory policy as
    `search`, which is the correction this module exists to make permanent:
    dropping either one silently served unfiltered — and in the memory case,
    *corrected* — content to whichever surface forgot it.
    """

    kb_id = context.knowledge_base(request.knowledge_base)
    payload = context.searcher.search_context(
        kb_id,
        request.task,
        "hybrid",
        request.max_files,
        request.source_name,
        principal=request.principal,
        principal_groups=request.principal_groups,
        security=context.config.security,
        section=request.section,
        memory=request.memory,
    )
    seen: set[str] = set()
    files: list[dict[str, Any]] = []
    for result in payload.get("results") or []:
        relative_path = result.get("relative_path")
        if relative_path and relative_path not in seen:
            seen.add(relative_path)
            files.append(result)
    return {"files": files}


#: The most queries one batch may carry, and the most hits it may ask for in
#: total (queries × ``max_results``). A batch runs on a serving replica inside
#: one request slot, so an unbounded one is a way for a single caller to hold
#: that slot for as long as it likes. Both refusals say how to split the call.
MAX_BATCH_QUERIES = 25
MAX_BATCH_RESULTS = 1000


@dataclass(frozen=True)
class BatchSearchRequest:
    """Many queries, one set of criteria: bulk context in one round trip.

    ``criteria`` is an ordinary :class:`SearchRequest` whose ``query`` is
    ignored. Reusing it rather than re-declaring its fields is what keeps a
    batch from drifting away from a single search: a criterion added there is
    a criterion a batch honours, with no second list to remember.
    """

    queries: tuple[str, ...]
    criteria: SearchRequest
    #: Include each query's own payload under ``searches``. Off when a caller
    #: wants only the merged context and would rather not receive every hit
    #: twice.
    per_query: bool = True


def search_batch(context: ServiceContext, request: BatchSearchRequest) -> dict[str, Any]:
    """Run :func:`search` once per query and merge the hits into one context.

    Each query is an ordinary search — same over-fetch, criteria, ACL, memory
    policy, metrics and lineage — so a batch cannot answer a query differently
    than asking it alone would. What batching buys is everything *around* the
    arms: one round trip, one knowledge-base resolution, and one snapshot
    verification rather than one per query (re-deriving a manifest is the most
    expensive thing a pinned search does before it retrieves anything).

    ``results`` is the union, deduplicated by chunk or node id, and ordered
    **by rank, not by score**: every query's first hit, then every query's
    second, and so on. Fused RRF scores have no absolute scale, so comparing
    them across two queries would rank by how *confident* each query happened
    to be; ordering by rank means no query's tail can crowd out another's head,
    and a caller that truncates the list keeps coverage of every query. Ties
    go to the hit more queries agreed on. Each merged hit carries a ``batch``
    block naming the queries that returned it and its best rank among them.

    Queries run in order on the calling thread. Concurrent reads do not scale
    on SQLite in a container (see CLAUDE.md, "Serving concurrency"), and a
    batch that fanned out would take several request slots' worth of work
    while holding one.
    """

    queries = _batch_queries(request.queries, request.criteria.max_results)
    kb_id = context.knowledge_base(request.criteria.knowledge_base)
    # Expanded once over the merged context, not once per query: the queries
    # of a batch overlap, and a walk per query would repeat the same
    # neighbourhoods up to 25 times over.
    expansion = parse_expansion(request.criteria.expand)
    criteria = replace(request.criteria, expand=None)
    snapshot = _require_snapshot(context, request.criteria.snapshot_id)

    started = time.perf_counter()
    # A repeated query is asked once and answered for every position it holds.
    answered: dict[str, dict[str, Any]] = {}
    searches: list[dict[str, Any]] = []
    for query in queries:
        if query not in answered:
            answered[query] = _search(context, replace(criteria, query=query), kb_id, snapshot)
        searches.append(answered[query])

    merged = _merge_batch(searches)
    expansion_block = None
    if expansion is not None:
        merged, expansion_block = _expand(context, replace(criteria, expand=expansion), merged)
    hits = sum(len(payload.get("results") or []) for payload in searches)
    payload: dict[str, Any] = {
        "knowledge_base": kb_id,
        "queries": list(queries),
        "results": merged,
        "counts": {
            "queries": len(queries),
            "distinct_queries": len(answered),
            "hits": hits,
            "results": len(merged),
            # Hits another query had already returned. High overlap means the
            # queries are paraphrases of one another rather than facets.
            "overlap": hits - len(merged),
        },
        "graph_generation": getattr(context.engine, "loaded_graph_generation", None),
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
    }
    if request.criteria.filtering:
        payload["criteria"] = request.criteria.criteria_block()
    if expansion_block is not None:
        payload["expansion"] = expansion_block
    if request.per_query:
        payload["searches"] = [{"query": query, **searches[i]} for i, query in enumerate(queries)]
    return payload


def _batch_queries(queries: Any, max_results: int) -> tuple[str, ...]:
    """The batch's queries, or a refusal that says how to fix the call."""

    if isinstance(queries, str) or not isinstance(queries, (list, tuple)) or not queries:
        raise InvalidRequest("queries must be a non-empty list of query strings")
    if len(queries) > MAX_BATCH_QUERIES:
        raise InvalidRequest(
            f"A batch holds at most {MAX_BATCH_QUERIES} queries; this one has {len(queries)}. "
            "Split it into several calls."
        )
    for index, query in enumerate(queries):
        if not isinstance(query, str) or not query.strip():
            raise InvalidRequest(f"queries[{index}] is empty; every query needs text")
    requested = len(queries) * max(1, int(max_results or 1))
    if requested > MAX_BATCH_RESULTS:
        raise InvalidRequest(
            f"A batch may ask for at most {MAX_BATCH_RESULTS} hits in total; "
            f"{len(queries)} queries × max_results {max_results} is {requested}. "
            "Lower max_results or split the batch."
        )
    return tuple(queries)


def _merge_batch(searches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate every query's hits into one list ordered by rank."""

    merged: dict[str, dict[str, Any]] = {}
    for query_index, payload in enumerate(searches):
        for rank, item in enumerate(payload.get("results") or []):
            # A hit with no id cannot be recognised twice, so it is kept as
            # its own entry rather than collapsed into a stranger.
            key = str(item.get("chunk_id") or item.get("node_id") or f"#{query_index}:{rank}")
            entry = merged.get(key)
            if entry is None:
                merged[key] = {
                    "item": item,
                    "rank": rank,
                    "first": (query_index, rank),
                    "queries": [query_index],
                }
                continue
            if query_index not in entry["queries"]:
                entry["queries"].append(query_index)
            if rank < entry["rank"]:
                entry["item"], entry["rank"] = item, rank
    ordered = sorted(
        merged.values(), key=lambda entry: (entry["rank"], -len(entry["queries"]), entry["first"])
    )
    return [
        {
            **entry["item"],
            "batch": {"best_rank": entry["rank"] + 1, "matched_queries": entry["queries"]},
        }
        for entry in ordered
    ]


def _query_id(kb_id: str, request: SearchRequest, snapshot_id: str | None) -> str:
    """This query's identity: a digest over everything that decides its answer.

    Content-addressed, and with no clock in it, for the reason the snapshot id
    has none: two runs issuing the same query against the same state under the
    same profile are *one* measurement, and an id that moved with the wall
    clock would make them two rows that cannot be paired. A harness joins its
    per-question operands to a Pheasant result on this.

    The ranking bundle is in the digest because it changes the answer. The
    trace id deliberately is not: it identifies the *call*, and two calls of
    one query under one configuration must agree here or the join is useless.
    """

    digest = hashlib.blake2b(digest_size=16)
    for part in (
        kb_id,
        request.query,
        request.mode,
        str(request.max_results),
        snapshot_id or "",
        request.as_of or "",
        request.principal or "",
        json.dumps(request.criteria_block(), sort_keys=True, default=str),
    ):
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x1f")
    return "q-" + digest.hexdigest()


def _lineage(
    context: ServiceContext,
    request: SearchRequest,
    kb_id: str,
    *,
    ranking: Any,
    snapshot: dict[str, Any] | None,
    payload: dict[str, Any],
    elapsed_ms: float,
) -> dict[str, Any]:
    """Everything a result has to be attributable to, in one block.

    The result rows already carried the *locator* half of this — artifact id,
    chunk id, line span, heading path — and none of the *state* half: which
    snapshot, which ranking bundle, which memory policy, which principal,
    which instant. An answer that cannot name those is reproducible only by
    somebody who happened to be watching when it was produced.

    Split into `state` and `timing` on purpose. Everything under `state` is a
    function of the request and the region, so two replicas answering one
    pinned query agree on it exactly; `timing` is a measurement and differs
    every call. Mixing them would make the whole block un-comparable and
    quietly cost the property the rest of it exists for.
    """

    results = payload.get("results") or []
    memory_policy = payload.get("memory_policy")
    return {
        "query_id": _query_id(kb_id, request, request.snapshot_id),
        "knowledge_base": kb_id,
        "state": {
            "snapshot_id": request.snapshot_id,
            "snapshot_current": None if snapshot is None else bool(snapshot["current"]),
            "graph_generation": payload.get("graph_generation"),
            "ranking": ranking.describe(),
            "memory": {
                # Reported even when the region has no memory, unlike
                # `memory_policy` above: "no memory took part" is the answer a
                # memory-off arm needs to be able to *record*, and an absent
                # key is indistinguishable from a key nobody looked at.
                #
                # Asked through `memory_source`, which is the one predicate the
                # rest of the region uses — there is no `memory.enabled` flag,
                # and the first version of this read one. It was always False,
                # so a region with memory fully on reported it off, and the
                # probe that checked the field agreed with it. A flag nobody
                # declared reads exactly like a flag nobody sets.
                "enabled": memory_enabled(context),
                "policy": memory_policy,
                "steering": payload.get("memory_steering"),
            },
            "as_of": request.as_of,
            "principal": request.principal,
            "principal_groups": list(request.principal_groups or []),
            "acl_enforced": bool(
                getattr(getattr(context.config, "security", None), "acl_enforced", False)
            ),
            "criteria": payload.get("criteria") or request.criteria_block(),
            "mode": request.mode,
            "max_results": request.max_results,
        },
        "timing": {
            "retrieval_ms": round(elapsed_ms, 3),
            # Whether the cut is what limited the answer. A caller cannot tell
            # "the corpus has three matches" from "you asked for three" out of
            # a list of three, and those call for opposite next moves.
            "truncated": len(results) >= request.max_results,
            "returned": len(results),
        },
        "trace_id": request.trace_id,
    }


def memory_enabled(context: ServiceContext) -> bool:
    """Whether this region has an enabled memory source.

    The same question `describe_retrieval` and the memory routes ask, asked the
    same way — through `memory_source`, with the state store passed so the
    runtime-registry fallback applies. Memory enabled from the UI lives in the
    registry and reaches `config.sources` only in the process that created it,
    so a check reading the config alone would report "off" on every replica but
    one.

    Public because the readiness probes ask it too, and a predicate two callers
    share is not an internal detail of either.
    """

    try:
        from pheasant.memory.store import memory_source

        return memory_source(context.config, context.state) is not None
    except Exception:  # noqa: BLE001 - lineage must never break a search
        return False
