from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import wraps
from typing import Any

from pheasant.graph.simple import SimpleMultiDiGraph
from pheasant.memory.policy import (
    PREFER_SHARE,
    MemoryPolicy,
    admits,
    describe,
    load_memory_index,
    may_filter,
    resolve,
    utc_now_iso,
)
from pheasant.search import fusion
from pheasant.search.criteria import source_type_map, stamp_source_types
from pheasant.search.fusion import Fused
from pheasant.search.graph_search import search_graph
from pheasant.search.observability import observe_search, should_sample, stage_digest
from pheasant.search.ranking import DEFAULT_RANKING, RankingParameters
from pheasant.search.sqlite_store import SearchStore, section_matches, section_needle
from pheasant.search.vector_store import VectorSearcher
from pheasant.telemetry.interactions import annotate_current

logger = logging.getLogger(__name__)

VALID_MODES = {"hybrid", "text", "graph", "vector"}

#: Upper bound on a single query's result count. High enough that no
#: legitimate caller notices, low enough that a hostile one cannot ask the
#: region to load its whole index into memory.
MAX_RESULTS_CEILING = 500

# These are process-wide serving limits. Query fan-out stays inside one
# admitted top-level search, while PostgreSQL lexical ranking has its own
# narrower limit because the SQL ranking work is CPU-bound in the database.
_TOP_LEVEL_SEARCHES = threading.BoundedSemaphore(4)
_POSTGRES_LEXICAL_SEARCHES = threading.BoundedSemaphore(2)


def _bounded_top_level_search(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        from pheasant.request_budget import DeadlineExceeded, remaining_seconds

        started = time.perf_counter()
        remaining = remaining_seconds()
        acquired = (
            _TOP_LEVEL_SEARCHES.acquire()
            if remaining is None
            else _TOP_LEVEL_SEARCHES.acquire(timeout=remaining)
        )
        from pheasant.request_budget import record_active_timing

        record_active_timing("search_admission_wait", time.perf_counter() - started)
        if not acquired:
            raise DeadlineExceeded("search admission exceeded the assistant request deadline")
        try:
            result = fn(self, *args, **kwargs)
            from pheasant.request_budget import check_active

            check_active()
            return result
        finally:
            _TOP_LEVEL_SEARCHES.release()

    return wrapped


class HybridSearch:
    def __init__(
        self,
        store: SearchStore,
        vector: VectorSearcher | None = None,
        node_index: Any = None,
        wasm_relationship_search: bool = False,
        steering_enabled: bool = False,
        default_memory_policy: str = "auto",
        usage_tracking: bool = False,
        stage_sample_rate: float = 0.0,
    ):
        self.store = store
        # Optional semantic candidates (Synapse 21.4). None when
        # search.embeddings is disabled: "vector" contributes nothing and
        # every other mode behaves exactly as before.
        self.vector = vector
        # Optional FTS index over graph nodes. None (or an empty index) makes
        # graph search scan the graph in memory, which is what it always did.
        self.node_index = node_index
        # Synapse Step 34.5b: opt-in WASM-accelerated relationship search
        # (search.wasm_relationship_search). Default off; falls back to pure
        # Python on any failure.
        self.wasm_relationship_search = wasm_relationship_search
        # Step 33.8: let memory rules steer ranking (memory.steering_enabled).
        # Default off, so a region that has not asked for it ranks exactly as
        # it did before.
        self.steering_enabled = steering_enabled
        # memory.default_policy — what a caller that says nothing gets. A
        # per-call `memory` argument always wins, so this only fills the gap.
        self.default_memory_policy = default_memory_policy
        # memory.usage_tracking — count which memories retrieval returns, so
        # salience can reflect use. Off by default: it is a write on the read
        # path and recording what someone looks up is an operator's choice.
        self.usage_tracking = usage_tracking
        # observability.interactions.stage_sample_rate — the fraction of
        # searches that capture a per-stage digest onto their ledger row. The
        # always-on Prometheus counters do not depend on this; what this buys
        # is a *query you can look at* when one of those counters moves, and a
        # live diagnosis source for the tuning plane that does not wait for a
        # batch. Zero by default: it is a write on somebody's ledger, and how
        # much of one is an operator's call.
        self.stage_sample_rate = float(stage_sample_rate or 0.0)

    def ranking_parameters(self) -> RankingParameters:
        """The parameter point this search fuses under.

        Taken from the store so the lexical arm and the fusion cannot end up
        on different points: the SQL weights and the RRF constant are one
        configuration, and a tuning trial that moved only one of them would be
        measuring a state the region can never actually serve.
        """

        resolve = getattr(self.store, "ranking_parameters", None)
        return resolve() if callable(resolve) else DEFAULT_RANKING

    @_bounded_top_level_search
    def search_context(
        self,
        knowledge_base: str,
        query: str,
        mode: str = "hybrid",
        max_results: int = 10,
        source_name: str | None = None,
        graph: SimpleMultiDiGraph | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        security: Any = None,
        section: str | None = None,
        memory: Any = None,
        steering_kinds: tuple[str, ...] | None = None,
        extra_steering_records: list[dict[str, Any]] | None = None,
        explain: bool = False,
    ) -> dict:
        """Run one query across the arms and fuse them.

        ``steering_kinds`` narrows which memory rule kinds may fire. ``None``
        -- every production caller -- means "whatever ``steering_enabled``
        says", so this behaves exactly as it did before the argument existed.
        The evaluation plane passes an explicit subset to build a paired
        ablation: measuring what *alias* rules contribute needs a run in which
        preference and exclusion rules do not fire, and one in which they do,
        differing in nothing else.

        ``explain`` adds a ``stages`` block: what each arm returned before the
        filters ran, what each filter removed, the fused order before
        truncation, and the ranking parameters all of it ran under. It is off
        for every production caller and the payload is **byte-identical**
        without it -- the same rule ``heading_path`` and ``memory_policy``
        follow. It exists because "retrieval is bad for this query" is not an
        actionable statement: the answer is different, and the fix is
        different, depending on whether the lexical arm never had the document,
        a filter removed it, fusion ranked it below three worse ones, or it
        fell off the end of ``max_results``. Nothing else in the region can
        tell those four apart, because after the merge they all look identical
        -- an absent result.

        ``extra_steering_records`` adds rule records that are **not in the
        store** -- proposed rules under shadow validation. They go through the
        same `parse_rule`/`admits` path as stored ones, so a shadow run
        measures the rule retrieval would really apply rather than a
        re-implementation of it, and nothing is written anywhere: a candidate
        cannot reach production ranking by being evaluated.
        """
        mode = mode if mode in VALID_MODES else "hybrid"
        # Clamped, not just floored: `max_results` arrives straight off an
        # unauthenticated HTTP body, and an over-fetching ACL pass multiplies
        # it. Without a ceiling a single request can ask the region to
        # materialize the entire index in memory.
        max_results = min(max(1, int(max_results or 10)), MAX_RESULTS_CEILING)

        # Step 32.2 — principal-aware retrieval, opt-in via
        # security.acl_enforced. Candidates are over-fetched, filtered
        # against artifact ACLs *before* the merge/return, then truncated.
        enforced = bool(security is not None and getattr(security, "acl_enforced", False))
        # Step 33.6 — agent-memory policy. The index is loaded once and shared:
        # the text arm filters in SQL (recall), the vector and graph arms filter
        # against the same rows below, and every arm's hits are annotated from
        # it. An empty index means the region has no memory records, and then
        # none of this does anything at all.
        policy = MemoryPolicy.parse(memory if memory is not None else self.default_memory_policy)
        memory_index = load_memory_index(getattr(self.store, "state", None))
        memory_now = utc_now_iso() if memory_index else None
        memory_filtered = may_filter(policy, memory_index, now=memory_now or "")
        # Step 33.8 — rules from `alias`/`preference`/`exclusion` records, put
        # through the same `admits` predicate as retrieval so a corrected or
        # out-of-scope record steers nothing. Empty unless steering is on.
        steering = None
        steering_on = self.steering_enabled if steering_kinds is None else bool(steering_kinds)
        if steering_on and (memory_index or extra_steering_records):
            from pheasant.memory.steering import load_steering, load_steering_records

            records = load_steering_records(self.store.state)
            records.extend(extra_steering_records or [])
            steering = (
                load_steering(
                    records,
                    policy,
                    now=memory_now or utc_now_iso(),
                    enabled=True,
                    kinds=steering_kinds,
                )
                or None
            )
        # A section filter narrows hard, so the arms that can only be filtered
        # after the fact (graph, which has no breadcrumb at all) need room to
        # find in-section hits — same over-fetch reasoning as the ACL pass, and
        # the same again for a memory policy, where `only` narrows to a handful
        # of records and `off` can drop a lot.
        sectioned = bool(section_needle(section))
        ranking = self.ranking_parameters()
        fetch_n = ranking.overfetch(max_results, filtering=enforced or sectioned or memory_filtered)
        # Nothing is collected unless a caller asked or this search was
        # sampled, so the ordinary path allocates one `None` and does no extra
        # work at all.
        stages: dict[str, Any] | None = None
        sampled = not explain and self._sampled()
        if explain or sampled:
            stages = {
                "parameters": ranking.describe(),
                "query": {
                    "text": query,
                    "mode": mode,
                    "max_results": max_results,
                    "fetch_n": fetch_n,
                    "over_fetching": fetch_n > max_results,
                },
                "candidates": {},
                "surviving": {},
                "filters": {},
                "paths": {},
            }

        text_results: list[dict[str, Any]] = []
        graph_results: list[dict[str, Any]] = []
        vector_results: list[dict[str, Any]] = []

        # The three retrievers are independent, and in hybrid mode their costs
        # are wildly different — a graph scan and a vector query that waits on
        # a remote embedding have no reason to queue behind each other. Running
        # them together makes hybrid cost about as much as its slowest arm
        # instead of the sum of all three. Each is I/O or read-only work, so
        # threads are enough (SQLite reads release the GIL; the embedder is a
        # network call).
        jobs: dict[str, Any] = {}
        if mode in {"hybrid", "text"}:

            def search_text() -> list[dict[str, Any]]:
                state = getattr(self.store, "state", None)
                dialect = getattr(state, "dialect", None)
                admitted = False
                if dialect is not None and dialect.is_postgres:
                    from pheasant.request_budget import DeadlineExceeded, remaining_seconds

                    remaining = remaining_seconds()
                    wait_started = time.perf_counter()
                    admitted = (
                        _POSTGRES_LEXICAL_SEARCHES.acquire()
                        if remaining is None
                        else _POSTGRES_LEXICAL_SEARCHES.acquire(timeout=remaining)
                    )
                    from pheasant.request_budget import record_active_timing

                    record_active_timing(
                        "postgres_lexical_admission_wait", time.perf_counter() - wait_started
                    )
                    if not admitted:
                        raise DeadlineExceeded(
                            "PostgreSQL lexical admission exceeded the assistant request deadline"
                        )
                try:
                    return self.store.search(
                        query,
                        source_name=source_name,
                        max_results=fetch_n,
                        section=section,
                        memory_policy=policy if memory_index else None,
                        memory_now=memory_now,
                        steering=steering,
                    )
                finally:
                    if admitted:
                        _POSTGRES_LEXICAL_SEARCHES.release()

            jobs["text"] = search_text
        # Graph search needs the live graph; callers that don't supply one
        # (e.g. CLI/MCP search_context) transparently fall back to text search.
        if mode in {"hybrid", "graph"} and graph is not None:
            jobs["graph"] = lambda: search_graph(
                graph,
                query,
                max_results=fetch_n,
                source_name=source_name,
                node_index=self.node_index,
                wasm_relationship_search=self.wasm_relationship_search,
            )
        if mode in {"hybrid", "vector"} and self.vector is not None:
            jobs["vector"] = lambda: self.vector.search(
                query,
                source_name=source_name,
                max_results=fetch_n,
                # The vector arm filters by source *inside* itself, so it needs
                # its own headroom on top of `fetch_n`. Passed down rather than
                # hardcoded there: one parameter governs every over-fetch, and
                # a vector store that carried its own multiplier is exactly the
                # divergence this consolidation removed.
                overfetch=ranking.filter_overfetch,
            )

        failed_arms: list[str] = []
        if len(jobs) == 1:
            # A single explicitly-requested arm (mode="text"/"graph"/"vector")
            # has nothing to fall back to, so a failure here still raises —
            # the caller asked for exactly this arm and silently returning
            # nothing would be a worse answer than an error.
            name, job = next(iter(jobs.items()))
            collected = {name: job()}
        else:
            # In "hybrid" mode the arms are independent and their failure
            # modes are not equivalent: text/graph read the local SQLite
            # store, but vector calls a remote embedding provider (network,
            # auth, quota). One arm's outage — e.g. a misconfigured or
            # expired embedding API key — must not take down text and graph
            # results that were already fetched successfully; it degrades
            # hybrid to whatever arms are actually healthy instead of
            # crashing the whole search (and, upstream, the assistant chat
            # request that depends on it).
            with ThreadPoolExecutor(max_workers=len(jobs) or 1) as pool:
                futures = {name: pool.submit(copy_context().run, job) for name, job in jobs.items()}
                collected = {}
                for name, future in futures.items():
                    try:
                        collected[name] = future.result()
                    except Exception as exc:
                        from pheasant.request_budget import DeadlineExceeded, remaining_seconds

                        if isinstance(exc, DeadlineExceeded) or (
                            remaining_seconds() is not None and (remaining_seconds() or 0) <= 0
                        ):
                            raise DeadlineExceeded(
                                "assistant request deadline exceeded during hybrid retrieval"
                            ) from exc
                        logger.warning(
                            "hybrid search: %r arm failed, degrading", name, exc_info=True
                        )
                        collected[name] = []
                        # An arm that *failed* and an arm that legitimately
                        # found nothing both leave an empty list, and a
                        # diagnosis must never confuse them: "the vector arm
                        # is down" and "the vector arm has nothing for this
                        # query" call for opposite responses.
                        failed_arms.append(name)
        text_results = collected.get("text", [])
        graph_results = collected.get("graph", [])
        vector_results = collected.get("vector", [])

        if stages is not None:
            # Before any filter runs: this is what each arm could actually
            # find. A target absent here was never a candidate, which is a
            # different problem (indexing, chunking, the query itself) from
            # every other stage's.
            _record_arms(stages, "candidates", text_results, vector_results, graph_results)
            stages["arms_run"] = sorted(jobs)
            stages["arms_failed"] = sorted(failed_arms)

        def _filter_stage(name: str) -> None:
            """Attribute what this filter just removed, per arm."""

            if stages is None:
                return
            before = stages["surviving"] or stages["candidates"]
            after = _arm_ids(text_results, vector_results, graph_results)
            stages["filters"][name] = {
                arm: sorted(set(before.get(arm, [])) - set(after.get(arm, [])))
                for arm in ("text", "vector", "graph")
                if set(before.get(arm, [])) - set(after.get(arm, []))
            }
            stages["surviving"] = after

        if sectioned:
            # The text arm was already narrowed in SQL; the vector arm carries a
            # breadcrumb and is filtered here. Graph hits are dropped outright:
            # a symbol or entity node is not part of any document section, so it
            # cannot satisfy a claim about one (the same conservative call the
            # ACL pass makes for nodes with no artifact row).
            vector_results = [
                r for r in vector_results if section_matches(r.get("heading_path"), section)
            ]
            graph_results = [
                r for r in graph_results if section_matches(r.get("heading_path"), section)
            ]
            _filter_stage("section")

        if memory_index:
            # The text arm was already narrowed in SQL. These two carry no
            # memory columns of their own, so they are filtered against the
            # same rows through the same predicate — one rule, two encodings,
            # and `tests/test_memory_policy.py` pins them together.
            def memory_admits(item: dict[str, Any]) -> bool:
                return admits(policy, resolve(item, memory_index), now=memory_now)

            vector_results = [r for r in vector_results if memory_admits(r)]
            graph_results = [r for r in graph_results if memory_admits(r)]
            _filter_stage("memory_policy")

        if enforced:
            from pheasant.security.acl import expand_principal, is_allowed

            identities = expand_principal(
                principal, principal_groups, getattr(security, "groups", None)
            )
            # Step 32.4 — union in IdP-synced groups, but only while the
            # mapping honors the staleness SLA (stale grants fail closed).
            if identities is not None and principal:
                from pheasant.security.idp import fresh_idp_groups

                identities |= fresh_idp_groups(
                    self.store.state, principal, getattr(security, "idp", None)
                )
            default_public = getattr(security, "default_visibility", "public") != "private"
            candidate_ids = [
                str(item.get("node_id"))
                for group in (text_results, vector_results, graph_results)
                for item in group
                if item.get("node_id")
            ]
            acls = self.store.state.artifact_acls(candidate_ids)

            def visible(item: dict[str, Any]) -> bool:
                node_id = str(item.get("node_id") or "")
                if node_id not in acls:
                    # Not resolvable to an artifact row (concept/symbol graph
                    # nodes): conservative deny under enforcement.
                    return False
                return is_allowed(acls[node_id], identities, default_public=default_public)

            text_results = [r for r in text_results if visible(r)]
            vector_results = [r for r in vector_results if visible(r)]
            graph_results = [r for r in graph_results if visible(r)]
            _filter_stage("acl")

        if memory_index and policy.mode == "prefer":
            text_results, vector_results, graph_results = _prefer_memory(
                text_results, vector_results, graph_results, memory_index, max_results
            )

        # Record which *kind* of source each hit came from, before the merge so
        # every arm is covered and before any caller-side criteria run. One
        # lookup per search, shared by all three arms.
        source_types = source_type_map(getattr(self.store, "state", None))
        for group in (text_results, vector_results, graph_results):
            stamp_source_types(group, source_types)

        if stages is not None and not stages["surviving"]:
            # No filter ran, so nothing narrowed the candidates. Say so
            # explicitly rather than leaving the reader to infer it from an
            # empty dict, which also reads as "everything was removed".
            stages["surviving"] = dict(stages["candidates"])
        results = _merge_rrf(
            text_results,
            vector_results,
            graph_results,
            max_results,
            ranking,
            collect=stages,
        )
        if memory_index:
            _annotate_memory(results, memory_index, policy)
            if self.usage_tracking:
                # Step 33.9 — count what retrieval actually returned. Last,
                # after truncation, so a record is credited for being *served*
                # rather than merely considered. Best-effort by construction:
                # `record_memory_use` swallows its own failures, because a
                # ranking signal must never cost a query.
                self.store.state.record_memory_use(
                    sorted(
                        {
                            str(item["memory"]["record_id"])
                            for item in results
                            if item.get("memory") and item["memory"].get("record_id")
                        }
                    ),
                    memory_now,
                )
        payload = {
            "query": query,
            "knowledge_base": knowledge_base,
            "mode": mode,
            "results": results,
            "counts": {
                "text": len(text_results),
                "graph": len(graph_results),
                "vector": len(vector_results),
                "returned": len(results),
            },
        }
        if failed_arms:
            payload["arm_failures"] = sorted(failed_arms)
        # Reported only when the region has memory, so a corpus without it
        # returns the exact payload it did before — the same "add the key only
        # when it says something" rule `heading_path` follows.
        if memory_index:
            payload["memory_policy"] = policy.as_dict()
        if steering:
            # Reported so an agent can see *why* it got what it got — a rule
            # that silently re-orders results is the thing to avoid.
            payload["memory_steering"] = steering.describe()
        if stages is not None:
            stages["returned"] = [_identity(item) for item in results]
            _record_paths(stages, text_results, vector_results, graph_results, results)
            if steering:
                stages["query"]["steering"] = steering.describe()
            if not sampled:
                payload["stages"] = stages
        # Emitted here rather than at each call site, so HTTP, MCP, the CLI and
        # the assistant are all covered by construction — and so a new caller
        # cannot be added without it. Counters only: never a database write on
        # the request path.
        observe_search(payload, stages)
        if sampled and stages is not None:
            # A sampled search annotates the row the *handler* is already
            # going to write. It does not write one of its own, which would be
            # a database write per sampled request — the thing the observation
            # plane's hot tier exists to avoid.
            annotate_current("retrieval_stages", stage_digest(payload, stages))
        return payload

    def _sampled(self) -> bool:
        """Is this search one of the sampled ones?

        Decided from the ambient trace, so every hop of one call agrees. A
        search running outside an observed handler has no trace and is never
        sampled — there would be no row to annotate.
        """

        if self.stage_sample_rate <= 0.0:
            return False
        try:
            from pheasant.telemetry.interactions import current_trace

            trace = current_trace()
        except Exception:  # noqa: BLE001 - observation is optional
            return False
        return bool(trace) and should_sample(self.stage_sample_rate, trace[0])


#: How many ids per arm a ``stages`` block carries. The arms are already
#: bounded by ``fetch_n``, so this is a second bound rather than the only one
#: -- but a diagnostic that can be asked for over HTTP should not be able to
#: return a list proportional to the index, and the attribution only ever
#: reads the top of each arm anyway.
EXPLAIN_MAX_IDS = 200


def _identity(item: dict[str, Any]) -> str:
    """The id a stage records a hit under.

    ``node_id`` first, deliberately: for a chunk hit that is the *artifact*
    id, which is what the evaluation plane's replay records and what a proof
    names as its target. Keying stages on ``chunk_id`` would produce a
    diagnostic that could never be joined to the evidence explaining why the
    query mattered.
    """

    return str(item.get("node_id") or item.get("chunk_id") or "")


def _arm_ids(
    text_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    graph_results: list[dict[str, Any]],
) -> dict[str, list[str]]:
    return {
        arm: [_identity(item) for item in results[:EXPLAIN_MAX_IDS] if _identity(item)]
        for arm, results in (
            ("text", text_results),
            ("vector", vector_results),
            ("graph", graph_results),
        )
    }


def _record_arms(
    stages: dict[str, Any],
    key: str,
    text_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    graph_results: list[dict[str, Any]],
) -> None:
    stages[key] = _arm_ids(text_results, vector_results, graph_results)


def _record_paths(
    stages: dict[str, Any],
    *groups: list[dict[str, Any]],
) -> None:
    """``{id: relative_path}`` for everything any stage mentioned.

    Carried once rather than per arm entry: an id appearing in three arms and
    the fused list would otherwise repeat its path four times, and the paths
    are what make a diagnosis readable by a person.
    """

    paths: dict[str, str] = stages.setdefault("paths", {})
    for group in groups:
        for item in group:
            node_id = _identity(item)
            if node_id and node_id not in paths:
                path = str(item.get("relative_path") or item.get("path") or item.get("title") or "")
                if path:
                    paths[node_id] = path


#: Reciprocal-rank-fusion constant. 60 is the value from the original RRF
#: paper and the usual default; it damps the top ranks just enough that one
#: arm's confident first place cannot alone decide the merge. It is
#: ``RankingParameters.rrf_k``'s default, and stays named here because the
#: fusion is the one place a reader looks for it.
RRF_K = 60


def _merge_rrf(
    text_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    graph_results: list[dict[str, Any]],
    max_results: int,
    ranking: RankingParameters = DEFAULT_RANKING,
    collect: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The query path's entry point onto :func:`pheasant.search.fusion.fuse`.

    Projects the rich records down to the three fields the merge reads,
    fuses, and maps the answer back to the dicts it started with. The loop
    itself lives in `search.fusion` because the tuning plane's re-fusion needs
    the same one: it used to be a hand-maintained mirror of this function,
    checked for drift by `verify_equivalence` rather than prevented from
    drifting at all.

    What stays here is what is genuinely the query path's: which dict is the
    richest record for a key, and the mutation of the returned rows with their
    score, rank and contributing arms.
    """

    def key_of(item: dict[str, Any]) -> str:
        return str(item.get("chunk_id") or item.get("node_id") or item.get("title") or "")

    arms: tuple[tuple[str, list[dict[str, Any]]], ...] = (
        ("text", text_results),
        ("vector", vector_results),
        ("graph", graph_results),
    )
    candidates = {
        arm: [
            fusion.Candidate(
                key=key_of(item),
                node_id=str(item.get("node_id") or ""),
                kind=str(item.get("kind") or ""),
            )
            for item in results
        ]
        for arm, results in arms
    }
    by_arm = dict(arms)
    merged = fusion.fuse(candidates, max_results=max_results, ranking=ranking)

    def record(item: Fused) -> dict[str, Any]:
        """The original dict the merge kept for this key."""

        return by_arm[item.arm][item.position]

    if collect is not None:
        # The fusion's own inputs, in the arms' own order: `(fusion key,
        # reporting identity, kind)` per candidate. This is what lets a tuning
        # trial re-fuse a query under different parameters **without running
        # retrieval again** — and it is three fields rather than one because
        # the merge keys on `chunk_id`, reports on `node_id`, and breaks ties
        # on `kind`. Recording only the identity would produce a re-fusion
        # that merged every chunk of a file into one entry, which is not what
        # the merge does and would make the cheap path measure something the
        # region never serves.
        #
        # Truncation is recorded rather than hidden: a re-fusion over a
        # truncated arm is an approximation, and the consumer refuses to run
        # on one instead of quietly reporting a number.
        collect["fusion_input"] = {
            arm: [
                [key_of(item), _identity(item), str(item.get("kind") or "")]
                for item in results[:EXPLAIN_MAX_IDS]
                if key_of(item)
            ]
            for arm, results in arms
        }
        collect["fusion_input_truncated"] = any(
            len(results) > EXPLAIN_MAX_IDS for _arm, results in arms
        )
        collect["max_results"] = max_results
        # The fused order *before* truncation. This is the one list that
        # separates "fusion ranked it 47th" from "fusion ranked it 11th and
        # max_results was 10" — two failures that look identical downstream
        # and have nothing in common: the first is a fusion or arm-weight
        # problem, the second is a caller asking for too few results.
        head = merged.ordered[:EXPLAIN_MAX_IDS]
        collect["fusion"] = {
            "ranked": [_identity(record(item)) for item in head if _identity(record(item))],
            "scores": {
                _identity(record(item)): round(item.score, 6)
                for item in head
                if _identity(record(item))
            },
            "contributors": {
                _identity(record(item)): list(item.contributors)
                for item in head
                if _identity(record(item))
            },
            "rrf_k": ranking.rrf_k,
            "arm_weights": {arm: ranking.arm_weight(arm) for arm in ("text", "vector", "graph")},
        }

    results: list[dict[str, Any]] = []
    for rank, item in enumerate(merged.selected, start=1):
        row = record(item)
        row["score"] = round(item.score, 6)
        row["retrieved_by"] = item.retrieved_by
        row["rank"] = rank
        results.append(row)
    return results


def _annotate_memory(
    results: list[dict[str, Any]],
    memory_index: dict[str, dict[str, Any]],
    policy: Any,
) -> None:
    """Mark which hits are remembered assertions rather than corpus content.

    Added only to hits that *are* memory records, so a corpus result keeps the
    exact shape it had before Step 33.6 — the same rule `heading_path` follows.

    This is not decoration. A caller that cannot tell a remembered assertion
    from a document has no way to weigh it differently, and the answering
    prompt and the UI both need the distinction to be explicit rather than
    inferred from a path that happens to start with a scope directory.
    """
    for item in results:
        record = resolve(item, memory_index)
        if record is None:
            continue
        item["memory"] = describe(record)
        provenance = item.get("provenance")
        if isinstance(provenance, dict):
            provenance["memory"] = True
    _ = policy


def _prefer_memory(
    text_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    graph_results: list[dict[str, Any]],
    memory_index: dict[str, dict[str, Any]],
    max_results: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Guarantee memory a share of the slots under `mode="prefer"`.

    Fusion ranks memory against a whole corpus, so on a large index a genuinely
    relevant memory can lose every slot to documents that merely match more
    words. `prefer` reserves up to half the results for memory hits by moving
    them to the front of their own arm, which is enough for RRF to keep them:
    their rank within the arm is what the fusion actually reads.

    Deliberately a *reordering*, not a score change. Inventing a boost would
    make the same query answer differently depending on a flag, in a way no
    caller could predict; promoting within an arm is bounded, explainable and
    leaves every other hit's relative order intact.
    """
    reserved = max(1, int(max_results * PREFER_SHARE))

    def promote(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        remembered = [r for r in results if resolve(r, memory_index) is not None]
        if not remembered:
            return results
        rest = [r for r in results if resolve(r, memory_index) is None]
        return remembered[:reserved] + rest + remembered[reserved:]

    return promote(text_results), promote(vector_results), promote(graph_results)
