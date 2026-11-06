"""Who made this request, and how they proved it.

Before this, every principal in pheasant was *asserted*: an
``x-pheasant-principal`` header, a ``principal`` tool argument. Nothing
checked them, and ``telemetry.interactions`` says so. An :class:`Identity` is
the other kind -- the auth middleware sets one only after a credential was
verified -- and it travels with the request in a context variable, so the MCP
tool observer one mount down and the access log both read the same answer
without anything being threaded through the call stack.

``method`` is part of the record on purpose. "esa@example.com" signed in
through the browser and "esa@example.com" presenting a personal key are the
same person and different events: a leaked key shows up as the second.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field

#: How a credential was proven. Public vocabulary: the access log and the
#: interaction ledger carry these strings, so they are added to, never renamed.
API_TOKEN = "api_token"  # the region's shared bearer token
ENTRA_TOKEN = "entra_token"  # an Entra access token for this app (az login)
ENTRA_SSO = "entra_sso"  # a session issued after a browser sign-in
PERSONAL_KEY = "personal_key"  # a key a signed-in user minted
MCP_OAUTH = "mcp_oauth"  # a token issued to an MCP client after a sign-in
SERVICE_TOKEN = "service_token"  # a per-boundary internal token
MANAGED_IDENTITY = "managed_identity"  # an internal call proven by an identity

#: The principal the shared token resolves to. It names a credential, not a
#: person, which is the honest answer: anyone holding it is this principal.
SHARED_TOKEN_PRINCIPAL = "shared-token"


@dataclass(frozen=True)
class Identity:
    principal: str
    method: str
    display_name: str = ""
    subject: str = ""  # Entra object id, when there is one
    roles: tuple[str, ...] = field(default_factory=tuple)
    token_id: str = ""  # the issued credential's id, for revocation and audit

    @property
    def is_person(self) -> bool:
        """Signed in as somebody, rather than holding a shared credential."""

        return self.method in {ENTRA_TOKEN, ENTRA_SSO, PERSONAL_KEY, MCP_OAUTH}

    def as_dict(self) -> dict[str, object]:
        return {
            "principal": self.principal,
            "auth_method": self.method,
            "display_name": self.display_name or self.principal,
            "roles": list(self.roles),
            "person": self.is_person,
        }


_CURRENT: contextvars.ContextVar[Identity | None] = contextvars.ContextVar(
    "pheasant_identity", default=None
)


def current_identity() -> Identity | None:
    return _CURRENT.get()


def bind_identity(identity: Identity | None) -> contextvars.Token:
    return _CURRENT.set(identity)


def reset_identity(token: contextvars.Token) -> None:
    _CURRENT.reset(token)


def effective_principal(asserted: str | None) -> str | None:
    """The verified principal when there is one, else whatever was asserted."""

    identity = _CURRENT.get()
    if identity is not None and identity.principal:
        return identity.principal
    return asserted or None
