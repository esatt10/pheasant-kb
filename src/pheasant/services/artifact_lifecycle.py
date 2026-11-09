"""Archive, restore, retention and bulk deletion of kept artifacts.

The row is the artifact; its files in the ``artifacts`` source are a
projection of it. That is what makes archiving reversible without a second
copy, the way a memory record's TTL archive is (`memory/maintenance.py`
renames a record rather than deleting it): **archiving** stamps the row and
takes its files out of the index, **restoring** clears the stamp and lands the
files again from the row. Only a **delete** forgets the row.

Retention is the memory plane's TTL, for generated work: every scheduler beat
archives what is older than ``retention_days`` (of ``retention_kinds``, or of
every kind), bounded per beat so a first pass over a large region costs
several beats rather than one long one, and never under ``sync_lock``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from pheasant.services import ServiceContext
from pheasant.services import artifacts as store
from pheasant.services.errors import InvalidRequest

#: The most artifacts one bulk call, or one retention pass, touches.
MAX_BULK = 500
RETENTION_PER_BEAT = 200


@dataclass(frozen=True)
class Selection:
    """Which artifacts a bulk call means: ids, or a filter. Never nothing."""

    ids: list[str] | None = None
    owner: str | None = None
    conversation_id: str | None = None
    kind: str | None = None
    #: Saved before this instant (an ISO date or timestamp).
    before: str | None = None
    #: ``True`` archived only, ``False`` live only, ``None`` either.
    archived: bool | None = None


def select(context: ServiceContext, selection: Selection) -> list[dict[str, Any]]:
    """The artifacts a selection names, oldest first; refused if it names nothing."""

    if not (
        selection.ids
        or selection.owner
        or selection.conversation_id
        or selection.kind
        or selection.before
    ):
        raise InvalidRequest(
            "name the artifacts: ids, or at least one of owner, conversation_id, kind, before"
        )
    if selection.ids and len(selection.ids) > MAX_BULK:
        raise InvalidRequest(f"at most {MAX_BULK} artifacts per call; split the ids")
    clauses, params = ["kb_id=?"], [context.config.knowledge_base_id]
    if selection.ids:
        clauses.append(f"id IN ({','.join('?' for _ in selection.ids)})")
        params += [str(i) for i in selection.ids]
    for column, value in (
        ("owner", selection.owner),
        ("session_id", selection.conversation_id),
        ("kind", selection.kind),
    ):
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    if selection.before:
        clauses.append("created_at < ?")
        params.append(str(selection.before))
    if selection.archived is not None:
        clauses.append("archived_at IS NOT NULL" if selection.archived else "archived_at IS NULL")
    rows = context.state.rows(
        f"SELECT {store.COLUMNS} FROM saved_artifacts WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at, id LIMIT ?",
        (*params, MAX_BULK),
    )
    return [store._record(row) for row in rows]


def delete_many(
    context: ServiceContext,
    selection: Selection,
    *,
    principal: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete every artifact the selection names, or say which it would (``dry_run``)."""

    chosen = select(context, selection)
    summaries = [store.summary_of(record) for record in chosen]
    if dry_run:
        return {"dry_run": True, "matched": len(chosen), "artifacts": summaries}
    deleted = [store.delete(context, record["id"], principal)["deleted"] for record in chosen]
    return {"dry_run": False, "matched": len(chosen), "deleted": deleted}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def archive(
    context: ServiceContext, selection: Selection, *, principal: str | None = None
) -> dict[str, Any]:
    """Archive the selected live artifacts: stamped, and out of the index."""

    chosen = select(context, Selection(**{**selection.__dict__, "archived": False}))
    now = _now()
    for record in chosen:
        # Stamped first: removing its index page must not retire it
        # (`artifacts.forget_removed` keeps an archived row).
        context.state.execute(
            "UPDATE saved_artifacts SET archived_at=?, updated_at=? WHERE id=? AND kb_id=?",
            (now, now, record["id"], context.config.knowledge_base_id),
        )
        folder = (record.get("payload") or {}).get("folder")
        if folder:
            names = [name for name, _text in store.source_files(record)]
            store._remove(context, folder, names, store.owner(principal))
    return {"archived": [r["id"] for r in chosen], "archived_at": now}


def restore(
    context: ServiceContext, selection: Selection, *, principal: str | None = None
) -> dict[str, Any]:
    """Restore archived artifacts: unstamped, and landed again from their rows.

    Refused while an archived copy's removal is still waiting for the indexer:
    landing the files then would have the indexer delete the new bytes as it
    finishes removing the old ones.
    """

    chosen = select(context, Selection(**{**selection.__dict__, "archived": True}))
    restored, waiting = [], []
    for record in chosen:
        folder = (record.get("payload") or {}).get("folder") or ""
        pending = context.state.rows(
            "SELECT 1 AS x FROM archived_documents WHERE source_id=? AND relative_path LIKE ? "
            "LIMIT 1",
            (store.ARTIFACTS_SOURCE, f"{folder}/%"),
        )
        if pending:
            waiting.append(record["id"])
            continue
        context.state.execute(
            "UPDATE saved_artifacts SET archived_at=NULL, updated_at=? WHERE id=? AND kb_id=?",
            (_now(), record["id"], context.config.knowledge_base_id),
        )
        record["archived_at"] = None
        store._publish(context, record)
        restored.append(record["id"])
    result: dict[str, Any] = {"restored": restored}
    if waiting:
        result["waiting"] = waiting
        result["note"] = (
            "still being taken out of the index; restore these after the next artifacts sync"
        )
    return result


def run_retention(context: ServiceContext, *, now: datetime | None = None) -> dict[str, Any]:
    """One beat's retention: archive what is older than ``retention_days``."""

    from pheasant.services import artifact_settings

    settings = artifact_settings.current(context)
    if not settings.retention_days:
        return {"archived": 0, "enabled": False}
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=settings.retention_days)).isoformat(
        timespec="seconds"
    )
    kinds = list(settings.retention_kinds or []) or [None]
    archived: list[str] = []
    for kind in kinds:
        chosen = select(context, Selection(kind=kind, before=cutoff, archived=False))
        ids = [r["id"] for r in chosen][: RETENTION_PER_BEAT - len(archived)]
        if ids:
            archived += archive(context, Selection(ids=ids), principal="retention")["archived"]
        if len(archived) >= RETENTION_PER_BEAT:
            break
    return {"archived": len(archived), "enabled": True, "cutoff": cutoff}


def file_legacy(context: ServiceContext, records: list[dict[str, Any]]) -> None:
    """Bring artifacts kept under an older layout up to date, as a listing shows them."""

    _name_unnamed(context, records)
    _file_unfiled(context, records)


#: Artifacts filed into the source per listing, for those kept before they were knowledge.
FILE_PER_LISTING = 20


def _file_unfiled(context: ServiceContext, records: list[dict[str, Any]]) -> None:
    """File an artifact kept before the date-first directory where it belongs now.

    One with no folder (kept before artifacts were knowledge) or an
    ``<owner>/<yyyy>/<mm>`` one gets its ``<yyyy>/<mm>/<dd>/<owner>/<session>``
    folder: written to the row first, so the next listing finds it filed and
    the removal of the old copy cannot retire it (`forget_removed` reads the
    row's folder), then landed, then the old copy taken out of the index. The
    beat indexes the new one.
    """

    stale = [
        r
        for r in records
        if not r.get("archived_at")
        and not store._current_grammar((r.get("payload") or {}).get("folder"))
    ]
    for record in stale[:FILE_PER_LISTING]:
        payload = record["payload"]
        old = payload.get("folder")
        names = [name for name, _text in store.source_files(record)]
        payload["folder"] = store.folder_for(
            record.get("owner"),
            record["created_at"],
            record["title"],
            record["id"],
            record.get("conversation_id"),
        )
        store._save_payload(context, record["id"], payload)
        store._publish(context, record)
        if old:
            store._remove(context, old, names, record.get("owner"))


def _name_unnamed(context: ServiceContext, records: list[dict[str, Any]]) -> None:
    """Give a file kept before unnamed blocks were named by language its extension.

    An unnamed ```html block was kept as ``block-1``, and everything that
    decides by name -- the taxonomy view, a download -- showed its source. It
    becomes ``block-1.html``: the row is rewritten (so the next listing finds
    nothing to do), the folder landed again, and the extensionless copy taken
    out of the index. The artifact's evidence is untouched.
    """

    from pheasant.assistant.artifact_files import extension_for

    for record in records:
        payload = record.get("payload") or {}
        renamed: list[str] = []
        for file in payload.get("files") or []:
            name = str(file.get("name") or "")
            extension = extension_for(file.get("language"))
            if extension and name and not PurePosixPath(name).suffix:
                renamed.append(name)
                file["name"] = name + extension
        if not renamed:
            continue
        store._save_payload(context, record["id"], payload)
        if payload.get("folder"):  # an unfiled one lands under its new name
            store._publish(context, record)
            store._remove(context, payload["folder"], renamed, record.get("owner"))
