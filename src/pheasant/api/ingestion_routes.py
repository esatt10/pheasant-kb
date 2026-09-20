"""Manual ingestion's HTTP routes: the drop zone, and the tier that writes it.

Split out of `api/app.py` rather than added to it, for the reason
`api/readiness_routes.py` already was: that file is the module ratchet's
headline and its own ceiling comment names the fix — one router per plane.
This is the ingestion plane, and it is a plane rather than two routes because
the pair below are the two halves of a single mechanism:

* ``POST /sources/upload`` is the surface a browser or an agent reaches. It
  does not assume it can write `/state`, because in a role-split fleet it
  cannot: every serving role mounts it read-only so the indexer stays the sole
  writer of committed state. It writes through whichever `LandingZone` this
  process was given.
* ``POST /internal/ingestion/land`` is the far side of that zone when it is
  remote — the endpoint the indexer serves and an api replica posts to. It
  performs the *identical* local write, so there is still one implementation
  of "where does a submitted file go" and still no second ingestion path.

`register_ingestion_routes` is handed its collaborators rather than reaching
for them, exactly as `register_readiness_routes` is: a registration function
that takes what it needs can be driven by `create_app`, by a test, and by any
future composition without any of them knowing about the others.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile

from pheasant.ingestion.landing_service import LANDING_PATH


def register_ingestion_routes(
    app: FastAPI,
    *,
    config: Any,
    state: Any,
    landing: Any,
    audit: Callable[..., None],
    index: Callable[..., dict],
    start_background_sync: Callable[[str | None, str], tuple[str | None, list[str]]],
    source_from_payload: Callable[[dict], Any],
) -> None:
    """Register the drop zone and the internal landing endpoint on ``app``."""

    def _authorize_landing(authorization: str | None) -> None:
        """Its own boundary, and deliberately not one of the existing two.

        Holding this token means being able to put bytes into the corpus. That
        is not the API token (which is the region's front door, and `/internal`
        is structurally exempt from it so a worker is never handed it) and it
        is not the graph token (which reads the graph and writes nothing). The
        fleet gives each boundary its own secret for the reason the shipped
        Compose file once demonstrated by not doing it.
        """

        expected = os.environ.get(config.ingestion.landing_service_token_env or "", "")
        if not expected:
            raise HTTPException(
                status_code=503,
                detail=(
                    "The landing service is not configured on this process: "
                    f"{config.ingestion.landing_service_token_env!r} is unset."
                ),
            )
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="invalid landing service token")

    @app.api_route(LANDING_PATH, methods=["GET", "POST"])
    async def internal_land(
        request: Request,
        source_name: str,
        relative_path: str | None = None,
        unique: bool = False,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Write one submitted document where committed state is writable.

        This is the far side of `ingestion/landing_service.py`, and the whole
        of what crosses the boundary: a serving replica cannot write `/state`,
        so it forwards the bytes and this performs the identical local write.
        `LocalLandingZone` is the same object a standalone container uses, so
        there is one implementation of the write rather than a second ingestion
        path — which is the property the whole module exists to protect.

        ``GET`` answers where a source *would* land without writing anything,
        because a caller registering the source needs this process's absolute
        path rather than a guess built from its own mounts.
        """

        import anyio.to_thread

        from pheasant.ingestion.landing import safe_filename
        from pheasant.ingestion.landing_service import landing_zone_for_config
        from pheasant.services.errors import LandingZoneUnwritable

        _authorize_landing(authorization)
        # force_local, or a process pointed at its own Service forwards in a
        # circle. The same guard the graph service has, for the same reason.
        zone = landing_zone_for_config(config, force_local=True)
        name = safe_filename(source_name or "uploads", fallback="uploads")

        def _resolve() -> dict:
            try:
                return {"directory": zone.directory(name)}
            except OSError as exc:
                raise LandingZoneUnwritable(name, exc) from exc

        if request.method == "GET":
            try:
                return await anyio.to_thread.run_sync(_resolve)
            except LandingZoneUnwritable as exc:
                raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

        if not relative_path:
            raise HTTPException(status_code=422, detail="relative_path is required")
        # Read on the event loop (genuine async I/O), write on a worker thread
        # (blocking), exactly as `/sources/upload` splits the same work.
        data = await request.body()
        limits = config.sync.limits
        max_bytes = (limits.max_file_size_mb or 0) * 1024 * 1024 or None

        def _write() -> dict:
            placement = zone.write(name, relative_path, data, unique=unique, max_bytes=max_bytes)
            return {
                "directory": placement.directory,
                "path": placement.stored.path,
                "filename": placement.stored.filename,
                "size_bytes": placement.stored.size_bytes,
            }

        try:
            return await anyio.to_thread.run_sync(_write)
        except ValueError as exc:
            # Oversized or empty: the submission's problem, and permanent.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            refusal = LandingZoneUnwritable(name, exc)
            raise HTTPException(status_code=refusal.status, detail=str(refusal)) from exc

    @app.post("/sources/upload")
    async def upload_documents(
        files: Annotated[list[UploadFile], File()],
        source_name: Annotated[str, Form()] = "uploads",
        sync_now: Annotated[bool, Form()] = True,
        wait: Annotated[bool, Form()] = False,
    ) -> dict:
        """Index documents dropped into the UI, with no filesystem setup.

        The files land in a directory under ``/state/uploads`` which is
        registered as an ordinary ``document_folder`` source — so they flow
        through the same connector → chunk → graph pipeline as everything
        else, get the same idempotent re-sync, and can be removed by deleting
        the source. There is deliberately no second ingestion path.

        Uploading again into the same source name adds to it rather than
        replacing it, which is what "drop a few more files in" should mean.
        """
        import anyio.to_thread

        from pheasant.api.uploads import safe_filename
        from pheasant.ingestion.landing_service import LandingServiceError
        from pheasant.registry.source_registry import SourceRegistry
        from pheasant.services.errors import LandingZoneUnwritable

        if not files:
            raise HTTPException(status_code=400, detail="No files uploaded")
        name = safe_filename(source_name or "uploads", fallback="uploads")
        # Whoever can write. On a standalone container that is this process,
        # writing exactly where it always did; on a fleet api replica, whose
        # `/state` is read-only because the indexer is the sole writer of
        # committed state, it is the indexer over an internal endpoint. The
        # directory comes back from that process rather than being predicted
        # here, because the source registered below has to point at the path
        # the *indexer* will read during the sync.
        try:
            directory = Path(landing.directory(name))
        except OSError as exc:
            refusal = LandingZoneUnwritable(
                Path(config.pheasant.state_path) / "uploads" / name, exc
            )
            raise HTTPException(status_code=refusal.status, detail=str(refusal)) from exc
        except LandingServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        limits = config.sync.limits
        max_bytes = (limits.max_file_size_mb or 0) * 1024 * 1024 or None

        # Reading each upload's body is genuine async I/O and stays on the
        # event loop. Everything after it — the disk write, the source
        # registry, the audit log, and (wait=True) the whole sync pipeline —
        # is blocking, synchronous work, so it moves onto a worker thread
        # below. Left on the loop, one slow upload stalls every other
        # request this process is serving: measured, a single wait=true
        # upload delayed a *concurrently issued* GET /ready by the same ~5s
        # the upload itself took.
        pairs: list[tuple[str | None, bytes]] = [
            (upload.filename, await upload.read()) for upload in files
        ]

        def _finish_upload() -> dict:
            stored: list[dict] = []
            rejected: list[dict] = []
            for filename, data in pairs:
                try:
                    placement = landing.write(
                        name,
                        filename or "upload",
                        data,
                        unique=True,
                        max_bytes=max_bytes,
                    )
                except ValueError as exc:
                    # One bad file must not lose the good ones in the same drop.
                    # A remote zone raises this for the same two reasons a local
                    # one does, before the bytes leave this process.
                    rejected.append({"filename": filename, "error": str(exc)})
                    continue
                except OSError as exc:
                    # Not this file's fault, and not survivable per-file: the
                    # filesystem is the same for every item in the drop, so
                    # rejecting them one at a time would report a mount problem
                    # as forty bad files. `mkdir(exist_ok=True)` does not raise
                    # on a read-only mount when the directory already exists —
                    # EEXIST wins over EROFS — so this is genuinely reachable
                    # even when the check above passed.
                    refusal = LandingZoneUnwritable(directory, exc)
                    raise HTTPException(status_code=refusal.status, detail=str(refusal)) from exc
                except LandingServiceError as exc:
                    # The tier that writes is unreachable or refused. 502: this
                    # region is the caller's proxy for it, and the caller did
                    # nothing wrong.
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
                stored.append(placement.stored.__dict__)
            if not stored:
                raise HTTPException(
                    status_code=400,
                    detail="; ".join(item["error"] for item in rejected) or "Nothing stored",
                )

            registry = SourceRegistry(config, state)
            existing = next((s for s in config.sources if s.name == name), None)
            if existing is None:
                source = source_from_payload(
                    {
                        "name": name,
                        "type": "document_folder",
                        "path": str(directory),
                        "description": f"Documents uploaded through the UI ({name})",
                        # Uploads are arbitrary documents, not a code tree: the
                        # default include list is code-shaped and would silently
                        # drop a dropped PDF or .docx.
                        "include": ["**/*"],
                    }
                )
                registry.register_source(source)
                config.sources = [s for s in config.sources if s.name != name]
                config.sources.append(source)
            audit(name, "upload_documents", {"files": [item["filename"] for item in stored]})

            syncing = False
            job_id = None
            queued: list[str] = []
            sync_result = None
            if sync_now:
                if wait:
                    try:
                        sync_result = index(name, "incremental")["results"][0]
                    except (KeyError, ValueError) as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
                else:
                    job_id, queued = start_background_sync(name, "incremental")
                    syncing = job_id is not None or bool(queued)
            return {
                "status": "stored",
                "source_name": name,
                "path": str(directory),
                "stored": stored,
                "rejected": rejected,
                "syncing": syncing,
                "job_id": job_id,
                "queued_tasks": queued,
                "sync_result": sync_result,
            }

        return await anyio.to_thread.run_sync(_finish_upload)
