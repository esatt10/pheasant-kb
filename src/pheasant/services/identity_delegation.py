"""Delegated access inside the identity service: grants, organizations, tenants.

The part of :class:`pheasant.services.identity.IdentityService` that exists
only when ``security.entra.delegated_access`` is on, split out as a mixin (that
module is at its size ceiling). What a grant *is* and who may make one is
``services/access.py``; this is how one becomes a role on a request, how a
connected organization's tokens are verified against that organization's own
keys, and how a sign-in is sent to the right directory.
"""

from __future__ import annotations

import logging
from typing import Any

from pheasant.persistence import access_grants, auth_tokens, fleet_store
from pheasant.security import entra
from pheasant.security.identity import Identity
from pheasant.services.errors import SignInFailed

logger = logging.getLogger("pheasant.services.identity")

_RANK = {"Reader": 1, "Contributor": 2, "Admin": 3}


def higher_role(first: str | None, second: str | None) -> str | None:
    """The stronger of two roles; an unknown one counts as none."""

    return max((first, second), key=lambda role: _RANK.get(role or "", 0)) or None


class DelegatedAccess:
    """Mixed into ``IdentityService``, which supplies the attributes below."""

    config: Any
    settings: Any
    state: Any
    kb_id: str
    verifier: Any
    _verifiers: dict[tuple[str, bool], Any]
    _lock: Any
    entra_enabled: bool

    @property
    def delegated(self) -> bool:
        """Whether access is granted at runtime rather than read off a token."""

        return bool(self.entra_enabled and self.settings.delegated_access)

    def grants(self) -> access_grants.Snapshot:
        return access_grants.snapshot(self.state, self.kb_id)

    def _granted_role(self, identity: Identity) -> str | None:
        """What the grants give this person: their own, or their organization's."""

        snapshot = self.grants()
        tenant = identity.tenant_id
        org = snapshot.orgs.get(tenant)
        if tenant != self.settings.tenant_id and tenant not in self.admitted_tenants():
            return None  # an organization nobody connected, or one since removed
        grant = snapshot.user(tenant, identity.subject, identity.principal)
        role = higher_role(grant.get("role") if grant else None, org.get("role") if org else None)
        return higher_role(role, self._fleet_role(identity))

    def admitted_tenants(self) -> set[str]:
        """Tenants whose people may sign in here besides the operator's: the
        connected organizations, and the ones this knowledge base's own admins
        come from (a fleet claim, ``services/fleet.py``)."""

        if not self.delegated:
            return set()
        return set(self.grants().orgs) | fleet_store.admin_tenants(self.config)

    def _fleet_role(self, identity: Identity) -> str | None:
        """Admin, for someone the fleet directory names as this stack's admin."""

        for admin in fleet_store.slot_admins(self.config):
            if admin.get("tenant_id") != identity.tenant_id:
                continue
            if admin.get("subject") and admin["subject"] == identity.subject:
                return "Admin"
            if not admin.get("subject") and admin.get("email") == identity.principal:
                return "Admin"
        return None

    def _bind_grant(self, identity: Identity) -> None:
        """Pin a grant made by sign-in name to the object id that just proved it."""

        if not self.delegated or not identity.subject:
            return
        grant = self.grants().user(identity.tenant_id, identity.subject, identity.principal)
        if grant is None or grant.get("subject"):
            return
        try:
            access_grants.bind_subject(
                self.state, self.kb_id, grant["id"], identity.subject, identity.display_name
            )
        except Exception:  # noqa: BLE001 - bookkeeping must never fail a sign-in
            logger.debug("could not bind grant %s", grant["id"], exc_info=True)

    def _verifier_for(self, tenant_id: str, *, id_token: bool = False) -> Any:
        """The verifier for one tenant's tokens, or ``None`` when it is not admitted."""

        tenant_id = str(tenant_id or "")
        home = tenant_id == self.settings.tenant_id
        if home and not id_token:
            return self.verifier
        if not home and tenant_id not in self.admitted_tenants():
            return None
        with self._lock:
            verifier = self._verifiers.get((tenant_id, id_token))
            if verifier is None:
                audiences = (
                    [self.settings.client_id]
                    if id_token
                    else [self.settings.client_id, *self.settings.audiences]
                )
                verifier = entra.EntraVerifier(tenant_id, audiences)
                self._verifiers[(tenant_id, id_token)] = verifier
        return verifier

    def organization_tenant(self, org: str) -> str:
        """The tenant id ``org`` names: the operator's, or a connected organization's."""

        named = str(org or "").strip().lower()
        if named in {self.settings.tenant_id.lower(), "home"}:
            return self.settings.tenant_id
        for tenant in self.admitted_tenants():
            if tenant.lower() == named:
                return tenant
        if self.delegated and named in self.grants().domains:
            return self.grants().domains[named]
        raise SignInFailed("that organization is not connected to this knowledge base")

    def choice_request(self, credential: str) -> dict[str, Any] | None:
        return auth_tokens.lookup(self.state, credential, kinds=("login_choice",))

    def choose_organization(self, credential: str, org: str, *, base_url: str) -> str:
        """Settle ``/auth/choose``: the Entra URL for the organization picked."""

        record = auth_tokens.consume(self.state, credential, kind="login_choice")
        if record is None:
            raise SignInFailed("the sign-in expired or was already used; start again")
        data = dict(record["data"])
        return self.begin_login(
            flow=str(data.pop("flow", "ui")), base_url=base_url, data=data, org=org or "home"
        )
