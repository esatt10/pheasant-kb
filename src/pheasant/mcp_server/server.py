from __future__ import annotations

import functools
import json
import logging
from typing import Any

from pheasant.config.schema import PheasantConfig
from pheasant.graph.exporter import node_link
from pheasant.mcp_server.readiness_tools import register_readiness_tools
from pheasant.mcp_server.tools import PheasantTools
from pheasant.version import __version__

logger = logging.getLogger(__name__)


def create_mcp_tools(config: PheasantConfig) -> PheasantTools:
    """Return a tool facade usable by MCP adapters or direct tests.

    The optional official MCP SDK can wrap this facade at runtime; keeping the
    core implementation free of it makes CLI/API tests deterministic and lets a
    core-only install use the same tools over HTTP.
    """

    return PheasantTools(config)


def create_mcp_server(config: PheasantConfig) -> Any:
    """Create the official MCP SDK server around the pheasant tool facade."""

    server_class = _mcp_server_class()
    # `ToolError` and `ResourceError` are siblings, and each surface only
    # forwards the text of its own: a `ToolError` raised inside a resource
    # handler is an `UnexpectedResourceError` with the message stripped.
    anticipated = _anticipated_failures("tool")
    anticipated_resource = _anticipated_failures("resource")
    tools = create_mcp_tools(config)
    mcp = server_class(
        # Keep the name positional and everything else keyword: SDK 2.x
        # inserted `title` and `description` ahead of `instructions` in the
        # positional order, so a positional second argument now silently
        # lands in `title` and the server stops sending instructions at all.
        "pheasant",
        instructions=(
            "Use pheasant to sync configured knowledge sources, search indexed context, "
            "inspect graph relationships, and record or recall agent memory."
        ),
        # An unversioned SDK 2.x server reports an empty `serverInfo.version`
        # (1.x reported the SDK's own version, which was never pheasant's).
        # An agent logging which region answered it should see the release.
        version=__version__,
    )

    @mcp.tool()
    @anticipated
    def list_knowledge_bases() -> dict:
        """Return registered knowledge bases and their status."""

        return tools.list_knowledge_bases()

    @mcp.tool()
    @anticipated
    def register_source(
        knowledge_base: str,
        name: str,
        source_type: str,
        path: str,
        description: str | None = None,
        enabled: bool = True,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        taxonomy: bool = False,
        sync_now: bool = False,
        wait: bool = False,
        sync_mode: str = "incremental",
    ) -> dict:
        """Register a source path after allowlisted path validation.

        Set ``taxonomy`` for structured documentation (books, procedures,
        legal documents): each artifact's outline — Chapter / Article /
        Section / § / 1.2.3 / (a) — is then extracted on every sync, chunks
        are cut and labelled per section, and `heading` graph nodes are
        emitted. Off by default.
        """

        return tools.register_source(
            knowledge_base=knowledge_base,
            name=name,
            source_type=source_type,
            path=path,
            description=description,
            enabled=enabled,
            include=include,
            exclude=exclude,
            taxonomy=taxonomy,
            sync_now=sync_now,
            wait=wait,
            sync_mode=sync_mode,
        )

    @mcp.tool()
    @anticipated
    def start_sync_source(
        knowledge_base: str,
        source_name: str,
        mode: str = "incremental",
        max_depth: int | None = None,
        full_scan: bool = False,
    ) -> dict:
        """Start a source sync and return immediately with a progress job id."""

        return tools.start_sync_source(knowledge_base, source_name, mode, max_depth, full_scan)

    @mcp.tool()
    @anticipated
    def get_job(job_id: str) -> dict:
        """Inspect phase, counts, messages, and outcome of a background job."""

        return tools.get_job(job_id)

    @mcp.tool()
    @anticipated
    def list_jobs(active_only: bool = False, limit: int = 50) -> dict:
        """List recent background jobs, with active jobs first."""

        return tools.list_jobs(active_only, limit)

    @mcp.tool()
    @anticipated
    def list_sources(
        knowledge_base: str,
        enabled: bool | None = None,
        status: str | None = None,
        source_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """List sources with optional filters and pagination."""

        return tools.list_sources(knowledge_base, enabled, status, source_type, limit, offset)

    @mcp.tool()
    @anticipated
    def disable_source(knowledge_base: str, source_name: str) -> dict:
        """Disable a source without deleting its state."""

        return tools.disable_source(knowledge_base, source_name)

    @mcp.tool()
    @anticipated
    def remove_source(knowledge_base: str, source_name: str) -> dict:
        """Remove a source and its indexed state."""

        return tools.remove_source(knowledge_base, source_name)

    @mcp.tool()
    @anticipated
    def promote_runtime_source_to_config(
        knowledge_base: str,
        source_name: str,
        config_path: str | None = None,
        write: bool = False,
    ) -> dict:
        """Return a deterministic YAML patch or write a source into durable config."""

        return tools.promote_runtime_source_to_config(
            knowledge_base,
            source_name,
            config_path,
            write,
        )

    @mcp.tool()
    @anticipated
    def sync_source(
        knowledge_base: str,
        source_name: str,
        mode: str = "incremental",
        max_depth: int | None = None,
        full_scan: bool = False,
    ) -> dict:
        """Sync one configured source.

        A source that would exceed its configured size limits returns
        ``status: "limit_exceeded"`` and indexes nothing — call
        ``scan_source`` first to see the shape, then either narrow it with
        ``max_depth`` or re-run with ``full_scan=True``.
        """

        return tools.sync_source(
            knowledge_base, source_name, mode, max_depth=max_depth, full_scan=full_scan
        )

    @mcp.tool()
    @anticipated
    def scan_source(
        knowledge_base: str,
        source_name: str,
        max_depth: int | None = None,
    ) -> dict:
        """Estimate what a source would index, without indexing anything."""

        return tools.scan_source(knowledge_base, source_name, max_depth=max_depth)

    @mcp.tool()
    @anticipated
    def memory_write(
        knowledge_base: str,
        text: str,
        scope: str = "user",
        subject: str | None = None,
        supersedes: str | None = None,
        tags: list[str] | None = None,
        sync: bool = True,
        kind: str = "fact",
        principal: str | None = None,
        valid_until: str | None = None,
        session: str | None = None,
    ) -> dict:
        """Persist one agent-memory record (session/user/org scope) and index it.

        The memory becomes retrievable via search_context immediately when
        sync=true (read-your-writes). Requires a `type: memory` source.

        principal records who asserted the memory; pass it whenever you know
        the caller's identity, since it is what keeps one agent's memories
        distinct from another's. kind marks a record as retrieval policy
        (alias/preference/exclusion) instead of a fact. valid_until gives the
        record an expiry it declares itself.

        session identifies the conversation this call belongs to. It is
        recorded, never enforced -- pass a stable opaque string and the
        region can keep one refined memory per session; omit it and nothing
        changes. Like principal, it is asserted by you and verified by
        nobody.
        """

        return tools.memory_write(
            knowledge_base,
            text,
            scope,
            subject,
            supersedes,
            tags,
            sync,
            kind,
            principal,
            valid_until,
        )

    @mcp.tool()
    @anticipated
    def list_memory_candidates(
        knowledge_base: str,
        status: str = "pending",
        rule_id: str | None = None,
        principal: str | None = None,
        max_results: int = 50,
    ) -> dict:
        """List memory this region has proposed but nothing has admitted yet.

        These are NOT memories: nothing here is retrievable, and nothing
        becomes retrievable until it is promoted. Each carries the evidence
        behind it (which rule, how many observations, across how many
        sessions) -- read that before promoting. A proposal is a suggestion,
        not a finding.
        """

        return tools.list_memory_candidates(knowledge_base, status, rule_id, principal, max_results)

    @mcp.tool()
    @anticipated
    def promote_memory_candidate(
        knowledge_base: str, candidate_id: str, principal: str | None = None
    ) -> dict:
        """Admit one proposed memory, making it an ordinary record.

        It is written through the same path memory_write uses and indexed
        identically, so it competes on the same ranking as anything else.
        """

        return tools.promote_memory_candidate(knowledge_base, candidate_id, principal)

    @mcp.tool()
    @anticipated
    def reject_memory_candidate(
        knowledge_base: str, candidate_id: str, principal: str | None = None
    ) -> dict:
        """Decline one proposed memory, permanently.

        The rule that proposed it will not suggest it again.
        """

        return tools.reject_memory_candidate(knowledge_base, candidate_id, principal)

    @mcp.tool()
    @anticipated
    def memory_consolidate(knowledge_base: str) -> dict:
        """Archive superseded/expired memory records and re-index the memory source."""

        return tools.memory_consolidate(knowledge_base)

    @mcp.tool()
    @anticipated
    def memory_synthesize(knowledge_base: str) -> dict:
        """LLM-merge a near-duplicate memory cluster deterministic compaction
        could not resolve into one canonical record. Off by default
        (`memory.synthesis.enabled`) and never automatic."""

        return tools.memory_synthesize(knowledge_base)

    @mcp.tool()
    @anticipated
    def sync_all(
        knowledge_base: str,
        mode: str = "incremental",
        max_depth: int | None = None,
        full_scan: bool = False,
    ) -> dict:
        """Sync all enabled configured sources."""

        return tools.sync_all(knowledge_base, mode, max_depth=max_depth, full_scan=full_scan)

    @mcp.tool()
    @anticipated
    def search_context(  # noqa: PLR0913 - additive principal params (32.2)
        knowledge_base: str,
        query: str,
        mode: str = "hybrid",
        max_results: int = 10,
        include_chunks: bool = True,
        include_graph_neighbors: bool = True,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        session: str | None = None,
        section: str | None = None,
        source_name: str | None = None,
        exclude_sources: list[str] | None = None,
        node_types: list[str] | None = None,
        min_score: float | None = None,
        memory: dict | str | None = None,
        source_types: list[str] | None = None,
        exclude_source_types: list[str] | None = None,
    ) -> dict:
        """Search indexed context and return compact results with provenance.

        principal/principal_groups scope results to what that caller may see
        when security.acl_enforced is on (Step 32.2); ignored otherwise.

        session identifies the conversation this call belongs to. It is
        recorded, never enforced -- pass a stable opaque string and the
        region can keep one refined memory per session; omit it and nothing
        changes. Like principal, it is asserted by you and verified by
        nobody.

        section restricts results to one part of a document's extracted
        taxonomy, matched against the breadcrumb — "§ 12.3", "Article IV" or a
        section's wording all work, and naming a parent returns everything
        nested under it. Only meaningful for sources with taxonomy enabled.

        source_types/exclude_source_types scope by the kind of source —
        repository, notion, slack, markdown_folder — rather than by name, which
        is usually what you want when you do not already know every source in
        the region. Every hit reports its own under provenance.source_type.

        source_name/exclude_sources/node_types/min_score are retrieval
        criteria you can set per call instead of relying on how the region
        was configured. Call describe_retrieval to see what this region
        offers, and preview_retrieval to compare criteria against the
        standing configuration before committing to them.

        memory controls how this region's agent memory takes part: one of
        "auto" (default), "off", "only", "prefer", or an object such as
        {"scopes": ["user"], "subject": "deploy", "as_of": "2026-01-01T00:00:00Z"}.
        Records a later record corrected are excluded by default; pass an
        as_of instant to ask what was believed at that time. Results that came
        from memory carry a "memory" block naming the record and its scope.
        """

        return tools.search_context(
            knowledge_base,
            query,
            mode,
            max_results,
            include_chunks,
            include_graph_neighbors,
            principal=principal,
            principal_groups=principal_groups,
            section=section,
            source_name=source_name,
            exclude_sources=exclude_sources,
            node_types=node_types,
            min_score=min_score,
            memory=memory,
            source_types=source_types,
            exclude_source_types=exclude_source_types,
        )

    @mcp.tool()
    @anticipated
    def describe_retrieval(knowledge_base: str) -> dict:
        """Report how this knowledge base retrieves, and what you can override.

        Returns the standing configuration (default mode, result count,
        retrieval rounds and depth), which search modes actually work here
        (semantic search is only offered when a vector index exists), the
        sources present, and the criteria each retrieval tool accepts per
        call. Call this before guessing at parameters for an unfamiliar
        region.
        """

        return tools.describe_retrieval(knowledge_base)

    @mcp.tool()
    @anticipated
    def preview_retrieval(  # noqa: PLR0913 - mirrors search_context's criteria
        knowledge_base: str,
        query: str,
        mode: str = "hybrid",
        max_results: int = 10,
        source_name: str | None = None,
        exclude_sources: list[str] | None = None,
        node_types: list[str] | None = None,
        min_score: float | None = None,
        memory: dict | str | None = None,
        source_types: list[str] | None = None,
        exclude_source_types: list[str] | None = None,
    ) -> dict:
        """Try retrieval criteria and see how they differ from the configuration.

        Runs the given criteria and the region's configured retrieval over the
        same query, then reports both result sets and the delta (added,
        dropped, kept). Use it to test a setting against real content before
        anyone writes it into pheasant.yaml. Read-only — nothing is persisted.
        """

        return tools.preview_retrieval(
            knowledge_base,
            query,
            mode=mode,
            max_results=max_results,
            source_name=source_name,
            exclude_sources=exclude_sources,
            node_types=node_types,
            min_score=min_score,
            memory=memory,
            source_types=source_types,
            exclude_source_types=exclude_source_types,
        )

    @mcp.tool()
    @anticipated
    def ask_knowledge_base(  # noqa: PLR0913 - mirrors the HTTP surface
        knowledge_base: str,
        question: str,
        workflow: str | None = None,
        mode: str = "hybrid",
        max_results: int = 8,
        source_name: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        session: str | None = None,
        options: dict | None = None,
        source_types: list[str] | None = None,
        exclude_source_types: list[str] | None = None,
    ) -> dict:
        """Answer a question from the knowledge base, with citations and graph facts.

        Runs the configured agent workflow over pheasant's own search. Use
        this for a synthesized, cited answer; use search_context when you
        want the raw passages to reason over yourself.

        session identifies the conversation this call belongs to. It is
        recorded, never enforced -- pass a stable opaque string and the
        region can keep one refined memory per session; omit it and nothing
        changes. Like principal, it is asserted by you and verified by
        nobody.
        """

        return tools.ask_knowledge_base(
            knowledge_base,
            question,
            workflow=workflow,
            mode=mode,
            max_results=max_results,
            source_name=source_name,
            principal=principal,
            principal_groups=principal_groups,
            options=options,
            source_types=source_types,
            exclude_source_types=exclude_source_types,
        )

    @mcp.tool()
    @anticipated
    def get_relevant_files(
        knowledge_base: str,
        task: str,
        source_name: str | None = None,
        max_files: int = 8,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        session: str | None = None,
    ) -> dict:
        """Return files likely needed for a coding or research task.

        session identifies the conversation this call belongs to. It is
        recorded, never enforced -- pass a stable opaque string and the
        region can keep one refined memory per session; omit it and nothing
        changes. Like principal, it is asserted by you and verified by
        nobody.
        """

        return tools.get_relevant_files(
            knowledge_base,
            task,
            source_name,
            max_files,
            principal=principal,
            principal_groups=principal_groups,
        )

    @mcp.tool()
    @anticipated
    def get_graph_neighbors(
        knowledge_base: str,
        node_id: str,
        depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> dict:
        """Return graph neighbors around a node."""

        return tools.get_graph_neighbors(knowledge_base, node_id, depth, edge_types)

    @mcp.tool()
    @anticipated
    def get_file_summary(
        knowledge_base: str,
        path: str,
        source_name: str | None = None,
    ) -> dict:
        """Return summary and provenance for one indexed file."""

        return tools.get_file_summary(knowledge_base, path, source_name)

    @mcp.tool()
    @anticipated
    def get_repo_map(knowledge_base: str, source_name: str, depth: int = 3) -> dict:
        """Return a compact repository map for one source."""

        return tools.get_repo_map(knowledge_base, source_name, depth)

    @mcp.tool()
    @anticipated
    def explain_node(knowledge_base: str, node_id: str) -> dict:
        """Explain what an indexed graph node represents."""

        return tools.explain_node(knowledge_base, node_id)

    @mcp.tool()
    @anticipated
    def get_sync_status(knowledge_base: str) -> dict:
        """Return source freshness and last sync status."""

        return tools.get_sync_status(knowledge_base)

    @mcp.tool()
    @anticipated
    def get_contract(knowledge_base: str) -> dict:
        """Return this region's published Synapse semantic contract."""

        return tools.get_contract(knowledge_base)

    @mcp.tool()
    @anticipated
    def get_sync_history(
        knowledge_base: str,
        source_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """Return runtime source lifecycle history."""

        return tools.get_sync_history(knowledge_base, source_name, limit, offset)

    # The readiness plane's tools register from their own module: they are one
    # bounded context, and this file is on the module ratchet. `mcp` and the
    # `anticipated` decorator are handed over rather than imported there, so
    # the registration is identical to the ones above it.
    register_readiness_tools(mcp, tools, anticipated)

    @mcp.tool()
    @anticipated
    def record_evidence(
        knowledge_base: str,
        query: str,
        target_id: str,
        event_type: str,
        target_type: str = "artifact",
        principal: str | None = None,
        session_id: str | None = None,
        position: int | None = None,
        outcome_reference: str | None = None,
    ) -> dict:
        """Record what came of a result: cited, selected, accepted, rejected, validated.

        Being served is not evidence of usefulness and not selecting something
        is not evidence against it, so reporting nothing lowers the
        evaluation's coverage rather than corrupting it. See
        `pheasant://evaluation/taxonomy` for the event types.
        """

        return tools.record_evidence(
            knowledge_base,
            query,
            target_id,
            event_type,
            target_type,
            principal,
            session_id,
            position,
            outcome_reference,
        )

    @mcp.tool()
    @anticipated
    def start_evaluation(
        knowledge_base: str,
        mode: str = "current_state",
        as_of: str | None = None,
        force: bool = False,
    ) -> dict:
        """Start a knowledge-effectiveness batch; returns a job id, not a report.

        Minutes of work, so it runs in the background. Poll
        `get_evaluation_status` — progress lives in `/state`, so it survives a
        restart and is readable from any process.
        """

        return tools.start_evaluation(knowledge_base, mode, as_of, force)

    @mcp.tool()
    @anticipated
    def get_evaluation_status(knowledge_base: str, run_id: str | None = None) -> dict:
        """How far an evaluation batch has got: phase, units done, attempts, status."""

        return tools.get_evaluation_status(knowledge_base, run_id)

    @mcp.tool()
    @anticipated
    def get_evaluation_report(knowledge_base: str, run_id: str | None = None) -> dict:
        """Return the latest knowledge-effectiveness report, with its gates and limits."""

        return tools.get_evaluation_report(knowledge_base, run_id)

    @mcp.tool()
    @anticipated
    def start_retrieval_tuning(
        knowledge_base: str,
        diagnose_only: bool = False,
        apply: bool = False,
        force: bool = False,
    ) -> dict:
        """Find which step of retrieval is failing; returns a job id, not a report.

        `diagnose_only=true` attributes every miss to the stage that lost it
        and proposes nothing — the right first call, because it can tell you
        the failures are somewhere no retrieval parameter reaches. `apply` lets
        a gated winner change the fleet's ranking; off by default.
        """

        return tools.start_retrieval_tuning(knowledge_base, diagnose_only, apply, force)

    @mcp.tool()
    @anticipated
    def get_retrieval_tuning_status(knowledge_base: str, experiment_id: str | None = None) -> dict:
        """How far a tuning batch has got: phase, units done, attempts, status."""

        return tools.get_retrieval_tuning_status(knowledge_base, experiment_id)

    @mcp.tool()
    @anticipated
    def get_retrieval_diagnosis(knowledge_base: str, experiment_id: str | None = None) -> dict:
        """Where retrieval loses documents, by pipeline stage, and what a parameter can reach."""

        return tools.get_retrieval_diagnosis(knowledge_base, experiment_id)

    @mcp.tool()
    @anticipated
    def get_retrieval_parameters(knowledge_base: str) -> dict:
        """What this region ranks with, whether a tuning bundle is in force, and what is tunable."""

        return tools.get_retrieval_parameters(knowledge_base)

    @mcp.tool()
    @anticipated
    def list_tuning_bundles(knowledge_base: str, limit: int = 20) -> dict:
        """Configuration bundles this region has produced, and which one is live."""

        return tools.list_tuning_bundles(knowledge_base, limit)

    @mcp.tool()
    @anticipated
    def apply_tuning_bundle(knowledge_base: str, bundle_id: str, applied_by: str = "mcp") -> dict:
        """Make a bundle this region's live retrieval overlay, for every replica."""

        return tools.apply_tuning_bundle(knowledge_base, bundle_id, applied_by)

    @mcp.tool()
    @anticipated
    def rollback_tuning_bundle(knowledge_base: str, to: str = "base") -> dict:
        """Roll the retrieval overlay back to the configured base, or to an earlier bundle."""

        return tools.rollback_tuning_bundle(knowledge_base, to)

    @mcp.tool()
    @anticipated
    def explain_retrieval_measures(knowledge_base: str, term: str | None = None) -> dict:
        """What every tuning measure means, and the misreading each one invites.

        Call this before acting on a number from a tuning report or health
        payload — several of them look alarming and are normal.
        """

        return tools.explain_retrieval_measures(knowledge_base, term)

    @mcp.tool()
    @anticipated
    def get_tuning_objective(knowledge_base: str) -> dict:
        """What "better" means here, and what optimizing it accepts getting worse."""

        return tools.get_tuning_objective(knowledge_base)

    @mcp.tool()
    @anticipated
    def get_retrieval_lineage(knowledge_base: str, limit: int = 50) -> dict:
        """Every retrieval configuration this region has served: what changed, when, and why."""

        return tools.get_retrieval_lineage(knowledge_base, limit)

    @mcp.tool()
    @anticipated
    def get_retrieval_health(knowledge_base: str, since: str | None = None) -> dict:
        """How retrieval is behaving on live traffic: empty rate by stage, arm contribution.

        Needs no cohort and no proof, so it works between tuning batches — but
        it says what the pipeline did, never whether an answer was correct.
        """

        return tools.get_retrieval_health(knowledge_base, since)

    @mcp.tool()
    @anticipated
    def cancel_retrieval_tuning(knowledge_base: str, experiment_id: str | None = None) -> dict:
        """Ask the running tuning batch to stop. Resumable: its trials are already stored."""

        return tools.cancel_retrieval_tuning(knowledge_base, experiment_id)

    @mcp.tool()
    @anticipated
    def pin_retrieval_parameters(knowledge_base: str, pinned: list[str]) -> dict:
        """Settle ranking parameters so no tuning batch proposes them again."""

        return tools.pin_retrieval_parameters(knowledge_base, pinned)

    @mcp.tool()
    @anticipated
    def list_tuning_trials(
        knowledge_base: str, experiment_id: str | None = None, limit: int = 50
    ) -> dict:
        """Every trial in an experiment: the point, its score, its stage, and its rationale."""

        return tools.list_tuning_trials(knowledge_base, experiment_id, limit)

    @mcp.resource("pheasant://knowledge-bases")
    @anticipated_resource
    def knowledge_bases_resource() -> str:
        """Return registered knowledge bases as JSON."""

        return _json(tools.list_knowledge_bases())

    @mcp.resource("pheasant://evaluation/taxonomy")
    @anticipated_resource
    def evaluation_taxonomy_resource() -> str:
        """Return the evidence taxonomy as JSON: what each event type licenses."""

        return _json(tools.get_evaluation_taxonomy(""))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/sources")
    @anticipated_resource
    def sources_resource(kb_id: str) -> str:
        """Return configured source status for a knowledge base as JSON."""

        return _json(tools.get_sync_status(kb_id))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/graph")
    @anticipated_resource
    def graph_resource(kb_id: str) -> str:
        """Return the current graph snapshot as JSON."""

        tools._require_knowledge_base(kb_id)
        return _json(node_link(tools.graph))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/sources/{source_id}/manifest")
    @anticipated_resource
    def source_manifest_resource(kb_id: str, source_id: str) -> str:
        """Return the manifest for one source as JSON."""

        return _json(tools.get_source_manifest(kb_id, source_id))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/sources/{source_id}/history")
    @anticipated_resource
    def source_history_resource(kb_id: str, source_id: str) -> str:
        """Return lifecycle history for one source as JSON."""

        return _json(tools.get_sync_history(kb_id, source_id))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/sync-history")
    @anticipated_resource
    def sync_history_resource(kb_id: str) -> str:
        """Return lifecycle history for a knowledge base as JSON."""

        return _json(tools.get_sync_history(kb_id))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/sources/{source_id}/repo-map")
    @anticipated_resource
    def source_repo_map_resource(kb_id: str, source_id: str) -> str:
        """Return a repository map resource for one source as JSON."""

        return _json(tools.get_repo_map(kb_id, source_id))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/nodes/{node_id}")
    @anticipated_resource
    def node_resource(kb_id: str, node_id: str) -> str:
        """Return an explanation for one graph node as JSON."""

        return _json(tools.explain_node(kb_id, node_id))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/graph-slices/{node_id}")
    @anticipated_resource
    def graph_slice_resource(kb_id: str, node_id: str) -> str:
        """Return a small graph slice rooted at one node as JSON."""

        return _json(tools.get_graph_slice(kb_id, node_id, depth=2))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/memory")
    @anticipated_resource
    def memory_resource(kb_id: str) -> str:
        """Return this region's current agent-memory records as JSON.

        A *resource* rather than only a tool because "what does this region
        remember" is context to read, not an action to take — an agent can
        pull it into a conversation the way it pulls a file, without spending
        a tool call on a search that may or may not surface the record it
        needs. Corrected records are excluded; use `search_context` with an
        `as_of` instant to see what was believed at a past time.
        """

        return _json(tools.memory_list(kb_id, current_only=True))

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/contract")
    @anticipated_resource
    def contract_resource(kb_id: str) -> str:
        """Return this region's published Synapse semantic contract as JSON."""

        return _json(tools.get_contract(kb_id))

    @mcp.prompt()
    def use_pheasant_for_coding_task(task: str = "") -> str:
        """Guide an agent through a pheasant-backed coding workflow."""

        suffix = f"\nTask: {task}" if task else ""
        return (
            "Call get_relevant_files for the task, inspect returned provenance, make the "
            "smallest safe change, run checks, then call sync_source with mode=incremental."
            f"{suffix}"
        )

    @mcp.prompt()
    def use_pheasant_for_document_research(query: str = "") -> str:
        """Guide an agent through provenance-first document research."""

        suffix = f"\nQuery: {query}" if query else ""
        return (
            "Use search_context first. Prefer chunks with explicit provenance, avoid claims "
            "beyond retrieved evidence, and call get_graph_neighbors for related material."
            f"{suffix}"
        )

    return mcp


# The SDK's own allow-list for a server bound to loopback. Kept as the floor
# pheasant only ever widens from, so a config that never mentions CORS behaves
# exactly as the SDK intends.
_LOOPBACK_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
_LOOPBACK_ORIGINS = ("http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*")


def _mcp_server_class() -> Any:
    """Import the SDK server class, or explain which half of it is missing.

    Two failures look identical from a traceback and need opposite fixes: the
    ``[mcp]`` extra was never installed, or it is installed at 1.x, where the
    class was ``mcp.server.fastmcp.FastMCP``. 2.x renamed it to
    ``mcp.server.mcpserver.MCPServer`` and left the old module raising
    ``ModuleNotFoundError`` deliberately, so distinguish the two here rather
    than let a 1.x install read as "no MCP runtime".
    """

    try:
        from mcp.server.mcpserver import MCPServer
    except ModuleNotFoundError as exc:
        try:
            import mcp  # noqa: F401
        except ModuleNotFoundError:
            raise RuntimeError(
                "The MCP runtime is not installed. Install pheasant with the mcp extra "
                "or use the Docker image, which includes it."
            ) from exc
        raise RuntimeError(
            "pheasant needs MCP SDK 2.x (mcp>=2.1,<3); the installed mcp package is 1.x, "
            "where the server class was still FastMCP. Reinstall pheasant with the mcp "
            "extra to pick it up."
        ) from exc
    return MCPServer


def _anticipated_failures(surface: str) -> Any:
    """A decorator that lets a refusal's own reason reach the agent.

    SDK 2.x sorts a handler's exceptions into two buckets: ``ToolError`` /
    ``ResourceError`` are deliberate refusals whose text is appended to what
    the client sees, and *everything else* is a crash, reported as a bare
    "Error executing tool <name>" with the exception's text kept on the
    server. That default is right for a crash and wrong for pheasant, which
    refuses deliberately and informatively — "Unknown knowledge base: x",
    "Unknown source: y", "Path ... is outside allowed roots" — by raising
    plain ``ValueError``/``KeyError``. 1.x appended every exception's text
    regardless, so porting without this blanks the reason on every refusal
    across the whole tool surface, and an agent that mistypes a source name
    is told only that something went wrong.

    The translation lives here, at the SDK boundary, because
    ``PheasantTools`` is also the HTTP surface's facade and must stay free of
    the MCP SDK. ``PathPolicyError`` subclasses ``ValueError``, so these two
    types cover every deliberate raise in the facade; anything else stays a
    crash and keeps its text off the wire.

    ``surface`` picks which of the two the wrapper raises: the tool and
    resource paths each forward only their own exception type, so the wrong
    one is stripped exactly like a crash.

    It is also where the MCP surface is **observed**, when observation is on.
    This decorator already wraps every tool and every resource and already
    sees each call's arguments and its return value, so instrumenting here
    covers the whole surface in one place instead of 27 -- and it keeps
    ``PheasantTools`` free of any of it, exactly as it is free of the SDK.
    """

    from mcp.server.mcpserver.exceptions import ResourceError, ToolError

    deliberate = ToolError if surface == "tool" else ResourceError

    def decorate(fn):
        operation = getattr(fn, "__name__", surface)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            def run():
                try:
                    return fn(*args, **kwargs)
                except (ValueError, KeyError) as exc:
                    # `str(KeyError("x"))` is `"'x'"`; the message reads
                    # better to a model without the quoting `repr` adds.
                    message = (
                        exc.args[0]
                        if isinstance(exc, KeyError) and exc.args and isinstance(exc.args[0], str)
                        else str(exc)
                    )
                    raise deliberate(message) from exc

            buffer = _interaction_buffer()
            if buffer is None:
                return run()
            return _observed(buffer, operation, kwargs, run)

        return wrapper

    return decorate


def _interaction_buffer() -> Any:
    """The process buffer, if observation is on. Never raises, never imports
    the module when it is off."""

    try:
        from pheasant.telemetry.interactions import process_buffer

        return process_buffer()
    except Exception:  # noqa: BLE001 - observation must never break a tool call
        return None


def _observed(buffer: Any, operation: str, kwargs: dict[str, Any], run: Any) -> Any:
    """Wrap one tool call in an observation.

    ``principal`` and ``session`` are ordinary tool arguments the model
    supplies, which is the same trust level ``principal`` already has
    everywhere else in pheasant: asserted, never verified. The MCP transport
    is ``stateless_http=True`` by design, so there is no server-side session
    to read them from and there deliberately is not going to be one.
    """

    from pheasant.telemetry.interactions import (
        InteractionContext,
        Modality,
        extract_results,
        observe,
    )

    context = InteractionContext.create(
        Modality.MCP,
        principal=kwargs.get("principal"),
        session_id=kwargs.get("session"),
        client_id=kwargs.get("client_id"),
    )
    with observe(
        buffer,
        context,
        kb_id=str(kwargs.get("knowledge_base") or ""),
        operation=operation,
    ) as event:
        query = kwargs.get("query")
        event.query_text = str(query) if isinstance(query, str) else None
        event.criteria = {
            key: value
            for key, value in kwargs.items()
            if key
            in (
                "mode",
                "source_name",
                "exclude_sources",
                "node_types",
                "min_score",
                "section",
                "source_types",
                "exclude_source_types",
                "max_results",
            )
            and value is not None
        } or None
        result = run()
        # The same extractor the HTTP surface uses. Two implementations would
        # mean a rule mining different evidence depending on which surface a
        # caller happened to be on.
        ids, paths, top, count = extract_results(result)
        event.result_ids, event.result_paths = ids, paths
        event.top_score = top
        event.result_count = count
        return result


def _transport_security(config: PheasantConfig) -> Any:
    """Build the DNS-rebinding guard from pheasant's own CORS policy.

    Two failures sit either side of this function, and the SDK's default hits
    one or the other depending on the bind address — never the thing pheasant
    wants.

    Too narrow: the SDK's own allow-list is ``127.0.0.1``/``localhost``/
    ``[::1]`` only, which is right for a laptop and wrong for the container
    this normally runs in. Reach the very same server as
    ``http://pheasant:8765`` or by LAN IP and every MCP request answers
    **421 Misdirected Request**.

    Too wide: the SDK only reaches for that allow-list when the bind address
    is loopback — a decision 1.x took from the constructor's own default (so
    pheasant got the guard whatever it bound) and 2.x takes in
    ``streamable_http_app()``/``run()`` from the real one. pheasant binds
    ``0.0.0.0``, so the 2.x default is no guard at all: every host admitted,
    in every container deployment, with nothing raised to say so. Passing
    these settings explicitly is what keeps the guard on.

    Between the two, rather than invent a second allow-list, this derives one
    from ``server.api.cors_origins`` — the knob an operator already uses to
    say who may reach this API — and honours ``cors_allow_all_origins`` as the
    same documented escape hatch it already is. The SDK's loopback entries are
    kept alongside, so this only ever *widens* to hosts the operator already
    admitted, and a config that never mentions CORS behaves as the SDK
    intends for a laptop.
    """
    from urllib.parse import urlsplit

    from mcp.server.transport_security import TransportSecuritySettings

    api = config.server.api
    if getattr(api, "cors_allow_all_origins", False):
        # The operator has declared this API open (their own authenticating
        # ingress fronts it). Refusing MCP on host grounds here would be
        # inconsistent with every other route on the same port.
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = list(_LOOPBACK_HOSTS)
    origins = list(_LOOPBACK_ORIGINS)
    for origin in api.cors_origins or []:
        parsed = urlsplit(origin)
        if not parsed.netloc:
            continue
        if parsed.netloc not in hosts:
            hosts.append(parsed.netloc)
        if origin not in origins:
            origins.append(origin)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def _streamable_http_options(config: PheasantConfig) -> dict[str, Any]:
    """Streamable-HTTP transport options, in one place for both entry points.

    SDK 2.x moved these off the server constructor and deleted the settings
    object that used to carry them, so the mounted ASGI app and
    ``pheasant mcp --transport streamable-http`` have to be handed the same
    dict or they drift apart.

    ``stateless_http`` is by design, and load-bearing for horizontal scale
    (Phase 35.6): with no per-session server state, two requests from one
    agent may land on different replicas and both answer correctly. A sticky
    session would make the replica count a lie. Pinned by a test.
    """
    return {
        "json_response": True,
        "stateless_http": True,
        "transport_security": _transport_security(config),
        "host": config.server.host,
    }


def streamable_http_app(config: PheasantConfig) -> Any:
    """The MCP streamable-HTTP ASGI app, for mounting inside the API server.

    ``pheasant serve`` advertises ``streamable_http_url: <base>/mcp`` from
    ``GET /mcp/info`` whenever ``server.mcp.transports.streamable_http`` is on
    (the default) — but it only ever built the FastAPI app, so that URL 404'd
    and a client POSTing to it got a 405. Found by pointing a real MCP client
    at a live container: the server was telling every agent to connect
    somewhere it did not listen.

    Returns None when the transport is disabled or the installed ``mcp``
    package cannot build the app, so the caller mounts nothing and the API
    behaves exactly as it did.
    """
    if not config.server.mcp.enabled:
        return None
    if not config.server.mcp.transports.get("streamable_http", False):
        return None
    try:
        mcp = create_mcp_server(config)
        # The route inside the SDK's app becomes the *mount* point's suffix,
        # so its internal path has to be "/" — leaving it at "/mcp" while
        # mounting at "/mcp" would serve "/mcp/mcp". Mounting the app at "/"
        # instead would put an ASGI catch-all ahead of the UI's static files.
        return mcp.streamable_http_app(
            streamable_http_path="/",
            **_streamable_http_options(config),
        )
    except Exception:  # pragma: no cover - depends on the installed mcp version
        logger.warning("MCP streamable-http app unavailable; /mcp not mounted", exc_info=True)
        return None


def run_mcp_server(config: PheasantConfig, transport: str = "stdio") -> None:
    """Run the MCP server with the selected transport.

    Every transport parameter is passed here rather than to the constructor:
    SDK 2.x takes them on ``run()``, per transport, and raises ``TypeError``
    on a keyword the chosen transport does not accept.
    """

    mcp = create_mcp_server(config)
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    if transport == "sse":
        mcp.run(
            transport="sse",
            host=config.server.host,
            port=config.server.port,
            sse_path="/sse",
            transport_security=_transport_security(config),
        )
        return
    mcp.run(
        transport=transport,
        port=config.server.port,
        streamable_http_path="/mcp",
        **_streamable_http_options(config),
    )


def _json(payload: Any) -> str:
    return json.dumps(payload, default=str, indent=2, sort_keys=True)
