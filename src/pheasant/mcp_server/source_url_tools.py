"""A web collection's pages, over MCP: add some, take some out.

A mixin plus a registration function, like ``archive_tools.py``. One adapter
over `services.source_urls`, the operation behind ``POST
/sources/{name}/urls``. ``register_source`` sets a source's whole URL list;
this edits it, so an agent adding a page does not have to restate every page
already there - and cannot replace the collection by forgetting to.
"""

from __future__ import annotations

from typing import Any


class SourceUrlTools:
    """``services.source_urls`` adapter. Parse, call, return."""

    services: Any
    config: Any

    def _require_knowledge_base(self, knowledge_base: str | None) -> None:  # pragma: no cover
        raise NotImplementedError

    def edit_source_urls(  # noqa: PLR0913 - one argument per field
        self,
        knowledge_base: str,
        source_name: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        sync_now: bool = True,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import source_urls

        return source_urls.edit_urls(
            self.services,
            source_urls.UrlEditRequest(
                source_name=source_name,
                add=list(add or []),
                remove=list(remove or []),
                knowledge_base=knowledge_base,
                allow_private=bool(self.config.security.allow_agent_private_urls),
                sync=sync_now,
            ),
        )


def register_source_url_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
    @mcp.tool()
    @anticipated
    def edit_source_urls(
        knowledge_base: str,
        source_name: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        sync_now: bool = True,
    ) -> dict:
        """Add pages to a web collection (or API source) or take them out.

        ``add`` joins URLs on after the ones already listed, each once;
        ``remove`` takes URLs off. Every other page stays as indexed. Returns
        ``urls`` (the list afterwards), ``added``, ``removed``,
        ``already_listed`` and ``not_listed``. With ``sync_now`` the source
        syncs, which fetches an added page and removes a removed one from the
        index. Use this rather than ``register_source`` to change one page:
        ``register_source`` replaces the whole list. Only public addresses are
        accepted unless ``security.allow_agent_private_urls`` is set.
        """

        return tools.edit_source_urls(knowledge_base, source_name, add, remove, sync_now)
