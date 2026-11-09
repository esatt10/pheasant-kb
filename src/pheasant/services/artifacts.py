"""Artifacts the region keeps: composed files and saved reports, until deleted.

A composed SuiteScript or Terraform module, or an answer worth keeping as a
report, used to live only in the browser tab that asked for it -- the region
keeps no chat state, so a refresh took it away, where an MCP host keeps its
tool results in the conversation and shows them again. Now the region keeps
it: every composed answer is saved as a ``files`` artifact the moment it is
written, and any answer the stream kept (`services/answer_resume.py`) or any
content a caller sends can be saved as a ``report``. They are listed, read,
rendered (`services/artifact_render.py`), exported and deleted through one
operation each, behind ``/artifacts`` and the MCP artifact tools.

Owned by the principal that made them, as `effective_principal` names it --
a signed-in person is the same owner over HTTP and MCP -- and read back by
that principal only: an id held for someone else is the same 404 as one never
held. Stored as rows (``saved_artifacts``), so a replica, a restart or a
backend migration keeps them: they are user data (rule 2).
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pheasant.assistant.artifact_files import artifact_payload
from pheasant.security.identity import effective_principal
from pheasant.services import ServiceContext
from pheasant.services import artifact_render as render
from pheasant.services.errors import ArtifactNotFound, InvalidRequest

KINDS = ("files", "report")
#: What one saved artifact may hold. A composed module of forty files is a few
#: hundred kilobytes; this refuses a payload that is not an artifact at all.
MAX_BYTES = 2_000_000
EXPORT_FORMATS = ("html", "markdown", "zip")


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


def summary_of(record: dict[str, Any]) -> dict[str, Any]:
    """What a listing shows: no file contents."""

    payload = record.get("payload") or {}
    return {
        "id": record["id"],
        "kind": record["kind"],
        "title": record["title"],
        "created_at": record["created_at"],
        "files": [
            {
                "name": f.get("name"),
                "language": f.get("language"),
                "previewable": render.previewable(str(f.get("name") or "")),
            }
            for f in payload.get("files") or []
        ],
        "has_visual": render.has_visual(payload),
    }


def _store(
    context: ServiceContext, kind: str, title: str, payload: dict[str, Any], who: str | None
) -> dict[str, Any]:
    encoded = json.dumps(payload, default=str)
    if len(encoded.encode("utf-8")) > MAX_BYTES:
        raise InvalidRequest(f"an artifact holds at most {MAX_BYTES // 1_000_000} MB")
    record = {
        "id": secrets.token_hex(12),
        "kind": kind,
        "title": title,
        "created_at": _now(),
        "payload": payload,
    }
    context.state.execute(
        "INSERT INTO saved_artifacts(id, kb_id, owner, kind, title, payload_json, created_at, "
        "updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            record["id"],
            context.config.knowledge_base_id,
            who,
            kind,
            title,
            encoded,
            record["created_at"],
            record["created_at"],
        ),
    )
    return record


def keep_composed(
    context: ServiceContext, question: str, principal: str | None, result: dict[str, Any]
) -> dict[str, Any] | None:
    """Save a composed answer's files, if it wrote any; its listing summary.

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
        import logging

        logging.getLogger(__name__).warning("could not keep a composed artifact", exc_info=True)
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


def _load(context: ServiceContext, artifact_id: str, principal: str | None) -> dict[str, Any]:
    rows = context.state.rows(
        "SELECT id, owner, kind, title, payload_json, created_at FROM saved_artifacts "
        "WHERE id=? AND kb_id=?",
        (str(artifact_id), context.config.knowledge_base_id),
    )
    row = dict(rows[0]) if rows else None
    if row is None or (row.get("owner") or None) != owner(principal):
        raise ArtifactNotFound(artifact_id)
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "created_at": row["created_at"],
        "payload": json.loads(row["payload_json"] or "{}"),
    }


def get(context: ServiceContext, artifact_id: str, principal: str | None = None) -> dict[str, Any]:
    """One artifact, its files' contents included."""

    return _with_payload(context, _load(context, artifact_id, principal))


def list_artifacts(
    context: ServiceContext, principal: str | None = None, *, limit: int = 50
) -> dict[str, Any]:
    """The caller's artifacts, newest first, without file contents."""

    who = owner(principal)
    limit = max(1, min(int(limit or 50), 200))
    clause, params = ("owner=?", (who,)) if who else ("owner IS NULL", ())
    rows = context.state.rows(
        "SELECT id, kind, title, payload_json, created_at FROM saved_artifacts "
        f"WHERE kb_id=? AND {clause} ORDER BY created_at DESC, id DESC LIMIT ?",
        (context.config.knowledge_base_id, *params, limit),
    )
    records = [{**dict(row), "payload": json.loads(row["payload_json"] or "{}")} for row in rows]
    return {"artifacts": [summary_of(record) for record in records], "limit": limit}


def delete(
    context: ServiceContext, artifact_id: str, principal: str | None = None
) -> dict[str, Any]:
    record = _load(context, artifact_id, principal)
    context.state.execute(
        "DELETE FROM saved_artifacts WHERE id=? AND kb_id=?",
        (record["id"], context.config.knowledge_base_id),
    )
    return {"deleted": record["id"], "title": record["title"]}


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
