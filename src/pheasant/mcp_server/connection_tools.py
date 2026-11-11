"""Connected accounts and one-field source adds, over MCP.

A mixin plus a registration function, like ``source_url_tools.py``. Adapters
over `services.connections` (the operations behind ``/connections*``) and
`services.quick_add` (behind ``POST /sources/quick-add``):

* ``list_connections`` -- what is connected (never a secret) and what can be.
* ``connect_service`` -- a token or key, checked with its service, then stored.
* ``start_service_sign_in`` -- a browser sign-in (Google Drive): returns the
  address for the person to open; an agent cannot click "Allow" for them.
* ``test_connection`` / ``remove_connection``.
* ``add_source`` -- anything in one field: a path, a repository URL, a page,
  ``gdrive:``, read with ``connection`` when it names one.
"""

from __future__ import annotations

from typing import Any


class ConnectionTools:
    """``services.connections`` and ``services.quick_add`` adapters."""

    services: Any
    config: Any

    def _require_knowledge_base(self, knowledge_base: str | None) -> None:  # pragma: no cover
        raise NotImplementedError

    def _connected(self, name: str | None) -> str | None:
        """``name`` when it is a connection this region holds (``register_source``)."""

        if not name:
            return None
        from pheasant.connections import vault
        from pheasant.services.errors import ConnectionNotFound

        row = vault.get(self.services.state, name)
        if row is None or row.status != "connected":
            raise ConnectionNotFound(name)
        return name

    def list_connections(self) -> dict:
        from pheasant.services import connections

        return connections.list_connections(self.services)

    def connect_service(  # noqa: PLR0913 - one argument per field
        self,
        provider: str,
        fields: dict[str, str] | None = None,
        name: str | None = None,
        method: str | None = None,
        verify: bool = True,
        actor: str = "mcp",
    ) -> dict:
        from pheasant.services import connections

        return connections.connect(
            self.services,
            connections.ConnectRequest(
                provider=provider,
                fields=dict(fields or {}),
                name=name,
                method=method,
                verify=verify,
                actor=actor,
                transport="mcp",
            ),
        )

    def start_service_sign_in(
        self,
        provider: str = "google_drive",
        name: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> dict:
        from pheasant.services import connections

        settings = self.config.security.connections
        public = settings.public_url or getattr(self.config.security.entra, "public_url", None)
        return connections.start_sign_in(
            self.services,
            connections.SignInRequest(
                provider=provider,
                name=name,
                client_id=client_id,
                client_secret=client_secret,
                public_url=str(public or ""),
                actor="mcp",
            ),
        )

    def test_connection(self, name: str) -> dict:
        from pheasant.services import connections

        return connections.check(self.services, name)

    def remove_connection(self, name: str, force: bool = False) -> dict:
        from pheasant.services import connections

        return connections.remove(self.services, name, force=force)

    def add_source(  # noqa: PLR0913 - one argument per field
        self,
        knowledge_base: str,
        target: str,
        connection: str | None = None,
        name: str | None = None,
        split: bool = False,
        taxonomy: bool = False,
        sync_now: bool = True,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services.quick_add import AddTargetRequest, add_targets

        result = add_targets(
            self.services,
            AddTargetRequest(
                target=target,
                name=name,
                split=split,
                taxonomy=taxonomy,
                connection=connection,
                actor="mcp",
                transport="mcp",
            ),
        )
        syncs = []
        indexing = self.services.indexing
        if sync_now and indexing is not None:
            syncs = [indexing.start_sync(entry["name"]) for entry in result["sources"]]
        return {**result, "syncs": syncs}


def register_connection_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
    @mcp.tool()
    @anticipated
    def list_connections() -> dict:
        """List connected accounts and what can be connected.

        ``connections`` names each one's provider, the account its service
        reported, a four-character hint and which sources read with it -- never
        the secret. ``providers`` is the catalog: each provider's ``methods``
        and the ``fields`` each method takes, which is what ``connect_service``
        expects. ``vault.available`` false means this region holds no key to
        store accounts with (its ``reason`` says what to set)."""

        return tools.list_connections()

    @mcp.tool()
    @anticipated
    def connect_service(
        provider: str,
        fields: dict[str, str] | None = None,
        name: str | None = None,
        method: str | None = None,
        verify: bool = True,
    ) -> dict:
        """Connect an account with a token or key: GitHub, GitLab, Azure DevOps,
        Google Drive (service account or access token), an API token, OpenAI,
        Azure AI Foundry, Anthropic, Gemini, or any environment variable.

        ``fields`` are the method's fields from ``list_connections`` (e.g.
        ``{"token": "..."}`` for GitHub, ``{"api_key": "...", "endpoint":
        "https://<resource>.services.ai.azure.com"}`` for Foundry). With
        ``verify`` the service is asked first, and a secret it rejects is not
        stored (CONNECTION_REJECTED). ``name`` defaults to the provider. A model
        key stands in for its variable (OPENAI_API_KEY ...) unless the operator
        set that variable. Then add a private repository or Drive with
        ``add_source(target, connection=name)``. A secret passed here passes
        through this conversation; prefer the web UI for one you would not
        paste into a chat."""

        return tools.connect_service(provider, fields, name, method, verify)

    @mcp.tool()
    @anticipated
    def start_service_sign_in(
        provider: str = "google_drive",
        name: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> dict:
        """Begin a browser sign-in to a service (Google Drive).

        Returns ``authorize_url``: give it to the person to open. After they
        allow access, the connection named ``name`` appears in
        ``list_connections``. ``client_id``/``client_secret`` are a Google OAuth
        client, needed only when the region has none configured."""

        return tools.start_service_sign_in(provider, name, client_id, client_secret)

    @mcp.tool()
    @anticipated
    def test_connection(name: str) -> dict:
        """Ask a connection's service again whether it still accepts it."""

        return tools.test_connection(name)

    @mcp.tool()
    @anticipated
    def remove_connection(name: str, force: bool = False) -> dict:
        """Remove a connection. Refused while a source reads with it, unless
        ``force`` (those sources then stop syncing until it is reconnected)."""

        return tools.remove_connection(name, force)

    @mcp.tool()
    @anticipated
    def add_source(
        knowledge_base: str,
        target: str,
        connection: str | None = None,
        name: str | None = None,
        split: bool = False,
        taxonomy: bool = False,
        sync_now: bool = True,
    ) -> dict:
        """Add a source from one string, the way the web UI's Add box does.

        ``target`` is a folder path, a repository URL (github.com, gitlab.com,
        dev.azure.com ...), a web page URL, ``gdrive:`` for a Google Drive, or a
        glob of folders with ``split``. ``connection`` names the account to
        read it with (a private repository's token, a Drive sign-in), from
        ``list_connections``. Detects the type, clones what needs cloning,
        registers and (with ``sync_now``) starts indexing; follow it with
        ``wait_for_sync``."""

        return tools.add_source(knowledge_base, target, connection, name, split, taxonomy, sync_now)
