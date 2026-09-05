"""Who is answering, and does it expose what the contract says.

Two probes and a layering exception. `mcp_discovery` imports the MCP facade,
which is a transport, and this module is therefore named in
`tests/test_service_layering.py`'s COMPOSITION_ROOTS — for the reason that set
already gives `memory/benchmark.py`: a module whose *subject* is a transport
has to be able to look at one. Keeping that exception to two probes in one
small file is why this is not in `ingestion.py` alongside everything else.
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

    Introspection of `PheasantTools` rather than a live `tools/list` round
    trip: the SDK's registration is asserted by `tests/test_mcp_lifecycle.py`,
    and what a *harness* gets wrong is assuming a tool name. So this checks the
    names in the contract against the facade, which is the mapping a harness
    would otherwise guess at.
    """

    from pheasant.mcp_server.tools import PheasantTools
    from pheasant.readiness.contract import CAPABILITIES

    declared = sorted({tool for cap in CAPABILITIES for tool in cap.mcp_tools})
    missing = [name for name in declared if not hasattr(PheasantTools, name)]
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
