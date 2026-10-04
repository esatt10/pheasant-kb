from __future__ import annotations

from pathlib import Path


def source_includes_zip(source: object) -> bool:
    """An explicit ZIP include may contain any supported file type."""

    return any(
        str(pattern).replace("\\", "/").lower().rstrip("/").endswith(".zip")
        for pattern in getattr(source, "include", ()) or ()
    )


TEXT_EXTENSIONS = {
    # Prose and markup
    ".md", ".mdx", ".markdown", ".txt", ".rst", ".adoc", ".org", ".tex",
    ".html", ".xml",
    # Data and configuration
    ".json", ".jsonc", ".json5", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".properties", ".tf", ".tfvars", ".hcl",
    ".proto", ".graphql", ".gql", ".sql", ".prisma",
    # Source: scripting and web
    ".py", ".pyi", ".pyx", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".mts", ".cts", ".vue", ".svelte", ".css", ".scss", ".sass", ".less",
    ".rb", ".rake", ".gemspec", ".php", ".lua", ".pl", ".pm", ".r", ".jl",
    # Source: compiled and JVM
    ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".groovy", ".gradle",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cs", ".fs", ".vb",
    ".swift", ".m", ".mm", ".dart", ".zig", ".nim", ".sol",
    # Source: functional and BEAM
    ".hs", ".ml", ".mli", ".clj", ".cljs", ".ex", ".exs", ".erl", ".elm",
    # Shell and build
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd", ".cmake",
    ".mk", ".patch", ".diff",
}  # fmt: skip

#: Files recognised by their whole name because they carry no (useful)
#: extension. Compared lower-cased.
TEXT_FILENAMES = frozenset(
    {
        "dockerfile", "containerfile", "makefile", "gnumakefile", "rakefile",
        "gemfile", "podfile", "procfile", "jenkinsfile", "vagrantfile",
        "justfile", "brewfile", "caddyfile", "codeowners", "license",
        "licence", "notice", "authors", "contributing", "changelog",
        ".gitignore", ".gitattributes", ".dockerignore", ".editorconfig",
        ".prettierrc", ".eslintrc", ".babelrc", ".nvmrc", ".tool-versions",
    }
)  # fmt: skip


def is_text_file(path: Path | str) -> bool:
    """Whether a path names a text file this pipeline decodes directly."""

    candidate = Path(path)
    return candidate.suffix.lower() in TEXT_EXTENSIONS or candidate.name.lower() in TEXT_FILENAMES


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


#: Text formats a source reads only when asked, because their bytes are mostly
#: markup or machine output rather than prose or code: a page's tags, a patch's
#: hunks. They stay supported (``include`` them to index them).
OPT_IN_TEXT_EXTENSIONS = frozenset({".html", ".xml", ".patch", ".diff"})


def default_include_globs() -> tuple[str, ...]:
    """The ``include`` a source gets when it names none.

    Derived from the lists above so a format cannot be parseable yet never
    reached by default. Globs are case-sensitive and ``TEXT_FILENAMES`` is
    lower-cased, so each name is emitted as its usual spellings: Dockerfile,
    LICENSE, license.
    """

    return (
        *(f"**/*{suffix}" for suffix in sorted(TEXT_EXTENSIONS - OPT_IN_TEXT_EXTENSIONS)),
        *(
            f"**/{spelling}"
            for name in sorted(TEXT_FILENAMES)
            for spelling in dict.fromkeys((name, name.capitalize(), name.upper()))
        ),
    )
