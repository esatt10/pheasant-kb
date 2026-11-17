"""Waiting for a background job, bounded, the same way on every surface.

``GET /jobs/{id}`` and MCP ``get_job`` answer "where is it now"; a caller that
wants "tell me when it is done" otherwise polls on its own clock. This is that
wait, held for at most :data:`MAX_WAIT_SECONDS` per call so it fits inside a
proxy's idle timeout and an MCP client's tool timeout (as ``wait_for_sync``
does): it returns the job when it finishes, or as it stands with
``timed_out: true``, and the caller calls again. ``GET /jobs/{id}/events``
streams the same record as server-sent events for a reader that keeps a
connection open.
"""

from __future__ import annotations

import time
from typing import Any

from pheasant.services.errors import InvalidRequest, UnknownJob

MAX_WAIT_SECONDS = 55.0
POLL_SECONDS = 0.5


def wait_for_job(jobs: Any, job_id: str, timeout_seconds: float = 30.0) -> dict[str, Any]:
    """The job once it is terminal, or within ``timeout_seconds`` with ``timed_out``."""

    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise InvalidRequest(f"timeout_seconds must be a number; got {timeout_seconds!r}") from exc
    timeout = max(0.0, min(timeout, MAX_WAIT_SECONDS))
    deadline = time.monotonic() + timeout
    while True:
        job = jobs.get(job_id)
        if job is None:
            raise UnknownJob(job_id)
        if not job.get("active"):
            return {**job, "timed_out": False}
        if time.monotonic() >= deadline:
            return {**job, "timed_out": True}
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
