"""The drop zone's background half: received files land, register and sync as a job.

``POST /sources/upload`` with ``background=true`` (what the UI sends) answers
as soon as the request's files are received. They are copied aside -- the
request's spooled files close when it returns -- and an ``upload`` job lands
them, registers the source and starts its sync, through the same per-file loop
the synchronous path runs (``_finish_upload`` in ``ingestion_routes``). So
the person carries on, the jobs tray shows "Uploading 3 files to uploads" with
a counter, and a second drop of the same files while it runs returns the same
job rather than a second copy of the work.

HTTP-only on purpose, like the multipart route it belongs to: an agent submits
documents with MCP ``submit_documents``, which has its own receipts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

KIND = "upload"
CHUNK = 1024 * 1024
_LOCK = threading.Lock()


def start_upload_job(
    jobs: Any,
    source_name: str,
    files: list[Any],
    max_bytes: int | None,
    finish_upload: Callable[..., dict],
) -> dict[str, Any]:
    """Copy the received files aside and land them in a background job."""

    scratch = Path(tempfile.mkdtemp(prefix="pheasant-upload-"))
    copies: list[tuple[str | None, int | None, Path, str]] = []
    try:
        for index, upload in enumerate(files):
            target = scratch / f"{index:04d}"
            digest = hashlib.sha256()
            written = 0
            with target.open("wb") as handle:
                while True:
                    # One byte past the limit is enough to refuse it; the rest
                    # of an oversized file is not worth copying.
                    if max_bytes is not None and written > max_bytes:
                        break
                    chunk = upload.file.read(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
            copies.append((upload.filename, upload.size, target, digest.hexdigest()))
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise

    key = hashlib.sha256(
        json.dumps(
            [source_name, sorted((str(name or ""), sha) for name, _, _, sha in copies)]
        ).encode("utf-8")
    ).hexdigest()[:24]
    target = f"upload:{source_name}:{key}"
    names = [str(name or "upload") for name, _, _, _ in copies]
    with _LOCK:
        running = jobs.active_snapshot_for(target)
        if running is not None:
            shutil.rmtree(scratch, ignore_errors=True)
            return {**_accepted(running, source_name, names), "reused": "running"}
        count = len(copies)
        job = jobs.create(
            KIND,
            f"Uploading {count} file{'' if count == 1 else 's'} to {source_name}",
            [target],
        )

    def read_from(path: Path) -> Callable[[int], bytes]:
        def read(limit: int) -> bytes:
            with path.open("rb") as handle:
                return handle.read(limit)

        return read

    items = [(name, size, read_from(path)) for name, size, path, _ in copies]

    def progress(done: int, total: int, filename: str) -> None:
        jobs.progress(job.id, phase="landing", current=done, total=total, detail=filename)

    def run() -> None:
        try:
            result = finish_upload(items, progress)
            jobs.progress(job.id, phase="landed", current=len(items), total=len(items), detail="")
            jobs.finish(job.id, "succeeded", result=result)
        except Exception as exc:  # noqa: BLE001 - a background job reports, it cannot raise
            detail = getattr(exc, "detail", None) or str(exc)
            logger.warning("upload job %s failed: %s", job.id, detail)
            jobs.finish(job.id, "failed", error=str(detail))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    from pheasant.security.authorization import background_target

    threading.Thread(
        target=background_target(run), name=f"pheasant-upload-{job.id}", daemon=True
    ).start()
    return {**_accepted(job.as_dict(), source_name, names), "reused": None}


def _accepted(job: dict[str, Any], source_name: str, names: list[str]) -> dict[str, Any]:
    return {
        **job,
        "status": "accepted",
        "job_id": job["id"],
        "source_name": source_name,
        "files": names,
        "syncing": False,
    }
