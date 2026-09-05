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
from dataclasses import dataclass
from typing import Any

from pheasant.search.criteria import apply_retrieval_criteria, criteria_active, criteria_dict
from pheasant.services import ServiceContext
from pheasant.telemetry import metrics


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
    ranking = context.searcher.ranking_parameters()
    fetch = ranking.overfetch(request.max_results, filtering=request.filtering)

    # Before the arms run, not after. A pinned search whose corpus has moved
    # must not spend the retrieval and then discard it: the caller is going to
    # attribute whatever comes back to the snapshot it named, so the only safe
    # order is to establish that the name is still true first.
    snapshot = None
    if request.snapshot_id:
        from pheasant.services import snapshots as snapshot_service

        snapshot = snapshot_service.require_current(context, request.snapshot_id)

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
    return payload


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
