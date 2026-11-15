"""Who may use this knowledge base, decided by the people who use it.

The operator of a hosted region should not be the one adding a customer's
colleagues. With ``security.entra.delegated_access`` the region keeps its own
grants (``persistence/access_grants.py``) and three rules decide who may
change them:

* an **Admin** grants Admin, Contributor or Reader, to anyone in the
  operator's tenant or a connected organization;
* a **Contributor** grants Contributor or Reader, inside their own
  organization, and changes or removes only grants at or below those;
* a **Reader** grants nothing and is not shown who else has access.

"Connecting an organization" is the other half: an Admin names a customer's
Microsoft Entra tenant once (its id, optionally its domains and a role every
member gets), and from then on that customer's people sign in with their own
accounts and their own Admins and Contributors manage them. Nothing about a
customer's directory is ever configured in the operator's.

The operator's own admins come from the deployment
(``security.entra.role_assignments``). They are listed, and cannot be changed
here: a region whose last admin was removed from inside could only be
recovered from outside anyway.

One implementation behind ``/access*`` and MCP ``list_access`` /
``grant_access`` / ``revoke_access`` / ``connect_organization`` /
``disconnect_organization`` (rule 13). How a grant becomes a role on a request
is ``services/identity.py``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pheasant.persistence import access_grants
from pheasant.security.authorization import effective_identity, permissions, require_permission
from pheasant.security.identity import current_identity
from pheasant.services import ServiceContext
from pheasant.services.errors import (
    AccessGrantNotFound,
    AccessNotDelegated,
    AccessRoleNotGrantable,
    InvalidRequest,
    OrganizationNotConnected,
)

logger = logging.getLogger("pheasant.access")

ROLES = ("Reader", "Contributor", "Admin")
_RANK = {role: rank for rank, role in enumerate(ROLES, start=1)}
#: The role a whole organization may be given. Never Admin: one tenant-wide
#: default must not be able to hand every member the perimeter.
ORG_ROLES = ("Reader", "Contributor")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class GrantRequest:
    """Give one person a role. ``email`` is their sign-in name."""

    role: str
    email: str | None = None
    subject: str | None = None
    #: A connected organization's tenant id or domain; unset is the caller's own.
    organization: str | None = None
    display_name: str | None = None


@dataclass(frozen=True)
class OrganizationRequest:
    tenant_id: str
    name: str | None = None
    domains: list[str] = field(default_factory=list)
    #: The role every member gets (Reader or Contributor), or ``None``.
    default_role: str | None = None


def _settings(context: ServiceContext) -> Any:
    settings = context.config.security.entra
    if not (settings.enabled and settings.tenant_id and settings.client_id):
        raise AccessNotDelegated()
    if not settings.delegated_access:
        raise AccessNotDelegated()
    return settings


def caller_role() -> str:
    """The role the caller acts with. No request context is the local operator."""

    identity = current_identity()
    if identity is None:
        return "Admin"
    granted = permissions(effective_identity())
    if "security.manage" in granted:
        return "Admin"
    if "settings.manage" in granted:
        return "Contributor"
    return "Reader"


def grantable_roles(role: str | None = None) -> list[str]:
    """The roles ``role`` (default: the caller's) may hand to someone else."""

    role = role or caller_role()
    if role == "Admin":
        return list(ROLES)
    if role == "Contributor":
        return ["Reader", "Contributor"]
    return []


def _caller(settings: Any) -> dict[str, Any]:
    identity = current_identity()
    person = identity is not None and identity.is_person
    return {
        "principal": identity.principal if identity else "operator",
        "subject": identity.subject if person else "",
        "tenant_id": (identity.tenant_id if person else "") or settings.tenant_id,
        "role": caller_role(),
        "person": person,
    }


def _require_granting() -> None:
    # The route inventory names the permission; the role ceiling is checked per
    # grant below, because it depends on what is being granted.
    require_permission("settings.manage")


def _organization(row: dict[str, Any], settings: Any) -> dict[str, Any]:
    public = str(settings.public_url or "").rstrip("/")
    name = (row.get("domains") or [row["tenant_id"]])[0]
    return {
        "tenant_id": row["tenant_id"],
        "name": row.get("display_name") or name,
        "domains": row.get("domains") or [],
        "default_role": row.get("role"),
        "connected_by": row.get("granted_by"),
        "connected_at": row.get("granted_at"),
        "sign_in_url": f"{public}/auth/login?org={name}" if public else None,
        # A customer's directory admin approves the app once, here; until
        # then that tenant's members are asked for consent they cannot give.
        "admin_consent_url": (
            f"https://login.microsoftonline.com/{row['tenant_id']}/adminconsent"
            f"?client_id={settings.client_id}"
            + (f"&redirect_uri={public}/auth/callback" if public else "")
        ),
    }


def _person(row: dict[str, Any], names: dict[str, str], caller: dict[str, Any]) -> dict[str, Any]:
    mine = bool(
        caller["person"]
        and row["tenant_id"] == caller["tenant_id"]
        and (
            (row.get("subject") and row["subject"] == caller["subject"])
            or (row.get("email") and row["email"] == caller["principal"])
        )
    )
    return {
        "id": row["id"],
        "email": row.get("email"),
        "subject": row.get("subject"),
        "display_name": row.get("display_name"),
        "role": row.get("role"),
        "tenant_id": row["tenant_id"],
        "organization": names.get(row["tenant_id"], row["tenant_id"]),
        "granted_by": row.get("granted_by"),
        "granted_at": row.get("granted_at"),
        "updated_at": row.get("updated_at"),
        # Granted by name and not signed in yet: nothing proved the address.
        "pending": not row.get("subject"),
        "you": mine,
        "can_change": not mine and row.get("role") in grantable_roles(caller["role"]),
    }


def list_access(context: ServiceContext) -> dict[str, Any]:
    """Who has access, what the caller may grant, and the connected organizations.

    A Contributor sees their own organization; an Admin sees everyone.
    """

    settings = _settings(context)
    _require_granting()
    caller = _caller(settings)
    kb_id = context.config.knowledge_base_id
    rows = access_grants.rows(context.state, kb_id)
    orgs = [row for row in rows if row["kind"] == access_grants.ORG]
    names = {settings.tenant_id: "operator"} | {
        row["tenant_id"]: _organization(row, settings)["name"] for row in orgs
    }
    everyone = caller["role"] == "Admin"
    people = [
        _person(row, names, caller)
        for row in rows
        if row["kind"] == access_grants.USER
        and (everyone or row["tenant_id"] == caller["tenant_id"])
    ]
    return {
        "knowledge_base": kb_id,
        "caller": {k: caller[k] for k in ("principal", "role", "tenant_id")},
        "grantable_roles": grantable_roles(caller["role"]),
        "people": people,
        "organizations": [
            _organization(row, settings)
            for row in orgs
            if everyone or row["tenant_id"] == caller["tenant_id"]
        ],
        # Set by the deployment, shown so nobody wonders who else is an admin.
        "operator_admins": [
            {"subject": subject, "role": role, "tenant_id": settings.tenant_id}
            for subject, role in sorted((settings.role_assignments or {}).items())
        ]
        if everyone
        else [],
        "home_tenant_id": settings.tenant_id,
    }


def _tenant(context: ServiceContext, settings: Any, caller: dict[str, Any], named: str | None):
    """The tenant a grant is made in, refusing one the caller may not grant in."""

    orgs = access_grants.snapshot(context.state, context.config.knowledge_base_id)
    tenant = caller["tenant_id"]
    if named:
        wanted = str(named).strip().lower()
        if wanted in {"operator", "home", settings.tenant_id.lower()}:
            tenant = settings.tenant_id
        elif wanted in orgs.domains:
            tenant = orgs.domains[wanted]
        else:
            tenant = next((t for t in orgs.orgs if t.lower() == wanted), "")
            if not tenant:
                raise OrganizationNotConnected(str(named))
    from pheasant.persistence import fleet_store

    # A knowledge base's own admins may come from a tenant nobody "connected"
    # here: the fleet claim that made them admins admits it.
    if tenant != settings.tenant_id and tenant not in (
        set(orgs.orgs) | fleet_store.admin_tenants(context.config)
    ):
        raise OrganizationNotConnected(tenant)
    if caller["role"] != "Admin" and tenant != caller["tenant_id"]:
        raise AccessRoleNotGrantable("a Contributor grants access only in their own organization")
    return tenant


def _refuse_above(caller: dict[str, Any], role: str | None, what: str) -> None:
    if role not in grantable_roles(caller["role"]):
        allowed = " or ".join(grantable_roles(caller["role"])) or "no role"
        raise AccessRoleNotGrantable(
            f"{caller['role']} may {what} {allowed}; {role or 'this grant'} is above that"
        )


def grant_access(context: ServiceContext, request: GrantRequest) -> dict[str, Any]:
    """Give a person a role, or change the one they have."""

    settings = _settings(context)
    _require_granting()
    caller = _caller(settings)
    role = str(request.role or "").strip().title()
    if role not in ROLES:
        raise InvalidRequest(f"role must be one of {', '.join(ROLES)}; got {request.role!r}")
    _refuse_above(caller, role, "grant")
    email = str(request.email or "").strip().lower() or None
    subject = str(request.subject or "").strip().lower() or None
    if email is None and subject is None:
        raise InvalidRequest("name the person by email (their sign-in name) or subject")
    if email is not None and not _EMAIL.match(email):
        raise InvalidRequest(f"{request.email!r} is not a sign-in name (name@organization)")
    if subject is not None and not _GUID.match(subject):
        raise InvalidRequest("subject is a Microsoft Entra object id (a GUID)")
    tenant = _tenant(context, settings, caller, request.organization)
    if caller["person"] and tenant == caller["tenant_id"]:
        if email == caller["principal"] or (subject and subject == caller["subject"]):
            raise AccessRoleNotGrantable("you cannot change your own access; ask another Admin")
    kb_id = context.config.knowledge_base_id
    key = email or subject or ""
    existing = access_grants.get(
        context.state, kb_id, access_grants.grant_id(kb_id, access_grants.USER, tenant, key)
    )
    if existing is not None:
        # Changing a grant is also taking the old one away.
        _refuse_above(caller, existing.get("role"), "change a grant of")
    row = access_grants.upsert(
        context.state,
        kb_id=kb_id,
        kind=access_grants.USER,
        tenant_id=tenant,
        key=key,
        role=role,
        granted_by=caller["principal"],
        subject=subject,
        email=email,
        display_name=(request.display_name or "").strip() or None,
    )
    logger.info(
        "access granted: by=%s to=%s role=%s tenant=%s previous=%s",
        caller["principal"],
        key,
        role,
        tenant,
        existing.get("role") if existing else "-",
    )
    public = str(settings.public_url or "").rstrip("/")
    names = {settings.tenant_id: "operator"}
    return {
        "status": "updated" if existing else "granted",
        "grant": _person(row, names, caller),
        "sign_in_url": (
            f"{public}/auth/login" + ("" if tenant == settings.tenant_id else f"?org={tenant}")
            if public
            else None
        ),
    }


def revoke_access(
    context: ServiceContext,
    *,
    grant_id: str | None = None,
    email: str | None = None,
    organization: str | None = None,
) -> dict[str, Any]:
    """Take a person's grant away. Their sessions and keys stop within seconds."""

    settings = _settings(context)
    _require_granting()
    caller = _caller(settings)
    kb_id = context.config.knowledge_base_id
    if not grant_id:
        if not email:
            raise InvalidRequest("name the grant by id or by email")
        tenant = _tenant(context, settings, caller, organization)
        grant_id = access_grants.grant_id(
            kb_id, access_grants.USER, tenant, str(email).strip().lower()
        )
    row = access_grants.get(context.state, kb_id, grant_id)
    if row is None or row["kind"] != access_grants.USER:
        raise AccessGrantNotFound(str(email or grant_id))
    if caller["role"] != "Admin" and row["tenant_id"] != caller["tenant_id"]:
        raise AccessGrantNotFound(str(email or grant_id))  # not theirs to know about
    if _person(row, {}, caller)["you"]:
        raise AccessRoleNotGrantable("you cannot remove your own access; ask another Admin")
    _refuse_above(caller, row.get("role"), "remove a grant of")
    access_grants.delete(context.state, kb_id, grant_id)
    logger.info(
        "access revoked: by=%s from=%s role=%s tenant=%s",
        caller["principal"],
        row.get("email") or row.get("subject"),
        row.get("role"),
        row["tenant_id"],
    )
    return {"status": "revoked", "id": grant_id, "email": row.get("email")}


def connect_organization(context: ServiceContext, request: OrganizationRequest) -> dict[str, Any]:
    """Let a customer's Microsoft Entra tenant sign in to this knowledge base."""

    settings = _settings(context)
    require_permission("security.manage")
    caller = _caller(settings)
    tenant = str(request.tenant_id or "").strip().lower()
    if not _GUID.match(tenant):
        raise InvalidRequest(
            "tenant_id is the organization's Microsoft Entra tenant id (a GUID); "
            "its admin finds it under Entra ID > Overview"
        )
    if tenant == settings.tenant_id.lower():
        raise InvalidRequest("that is the operator's own tenant, which is always connected")
    role = (str(request.default_role).strip().title() or None) if request.default_role else None
    if role is not None and role not in ORG_ROLES:
        raise InvalidRequest(
            f"default_role is {' or '.join(ORG_ROLES)} (or none); Admin is granted per person"
        )
    domains = sorted({str(d).strip().lower() for d in request.domains or [] if str(d).strip()})
    for domain in domains:
        if not _DOMAIN.match(domain):
            raise InvalidRequest(f"{domain!r} is not a domain name")
    kb_id = context.config.knowledge_base_id
    taken = access_grants.snapshot(context.state, kb_id).domains
    clash = next((d for d in domains if taken.get(d, tenant) != tenant), None)
    if clash:
        raise InvalidRequest(f"{clash} already names another connected organization")
    row = access_grants.upsert(
        context.state,
        kb_id=kb_id,
        kind=access_grants.ORG,
        tenant_id=tenant,
        key=tenant,
        role=role,
        granted_by=caller["principal"],
        display_name=(request.name or "").strip() or None,
        domains=domains,
    )
    logger.info(
        "organization connected: by=%s tenant=%s default_role=%s",
        caller["principal"],
        tenant,
        role or "-",
    )
    return {"status": "connected", "organization": _organization(row, settings)}


def disconnect_organization(context: ServiceContext, tenant_id: str) -> dict[str, Any]:
    """Stop a tenant signing in, and drop the grants made to its people."""

    settings = _settings(context)
    require_permission("security.manage")
    caller = _caller(settings)
    tenant = str(tenant_id or "").strip().lower()
    kb_id = context.config.knowledge_base_id
    identifier = access_grants.grant_id(kb_id, access_grants.ORG, tenant, tenant)
    if access_grants.get(context.state, kb_id, identifier) is None:
        raise OrganizationNotConnected(tenant)
    if caller["person"] and caller["tenant_id"] == tenant:
        raise AccessRoleNotGrantable("you cannot disconnect the organization you signed in from")
    access_grants.delete(context.state, kb_id, identifier)
    removed = access_grants.delete_tenant_users(context.state, kb_id, tenant)
    logger.info(
        "organization disconnected: by=%s tenant=%s grants_removed=%d",
        caller["principal"],
        tenant,
        removed,
    )
    return {"status": "disconnected", "tenant_id": tenant, "grants_removed": removed}
