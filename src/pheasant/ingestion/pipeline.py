from __future__ import annotations

import fnmatch
import hashlib
import logging
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pheasant.config.schema import SourceConfig
from pheasant.ingestion.chunking import TextChunk, chunk_text
from pheasant.ingestion.content_types import (
    AUDIO_EXTENSIONS,
    DOCUMENT_EXTENSIONS,
    IMAGE_EXTENSIONS,
    TEXT_EXTENSIONS,
    artifact_type,
)
from pheasant.ingestion.extractor import EXTRACT_SIDECAR_SUFFIX, HTML_EXTENSIONS
from pheasant.ingestion.taxonomy import (
    SectionHeading,
    heading_path_for_line,
    headings_for_source,
)

if TYPE_CHECKING:
    from pheasant.ingestion.captioner import Captioner
    from pheasant.ingestion.extractor import DocumentExtractor
    from pheasant.ingestion.transcriber import Transcriber
    from pheasant.sync.connectors import ConnectorItem, ConnectorPayload

logger = logging.getLogger(__name__)

CAPTION_SIDECAR_SUFFIX = ".caption.txt"
TRANSCRIPT_SIDECAR_SUFFIX = ".transcript.txt"


@dataclass(frozen=True)
class ParsedArtifact:
    id: str
    source_id: str
    path: Path | str
    relative_path: str
    type: str
    mime_type: str | None
    size_bytes: int
    sha256: str
    mtime: str
    git_branch: str | None
    git_commit: str | None
    chunks: list[TextChunk]
    #: Structural outline detected for this artifact, in document order.
    #: Empty unless the source enables `taxonomy` — see
    #: `pheasant.ingestion.taxonomy`. The graph builder turns these into
    #: `heading` nodes; `chunks[*].heading_path` is derived from them.
    headings: list[SectionHeading] = field(default_factory=list)


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _match_any(relative: str, patterns: Iterable[str]) -> bool:
    return any(
        fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch("/" + relative, pattern)
        for pattern in patterns
    )


def within_max_depth(relative: str, max_depth: int | None) -> bool:
    if max_depth is None:
        return True
    depth = max(0, len(Path(relative).parts) - 1)
    return depth <= max(0, max_depth)


def discover_files(source: SourceConfig) -> list[Path]:
    """Files a source would index, via the shared pruning walk.

    Delegates to :func:`pheasant.ingestion.walk.walk_source` so there is one
    traversal implementation rather than two that drift — this used to
    ``rglob("*")`` the whole tree and filter afterwards, which meant exclude
    patterns never pruned the walk.
    """
    from pheasant.ingestion.walk import WalkBudget, walk_source

    report = walk_source(
        source.path,
        include=source.include,
        exclude=source.exclude,
        max_depth=source.max_depth,
        budget=WalkBudget.from_settings(source.limits),
        follow_symlinks=bool(getattr(source.limits, "follow_symlinks", False)),
    )
    return report.files


def git_state(root: Path) -> tuple[str | None, str | None, bool]:
    cwd = root if root.is_dir() else root.parent
    try:
        branch = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        return branch, commit, False
    except Exception:
        return None, None, False


def read_text(path: Path, extractor: DocumentExtractor | None = None) -> str:
    """Read a file's indexable text.

    ``.pdf``/``.docx`` (and, when the extractor is configured for it, HTML)
    are binary or markup containers whose text has to be extracted rather
    than decoded. With no extractor these return ``""`` — the long-standing
    behavior, kept so a region with no document source is unchanged.
    """
    suffix = path.suffix.lower()
    if suffix in DOCUMENT_EXTENSIONS or (extractor is not None and suffix in HTML_EXTENSIONS):
        if extractor is None:
            return ""
        return extract_to_text(extractor, path.read_bytes(), path.name, _extract_sidecar(path))
    return path.read_text(encoding="utf-8", errors="ignore")


def read_text_bytes(
    content: bytes, relative_path: str, extractor: DocumentExtractor | None = None
) -> str:
    """Decode connector payload bytes to indexable text (see :func:`read_text`)."""
    suffix = Path(relative_path).suffix.lower()
    if suffix in DOCUMENT_EXTENSIONS or (extractor is not None and suffix in HTML_EXTENSIONS):
        if extractor is None:
            return ""
        return extract_to_text(extractor, content, relative_path, None)
    return content.decode("utf-8", errors="ignore")


def _extract_sidecar(path: Path) -> bytes | None:
    return _sidecar_for_path(path, EXTRACT_SIDECAR_SUFFIX)


def extract_to_text(
    extractor: DocumentExtractor | None,
    content: bytes,
    relative_path: str,
    sidecar: bytes | None,
) -> str:
    """Extract document bytes into indexable text.

    Mirrors :func:`caption_to_text` / :func:`transcribe_to_text`, including the
    duck-typed ``sidecar=`` tolerance, so all three modality handlers stay in
    lock-step. Extraction never raises into the sync: a document that cannot
    be read contributes no text, exactly as before this path existed.
    """

    if extractor is None:
        return ""
    try:
        try:
            return extractor.extract(content, relative_path, sidecar=sidecar)  # type: ignore[call-arg]
        except TypeError:
            # An extractor without the optional ``sidecar`` kwarg (duck-typed).
            return extractor.extract(content, relative_path)
    except Exception as exc:
        # A malformed document must never abort a whole sync — it just
        # contributes no text, which is the pre-extraction behavior anyway.
        logger.warning("document extraction failed for %s: %s", relative_path, exc)
        return ""


def _chunks_and_headings(
    source: SourceConfig, text: str
) -> tuple[list[TextChunk], list[SectionHeading]]:
    """Detect headings and cut chunks, minus any agent-memory frontmatter.

    Both parse entry points route through here so normalization cannot be applied
    on one and forgotten on the other. That is not hypothetical: the first
    version of Step 33.5 patched only ``parse_file``, while the sync engine
    goes through ``parse_connector_payload`` — so record frontmatter went on
    being indexed as prose everywhere it actually mattered.

    Line numbers are corrected back to absolute afterwards, because
    ``chunk_text`` derives them from offsets into the text it is handed and
    strips each slice; without the correction every memory chunk would claim
    to start at line 1.

    PostgreSQL deliberately rejects NUL in text values. A repository can still
    contain one in a file whose extension looks textual (binary fixtures are a
    common example), so remove it before chunking rather than failing the whole
    source during persistence. SQLite accepts NUL, but applying this at the
    ingestion boundary keeps both backends' indexed corpus identical.
    """

    text = text.replace("\x00", "")
    text, line_offset = _strip_memory_frontmatter(source, text)
    headings = headings_for_source(source, text)
    chunks = chunks_for_source(source, text, headings)
    if not line_offset:
        return chunks, headings
    chunks = [
        replace(
            chunk,
            start_line=chunk.start_line + line_offset,
            end_line=chunk.end_line + line_offset,
        )
        for chunk in chunks
    ]
    headings = [replace(heading, line=heading.line + line_offset) for heading in headings]
    return chunks, headings


def _strip_memory_frontmatter(source: SourceConfig, text: str) -> tuple[str, int]:
    """Drop an agent-memory record's frontmatter before it is indexed.

    Returns ``(text, lines_removed)``; a no-op for every other source type, so
    no existing index shifts. Scoped this narrowly on purpose: stripping YAML
    frontmatter from *all* Markdown would change chunk boundaries — and
    therefore ``chunk:{...}:sha256={text_hash}`` ids — for every notes and
    docs folder already indexed, which is a re-index nobody asked for.

    For memory records it is a straight win. The frontmatter is machine-written
    bookkeeping (`record_id`, `schema_version`, ISO timestamps) that no one
    would ever search for, and Step 33.5 puts every one of those fields in the
    `memory_records` table where it can actually be queried. Leaving it in the
    text only diluted BM25 with high-cardinality tokens.
    """
    if getattr(source.type, "value", source.type) != "memory":
        return text, 0
    from pheasant.memory.store import strip_frontmatter

    return strip_frontmatter(text)


def chunks_for_source(
    source: SourceConfig,
    text: str,
    headings: list[SectionHeading] | None = None,
) -> list[TextChunk]:
    """Chunk ``text``, labelling each chunk with the section it falls inside.

    ``heading_path`` has existed on :class:`TextChunk`, in the ``chunks``
    table and in ``chunks_fts`` (at BM25 weight 2.0) since long before this
    function could produce one — it was ``NULL`` for every chunk ever
    indexed. Passing ``headings`` fills it.

    With ``taxonomy.split_on_sections`` (the default once taxonomy is on),
    chunks are cut at section boundaries so one chunk is one section; see
    :func:`_section_aligned_chunks` for why that matters. Otherwise the
    original boundaries are kept and each chunk is merely *labelled* with the
    section its first line falls in.
    """
    if not text:
        return []
    if headings and _split_on_sections(source) and source.chunking.enabled:
        return _section_aligned_chunks(source, text, headings)
    if not source.chunking.enabled:
        lines = text.splitlines()
        chunks = [
            TextChunk(
                index=0,
                text=text,
                start_line=1,
                end_line=len(lines) or 1,
            )
        ]
    else:
        chunks = chunk_text(text, source.chunking.max_chars, source.chunking.overlap_chars)
    if not headings:
        return chunks
    return [
        replace(chunk, heading_path=heading_path_for_line(headings, chunk.start_line))
        for chunk in chunks
    ]


def _split_on_sections(source: SourceConfig) -> bool:
    return bool(getattr(getattr(source, "taxonomy", None), "split_on_sections", False))


def _section_aligned_chunks(
    source: SourceConfig,
    text: str,
    headings: list[SectionHeading],
) -> list[TextChunk]:
    """One chunk per section, subdivided only when a section is oversized.

    This is what turns the taxonomy from a label into a retrieval unit. With
    the default 4000-char chunking, a whole contract lands in a single chunk,
    so labelling alone gives it one ``heading_path`` (its first heading) and
    tells a searcher nothing; worse, a chunk that straddles three sections is
    labelled with only the first, which is misleading rather than merely
    coarse. Cutting on the boundaries the document itself declares makes
    "what does § 12.3 say" retrieve § 12.3.

    ``chunking.max_chars`` is still respected: a section longer than the limit
    is split into several chunks that all share its ``heading_path``, so one
    enormous section cannot produce one enormous chunk.

    Line numbers stay absolute (offset back onto the original text) so
    provenance keeps pointing at the real file, and chunk indices stay a
    single ascending run so ``chunk:{...}:chunk={index}`` IDs remain stable.
    """
    lines = text.splitlines()
    total = len(lines)
    spans: list[tuple[int, int, str | None]] = []
    first = headings[0].line
    if first > 1:
        # Front matter before the first heading is still content.
        spans.append((1, first, None))
    for position, heading in enumerate(headings):
        end = headings[position + 1].line if position + 1 < len(headings) else total + 1
        if end > heading.line:
            spans.append((heading.line, end, heading.path))

    out: list[TextChunk] = []
    index = 0
    for start, end, path in spans:
        segment = "\n".join(lines[start - 1 : end - 1])
        if not segment.strip():
            continue
        for chunk in chunk_text(segment, source.chunking.max_chars, source.chunking.overlap_chars):
            out.append(
                replace(
                    chunk,
                    index=index,
                    start_line=start - 1 + chunk.start_line,
                    end_line=start - 1 + chunk.end_line,
                    heading_path=path,
                )
            )
            index += 1
    return out


def _sidecar_for_path(path: Path, suffix: str = CAPTION_SIDECAR_SUFFIX) -> bytes | None:
    sidecar = path.with_name(path.name + suffix)
    try:
        return sidecar.read_bytes()
    except OSError:
        return None


def caption_to_text(
    captioner: Captioner | None,
    content: bytes,
    relative_path: str,
    sidecar: bytes | None,
) -> str:
    """Caption image bytes into indexable text (architecture §8).

    With no captioner configured we fall back to the file name so an
    image-bearing source still produces *some* deterministic indexable text
    rather than an empty (skipped) artifact.
    """

    if captioner is None:
        stem = Path(relative_path).stem.replace("_", " ").replace("-", " ").strip()
        return f"Image {stem}." if stem else "Image."
    try:
        return captioner.caption(content, relative_path, sidecar=sidecar)  # type: ignore[call-arg]
    except TypeError:
        # A captioner without the optional ``sidecar`` kwarg (duck-typed).
        return captioner.caption(content, relative_path)


def transcribe_to_text(
    transcriber: Transcriber | None,
    content: bytes,
    relative_path: str,
    sidecar: bytes | None,
) -> str:
    """Transcribe audio bytes into indexable text (architecture §8).

    The audio twin of :func:`caption_to_text`. With no transcriber configured
    we fall back to the file name so an audio-bearing source still produces
    *some* deterministic indexable text rather than an empty (skipped) artifact.
    """

    if transcriber is None:
        stem = Path(relative_path).stem.replace("_", " ").replace("-", " ").strip()
        return f"Audio {stem}." if stem else "Audio."
    try:
        return transcriber.transcribe(content, relative_path, sidecar=sidecar)  # type: ignore[call-arg]
    except TypeError:
        # A transcriber without the optional ``sidecar`` kwarg (duck-typed).
        return transcriber.transcribe(content, relative_path)


def parse_file(
    source: SourceConfig,
    path: Path,
    git_metadata: tuple[str | None, str | None, bool] | None = None,
    captioner: Captioner | None = None,
    transcriber: Transcriber | None = None,
    extractor: DocumentExtractor | None = None,
) -> ParsedArtifact | None:
    suffix = path.suffix.lower()
    if suffix not in TEXT_EXTENSIONS | DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS:
        return None
    root = source.path if source.path.is_dir() else source.path.parent
    relative = path.relative_to(root).as_posix()
    digest = sha256_file(path)
    branch, commit, _dirty = (
        git_metadata
        if git_metadata is not None
        else git_state(root)
        if source.type.value == "repository"
        else (None, None, False)
    )
    if suffix in IMAGE_EXTENSIONS:
        text = caption_to_text(captioner, path.read_bytes(), relative, _sidecar_for_path(path))
    elif suffix in AUDIO_EXTENSIONS:
        text = transcribe_to_text(
            transcriber,
            path.read_bytes(),
            relative,
            _sidecar_for_path(path, TRANSCRIPT_SIDECAR_SUFFIX),
        )
    else:
        text = read_text(path, extractor)
    chunks, headings = _chunks_and_headings(source, text)
    stat = path.stat()
    artifact_id = f"file:{source.name}:{relative}:branch={branch or 'none'}"
    return ParsedArtifact(
        id=artifact_id,
        source_id=source.name,
        path=path,
        relative_path=relative,
        type=artifact_type(path, getattr(source.type, "value", source.type)),
        mime_type=None,
        size_bytes=stat.st_size,
        sha256=digest,
        mtime=datetime.fromtimestamp(stat.st_mtime, UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        git_branch=branch,
        git_commit=commit,
        chunks=chunks,
        headings=headings,
    )


def parse_connector_payload(
    source: SourceConfig,
    item: ConnectorItem,
    payload: ConnectorPayload,
    git_metadata: tuple[str | None, str | None, bool] | None = None,
    captioner: Captioner | None = None,
    transcriber: Transcriber | None = None,
    extractor: DocumentExtractor | None = None,
) -> ParsedArtifact | None:
    suffix = Path(item.relative_path).suffix.lower()
    mime_type = payload.mime_type or item.mime_type
    is_image = suffix in IMAGE_EXTENSIONS
    is_audio = suffix in AUDIO_EXTENSIONS
    if (
        suffix not in TEXT_EXTENSIONS | DOCUMENT_EXTENSIONS
        and not is_image
        and not is_audio
        and not _is_text_like(mime_type)
    ):
        return None
    branch, commit, _dirty = (
        git_metadata
        if git_metadata is not None
        else git_state(source.path)
        if source.type.value == "repository"
        else (None, None, False)
    )
    content_hash = payload.sha256 or item.sha256 or sha256_bytes(payload.content)
    if is_image:
        text = caption_to_text(
            captioner,
            payload.content,
            item.relative_path,
            _sidecar_for_payload(payload),
        )
    elif is_audio:
        text = transcribe_to_text(
            transcriber,
            payload.content,
            item.relative_path,
            _sidecar_for_payload(payload, TRANSCRIPT_SIDECAR_SUFFIX),
        )
    elif suffix in DOCUMENT_EXTENSIONS or (extractor is not None and suffix in HTML_EXTENSIONS):
        # Connector-backed documents get the same sidecar courtesy as local
        # ones when the payload carries a filesystem path.
        text = extract_to_text(
            extractor,
            payload.content,
            item.relative_path,
            _sidecar_for_payload(payload, EXTRACT_SIDECAR_SUFFIX),
        )
    elif extractor is not None and _is_html(mime_type):
        # A page served as HTML from a URL with no extension (``/blog/post``)
        # has a ``.txt`` relative path - the artifact id is built from it, so
        # it stays - and the suffix alone would index its raw markup, script
        # and style bodies included. The extractor picks its reader by
        # suffix, so it is told the format the server declared.
        text = extract_to_text(
            extractor,
            payload.content,
            f"{item.relative_path}.html",
            _sidecar_for_payload(payload, EXTRACT_SIDECAR_SUFFIX),
        )
    else:
        text = read_text_bytes(payload.content, item.relative_path, extractor)
    chunks, headings = _chunks_and_headings(source, text)
    artifact_id = f"file:{source.name}:{item.relative_path}:branch={branch or 'none'}"
    return ParsedArtifact(
        id=artifact_id,
        source_id=source.name,
        path=payload.metadata.get("path") or item.uri,
        relative_path=item.relative_path,
        type=artifact_type(Path(item.relative_path), getattr(source.type, "value", source.type)),
        mime_type=mime_type,
        size_bytes=payload.size_bytes or item.size_bytes or len(payload.content),
        sha256=content_hash,
        mtime=payload.mtime or item.mtime or utc_now(),
        git_branch=branch,
        git_commit=commit,
        chunks=chunks,
        headings=headings,
    )


def _sidecar_for_payload(
    payload: ConnectorPayload, suffix: str = CAPTION_SIDECAR_SUFFIX
) -> bytes | None:
    """Read an authored caption/transcript sidecar for a filesystem payload."""

    path = payload.metadata.get("path")
    if not path:
        return None
    return _sidecar_for_path(Path(path), suffix)


def _is_html(mime_type: str | None) -> bool:
    return (mime_type or "").split(";", 1)[0].strip().lower() in {
        "text/html",
        "application/xhtml+xml",
    }


def _is_text_like(mime_type: str | None) -> bool:
    if not mime_type:
        return False
    return mime_type.startswith("text/") or mime_type in {
        "application/json",
        "application/xml",
        "application/x-yaml",
    }
