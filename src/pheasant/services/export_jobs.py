"""Exporting as a job: started once, watched from anywhere, downloaded when ready.

A source's export takes as long as reading every document it holds -- 254 MB
of an uploads source is tens of seconds -- and a request that blocks for that
long holds the person's attention on one panel. So :func:`start_export` returns
a job at once, and the work runs in the background of the process that took the
request. It shows in the jobs tray (``GET /jobs``, ``/jobs/stream``), over MCP
(``get_job``, ``wait_for_job``), and the finished file is fetched with
``GET /jobs/{id}/download`` while the person carries on with something else.

**Idempotent.** An export is named by what it would contain: the knowledge
base, source, folder, format, chunk option, reader, and every selected
document's id and content hash. Asking again while it runs returns the same
job; asking again once it has finished, with nothing changed, returns that
finished job and its file. A change to any document is a new export.

**Any replica can serve it.** Files are written under
``<exports_path>/source-exports/<kb>/<key>/``: ``/exports`` is the volume every
api replica mounts read-write (``/state`` is read-only there), and job records
are already shared, so a download need not reach the replica that wrote it. A
claim file created with ``O_EXCL`` in that directory stops two replicas that
receive the same request at once from both writing it.

Files are regenerable payloads, kept :data:`KEEP_SECONDS` and swept when the
next export starts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from pheasant.services import ServiceContext, source_export
from pheasant.services.errors import ExportExpired, ExportNotReady, UnknownJob

logger = logging.getLogger(__name__)

SUBDIR = "source-exports"
#: How long a finished export's files are kept for downloading.
KEEP_SECONDS = 24 * 3600
#: A claim whose job nobody can see any more is abandoned after this long.
CLAIM_STALE_SECONDS = 15 * 60
CLAIM = "claim.json"
KIND = "export"

_LOCK = threading.Lock()


def _root(context: ServiceContext) -> Path:
    return Path(context.config.pheasant.exports_path) / SUBDIR / context.knowledge_base()


def export_key(context: ServiceContext, request: source_export.ExportRequest) -> str:
    """What the export would contain, as a digest. Validates the request on the way."""

    selection = source_export._select(context, request)
    parts = {
        "kb": selection.kb_id,
        "source": selection.source,
        "prefix": selection.prefix,
        "format": selection.fmt,
        "chunks": bool(request.include_chunks),
        "principal": request.principal or "",
        "groups": sorted(request.principal_groups or []),
        "documents": [
            [str(row["id"]), str(row["sha256"] or ""), str(row["last_indexed_at"] or "")]
            for row in selection.rows
        ],
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def _files_present(context: ServiceContext, job: dict[str, Any]) -> bool:
    result = job.get("result") or {}
    folder = _root(context) / str(result.get("key") or "")
    files = result.get("files") or []
    return bool(files) and all((folder / str(f["filename"])).is_file() for f in files)


def _reusable(context: ServiceContext, job: dict[str, Any] | None) -> str | None:
    """``running`` or ``ready`` when ``job`` answers the request, else ``None``."""

    if not job or job.get("kind") != KIND:
        return None
    if job.get("active"):
        return "running"
    if job.get("status") == "succeeded" and _files_present(context, job):
        return "ready"
    return None


def start_export(
    context: ServiceContext, jobs: Any, request: source_export.ExportRequest
) -> dict[str, Any]:
    """Start (or rejoin) the export ``request`` names; its job, with ``reused``."""

    key = export_key(context, request)
    target = f"export:{key}"
    folder = _root(context) / key
    with _LOCK:
        for found in (
            jobs.active_snapshot_for(target),
            jobs.last_outcome_snapshot_for(target),
        ):
            reused = _reusable(context, found)
            if reused:
                return {**found, "job_id": found["id"], "reused": reused}
        folder.mkdir(parents=True, exist_ok=True)
        claimed = _claim(context, jobs, folder)
        if isinstance(claimed, dict):
            return claimed
        selection = source_export._select(context, request)
        where = f" / {selection.prefix}" if selection.prefix else ""
        job = jobs.create(KIND, f"Exporting {selection.source}{where} ({selection.fmt})", [target])
        (folder / CLAIM).write_text(json.dumps({"job_id": job.id, "at": time.time()}))
    _sweep(context, jobs, keep=key)

    from pheasant.security.authorization import background_target

    threading.Thread(
        target=background_target(lambda: _run(context, jobs, job.id, request, key, folder)),
        name=f"pheasant-export-{job.id}",
        daemon=True,
    ).start()
    return {**job.as_dict(), "job_id": job.id, "reused": None}


def _claim(context: ServiceContext, jobs: Any, folder: Path) -> dict[str, Any] | None:
    """Take this export for this process, or the job another replica is running."""

    path = folder / CLAIM
    for _ in range(2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return None
        except FileExistsError:
            try:
                held = json.loads(path.read_text() or "{}")
            except (OSError, ValueError):
                held = {}
            job = jobs.get(str(held.get("job_id") or "")) if held.get("job_id") else None
            reused = _reusable(context, job)
            if reused and job is not None:
                return {**job, "job_id": job["id"], "reused": reused}
            started = float(held.get("at") or 0)
            if (
                job is None
                and held
                and not held.get("done")
                and time.time() - started < CLAIM_STALE_SECONDS
            ):
                # Another replica's job, not yet visible here: wait it out
                # rather than write the same file twice.
                raise ExportNotReady("the same export is starting on another replica") from None
            # Finished badly, or abandoned: this request takes it over.
            path.unlink(missing_ok=True)
    return None


def _run(
    context: ServiceContext,
    jobs: Any,
    job_id: str,
    request: source_export.ExportRequest,
    key: str,
    folder: Path,
) -> None:
    from dataclasses import replace

    def progress(done: int, total: int) -> None:
        jobs.progress(job_id, phase="exporting", current=done, total=total, detail="")

    files: list[dict[str, Any]] = []
    offset = int(request.offset or 0)
    try:
        jobs.progress(job_id, phase="exporting", current=offset, detail="reading documents")
        while True:
            part = replace(request, offset=offset)
            partial = folder / f".part-{offset:06d}.partial"
            with partial.open("wb") as handle:
                described = source_export.export_source_file(context, part, handle, progress)
            final = folder / described["filename"]
            os.replace(partial, final)
            files.append(
                {
                    "part": len(files),
                    "filename": described["filename"],
                    "bytes": described["bytes"],
                    "documents": described["documents"],
                    "media_type": described["media_type"],
                }
            )
            if not described["has_more"]:
                break
            offset = int(described["next_offset"])
        result = {
            "key": key,
            "source": described["source"],
            "format": described["format"],
            "path_prefix": request.path_prefix or None,
            "documents": sum(f["documents"] for f in files),
            "total": described["total"],
            "files": files,
            "download": f"/jobs/{job_id}/download",
        }
        (folder / CLAIM).write_text(json.dumps({"job_id": job_id, "at": time.time(), "done": True}))
        jobs.finish(job_id, "succeeded", result=result)
    except Exception as exc:  # noqa: BLE001 - a background job reports, it cannot raise
        logger.warning("export %s failed", job_id, exc_info=True)
        for leftover in folder.glob(".part-*.partial"):
            leftover.unlink(missing_ok=True)
        (folder / CLAIM).unlink(missing_ok=True)
        jobs.finish(job_id, "failed", error=str(exc))


def download(
    context: ServiceContext, jobs: Any, job_id: str, part: int = 0
) -> tuple[Path, str, str]:
    """``(path, filename, media_type)`` of one finished export file."""

    job = jobs.get(job_id)
    if job is None or job.get("kind") != KIND:
        raise UnknownJob(job_id)
    if job.get("active"):
        raise ExportNotReady(f"export {job_id} is still running")
    if job.get("status") != "succeeded":
        raise ExportExpired(job_id, job.get("error") or job.get("status"))
    result = job.get("result") or {}
    files = result.get("files") or []
    if not 0 <= part < len(files):
        raise ExportExpired(job_id, f"it has {len(files)} file(s), no part {part}")
    entry = files[part]
    path = _root(context) / str(result.get("key")) / str(entry["filename"])
    if not path.is_file():
        raise ExportExpired(job_id, "its file has been cleaned up")
    return path, str(entry["filename"]), str(entry["media_type"])


def _sweep(context: ServiceContext, jobs: Any, *, keep: str) -> None:
    """Remove export folders older than :data:`KEEP_SECONDS` that nothing is writing."""

    root = _root(context)
    cutoff = time.time() - KEEP_SECONDS
    try:
        folders = [p for p in root.iterdir() if p.is_dir() and p.name != keep]
    except OSError:
        return
    for folder in folders:
        try:
            if folder.stat().st_mtime > cutoff:
                continue
            held = json.loads((folder / CLAIM).read_text() or "{}")
            job = jobs.get(str(held.get("job_id") or "")) if held.get("job_id") else None
            if job is not None and job.get("active"):
                continue
        except (OSError, ValueError):
            pass
        shutil.rmtree(folder, ignore_errors=True)
