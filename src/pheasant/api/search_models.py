"""The request bodies of `/search` and `/search/batch`, and their one mapping
onto the service layer's `SearchRequest`.

Split out of `api/app.py`, which sits at its size ceiling. The two bodies share
`SearchCriteria` so that a criterion added for one search is a criterion the
batch honours, and the field-by-field mapping onto the service request exists
once rather than once per route.
"""

from __future__ import annotations

from pydantic import BaseModel

from pheasant.services import retrieval as retrieval_service


class SearchCriteria(BaseModel):
    """Everything a search is scoped by. `/search` adds one query to it and
    `/search/batch` many, so a criterion added here reaches both."""

    # Step 32.2 — optional caller identity; enforced only when
    # security.acl_enforced is on. The caller (router / deployment
    # perimeter) authenticates; the region enforces visibility.
    principal: str | None = None
    principal_groups: list[str] = []
    knowledge_base: str | None = None
    mode: str = "hybrid"
    max_results: int = 10
    source_name: str | None = None
    # Restrict to one part of a document's extracted taxonomy, matched against
    # the heading breadcrumb. Only meaningful for sources with taxonomy on.
    section: str | None = None
    # Step 33.6 — the same retrieval criteria the MCP tool has always had.
    # They lived only on the MCP surface, so the same region answered a query
    # differently depending on which protocol asked; the router, which reaches
    # this region over HTTP, could not scope a search at all.
    exclude_sources: list[str] | None = None
    node_types: list[str] | None = None
    min_score: float | None = None
    # Scope by the *kind* of source (repository, notion, slack, ...) rather
    # than by name. A caller that does not already know every source in the
    # region can still say "only our wikis" or "nothing from git". Each hit
    # reports its own under `provenance.source_type`.
    source_types: list[str] | None = None
    exclude_source_types: list[str] | None = None
    # How this region's agent memory takes part: "auto" (default), "off",
    # "only", "prefer", or an object with scopes/subject/current_only/as_of.
    memory: dict | str | None = None
    # Pin this search to a sealed snapshot. The region verifies it still
    # stands there and refuses with SNAPSHOT_DRIFTED if it does not — it holds
    # one version of its corpus, so the guarantee is that two runs naming one
    # snapshot cannot silently have seen different corpora.
    snapshot_id: str | None = None
    # The instant memory validity is evaluated at, echoed into the lineage
    # even where the region holds no memory — an arm that ran with memory off
    # has to be able to record that it did.
    as_of: str | None = None
    # The caller's correlation id, echoed so a result joins to the ledger row
    # and the span that produced it.
    trace_id: str | None = None

    def service_request(self, query: str) -> retrieval_service.SearchRequest:
        return retrieval_service.SearchRequest(
            query=query,
            knowledge_base=self.knowledge_base,
            mode=self.mode,
            max_results=self.max_results,
            source_name=self.source_name,
            section=self.section,
            principal=self.principal,
            principal_groups=self.principal_groups,
            memory=self.memory,
            exclude_sources=self.exclude_sources,
            node_types=self.node_types,
            min_score=self.min_score,
            source_types=self.source_types,
            exclude_source_types=self.exclude_source_types,
            snapshot_id=self.snapshot_id,
            as_of=self.as_of,
            trace_id=self.trace_id,
        )


class SearchRequest(SearchCriteria):
    query: str


class BatchSearchRequest(SearchCriteria):
    """Up to 25 queries under one set of criteria; see `services.retrieval.search_batch`."""

    queries: list[str]
    # Each query's own payload under `searches`. Off to receive only the
    # merged, deduplicated `results`.
    per_query: bool = True
