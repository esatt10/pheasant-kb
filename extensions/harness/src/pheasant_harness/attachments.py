"""Files a person attaches to a run: pinned for the run, and kept in Pheasant.

A run's inputs are registrations, and an attachment becomes one: the bytes go
into the blob store under their digests and a manifest names them, so the
attachment is an immutable revision (``attachment://<batch>``) that any
replica can read -- no shared disk, no path policy, and nothing that can move
under a citation.

Attachments take every input type Pheasant's own uploads take, and are read
by the same code the index reads them with (`pheasant.ingestion.pipeline`'s
``payload_text``): a document, an HTML page or a notebook is extracted, an
image is captioned, audio is transcribed, and text and code are read as they
are. Whatever is not plain text gets a *text twin* beside the original
(``<name>.txt``, ``<name>.caption.txt``, ``<name>.transcript.txt``), marked
as derived from it, so grep and read work on it while materialize still copies
the original bytes into a sandbox. A ZIP or tar archive (nested ones too) is
expanded by Pheasant's archive reader (`pheasant.sync.zip_archive`) into its
members under ``<archive name without its suffix>/``, with the archive kept
beside them; members the index would skip are counted, not silently dropped.

Every entry carries the metadata the index would give it: ``mime_type``,
``artifact_type``, whether the index reads it (``indexable``), what read it
(``text_from``), and for a member the archive it came from. The same metadata
travels with each original into Pheasant's ``uploads`` source (the ordinary
submission path: receipts, idempotent placement, forwarded to the writing tier
on a fleet) under ``harness/<batch>/``, and the Pheasant artifact each one
becomes is recorded against the batch (``harness_attachment_uploads``), so an
attachment can be followed from the run to the knowledge base and back.
Landing is best-effort and reported: a caller who may not add documents still
gets the run input, and is told the upload was skipped and why.

The batch id is a digest of the files' names and bytes, so attaching the same
files twice yields the same registration and the same upload.
"""

from __future__ import annotations

import json
import mimetypes
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from pheasant_harness.common import HarnessError, canonical, digest_bytes, digest_value, now
from pheasant_harness.registry import (
    EntryUnreadable,
    NotFound,
    Registration,
    Resolver,
    Revision,
    is_binary,
    is_generated,
)

UPLOADS_SOURCE = "uploads"
MAX_FILES = 200
MAX_FILE_BYTES = 64 * 1024 * 1024
#: Entries one batch may hold once archives are expanded, and their bytes.
MAX_ENTRIES = 2000
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
#: Kept for callers that ask which formats have an extracted twin.
EXTRACTABLE = {".pdf", ".docx", ".pptx", ".xlsx", ".doc", ".rtf", ".epub"}
#: The twin's suffix for what read the original.
_TWIN_SUFFIX = {"caption": ".caption.txt", "transcript": ".transcript.txt"}
_DERIVATION = {
    "caption": "pheasant image captioner",
    "transcript": "pheasant audio transcriber",
    "extractor": "pheasant document extractor",
    "html": "pheasant HTML text extractor",
    "notebook": "pheasant notebook reader",
}


def safe_name(raw: str) -> str:
    """A relative path an attachment keeps (folders allowed), or a refusal."""
    path = PurePosixPath(str(raw or "").replace("\\", "/").lstrip("/"))
    parts = [p for p in path.parts if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise HarnessError(f"attachment name {raw!r} is not a relative file path")
    return "/".join(parts)


class _Readers:
    """The index's readers, built once per batch from the region's settings."""

    def __init__(self, harness: Any):
        settings = harness.config.ingestion
        self.extractor = self.captioner = self.transcriber = None
        try:
            from pheasant.ingestion.extractor import build_extractor

            self.extractor = build_extractor(settings.extractor)
        except Exception:  # noqa: BLE001 - no extractor reads documents as nothing
            pass
        try:
            from pheasant.ingestion.captioner import build_captioner

            self.captioner = build_captioner(settings.captioner)
        except Exception:  # noqa: BLE001 - the pipeline falls back to the file name
            pass
        try:
            from pheasant.ingestion.transcriber import build_transcriber

            self.transcriber = build_transcriber(settings.transcriber)
        except Exception:  # noqa: BLE001 - the pipeline falls back to the file name
            pass


#: Types the platform's MIME table may not know, as the index's readers do.
_EXTRA_TYPES = {
    ".ipynb": "application/x-ipynb+json",
    ".md": "text/markdown",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".epub": "application/epub+zip",
}


def describe(name: str) -> dict[str, Any]:
    """What the index would call this file: its type, and whether it is read at all."""
    from pheasant.ingestion.content_types import artifact_type
    from pheasant.ingestion.pipeline import indexable
    from pheasant.sync.zip_archive import archive_mime, is_archive

    archive = is_archive(name)
    mime = (
        archive_mime(name)
        if archive
        else _EXTRA_TYPES.get(PurePosixPath(name).suffix.lower()) or mimetypes.guess_type(name)[0]
    )
    return {
        "mime_type": mime or "application/octet-stream",
        "artifact_type": artifact_type(Path(name), "document_folder"),
        "indexable": bool(archive or indexable(name, mime)),
        "archive": archive,
    }


def _read_text(readers: _Readers, name: str, data: bytes, mime: str | None) -> tuple[Any, str]:
    from pheasant.ingestion.pipeline import payload_text

    return payload_text(
        name,
        data,
        mime,
        captioner=readers.captioner,
        transcriber=readers.transcriber,
        extractor=readers.extractor,
    )


def _expand(name: str, data: bytes) -> tuple[list[tuple[str, bytes, dict]], dict]:
    """An archive's members as (path inside it, bytes, member metadata), and a summary."""
    from pheasant.sync import zip_archive

    suffix = "".join(PurePosixPath(name).suffixes[-2:]).lower()
    with tempfile.TemporaryDirectory(prefix="harness-archive-") as tmp:
        path = Path(tmp) / (
            "archive" + (suffix if zip_archive.is_archive("a" + suffix) else ".zip")
        )
        path.write_bytes(data)
        try:
            listing = zip_archive.scan(path)
        except zip_archive.ArchiveError as exc:
            return [], {"error": str(exc).replace(str(path), name), "members": 0}
        found: list[tuple[str, bytes, dict]] = []
        unreadable: list[dict] = []
        for virtual, info in listing.members:
            if info.error:
                unreadable.append({"path": virtual, "reason": info.error})
                continue
            try:
                content = zip_archive.read_member(path, info.filename, MAX_FILE_BYTES)
            except zip_archive.ArchiveError as exc:
                unreadable.append({"path": virtual, "reason": str(exc).replace(str(path), name)})
                continue
            found.append((virtual, content, {"member": info.filename}))
    return found, {
        "members": len(found),
        "nested_archives": listing.nested,
        "skipped": dict(listing.skipped),
        "unreadable": unreadable,
    }


def _contents_folder(name: str, taken: set[str]) -> str:
    pure = PurePosixPath(name)
    stem = pure.name
    for suffix in (
        ".tar.gz",
        ".tar.bz2",
        ".tar.xz",
        ".tgz",
        ".tbz2",
        ".tbz",
        ".txz",
        ".tar",
        ".zip",
    ):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    folder = str(pure.with_name(stem or "archive"))
    # A file of the same name would make the folder a file: say so in the name.
    while folder in taken:
        folder += "-contents"
    return folder


def build_manifest(harness: Any, files: list[tuple[str, bytes]]) -> tuple[str, list[dict]]:
    batch, entries, _ = _manifest(harness, files)
    return batch, entries


def _manifest(
    harness: Any, files: list[tuple[str, bytes]]
) -> tuple[str, list[dict], dict[str, dict]]:
    if not files:
        raise HarnessError("attach at least one file")
    if len(files) > MAX_FILES:
        raise HarnessError(f"attach at most {MAX_FILES} files at once")
    seen: dict[str, bytes] = {}
    for raw, data in files:
        name = safe_name(raw)
        if len(data) > MAX_FILE_BYTES:
            raise HarnessError(f"{name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        if name in seen and seen[name] != data:
            raise HarnessError(f"two different files are named {name}")
        seen[name] = data
    # Archives expand into their members; the archive itself stays beside them.
    pending: list[tuple[str, bytes, dict]] = []
    archives: dict[str, dict] = {}
    for name in sorted(seen):
        data = seen[name]
        pending.append((name, data, {}))
        if describe(name)["archive"]:
            members, summary = _expand(name, data)
            folder = _contents_folder(name, set(seen))
            summary["folder"] = folder
            archives[name] = summary
            for member, content, extra in members:
                pending.append((f"{folder}/{member}", content, {"archive": name, **extra}))
    if len(pending) > MAX_ENTRIES:
        raise HarnessError(f"these files expand to more than {MAX_ENTRIES} entries")
    if sum(len(d) for _, d, _ in pending) > MAX_EXPANDED_BYTES:
        raise HarnessError(
            f"these files expand past {MAX_EXPANDED_BYTES // (1024 * 1024)} MB; attach fewer"
        )
    readers = _Readers(harness)
    entries: list[dict] = []
    paths = {path for path, _, _ in pending}
    for name, data, extra in sorted(pending, key=lambda item: item[0]):
        meta = describe(name)
        entry = {
            "path": name,
            "kind": "file",
            "size": len(data),
            "digest": digest_bytes(data),
            "blob": harness.blobs.put(data, meta["mime_type"]),
            "binary": is_binary(data[:8192]),
            "generated": is_generated(name, data[:512]),
            "hidden": any(p.startswith(".") for p in name.split("/")),
            "ignored": False,
            "mime_type": meta["mime_type"],
            "artifact_type": meta["artifact_type"],
            "indexable": meta["indexable"],
            **extra,
        }
        if meta["archive"]:
            entry["text_from"] = "archive"
            entry["expanded_into"] = archives.get(name, {}).get("folder")
            entries.append(entry)
            continue
        if not meta["indexable"]:
            entry["text_from"] = None
            entries.append(entry)
            continue
        try:
            text, how = _read_text(readers, name, data, meta["mime_type"])
            text = str(text) if text is not None else ""
        except Exception as exc:  # noqa: BLE001 - an unreadable file is still attached
            text, how = "", "error:" + type(exc).__name__
        entry["text_from"] = how if not how.startswith("error:") else None
        entries.append(entry)
        if how == "text":
            continue
        twin_path = name + _TWIN_SUFFIX.get(how, ".txt")
        if twin_path in paths:
            continue  # an authored twin (``x.png.caption.txt``) wins, as a sidecar does
        if text.strip():
            encoded = text.encode("utf-8")
            entries.append(
                {
                    "path": twin_path,
                    "kind": "file",
                    "size": len(encoded),
                    "digest": digest_bytes(encoded),
                    "blob": harness.blobs.put(encoded, "text/plain"),
                    "binary": False,
                    "generated": False,
                    "hidden": False,
                    "ignored": False,
                    "derived_from": name,
                    "derivation": _DERIVATION.get(how, "pheasant reader"),
                    "mime_type": "text/plain",
                    "artifact_type": "text",
                    "indexable": False,
                    "text_from": how,
                    **({"archive": extra["archive"]} if extra.get("archive") else {}),
                }
            )
        else:
            entries.append(
                {
                    "path": twin_path,
                    "kind": "unreadable",
                    "reason": "no text could be read from this file"
                    if not how.startswith("error:")
                    else f"reading it failed ({how[6:]})",
                    "hidden": False,
                    "ignored": False,
                    "derived_from": name,
                }
            )
    entries.sort(key=lambda e: e["path"])
    batch = digest_value([[e["path"], e.get("digest")] for e in entries])[7:23]
    return batch, entries, archives


class AttachmentResolver(Resolver):
    """An attachment batch: an immutable manifest of blobs."""

    name = "attachment"
    operations = ("resolve", "enumerate", "grep", "read", "inspect", "materialize", "compare")

    def _entries(self) -> list[dict]:
        manifest = self.registration.scope.get("manifest")
        if not manifest or not self.harness.blobs.exists(manifest):
            raise NotFound(f"attachment {self.registration.uri} has no stored manifest")
        return json.loads(self.harness.blobs.get_text(manifest))

    def resolve(self, selector: str | None) -> Revision:
        entries = self._entries()
        manifest = self.registration.scope["manifest"]
        return Revision(
            self.registration.id,
            manifest,
            "attachment",
            manifest,
            entries,
            self.registration.created_at,
            now(),
            time_bound=False,
        )

    def read_bytes(self, revision: Revision, path: str) -> bytes:
        entry = revision.entry(path)
        if entry is None:
            raise NotFound(f"{path} is not in this attachment")
        if entry["kind"] != "file":
            raise EntryUnreadable(f"{path} is unreadable: {entry.get('reason')}")
        data = self.harness.blobs.get(entry["blob"])
        if digest_bytes(data) != entry["digest"]:
            raise EntryUnreadable(f"{path}: stored bytes do not match their digest")
        return data


def register(harness: Any, actor: str, entries: list[dict], batch: str, label: str) -> dict:
    """The registration for a batch (created once; granted to the actor)."""
    registry = harness.registry
    manifest = harness.blobs.put_text(canonical(entries), "application/json")
    uri = f"attachment://{batch}"
    reg_id = "reg_" + digest_value([harness.kb_id, uri])[7:23]
    rows = harness.state.rows(
        "SELECT id FROM harness_registrations WHERE kb_id=? AND id=?", (harness.kb_id, reg_id)
    )
    if not rows:
        harness.state.execute(
            "INSERT INTO harness_registrations(kb_id,id,uri,resolver,scope_json,freshness_json,"
            "operations_json,classification,label,registered_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                harness.kb_id,
                reg_id,
                uri,
                "attachment",
                canonical({"batch": batch, "manifest": manifest}),
                canonical({"snapshot": True}),
                canonical(list(AttachmentResolver.operations)),
                "internal",
                label,
                actor,
                now(),
            ),
        )
    registry.grant(reg_id, actor, actor)
    return registry.get(reg_id).public() | {"created": not rows}


def submission_metadata(
    entry: dict, *, batch: str, registration_id: str | None, actor: str, label: str
) -> dict[str, Any]:
    """What an original carries into its uploads receipt: the index's view of it, and ours."""
    return {
        "harness_batch": batch,
        "harness_registration": registration_id,
        "harness_attachment": f"attachment://{batch}/{entry['path']}",
        "attachment_label": label,
        "uploaded_by": actor,
        "original_name": entry["path"],
        "mime_type": entry.get("mime_type"),
        "artifact_type": entry.get("artifact_type"),
        "indexable": entry.get("indexable"),
        "text_from": entry.get("text_from"),
        "size_bytes": entry.get("size"),
        "sha256": str(entry.get("digest") or "").removeprefix("sha256:"),
        **({"expanded_into": entry["expanded_into"]} if entry.get("expanded_into") else {}),
    }


def land_in_uploads(
    harness: Any,
    batch: str,
    files: list[tuple[str, bytes]],
    actor: str,
    sync: bool,
    *,
    entries: list[dict] | None = None,
    registration_id: str | None = None,
    label: str = "",
) -> dict:
    """Offer the originals to Pheasant's uploads source and start its sync. Best-effort.

    Only originals land: an archive is landed whole, because the uploads sync
    expands it with the same reader, and a text twin is the harness's own copy
    of what the index will read anyway.
    """
    from pheasant.services import ingestion
    from pheasant.services.errors import ServiceError

    services = harness.services
    folder = f"harness/{batch}"
    by_path = {e["path"]: e for e in entries or []}
    items = [
        ingestion.SubmissionItem(
            relative_path=f"{folder}/{safe_name(name)}",
            content=data,
            idempotency_key=f"harness-attachment:{batch}:{safe_name(name)}",
            metadata=submission_metadata(
                by_path.get(safe_name(name)) or {"path": safe_name(name)},
                batch=batch,
                registration_id=registration_id,
                actor=actor,
                label=label,
            ),
        )
        for name, data in files
    ]
    try:
        landed = ingestion.submit(
            services,
            ingestion.SubmissionRequest(
                items=items,
                source_name=UPLOADS_SOURCE,
                submission_id=f"harness-{batch}",
                agent_id=actor,
            ),
        )
    except (ServiceError, OSError, PermissionError) as exc:
        return {
            "source": UPLOADS_SOURCE,
            "folder": folder,
            "landed": 0,
            "status": "skipped",
            "reason": str(exc) or type(exc).__name__,
        }
    _ensure_uploads_source(services, str(landed["directory"]))
    record_uploads(harness, batch, landed, folder)
    status, job = "landed", None
    if sync and landed["accepted"]:
        indexing = getattr(services, "indexing", None)
        try:
            if indexing is not None:
                started = indexing.start_sync(UPLOADS_SOURCE)
                job = (started or {}).get("job_id") if isinstance(started, dict) else None
                status = "syncing"
        except Exception as exc:  # noqa: BLE001 - the files are landed; the sync is a schedule
            status = "landed"
            job = None
            return {
                "source": UPLOADS_SOURCE,
                "folder": folder,
                "landed": len(landed["accepted"]),
                "rejected": landed["rejected"],
                "status": status,
                "reason": f"sync not started: {exc}",
            }
    return {
        "source": UPLOADS_SOURCE,
        "folder": folder,
        "directory": str(landed["directory"]),
        "landed": len(landed["accepted"]),
        "rejected": landed["rejected"],
        "submission_id": landed["submission_id"],
        "status": status,
        "job_id": job,
    }


def record_uploads(harness: Any, batch: str, landed: dict, folder: str) -> None:
    """Which Pheasant document each attached original became (one row per file)."""
    prefix = folder + "/"
    for receipt in landed.get("accepted") or []:
        relative = str(
            receipt.get("relative_path") or receipt.get("detail", {}).get("relative_path") or ""
        )
        path = relative[len(prefix) :] if relative.startswith(prefix) else relative
        harness.state.execute(
            "INSERT INTO harness_attachment_uploads(kb_id,batch,path,source_name,relative_path,"
            "artifact_id,submission_id,idempotency_key,landed_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (kb_id,batch,path) DO UPDATE SET artifact_id=excluded.artifact_id, "
            "relative_path=excluded.relative_path, submission_id=excluded.submission_id",
            (
                harness.kb_id,
                batch,
                path,
                UPLOADS_SOURCE,
                relative,
                receipt.get("artifact_id"),
                landed.get("submission_id"),
                receipt.get("idempotency_key"),
                now(),
            ),
        )


def uploads_for(harness: Any, batch: str) -> list[dict]:
    rows = harness.state.rows(
        "SELECT path, source_name, relative_path, artifact_id, submission_id, landed_at "
        "FROM harness_attachment_uploads WHERE kb_id=? AND batch=? ORDER BY path",
        (harness.kb_id, batch),
    )
    return [dict(row) for row in rows]


def _ensure_uploads_source(services: Any, directory: str) -> None:
    """Register ``uploads`` as the drop zone registers it, if nothing has."""
    config, state = services.config, services.state
    known = any(s.name == UPLOADS_SOURCE for s in config.sources) or bool(
        state.get_source(UPLOADS_SOURCE)
    )
    if known and not state.source_removed(UPLOADS_SOURCE):
        return
    from pheasant.config.schema import PheasantConfig
    from pheasant.registry.source_registry import SourceRegistry

    source = PheasantConfig.model_validate(
        {
            "sources": [
                {
                    "name": UPLOADS_SOURCE,
                    "type": "document_folder",
                    "path": directory,
                    "description": f"Documents uploaded through the UI ({UPLOADS_SOURCE})",
                    "include": ["**/*"],
                }
            ]
        }
    ).sources[0]
    SourceRegistry(config, state).register_source(source)
    config.sources = [s for s in config.sources if s.name != UPLOADS_SOURCE] + [source]


def attach(
    harness: Any,
    files: list[tuple[str, bytes]],
    actor: str,
    *,
    label: str | None = None,
    sync: bool = True,
    keep_in_uploads: bool = True,
) -> dict:
    batch, entries, archives = _manifest(harness, files)
    originals = sorted({safe_name(name) for name, _ in files})
    title = label or (originals[0] if len(originals) == 1 else f"{len(originals)} attached files")
    registration = register(harness, actor, entries, batch, title)
    uploads = (
        land_in_uploads(
            harness,
            batch,
            files,
            actor,
            sync,
            entries=entries,
            registration_id=registration["id"],
            label=title,
        )
        if keep_in_uploads
        else {"status": "not requested"}
    )
    documents = {row["path"]: row for row in uploads_for(harness, batch)}
    return {
        "registration": registration,
        "batch": batch,
        "files": [
            {
                "path": e["path"],
                "size": e.get("size"),
                "kind": e["kind"],
                "binary": e.get("binary", False),
                "derived_from": e.get("derived_from"),
                "reason": e.get("reason"),
                "mime_type": e.get("mime_type"),
                "artifact_type": e.get("artifact_type"),
                "indexable": e.get("indexable"),
                "text_from": e.get("text_from"),
                "archive": e.get("archive"),
                "pheasant_document": (documents.get(e["path"]) or {}).get("artifact_id"),
            }
            for e in entries
        ],
        "archives": archives,
        "uploads": uploads,
    }


__all__ = ["AttachmentResolver", "attach", "build_manifest", "safe_name", "Registration"]
