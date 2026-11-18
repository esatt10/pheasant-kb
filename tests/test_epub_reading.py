"""EPUB is read by the builtin reader under every provider.

It was MuPDF under ``native`` and ``auto``, which laid the book out. MuPDF
left with its licence, and the builtin reader (:mod:`pheasant.ingestion.office`)
takes its place. Measured against MuPDF before it was removed, on the books
below: the committed ``primer.epub`` reads word for word the same; a book read
in spine order reads in the same order; and every malformed package -- no
container, a missing OPF, a broken OPF, no spine -- yields its chapters, where
MuPDF failed to open five of them and returned nothing for a missing spine.
A chapter's ``<head>`` is not read, as MuPDF never showed it.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import pytest

from pheasant.ingestion.extractor import AutoExtractor, BuiltinExtractor, NativeExtractor

PRIMER = Path(__file__).parent / "fixtures" / "sample_workspace" / "documents" / "primer.epub"

CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="{opf}" media-type="application/oebps-package+xml"/>'
    "</rootfiles></container>"
)


def _chapter(title: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
        f"<head><title>The Book: {title}</title></head>"
        f"<body><h1>{title}</h1><p>{body}</p></body></html>"
    )


def _opf(items: list[tuple[str, str]], spine: list[str] | None) -> str:
    manifest = "".join(
        f'<item id="{key}" href="{href}" media-type="application/xhtml+xml"/>'
        for key, href in items
    )
    order = (
        ""
        if spine is None
        else "<spine>" + "".join(f'<itemref idref="{key}"/>' for key in spine) + "</spine>"
    )
    return (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        f"<metadata/><manifest>{manifest}</manifest>{order}</package>"
    )


def _book(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        for name, text in files.items():
            archive.writestr(name, text)
    return out.getvalue()


ITEMS = [("c1", "text/ch3.xhtml"), ("c2", "text/ch1.xhtml"), ("c3", "text/ch2.xhtml")]
CHAPTERS = {
    "OEBPS/text/ch1.xhtml": _chapter("Alpha", "First chapter about kestrels."),
    "OEBPS/text/ch2.xhtml": _chapter("Beta", "Second chapter about ospreys."),
    "OEBPS/text/ch3.xhtml": _chapter("Gamma", "Third chapter about falcons ünïcode 東京."),
}
PACKAGED = {"META-INF/container.xml": CONTAINER.format(opf="OEBPS/content.opf")}
IN_ORDER = (
    "Alpha First chapter about kestrels. Beta Second chapter about ospreys. "
    "Gamma Third chapter about falcons ünïcode 東京."
)


def _read(content: bytes, provider: str = "auto") -> str:
    extractor = {"auto": AutoExtractor, "native": NativeExtractor, "builtin": BuiltinExtractor}[
        provider
    ]()
    return re.sub(r"\s+", " ", extractor.extract(content, "book.epub")).strip()


@pytest.mark.parametrize("provider", ["auto", "native", "builtin"])
def test_every_provider_reads_the_spine_in_order(provider: str) -> None:
    spine = _book({**PACKAGED, "OEBPS/content.opf": _opf(ITEMS, ["c2", "c3", "c1"]), **CHAPTERS})
    assert _read(spine, provider) == IN_ORDER


@pytest.mark.parametrize(
    "files",
    [
        pytest.param(
            {**PACKAGED, "OEBPS/content.opf": _opf(ITEMS, None), **CHAPTERS}, id="no-spine"
        ),
        pytest.param(
            {"META-INF/container.xml": CONTAINER.format(opf="OEBPS/gone.opf"), **CHAPTERS},
            id="missing-opf",
        ),
        pytest.param(dict(CHAPTERS), id="no-container"),
        pytest.param(
            {**PACKAGED, "OEBPS/content.opf": "<package><manifest><item id=", **CHAPTERS},
            id="broken-opf",
        ),
    ],
)
def test_a_malformed_package_still_yields_every_chapter(files: dict[str, str]) -> None:
    assert _read(_book(files)) == IN_ORDER


def test_an_empty_or_corrupt_book_is_empty_text_not_a_failed_sync() -> None:
    assert _read(_book({})) == ""
    assert _read(b"this is not an epub at all") == ""


def test_the_committed_book_reads_as_mupdf_read_it() -> None:
    assert _read(PRIMER.read_bytes()) == (
        "Pheasant Legacy Format Handbook The ZEBRAFISH protocol governs incremental "
        "resync across regions. Extraction coverage is tracked by the OKAPI metric. "
        "Escaped glyphs: (parens) and a backslash \\ here. Format PANGOLIN"
    )


def test_a_large_book_is_read_whole() -> None:
    body = " ".join(f"sentence {number} about migration routes." for number in range(4000))
    chapters = {f"OEBPS/c{n}.xhtml": _chapter(f"Part {n}", body) for n in range(25)}
    items = [(f"i{n}", f"c{n}.xhtml") for n in range(25)]
    text = _read(
        _book({**PACKAGED, "OEBPS/content.opf": _opf(items, [k for k, _ in items]), **chapters})
    )
    assert text.count("sentence 3999 about migration routes.") == 25
    assert text.startswith("Part 0 sentence 0")
