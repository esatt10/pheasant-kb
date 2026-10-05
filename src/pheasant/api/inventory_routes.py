"""What the knowledge base holds, over HTTP: its overview, its documents, one
source, one document, and the links between documents.

One router per plane, as `api/app.py`'s ceiling comment asks. Every handler
parses a request, calls `services.inventory` or `services.inventory_detail`,
and returns its answer. The MCP inventory tools call the same functions, and
so does the assistant when a chat question is about the knowledge base itself.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Query

from pheasant.services import inventory, inventory_detail


def register_inventory_routes(app: FastAPI, *, services: Any) -> None:
    @app.get("/knowledge-base/overview")
    def knowledge_base_overview(
        principal: str | None = None,
        principal_groups: list[str] | None = Query(default=None),
    ) -> dict:
        """Sources with their document counts, totals and file types."""

        return inventory.overview(services, None, principal, principal_groups)

    @app.get("/documents")
    def list_documents(  # noqa: PLR0913 - one parameter per filter
        source_name: str | None = None,
        extension: list[str] | None = Query(default=None),
        path_contains: str | None = None,
        order: str = "path",
        limit: int = 50,
        offset: int = 0,
        principal: str | None = None,
        principal_groups: list[str] | None = Query(default=None),
    ) -> dict:
        """The indexed documents, filtered and paged. ``extension`` repeats."""

        return inventory.documents(
            services,
            inventory.DocumentsRequest(
                source_name=source_name,
                extensions=list(extension or []),
                path_contains=path_contains,
                order=order,
                limit=limit,
                offset=offset,
                principal=principal,
                principal_groups=principal_groups,
            ),
        )

    @app.get("/sources/{source_name}/overview")
    def describe_source(
        source_name: str,
        principal: str | None = None,
        principal_groups: list[str] | None = Query(default=None),
    ) -> dict:
        """One source in detail: what it holds and what it links to."""

        return inventory_detail.source(services, source_name, None, principal, principal_groups)

    @app.get("/documents/detail")
    def describe_document(
        path: str,
        source_name: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = Query(default=None),
    ) -> dict:
        """One document in detail: outline, symbols, links in and out."""

        return inventory_detail.document(
            services, path, source_name, None, principal, principal_groups
        )

    @app.get("/documents/links")
    def list_document_links(  # noqa: PLR0913 - one parameter per filter
        source_name: str | None = None,
        other_source: str | None = None,
        edge_type: list[str] | None = Query(default=None),
        cross_source_only: bool = False,
        document: str | None = None,
        direction: str | None = None,
        limit: int = 50,
        offset: int = 0,
        principal: str | None = None,
        principal_groups: list[str] | None = Query(default=None),
    ) -> dict:
        """Links between documents, summarised and paged. ``edge_type`` repeats."""

        return inventory_detail.links(
            services,
            inventory_detail.LinksRequest(
                source_name=source_name,
                other_source=other_source,
                edge_types=list(edge_type or []),
                cross_source_only=cross_source_only,
                document=document,
                direction=direction,
                limit=limit,
                offset=offset,
                principal=principal,
                principal_groups=principal_groups,
            ),
        )
