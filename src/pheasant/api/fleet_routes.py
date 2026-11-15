"""The fleet of knowledge bases over HTTP.

One router per plane. Every handler calls `services.fleet`; MCP's
``list_fleet`` / ``create_knowledge_base`` / ``request_capacity`` /
``resolve_capacity_request`` call the same functions.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from pheasant.services import fleet


class CreateBody(BaseModel):
    name: str
    description: str = ""
    admins: list[str] = Field(default_factory=list)


class CapacityBody(BaseModel):
    quantity: int = 1
    reason: str = ""
    kind: str = "slots"


class ResolveBody(BaseModel):
    status: str
    note: str = ""


def register_fleet_routes(app: FastAPI, *, services: Any) -> None:
    @app.get("/fleet")
    def list_fleet() -> dict:
        """The knowledge bases the caller administers, how many slots are free,
        and their capacity requests. The operator sees every slot, with usage."""

        return fleet.describe(services)

    @app.post("/fleet/knowledge-bases")
    def create_knowledge_base(body: CreateBody) -> dict:
        """Claim a free slot for a new knowledge base (Admin). The caller, and
        anyone in ``admins``, becomes its Admin. ``409 FLEET_NO_CAPACITY`` when
        no slot is free."""

        return fleet.create_knowledge_base(
            services, name=body.name, description=body.description, admins=list(body.admins)
        )

    @app.post("/fleet/requests")
    def request_capacity(body: CapacityBody) -> dict:
        """Ask the operator for more knowledge-base slots, or more room here."""

        return fleet.request_capacity(
            services, quantity=body.quantity, reason=body.reason, kind=body.kind
        )

    @app.patch("/fleet/requests/{request_id}")
    def resolve_request(request_id: str, body: ResolveBody) -> dict:
        """Record the operator's answer to a capacity request. Provisions nothing."""

        return fleet.resolve_request(services, request_id, status=body.status, note=body.note)
