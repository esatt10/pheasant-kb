"""What the knowledge base holds, over HTTP: its overview and its documents.

One router per plane, as `api/app.py`'s ceiling comment asks. Both handlers
parse a request, call `services.inventory`, and return its answer. The MCP
tools ``describe_knowledge_base`` / ``list_documents`` call the same
functions, and so does the assistant when a chat question is about the
knowledge base itself.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Query

from pheasant.services import inventory


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
