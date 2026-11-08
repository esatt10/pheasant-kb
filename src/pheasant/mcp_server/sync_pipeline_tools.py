"""Every source's sync pipeline, over MCP: see it, configure it, delete it, read its ledger.

A mixin plus a registration function, like ``sync_control_tools.py``.
Adapters over `services.sync_pipelines`, which ``/sync/pipelines`` and the
Syncs page use too.
"""

from __future__ import annotations

from typing import Any


class SyncPipelineTools:
    """``services.sync_pipelines`` adapters. Parse, call, return."""

    services: Any

    def _require_knowledge_base(self, knowledge_base: str | None) -> None:  # pragma: no cover
        raise NotImplementedError

    def list_sync_pipelines(self, knowledge_base: str) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import sync_pipelines

        return sync_pipelines.pipelines(self.services, knowledge_base)

    def describe_sync_pipeline(self, knowledge_base: str, source_name: str) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import sync_pipelines

        return sync_pipelines.pipeline(self.services, source_name, knowledge_base)

    def configure_sync_pipeline(
        self,
        knowledge_base: str,
        source_name: str,
        settings: dict[str, Any],
        sync_now: bool = False,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import sync_pipelines

        return sync_pipelines.configure(
            self.services,
            sync_pipelines.PipelineChange(
                source_name, knowledge_base, dict(settings or {}), sync=sync_now
            ),
        )

    def delete_sync_pipeline(self, knowledge_base: str, source_name: str, what: str) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import sync_pipelines

        return sync_pipelines.delete(self.services, source_name, what, knowledge_base)

    def list_pipeline_documents(  # noqa: PLR0913 - one argument per filter
        self,
        knowledge_base: str,
        source_name: str,
        state: str = "all",
        path_contains: str | None = None,
        path: str | None = None,
        limit: int = 100,
        offset: int = 0,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import sync_pipelines

        if path:
            return sync_pipelines.document(
                self.services, source_name, path, knowledge_base, principal, principal_groups
            )
        return sync_pipelines.documents(
            self.services,
            sync_pipelines.DocumentsRequest(
                source_name=source_name,
                knowledge_base=knowledge_base,
                state=state,
                path_contains=path_contains,
                limit=limit,
                offset=offset,
                principal=principal,
                principal_groups=principal_groups,
            ),
        )


def register_sync_pipeline_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
    @mcp.tool()
    @anticipated
    def list_sync_pipelines(knowledge_base: str) -> dict:
        """List every source's sync pipeline: how and when it syncs, and how it went.

        One pipeline per source, made when the source was added. Each row has
        ``state`` (scheduled, paused, manual or deleted), ``schedule`` in words,
        ``last_run`` (status, times, counts, ``trace_id``), ``running`` and
        ``queued``, ``next_run``, ``due_next_hour`` (web pages), ``health``
        (percent readable), ``quality`` (files ok / warning / error) and
        ``pacing`` (a site's crawl delay from robots.txt, or how many pages are
        fetched at once). Change one with configure_sync_pipeline.
        """

        return tools.list_sync_pipelines(knowledge_base)

    @mcp.tool()
    @anticipated
    def describe_sync_pipeline(knowledge_base: str, source_name: str) -> dict:
        """Describe one source's sync pipeline in depth.

        Everything list_sync_pipelines gives, plus ``stages`` (seconds per
        stage of the latest run: listing, fetching, preparing, committing,
        embedding, ...), ``runs`` (the last ten, each with its ``trace_id``:
        its OpenTelemetry spans are in the region's logs under that id),
        ``failures`` (the latest run's unreadable documents grouped by cause,
        e.g. "HTTP 404"), ``next_due`` (the web pages checked next, with when)
        and ``settings`` (every value configure_sync_pipeline can change).
        """

        return tools.describe_sync_pipeline(knowledge_base, source_name)

    @mcp.tool()
    @anticipated
    def configure_sync_pipeline(
        knowledge_base: str,
        source_name: str,
        settings: dict[str, Any],
        sync_now: bool = False,
    ) -> dict:
        """Change how and when one source syncs. Only the settings given change.

        ``settings`` keys: ``schedule`` ("auto": the scheduler and the source's
        own triggers; "manual": only when asked), ``paused`` (hold an auto
        schedule), ``pipeline`` (false deletes it: nothing syncs the source),
        ``interval_seconds`` (web: the shortest recheck interval; folders and
        repositories: unused), ``max_refresh_seconds`` (web: the longest),
        ``max_items_per_run`` (web: pages fetched per run at most),
        ``min_request_interval_seconds`` (web: seconds between requests to a
        site; null reads its robots.txt), ``respect_robots``, ``on_startup``,
        ``on_file_change``, ``on_git_commit``. ``sync_now`` starts a sync after
        saving. Returns the pipeline as describe_sync_pipeline does.
        """

        return tools.configure_sync_pipeline(knowledge_base, source_name, settings, sync_now)

    @mcp.tool()
    @anticipated
    def delete_sync_pipeline(knowledge_base: str, source_name: str, what: str) -> dict:
        """Delete a pipeline's schedule, or the pipeline itself.

        ``what="schedule"``: the source syncs only when asked (sync_source).
        ``what="pipeline"``: nothing syncs it, not even when asked; its
        indexed documents stay searchable until the source is removed
        (remove_source). configure_sync_pipeline with ``pipeline: true``
        brings a deleted pipeline back.
        """

        return tools.delete_sync_pipeline(knowledge_base, source_name, what)

    @mcp.tool()
    @anticipated
    def list_pipeline_documents(  # noqa: PLR0913 - one argument per filter
        knowledge_base: str,
        source_name: str,
        state: str = "all",
        path_contains: str | None = None,
        path: str | None = None,
        limit: int = 100,
        offset: int = 0,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        """List a pipeline's documents with when each was read, changed and is next checked.

        Each document has ``last_read_at``, ``last_changed_at``,
        ``next_check_at`` (web pages; ``next_check`` says "on change" or "next
        beat" for files), ``interval_seconds``, ``server_said`` (200, 304, or the
        HTTP error), ``validator`` and its quality: ``status`` and ``flags``
        with reasons. ``state`` filters: all, due_soon (next hour), changed_week,
        attention, unreadable, stale, little_text; ``totals`` counts each.
        ``path`` (a path, URL or id) returns one document instead, with the
        recent runs that could not read it.
        """

        return tools.list_pipeline_documents(
            knowledge_base,
            source_name,
            state,
            path_contains,
            path,
            limit,
            offset,
            principal,
            principal_groups,
        )
