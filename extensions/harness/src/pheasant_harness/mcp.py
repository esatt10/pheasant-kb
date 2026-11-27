"""MCP adapter: the same service functions the HTTP routes call."""

from __future__ import annotations

from typing import Any, Literal

from pheasant_harness import service
from pheasant_harness.models import (
    ApprovalRequest,
    ConnectionPolicyRequest,
    DiscoverRequest,
    GrepRequest,
    ReadRequest,
    RegisterRequest,
    ResumeRequest,
    RunRequest,
    StartRequest,
)


def register_harness_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
    services = tools.services

    @mcp.tool()
    @anticipated
    def harness_contract(knowledge_base: str) -> dict:
        """What this agentic runtime supports: tools, workloads and their definitions of done,
        delivery adapters and what each can promise, resolvers, sandbox isolation and run states."""
        return service.contract(services, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_register(knowledge_base: str, request: RegisterRequest) -> dict:
        """Register a place to look: file:// (a folder or git checkout under the region's allowed
        roots) or pheasant://<source>/<prefix>. Records a pointer only; reading it needs a grant."""
        return service.register(services, request, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_list_registrations(knowledge_base: str) -> dict:
        """List registrations and whether you may read each."""
        return service.registrations(services, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_resolve(
        knowledge_base: str, registration_id: str, version_selector: str | None = None
    ) -> dict:
        """Pin a registration to a revision (commit, snapshot or manifest digest) you can cite."""
        return service.resolve(services, registration_id, version_selector, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_set_grant(
        knowledge_base: str, registration_id: str, principal: str, granted: bool = True
    ) -> dict:
        """Grant or revoke one principal's access to a registration. Revocation applies to resumes,
        caches and deliveries of runs already in flight."""
        return service.set_grant(services, registration_id, principal, granted, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_enumerate(
        knowledge_base: str,
        registration_id: str,
        revision: str | None = None,
        prefix: str = "",
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict:
        """List a revision's entries with kind, digest, hidden/ignored/binary/generated flags."""
        return service.enumerate_entries(
            services, registration_id, revision, prefix, cursor, limit, knowledge_base
        )

    @mcp.tool()
    @anticipated
    def harness_grep(knowledge_base: str, request: GrepRequest) -> dict:
        """Literal or regex search over a pinned revision.

        The coverage block says what was scanned and skipped; only a complete
        scan supports a count or absence. Pass next_cursor to resume.
        """
        return service.grep(services, request, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_read(knowledge_base: str, request: ReadRequest) -> dict:
        """Read a line range of one entry at a pinned revision, with a citable locator."""
        return service.read(services, request, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_discover(knowledge_base: str, request: DiscoverRequest) -> dict:
        """Candidate locations from the region's search index, with index version and the digest it
        indexed. Candidates are places to look; open the source before citing it."""
        return service.discover(services, request, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_start_run(knowledge_base: str, request: RunRequest, client_key: str) -> dict:
        """Start a durable, verified run (answer, summarize, build_content, code_change). The same
        client_key with the same request returns the same run; with a different
        request it is refused.
        Follow it with harness_run_events (after_seq) or harness_run_status."""
        return service.start_run(
            services, StartRequest(request=request, client_key=client_key), knowledge_base
        )

    @mcp.tool()
    @anticipated
    def harness_list_runs(knowledge_base: str, limit: int = 50) -> dict:
        """Your runs, newest first."""
        return service.list_runs(services, limit, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_run_status(knowledge_base: str, run_id: str) -> dict:
        """A run's state, budget spent and remaining, effects and its final handoff."""
        return service.run_status(services, run_id, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_run_events(
        knowledge_base: str, run_id: str, after_seq: int = 0, limit: int = 200
    ) -> dict:
        """Journal events after a sequence number: gap-free, so a reader misses nothing."""
        return service.run_events(services, run_id, after_seq, limit, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_get_run_detail(knowledge_base: str, run_id: str) -> dict:
        """Steps, attempts, fences, evidence, artifacts, checks, sandboxes and integrity."""
        return service.run_detail(services, run_id, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_step_run(knowledge_base: str, run_id: str) -> dict:
        """Advance a run now on this replica (a background runner also advances it)."""
        return service.advance_run(services, run_id, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_resume_run(knowledge_base: str, run_id: str, input: dict | None = None) -> dict:
        """Resume a waiting run with the input it asked for. Authority is checked again first."""
        return service.resume_run(
            services, run_id, ResumeRequest(input=input or {}), knowledge_base
        )

    @mcp.tool()
    @anticipated
    def harness_cancel_run(knowledge_base: str, run_id: str) -> dict:
        """Cancel durably: no new effects are scheduled, running sandboxes are stopped, and effects
        already committed or still uncertain are reported."""
        return service.cancel_run(services, run_id, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_get_artifact(knowledge_base: str, run_id: str, digest: str) -> dict:
        """One artifact of a run by digest: a document, render, figure or code tree."""
        return service.artifact(services, run_id, digest, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_resolve_effect(
        knowledge_base: str,
        run_id: str,
        operation_id: str,
        disposition: Literal["applied", "not_applied"],
    ) -> dict:
        """Record an operator's finding about an uncertain external effect, so the run can finish
        without blindly repeating a write the target cannot deduplicate."""
        return service.resolve_effect(services, run_id, operation_id, disposition, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_decide_approval(
        knowledge_base: str,
        run_id: str,
        operation_id: str,
        decision: Literal["approve", "reject"],
        note: str = "",
    ) -> dict:
        """Approve or reject the one write a waiting run would make through a connection.
        Approving is an operator's decision and never the requester's own unless the
        connection's policy allows self-approval; an approval cannot widen the policy."""
        return service.decide_approval(
            services,
            run_id,
            operation_id,
            ApprovalRequest(decision=decision, note=note),
            knowledge_base,
        )

    @mcp.tool()
    @anticipated
    def harness_list_connections(knowledge_base: str) -> dict:
        """Connected accounts (never their secrets), what the harness could do through each,
        and the policy that says what it may: actions, resources, quotas, approval."""
        return service.connections(services, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_set_connection_policy(
        knowledge_base: str, connection: str, policy: ConnectionPolicyRequest
    ) -> dict:
        """Set what the harness may do through one connection. Until a policy exists the
        connection is unusable here; delete, share and similar actions can never be enabled."""
        return service.set_connection_policy(services, connection, policy, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_disable_connection_policy(knowledge_base: str, connection: str) -> dict:
        """Stop every harness use of a connection at its next request, in-flight runs included."""
        return service.disable_connection_policy(services, connection, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_connection_policy_history(knowledge_base: str, connection: str) -> dict:
        """Every version of a connection's harness policy, who set it and when."""
        return service.connection_policy_history(services, connection, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_connection_calls(
        knowledge_base: str,
        run_id: str | None = None,
        connection: str | None = None,
        limit: int = 200,
    ) -> dict:
        """The audit of requests made or refused through connections: action, resource,
        outcome, status and bytes, and why a denial was one. Never a credential."""
        return service.connection_calls(services, run_id, connection, limit, knowledge_base)

    @mcp.tool()
    @anticipated
    def harness_reap_sandboxes(knowledge_base: str) -> dict:
        """Dispose of abandoned sandbox generations and report disposal times."""
        return service.reap(services, knowledge_base)
