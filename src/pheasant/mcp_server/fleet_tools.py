"""The fleet of knowledge bases over MCP.

A mixin plus a registration function, like ``access_tools.py``. Adapters over
`services.fleet`, the operations behind ``/fleet*``.
"""

from __future__ import annotations

from typing import Any


class FleetTools:
    """``services.fleet`` adapters. Parse, call, return."""

    services: Any

    def list_fleet(self) -> dict:
        from pheasant.services import fleet

        return fleet.describe(self.services)

    def create_knowledge_base(
        self, name: str, description: str = "", admins: list[str] | None = None
    ) -> dict:
        from pheasant.services import fleet

        return fleet.create_knowledge_base(
            self.services, name=name, description=description, admins=list(admins or [])
        )

    def request_capacity(self, quantity: int = 1, reason: str = "", kind: str = "slots") -> dict:
        from pheasant.services import fleet

        return fleet.request_capacity(self.services, quantity=quantity, reason=reason, kind=kind)

    def resolve_capacity_request(self, request_id: str, status: str, note: str = "") -> dict:
        from pheasant.services import fleet

        return fleet.resolve_request(self.services, request_id, status=status, note=note)


def register_fleet_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
    @mcp.tool()
    @anticipated
    def list_fleet() -> dict:
        """List the knowledge bases you administer in this fleet and its capacity.

        ``capacity.free`` is how many more knowledge bases can be created now
        without the operator provisioning anything. ``knowledge_bases`` gives
        each one's name, address, admins and size against its ceiling; the
        region's operator also sees unclaimed slots and each knowledge base's
        last thirty days of usage. ``requests`` are capacity requests and
        their answers. FLEET_NOT_ENABLED means this region stands alone."""

        return tools.list_fleet()

    @mcp.tool()
    @anticipated
    def create_knowledge_base(
        name: str, description: str = "", admins: list[str] | None = None
    ) -> dict:
        """Create a new, empty knowledge base in a free slot (Admin only).

        You become its Admin, with anyone in ``admins`` (sign-in names from
        your organization). It shares the operator's resources and nothing
        else: it has its own sources, documents, Contributors and Readers.
        Returns its address; its first page takes about a minute, because a
        knowledge base nobody is using is asleep. FLEET_NO_CAPACITY means
        every slot is in use: call request_capacity."""

        return tools.create_knowledge_base(name, description, admins)

    @mcp.tool()
    @anticipated
    def request_capacity(quantity: int = 1, reason: str = "", kind: str = "slots") -> dict:
        """Ask the region's operator for more capacity (Admin only).

        ``kind`` is "slots" (``quantity`` more knowledge bases) or "storage"
        (``quantity`` more gigabytes for this one). This files a request the
        operator answers; nothing is provisioned by calling it. Say why in
        ``reason``. Follow it in list_fleet."""

        return tools.request_capacity(quantity, reason, kind)

    @mcp.tool()
    @anticipated
    def resolve_capacity_request(request_id: str, status: str, note: str = "") -> dict:
        """Record the operator's answer to a capacity request: ``status`` is
        approved, declined or fulfilled. Only the region's operator may; it
        records a decision and provisions nothing."""

        return tools.resolve_capacity_request(request_id, status, note)
