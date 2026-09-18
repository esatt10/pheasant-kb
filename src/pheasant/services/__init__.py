"""The application layer: one implementation per operation, two transports.

pheasant has two public APIs — HTTP (whose largest consumer is the bundled UI)
and MCP (whose consumers are agents). The documentation said they were one
facade exposed twice. They were not. `api/app.py` referenced `PheasantTools`
exactly once, for introspection; two of its own docstrings described functions
as "mirrors PheasantTools.get_graph_neighbors"; and both surfaces reached
straight past any application layer into `StateStore` and the searcher.

The cost was not theoretical. Measured at the time this package was written,
five operations existed on both surfaces and **four had already diverged**:

* ``relevant_files`` deduplicated by path, honoured ``section`` and applied the
  memory policy over HTTP, and did none of those three over MCP — so an agent
  could be served a record the region *knew* had been corrected.
* ``graph_neighbors`` walked hierarchy edges first, honoured ``max_nodes`` and
  could exclude edge and node types over HTTP. The MCP tool had none of that
  and therefore returned a different ordering for the same node.
* ``explain_node`` returned the node's attributes over HTTP and omitted them
  over MCP.
* ``file_summary`` concatenated chunk summaries in chunk order and returned the
  file's content over HTTP; over MCP the order was whatever ``GROUP_CONCAT``
  produced and the content was absent.

None of that is a decision anybody made. It is what happens when one operation
has two implementations: a fix lands on the surface whose bug report arrived.

**The rule.** An operation lives here, once. It owns retrieval criteria, the
over-fetch, the memory policy, metrics and — importantly — its refusal text.
`api/app.py` and `mcp_server/tools.py` are adapters: they parse a request,
call one function, and marshal the answer back into their transport's shape.
A behaviour that differs between the surfaces has to be a difference in the
*adapters*, which is a thing a reader can see, rather than a difference in two
implementations, which is a thing nobody sees until it is reported.

`tests/test_surface_conformance.py` is what makes that real rather than
aspirational: it drives the same operations through both surfaces against one
corpus and asserts identical results and identical refusal text.

**Layering.** transport → services → domain → persistence, no upward edges.
Services may import the domain (`search`, `graph`, `memory`, `persistence`);
they may not import `api` or `mcp_server`. `tests/test_service_layering.py`
enforces that, which is also what stopped `assistant/retrieval.py` and
`mcp_server/tools.py` importing graph helpers out of the HTTP transport
module — a dependency that had nothing to do with HTTP and existed only
because that was where the code happened to live.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pheasant.services.errors import ServiceError, SourceNotFound, UnknownKnowledgeBase

__all__ = [
    "ServiceContext",
    "ServiceError",
    "SourceNotFound",
    "UnknownKnowledgeBase",
]


@dataclass(frozen=True)
class ServiceContext:
    """What an operation needs, assembled once per process by the adapter.

    Passed explicitly rather than reached for: an operation that takes its
    collaborators as an argument can be driven by a test, by the HTTP app, by
    the MCP tools and by the assistant without any of them knowing about the
    others. That is the whole difference between a service layer and a module
    of shared helpers.

    ``graph`` is the *serving* graph, which on an API replica pointed at the
    graph service is a remote proxy rather than a resident snapshot — the
    operations below have to work either way, which is why every graph
    function checks for the remote entry points first.

    ``landing`` is the same idea for the write side: on a standalone install it
    writes submitted bytes to this process's own disk, and on a serving replica
    whose ``/state`` is read-only it forwards them to the tier that can. An
    operation that lands a file must not care which, and must not reach for the
    filesystem directly — that is what made manual ingestion a 500 on every
    role-split deployment. ``None`` means "derive the local one from config",
    so a caller that predates this keeps working.
    """

    config: Any
    state: Any
    searcher: Any
    graph: Any = None
    engine: Any = None
    landing: Any = None

    def landing_zone(self) -> Any:
        """The landing zone, derived from config when an adapter did not pass one.

        The fallback **honours a configured landing service** rather than
        forcing a local write, and the asymmetry is deliberate: a caller that
        knows its role passes the right zone, and a caller that does not is
        better off making one extra network hop than writing to a filesystem it
        may not be allowed to write. Being wrong in the forwarding direction
        costs a round trip on the one tier that writes its own zone — the
        endpoint on the far side always writes locally, so it cannot loop.
        Being wrong the other way is the 500 this whole module exists to
        remove.
        """

        if self.landing is not None:
            return self.landing
        from pheasant.ingestion.landing_service import landing_zone_for_config

        return landing_zone_for_config(self.config)

    def knowledge_base(self, requested: str | None = None) -> str:
        """The knowledge base this call addresses, refusing an unknown one.

        Both surfaces take an optional knowledge-base argument and both have to
        answer the same way when it names something this region does not hold.
        Before this they did not: MCP raised `ValueError("Unknown knowledge
        base: x")` and HTTP happily searched the configured one anyway.
        """

        configured = self.config.knowledge_base_id
        if requested and requested != configured:
            raise UnknownKnowledgeBase(requested)
        return configured
