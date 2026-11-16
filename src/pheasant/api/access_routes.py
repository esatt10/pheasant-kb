"""Access over HTTP: who has it, granting, revoking, connecting an organization.

One router per plane. Every handler parses the request and calls
`services.access`; MCP's ``list_access`` / ``grant_access`` / ``revoke_access``
/ ``connect_organization`` / ``disconnect_organization`` call the same
functions.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from pheasant.services import access


class GrantBody(BaseModel):
    role: str
    email: str | None = None
    subject: str | None = None
    organization: str | None = None
    display_name: str | None = None


class OrganizationBody(BaseModel):
    tenant_id: str
    name: str | None = None
    domains: list[str] = Field(default_factory=list)
    default_role: str | None = None


def register_access_routes(app: FastAPI, *, services: Any) -> None:
    @app.get("/access")
    def list_access() -> dict:
        """People with access, the roles the caller may grant, and the
        connected organizations. A Contributor sees their own organization."""

        return access.list_access(services)

    @app.post("/access/grants")
    def grant_access(body: GrantBody) -> dict:
        """Give a person a role by sign-in name. An Admin grants any role; a
        Contributor grants Contributor or Reader (``ACCESS_ROLE_NOT_GRANTABLE``
        otherwise)."""

        return access.grant_access(
            services,
            access.GrantRequest(
                role=body.role,
                email=body.email,
                subject=body.subject,
                organization=body.organization,
                display_name=body.display_name,
            ),
        )

    @app.delete("/access/grants/{grant_id}")
    def revoke_access(grant_id: str) -> dict:
        """Remove a grant. The person's sessions and keys stop within seconds."""

        return access.revoke_access(services, grant_id=grant_id)

    @app.post("/access/organizations")
    def connect_organization(body: OrganizationBody) -> dict:
        """Connect a customer's Microsoft Entra tenant (Admin). Returns the
        link its directory admin approves the app with, and its sign-in link."""

        return access.connect_organization(
            services,
            access.OrganizationRequest(
                tenant_id=body.tenant_id,
                name=body.name,
                domains=list(body.domains),
                default_role=body.default_role,
            ),
        )

    @app.delete("/access/organizations/{tenant_id}")
    def disconnect_organization(tenant_id: str) -> dict:
        """Disconnect an organization and remove its people's grants (Admin)."""

        return access.disconnect_organization(services, tenant_id)
