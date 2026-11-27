"""Harness operations, implemented once. HTTP and MCP are adapters over these."""

from __future__ import annotations

import base64
from typing import Any

from pheasant.security.identity import current_identity
from pheasant_harness import discovery as discovery_mod
from pheasant_harness import investigate, runner
from pheasant_harness.broker import TOOLS
from pheasant_harness.common import POLICY_VERSION, WORKFLOW_VERSION, AccessDenied, NotFound
from pheasant_harness.context import harness_for
from pheasant_harness.coordinator import Coordinator
from pheasant_harness.delivery import ADAPTERS
from pheasant_harness.models import (
    DiscoverRequest,
    GrepRequest,
    ReadRequest,
    RegisterRequest,
    ResumeRequest,
    StartRequest,
)
from pheasant_harness.planner import default_criteria
from pheasant_harness.sandbox import isolation_manifest
from pheasant_harness.verify import GRADER_VERSION


def actor() -> str:
    identity = current_identity()
    return identity.principal if identity else "local"


def is_operator() -> bool:
    from pheasant.security.authorization import allowed

    return allowed("security.manage") or allowed("settings.manage")


def _llm(services: Any):
    try:
        from pheasant.assistant.answering import resolve_llm

        return resolve_llm(services.config)
    except Exception:  # noqa: BLE001 - no model is the standalone default
        return None


def _coordinator(services: Any) -> Coordinator:
    return Coordinator(harness_for(services), llm=_llm(services))


def _kb(services: Any, knowledge_base: str | None) -> None:
    services.knowledge_base(knowledge_base)


# -- registry ------------------------------------------------------------------


def register(services: Any, request: RegisterRequest, knowledge_base: str | None = None) -> dict:
    _kb(services, knowledge_base)
    return harness_for(services).registry.register(
        request.uri,
        actor(),
        resolver=request.resolver,
        label=request.label,
        scope=request.scope,
        freshness_policy=request.freshness_policy,
        operations=request.operations,
        classification=request.classification,
    )


def registrations(services: Any, knowledge_base: str | None = None) -> dict:
    _kb(services, knowledge_base)
    return {"registrations": harness_for(services).registry.list(actor())}


def resolve(
    services: Any,
    registration_id: str,
    version_selector: str | None = None,
    knowledge_base: str | None = None,
) -> dict:
    _kb(services, knowledge_base)
    revision = harness_for(services).registry.resolve(registration_id, actor(), version_selector)
    return {
        **revision.handle(),
        "unreadable": [e["path"] for e in revision.entries if e["kind"] == "unreadable"],
    }


def set_grant(
    services: Any,
    registration_id: str,
    principal: str,
    granted: bool,
    knowledge_base: str | None = None,
) -> dict:
    _kb(services, knowledge_base)
    registry = harness_for(services).registry
    reg = (
        registry.require(registration_id, actor(), "resolve")
        if not is_operator()
        else registry.get(registration_id)
    )
    if granted:
        registry.grant(reg.id, principal, actor())
        return {"registration_id": reg.id, "principal": principal, "granted": True}
    return registry.revoke(reg.id, principal)


# -- direct inspection -----------------------------------------------------------


def _revision(services, registration_id: str, revision: str | None):
    harness = harness_for(services)
    if revision:
        return harness.registry.revision(registration_id, revision, actor())
    return harness.registry.resolve(registration_id, actor())


def enumerate_entries(
    services: Any,
    registration_id: str,
    revision: str | None = None,
    prefix: str = "",
    cursor: str | None = None,
    limit: int = 200,
    knowledge_base: str | None = None,
) -> dict:
    _kb(services, knowledge_base)
    harness = harness_for(services)
    harness.registry.require(registration_id, actor(), "enumerate")
    return investigate.enumerate_entries(
        _revision(services, registration_id, revision), prefix, cursor, limit
    )


def grep(services: Any, request: GrepRequest, knowledge_base: str | None = None) -> dict:
    _kb(services, knowledge_base)
    harness = harness_for(services)
    reg = harness.registry.require(request.registration_id, actor(), "grep")
    return investigate.grep(
        harness.registry.resolver(reg),
        _revision(services, reg.id, request.revision),
        request.pattern,
        request.options,
        request.cursor,
    )


def read(services: Any, request: ReadRequest, knowledge_base: str | None = None) -> dict:
    _kb(services, knowledge_base)
    harness = harness_for(services)
    reg = harness.registry.require(request.registration_id, actor(), "read")
    return investigate.read_range(
        harness.registry.resolver(reg),
        _revision(services, reg.id, request.revision),
        request.path,
        request.start_line,
        request.end_line,
    )


def discover(services: Any, request: DiscoverRequest, knowledge_base: str | None = None) -> dict:
    _kb(services, knowledge_base)
    harness = harness_for(services)
    regs = [
        harness.registry.get(r["id"]) for r in harness.registry.list(actor()) if r["authorized"]
    ]
    return discovery_mod.discover(
        harness,
        request.question,
        filters=request.filters,
        cursor=request.cursor,
        limit=request.limit,
        registrations=regs,
    )


# -- runs ----------------------------------------------------------------------------


def start_run(
    services: Any, request: StartRequest, knowledge_base: str | None = None, background: bool = True
) -> dict:
    _kb(services, knowledge_base)
    coordinator = _coordinator(services)
    status = coordinator.start(
        request.request, actor=actor(), client_key=request.client_key, tenant=request.request.tenant
    )
    if background:
        runner.ensure_runner(coordinator.harness, coordinator.llm)
    return status


def _owned(services: Any, run_id: str) -> Coordinator:
    coordinator = _coordinator(services)
    run = coordinator.runs.get(run_id)
    if run["actor"] != actor() and not is_operator():
        raise NotFound(f"Unknown harness run: {run_id}")
    return coordinator


def list_runs(services: Any, limit: int = 50, knowledge_base: str | None = None) -> dict:
    _kb(services, knowledge_base)
    harness = harness_for(services)
    rows = harness.runs.list(actor=None if is_operator() else actor(), limit=limit)
    return {
        "runs": [
            {
                "run_id": r["id"],
                "state": r["state"],
                "workload": r["workload"],
                "tenant": r["tenant"],
                "actor": r["actor"],
                "question": r["request"].get("question") or r["request"].get("q"),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "last_seq": r["last_seq"],
                "result_status": (r["result"] or {}).get("status"),
            }
            for r in rows
        ]
    }


def run_status(services: Any, run_id: str, knowledge_base: str | None = None) -> dict:
    _kb(services, knowledge_base)
    return _owned(services, run_id).status(run_id)


REDACTED = "[withheld: the caller may no longer read this source]"


def _readable(services: Any) -> set[str]:
    registry = harness_for(services).registry
    who = actor()
    return {r["id"] for r in registry.list(who) if r["authorized"]}


def _redact(detail: dict, readable: set[str]) -> dict:
    """Derived data inherits its source's restrictions: revoked text leaves every view."""
    withheld = set()
    for item in detail["evidence"]:
        if item["registration_id"] and item["registration_id"] not in readable:
            item["extract"] = REDACTED
            item["flags"] = {**item["flags"], "withheld": True}
            withheld.add(item["id"])
    for step in detail["steps"]:
        reg_id = step["args"].get("registration_id")
        if reg_id and reg_id not in readable:
            step["observation"] = {"withheld": True}
    if withheld:
        detail["context_preview"] = REDACTED
    detail["withheld_evidence"] = sorted(withheld)
    return detail


def run_detail(services: Any, run_id: str, knowledge_base: str | None = None) -> dict:
    _kb(services, knowledge_base)
    return _redact(_owned(services, run_id).detail(run_id), _readable(services))


def run_events(
    services: Any,
    run_id: str,
    after_seq: int = 0,
    limit: int = 500,
    knowledge_base: str | None = None,
) -> dict:
    _kb(services, knowledge_base)
    coordinator = _owned(services, run_id)
    events = coordinator.runs.events(run_id, after_seq, limit, operator=is_operator())
    return {
        "run_id": run_id,
        "events": events,
        "next_after_seq": events[-1]["seq"] if events else after_seq,
        "state": coordinator.runs.get(run_id)["state"],
    }


def advance_run(services: Any, run_id: str, knowledge_base: str | None = None) -> dict:
    _kb(services, knowledge_base)
    return _owned(services, run_id).tick(run_id)


def resume_run(
    services: Any, run_id: str, request: ResumeRequest, knowledge_base: str | None = None
) -> dict:
    _kb(services, knowledge_base)
    coordinator = _owned(services, run_id)
    status = coordinator.resume(run_id, actor(), request.input)
    runner.ensure_runner(coordinator.harness, coordinator.llm)
    return status


def cancel_run(services: Any, run_id: str, knowledge_base: str | None = None) -> dict:
    _kb(services, knowledge_base)
    return _owned(services, run_id).cancel(run_id, actor())


def resolve_effect(
    services: Any,
    run_id: str,
    operation_id: str,
    disposition: str,
    knowledge_base: str | None = None,
) -> dict:
    _kb(services, knowledge_base)
    return _owned(services, run_id).resolve_effect(run_id, operation_id, disposition, actor())


def artifact(services: Any, run_id: str, digest: str, knowledge_base: str | None = None) -> dict:
    _kb(services, knowledge_base)
    coordinator = _owned(services, run_id)
    rows = coordinator.harness.state.rows(
        "SELECT * FROM harness_artifacts WHERE run_id=? AND digest=?", (run_id, digest)
    )
    if not rows:
        raise NotFound(f"Unknown artifact {digest} in run {run_id}")
    row = rows[0]
    import json as _json

    cited = set(_json.loads(row["provenance_json"]).get("cites") or [])
    if cited:
        readable = _readable(services)
        sources = coordinator.harness.state.rows(
            "SELECT id, registration_id FROM harness_evidence WHERE run_id=?", (run_id,)
        )
        blocked = {
            r["registration_id"]
            for r in sources
            if r["id"] in cited and r["registration_id"] and r["registration_id"] not in readable
        }
        if blocked:
            raise AccessDenied(
                f"artifact {digest[:19]} is derived from sources you may no longer read"
            )
    data = coordinator.harness.blobs.get(digest.removeprefix("tree:"))
    textual = row["media_type"].startswith(
        ("text/", "application/json", "application/vnd.pheasant")
    )
    return {
        "digest": digest,
        "kind": row["kind"],
        "name": row["name"],
        "media_type": row["media_type"],
        "parent_digest": row["parent_digest"],
        "content": data.decode("utf-8", errors="replace") if textual else None,
        "content_base64": None if textual else base64.b64encode(data).decode(),
        "bytes": len(data),
    }


def integrity(services: Any, run_id: str, knowledge_base: str | None = None) -> dict:
    _kb(services, knowledge_base)
    return _owned(services, run_id).integrity(run_id)


# -- operations ----------------------------------------------------------------------


def sandboxes(services: Any, run_id: str | None = None, knowledge_base: str | None = None) -> dict:
    _kb(services, knowledge_base)
    return {
        "sandboxes": harness_for(services).sandboxes.list(run_id),
        "isolation": isolation_manifest(),
    }


def reap(services: Any, knowledge_base: str | None = None) -> dict:
    _kb(services, knowledge_base)
    return harness_for(services).sandboxes.reap()


def contract(services: Any, knowledge_base: str | None = None) -> dict:
    """What this harness supports, derived from the code that implements it."""
    _kb(services, knowledge_base)
    return {
        "workflow_version": WORKFLOW_VERSION,
        "policy_version": POLICY_VERSION,
        "verifier": GRADER_VERSION,
        "tools": {
            name: {
                "args": sorted(spec["args"]),
                "effect": spec["effect"],
                "capability": spec["capability"],
            }
            for name, spec in TOOLS.items()
        },
        "workloads": {
            w: [
                c["id"]
                for c in default_criteria({"workload": w, "test_command": ["true"], "facts": [1]})
            ]
            for w in ("answer", "summarize", "build_content", "code_change")
        },
        "delivery_adapters": {
            name: cls(harness_for(services)).describe() for name, cls in ADAPTERS.items()
        },
        "resolvers": ["git", "folder", "pheasant"],
        "isolation": isolation_manifest(),
        "states": [
            "accepted",
            "running",
            "waiting",
            "verifying",
            "delivering",
            "reconciling",
            "succeeded",
            "partial",
            "failed",
            "cancelled",
        ],
        "model_planner": _llm(services) is not None,
    }
