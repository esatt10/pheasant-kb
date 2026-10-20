"""Removing documents from the index, over MCP: archive, reinstate, list.

A mixin plus a registration function, like ``inventory_tools.py``. Three
adapters over `services.archive`, the operation behind ``POST
/documents/archive``, ``POST /documents/reinstate`` and ``GET
/documents/archived`` and the chat's ``@remove``:

* ``archive_documents`` — remove documents from the index and hold them out of
  every sync (an upload's bytes are deleted);
* ``reinstate_documents`` — let archived documents back in; their sources sync;
* ``list_archived_documents`` — what is held out, by source then path.

How this process reaches the index rides in the service context's
``indexing``: removed here when the tool facade owns the graph, published to
the indexer when it serves a remote one (``graph.query_service_url``).
"""

from __future__ import annotations

from typing import Any


class ArchiveTools:
    """``services.archive`` adapters. Parse, call, return."""

    services: Any

    def _require_knowledge_base(self, knowledge_base: str | None) -> None:  # pragma: no cover
        raise NotImplementedError

    def archive_documents(  # noqa: PLR0913 - one argument per field
        self,
        knowledge_base: str,
        documents: list[str],
        source_name: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import archive

        return archive.archive_documents(
            self.services,
            archive.ArchiveRequest(
                documents=documents,
                knowledge_base=knowledge_base,
                source_name=source_name,
                principal=principal,
                principal_groups=principal_groups,
                actor=principal or "mcp",
            ),
        )

    def reinstate_documents(  # noqa: PLR0913 - one argument per field
        self,
        knowledge_base: str,
        documents: list[str],
        source_name: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import archive

        return archive.reinstate_documents(
            self.services,
            archive.ArchiveRequest(
                documents=documents,
                knowledge_base=knowledge_base,
                source_name=source_name,
                principal=principal,
                principal_groups=principal_groups,
                actor=principal or "mcp",
            ),
        )

    def list_archived_documents(  # noqa: PLR0913 - one argument per filter
        self,
        knowledge_base: str,
        source_name: str | None = None,
        path_prefix: str | None = None,
        limit: int = 200,
        offset: int = 0,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import archive

        return archive.archived_documents(
            self.services,
            archive.ArchivedRequest(
                knowledge_base=knowledge_base,
                source_name=source_name,
                path_prefix=path_prefix,
                limit=limit,
                offset=offset,
                principal=principal,
                principal_groups=principal_groups,
            ),
        )


def register_archive_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
    """Register the archive tools. ``mcp`` and ``anticipated`` are handed in so
    this module imports no SDK, for the reason ``register_readiness_tools`` gives."""

    @mcp.tool()
    @anticipated
    def archive_documents(  # noqa: PLR0913 - one argument per field
        knowledge_base: str,
        documents: list[str],
        source_name: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        """Remove documents from the index and keep them out of every later sync.

        documents names each one by id, "<source>/<path>", path, or a path tail
        that matches exactly one document (a name matching several is refused
        with the candidates). Its chunks, full-text rows, embeddings and graph
        nodes are removed. A document from a folder, repository or connector
        source is archived: its file stays where it is, and incremental and
        full syncs skip it until reinstate_documents lets it back. An upload's
        bytes are deleted, so it cannot be reinstated (disposition "removed").
        Where the region publishes to an indexer, status is "queued" and the
        removal lands with that sync. With security.acl_enforced on, only
        documents the principal may read.
        """

        return tools.archive_documents(
            knowledge_base, documents, source_name, principal, principal_groups
        )

    @mcp.tool()
    @anticipated
    def reinstate_documents(  # noqa: PLR0913 - one argument per field
        knowledge_base: str,
        documents: list[str],
        source_name: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        """Let archived documents back into the index.

        documents names archived documents as list_archived_documents lists
        them (id, "<source>/<path>", path or a unique tail). Each one's source
        then runs an incremental sync, which reads the file again and rebuilds
        its chunks, embeddings and graph nodes; "sources" reports the sync (a
        job id, or the queued task where an indexer does the work). An upload
        that was removed is refused: its bytes were deleted.
        """

        return tools.reinstate_documents(
            knowledge_base, documents, source_name, principal, principal_groups
        )

    @mcp.tool()
    @anticipated
    def list_archived_documents(  # noqa: PLR0913 - one argument per filter
        knowledge_base: str,
        source_name: str | None = None,
        path_prefix: str | None = None,
        limit: int = 200,
        offset: int = 0,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        """List the documents removed from the index and held out of syncs.

        Optionally of one source, and of one folder of it (path_prefix). Each
        carries when and by whom it was archived and whether it is reinstatable
        (an upload awaiting deletion is not). Paged with limit (at most 500)
        and offset.
        """

        return tools.list_archived_documents(
            knowledge_base,
            source_name,
            path_prefix,
            limit,
            offset,
            principal,
            principal_groups,
        )
