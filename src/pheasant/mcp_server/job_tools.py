"""Background work over MCP: exports as jobs, and waiting on any job.

A mixin plus a registration function, like ``fleet_tools.py``. Adapters over
``services.export_jobs`` and ``services.job_wait``, the operations behind
``POST /sources/{name}/exports`` and ``GET /jobs/{id}/wait``. The job record
is shared with HTTP, so an export started here shows in the UI's jobs tray and
the same request over either surface returns the same job.
"""

from __future__ import annotations

from typing import Any


class JobTools:
    """``services.export_jobs`` / ``services.job_wait`` adapters. Parse, call, return."""

    services: Any
    jobs: Any

    def start_source_export(  # noqa: PLR0913 - one argument per option
        self,
        knowledge_base: str,
        source_name: str,
        format: str = "zip",  # noqa: A002 - the tool's public argument name
        path_prefix: str | None = None,
        include_chunks: bool = False,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        from pheasant.services import export_jobs, source_export

        return export_jobs.start_export(
            self.services,
            self.jobs,
            source_export.ExportRequest(
                source_name=source_name,
                knowledge_base=knowledge_base,
                format=format,
                include_chunks=include_chunks,
                principal=principal,
                principal_groups=principal_groups,
                path_prefix=path_prefix,
            ),
        )

    def wait_for_job(self, job_id: str, timeout_seconds: float = 30.0) -> dict:
        from pheasant.services import job_wait

        return job_wait.wait_for_job(self.jobs, job_id, timeout_seconds)


def register_job_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
    """Register the job tools. ``mcp`` and ``anticipated`` are handed in so this
    module imports no SDK, for the reason ``register_readiness_tools`` gives."""

    @mcp.tool()
    @anticipated
    def start_source_export(  # noqa: PLR0913 - one argument per option
        knowledge_base: str,
        source_name: str,
        format: str = "zip",  # noqa: A002 - the tool's public argument name
        path_prefix: str | None = None,
        include_chunks: bool = False,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        """Export a source (or one folder of it, path_prefix) as ONE file, in the
        background, and return its job at once.

        Idempotent: asking again for the same export over unchanged content
        returns the job already running, or the finished job and its file
        (reused: "running" or "ready"). Follow it with wait_for_job; when it has
        succeeded, result.files lists the file(s) and result.download is the
        HTTP path that serves them (GET {download}?part=0). The job shows in the
        UI's jobs tray too. To read the documents inline instead, use
        export_source, which pages by size.
        """

        return tools.start_source_export(
            knowledge_base,
            source_name,
            format,
            path_prefix,
            include_chunks,
            principal,
            principal_groups,
        )

    @mcp.tool()
    @anticipated
    def wait_for_job(job_id: str, timeout_seconds: float = 30.0) -> dict:
        """Wait for a background job (an export, upload, sync or composition) to
        finish, for at most timeout_seconds (55 at most per call).

        Returns the job when it is done, or as it stands with timed_out true:
        call again with the same job_id to keep waiting. progress has its
        phase, current and total; result holds what it produced.
        """

        return tools.wait_for_job(job_id, timeout_seconds)
