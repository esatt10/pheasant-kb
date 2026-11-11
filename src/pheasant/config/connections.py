"""``security.connections`` — accounts connected from the UI, MCP or HTTP.

Lives outside ``schema.py`` for the reason ``assistant_voice.py`` does: that
module is at its size ceiling, whose stated remedy is one module per section.
"""

from __future__ import annotations

from dataclasses import dataclass

from pheasant.config.model import ModelMixin


@dataclass
class ConnectionSettings(ModelMixin):
    """Credentials a person or an agent connects while the region runs.

    The house rule stays: no secret is ever written to YAML. A connection is a
    row in the state store (``connections``), sealed with AES-256-GCM under a
    key that is never stored beside it: the variable ``key_env`` names, or, on
    a single process whose ``/state`` is writable and has no such variable, a
    ``credential.key`` file it generates there once. A fleet sets the variable
    on the api and the indexer, the two tiers that open connections; a worker
    refuses to start holding it.

    An environment variable an operator set still wins over a connection that
    stands in for it (``OPENAI_API_KEY``, ``GITHUB_TOKEN`` …), and a source that
    names a connection (``connector.connection``) is read with that one.

    ``public_url`` is the address a browser returns to after signing in to a
    service (Google Drive): the callback is ``{public_url}/connections/oauth/
    callback``. Unset, it is the address the browser used, as the region's own
    sign-in derives it. Each service's OAuth client is read from its own pair of
    variables (``GITHUB_OAUTH_CLIENT_ID``/``_SECRET``, ``MICROSOFT_...``,
    ``GITLAB_...``, ``GOOGLE_...``; ``connections/oauth.py``), so a person only
    presses "Sign in with ..."; without one the form asks for a client or a
    token. ``microsoft_tenant`` is the Entra tenant a Microsoft sign-in goes to:
    ``common`` takes work, school and personal accounts.
    """

    enabled: bool = True
    key_env: str = "PHEASANT_CREDENTIAL_KEY"
    #: Generate ``<state_path>/credential.key`` when ``key_env`` is unset and
    #: ``/state`` is writable. Off, a region without the variable connects
    #: nothing and says so.
    generate_key_file: bool = True
    public_url: str | None = None
    microsoft_tenant: str = "common"
    #: How long a process reuses what it read of the table before reading it
    #: again: a connection made on the api reaches the indexer within this.
    cache_seconds: int = 15
    #: How long a browser has to finish a service's sign-in.
    oauth_timeout_seconds: int = 900

    def __post_init__(self) -> None:
        if not 0 <= int(self.cache_seconds) <= 3600:
            raise ValueError("security.connections.cache_seconds must be between 0 and 3600")
        if not 60 <= int(self.oauth_timeout_seconds) <= 86400:
            raise ValueError(
                "security.connections.oauth_timeout_seconds must be between 60 and 86400"
            )
