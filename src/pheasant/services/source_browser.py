"""Browsing a source as folders and files, and reading one file for a viewer.

``services.inventory`` answers questions about the knowledge base as a list
("which documents are indexed"), and ``services.inventory_detail`` describes
one source or one document. Neither answers the question a person asks of a
file collection: *what is in this folder*, and *show me this file*. Three
operations do:

* :func:`browse` — one directory level of a source: its sub-folders (each with
  the number of documents beneath it) and the files directly in it. With no
  source, the level above every source: the sources themselves, as folders.
  The breadcrumbs lead back up, so a caller can walk to the last layer and
  back again without keeping state of its own.
* :func:`read_document` — one document's content, classified for a viewer
  (``markdown``, ``code``, ``table``, ``pdf``, ``image`` …), with its outline
  and, given a cited passage's ``chunk_id``, the lines that passage spans.
* :func:`raw_document` — the bytes of a binary document (a PDF, an image, a
  recording) for a browser's native viewer. HTTP only: an agent reads a PDF
  through :func:`read_document`'s extracted text, and bytes in an MCP result
  are a payload nobody can read.

The tree is the directory structure of what was *indexed*, read from the
``artifacts`` rows rather than from the filesystem: it is the same on a
replica with no source mount, it never shows a file the index does not hold,
and it is filtered by the same scope and ACL rule as every inventory listing
(``inventory.ARTIFACT_SCOPE``, ``inventory.reader``) — a folder's count never
includes a document the caller could not open.

Content comes from the indexed file when this process can read it *and* it is
still the file that was indexed (its sha256 matches the artifact row), and
otherwise from the chunks, reassembled in order with the overlap between
neighbours removed. Either way a reader sees what the index holds, and the
answer says which (``content_origin``).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pheasant.ingestion.content_types import (
    AUDIO_EXTENSIONS,
    CODE_LANGUAGES,
    IMAGE_EXTENSIONS,
    MEMORY_ARTIFACT_TYPE,
)
from pheasant.services import ServiceContext
from pheasant.services.errors import DocumentNotFound, InvalidRequest
from pheasant.services.inventory import ARTIFACT_SCOPE, extension_of, reader

#: Files one call lists at a level. Folders are always listed whole: there are
#: few of them per level, and a folder that is paged away is a subtree nobody
#: can reach.
MAX_FILES = 500
#: Characters of content one read returns. Past this the text is cut and
#: ``truncated`` says so; a viewer that wants the rest has the raw bytes.
MAX_CONTENT_CHARS = 2_000_000
#: Bytes a raw read serves. A larger file is read through its extracted text.
MAX_RAW_BYTES = 100 * 1024 * 1024

#: Extension -> the viewer that renders it best. Anything absent is ``text``
#: (or ``code`` when the graph's code analysis knows the language).
VIEWERS: dict[str, str] = {
    ".md": "markdown",
    ".mdx": "markdown",
    ".markdown": "markdown",
    ".ipynb": "notebook",
    ".json": "json",
    ".jsonc": "json",
    ".json5": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".csv": "table",
    ".tsv": "table",
    ".html": "html",
    ".htm": "html",
    ".svg": "svg",
    ".pdf": "pdf",
    ".xlsx": "spreadsheet",
    ".pptx": "slides",
    ".docx": "document",
    ".doc": "document",
    ".rtf": "document",
    ".epub": "document",
    **{suffix: "image" for suffix in IMAGE_EXTENSIONS},
    **{suffix: "audio" for suffix in AUDIO_EXTENSIONS},
}
#: Viewers whose text is extracted from a binary format rather than decoded.
EXTRACTED_VIEWERS = frozenset({"pdf", "image", "audio", "spreadsheet", "slides", "document"})
#: Languages for source that is highlighted but not analysed for the graph.
EXTRA_LANGUAGES: dict[str, str] = {
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".properties": "properties",
    ".sql": "sql",
    ".css": "css",
    ".scss": "scss",
    ".sass": "scss",
    ".less": "less",
    ".xml": "xml",
    ".graphql": "graphql",
    ".gql": "graphql",
    ".tf": "hcl",
    ".tfvars": "hcl",
    ".hcl": "hcl",
    ".prisma": "prisma",
    ".r": "r",
    ".jl": "julia",
    ".pl": "perl",
    ".pm": "perl",
    ".ps1": "powershell",
    ".bat": "dos",
    ".cmd": "dos",
    ".fish": "shell",
    ".cmake": "cmake",
    ".mk": "makefile",
    ".patch": "diff",
    ".diff": "diff",
    ".fs": "fsharp",
    ".vb": "vbnet",
    ".ml": "ocaml",
    ".mli": "ocaml",
    ".elm": "elm",
    ".nim": "nim",
    ".sol": "solidity",
    ".pyx": "python",
    ".tex": "latex",
}
#: Whole file names that are source in a known language.
NAMED_LANGUAGES: dict[str, str] = {
    "dockerfile": "dockerfile",
    "containerfile": "dockerfile",
    "makefile": "makefile",
    "gnumakefile": "makefile",
    "rakefile": "ruby",
    "gemfile": "ruby",
    "podfile": "ruby",
    "vagrantfile": "ruby",
    "jenkinsfile": "groovy",
    "justfile": "makefile",
}
#: Binary formats a browser renders natively, and the type it is served as.
RAW_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
}


@dataclass(frozen=True)
class BrowseRequest:
    """Which level of which source to list."""

    knowledge_base: str | None = None
    #: ``None`` lists the sources themselves.
    source_name: str | None = None
    #: A directory inside the source, ``""`` for its root. Matched without
    #: regard to case; the answer spells it as the index does.
    path: str = ""
    limit: int = 200
    offset: int = 0
    principal: str | None = None
    principal_groups: list[str] | None = None


@dataclass(frozen=True)
class ReadRequest:
    """Which document to read, named as ``describe_document`` names one."""

    path: str
    knowledge_base: str | None = None
    source_name: str | None = None
    #: A cited passage; the answer then says which lines it spans.
    chunk_id: str | None = None
    principal: str | None = None
    principal_groups: list[str] | None = None


def browse(context: ServiceContext, request: BrowseRequest) -> dict[str, Any]:
    """One level of the tree: folders with their counts, then files, paged."""

    kb_id = context.knowledge_base(request.knowledge_base)
    limit = max(1, min(int(request.limit), MAX_FILES))
    offset = max(0, int(request.offset))
    if not request.source_name:
        if clean_directory(request.path):
            raise InvalidRequest("path needs a source_name: a folder lives inside one source")
        return _sources_level(context, kb_id, request)
    from pheasant.services.inventory_detail import _registered

    source = str(_registered(context, request.source_name)["name"])
    wanted = clean_directory(request.path)
    admit = reader(context, request.principal, request.principal_groups)
    acl = ", a.acl" if admit is not None else ""
    where = f"{ARTIFACT_SCOPE} AND a.source_id = ?"
    params: list[Any] = [MEMORY_ARTIFACT_TYPE, source]
    if wanted:
        where += " AND LOWER(a.relative_path) LIKE ? ESCAPE '\\'"
        params.append(f"{_escape_like(wanted.lower())}/%")
    # Only the paths, for the grouping: a level is decided by the next path
    # segment, which neither dialect spells the same way in SQL, and a path
    # column is cheap to carry even for a large source.
    rows = context.state.rows(
        f"SELECT a.id, a.relative_path{acl} FROM artifacts a WHERE {where}", tuple(params)
    )
    if admit is not None:
        rows = admit(rows)
    if wanted and not rows:
        raise InvalidRequest(f"No folder {wanted!r} in source {source!r}")

    actual = _actual_prefix(wanted, [str(r["relative_path"] or "") for r in rows])
    folders: Counter[str] = Counter()
    here: list[tuple[str, str]] = []
    depth = len(actual.split("/")) if actual else 0
    for row in rows:
        rest = _parts(row["relative_path"])[depth:]
        if len(rest) > 1:
            folders[rest[0]] += 1
        elif rest:
            here.append((rest[0].lower(), str(row["id"])))
    # Name order, so a page is stable from one call to the next.
    here.sort()
    files = _file_rows(context, [artifact_id for _, artifact_id in here[offset : offset + limit]])
    directories = [
        {"name": name, "path": _join(actual, name), "documents": count}
        for name, count in sorted(folders.items(), key=lambda item: item[0].lower())
    ]
    returned = len(files)
    return {
        "knowledge_base": kb_id,
        "source": source,
        "path": actual,
        "parent": _parent(actual),
        "breadcrumbs": _breadcrumbs(source, actual),
        "directories": directories,
        "files": files,
        "totals": {
            "directories": len(directories),
            "files": len(here),
            "documents": len(rows),
        },
        "page": {
            "offset": offset,
            "limit": limit,
            "returned": returned,
            "has_more": offset + returned < len(here),
        },
    }


def read_document(context: ServiceContext, request: ReadRequest) -> dict[str, Any]:
    """One document's content, classified for a viewer.

    ``document`` is the row (as ``list_documents`` spells it); ``viewer`` and
    ``language`` say how to render it; ``content`` is its text (or ``None``
    for an image with no caption) and ``content_origin`` where that came from
    (``file``, ``extracted`` for a binary format's indexed text, or
    ``chunks`` when a text file was not readable here as indexed);
    ``outline`` its section headings with the
    line each starts on; ``focus`` the line span of ``chunk_id``, when given;
    ``raw`` whether :func:`raw_document` can serve the original bytes here.
    """

    kb_id = context.knowledge_base(request.knowledge_base)
    row = _resolve(context, request.path, request.source_name, request)
    artifact_id = str(row["id"])
    relative = str(row["relative_path"] or "")
    suffix = PurePosixPath(relative).suffix.lower()
    viewer, language = classify(relative)

    content: str | None = None
    origin: str | None = None
    if viewer not in EXTRACTED_VIEWERS:
        content = _indexed_text(row)
        origin = "file" if content is not None else None
    elif viewer == "spreadsheet":
        # The indexed text collapses a row's tabs into spaces, so its columns
        # cannot be told apart. The same deterministic extraction over the
        # original workbook keeps them, when the workbook is still the one
        # that was indexed.
        content = _spreadsheet_text(row)
        origin = "file" if content is not None else None
    if content is None:
        content = _chunk_text(context, artifact_id)
        # For a binary format the extracted text *is* its text; for a text
        # file it means the file was not readable here, or changed.
        origin = ("extracted" if viewer in EXTRACTED_VIEWERS else "chunks") if content else None
        if viewer == "notebook":
            # Chunks hold a notebook's cells as text, not its JSON.
            viewer, language = "code", "python"
    truncated = bool(content and len(content) > MAX_CONTENT_CHARS)
    if truncated and content is not None:
        content = content[:MAX_CONTENT_CHARS]

    raw_type = RAW_TYPES.get(suffix)
    return {
        "knowledge_base": kb_id,
        "document": {
            "id": artifact_id,
            "source": row["source_id"],
            "path": relative,
            "name": PurePosixPath(relative).name or relative,
            "type": row["type"],
            "extension": extension_of(relative),
            "size_bytes": row["size_bytes"],
            "last_indexed_at": row["last_indexed_at"],
            "sha256": row["sha256"],
            "mime_type": row["mime_type"],
        },
        "breadcrumbs": _breadcrumbs(str(row["source_id"]), str(PurePosixPath(relative).parent)),
        "viewer": viewer,
        "language": language,
        "content": content,
        "content_origin": origin,
        "truncated": truncated,
        "outline": _outline(context, artifact_id),
        "focus": _focus(context, artifact_id, request.chunk_id),
        "raw": {
            "available": bool(raw_type) and _raw_available(row),
            "mime_type": raw_type,
        },
    }


def raw_document(context: ServiceContext, request: ReadRequest) -> dict[str, Any]:
    """``{path, content, mime_type, sha256, size_bytes, name}`` for a binary document.

    The path comes from the region's own artifact row, never from the caller,
    and is served only while it still holds the bytes that were indexed: a
    file edited since would show something no chunk, citation or caption
    describes. A file is handed over as ``path`` (``content`` is ``None``) so
    it streams; a member of a ZIP archive has no file of its own, so its
    inflated bytes are ``content`` and ``path`` is ``None``.
    """

    context.knowledge_base(request.knowledge_base)
    row = _resolve(context, request.path, request.source_name, request)
    relative = str(row["relative_path"] or "")
    mime = RAW_TYPES.get(PurePosixPath(relative).suffix.lower())
    if mime is None:
        raise InvalidRequest(f"{relative} is read as text; use read_document")
    original = _original(row) if _raw_available(row) else None
    content: bytes | None = None
    if original is not None and original.member is not None:
        content = _indexed_bytes(row, limit=MAX_RAW_BYTES)
        readable = content is not None
    else:
        readable = original is not None and _sha256_file(original.path) == row["sha256"]
    if original is None or not readable:
        raise DocumentNotFound(f"{relative} (its original file is not readable here)")
    return {
        "path": original.path if content is None else None,
        "content": content,
        "name": PurePosixPath(relative).name,
        "mime_type": mime,
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
    }


def classify(relative_path: str) -> tuple[str, str | None]:
    """``(viewer, language)`` for a path: how a client renders it best."""

    pure = PurePosixPath(relative_path)
    suffix = pure.suffix.lower()
    named = NAMED_LANGUAGES.get(pure.name.lower())
    if named:
        return "code", named
    viewer = VIEWERS.get(suffix)
    if viewer == "yaml":
        return viewer, "yaml"
    if viewer == "json":
        return viewer, "json"
    if viewer == "html":
        return viewer, "html"
    if viewer:
        return viewer, None
    language = CODE_LANGUAGES.get(suffix) or EXTRA_LANGUAGES.get(suffix)
    if language:
        return "code", language
    return "text", None


def clean_directory(path: Any) -> str:
    """``"/notes/guide/"`` → ``"notes/guide"``; ``"."`` and ``".."`` segments go."""

    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p and p not in {".", ".."}]
    return "/".join(parts)


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


def _sources_level(context: ServiceContext, kb_id: str, request: BrowseRequest) -> dict[str, Any]:
    """The level above every source: each source is a folder."""

    admit = reader(context, request.principal, request.principal_groups)
    acl = ", a.acl" if admit is not None else ""
    rows = context.state.rows(
        f"SELECT a.source_id{acl} FROM artifacts a WHERE {ARTIFACT_SCOPE}",
        (MEMORY_ARTIFACT_TYPE,),
    )
    if admit is not None:
        rows = admit(rows)
    counts = Counter(str(row["source_id"]) for row in rows)
    registered = {
        str(row["name"]): row
        for row in context.state.rows(
            "SELECT s.name, s.type FROM sources s WHERE NOT EXISTS "
            "(SELECT 1 FROM removed_sources r WHERE r.source_id = s.id)"
        )
        if not str(row["name"]).startswith("__")
    }
    directories = [
        {
            "name": name,
            "path": "",
            "source": name,
            "source_type": registered[name]["type"] if name in registered else None,
            "documents": counts.get(name, 0),
        }
        for name in sorted(set(registered) | set(counts), key=str.lower)
        if name in counts or admit is None
    ]
    return {
        "knowledge_base": kb_id,
        "source": None,
        "path": "",
        "parent": None,
        "breadcrumbs": [],
        "directories": directories,
        "files": [],
        "totals": {
            "directories": len(directories),
            "files": 0,
            "documents": sum(counts.values()),
        },
        "page": {"offset": 0, "limit": 0, "returned": 0, "has_more": False},
    }


def _actual_prefix(wanted: str, paths: list[str]) -> str:
    """``wanted`` spelled as the index spells it (the match was case-blind)."""

    if not wanted or not paths:
        return wanted
    depth = len(wanted.split("/"))
    return "/".join(_parts(paths[0])[:depth])


def _file_rows(context: ServiceContext, ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    marks = ", ".join("?" for _ in ids)
    rows = {
        str(row["id"]): row
        for row in context.state.rows(
            "SELECT id, source_id, relative_path, type, size_bytes, last_indexed_at "
            f"FROM artifacts WHERE id IN ({marks})",
            tuple(ids),
        )
    }
    out = []
    for artifact_id in ids:
        row = rows.get(artifact_id)
        if row is None:
            continue
        relative = str(row["relative_path"] or "")
        viewer, language = classify(relative)
        out.append(
            {
                "id": artifact_id,
                "name": PurePosixPath(relative).name or relative,
                "path": relative,
                "type": row["type"],
                "extension": extension_of(relative),
                "size_bytes": row["size_bytes"],
                "last_indexed_at": row["last_indexed_at"],
                "viewer": viewer,
                "language": language,
            }
        )
    return out


def _breadcrumbs(source: str, directory: str) -> list[dict[str, Any]]:
    """Every level from the source down to ``directory``, each one browsable."""

    crumbs = [{"name": source, "source": source, "path": ""}]
    walked: list[str] = []
    for part in _parts(directory):
        walked.append(part)
        crumbs.append({"name": part, "source": source, "path": "/".join(walked)})
    return crumbs


def _parts(path: Any) -> list[str]:
    return [p for p in str(path or "").replace("\\", "/").split("/") if p and p != "."]


def _join(prefix: str, name: str) -> str:
    return f"{prefix}/{name}" if prefix else name


def _parent(path: str) -> str | None:
    if not path:
        return None
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _batches(ids: list[str], size: int = 500) -> list[list[str]]:
    return [ids[i : i + size] for i in range(0, len(ids), size)]


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------------------
# One document
# ---------------------------------------------------------------------------


def _resolve(
    context: ServiceContext, path: str, source_name: str | None, request: ReadRequest
) -> Any:
    """The one readable document ``path`` names, or a refusal that says why.

    Spelled as ``describe_document`` spells it (``inventory_detail._find``),
    so a path that one tool accepts the other accepts too. Several matches are
    refused rather than guessed: a viewer opening the wrong file is worse than
    one asking which.
    """

    from pheasant.services.inventory_detail import _clean_path, _find, _registered

    wanted = _clean_path(path)
    if not wanted:
        raise InvalidRequest("path must name a document")
    if source_name:
        _registered(context, source_name)
    found = _find(context, wanted, source_name, request.principal, request.principal_groups)
    if not found:
        raise DocumentNotFound(path)
    if len(found) > 1:
        names = ", ".join(f"{r['source_id']}/{r['relative_path']}" for r in found[:5])
        more = f" and {len(found) - 5} more" if len(found) > 5 else ""
        raise InvalidRequest(f"{path!r} names {len(found)} documents ({names}{more}); be exact")
    row = found[0]
    full = context.state.rows(
        "SELECT id, source_id, relative_path, type, path, size_bytes, sha256, mime_type, "
        "last_indexed_at FROM artifacts WHERE id=? LIMIT 1",
        (str(row["id"]),),
    )
    return full[0]


def _indexed_text(row: Any) -> str | None:
    """The indexed file's text, if this process can read the bytes that were indexed."""

    content = _indexed_bytes(row, limit=None)
    return None if content is None else content.decode("utf-8", errors="replace")


def _spreadsheet_text(row: Any) -> str | None:
    content = _indexed_bytes(row, limit=MAX_RAW_BYTES)
    if content is None:
        return None
    from pheasant.ingestion.office import extract_xlsx_text

    return extract_xlsx_text(content) or None


@dataclass(frozen=True)
class _Original:
    """Where an indexed document's bytes are on this process: a file, or one
    member of a ZIP archive the folder connector indexed without extracting.

    A member's artifact ``path`` is ``file:///…/a.zip#member``, which is no
    file at all; reading it as one is what left every PDF, image and recording
    from an uploaded archive with no raw bytes and its text read from chunks.
    """

    path: Path
    #: The member's name as its artifact carries it; ``None`` for a file.
    member: str | None = None

    def size(self) -> int | None:
        if self.member is None:
            try:
                return self.path.stat().st_size
            except OSError:
                return None
        info = self._info()
        return None if info is None else info.file_size

    def read(self, limit: int | None) -> bytes | None:
        """The bytes, or ``None`` when unreadable here or larger than ``limit``."""

        size = self.size()
        if size is None or (limit is not None and size > limit):
            return None
        if self.member is None:
            try:
                return self.path.read_bytes()
            except OSError:
                return None
        from pheasant.sync.zip_archive import ArchiveError, read_member

        info = self._info()
        if info is None:
            return None
        try:
            return read_member(self.path, info.filename, info.file_size)
        except ArchiveError:
            return None

    def _info(self) -> Any:
        from pheasant.sync.zip_archive import find_member

        assert self.member is not None
        return find_member(self.path, self.member)


def _original(row: Any) -> _Original | None:
    raw = str(row["path"] or "")
    if not raw:
        return None
    if raw.startswith("file:") and "#" in raw:
        from urllib.parse import unquote, urlsplit
        from urllib.request import url2pathname

        parts = urlsplit(raw)
        found = _Original(Path(url2pathname(parts.path)), unquote(parts.fragment))
    else:
        found = _Original(Path(raw))
    try:
        return found if found.path.is_file() else None
    except OSError:
        return None


def _indexed_bytes(row: Any, limit: int | None) -> bytes | None:
    """The original bytes, only while they are still the ones that were indexed."""

    original = _original(row)
    content = None if original is None else original.read(limit)
    if content is None or hashlib.sha256(content).hexdigest() != row["sha256"]:
        return None
    return content


def _raw_available(row: Any) -> bool:
    """Whether :func:`raw_document` can serve this row here, without reading it."""

    original = _original(row)
    size = None if original is None else original.size()
    if size is None or size > MAX_RAW_BYTES:
        return False
    return row["size_bytes"] is None or size == row["size_bytes"]


def _sha256_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _chunk_text(context: ServiceContext, artifact_id: str) -> str:
    """The chunks, in order, with the lines neighbouring chunks share removed.

    A chunk that overlaps its predecessor repeats the predecessor's last lines;
    joined naively, a reader sees those lines twice. When both chunks carry a
    line span that agrees with their text, the repeat is dropped; when they do
    not, the chunk is kept whole, because guessing wrong would delete text.
    """

    rows = context.state.rows(
        "SELECT text, start_line, end_line FROM chunks WHERE artifact_id=? ORDER BY chunk_index",
        (artifact_id,),
    )
    out = ""
    last_end: int | None = None
    for row in rows:
        text = str(row["text"] or "")
        start, end = row["start_line"], row["end_line"]
        lines = text.split("\n")
        spans = start is not None and end is not None and len(lines) == int(end) - int(start) + 1
        if not spans:
            out += ("\n\n" if out else "") + text
            last_end = None
            continue
        start, end = int(start), int(end)
        if last_end is not None and start <= last_end:
            lines = lines[last_end - start + 1 :]
            if lines:
                out += "\n" + "\n".join(lines)
            last_end = max(last_end, end)
            continue
        # Contiguous lines continue the text; a gap is a paragraph break.
        joiner = "\n" if last_end is not None and start == last_end + 1 else "\n\n"
        out += (joiner if out else "") + text
        last_end = end
    return out


def _outline(context: ServiceContext, artifact_id: str) -> list[dict[str, Any]]:
    """Section headings in reading order, each with the line it starts on."""

    rows = context.state.rows(
        "SELECT heading_path, MIN(start_line) AS line, MIN(chunk_index) AS first FROM chunks "
        "WHERE artifact_id=? AND heading_path IS NOT NULL AND heading_path <> '' "
        "GROUP BY heading_path ORDER BY first",
        (artifact_id,),
    )
    out = []
    for row in rows[:500]:
        crumbs = [c.strip() for c in str(row["heading_path"]).split(">") if c.strip()]
        if not crumbs:
            continue
        out.append(
            {
                "title": crumbs[-1],
                "path": str(row["heading_path"]),
                "level": len(crumbs),
                "line": row["line"],
            }
        )
    return out


def _focus(context: ServiceContext, artifact_id: str, chunk_id: str | None) -> dict | None:
    """The line span of a cited passage, when it is a passage of this document."""

    if not chunk_id:
        return None
    rows = context.state.rows(
        "SELECT artifact_id, chunk_index, start_line, end_line, heading_path, text "
        "FROM chunks WHERE id=? LIMIT 1",
        (chunk_id,),
    )
    if not rows or str(rows[0]["artifact_id"]) != artifact_id:
        return None
    row = rows[0]
    return {
        "chunk_id": chunk_id,
        "chunk_index": row["chunk_index"],
        "start_line": row["start_line"],
        "end_line": row["end_line"],
        "heading_path": row["heading_path"],
        "excerpt": str(row["text"] or "")[:400],
    }
