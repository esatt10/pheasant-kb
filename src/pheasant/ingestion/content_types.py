from __future__ import annotations

from pathlib import Path


def source_includes_zip(source: object) -> bool:
    """An explicit ZIP include may contain any supported file type."""

    return any(
        str(pattern).replace("\\", "/").lower().rstrip("/").endswith(".zip")
        for pattern in getattr(source, "include", ()) or ()
    )


TEXT_EXTENSIONS = {
    ".py",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".html",
    ".xml",
    ".css",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mdx",
    ".rst",
    ".sh",
}
# Formats whose text has to be *extracted* rather than decoded — see
# pheasant.ingestion.extractor (PDF/DOCX/HTML),
# pheasant.ingestion.office (PPTX/XLSX/EPUB/RTF) and
# pheasant.ingestion.msdoc (legacy binary DOC).
#
# Membership here means two things: `artifact_type` labels the file
# "document", and `parse_file`/`parse_connector_payload` will accept it. It
# does NOT mean every source indexes them — a source only sees these files if
# its own `include` globs admit the extension (the default list is
# code/markdown/config only), and `extractor_from_config` only builds an
# extractor when they do.
DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".doc", ".rtf", ".epub"}
# Synapse 25.4 (session A): images are ingested by captioning them into text
# (see pheasant.ingestion.captioner). The caption flows through the normal
# chunk -> embed -> graph path; the artifact type is "image".
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
# Synapse 25.4 (session B): audio is ingested by transcribing it into text
# (see pheasant.ingestion.transcriber). The transcript flows through the normal
# chunk -> embed -> graph path; the artifact type is "audio".
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}


#: Step 33.7 — an agent-memory record is its own kind of artifact. It is still
#: a Markdown file and still goes through the identical pipeline, but calling it
#: a `markdown_note` left the graph unable to say which of its notes were things
#: an agent *remembered*, and left the UI legend unable to show them apart.
MEMORY_ARTIFACT_TYPE = "memory_record"

#: Node types that stand for a **whole indexed artifact**, as opposed to a piece
#: of one (`chunk`, `heading`) or something derived from one (`entity`,
#: `symbol`, `external_reference`).
#:
#: Defined once because four modules independently hard-coded
#: `{"file", "markdown_note", "document"}` — similarity edges, cross-source
#: reference resolution, the assistant's graph-fact filter and its neighbour
#: walk. Adding a fifth artifact type meant finding all four and hoping; the
#: taxonomy work hit exactly this and fixed it the same way, by asserting one
#: definition instead of repeating a literal.
#:
#: `image` and `audio` are deliberately absent, as they have been since 25.4:
#: their captions and transcripts are indexed as text, but a caption is not a
#: document that can resolve a link or anchor a similarity pair.
ARTIFACT_TYPES = frozenset({"file", "markdown_note", "document", MEMORY_ARTIFACT_TYPE})


def artifact_type(path: Path, source_type: str | None = None) -> str:
    """The node/artifact type for a file.

    ``source_type`` is optional and additive: without it the answer is exactly
    what it was before Step 33.7, which keeps every caller that classifies a
    bare path working unchanged.
    """
    if source_type == "memory":
        # Checked before the suffix, because a memory record *is* a `.md` file
        # and the point is that it is not merely one.
        return MEMORY_ARTIFACT_TYPE
    if path.suffix.lower() == ".md":
        return "markdown_note"
    if path.suffix.lower() in DOCUMENT_EXTENSIONS:
        return "document"
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        return "image"
    if path.suffix.lower() in AUDIO_EXTENSIONS:
        return "audio"
    return "file"
