"""Files a person attaches to a run: pinned for the run, and kept in Pheasant.

A run's inputs are registrations, and an attachment becomes one: the bytes go
into the blob store under their digests and a manifest names them, so the
attachment is an immutable revision (``attachment://<batch>``) that any
replica can read -- no shared disk, no path policy, and nothing that can move
under a citation. A document Pheasant can extract (PDF, Word, PowerPoint,
Excel, RTF, EPUB) also gets a text twin, ``<name>.txt``, marked as derived from
the original, so grep and read work on it while materialize still copies the
original bytes into a sandbox.

The same files are offered to Pheasant's ``uploads`` source through the
ordinary submission path (receipts, idempotent placement, forwarded to the
writing tier on a fleet), under ``harness/<batch>/``, and that source is
registered and synced as the drop zone does it. So an attachment is also
knowledge: searchable, browsable in the taxonomy view, removable like any
upload. Landing is best-effort and reported: a caller who may not add
documents still gets the run input, and is told the upload was skipped and why.

The batch id is a digest of the files' names and bytes, so attaching the same
files twice yields the same registration and the same upload.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath
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
EXTRACTABLE = {".pdf", ".docx", ".pptx", ".xlsx", ".doc", ".rtf", ".epub"}


def safe_name(raw: str) -> str:
    """A relative path an attachment keeps (folders allowed), or a refusal."""
    path = PurePosixPath(str(raw or "").replace("\\", "/").lstrip("/"))
    parts = [p for p in path.parts if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise HarnessError(f"attachment name {raw!r} is not a relative file path")
    return "/".join(parts)


def _extract(harness: Any, name: str, data: bytes) -> str | None:
    if PurePosixPath(name).suffix.lower() not in EXTRACTABLE:
        return None
    try:
        from pheasant.ingestion.extractor import build_extractor

        settings = harness.config.ingestion.extractor
        text = build_extractor(settings).extract(data, name)
    except Exception:  # noqa: BLE001 - an unreadable document is still attached
        return None
    return str(text) if text and str(text).strip() else None


def build_manifest(harness: Any, files: list[tuple[str, bytes]]) -> tuple[str, list[dict]]:
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
    entries: list[dict] = []
    for name in sorted(seen):
        data = seen[name]
        blob = harness.blobs.put(data, "application/octet-stream")
        entries.append(
            {
                "path": name,
                "kind": "file",
                "size": len(data),
                "digest": digest_bytes(data),
                "blob": blob,
                "binary": is_binary(data[:8192]),
                "generated": is_generated(name, data[:512]),
                "hidden": any(p.startswith(".") for p in name.split("/")),
                "ignored": False,
            }
        )
        text = _extract(harness, name, data)
        if text is not None:
            encoded = text.encode("utf-8")
            entries.append(
                {
                    "path": name + ".txt",
                    "kind": "file",
                    "size": len(encoded),
                    "digest": digest_bytes(encoded),
                    "blob": harness.blobs.put(encoded, "text/plain"),
                    "binary": False,
                    "generated": False,
                    "hidden": False,
                    "ignored": False,
                    "derived_from": name,
                    "derivation": "pheasant document extractor",
                }
            )
        elif PurePosixPath(name).suffix.lower() in EXTRACTABLE:
            entries.append(
                {
                    "path": name + ".txt",
                    "kind": "unreadable",
                    "reason": "no text could be extracted from this document",
                    "hidden": False,
                    "ignored": False,
                    "derived_from": name,
                }
            )
    batch = digest_value([[e["path"], e.get("digest")] for e in entries])[7:23]
    return batch, entries


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


def land_in_uploads(
    harness: Any, batch: str, files: list[tuple[str, bytes]], actor: str, sync: bool
) -> dict:
    """Offer the originals to Pheasant's uploads source and start its sync. Best-effort."""
    from pheasant.services import ingestion
    from pheasant.services.errors import ServiceError

    services = harness.services
    folder = f"harness/{batch}"
    items = [
        ingestion.SubmissionItem(
            relative_path=f"{folder}/{safe_name(name)}",
            content=data,
            idempotency_key=f"harness-attachment:{batch}:{safe_name(name)}",
            metadata={"harness_batch": batch},
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
    batch, entries = build_manifest(harness, files)
    names = [e["path"] for e in entries if not e.get("derived_from")]
    title = label or (names[0] if len(names) == 1 else f"{len(names)} attached files")
    registration = register(harness, actor, entries, batch, title)
    uploads = (
        land_in_uploads(harness, batch, files, actor, sync)
        if keep_in_uploads
        else {"status": "not requested"}
    )
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
            }
            for e in entries
        ],
        "uploads": uploads,
    }


__all__ = ["AttachmentResolver", "attach", "build_manifest", "safe_name", "Registration"]
