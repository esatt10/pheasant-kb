"""Access over MCP: who has it, granting, revoking, connecting an organization.

A mixin plus a registration function, like ``connection_tools.py``. Adapters
over `services.access`, the operations behind ``/access*``.
"""

from __future__ import annotations

from typing import Any


class AccessTools:
    """``services.access`` adapters."""

    services: Any

    def list_access(self) -> dict:
        from pheasant.services import access

        return access.list_access(self.services)

    def grant_access(
        self,
        email: str,
        role: str,
        organization: str | None = None,
        display_name: str | None = None,
    ) -> dict:
        from pheasant.services import access

        return access.grant_access(
            self.services,
            access.GrantRequest(
                role=role, email=email, organization=organization, display_name=display_name
            ),
        )

    def revoke_access(self, email: str, organization: str | None = None) -> dict:
        from pheasant.services import access

        return access.revoke_access(self.services, email=email, organization=organization)

    def connect_organization(
        self,
        tenant_id: str,
        name: str | None = None,
        domains: list[str] | None = None,
        default_role: str | None = None,
    ) -> dict:
        from pheasant.services import access

        return access.connect_organization(
            self.services,
            access.OrganizationRequest(
                tenant_id=tenant_id,
                name=name,
                domains=list(domains or []),
                default_role=default_role,
            ),
        )

    def disconnect_organization(self, tenant_id: str) -> dict:
        from pheasant.services import access

        return access.disconnect_organization(self.services, tenant_id)


def register_access_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
    @mcp.tool()
    @anticipated
    def list_access() -> dict:
        """Who has access to this knowledge base and what you may grant.

        ``people`` are the grants (``pending`` until that person first signs
        in), ``grantable_roles`` the roles you may hand out, ``organizations``
        the connected customer directories with their sign-in links. A
        Contributor sees their own organization; a Reader is refused."""

        return tools.list_access()

    @mcp.tool()
    @anticipated
    def grant_access(
        email: str,
        role: str,
        organization: str | None = None,
        display_name: str | None = None,
    ) -> dict:
        """Give a person access by their sign-in name, or change their role.

        ``role`` is Reader, Contributor or Admin. An Admin grants any of them;
        a Contributor grants Contributor or Reader, in their own organization;
        a Reader cannot grant (ACCESS_ROLE_NOT_GRANTABLE). ``organization`` is a
        connected organization's tenant id or domain, default your own. Returns
        ``sign_in_url``: give it to the person. Nobody can change their own
        access."""

        return tools.grant_access(email, role, organization, display_name)

    @mcp.tool()
    @anticipated
    def revoke_access(email: str, organization: str | None = None) -> dict:
        """Remove a person's access. Their sessions, personal keys and connected
        clients stop working within seconds. You can remove only grants of a
        role you could have granted."""

        return tools.revoke_access(email, organization)

    @mcp.tool()
    @anticipated
    def connect_organization(
        tenant_id: str,
        name: str | None = None,
        domains: list[str] | None = None,
        default_role: str | None = None,
    ) -> dict:
        """Let a customer's Microsoft Entra tenant sign in (Admin only).

        ``tenant_id`` is that organization's Entra tenant id. ``domains`` are
        names its people may type at sign-in instead (``contoso.com``).
        ``default_role`` (Reader or Contributor) gives every member that role;
        without it members are granted one at a time with ``grant_access``.
        Returns ``admin_consent_url``, which the organization's directory admin
        opens once, and ``sign_in_url`` for its people."""

        return tools.connect_organization(tenant_id, name, domains, default_role)

    @mcp.tool()
    @anticipated
    def disconnect_organization(tenant_id: str) -> dict:
        """Disconnect an organization (Admin only): its people can no longer
        sign in and every grant made to them is removed."""

        return tools.disconnect_organization(tenant_id)
