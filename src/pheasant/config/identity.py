"""Identity settings: sign-in with Microsoft, and managed identity between tiers.

Split out of ``schema.py`` (whose size is ratcheted) the way the assistant's
decision and voice settings were; both classes nest under
``security`` and are constructed from their annotations like any other section.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pheasant.config.model import ModelMixin


@dataclass
class EntraSettings(ModelMixin):
    """Sign-in with Microsoft Entra ID, beside the shared token (not instead).

    Off by default, and off it changes nothing: the shared bearer token above
    stays the whole feature for a region that does not set this. On, a caller
    may also present an Entra access token for this app (``az account
    get-access-token``), a session the region issued after a browser sign-in,
    a personal key a signed-in user minted, or an MCP OAuth token the region
    issued after the same sign-in. Every one resolves to a verified principal
    and an auth method, which is what the access log and the interaction
    ledger record. ``docs/security.md`` has the trust model.
    """

    enabled: bool = False
    #: The directory (tenant) id users and tokens must come from.
    tenant_id: str = ""
    #: The app registration's client id: the audience of user tokens, and the
    #: confidential client the region signs users in as.
    client_id: str = ""
    #: Further accepted audiences, e.g. ``api://<client id>`` for v1 tokens.
    audiences: list[str] = field(default_factory=list)
    #: A token or sign-in must carry one of these app roles. Empty admits any
    #: user of the tenant the app registration lets through.
    allowed_roles: list[str] = field(default_factory=lambda: ["Reader", "Contributor", "Admin"])
    #: Optional authoritative Terraform assignments, keyed by Entra object id.
    #: None uses verified claims; an empty map denies every Entra identity.
    role_assignments: dict[str, str] | None = None
    #: The externally reachable base URL (``https://host``): redirect URIs and
    #: the MCP OAuth metadata are absolute and cannot be derived per request
    #: behind an ingress.
    public_url: str = ""
    #: Client authentication when redeeming a sign-in code. A managed
    #: identity's client id here signs the request with that identity's token
    #: (a federated credential on the app registration, no secret anywhere);
    #: otherwise the secret in ``client_secret_env`` is sent.
    federated_identity_client_id: str = ""
    client_secret_env: str = "PHEASANT_ENTRA_CLIENT_SECRET"
    #: Lifetimes of what the region issues.
    session_hours: int = 12
    personal_key_max_days: int = 365
    oauth_access_minutes: int = 60
    oauth_refresh_days: int = 30


@dataclass
class ServiceIdentitySettings(ModelMixin):
    """Managed-identity tokens on the internal boundaries, beside their tokens.

    The api -> graph, indexer -> worker and api -> indexer (landing) calls each
    carry a per-boundary shared token. With this on, a process whose token
    variable is unset sends a managed-identity token for ``audience`` instead,
    and a receiver accepts one that carries the boundary's app role
    (``graph.query``, ``worker.prepare``, ``ingestion.land``). A configured
    token still works on both sides, so a fleet can move one boundary at a
    time. Off by default: nothing here runs without Azure.
    """

    enabled: bool = False
    tenant_id: str = ""
    #: The resource a sender asks its identity for (``api://<app id>``).
    audience: str = ""
    #: The internal app registration's client id: the ``aud`` of a v2 token.
    app_id: str = ""
    #: A user-assigned identity's client id; empty uses ``AZURE_CLIENT_ID``.
    managed_identity_client_id: str = ""


@dataclass
class ApiAuthSettings(ModelMixin):
    """First-party bearer auth for the HTTP/MCP surface (Phase 35.8).

    The HTTP API has no authentication of its own, and for one container on
    loopback that is the right trade: a local tool behind the operator's own
    perimeter. The fleet is a different deployment. There the API is a
    multi-replica Service that necessarily binds ``0.0.0.0``, and the only
    thing between it and the network is a port-publishing decision an
    operator can reasonably change — while the surface behind it can register
    a source over any allow-listed path and read what it finds.

    So this makes "behind an authenticating ingress" the default rather than
    the instruction; ``security.entra`` (``config/identity.py``) adds people.

    Roles other than ``all`` refuse to serve a non-loopback bind with neither
    of the two set — see :func:`pheasant.deployment.roles.validate_role`.
    ``all`` is exempt because a single container is the local-first tool and
    must keep starting with no configuration at all.
    """

    #: Environment variable holding the shared bearer token. The secret itself
    #: never belongs in YAML, so — like every other credential here — only the
    #: variable *name* is written to config.
    token_env: str = "PHEASANT_API_TOKEN"
    #: Additional bearer credentials: environment variable name -> app role.
    #: Values are read from the environment, never from config or API output.
    role_tokens: dict[str, str] = field(default_factory=dict)
    #: "Something in front of this authenticates callers." Turns off the
    #: startup refusal without turning on a token. Opt-in and explicit,
    #: because the failure it suppresses is silent by construction.
    behind_authenticating_proxy: bool = False
    #: Paths answerable without a token. The probes must stay open or the
    #: orchestrator cannot tell a healthy pod from an unauthenticated one, and
    #: ``/metrics`` is scraped by a collector that holds no pheasant identity.
    #: ``/internal/*`` is deliberately absent: those routes enforce their own
    #: per-boundary tokens and are exempted structurally, not by this list.
    public_paths: list[str] = field(default_factory=lambda: ["/health", "/ready", "/metrics"])
