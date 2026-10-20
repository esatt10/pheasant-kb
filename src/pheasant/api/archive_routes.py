"""Removing documents from the index, over HTTP: archive, reinstate, list.

One router per plane, as `api/app.py`'s ceiling comment asks. Every handler
parses a request and calls `services.archive`; the MCP tools
``archive_documents`` / ``reinstate_documents`` / ``list_archived_documents``
call the same functions, and so does the chat's ``@remove``. How this process
reaches the index (here, or by publishing to an indexer) rides in the service
context's ``indexing``, which `api/app.py` builds from the role.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

from pheasant.services import archive


class ArchiveDocumentsRequest(BaseModel):
    #: Ids, ``source/path``, paths, or a unique path tail. One document each.
    documents: list[str] = Field(default_factory=list)
    source_name: str | None = None
    principal: str | None = None
    principal_groups: list[str] | None = None


def register_archive_routes(app: FastAPI, *, services: Any) -> None:
    @app.post("/documents/archive")
    def archive_documents(req: ArchiveDocumentsRequest) -> dict:
        """Remove documents from the index and hold them out of every sync.

        A folder or connector document is archived and can be reinstated; an
        upload's bytes are deleted. On an api replica the removal is queued
        for the indexer (``status: queued``)."""

        return archive.archive_documents(services, _request(req, "http"))

    @app.post("/documents/reinstate")
    def reinstate_documents(req: ArchiveDocumentsRequest) -> dict:
        """Let archived documents back in; their sources sync to read them again."""

        return archive.reinstate_documents(services, _request(req, "http"))

    @app.get("/documents/archived")
    def archived_documents(  # noqa: PLR0913 - one parameter per filter
        source_name: str | None = None,
        path_prefix: str | None = None,
        limit: int = 200,
        offset: int = 0,
        principal: str | None = None,
        principal_groups: list[str] | None = Query(default=None),
    ) -> dict:
        """The documents held out of the index, by source then path, paged."""

        return archive.archived_documents(
            services,
            archive.ArchivedRequest(
                source_name=source_name,
                path_prefix=path_prefix,
                limit=limit,
                offset=offset,
                principal=principal,
                principal_groups=principal_groups,
            ),
        )


def _request(req: ArchiveDocumentsRequest, actor: str) -> archive.ArchiveRequest:
    return archive.ArchiveRequest(
        documents=list(req.documents),
        source_name=req.source_name,
        principal=req.principal,
        principal_groups=req.principal_groups,
        actor=req.principal or actor,
    )
