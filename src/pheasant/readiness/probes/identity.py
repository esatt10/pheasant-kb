"""Who is answering, and does it expose what the contract says.

Two probes. `mcp_discovery` checks the contract's tool names against the
served tool table in `security/permissions.py`, so it needs no transport
import and this module sits in its layer like any other probe.
"""

from __future__ import annotations

from pheasant.readiness.probes import (
    ProbeContext,
    ProbeResult,
    probe,
)


@probe("service_identity")
def _service_identity(context: ProbeContext) -> ProbeResult:
    """The region can name itself: version, knowledge base, graph generation."""

    from pheasant.version import __version__

    kb_id = context.services.knowledge_base(None)
    generation = getattr(context.services.engine, "loaded_graph_generation", None)
    return ProbeResult(
        "service_identity",
        True,
        f"pheasant {__version__} serving {kb_id}",
        observed={
            "server_version": __version__,
            "knowledge_base": kb_id,
            "graph_generation": generation,
        },
    )


@probe("mcp_discovery")
def _mcp_discovery(context: ProbeContext) -> ProbeResult:
    """Every operation the contract names as an MCP tool actually exists.

    Checked against the served tool table (`MCP_PERMISSIONS`, which
    `tests/test_role_permissions.py` holds to every registered tool) rather
    than a live `tools/list` round trip: what a *harness* gets wrong is
    assuming a tool name. It used to check `PheasantTools`, the facade, which
    stopped being the tool surface when tools such as `list_artifact_records`
    were registered beside it rather than on it.
    """

    from pheasant.readiness.contract import CAPABILITIES
    from pheasant.security.permissions import MCP_PERMISSIONS

    declared = sorted({tool for cap in CAPABILITIES for tool in cap.mcp_tools})
    missing = [name for name in declared if name not in MCP_PERMISSIONS]
    return ProbeResult(
        "mcp_discovery",
        not missing,
        (
            f"all {len(declared)} contracted tools exist on the facade"
            if not missing
            else f"contract names tools the facade does not have: {', '.join(missing)}"
        ),
        observed={"declared": len(declared), "missing": missing},
    )
