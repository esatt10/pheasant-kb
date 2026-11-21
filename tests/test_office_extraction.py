"""Acceptance tests for PPTX / XLSX / DOC / RTF / EPUB text extraction.

Fully offline. Companion to ``test_document_extraction.py`` (PDF/DOCX/HTML).

**Fixtures are producer-generated, not hand-authored.** Every binary fixture
here was written by a real office suite (LibreOffice Writer/Calc/Impress), so
these tests exercise structures a real producer actually emits. That matters
most for ``legacy.doc``: a hand-crafted Compound File Binary fixture would only
prove that the reader agrees with its own author's reading of the spec, which
is no evidence at all. Where an independent implementation is installed
(``olefile``), the container layer is additionally cross-checked against it.

Two branches cannot be reached through these fixtures and are covered by direct
unit tests instead, with the reason recorded at each: the **compressed cp1252**
piece encoding (LibreOffice always writes UTF-16 pieces; real Word prefers
compressed) and Word **field instruction** ranges.
"""

from __future__ import annotations

import io
import struct
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import ExtractorSettings, PheasantConfig
from pheasant.ingestion.content_types import DOCUMENT_EXTENSIONS, artifact_type
from pheasant.ingestion.extractor import (
    EXTRACTED_EXTENSIONS,
    AutoExtractor,
    BuiltinExtractor,
    NativeExtractor,
    extractable_extensions,
    source_includes_documents,
)
from pheasant.ingestion.msdoc import (
    _DROP,
    _clean,
    _parse_plcpcd,
    extract_doc_text,
    pieces_to_text,
)
from pheasant.ingestion.office import (
    extract_epub_text,
    extract_pptx_text,
    extract_rtf_text,
    extract_xlsx_text,
)
from pheasant.search.hybrid import HybridSearch
from pheasant.search.sqlite_store import SearchStore
from pheasant.sync.engine import SyncEngine

DOCS = Path(__file__).parent / "fixtures" / "sample_workspace" / "documents"
DECK = DOCS / "deck.pptx"
METRICS = DOCS / "metrics.xlsx"
PRIMER = DOCS / "primer.epub"
MEMO = DOCS / "memo.rtf"
LEGACY = DOCS / "legacy.doc"

ALL_FIXTURES = [DECK, METRICS, PRIMER, MEMO, LEGACY]


# ---------------------------------------------------------------------------
# Per-format extraction against producer-generated files
# ---------------------------------------------------------------------------


def test_pptx_extracts_slide_text_and_speaker_notes() -> None:
    text = extract_pptx_text(DECK.read_bytes())
    assert "Federation Overview MARMOSET" in text
    assert "The ZEBRAFISH protocol governs resync." in text
    # Speaker notes are authored content, so they are indexed too.
    assert "PANGOLIN" in text
    # Slide 2 follows slide 1.
    assert text.index("Federation Overview") < text.index("Chapter Two OKAPI")


def test_pptx_keeps_one_paragraph_per_line() -> None:
    """Walking <a:p> rather than concatenating every <a:t> keeps bullets apart."""
    text = extract_pptx_text(DECK.read_bytes())
    assert "Regions publish a semantic contract.\nThe ZEBRAFISH" in text


def test_xlsx_extracts_sheet_name_and_shared_strings() -> None:
    text = extract_xlsx_text(METRICS.read_bytes())
    # Shared-string cells resolve through xl/sharedStrings.xml.
    for marker in ("Region", "Metric", "OKAPI", "ZEBRAFISH", "PANGOLIN", "handbooks"):
        assert marker in text, marker
    # Numeric cells survive as their literal values.
    assert "0.94" in text
    # Rows are tab-separated so a row reads as one line.
    assert "handbooks\tOKAPI\t0.94" in text


def test_xlsx_includes_the_sheet_name() -> None:
    """A tab called "Q3 Forecast" is authored text a search should find."""
    assert "src" in extract_xlsx_text(METRICS.read_bytes()).splitlines()[0]


def test_epub_extracts_body_text_without_markup() -> None:
    text = extract_epub_text(PRIMER.read_bytes())
    assert "ZEBRAFISH" in text and "OKAPI" in text and "PANGOLIN" in text
    assert "<" not in text and "</p>" not in text


def test_rtf_extracts_prose_without_control_words() -> None:
    text = extract_rtf_text(MEMO.read_bytes())
    assert "The ZEBRAFISH protocol governs incremental resync" in text
    assert "OKAPI" in text
    # A real RTF preamble is full of markup destinations; none may leak.
    for leak in ("fonttbl", "colortbl", "stylesheet", "Times New Roman", "\\par", "Liberation"):
        assert leak not in text, leak


def test_rtf_preserves_literal_backslash_and_parens() -> None:
    text = extract_rtf_text(MEMO.read_bytes())
    assert "(parens)" in text
    assert "backslash \\ here" in text


def test_doc_extracts_text_from_a_real_compound_file() -> None:
    text = extract_doc_text(LEGACY.read_bytes())
    assert "Pheasant Legacy Format Handbook" in text
    assert "The ZEBRAFISH protocol governs incremental resync" in text
    assert "OKAPI" in text and "PANGOLIN" in text


def test_doc_does_not_drop_printable_ascii() -> None:
    """Regression guard for a real bug found during development.

    The control-character drop set briefly included 0x28/0x3C/0x3E on the
    mistaken belief they were Word markers. They are the printable ASCII "(",
    "<" and ">", so parentheses and angle brackets were silently deleted from
    every extracted document — no error, no crash, just missing characters.
    """
    text = extract_doc_text(LEGACY.read_bytes())
    assert "(parens)" in text
    assert all(code < 0x20 for code in _DROP), "_DROP must only hold control codes"
    assert 0x28 not in _DROP and 0x3C not in _DROP and 0x3E not in _DROP


def test_doc_container_matches_an_independent_implementation() -> None:
    """Cross-check the CFB layer against ``olefile`` where it is installed.

    ``olefile`` is deliberately NOT a dependency — this is an optional
    corroboration, not a requirement. It is the strongest available evidence
    that the container reader is right rather than merely self-consistent.
    """
    olefile = pytest.importorskip("olefile")
    from pheasant.ingestion.msdoc import CompoundFile

    mine = CompoundFile(LEGACY.read_bytes())
    theirs = olefile.OleFileIO(str(LEGACY))
    their_names = {entry[0] for entry in theirs.listdir()}
    assert their_names <= set(mine.names())
    for name in ("WordDocument", "1Table"):
        assert mine.read_stream(name) == theirs.openstream(name).read(), name


# ---------------------------------------------------------------------------
# Branches the producer-generated fixtures cannot reach
# ---------------------------------------------------------------------------


def test_compressed_cp1252_piece_halves_the_offset() -> None:
    """The encoding real Word prefers, which LibreOffice never writes.

    A compressed piece stores single-byte cp1252 text and encodes its offset
    doubled with bit 0x40000000 set, so the reader must mask the flag and halve
    the result. Every fixture here is UTF-16, so without this test the
    arithmetic would ship unverified against the producer that relies on it.
    """
    payload = "Compressed cp1252: caf\xe9 (parens)".encode("cp1252")
    word_stream = b"\x00" * 100 + payload
    fc_raw = 0x40000000 | (100 * 2)
    body = struct.pack("<II", 0, len(payload)) + struct.pack("<HIH", 0, fc_raw, 0)

    pieces = _parse_plcpcd(body)
    assert pieces[0] == (0, 100, True)  # flag masked off, offset halved
    assert pieces_to_text(word_stream, pieces) == "Compressed cp1252: café (parens)"


def test_utf16_piece_reads_two_bytes_per_character() -> None:
    payload = "UTF-16 piece: 知識".encode("utf-16-le")
    word_stream = b"\x00" * 64 + payload
    body = struct.pack("<II", 0, len(payload) // 2) + struct.pack("<HIH", 0, 64, 0)
    pieces = _parse_plcpcd(body)
    assert pieces[0] == (0, 64, False)
    assert pieces_to_text(word_stream, pieces) == "UTF-16 piece: 知識"


def test_doc_field_instructions_are_dropped_but_results_kept() -> None:
    """Field ranges are why naive extractors emit hyperlink machinery inline.

    Text between 0x13 and 0x14 is the field *instruction*; text between 0x14
    and 0x15 is the *result* a reader sees.
    """
    raw = 'See \x13HYPERLINK "http://example.invalid/x"\x14our site\x15 for more.'
    cleaned = _clean(raw)
    assert cleaned == "See our site for more."
    assert "HYPERLINK" not in cleaned and "example.invalid" not in cleaned


def test_doc_control_characters_become_whitespace() -> None:
    assert _clean("a\x0db") == "a\nb"  # paragraph end
    assert _clean("a\x07b") == "a\tb"  # cell/row end
    assert _clean("a\x0bb") == "a\nb"  # line break
    assert _clean("a\xa0b") == "a b"  # non-breaking space
    assert _clean("a\x1fb") == "ab"  # optional hyphen disappears
    assert _clean("a\x1eb") == "a-b"  # non-breaking hyphen


def test_epub_uses_spine_order_not_filename_order() -> None:
    """Chapter files are often named such that sorting scrambles the book."""
    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        "<manifest>"
        '<item id="c1" href="zzz_first.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="c2" href="aaa_second.xhtml" media-type="application/xhtml+xml"/>'
        "</manifest>"
        '<spine><itemref idref="c1"/><itemref idref="c2"/></spine>'
        "</package>"
    )
    container = (
        '<?xml version="1.0"?><container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/zzz_first.xhtml", "<html><body><p>ALPHA chapter</p></body></html>")
        archive.writestr("OEBPS/aaa_second.xhtml", "<html><body><p>BETA chapter</p></body></html>")

    text = extract_epub_text(buf.getvalue())
    assert "ALPHA" in text and "BETA" in text
    assert text.index("ALPHA") < text.index("BETA"), "spine order was not honoured"


def test_rtf_unicode_escapes_do_not_double_characters() -> None:
    r"""``\uN`` is followed by fallback characters that ``\ucN`` counts.

    Without honouring ``\ucN`` every non-ASCII character appears twice — once
    decoded and once as its ASCII substitution.
    """

    # RTF spells non-ASCII as \uN (decimal code point) followed by fallback
    # characters. Built programmatically so no Python-level escape processing
    # can quietly turn "\uNNNN" into a literal character before the test runs.
    def esc(code: int) -> bytes:
        return b"\\u" + str(code).encode("ascii") + b" ?"

    rtf = (
        b"{\\rtf1\\ansi\\uc1 caf"
        + esc(0xE9)  # é
        + b" and "
        + esc(0x77E5)  # 知
        + esc(0x8B58)  # 識
        + b" plus \\'bd end}"
    )
    text = extract_rtf_text(rtf)
    assert "café" in text  # é decoded, its "?" fallback swallowed
    assert "知識" in text  # 知識 from consecutive \uN runs
    assert "½" in text  # \'bd -> cp1252 one-half
    assert "?" not in text, f"fallback characters leaked: {text!r}"


def test_rtf_skips_ignorable_destinations() -> None:
    rtf = rb"{\rtf1{\*\generator Secret Tool 1.0;}{\fonttbl{\f0 Arial;}}Visible prose.}"
    text = extract_rtf_text(rtf)
    assert "Visible prose." in text
    assert "Secret Tool" not in text and "Arial" not in text


# ---------------------------------------------------------------------------
# Malformed and hostile input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload,name",
    [
        (b"", "empty.pptx"),
        (b"", "empty.xlsx"),
        (b"", "empty.epub"),
        (b"", "empty.doc"),
        (b"", "empty.rtf"),
        (b"not an archive", "bogus.pptx"),
        (b"not an archive", "bogus.xlsx"),
        (b"not an archive", "bogus.epub"),
        (b"PK\x03\x04 truncated", "trunc.pptx"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600, "headeronly.doc"),
        (b"{\\rtf1", "trunc.rtf"),
        (b"\xff\xfe\x00garbage", "binary.rtf"),
    ],
)
def test_malformed_documents_return_empty_rather_than_raising(payload: bytes, name: str) -> None:
    assert AutoExtractor().extract(payload, name) == ""


def test_corrupt_compound_file_terminates_promptly() -> None:
    """A hostile FAT must not spin: chain walks carry a visited set."""
    header = bytearray(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504)
    struct.pack_into("<H", header, 30, 9)  # 512-byte sectors
    struct.pack_into("<H", header, 32, 6)
    struct.pack_into("<I", header, 44, 1)  # one FAT sector
    struct.pack_into("<I", header, 48, 0)  # directory at sector 0
    struct.pack_into("<I", header, 76, 0)  # DIFAT[0] -> sector 0
    # Sector 0 is a FAT whose every entry points back at sector 0 (a cycle).
    fat = b"".join(struct.pack("<I", 0) for _ in range(128))
    started = time.perf_counter()
    assert extract_doc_text(bytes(header) + fat) == ""
    assert time.perf_counter() - started < 5.0


def test_oversized_archive_member_is_skipped() -> None:
    """A member declaring a huge inflated size is refused, not inflated."""
    from pheasant.ingestion import office

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("ppt/slides/slide1.xml", b"<p/>" * 10)
    data = buf.getvalue()
    original = office.MAX_MEMBER_BYTES
    try:
        office.MAX_MEMBER_BYTES = 1  # anything real now exceeds the cap
        assert extract_pptx_text(data) == ""
    finally:
        office.MAX_MEMBER_BYTES = original


# ---------------------------------------------------------------------------
# Wiring: accepted <-> extractable must not drift
# ---------------------------------------------------------------------------


def test_accepted_and_extractable_extension_sets_agree() -> None:
    """The load-bearing invariant of this change.

    A format the pipeline *accepts* but no extractor handles indexes with zero
    chunks — the original bug. Asserting set equality means adding an extension
    to one place without the other fails here rather than in production.
    """
    assert DOCUMENT_EXTENSIONS == EXTRACTED_EXTENSIONS
    assert EXTRACTED_EXTENSIONS <= extractable_extensions(ExtractorSettings())


@pytest.mark.parametrize("extension", sorted(EXTRACTED_EXTENSIONS))
def test_every_document_extension_builds_an_extractor(extension: str) -> None:
    class Source:
        include = [f"**/*{extension}"]

    assert source_includes_documents(Source())


@pytest.mark.parametrize("extension", sorted(EXTRACTED_EXTENSIONS))
def test_every_document_extension_types_as_document(extension: str) -> None:
    assert artifact_type(Path(f"file{extension}")) == "document"


@pytest.mark.parametrize("provider", ["builtin", "native", "auto"])
def test_every_provider_reads_every_fixture(provider: str) -> None:
    extractor: Any = {
        "builtin": BuiltinExtractor,
        "native": NativeExtractor,
        "auto": AutoExtractor,
    }[provider]()
    for fixture in ALL_FIXTURES:
        text = extractor.extract(fixture.read_bytes(), fixture.name)
        assert text.strip(), f"{provider} produced nothing for {fixture.name}"


# ---------------------------------------------------------------------------
# Region ingest + self-search
# ---------------------------------------------------------------------------


def _make_office_engine(tmp_path: Path, *, provider: str = "auto") -> SyncEngine:
    docs = tmp_path / "workspace" / "documents"
    docs.mkdir(parents=True, exist_ok=True)
    for fixture in ALL_FIXTURES:
        (docs / fixture.name).write_bytes(fixture.read_bytes())

    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "office-acceptance",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path / "workspace"),
                "exports_path": str(tmp_path / "exports"),
            },
            "ingestion": {"extractor": {"provider": provider}},
            "sources": [
                {
                    "name": "documents",
                    "type": "document_folder",
                    "path": str(docs),
                    "include": [
                        "**/*.pptx",
                        "**/*.xlsx",
                        "**/*.epub",
                        "**/*.rtf",
                        "**/*.doc",
                    ],
                }
            ],
        }
    )
    return SyncEngine(config)


def _search(engine: SyncEngine, query: str) -> list[str]:
    searcher = HybridSearch(SearchStore(engine.state), vector=None)
    payload = searcher.search_context(
        engine.config.knowledge_base_id, query, mode="text", max_results=10
    )
    return [str(item.get("relative_path") or "") for item in payload["results"]]


def test_every_office_format_produces_chunks(tmp_path: Path) -> None:
    engine = _make_office_engine(tmp_path)
    engine.sync_source("documents", mode="full")
    rows = engine.state.rows(
        "SELECT a.relative_path AS relative_path, COUNT(c.id) AS n "
        "FROM artifacts a LEFT JOIN chunks c ON c.artifact_id = a.id "
        "GROUP BY a.relative_path"
    )
    counts = {row["relative_path"]: row["n"] for row in rows}
    for fixture in ALL_FIXTURES:
        assert counts.get(fixture.name, 0) >= 1, f"{fixture.name} produced no chunks: {counts}"


def test_office_content_is_searchable(tmp_path: Path) -> None:
    engine = _make_office_engine(tmp_path)
    engine.sync_source("documents", mode="full")
    # A marker unique to each format finds that format's file.
    assert any("deck.pptx" in p for p in _search(engine, "MARMOSET federation overview"))
    assert any("metrics.xlsx" in p for p in _search(engine, "handbooks OKAPI 0.94"))
    assert any("legacy.doc" in p for p in _search(engine, "Pheasant Legacy Format Handbook"))
    # A query matching nothing returns nothing, so the assertions above are
    # not passing merely because the corpus is small.
    assert _search(engine, "xyzzy nonexistent qwerty") == []


def test_resync_unchanged_office_documents_reextracts_nothing(tmp_path: Path) -> None:
    engine = _make_office_engine(tmp_path)
    engine.sync_source("documents", mode="full")
    assert engine.extractor is not None
    baseline = engine.extractor.calls
    assert baseline >= len(ALL_FIXTURES)
    engine.sync_source("documents", mode="incremental")
    assert engine.extractor.calls == baseline


def test_office_resync_is_idempotent(tmp_path: Path) -> None:
    engine = _make_office_engine(tmp_path)
    engine.sync_source("documents", mode="full")
    first = [
        dict(r)
        for r in engine.state.rows(
            "SELECT c.text AS text FROM chunks c JOIN artifacts a ON c.artifact_id = a.id "
            "WHERE a.relative_path = 'legacy.doc' ORDER BY c.id"
        )
    ]
    engine.sync_source("documents", mode="full")
    second = [
        dict(r)
        for r in engine.state.rows(
            "SELECT c.text AS text FROM chunks c JOIN artifacts a ON c.artifact_id = a.id "
            "WHERE a.relative_path = 'legacy.doc' ORDER BY c.id"
        )
    ]
    assert first == second and first
