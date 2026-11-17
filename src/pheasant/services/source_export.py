"""Exporting a whole source: every document it holds, as one file to keep.

``services.source_browser`` reads one document at a time, which is right for a
viewer and wrong for "give me everything in this source": an agent that wants
a source's 2,600 documents would make 2,600 calls and carry every one through
its own context. :func:`export_source` answers that question in one call (or a
few, paged by size), in the format the caller will work with:

* ``zip`` — every document at its own path under ``files/``, plus
  ``manifest.jsonl`` (one row of metadata per document, no text),
  ``chunks.jsonl`` when asked for, and a ``README.md`` saying what is where.
  A document whose original bytes are still the ones indexed is written as
  those bytes, so a PDF comes back a PDF; anything else is written as the text
  the index holds, and a binary format's extracted text takes a ``.txt``
  suffix so nothing pretends to be a file it is not.
* ``jsonl`` — one JSON object per document: its metadata, its text and its
  outline (and its chunks when asked for).
* ``parquet`` — the same rows as one table, ``outline`` and ``chunks`` as JSON
  text so the schema never depends on what a page happened to hold. Written
  by DuckDB (the ``analytics`` extra), which here is what `analytics.py`
  already uses it as: a Parquet writer over a read, never a state backend.

Text comes from ``source_browser._content``, the one place a document's text
is chosen, so an export and ``read_document`` cannot disagree about what a
document says. The documents are the ones the inventory lists: the same scope
(``inventory.ARTIFACT_SCOPE``) and, under ``security.acl_enforced``, the same
reader rule, so an export never holds a document the caller could not open.

**A folder, or one file.** ``path_prefix`` narrows the export to everything
under one folder (or to one file), with the rule the taxonomy view's "search
only here" uses: the path itself, or anything beneath it. It is what exporting
from inside a folder of the taxonomy view sends; a prefix matching nothing is
refused rather than answered with an empty file.

**Paged by size, not by count -- for the JSON answer.** One part holds
documents until their text passes :data:`MAX_PART_BYTES`; ``next_offset``
says where the next part starts and ``has_more`` whether there is one. A part
always holds at least one document, so a source with one huge document still
makes progress. Binary formats come back base64-encoded (``encoding``), as
``export_artifact`` does. That bound exists because MCP carries the file
inside its result.

**A download is one file** (:func:`export_source_file`, HTTP's
``download=true``): the whole selection, written to disk a document at a time
and streamed back, so a source's tree arrives as one archive rather than as
seven, each holding a slice of it -- which is what exporting an uploads source
of 48 documents in 32 MB parts produced. :data:`MAX_FILE_BYTES` still bounds
one file, past which it continues in a second, the same way. Both paths use
one writer, so a source that fits in one part gives the same bytes either way.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from pheasant.ingestion.content_types import MEMORY_ARTIFACT_TYPE
from pheasant.services import ServiceContext
from pheasant.services.errors import CapabilityUnsupported, InvalidRequest
from pheasant.services.inventory import ARTIFACT_SCOPE, extension_of, reader

FORMATS = ("zip", "jsonl", "parquet")
MEDIA_TYPES = {
    "zip": "application/zip",
    "jsonl": "application/x-ndjson",
    "parquet": "application/vnd.apache.parquet",
}
#: Bytes of document text one part holds before it stops. Sized so a part
#: base64-encoded stays a payload an MCP client can take in one result.
MAX_PART_BYTES = 32 * 1024 * 1024
#: Documents one part may hold whatever their size: the outline query is one
#: statement per document, and a part is a request somebody is waiting on.
MAX_PART_DOCUMENTS = 5000
#: Bytes of documents one *downloaded* file holds before it continues in a
#: second: a ceiling for the disk the api writes it to, not a page size.
MAX_FILE_BYTES = 2 * 1024**3

_COLUMNS = (
    "a.id, a.source_id, a.relative_path, a.type, a.path, a.size_bytes, a.sha256, "
    "a.mime_type, a.last_indexed_at"
)


@dataclass(frozen=True)
class ExportRequest:
    """Which source to export, how, and which part of it."""

    source_name: str
    knowledge_base: str | None = None
    #: ``zip``, ``jsonl`` or ``parquet``.
    format: str = "zip"
    #: The first document of this part, in path order; ``next_offset`` of the
    #: previous part.
    offset: int = 0
    #: Add every chunk (index, line span, heading path, text) per document.
    include_chunks: bool = False
    principal: str | None = None
    principal_groups: list[str] | None = None
    #: One folder (everything beneath it) or one file; ``None`` is the whole source.
    path_prefix: str | None = None


@dataclass(frozen=True)
class _Selection:
    kb_id: str
    source: str
    fmt: str
    prefix: str
    offset: int
    rows: list[Any]


def _select(context: ServiceContext, request: ExportRequest) -> _Selection:
    from pheasant.services.inventory_detail import _registered

    kb_id = context.knowledge_base(request.knowledge_base)
    fmt = str(request.format or "").strip().lower()
    if fmt not in FORMATS:
        raise InvalidRequest(f"format must be one of {', '.join(FORMATS)}; got {request.format!r}")
    offset = max(0, int(request.offset))
    source = str(_registered(context, request.source_name)["name"])
    prefix = str(request.path_prefix or "").strip().strip("/")
    rows = _rows(context, source, request.principal, request.principal_groups, prefix)
    if prefix and not rows:
        raise InvalidRequest(f"No document under {prefix!r} in {source!r}")
    if offset > len(rows):
        raise InvalidRequest(f"offset {offset} is past the {len(rows)} documents in {source!r}")
    return _Selection(kb_id, source, fmt, prefix, offset, rows)


def _describe(selection: _Selection, documents: int, size: int) -> dict[str, Any]:
    """What a written part is: its name, type, extent and where the next one starts."""

    total = len(selection.rows)
    next_offset = selection.offset + documents
    has_more = next_offset < total
    named = (
        f"{selection.source}-{_slug(selection.prefix)}" if selection.prefix else selection.source
    )
    whole = selection.offset == 0 and not has_more
    stem = named if whole else f"{named}-{selection.offset:06d}"
    return {
        "knowledge_base": selection.kb_id,
        "source": selection.source,
        "format": selection.fmt,
        "filename": f"{stem}.{selection.fmt}",
        "media_type": MEDIA_TYPES[selection.fmt],
        "bytes": size,
        "documents": documents,
        "total": total,
        "offset": selection.offset,
        "next_offset": next_offset if has_more else None,
        "has_more": has_more,
    }


def export_source(context: ServiceContext, request: ExportRequest) -> dict[str, Any]:
    """One part of ``request.source_name`` as a file: ``content`` plus how to read it.

    Returns ``filename``, ``media_type``, ``encoding`` (``base64`` for zip and
    parquet, ``utf-8`` for jsonl), ``content``, ``bytes`` (the decoded size),
    ``documents`` (in this part), ``total`` (in the source), ``offset``,
    ``next_offset`` and ``has_more``.
    """

    selection = _select(context, request)
    buffer = io.BytesIO()
    documents = _write_into(
        context, selection, request.include_chunks, buffer, MAX_PART_BYTES, MAX_PART_DOCUMENTS
    )
    payload = buffer.getvalue()
    if selection.fmt == "jsonl":
        content, encoding = payload.decode("utf-8"), "utf-8"
    else:
        content, encoding = base64.b64encode(payload).decode("ascii"), "base64"
    described = _describe(selection, documents, len(payload))
    return {**described, "encoding": encoding, "content": content}


def export_source_file(
    context: ServiceContext,
    request: ExportRequest,
    target: BinaryIO,
    on_document: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """The whole selection (from ``offset``) written to ``target`` as one file.

    What :func:`export_source` describes, less ``content`` and ``encoding``:
    the bytes are in ``target``. One file holds everything unless it passes
    :data:`MAX_FILE_BYTES`, when ``next_offset`` says where the next starts.
    HTTP's ``download=true`` is its caller; MCP, which carries the file inside
    its result, pages with :func:`export_source` instead. ``on_document(done,
    total)`` is called after each document, for a job's progress.
    """

    selection = _select(context, request)
    documents = _write_into(
        context, selection, request.include_chunks, target, MAX_FILE_BYTES, None, on_document
    )
    return _describe(selection, documents, target.tell())


def _write_into(  # noqa: PLR0913 - the selection, the budget and where to write
    context: ServiceContext,
    selection: _Selection,
    include_chunks: bool,
    target: BinaryIO,
    max_bytes: int,
    max_documents: int | None,
    on_document: Callable[[int, int], None] | None = None,
) -> int:
    """Write documents from ``selection.offset`` into ``target`` until a budget is
    spent (always at least one); return how many went in.

    One document is held in memory at a time, so a file is bounded by the
    budget, not by what a process can hold.
    """

    def records():
        used = 0
        for count, row in enumerate(selection.rows[selection.offset :]):
            if count and (
                used >= max_bytes or (max_documents is not None and count >= max_documents)
            ):
                return
            record, original = _record(context, row, selection.fmt, include_chunks)
            used += len((record["content"] or "").encode("utf-8")) + len(original or b"")
            yield record, original
            if on_document is not None:
                on_document(selection.offset + count + 1, len(selection.rows))

    if selection.fmt == "jsonl":
        written = 0
        for record, _ in records():
            target.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
            written += 1
        return written
    if selection.fmt == "parquet":
        return _parquet(records(), include_chunks, target)
    return _zip(selection.source, selection.kb_id, records(), include_chunks, target)


def _rows(
    context: ServiceContext,
    source: str,
    principal: str | None,
    groups: list[str] | None,
    prefix: str = "",
) -> list[Any]:
    """Every document of ``source`` (under ``prefix``) the caller may read, in path order."""

    admit = reader(context, principal, groups)
    columns = _COLUMNS if admit is None else f"{_COLUMNS}, a.acl"
    where, params = "", ()
    if prefix:
        # `substr`, not LIKE: SQLite's LIKE folds case and Postgres's does not.
        where = " AND (a.relative_path = ? OR substr(a.relative_path, 1, ?) = ?)"
        params = (prefix, len(prefix) + 1, f"{prefix}/")
    rows = context.state.rows(
        f"SELECT {columns} FROM artifacts a WHERE {ARTIFACT_SCOPE} AND a.source_id = ?{where} "
        "ORDER BY a.relative_path, a.id",
        (MEMORY_ARTIFACT_TYPE, source, *params),
    )
    return list(rows) if admit is None else admit(list(rows))


def _slug(prefix: str) -> str:
    """A folder as part of a file name: ``a.zip/files`` -> ``a.zip-files``."""

    cleaned = "".join(c if c.isalnum() or c in "._-" else "-" for c in prefix.replace("/", "-"))
    return cleaned.strip("-.")[:80] or "folder"


def _record(
    context: ServiceContext, row: Any, fmt: str, include_chunks: bool
) -> tuple[dict[str, Any], bytes | None]:
    """One document's row, and its original bytes when a zip should carry them."""

    from pheasant.services.source_browser import _content, _indexed_bytes, _outline

    artifact_id = str(row["id"])
    relative = str(row["relative_path"] or "")
    content, origin, viewer, language = _content(context, row)
    record: dict[str, Any] = {
        "id": artifact_id,
        "source": row["source_id"],
        "path": relative,
        "name": PurePosixPath(relative).name or relative,
        "type": row["type"],
        "extension": extension_of(relative),
        "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "sha256": row["sha256"],
        "last_indexed_at": row["last_indexed_at"],
        "viewer": viewer,
        "language": language,
        "content_origin": origin,
        "content": content,
        "outline": _outline(context, artifact_id),
    }
    if include_chunks:
        record["chunks"] = _chunks(context, artifact_id)
    # Only a zip has somewhere to put a file's own bytes; for a text file read
    # from disk they are the content again, so they are not read twice.
    original = None
    if fmt == "zip" and origin != "file":
        original = _indexed_bytes(row, limit=None)
    return record, original


def _chunks(context: ServiceContext, artifact_id: str) -> list[dict[str, Any]]:
    return [
        {
            "id": row["id"],
            "chunk_index": row["chunk_index"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "heading_path": row["heading_path"],
            "text": row["text"],
        }
        for row in context.state.rows(
            "SELECT id, chunk_index, start_line, end_line, heading_path, text FROM chunks "
            "WHERE artifact_id=? ORDER BY chunk_index",
            (artifact_id,),
        )
    ]


def _member(record: dict[str, Any], has_original: bool) -> str:
    """Where a document goes in the zip: its own path, unless that would lie."""

    path = record["path"] or record["id"]
    from pheasant.services.source_browser import EXTRACTED_VIEWERS

    if has_original or record["viewer"] not in EXTRACTED_VIEWERS:
        return f"files/{path}"
    # A PDF's extracted text is not a PDF.
    return f"files/{path}.txt"


def _zip(source: str, kb_id: str, records: Any, include_chunks: bool, target: BinaryIO) -> int:
    manifest: list[str] = []
    chunks: list[str] = []
    written = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for record, original in records:
            member = _member(record, original is not None)
            if original is not None:
                archive.writestr(member, original)
            elif record["content"] is not None:
                archive.writestr(member, record["content"].encode("utf-8"))
            else:
                member = None
            row = {k: v for k, v in record.items() if k not in ("content", "chunks")}
            row["file"] = member
            manifest.append(json.dumps(row, ensure_ascii=False))
            for chunk in record.get("chunks") or []:
                chunks.append(
                    json.dumps({"document_id": record["id"], **chunk}, ensure_ascii=False)
                )
            written += 1
        archive.writestr("manifest.jsonl", "".join(line + "\n" for line in manifest))
        if include_chunks:
            archive.writestr("chunks.jsonl", "".join(line + "\n" for line in chunks))
        archive.writestr("README.md", _readme(source, kb_id, written, include_chunks))
    return written


def _readme(source: str, kb_id: str, documents: int, include_chunks: bool) -> str:
    lines = [
        f"# {source}",
        "",
        f"{documents} documents exported from source `{source}` of knowledge base `{kb_id}`.",
        "",
        "- `files/` — each document at its own path: its original bytes while they are",
        "  still the ones indexed, otherwise the text the index holds (a binary format's",
        "  extracted text with a `.txt` suffix).",
        "- `manifest.jsonl` — one row per document: id, path, type, size, sha256, last",
        "  indexed, viewer, where its text came from (`content_origin`), its outline, and",
        "  `file`, its member in this archive (null when the index holds no text).",
    ]
    if include_chunks:
        lines.append(
            "- `chunks.jsonl` — every chunk: `document_id`, index, line span, heading path, text."
        )
    return "\n".join(lines) + "\n"


def _parquet(records: Any, include_chunks: bool, target: BinaryIO) -> int:
    try:
        import duckdb
    except ModuleNotFoundError as exc:  # pragma: no cover - dep-gated
        raise CapabilityUnsupported(
            "parquet export", "needs DuckDB: pip install 'pheasant-kb[analytics]'"
        ) from exc

    columns = {
        "id": "VARCHAR",
        "source": "VARCHAR",
        "path": "VARCHAR",
        "name": "VARCHAR",
        "type": "VARCHAR",
        "extension": "VARCHAR",
        "mime_type": "VARCHAR",
        "size_bytes": "BIGINT",
        "sha256": "VARCHAR",
        "last_indexed_at": "VARCHAR",
        "viewer": "VARCHAR",
        "language": "VARCHAR",
        "content_origin": "VARCHAR",
        "content": "VARCHAR",
        "outline": "VARCHAR",
        "chunks": "VARCHAR",
    }
    with tempfile.TemporaryDirectory(prefix="pheasant-export-") as scratch:
        rows_path = Path(scratch) / "rows.jsonl"
        out_path = Path(scratch) / "out.parquet"
        written = 0
        with rows_path.open("w", encoding="utf-8") as handle:
            for record, _ in records:
                row = {
                    **{k: v for k, v in record.items() if k not in ("outline", "chunks")},
                    "outline": json.dumps(record["outline"], ensure_ascii=False),
                    "chunks": json.dumps(record["chunks"], ensure_ascii=False)
                    if include_chunks
                    else None,
                }
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
        spec = ", ".join(f"'{name}': '{kind}'" for name, kind in columns.items())
        # COPY takes no parameters; both paths are ours, inside a fresh
        # temporary directory, and quoted anyway.
        source_sql, target_sql = (_sql_string(rows_path), _sql_string(out_path))
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                f"COPY (SELECT * FROM read_json({source_sql}, format='newline_delimited', "
                f"columns={{{spec}}}, maximum_object_size=1073741824)) "
                f"TO {target_sql} (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        finally:
            connection.close()
        with out_path.open("rb") as built:
            shutil.copyfileobj(built, target)
    return written


def _sql_string(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"
