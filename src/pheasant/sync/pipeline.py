"""A source's sync pipeline: whether it runs by itself, and how it reads in words.

Every source has a pipeline from the moment it is added -- adding a source
stays one field -- and the Syncs page (`services/sync_pipelines.py`) is where
it is changed. Its settings live on ``sources[].sync``
(`config.schema.SourceSyncSettings`):

* ``schedule: auto`` runs on the scheduler beat, at startup and on the
  source's own triggers (a file change, a commit); ``manual`` only when
  someone asks. Deleting a schedule is ``manual``.
* ``paused`` holds an ``auto`` schedule without forgetting it.
* ``pipeline: false`` is a deleted pipeline: nothing syncs the source, not
  even by hand, and its indexed documents stay until the source is removed.

:func:`runs_automatically` is the one test every automatic trigger asks, so
a paused or manual pipeline cannot be started by a path that forgot.
"""

from __future__ import annotations

from typing import Any

SCHEDULES = ("auto", "manual")


class PipelineDeleted(ValueError):
    """A sync was asked of a source whose pipeline was deleted."""

    status = 409
    code = "PIPELINE_DELETED"
    retryable = False

    def __init__(self, name: str) -> None:
        super().__init__(
            f"{name}'s sync pipeline was deleted, so nothing syncs it. Restore the pipeline "
            "on the Syncs page (or configure_sync_pipeline with pipeline=true) to sync it again."
        )


def runs_automatically(source: Any) -> bool:
    """Whether the beat, startup and a source's own triggers may sync it now."""

    sync = source.sync
    return (
        bool(getattr(source, "enabled", True))
        and getattr(sync, "pipeline", True)
        and getattr(sync, "schedule", "auto") == "auto"
        and not getattr(sync, "paused", False)
    )


def startup_sources(sources: Any) -> list[str]:
    """The names of ``sources`` a boot syncs: ``on_startup`` and running by itself."""

    return [s.name for s in sources if s.sync.on_startup and runs_automatically(s)]


def require_pipeline(source: Any) -> None:
    """Refuse a sync of a source whose pipeline was deleted."""

    if not getattr(source.sync, "pipeline", True):
        raise PipelineDeleted(str(source.name))


def state_of(source: Any) -> str:
    """``deleted``, ``paused``, ``manual`` or ``scheduled``."""

    sync = source.sync
    if not getattr(sync, "pipeline", True):
        return "deleted"
    if getattr(sync, "schedule", "auto") != "auto":
        return "manual"
    if getattr(sync, "paused", False):
        return "paused"
    return "scheduled"
