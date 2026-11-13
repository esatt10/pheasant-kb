"""Artifacts the region keeps: composed files and saved reports, as knowledge.

Saved files, reports and conversation threads outlive the browser session.
Saving is explicit unless ``auto_save`` is enabled. Kept means knowledge:

* **A source.** Each artifact lands, through the ordinary submission path
  (`services/ingestion.py`: receipts, idempotent placement, forwarded to the
  writing tier on a fleet), in the ``artifacts`` source -- a landing-zone
  ``document_folder`` like the drop zone's -- and the normal pipeline indexes
  it. Its files are searchable from the overall index, browsable in the
  taxonomy view, and removable like any other document (`services/archive.py`;
  an upload is removed outright, its bytes deleted by the indexer).
* **A directory by date, person, then session.**
  ``<yyyy>/<mm>/<dd>/<owner>/<session>/<title>--<id>/``: a day keeps any one
  folder small however many people work in the region, a conversation's
  artifacts stay together, and the id makes every artifact's folder its own.
  Listings read the same way: newest day first, then person.
* **Indexed asynchronously on a fleet.** The row and durable projection intent
  commit together. The writer lands and indexes the files; the row is readable
  immediately. ``index_on_save`` and the scheduled beat control searchability.
* **Traceable.** Each kept answer carries its trace (`services/artifact_trace.py`):
  conversation, channel, ids, keywords, intent, the judgements behind it and
  the steps it took.
* **Its evidence as edges.** Every artifact holds a ``README.md`` whose Sources
  list links each cited passage's document by its path. The Markdown pass turns
  each link into a ``references`` edge and cross-source resolution lands it on
  the cited document -- deterministically, at indexing time (rule 1) -- so a
  graph walk goes from the artifact to what it was built from. The citations a
  compose made are those relationships.

The rows (``saved_artifacts``) keep what the viewers render, so an artifact is
there the moment it is written rather than once an indexer reaches it; the
source is what search, the graph and the taxonomy read. Visible to everyone
the region serves, as the rest of its knowledge is; the owner is the first
level of the directory and a field, not an access rule. User data (rule 2).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from pheasant.assistant.artifact_files import artifact_payload
from pheasant.config.schema import ARTIFACT_KINDS
from pheasant.security.identity import effective_principal
from pheasant.services import ServiceContext
from pheasant.services import artifact_render as render
from pheasant.services.artifact_dispatch import (
    _defer_removal,
    _deferred,
    _enqueue,
    _pacing,
    _wake_projection,
)
from pheasant.services.artifact_files import INDEX_FILE, kept_visual, source_files
from pheasant.services.artifact_inventory import record as _record
from pheasant.services.artifact_scope import (  # noqa: F401 - the module's public names
    ARTIFACT_MODES,
    ARTIFACTS_SOURCE,
    artifacts_mode,
    scope,
)
from pheasant.services.errors import ArtifactNotFound, InvalidRequest, ServiceError

logger = logging.getLogger(__name__)

KINDS = ARTIFACT_KINDS
#: What one saved artifact may hold. A composed module of forty files is a few
#: hundred kilobytes; this refuses a payload that is not an artifact at all.
MAX_BYTES = 2_000_000
EXPORT_FORMATS = ("html", "markdown", "zip")
#: How long saving a report waits for the diagram its answer is still drawing.
VISUAL_WAIT_SECONDS = 60
_FOLDER_ID = re.compile(r"--([0-9a-f]{24})$")


@dataclass(frozen=True)
class SaveRequest:
    """Keep ``title`` as an artifact, from a kept answer or from given content.

    With ``answer_id``, the answer the stream kept (``answer_resume``) is saved
    as a report; otherwise ``markdown`` and/or ``files`` are the content.
    """

    title: str | None = None
    #: ``None`` reads it off the content: a composed answer is ``files``.
    kind: str | None = None
    answer_id: str | None = None
    markdown: str | None = None
    files: list[dict[str, Any]] | None = None
    summary: str | None = None
    principal: str | None = None
    knowledge_base: str | None = None
    #: The conversation it belongs to, and the surface it was saved from.
    conversation_id: str | None = None
    channel: str | None = None
    #: ``kind: thread``: the conversation's turns, ``[{question, answer}]``.
    turns: list[dict[str, Any]] | None = None
    #: A diagram drawn after the answer (``create_visual``, "Redraw as") to keep
    #: in its place, and the passages it cites when they are not the answer's.
    visual: dict[str, Any] | None = None
    citations: list[dict[str, Any]] | None = None
    published: bool = True
    replaces: str | None = None
    scope: str = "org"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def owner(principal: str | None = None) -> str | None:
    return effective_principal(principal) or None


def _title(question: str, files: list[dict[str, Any]]) -> str:
    words = " ".join(w for w in str(question or "").split() if not w.startswith("@"))
    if words:
        return words[:90] + ("…" if len(words) > 90 else "")
    return files[0]["name"] if files else "Untitled artifact"


def folder_for(
    who: str | None,
    created_at: str,
    title: str,
    artifact_id: str,
    conversation_id: str | None = None,
) -> str:
    """``<yyyy>/<mm>/<dd>/<owner>/<session>/<title>--<id>``: date, person, session."""

    from pheasant.ingestion.landing import safe_filename

    person = safe_filename(who or "shared", fallback="shared")
    session = safe_filename(str(conversation_id or ""), fallback="no-session")[:40]
    day = f"{created_at[:4]}/{created_at[5:7]}/{created_at[8:10]}"
    return f"{day}/{person}/{session}/{render.slug(title)[:48]}--{artifact_id}"


def _current_grammar(folder: str | None) -> bool:
    return bool(folder) and bool(re.match(r"\d{4}/\d{2}/\d{2}/", str(folder)))


def summary_of(record: dict[str, Any]) -> dict[str, Any]:
    from pheasant.services.artifact_inventory import summary_of as summarize

    return summarize(record)


def _store(
    context: ServiceContext,
    kind: str,
    title: str,
    payload: dict[str, Any],
    who: str | None,
    *,
    conversation_id: str | None = None,
    channel: str | None = None,
    published: bool = True,
    replaces: str | None = None,
    visibility_scope: str = "org",
) -> dict[str, Any]:
    from pheasant.services import artifact_settings

    if not artifact_settings.current(context).enabled:
        raise InvalidRequest(
            "saving artifacts is turned off in this region (artifacts.enabled); "
            "turn it on from the Artifacts panel or configure_artifacts"
        )
    if visibility_scope not in {"org", "user", "session"}:
        raise InvalidRequest("artifact scope must be org, user or session")
    if visibility_scope != "org" and (not who or not context.config.security.acl_enforced):
        raise InvalidRequest("private saved artifacts require a writer and security.acl_enforced")
    if replaces:
        if not published:
            raise InvalidRequest("a draft cannot replace a published artifact")
        previous = _load(context, replaces, who)
        if previous.get("semantic_type"):
            raise InvalidRequest(
                "replace saved artifacts here; correct typed records with supersedes"
            )
        if previous["payload"].get("scope", "org") != visibility_scope:
            raise InvalidRequest("a replacement must retain its artifact's visibility scope")
    payload = {**payload, "scope": visibility_scope}
    from pheasant.services.artifact_evidence import capture, normalize

    payload = normalize(capture(context, payload, who))
    artifact_id = secrets.token_hex(12)
    created = _now()
    payload = {
        **payload,
        "folder": folder_for(who, created, title, artifact_id, conversation_id),
    }
    encoded = json.dumps(payload, default=str)
    if len(encoded.encode("utf-8")) > MAX_BYTES:
        raise InvalidRequest(f"an artifact holds at most {MAX_BYTES // 1_000_000} MB")
    record = {
        "id": artifact_id,
        "kind": kind,
        "title": title,
        "created_at": created,
        "owner": who,
        "payload": payload,
        "conversation_id": conversation_id,
        "channel": channel,
        "archived_at": None,
        "index_status": "pending",
    }
    deferred = _deferred(context)
    with context.state.conn:
        context.state.conn.execute(
            "INSERT INTO saved_artifacts(id, kb_id, owner, kind, title, payload_json, created_at, "
            "updated_at, session_id, channel, publication_state) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                artifact_id,
                context.config.knowledge_base_id,
                who,
                kind,
                title,
                encoded,
                created,
                created,
                conversation_id,
                channel,
                "published" if published else "draft",
            ),
        )
        if deferred:
            _enqueue(context, record, "publish")
        if published:
            from pheasant.artifacts.publications import publish

            publish(
                context.state,
                context.config.knowledge_base_id,
                artifact_id,
                record,
                receipt=payload.get("trace"),
                replaces=replaces,
                transaction=False,
            )
        else:
            from pheasant.artifacts.publications import keep_version

            keep_version(context.state, context.config.knowledge_base_id, artifact_id, record)
    if deferred:
        _wake_projection(context)
    else:
        _publish(context, record)
    if replaces:
        from pheasant.services.artifact_lifecycle import Selection, archive

        archive(context, Selection(ids=[replaces]), principal=who)
    return record


def _publish(context: ServiceContext, record: dict[str, Any]) -> None:
    """Land the artifact in its source, and start its sync when ``index_on_save`` says so.

    Best-effort past the row: a region that cannot land it (a read-only
    replica with no landing service) still has the artifact, just not yet as
    knowledge, and says so in the log.
    """

    if _deferred(context):
        with context.state.conn:
            _enqueue(context, record, "publish")
        _wake_projection(context)
        return

    from pheasant.services import ingestion

    folder = record["payload"]["folder"]
    items = [
        ingestion.SubmissionItem(
            relative_path=f"{folder}/{name}",
            content=text.encode("utf-8"),
            idempotency_key=f"artifact:{record['id']}:{name}",
        )
        for name, text in source_files(record)
    ]
    try:
        landed = ingestion.submit(
            context,
            ingestion.SubmissionRequest(
                items=items,
                source_name=ARTIFACTS_SOURCE,
                submission_id=f"artifact-{record['id']}",
                agent_id=record.get("owner"),
            ),
        )
        _ensure_source(context, str(landed["directory"]))
        from pheasant.services import artifact_settings

        if artifact_settings.current(context).index_on_save and context.indexing is not None:
            context.indexing.start_sync(ARTIFACTS_SOURCE)
    except (ServiceError, OSError):
        logger.warning("could not land artifact %s in its source", record["id"], exc_info=True)


def _ensure_source(context: ServiceContext, directory: str) -> None:
    """Register ``artifacts`` as a landing-zone ``document_folder``, once."""

    config = context.config
    known = any(s.name == ARTIFACTS_SOURCE for s in config.sources) or bool(
        context.state.get_source(ARTIFACTS_SOURCE)
    )
    # Registered here or by another replica (a runtime source lives in the
    # state store), and not removed since: nothing to do.
    if known and not context.state.source_removed(ARTIFACTS_SOURCE):
        apply_pacing(context)
        return
    from pheasant.config.schema import PheasantConfig
    from pheasant.registry.source_registry import SourceRegistry
    from pheasant.services import artifact_settings

    settings = artifact_settings.current(context)
    source = PheasantConfig.model_validate(
        {
            "sources": [
                {
                    "name": ARTIFACTS_SOURCE,
                    "type": "document_folder",
                    "path": directory,
                    "description": "Artifacts the region kept: composed files and saved reports",
                    # Every file an artifact holds: the default include list is
                    # code-shaped and would drop a report's HTML.
                    "include": ["**/*"],
                    "taxonomy": {"enabled": settings.taxonomy_enabled},
                    "sync": _pacing(settings),
                }
            ]
        }
    ).sources[0]
    SourceRegistry(config, context.state).register_source(source)
    config.sources = [s for s in config.sources if s.name != ARTIFACTS_SOURCE] + [source]


def apply_pacing(context: ServiceContext) -> None:
    """Hold the ``artifacts`` source to the settings' schedule, if it is registered."""

    from pheasant.config.schema import PheasantConfig
    from pheasant.registry.source_registry import SourceRegistry
    from pheasant.services import artifact_settings

    config = context.config
    source = next((s for s in config.sources if s.name == ARTIFACTS_SOURCE), None)
    if source is None:
        row = context.state.get_source(ARTIFACTS_SOURCE)
        if not row or not row.get("config_json"):
            return
        source = PheasantConfig.model_validate(
            {"sources": [json.loads(row["config_json"])]}
        ).sources[0]
    wanted = _pacing(artifact_settings.current(context))
    if all(getattr(source.sync, key, None) == value for key, value in wanted.items()):
        return
    for key, value in wanted.items():
        setattr(source.sync, key, value)
    SourceRegistry(config, context.state).register_source(source)
    config.sources = [s for s in config.sources if s.name != ARTIFACTS_SOURCE] + [source]


def keep_composed(
    context: ServiceContext,
    question: str,
    principal: str | None,
    result: dict[str, Any],
    *,
    save: bool | None = None,
    conversation_id: str | None = None,
    channel: str | None = None,
) -> dict[str, Any] | None:
    """Keep a composed answer's files when asked to, or when auto-save is on.

    Called by `services.assistant.answer` for every answer, so HTTP, the
    stream and MCP decide alike. ``save`` is the caller's choice for this
    answer; ``None`` follows ``auto_save``. A store that cannot keep it costs
    the artifact, never the answer.
    """

    from pheasant.services import artifact_settings
    from pheasant.services.artifact_trace import trace_of

    if (result.get("route") or {}).get("intent") != "compose":
        return None
    settings = artifact_settings.current(context)
    wanted = settings.auto_save if save is None else bool(save)
    if not (wanted and settings.enabled):
        return None
    parsed = artifact_payload(result)
    if not parsed.get("files"):
        return None
    payload = {
        "question": question,
        "summary": parsed.get("summary"),
        "files": parsed["files"],
        "not_covered": parsed.get("not_covered"),
        "citations": parsed.get("citations"),
        "trace": trace_of(
            result, question=question, conversation_id=conversation_id, channel=channel
        ),
    }
    try:
        record = _store(
            context,
            "files",
            _title(question, parsed["files"]),
            payload,
            owner(principal),
            conversation_id=conversation_id,
            channel=channel,
        )
    except Exception:  # noqa: BLE001 - the answer stands without its copy
        logger.warning("could not keep a composed artifact", exc_info=True)
        return None
    return summary_of(record)


def _report_from_answer(answer: dict[str, Any], question: str) -> dict[str, Any]:
    compact = artifact_payload({**answer, "answer_mode": None})
    if (answer.get("route") or {}).get("intent") == "compose":
        # A composed answer saved afterwards is the files it wrote, exactly
        # what auto-save would have kept.
        parsed = artifact_payload(answer)
        if parsed.get("files"):
            return {
                "question": question,
                "summary": parsed.get("summary"),
                "files": parsed["files"],
                "not_covered": parsed.get("not_covered"),
                "citations": parsed.get("citations"),
                "kind": "files",
            }
    return {
        "question": question,
        "answer": str(answer.get("answer") or ""),
        "citations": compact.get("citations"),
        "visual": answer.get("visual")
        if (answer.get("visual") or {}).get("status") == "ok"
        else None,
        "files": compact.get("files") or [],
    }


def save(context: ServiceContext, request: SaveRequest) -> dict[str, Any]:
    """Keep an answer or given content as an artifact; the artifact."""

    from pheasant.services.artifact_trace import channel_of, thread_payload, trace_of

    who = owner(request.principal)
    if request.kind is not None and request.kind not in KINDS:
        raise InvalidRequest(f"kind must be one of {', '.join(KINDS)}")
    kind = request.kind
    channel = channel_of(request.channel)
    keep = {
        "conversation_id": request.conversation_id,
        "channel": channel,
        "published": request.published,
        "replaces": request.replaces,
        "visibility_scope": request.scope,
    }
    drawn = kept_visual(request.visual)
    drawn_citations = (
        artifact_payload({"citations": request.citations, "answer_mode": None}).get("citations")
        if request.citations
        else None
    )
    if kind == "thread" or (kind is None and request.turns):
        payload, title = thread_payload(
            request.turns, conversation_id=request.conversation_id, channel=channel
        )
        record = _store(context, "thread", str(request.title or title)[:120], payload, who, **keep)
        return _with_payload(context, record)
    if request.answer_id:
        from pheasant.services import answer_resume

        kept = answer_resume.read(context, request.answer_id, who)
        # Its text exists and its diagram is still being drawn: a report saved
        # now would keep the answer without the picture it was asked with.
        waited = 0.0
        while kept.get("status") == "answered" and waited < VISUAL_WAIT_SECONDS:
            time.sleep(1.0)
            waited += 1.0
            kept = answer_resume.read(context, request.answer_id, who)
        if not kept.get("answer"):
            raise InvalidRequest("that answer is not finished yet; save it once it is done")
        answer = kept["answer"]
        question = str(answer.get("question") or "")
        payload = _report_from_answer(answer, question)
        kind = kind or payload.pop("kind", None) or "report"
        payload.pop("kind", None)
        payload["trace"] = trace_of(
            answer,
            question=question,
            conversation_id=request.conversation_id,
            channel=channel,
            answer_id=request.answer_id,
        )
        if drawn:
            payload["visual"] = drawn
            if drawn_citations:
                payload["visual_citations"] = drawn_citations
        title = request.title or _title(question, payload["files"])
    else:
        files = [
            {
                "name": render.safe_name(str(f.get("name") or ""), f"file-{n}.txt"),
                "language": f.get("language"),
                "content": str(f.get("content") or ""),
            }
            for n, f in enumerate(request.files or [], 1)
            if isinstance(f, dict)
        ]
        if not (request.markdown or files or drawn):
            raise InvalidRequest("save_artifact needs answer_id, markdown, files or a visual")
        payload = {
            "answer": request.markdown or "",
            "summary": request.summary,
            "files": files,
            "citations": drawn_citations or [],
            "visual": drawn,
        }
        title = request.title or (_title("", files) if files else "Report")
        kind = kind or "report"
    if request.scope not in {"org", "user", "session"}:
        raise InvalidRequest("artifact scope must be org, user or session")
    if request.scope != "org" and not who:
        raise InvalidRequest("a private artifact needs an identified writer")
    payload["scope"] = request.scope
    return _with_payload(context, _store(context, kind, str(title), payload, who, **keep))


def _with_payload(context: ServiceContext, record: dict[str, Any]) -> dict[str, Any]:
    from pheasant.artifacts import publications

    publication = publications.status(context.state, context.config.knowledge_base_id, record["id"])
    return {
        **summary_of(record),
        **(
            {"revision_id": publication["revision_id"], "publication_status": publication["status"]}
            if publication
            else {}
        ),
        "knowledge_base": context.config.knowledge_base_id,
        "payload": record["payload"],
    }


#: Every column a record is read from.
COLUMNS = "id, owner, kind, title, payload_json, created_at, session_id, channel, archived_at"


def _load(
    context: ServiceContext, artifact_id: str, principal: str | None = None
) -> dict[str, Any]:
    from pheasant.services import artifact_outputs

    if artifact_outputs.held(str(artifact_id)):
        return artifact_outputs.get(context, artifact_id)
    if str(artifact_id).startswith("mem-"):
        from pheasant.services.artifact_records import get_record

        return get_record(context, artifact_id, principal)
    rows = context.state.rows(
        f"SELECT {COLUMNS} FROM saved_artifacts WHERE id=? AND kb_id=?",
        (str(artifact_id), context.config.knowledge_base_id),
    )
    if not rows:
        raise ArtifactNotFound(artifact_id)
    record = _record(rows[0])
    if record["payload"].get("scope", "org") != "org" and record.get("owner") != owner(principal):
        raise ArtifactNotFound(artifact_id)
    _mark_indexed(context, [record])
    return record


def _mark_indexed(context: ServiceContext, records: list[dict[str, Any]]) -> None:
    from pheasant.services.artifact_inventory import mark_indexed

    mark_indexed(context, records)


def get(context: ServiceContext, artifact_id: str, principal: str | None = None) -> dict[str, Any]:
    """One artifact, its files' contents included."""

    return _with_payload(context, _load(context, artifact_id, principal))


def list_artifacts(
    context: ServiceContext,
    principal: str | None = None,
    *,
    limit: int = 50,
    owner_name: str | None = None,
    conversation_id: str | None = None,
    kind: str | None = None,
    archived: bool = False,
) -> dict[str, Any]:
    from pheasant.services.artifact_inventory import list_artifacts as listing

    return listing(
        context,
        principal,
        limit=limit,
        owner_name=owner_name,
        conversation_id=conversation_id,
        kind=kind,
        archived=archived,
    )


def _save_payload(context: ServiceContext, artifact_id: str, payload: dict[str, Any]) -> None:
    context.state.execute(
        "UPDATE saved_artifacts SET payload_json=?, updated_at=? WHERE id=? AND kb_id=?",
        (json.dumps(payload, default=str), _now(), artifact_id, context.config.knowledge_base_id),
    )


def _remove(context: ServiceContext, folder: str, names: list[str], actor: str | None) -> list[str]:
    """Take an artifact's files out of the index like any document's; the names it removed."""

    from pheasant.services import archive

    if _deferred(context):
        _defer_removal(context, folder, names, actor)
        _wake_projection(context)
        return names
    removed: list[str] = []
    paths = [f"{folder}/{name}" for name in names]
    indexed = {
        str(r["relative_path"])
        for r in context.state.rows(
            "SELECT relative_path FROM artifacts WHERE source_id=?", (ARTIFACTS_SOURCE,)
        )
    }
    present = [path for path in paths if path in indexed]
    if present:
        archive.archive_documents(
            context,
            archive.ArchiveRequest(
                documents=[f"{ARTIFACTS_SOURCE}/{path}" for path in present],
                actor=actor or "artifacts",
            ),
        )
        removed = [name for name, path in zip(names, paths, strict=True) if path in indexed]
    return removed


def delete(
    context: ServiceContext, artifact_id: str, principal: str | None = None
) -> dict[str, Any]:
    """Remove the artifact: its row, and its files from the index like any document's."""

    from pheasant.services import artifact_outputs

    if artifact_outputs.held(str(artifact_id)):
        return artifact_outputs.retire(context, artifact_id)

    if str(artifact_id).startswith("mem-"):
        from pheasant.services.artifact_records import retire

        return retire(context, artifact_id, principal)
    record = _load(context, artifact_id, principal)
    folder = (record.get("payload") or {}).get("folder")
    names = [name for name, _text in source_files(record)]
    if folder and _deferred(context):
        _defer_removal(context, folder, names, owner(principal), artifact_id=artifact_id)
        _wake_projection(context)
        return {"deleted": record["id"], "title": record["title"], "removed_files": names}
    removed = _remove(context, folder, names, owner(principal)) if folder else []
    _forget(context, artifact_id)
    return {"deleted": record["id"], "title": record["title"], "removed_files": removed}


def _forget(context: ServiceContext, artifact_id: str) -> None:
    from pheasant.artifacts.publications import deprecate

    deprecate(context.state, context.config.knowledge_base_id, artifact_id)
    context.state.execute(
        "DELETE FROM saved_artifacts WHERE id=? AND kb_id=?",
        (artifact_id, context.config.knowledge_base_id),
    )


def forget_removed(context: ServiceContext, source_name: str, paths: list[str]) -> None:
    """Documents removed from the ``artifacts`` source, as the artifacts they were.

    Removing an artifact's ``README.md`` (its index page) retires the artifact;
    removing one of its files drops that file. Called by `services/archive.py`,
    so the taxonomy view's Remove, ``@remove`` and MCP ``archive_documents``
    remove an artifact exactly as they remove anything else.
    """

    if source_name != ARTIFACTS_SOURCE:
        return
    by_artifact: dict[str, list[str]] = {}
    folder_of: dict[str, str] = {}
    for path in paths:
        parent = PurePosixPath(path).parent
        match = _FOLDER_ID.search(parent.name)
        if match:
            by_artifact.setdefault(match.group(1), []).append(PurePosixPath(path).name)
            folder_of[match.group(1)] = str(parent)
    for artifact_id, names in by_artifact.items():
        try:
            record = _load(context, artifact_id)
        except ArtifactNotFound:
            continue
        # A copy the artifact no longer lives at (re-filed), or an artifact
        # retention archived: the row is what stays.
        if record.get("archived_at") or (record["payload"] or {}).get("folder") != folder_of.get(
            artifact_id
        ):
            continue
        if INDEX_FILE in names:
            _forget(context, artifact_id)
            continue
        payload = record["payload"]
        payload["files"] = [f for f in payload.get("files") or [] if f.get("name") not in names]
        _save_payload(context, artifact_id, payload)


@dataclass(frozen=True)
class Rendered:
    """A document or an archive, and what a transport needs to send it."""

    content: str | bytes
    media_type: str
    filename: str


def view(
    context: ServiceContext,
    artifact_id: str,
    principal: str | None = None,
    *,
    file: str | None = None,
    mode: str = "code",
    theme: str | None = None,
) -> Rendered:
    """The artifact, or one file of it, as a pheasant-styled HTML document."""

    record = _load(context, artifact_id, principal)
    if file:
        chosen = render.find_file(record, file)
        if chosen is None:
            raise InvalidRequest(f"artifact {artifact_id} has no file named {file}")
        document = render.file_document(record, chosen, mode=mode, theme=theme)
        return Rendered(document, "text/html", f"{render.safe_name(file, 'file')}.html")
    return Rendered(
        render.overview(record, theme=theme), "text/html", f"{render.slug(record['title'])}.html"
    )


def export(
    context: ServiceContext,
    artifact_id: str,
    principal: str | None = None,
    *,
    format: str = "html",  # noqa: A002 - the public parameter's name
    file: str | None = None,
) -> Rendered:
    """Something to save: the HTML report, the Markdown, a .zip, or one file as written."""

    record = _load(context, artifact_id, principal)
    name = render.slug(record["title"])
    if file:
        chosen = render.find_file(record, file)
        if chosen is None:
            raise InvalidRequest(f"artifact {artifact_id} has no file named {file}")
        return Rendered(
            str(chosen.get("content") or ""),
            "text/plain; charset=utf-8",
            render.safe_name(file, "file.txt"),
        )
    if format == "zip":
        return Rendered(render.zip_export(record), "application/zip", f"{name}.zip")
    if format == "markdown":
        return Rendered(
            render.markdown_export(record), "text/markdown; charset=utf-8", f"{name}.md"
        )
    if format == "html":
        return Rendered(render.overview(record), "text/html; charset=utf-8", f"{name}.html")
    raise InvalidRequest(f"format must be one of {', '.join(EXPORT_FORMATS)}")
