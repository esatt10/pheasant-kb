"""Artifacts the region keeps: composed files and saved reports, as knowledge.

A composed SuiteScript or Terraform module, or an answer worth keeping as a
report, used to live only in the browser tab that asked for it -- the region
keeps no chat state, so a refresh took it away. Now every composed answer is
kept the moment it is written, and any answer the stream kept
(`services/answer_resume.py`) or any content a caller sends can be kept as a
report. And kept means *knowledge*, not a sidecar:

* **A source.** Each artifact lands, through the ordinary submission path
  (`services/ingestion.py`: receipts, idempotent placement, forwarded to the
  writing tier on a fleet), in the ``artifacts`` source -- a landing-zone
  ``document_folder`` like the drop zone's -- and the normal pipeline indexes
  it. Its files are searchable from the overall index, browsable in the
  taxonomy view, and removable like any other document (`services/archive.py`;
  an upload is removed outright, its bytes deleted by the indexer).
* **A directory by identity, then time.** ``<owner>/<yyyy>/<mm>/<title>--<id>/``:
  a person's artifacts stay together however many there are, a month keeps any
  one folder small, and the id makes every artifact's folder its own.
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
from pheasant.security.identity import effective_principal
from pheasant.services import ServiceContext
from pheasant.services import artifact_render as render
from pheasant.services.errors import ArtifactNotFound, InvalidRequest, ServiceError

logger = logging.getLogger(__name__)

#: The source every artifact lands in.
ARTIFACTS_SOURCE = "artifacts"
#: Each artifact's index page: what it is, and the documents it cites.
INDEX_FILE = "README.md"
#: The rendered report, for a report that draws a diagram.
REPORT_FILE = "report.html"
KINDS = ("files", "report")
#: What one saved artifact may hold. A composed module of forty files is a few
#: hundred kilobytes; this refuses a payload that is not an artifact at all.
MAX_BYTES = 2_000_000
EXPORT_FORMATS = ("html", "markdown", "zip")
#: How long saving a report waits for the diagram its answer is still drawing.
VISUAL_WAIT_SECONDS = 60
_FOLDER_ID = re.compile(r"--([0-9a-f]{24})$")
#: How a search or an answer treats saved artifacts, like ``memory`` does for
#: memory records: ``include`` (the default: they are knowledge like any other
#: document), ``exclude`` (the corpus as it was before anything was composed
#: from it) or ``only`` (search what has been composed).
ARTIFACT_MODES = ("include", "exclude", "only")


def artifacts_mode(value: Any) -> str:
    """``include`` / ``exclude`` / ``only`` from what a caller sent, or a refusal.

    A boolean reads as the memory toggle does: ``true`` includes, ``false``
    leaves them out.
    """

    if value is None or value == "" or value is True:
        return "include"
    if value is False:
        return "exclude"
    mode = str(value).strip().lower()
    if mode in ARTIFACT_MODES:
        return mode
    raise InvalidRequest(
        f"artifacts must be one of {', '.join(ARTIFACT_MODES)} (or true/false); got {value!r}"
    )


def scope(
    value: Any,
    source_name: str | None,
    source_names: list[str] | None,
    exclude_sources: list[str] | None,
) -> tuple[str | None, list[str] | None, list[str] | None]:
    """``(source_name, source_names, exclude_sources)`` with the artifacts toggle applied.

    ``exclude`` is an ordinary source exclusion and ``only`` the ordinary
    single-source scope, so every arm, filter and over-fetch already honours
    it. ``only`` beside a scope naming other sources is refused rather than
    silently overriding it.
    """

    mode = artifacts_mode(value)
    if mode == "exclude":
        excluded = list(dict.fromkeys([*(exclude_sources or []), ARTIFACTS_SOURCE]))
        return source_name, source_names, excluded
    if mode == "only":
        named = {n for n in [source_name, *(source_names or [])] if n} - {ARTIFACTS_SOURCE}
        if named:
            raise InvalidRequest(
                "artifacts: only searches the artifacts source and cannot be combined "
                f"with a scope naming {', '.join(sorted(named))}"
            )
        return ARTIFACTS_SOURCE, None, exclude_sources
    return source_name, source_names, exclude_sources


@dataclass(frozen=True)
class SaveRequest:
    """Keep ``title`` as an artifact, from a kept answer or from given content.

    With ``answer_id``, the answer the stream kept (``answer_resume``) is saved
    as a report; otherwise ``markdown`` and/or ``files`` are the content.
    """

    title: str | None = None
    kind: str = "report"
    answer_id: str | None = None
    markdown: str | None = None
    files: list[dict[str, Any]] | None = None
    summary: str | None = None
    principal: str | None = None
    knowledge_base: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def owner(principal: str | None = None) -> str | None:
    return effective_principal(principal) or None


def _title(question: str, files: list[dict[str, Any]]) -> str:
    words = " ".join(w for w in str(question or "").split() if not w.startswith("@"))
    if words:
        return words[:90] + ("…" if len(words) > 90 else "")
    return files[0]["name"] if files else "Untitled artifact"


def folder_for(who: str | None, created_at: str, title: str, artifact_id: str) -> str:
    """``<owner>/<yyyy>/<mm>/<title>--<id>``: identity, then time, then the artifact."""

    from pheasant.ingestion.landing import safe_filename

    person = safe_filename(who or "shared", fallback="shared")
    return f"{person}/{created_at[:4]}/{created_at[5:7]}/{render.slug(title)[:48]}--{artifact_id}"


def summary_of(record: dict[str, Any]) -> dict[str, Any]:
    """What a listing shows: no file contents."""

    payload = record.get("payload") or {}
    folder = payload.get("folder")
    return {
        "id": record["id"],
        "kind": record["kind"],
        "title": record["title"],
        "created_at": record["created_at"],
        "owner": record.get("owner"),
        "source": ARTIFACTS_SOURCE if folder else None,
        "folder": folder,
        "files": [
            {
                "name": f.get("name"),
                "language": f.get("language"),
                "previewable": render.previewable(str(f.get("name") or ""), f.get("language")),
            }
            for f in payload.get("files") or []
        ],
        "has_visual": render.has_visual(payload),
    }


def _store(
    context: ServiceContext, kind: str, title: str, payload: dict[str, Any], who: str | None
) -> dict[str, Any]:
    artifact_id = secrets.token_hex(12)
    created = _now()
    payload = {**payload, "folder": folder_for(who, created, title, artifact_id)}
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
    }
    context.state.execute(
        "INSERT INTO saved_artifacts(id, kb_id, owner, kind, title, payload_json, created_at, "
        "updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            artifact_id,
            context.config.knowledge_base_id,
            who,
            kind,
            title,
            encoded,
            created,
            created,
        ),
    )
    _publish(context, record)
    return record


def index_page(record: dict[str, Any]) -> str:
    """The artifact's ``README.md``: what it is, its content's gist, and its sources.

    Each source is a Markdown link to the cited document's path, which is
    what makes it an edge: the Markdown pass reads the link as a reference
    and cross-source resolution resolves the path to that document.
    """

    payload = record.get("payload") or {}
    kind = render.KIND_LABELS.get(str(record.get("kind")), "Artifact")
    lines = [f"# {record.get('title')}", ""]
    if payload.get("question"):
        lines += [f"> {payload['question']}", ""]
    lines += [
        f"- Artifact: `{record.get('id')}`",
        f"- Kind: {kind}",
        f"- Saved by: {record.get('owner') or 'shared'}",
        f"- Saved: {str(record.get('created_at') or '')[:16].replace('T', ' ')} UTC",
    ]
    files = [str(f.get("name")) for f in payload.get("files") or []]
    if files:
        lines.append(f"- Files: {', '.join(f'`{name}`' for name in files)}")
    lines.append("")
    body = payload.get("answer") if record.get("kind") == "report" else payload.get("summary")
    if body:
        lines += [str(body).strip(), ""]
    if payload.get("not_covered"):
        lines += ["## Not covered by the sources", "", str(payload["not_covered"]).strip(), ""]
    cited = [c for c in payload.get("citations") or [] if c.get("path")]
    if cited:
        lines += ["## Sources", ""]
        for citation in cited:
            label = f"{citation.get('source')} · {citation.get('path')}"
            if citation.get("section"):
                label += f" — {citation['section']}"
            target = str(citation["path"]).replace(" ", "%20").replace(")", "%29")
            lines.append(f"{citation.get('index')}. [{label.replace(']', '')}]({target})")
        lines.append("")
    return "\n".join(lines)


def source_files(record: dict[str, Any]) -> list[tuple[str, str]]:
    """``(name, text)`` for every file of the artifact's folder, the index page first."""

    payload = record.get("payload") or {}
    files = [(INDEX_FILE, index_page(record))]
    files += [
        (render.safe_name(str(f.get("name") or ""), "file.txt"), str(f.get("content") or ""))
        for f in payload.get("files") or []
    ]
    if record.get("kind") == "report" and render.has_visual(payload):
        files.append((REPORT_FILE, render.overview(record)))
    return files


def _publish(context: ServiceContext, record: dict[str, Any], *, sync: bool = True) -> None:
    """Land the artifact in its source and ask for that source to be indexed.

    Best-effort past the row: a region that cannot land it (a read-only
    replica with no landing service) still has the artifact, just not yet as
    knowledge, and says so in the log.
    """

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
        if sync and getattr(context, "indexing", None) is not None:
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
        return
    from pheasant.config.schema import PheasantConfig
    from pheasant.registry.source_registry import SourceRegistry

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
                }
            ]
        }
    ).sources[0]
    SourceRegistry(config, context.state).register_source(source)
    config.sources = [s for s in config.sources if s.name != ARTIFACTS_SOURCE] + [source]


def keep_composed(
    context: ServiceContext, question: str, principal: str | None, result: dict[str, Any]
) -> dict[str, Any] | None:
    """Keep a composed answer's files, if it wrote any; its listing summary.

    Called by `services.assistant.answer` for every compose answer, so HTTP,
    the stream and MCP keep the same artifact. A store that cannot keep it
    costs the artifact, never the answer.
    """

    if (result.get("route") or {}).get("intent") != "compose":
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
    }
    try:
        record = _store(
            context, "files", _title(question, parsed["files"]), payload, owner(principal)
        )
    except Exception:  # noqa: BLE001 - the answer stands without its copy
        logger.warning("could not keep a composed artifact", exc_info=True)
        return None
    return summary_of(record)


def _report_from_answer(answer: dict[str, Any], question: str) -> dict[str, Any]:
    compact = artifact_payload({**answer, "answer_mode": None})
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

    who = owner(request.principal)
    kind = request.kind if request.kind in KINDS else None
    if kind is None:
        raise InvalidRequest(f"kind must be one of {', '.join(KINDS)}")
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
        if not (request.markdown or files):
            raise InvalidRequest("save_artifact needs answer_id, markdown or files")
        payload = {
            "answer": request.markdown or "",
            "summary": request.summary,
            "files": files,
            "citations": [],
        }
        title = request.title or (_title("", files) if files else "Report")
    return _with_payload(context, _store(context, kind, str(title), payload, who))


def _with_payload(context: ServiceContext, record: dict[str, Any]) -> dict[str, Any]:
    return {
        **summary_of(record),
        "knowledge_base": context.config.knowledge_base_id,
        "payload": record["payload"],
    }


def _record(row: Any) -> dict[str, Any]:
    row = dict(row)
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "created_at": row["created_at"],
        "owner": row.get("owner"),
        "payload": json.loads(row.get("payload_json") or "{}"),
    }


def _load(context: ServiceContext, artifact_id: str) -> dict[str, Any]:
    rows = context.state.rows(
        "SELECT id, owner, kind, title, payload_json, created_at FROM saved_artifacts "
        "WHERE id=? AND kb_id=?",
        (str(artifact_id), context.config.knowledge_base_id),
    )
    if not rows:
        raise ArtifactNotFound(artifact_id)
    return _record(rows[0])


def get(context: ServiceContext, artifact_id: str, principal: str | None = None) -> dict[str, Any]:
    """One artifact, its files' contents included."""

    return _with_payload(context, _load(context, artifact_id))


def list_artifacts(
    context: ServiceContext,
    principal: str | None = None,
    *,
    limit: int = 50,
    owner_name: str | None = None,
) -> dict[str, Any]:
    """The region's artifacts, newest first, without contents; ``owner_name`` for one person's."""

    limit = max(1, min(int(limit or 50), 200))
    clause, params = ("AND owner=?", (owner_name,)) if owner_name else ("", ())
    rows = context.state.rows(
        "SELECT id, owner, kind, title, payload_json, created_at FROM saved_artifacts "
        f"WHERE kb_id=? {clause} ORDER BY created_at DESC, id DESC LIMIT ?",
        (context.config.knowledge_base_id, *params, limit),
    )
    records = [_record(row) for row in rows]
    _name_unnamed(context, records)
    _file_unfiled(context, records)
    return {
        "artifacts": [summary_of(record) for record in records],
        "limit": limit,
        "source": ARTIFACTS_SOURCE,
    }


#: Artifacts filed into the source per listing, for those kept before they were knowledge.
FILE_PER_LISTING = 20


def _file_unfiled(context: ServiceContext, records: list[dict[str, Any]]) -> None:
    """Give an artifact kept before artifacts were knowledge its folder, and land it.

    Done once per artifact, the first time a listing shows it: its folder is
    written to the row, so the next listing finds it filed. One sync for all
    of them, not one per artifact.
    """

    unfiled = [r for r in records if not (r.get("payload") or {}).get("folder")]
    for record in unfiled[:FILE_PER_LISTING]:
        folder = folder_for(
            record.get("owner"), record["created_at"], record["title"], record["id"]
        )
        record["payload"]["folder"] = folder
        _save_payload(context, record["id"], record["payload"])
        _publish(context, record, sync=False)
    if unfiled and getattr(context, "indexing", None) is not None:
        try:
            context.indexing.start_sync(ARTIFACTS_SOURCE)
        except Exception:  # noqa: BLE001 - the next sync files them anyway
            logger.warning("could not start the artifacts sync", exc_info=True)


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
        _save_payload(context, record["id"], payload)
        if payload.get("folder"):  # an unfiled one lands under its new name
            _publish(context, record)
            _remove(context, payload["folder"], renamed, record.get("owner"))


def _save_payload(context: ServiceContext, artifact_id: str, payload: dict[str, Any]) -> None:
    context.state.execute(
        "UPDATE saved_artifacts SET payload_json=?, updated_at=? WHERE id=? AND kb_id=?",
        (json.dumps(payload, default=str), _now(), artifact_id, context.config.knowledge_base_id),
    )


def _remove(context: ServiceContext, folder: str, names: list[str], actor: str | None) -> list[str]:
    """Take an artifact's files out of the index like any document's; the names it removed."""

    from pheasant.services import archive

    removed: list[str] = []
    for name in names:
        path = f"{ARTIFACTS_SOURCE}/{folder}/{name}"
        try:
            archive.archive_documents(
                context, archive.ArchiveRequest(documents=[path], actor=actor or "artifacts")
            )
            removed.append(name)
        except ServiceError:
            # Not indexed yet (an indexer has not reached it): nothing in the
            # index to take out, and nothing is lost by moving on.
            continue
    return removed


def delete(
    context: ServiceContext, artifact_id: str, principal: str | None = None
) -> dict[str, Any]:
    """Remove the artifact: its row, and its files from the index like any document's."""

    record = _load(context, artifact_id)
    folder = (record.get("payload") or {}).get("folder")
    names = [name for name, _text in source_files(record)]
    removed = _remove(context, folder, names, owner(principal)) if folder else []
    _forget(context, artifact_id)
    return {"deleted": record["id"], "title": record["title"], "removed_files": removed}


def _forget(context: ServiceContext, artifact_id: str) -> None:
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
    for path in paths:
        parent = PurePosixPath(path).parent
        match = _FOLDER_ID.search(parent.name)
        if match:
            by_artifact.setdefault(match.group(1), []).append(PurePosixPath(path).name)
    for artifact_id, names in by_artifact.items():
        try:
            record = _load(context, artifact_id)
        except ArtifactNotFound:
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

    record = _load(context, artifact_id)
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

    record = _load(context, artifact_id)
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
