"""What the knowledge base holds, over MCP: its overview and its documents.

A mixin plus a registration function, like ``readiness_tools.py`` and
``graph_tools.py``. `tools.py` and `server.py` both sit near their ceilings
in `tests/test_module_budget.py`, and the split the ratchet asks for is by
bounded context. Two adapters over `services.inventory`:

* ``describe_knowledge_base`` — identity, every source with its document
  count, and the file types in play;
* ``list_documents`` — the indexed documents, filtered by source, extension
  or path, and paged;
* ``describe_source`` / ``describe_document`` — one source or one document in
  detail, links in and out included (`services.inventory_detail`);
* ``list_document_links`` — document-to-document links, per source pair and
  edge type, paged.

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

    def describe_source(
        self,
        knowledge_base: str,
        source_name: str,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import inventory_detail

        return inventory_detail.source(
            self.services, source_name, knowledge_base, principal, principal_groups
        )

    def describe_document(
        self,
        knowledge_base: str,
        path: str,
        source_name: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import inventory_detail

        return inventory_detail.document(
            self.services, path, source_name, knowledge_base, principal, principal_groups
        )

    def list_document_links(  # noqa: PLR0913 - one argument per filter
        self,
        knowledge_base: str,
        source_name: str | None = None,
        other_source: str | None = None,
        edge_types: list[str] | None = None,
        cross_source_only: bool = False,
        limit: int = 50,
        offset: int = 0,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        document: str | None = None,
        direction: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import inventory_detail

        return inventory_detail.links(
            self.services,
            inventory_detail.LinksRequest(
                knowledge_base=knowledge_base,
                source_name=source_name,
                other_source=other_source,
                edge_types=list(edge_types or []),
                cross_source_only=cross_source_only,
                document=document,
                direction=direction,
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

    @mcp.tool()
    @anticipated
    def describe_source(
        knowledge_base: str,
        source_name: str,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        """Describe one source in detail, without searching it.

        Returns its type, location, status and last-indexed time; its document
        count and size; its documents by file type and by top-level directory;
        the five most recently indexed; and link counts per other source and
        edge type, outgoing (its documents link to) and incoming (linked from).
        Links are graph edges between two indexed documents: resolved imports,
        references, embeds and the like. With security.acl_enforced on, only
        documents the principal may read are counted.
        """

        return tools.describe_source(knowledge_base, source_name, principal, principal_groups)

    @mcp.tool()
    @anticipated
    def describe_document(
        knowledge_base: str,
        path: str,
        source_name: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        """Describe one indexed document: its row, outline, symbols and links.

        path is a relative path, "<source>/<path>", or a unique tail of one
        ("deploy.md"), matched without regard to case. When it names several
        documents, "document" is null and "candidates" lists them. Returns the
        chunk count, the section headings, the symbols it defines, the
        documents it links to ("links_to") and that link to it
        ("linked_from"), each with edge types and whether it crosses sources,
        and references that resolve to nothing this region holds. For the
        text itself use get_file_summary.
        """

        return tools.describe_document(
            knowledge_base, path, source_name, principal, principal_groups
        )

    @mcp.tool()
    @anticipated
    def list_document_links(  # noqa: PLR0913 - one argument per filter
        knowledge_base: str,
        source_name: str | None = None,
        other_source: str | None = None,
        edge_types: list[str] | None = None,
        cross_source_only: bool = False,
        limit: int = 50,
        offset: int = 0,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        document: str | None = None,
        direction: str | None = None,
    ) -> dict:
        """List links between indexed documents: how sources and files relate.

        A link is a graph edge whose two ends are both documents (resolved
        imports, references, embeds, links_to ...; never containment).
        source_name keeps links with an end in that source; with other_source
        too, links between the two in either direction. document (a path
        describe_document accepts, naming one document) keeps the links
        touching it, with direction "in" (links to it) or "out" (from it);
        this pages a document's backlinks. edge_types and cross_source_only
        narrow it. Returns a summary per (from_source,
        to_source, edge_type) and one row per linked document pair, paged with
        limit (at most 500) and offset.
        """

        return tools.list_document_links(
            knowledge_base,
            source_name,
            other_source,
            edge_types,
            cross_source_only,
            limit,
            offset,
            principal,
            principal_groups,
            document,
            direction,
        )
