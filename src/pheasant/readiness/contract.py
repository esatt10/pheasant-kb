"""What this build supports, derived from the build rather than described.

A capability contract is the first thing an outside harness reads and the
easiest thing to get quietly wrong. A hand-written list says what somebody
believed when they last edited it; the failure mode is not that it is missing
a capability but that it *claims* one — a harness then builds an experiment on
an operation that does not exist, and finds out after the corpus is collected.

So every entry here resolves against something live: the MCP tool that would
serve it, the HTTP route, or the service function both call. A capability whose
implementation is absent reports `unsupported` with the reason, and
`tests/test_readiness_contract.py` fails when an entry names a symbol nothing
provides. That is the same posture as `tests/test_config_surface_freshness.py`:
the list cannot rot, because the list is checked against the code.

**Support is a claim about the build. Proof is a claim about a run.** A
capability is `supported` when its implementation exists and `proven` only once
a probe has demonstrated it here, against this corpus, in this configuration.
The distinction is the whole reason this plane exists: the specification's
gaps are evidence gaps, and "the code is present" has never been evidence that
it works in the deployment somebody is about to measure.

Every capability names the gap it closes, so the contract and the readiness
specification cannot drift apart without one of them being obviously wrong.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import blake2b
from typing import Any

#: How a capability may report. Four values rather than a boolean because a
#: harness has to act differently on each: build it, fix the config, run the
#: probe, or proceed.
STATUSES = ("proven", "supported", "declared_untested", "unsupported")


@dataclass(frozen=True)
class Capability:
    """One logical operation the readiness specification asks for.

    ``logical`` is the specification's vocabulary and ``surfaces`` is
    pheasant's. They differ, and that is fine — an adapter maps them — but the
    mapping has to be published or every harness re-derives it by guessing at
    tool names.
    """

    logical: str
    #: The gap id in ``docs/stress-test-readiness.md`` this closes.
    gap: str
    summary: str
    #: ``module:attribute`` paths that must resolve for this to be supported.
    #: Checked, not trusted.
    implementation: tuple[str, ...] = ()
    #: MCP tool names, as `mcp_server/tools.py` spells them.
    mcp_tools: tuple[str, ...] = ()
    #: HTTP paths, as `api/app.py` spells them.
    http_routes: tuple[str, ...] = ()
    #: Probe ids in `probes.py` that demonstrate it. A capability with no probe
    #: can never be `proven`, which is a fact worth publishing rather than
    #: hiding: it tells a reader exactly how far the evidence goes.
    probes: tuple[str, ...] = ()
    #: Which gate set blocks on it.
    gate: str = "core"
    #: Set when this build genuinely cannot do the thing. Carrying the reason
    #: rather than omitting the row: a harness needs to know the difference
    #: between "unsupported" and "not in the contract I am reading".
    unsupported_reason: str = ""
    notes: str = ""

    def as_dict(self, *, status: str, detail: str = "") -> dict[str, Any]:
        payload = {
            "logical": self.logical,
            "gap": self.gap,
            "summary": self.summary,
            "status": status,
            "gate": self.gate,
            "mcp_tools": list(self.mcp_tools),
            "http_routes": list(self.http_routes),
            "probes": list(self.probes),
        }
        if self.notes:
            payload["notes"] = self.notes
        if detail:
            payload["detail"] = detail
        return payload


#: The logical operations from the readiness specification's §3.1, mapped onto
#: what this region actually exposes.
#:
#: Order is the specification's, not pheasant's, because this table is read by
#: somebody holding that document.
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        logical="health/capabilities",
        gap="G-PHE-001",
        summary="Readiness, version, protocol, limits and supported features.",
        implementation=("pheasant.readiness.contract:build_contract",),
        mcp_tools=("get_readiness_contract",),
        http_routes=("/health", "/ready", "/readiness/contract"),
        probes=("service_identity",),
    ),
    Capability(
        logical="mcp/discovery",
        gap="G-PHE-002",
        summary="initialize, protocol negotiation and tools/list.",
        implementation=("pheasant.mcp_server.server:create_mcp_server",),
        http_routes=("/mcp/info",),
        probes=("mcp_discovery",),
        notes=(
            "Tool names are discovered, never assumed. Rule 8 makes the tool "
            "surface public API, so a name here is additive-only."
        ),
    ),
    Capability(
        logical="source.register",
        gap="G-PHE-004",
        summary="Allocate or confirm a deterministic source identity.",
        implementation=("pheasant.mcp_server.tools:PheasantTools.register_source",),
        mcp_tools=("register_source",),
        http_routes=("/sources",),
        probes=("ingest_roundtrip",),
    ),
    Capability(
        logical="artifact.upload",
        gap="G-PHE-004",
        summary="Persist bytes with provenance and an idempotency key.",
        implementation=("pheasant.services.ingestion:submit",),
        mcp_tools=("submit_documents",),
        http_routes=("/ingest/submit", "/sources/upload"),
        probes=("ingest_roundtrip", "idempotent_write", "partial_failure"),
    ),
    Capability(
        logical="ingest.status",
        gap="G-PHE-008",
        summary="Accepted, indexed, rejected or failed — per submitted item.",
        implementation=(
            "pheasant.services.ingestion:ingest_status",
            "pheasant.services.ingestion:acknowledge_indexed",
        ),
        mcp_tools=("get_ingest_status", "acknowledge_ingest"),
        http_routes=("/ingest/status", "/ingest/acknowledge"),
        probes=("ingest_roundtrip", "index_barrier"),
    ),
    Capability(
        logical="ingest.reconcile",
        gap="G-PHE-006",
        summary="Submitted against held, with the silent-loss count named.",
        implementation=("pheasant.services.ingestion:reconcile",),
        mcp_tools=("reconcile_ingest",),
        http_routes=("/ingest/reconcile",),
        probes=("reconciliation", "partial_failure"),
    ),
    Capability(
        logical="snapshot.seal",
        gap="G-PHE-009",
        summary="Seal an immutable manifest over every retrieval-affecting input.",
        implementation=("pheasant.services.snapshots:seal",),
        mcp_tools=("seal_snapshot",),
        http_routes=("/snapshots/seal",),
        probes=("snapshot_seal", "snapshot_drift_refused"),
    ),
    Capability(
        logical="snapshot.get/list",
        gap="G-PHE-009",
        summary="Resolve the exact snapshot a run used, and verify it still holds.",
        implementation=(
            "pheasant.services.snapshots:describe",
            "pheasant.services.snapshots:catalogue",
            "pheasant.services.snapshots:verify",
        ),
        mcp_tools=("get_snapshot", "list_snapshots"),
        http_routes=("/snapshots", "/snapshots/{snapshot_id}"),
        probes=("snapshot_seal",),
    ),
    Capability(
        logical="search",
        gap="G-PHE-011",
        summary="Retrieval under an explicit namespace, snapshot and policy.",
        implementation=("pheasant.services.retrieval:search",),
        mcp_tools=("search_context", "get_relevant_files"),
        http_routes=("/search", "/relevant-files"),
        probes=("retrieval_lineage", "search_latency"),
    ),
    Capability(
        logical="read/evidence.get",
        gap="G-PHE-013",
        summary="Resolve a result to exact persisted content and source location.",
        implementation=("pheasant.mcp_server.tools:PheasantTools.get_file_summary",),
        mcp_tools=("get_file_summary", "explain_node"),
        http_routes=("/nodes/content", "/files/summary"),
        probes=("resolve_result",),
    ),
    Capability(
        logical="memory.configure/status",
        gap="G-PHE-018",
        summary="Select memory off/on and report the effective state per query.",
        implementation=("pheasant.memory.policy:MemoryPolicy",),
        mcp_tools=("search_context", "describe_retrieval"),
        http_routes=("/memory/enable", "/search"),
        probes=("memory_state_reported",),
        gate="memory",
    ),
    Capability(
        logical="memory.export",
        gap="G-PHE-019",
        summary="Append-only memory records and candidates, exportable for audit.",
        implementation=("pheasant.memory.store:MemoryStore",),
        mcp_tools=("memory_list", "list_memory_candidates"),
        http_routes=("/memory", "/memory/candidates"),
        probes=("memory_export",),
        gate="memory",
    ),
    Capability(
        logical="memory.isolation",
        gap="G-PHE-020",
        summary="Scope keeps one principal's records out of another's results.",
        implementation=("pheasant.security.acl:normalize_acl",),
        http_routes=("/search",),
        probes=("principal_isolation",),
        gate="memory",
    ),
    Capability(
        logical="search_profile.configure/status",
        gap="G-PHE-021",
        summary="Immutable retrieval profile ids, returned with every query.",
        implementation=(
            "pheasant.search.ranking:RankingParameters",
            "pheasant.tuning.bundle:package",
        ),
        mcp_tools=("apply_tuning_bundle", "get_retrieval_parameters"),
        http_routes=("/tuning/bundles", "/tuning/bundles/apply"),
        probes=("retrieval_lineage",),
        gate="tuning",
    ),
    Capability(
        logical="search_profile.lifecycle",
        gap="G-PHE-022",
        summary="Propose, shadow-test, promote, reject and roll back a profile.",
        implementation=("pheasant.tuning.runner:run_tuning",),
        mcp_tools=("start_retrieval_tuning", "rollback_tuning_bundle"),
        http_routes=("/tuning/run", "/tuning/bundles/rollback"),
        probes=(),
        gate="tuning",
        notes=(
            "Exercised by tests/test_tuning_plane.py rather than by a readiness "
            "probe: one promotion cycle needs a cohort and a holdout, which is a "
            "batch, not a check."
        ),
    ),
    Capability(
        logical="isolation.namespace",
        gap="G-PHE-014",
        summary="Two principals, zero cross-scope results.",
        implementation=("pheasant.security.acl:is_allowed",),
        http_routes=("/search",),
        probes=("principal_isolation",),
    ),
    Capability(
        logical="isolation.benchmark",
        gap="G-PHE-015",
        summary="Evaluation artifacts are refused entry to the searchable corpus.",
        implementation=("pheasant.services.ingestion:check_denylist",),
        http_routes=("/ingest/submit",),
        probes=("contamination_refused",),
    ),
    Capability(
        logical="errors.structured",
        gap="G-PHE-016",
        summary="Stable codes and a retryability flag on every deliberate refusal.",
        implementation=("pheasant.services.errors:ServiceError",),
        http_routes=("*",),
        probes=("structured_errors",),
    ),
    Capability(
        logical="concurrency.writers",
        gap="G-PHE-017",
        summary="Parallel writers lose, duplicate and cross-link nothing.",
        implementation=("pheasant.services.ingestion:submit",),
        probes=("concurrent_writers",),
        gate="swarm",
    ),
    Capability(
        logical="performance.slo",
        gap="G-PHE-023",
        summary="Latency and index-lag budgets are configured, so a gate can decide.",
        implementation=("pheasant.config.schema:ReadinessSettings",),
        probes=("search_latency", "index_barrier"),
    ),
    # ---------------------------------------------------------------------
    # Declared unsupported. Each of these is a real limit of this build, and
    # publishing it is worth more than a gate that quietly never runs.
    # ---------------------------------------------------------------------
    Capability(
        logical="temporal.corpus_as_of",
        gap="G-PHE-010",
        summary="Retrieve the corpus as it stood at an earlier instant.",
        probes=(),
        unsupported_reason=(
            "This region stores one version of its corpus. `as_of` replays memory "
            "validity — a correction supersedes rather than overwrites — and does "
            "not reconstruct earlier content. A sealed snapshot plus the drift "
            "refusal is what stands in for it: a pinned search is answered from "
            "the sealed state or it is not answered."
        ),
        notes="The `as_of` half that does work is probed by `memory_as_of`.",
    ),
    Capability(
        logical="temporal.memory_as_of",
        gap="G-PHE-010",
        summary="Replay memory validity at an earlier instant.",
        implementation=("pheasant.memory.policy:MemoryPolicy",),
        http_routes=("/search", "/memory"),
        probes=("memory_as_of",),
        gate="memory",
    ),
    Capability(
        logical="evidence.claim_stance",
        gap="G-PHE-012",
        summary="Typed support / contradict / qualify / unknown between claims.",
        probes=(),
        unsupported_reason=(
            "pheasant models evidence at the *record* level — validity, "
            "supersession, correction, retraction — and at the *proof* level, "
            "where the evaluation plane's taxonomy types what a caller asserted. "
            "There is no claim-to-claim stance edge in the graph, so a harness "
            "needing one has to hold it outside the region. Concept extraction "
            "was retired for failing every test set built for it; inventing a "
            "stance edge on the same rule-based footing would repeat that."
        ),
        notes="What does exist: `record_evidence`, memory supersession, `as_of`.",
    ),
)


def resolve(path: str) -> Any:
    """``module:attribute`` → the object, or ``None``.

    Import errors are answers, not exceptions: an optional extra that is not
    installed is exactly the case this table has to report rather than crash
    on, and a contract endpoint that raises because a capability is missing is
    the least useful possible response to "which capabilities are missing".
    """

    module_name, _, attribute = path.partition(":")
    try:
        module = __import__(module_name, fromlist=["_"])
    except Exception:  # noqa: BLE001 - a missing capability is data
        return None
    target: Any = module
    for part in attribute.split("."):
        if not part:
            continue
        target = getattr(target, part, None)
        if target is None:
            return None
    return target


def capability_status(
    capability: Capability, proven: dict[str, bool] | None = None
) -> tuple[str, str]:
    """This capability's status and, when it is not good news, why."""

    if capability.unsupported_reason:
        return "unsupported", capability.unsupported_reason
    missing = [path for path in capability.implementation if resolve(path) is None]
    if missing:
        return "unsupported", f"not present in this build: {', '.join(missing)}"
    if not capability.probes:
        return "supported", "no probe demonstrates this; it is asserted by the suite"
    results = proven or {}
    if not any(name in results for name in capability.probes):
        return "declared_untested", "present, and no probe has been run against this region"
    failed = [name for name in capability.probes if results.get(name) is False]
    if failed:
        return "supported", f"probe failed here: {', '.join(sorted(failed))}"
    return "proven", ""


def build_contract(
    config: Any,
    *,
    proven: dict[str, bool] | None = None,
    server_version: str | None = None,
) -> dict[str, Any]:
    """The whole contract, with a digest a harness can pin.

    The digest covers the capability rows and this region's limits, and
    deliberately not the probe results: a harness saves a *capability snapshot*
    to detect that the region it is talking to changed shape, and a snapshot
    whose id moved because a latency probe was one millisecond slower would
    make that detection useless.
    """

    from pheasant.version import __version__

    settings = getattr(config, "readiness", None)
    rows = []
    for capability in CAPABILITIES:
        status, detail = capability_status(capability, proven)
        rows.append(capability.as_dict(status=status, detail=detail))

    limits = {
        "max_search_latency_ms": getattr(settings, "max_search_latency_ms", None),
        "max_ingest_ack_ms": getattr(settings, "max_ingest_ack_ms", None),
        "max_index_lag_ms": getattr(settings, "max_index_lag_ms", None),
        "max_file_size_mb": getattr(
            getattr(getattr(config, "sync", None), "limits", None), "max_file_size_mb", None
        ),
    }
    payload = {
        "contract_version": 1,
        "server_version": server_version or __version__,
        "knowledge_base": getattr(getattr(config, "pheasant", None), "name", None),
        "readiness_enabled": bool(getattr(settings, "enabled", False)),
        "capabilities": rows,
        "limits": limits,
        "refusal_codes": refusal_codes(),
        "corpus_denylist": list(getattr(settings, "corpus_denylist", ()) or ()),
        "unsupported": [row["logical"] for row in rows if row["status"] == "unsupported"],
    }
    # Over the *build's* shape, not this run's results. `status` is in `rows`
    # and moves with a probe outcome, so digesting the rows made a capability
    # snapshot change when a latency probe was a millisecond slower — which
    # defeats the one thing a pinned digest is for. Recomputed here without
    # probe results, which is the same table a region publishes before any
    # check has ever run.
    build_rows = [
        {
            "logical": cap.logical,
            "gap": cap.gap,
            "gate": cap.gate,
            "status": capability_status(cap, None)[0],
        }
        for cap in CAPABILITIES
    ]
    payload["digest"] = _digest(build_rows, limits, payload["server_version"])
    if proven is not None:
        payload["probes"] = dict(sorted(proven.items()))
    return payload


def refusal_codes() -> list[dict[str, Any]]:
    """Every deliberate refusal this build can make.

    Published because the MCP surface cannot carry a code: `ToolError` is a
    string, so an agent maps the text it received onto this table. That makes
    the table load-bearing rather than documentation — which is why it is
    derived from the exception classes rather than typed out beside them.
    """

    from pheasant.services import errors as error_module

    rows = []
    for name in sorted(dir(error_module)):
        candidate = getattr(error_module, name)
        if (
            isinstance(candidate, type)
            and issubclass(candidate, error_module.ServiceError)
            and candidate is not error_module.ServiceError
        ):
            rows.append(
                {
                    "name": name,
                    "code": candidate.code,
                    "status": candidate.status,
                    "retryable": candidate.retryable,
                }
            )
    return rows


def _digest(rows: list[dict[str, Any]], limits: dict[str, Any], version: str) -> str:
    """The capability snapshot's id: what this build is, and its limits.

    Deliberately *not* what a run found. A harness pins this to detect the
    region it is talking to changing shape between arms; a digest that also
    moved with probe timings would report a change on every check.
    """

    hasher = blake2b(digest_size=16)
    hasher.update(version.encode("utf-8"))
    hasher.update(
        json.dumps(
            [{key: row[key] for key in ("logical", "gap", "status", "gate")} for row in rows],
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    )
    hasher.update(json.dumps(limits, sort_keys=True, default=str).encode("utf-8"))
    return "cap-" + hasher.hexdigest()
