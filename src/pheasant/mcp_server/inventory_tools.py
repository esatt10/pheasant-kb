"""What the knowledge base holds, over MCP: its overview and its documents.

A mixin plus a registration function, like ``readiness_tools.py`` and
``graph_tools.py``. `tools.py` and `server.py` both sit near their ceilings
in `tests/test_module_budget.py`, and the split the ratchet asks for is by
bounded context. Two adapters over `services.inventory`:

* ``describe_knowledge_base`` — identity, every source with its document
  count, and the file types in play;
* ``list_documents`` — the indexed documents, filtered by source, extension
  or path, and paged.

``ask_knowledge_base`` answers "list the sources" from these same functions
(``assistant.inventory``), so an agent that calls the tool and a person who
asks the question get one answer.
"""

from __future__ import annotations

from typing import Any


class InventoryTools:
    """``services.inventory`` adapters. Parse, call, return."""

    services: Any

    def _require_knowledge_base(self, knowledge_base: str | None) -> None:  # pragma: no cover
        raise NotImplementedError

    def describe_knowledge_base(
        self,
        knowledge_base: str,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import inventory

        return inventory.overview(self.services, knowledge_base, principal, principal_groups)

    def list_documents(  # noqa: PLR0913 - one argument per filter
        self,
        knowledge_base: str,
        source_name: str | None = None,
        extensions: list[str] | None = None,
        path_contains: str | None = None,
        order: str = "path",
        limit: int = 50,
        offset: int = 0,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import inventory

        return inventory.documents(
            self.services,
            inventory.DocumentsRequest(
                knowledge_base=knowledge_base,
                source_name=source_name,
                extensions=list(extensions or []),
                path_contains=path_contains,
                order=order,
                limit=limit,
                offset=offset,
                principal=principal,
                principal_groups=principal_groups,
            ),
        )


def register_inventory_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
    """Register the inventory tools. ``mcp`` and ``anticipated`` are handed in so
    this module imports no SDK, for the reason ``register_readiness_tools`` gives."""

    @mcp.tool()
    @anticipated
    def describe_knowledge_base(
        knowledge_base: str,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        """Describe what this knowledge base holds, without searching it.

        Returns its name and description, every source with its type, status,
        last-indexed time and document count, totals, and the file types in
        play. Use it for "which sources are there" or "how many documents",
        which search_context cannot answer: a search returns the passages that
        best match the words, not a list. Memory records and internal sources
        are not counted. With security.acl_enforced on, only documents the
        principal may read are counted.
        """

        return tools.describe_knowledge_base(knowledge_base, principal, principal_groups)

    @mcp.tool()
    @anticipated
    def list_documents(  # noqa: PLR0913 - one argument per filter
        knowledge_base: str,
        source_name: str | None = None,
        extensions: list[str] | None = None,
        path_contains: str | None = None,
        order: str = "path",
        limit: int = 50,
        offset: int = 0,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        """List the indexed documents, not search them.

        Filters: source_name (one source), extensions (["pdf", ".md"]),
        path_contains (a case-insensitive substring of the relative path).
        order is "path" (by source, then path) or "recent" (last indexed
        first). Pages with limit (at most 500) and offset, and returns the
        total with has_more. Each document's id is its graph node id, so
        get_graph_neighbors, explain_node and get_file_summary (with its path)
        follow on. Memory records are not listed (use memory_list). With
        security.acl_enforced on, only documents the principal may read.
        """

        return tools.list_documents(
            knowledge_base,
            source_name,
            extensions,
            path_contains,
            order,
            limit,
            offset,
            principal,
            principal_groups,
        )
