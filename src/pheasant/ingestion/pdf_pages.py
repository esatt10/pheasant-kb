"""Reading a PDF's pages with pymupdf: the one implementation.

Split out of `extractor.py` when reading page *ranges* arrived: the native
extractor reads every page through :func:`pdf_page_texts`, and a preparation
worker reads one range of a long PDF through the same function
(`sync/pdf_split.py`), so a PDF split across a fleet is read by exactly the
code that reads it whole. Kept free of everything else in the extractor so
that claim is checkable by looking at one small module.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

# MuPDF's global caches are not safe to drive concurrently from many threads
# in one worker process.  Serialize native MuPDF access per process; horizontal
# worker replicas still extract separate documents in parallel.
_NATIVE_MUPDF_LOCK = threading.Lock()

#: ``(content, relative_path) -> one text per page``: where a PDF's page texts
#: come from. ``None`` means :func:`pdf_page_texts` here, in this process; the
#: sync engine supplies one that splits a long PDF across preparation workers
#: (`sync/pdf_split.py`). Either way the extractor joins and tidies the pages
#: itself, so the text it returns does not depend on who read them.
PdfPages = Callable[[bytes, str], list[str]]


def _pymupdf() -> Any | None:
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        try:
            import fitz as pymupdf  # type: ignore[import-not-found,no-redef]
        except ModuleNotFoundError:
            return None
    return pymupdf


def pdf_reader_version(math: bool = False) -> str:
    """The pymupdf release that reads PDFs here, or ``""`` without one.

    With ``math``, also the equation reader's version: a worker that writes
    equations differently (or not at all) must not answer for this one.
    """

    pymupdf = _pymupdf()
    version = str(getattr(pymupdf, "__version__", "")) if pymupdf is not None else ""
    if math and version:
        from pheasant.ingestion.pdf_math import MATH_VERSION

        version += f"+math{MATH_VERSION}"
    return version


def pdf_page_count(content: bytes) -> int:
    """Pages in a PDF, via pymupdf. Raises when pymupdf cannot open it."""

    pymupdf = _pymupdf()
    if pymupdf is None:
        raise ModuleNotFoundError("pymupdf")
    with _NATIVE_MUPDF_LOCK:
        with pymupdf.open(stream=content, filetype="pdf") as document:
            return int(document.page_count)


def pdf_page_texts(
    content: bytes, first: int = 0, stop: int | None = None, math: bool = False
) -> list[str]:
    """The text of pages ``[first, stop)``, one string per page, untidied.

    The one implementation of "read a PDF's pages": the native extractor reads
    all of them through it and a preparation worker reads its range through
    it, so a long PDF split across a fleet is read by exactly the code that
    reads it whole. Raises when pymupdf is missing or fails, which the native
    extractor turns into its builtin fallback. ``math`` writes each page's
    equations as LaTeX (:func:`pdf_page_math_texts`).
    """

    if math:
        return pdf_page_math_texts(content, first, stop)
    pymupdf = _pymupdf()
    if pymupdf is None:
        raise ModuleNotFoundError("pymupdf")
    with _NATIVE_MUPDF_LOCK:
        with pymupdf.open(stream=content, filetype="pdf") as document:
            end = document.page_count if stop is None else min(stop, document.page_count)
            return [document[number].get_text() or "" for number in range(first, end)]


def pdf_page_math_texts(content: bytes, first: int = 0, stop: int | None = None) -> list[str]:
    """:func:`pdf_page_texts` with each page's equations written as LaTeX.

    A page with no mathematics is ``get_text()`` exactly, so turning
    ``ingestion.extractor.math`` on changes only the pages that hold some.
    The same function serves the whole-PDF read and a worker's page range,
    for the reason :func:`pdf_page_texts` does.
    """

    pymupdf = _pymupdf()
    if pymupdf is None:
        raise ModuleNotFoundError("pymupdf")
    with _NATIVE_MUPDF_LOCK:
        with pymupdf.open(stream=content, filetype="pdf") as document:
            end = document.page_count if stop is None else min(stop, document.page_count)
            return [_math_page_text(document[number]) for number in range(first, end)]


def _math_page_text(page: Any) -> str:
    from pheasant.ingestion.pdf_math import page_may_hold_math, rewrite_page

    plain = page.get_text() or ""
    fonts = [str(font[3]) for font in page.get_fonts()]
    if not page_may_hold_math(plain, fonts):
        return plain
    glyphs, rules = page_glyphs(page)
    rewritten = rewrite_page(glyphs, rules)
    return plain if rewritten is None else rewritten + "\n"


def pymupdf_flags() -> Any:
    return _pymupdf()


def page_glyphs(page: Any) -> tuple[list[Any], list[Any]]:
    """A pymupdf page as :mod:`pheasant.ingestion.pdf_math` glyphs and rules.

    Characters come from ``rawdict`` with their span's font and size, the
    advance from the character box and the baseline from its origin; a line
    is pymupdf's own (block, line), so the rewritten text keeps its reading
    order. Rules are thin horizontal fills and strokes from
    ``get_drawings``. A line not set left to right is left to the plain text.
    """

    from pheasant.ingestion.math_layout import Rule
    from pheasant.ingestion.pdf_math import Glyph

    glyphs: list[Glyph] = []
    order = 0
    line_id = 0
    # Unknown glyphs as their codes rather than U+FFFD: the Computer Modern
    # extension font's glyph names (`summationdisplay`, `parenleftbigg`) are
    # in no Unicode table, and its code is what says which glyph it is.
    flags = int(getattr(pymupdf_flags(), "TEXTFLAGS_RAWDICT", 0)) | int(
        getattr(pymupdf_flags(), "TEXT_CID_FOR_UNKNOWN_UNICODE", 0)
    )
    raw = page.get_text("rawdict", flags=flags)
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            line_id += 1
            direction = line.get("dir", (1.0, 0.0))
            upright = abs(direction[0] - 1.0) < 1e-3 and abs(direction[1]) < 1e-3
            for span in line.get("spans", []):
                italic = bool(int(span.get("flags", 0)) & 2)
                for char in span.get("chars", []):
                    x0, top, x1, bottom = char["bbox"]
                    glyphs.append(
                        Glyph(
                            char=char["c"] if upright else " ",
                            font=str(span.get("font", "")),
                            size=float(span.get("size", 10.0)) or 10.0,
                            x0=float(x0),
                            x1=float(x1),
                            baseline=float(char["origin"][1]),
                            order=order,
                            line=line_id,
                            italic=italic,
                            box_top=float(top),
                            box_bottom=float(bottom),
                        )
                    )
                    order += 1
    if any(g.char < " " for g in glyphs):
        # Codes in place of unknown glyphs are for the equations; prose keeps
        # what the plain reading shows. One more read, only on such a page,
        # whose characters align one to one with the first.
        plain = [
            char["c"]
            for block in page.get_text("rawdict").get("blocks", [])
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            for char in span.get("chars", [])
        ]
        if len(plain) == len(glyphs):
            for glyph, char in zip(glyphs, plain, strict=True):
                if char != glyph.char:
                    glyph.plain = char
    rules: list[Rule] = []
    width = float(page.rect.width) or 612.0
    for drawing in page.get_drawings():
        for item in drawing.get("items", []):
            kind = item[0]
            if kind == "re":
                rect = item[1]
                if 1.0 <= rect.width <= 0.6 * width and rect.height <= 1.6:
                    rules.append(Rule(float(rect.x0), float(rect.x1), float(rect.y0 + rect.y1) / 2))
            elif kind == "l":
                start, end = item[1], item[2]
                if abs(start.y - end.y) <= 0.3 and 1.0 <= abs(end.x - start.x) <= 0.6 * width:
                    rules.append(
                        Rule(float(min(start.x, end.x)), float(max(start.x, end.x)), float(start.y))
                    )
    return glyphs, rules
