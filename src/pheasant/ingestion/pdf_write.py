"""A small, deterministic PDF writer for tests and benchmark corpora.

Never on the indexing path: pheasant reads PDFs, it does not write them. The
test suite and ``scripts/fetch_benchmark_corpus.py`` need real PDFs to read,
and they used to make them with PyMuPDF, which left the removed engine as a
test dependency. This writes them with PDFium's own editing API (the
``pypdfium2`` already installed to read them): standard-14 Helvetica, one
text object per line, and pages concatenated by import -- enough for what the
fixtures need, and the same bytes on every run.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import re
import textwrap
from collections.abc import Sequence

#: US Letter, in points.
PAGE_WIDTH, PAGE_HEIGHT = 612.0, 792.0


_FILE_ID = re.compile(rb"/ID\[<([0-9A-Fa-f]{32})><([0-9A-Fa-f]{32})>\]")


def _deterministic(content: bytes) -> bytes:
    """PDFium writes a random file ``/ID``; make it a digest of the rest.

    Same length, and after the cross-reference table, so no offset moves.
    """

    digest = hashlib.sha256(_FILE_ID.sub(b"", content)).hexdigest()[:32].upper().encode()
    return _FILE_ID.sub(b"/ID[<" + digest + b"><" + digest + b">]", content, count=1)


def _widestring(text: str) -> ctypes.Array:
    return ctypes.create_string_buffer((text + "\x00").encode("utf-16-le"))


def text_pdf(
    pages: Sequence[Sequence[str]],
    *,
    font_size: float = 9.0,
    margin: float = 36.0,
    leading: float = 1.25,
) -> bytes:
    """A PDF with one page per entry, each line set where the previous ended."""

    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    document = pdfium.PdfDocument.new()
    try:
        font = raw.FPDFText_LoadStandardFont(document.raw, b"Helvetica")
        for lines in pages:
            page = document.new_page(PAGE_WIDTH, PAGE_HEIGHT)
            y = PAGE_HEIGHT - margin - font_size
            for line in lines:
                obj = raw.FPDFPageObj_CreateTextObj(document.raw, font, ctypes.c_float(font_size))
                buffer = _widestring(line)
                raw.FPDFText_SetText(obj, ctypes.cast(buffer, raw.FPDF_WIDESTRING))
                raw.FPDFPageObj_Transform(obj, 1, 0, 0, 1, margin, y)
                raw.FPDFPage_InsertObject(page.raw, obj)
                y -= font_size * leading
            raw.FPDFPage_GenerateContent(page.raw)
            page.close()
        raw.FPDFFont_Close(font)
        out = io.BytesIO()
        document.save(out)
        return _deterministic(out.getvalue())
    finally:
        document.close()


def wrapped_text_pdf(text: str, *, font_size: float = 9.0, margin: float = 56.0) -> bytes:
    """``text`` wrapped to the page and continued onto as many pages as it needs.

    Wrapped conservatively (Helvetica averages about half an em a character),
    so no line runs off the page and nothing the corpus contains is lost.
    """

    usable = PAGE_WIDTH - 2 * margin
    width = max(20, int(usable / (font_size * 0.56)))
    lines: list[str] = []
    for paragraph in text.split("\n"):
        lines.extend(textwrap.wrap(paragraph, width=width) or [""])
    per_page = max(1, int((PAGE_HEIGHT - 2 * margin) / (font_size * 1.25)))
    pages = [lines[start : start + per_page] for start in range(0, len(lines), per_page)]
    return text_pdf(pages or [[]], font_size=font_size, margin=margin)


def repeated_pdf(content: bytes, copies: int) -> bytes:
    """``content``'s pages, ``copies`` times over, as one document."""

    import pypdfium2 as pdfium

    source = pdfium.PdfDocument(content)
    document = pdfium.PdfDocument.new()
    try:
        for _ in range(copies):
            document.import_pages(source)
        out = io.BytesIO()
        document.save(out)
        return _deterministic(out.getvalue())
    finally:
        document.close()
        source.close()
