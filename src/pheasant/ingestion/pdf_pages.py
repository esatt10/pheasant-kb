"""Reading a PDF's pages with PDFium: the one implementation.

Split out of `extractor.py` when reading page *ranges* arrived: the native
extractor reads every page through :func:`pdf_page_texts`, and a preparation
worker reads one range of a long PDF through the same function
(`sync/pdf_split.py`), so a PDF split across a fleet is read by exactly the
code that reads it whole. Kept free of everything else in the extractor so
that claim is checkable by looking at one small module.

The engine is PDFium (BSD-3-Clause) through ``pypdfium2``. It replaced MuPDF,
whose terms are AGPL or a commercial licence. What the rest of pheasant sees
did not change: one text per page, and on a page holding mathematics the
:class:`~pheasant.ingestion.pdf_regions.Glyph` and
:class:`~pheasant.ingestion.math_layout.Rule` lists the equation reader takes,
in the shape MuPDF gave them -- top-left origin, a character's advance as its
box, its baseline from its origin, the font's own vertical extent, raw codes
for glyphs a font gives no Unicode, a synthesized space spanning each word gap
and a line per text line. ``tests/test_document_math.py`` holds the exact TeX
every committed fixture produces through this reader.

PDFium is not safe to call from two threads at once -- not even on two
documents -- so every call here holds one process-wide lock. Parallelism is
the fleet's: separate worker processes read separate ranges.
"""

from __future__ import annotations

import ctypes
import math as _math
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

#: PDFium is not re-entrant across documents; one call at a time per process.
_PDFIUM_LOCK = threading.Lock()

#: Bumped when a change here can change the text a PDF produces. It is part
#: of :func:`pdf_reader_version`, which a split PDF's workers must match and
#: which is folded into every document source's fingerprint, so stored text
#: from an older reader is re-read once (`sync/fingerprint.py`).
ADAPTER_REVISION = "1"

#: ``(content, relative_path) -> one text per page``: where a PDF's page texts
#: come from. ``None`` means :func:`pdf_page_texts` here, in this process; the
#: sync engine supplies one that splits a long PDF across preparation workers
#: (`sync/pdf_split.py`). Either way the extractor joins and tidies the pages
#: itself, so the text it returns does not depend on who read them.
PdfPages = Callable[[bytes, str], list[str]]

_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")

_PAGEOBJ_TEXT, _PAGEOBJ_PATH, _PAGEOBJ_FORM = 1, 2, 5
_SEGMENT_LINETO, _SEGMENT_BEZIERTO, _SEGMENT_MOVETO = 0, 1, 2
_FONT_ITALIC = 0x40


def _pdfium() -> Any | None:
    try:
        import pypdfium2  # type: ignore[import-not-found]
        import pypdfium2.raw  # type: ignore[import-not-found]  # noqa: F401
    except ModuleNotFoundError:
        return None
    return pypdfium2


def pdf_reader_name() -> str:
    """The engine, its build and this adapter's revision, or ``""`` without one."""

    pdfium = _pdfium()
    if pdfium is None:
        return ""
    from pypdfium2.version import PDFIUM_INFO, PYPDFIUM_INFO

    return f"pdfium-{PDFIUM_INFO.build}/pypdfium2-{PYPDFIUM_INFO}/r{ADAPTER_REVISION}"


def pdf_reader_version(math: bool = False) -> str:
    """The reader identity a worker must match, or ``""`` without a reader.

    With ``math``, also the equation reader's version: a worker that writes
    equations differently (or not at all) must not answer for this one.
    """

    version = pdf_reader_name()
    if math and version:
        from pheasant.ingestion.pdf_math import MATH_VERSION

        version += f"+math{MATH_VERSION}"
    return version


@contextmanager
def _document(content: bytes) -> Iterator[Any]:
    """An open document for exactly as long as the lock is held."""

    pdfium = _pdfium()
    if pdfium is None:
        raise ModuleNotFoundError("pypdfium2")
    with _PDFIUM_LOCK:
        document = pdfium.PdfDocument(content)
        try:
            yield document
        finally:
            document.close()


def pdf_page_count(content: bytes) -> int:
    """Pages in a PDF, via PDFium. Raises when PDFium cannot open it."""

    with _document(content) as document:
        return len(document)


def pdf_page_texts(
    content: bytes, first: int = 0, stop: int | None = None, math: bool = False
) -> list[str]:
    """The text of pages ``[first, stop)``, one string per page, untidied.

    The one implementation of "read a PDF's pages": the native extractor reads
    all of them through it and a preparation worker reads its range through
    it, so a long PDF split across a fleet is read by exactly the code that
    reads it whole. Raises when PDFium is missing or fails, which the native
    extractor turns into its builtin fallback. ``math`` writes each page's
    equations as LaTeX (:func:`pdf_page_math_texts`).
    """

    if math:
        return pdf_page_math_texts(content, first, stop)
    with _document(content) as document:
        end = len(document) if stop is None else min(stop, len(document))
        return [_with_page(document, number, _plain_text) for number in range(first, end)]


def pdf_page_math_texts(content: bytes, first: int = 0, stop: int | None = None) -> list[str]:
    """:func:`pdf_page_texts` with each page's equations written as LaTeX.

    A page with no mathematics is the plain text exactly, so turning
    ``ingestion.extractor.math`` on changes only the pages that hold some.
    The same function serves the whole-PDF read and a worker's page range,
    for the reason :func:`pdf_page_texts` does.
    """

    advances = _BuiltinAdvances(content)
    with _document(content) as document:
        end = len(document) if stop is None else min(stop, len(document))
        return [
            _with_page(
                document,
                number,
                lambda page, textpage, number=number: _math_page_text(
                    page, textpage, advances, number
                ),
            )
            for number in range(first, end)
        ]


def _with_page(document: Any, number: int, read: Callable[[Any, Any], str]) -> str:
    page = document[number]
    try:
        textpage = page.get_textpage()
        try:
            return read(page, textpage)
        finally:
            textpage.close()
    finally:
        page.close()


def _plain_text(page: Any, textpage: Any) -> str:
    """A page's text as PDFium reads it, newline-separated, ending in a newline.

    One call into PDFium per page. ``\\r\\n`` (PDFium's line break) becomes
    ``\\n``. A code below space that a font shows instead of a character
    (TeX's extension font) is kept, as MuPDF kept it: it is what
    :func:`~pheasant.ingestion.pdf_math.page_may_hold_math` looks for.
    """

    text = textpage.get_text_range()
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text if text.endswith("\n") else text + "\n"


def _math_page_text(
    page: Any, textpage: Any, advances: _BuiltinAdvances | None = None, number: int = 0
) -> str:
    from pheasant.ingestion.pdf_math import page_may_hold_math, rewrite_page

    plain = _plain_text(page, textpage)
    if not page_may_hold_math(plain, _page_fonts(page)):
        return plain
    glyphs, rules = page_glyphs(page, textpage, advances.page(number) if advances else None)
    rewritten = rewrite_page(glyphs, rules)
    return plain if rewritten is None else rewritten + "\n"


def _base_font(name: str) -> str:
    return _SUBSET_PREFIX.sub("", name)


def _font_name(raw: Any, font: Any) -> str:
    size = raw.FPDFFont_GetBaseFontName(font, None, 0)
    if size <= 1:
        return ""
    buffer = ctypes.create_string_buffer(size)
    raw.FPDFFont_GetBaseFontName(font, buffer, size)
    return buffer.value.decode("utf-8", "replace")


def _objects(raw: Any, container: Any, *, form: bool = False) -> Iterator[tuple[Any, list[Any]]]:
    """Every page object, descending into form XObjects, with its form chain."""

    count = (
        raw.FPDFFormObj_CountObjects(container) if form else raw.FPDFPage_CountObjects(container)
    )
    for index in range(max(0, count)):
        obj = (
            raw.FPDFFormObj_GetObject(container, index)
            if form
            else raw.FPDFPage_GetObject(container, index)
        )
        if not obj:
            continue
        kind = raw.FPDFPageObj_GetType(obj)
        if kind == _PAGEOBJ_FORM:
            for child, chain in _objects(raw, obj, form=True):
                yield child, [obj, *chain]
        else:
            yield obj, []


def _page_fonts(page: Any) -> list[str]:
    """Base font names of a page's text objects, for the cheap math gate."""

    import pypdfium2.raw as raw

    names: dict[int, str] = {}
    for obj, _chain in _objects(raw, page.raw):
        if raw.FPDFPageObj_GetType(obj) != _PAGEOBJ_TEXT:
            continue
        font = raw.FPDFTextObj_GetFont(obj)
        if not font:
            continue
        key = ctypes.cast(font, ctypes.c_void_p).value or 0
        if key not in names:
            names[key] = _font_name(raw, font)
    return [name for name in names.values() if name]


def _matrix(raw: Any, obj: Any) -> tuple[float, float, float, float, float, float]:
    matrix = raw.FS_MATRIX()
    if not raw.FPDFPageObj_GetMatrix(obj, ctypes.byref(matrix)):
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    return (matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f)


def _apply(m: tuple[float, ...], x: float, y: float) -> tuple[float, float]:
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _compose(
    inner: tuple[float, ...], outer: tuple[float, ...]
) -> tuple[float, float, float, float, float, float]:
    """``inner`` then ``outer`` (PDF row-vector convention)."""

    a, b, c, d, e, f = inner
    A, B, C, D, E, F = outer
    return (
        a * A + b * C,
        a * B + b * D,
        c * A + d * C,
        c * B + d * D,
        e * A + f * C + E,
        e * B + f * D + F,
    )


class _BuiltinAdvances:
    """Each glyph's advance as the PDF's own font metrics give it, by page.

    PDFium reports a character's box as its advance *or its ink*, whichever
    reaches further right, and exposes no advance on its own: an italic's
    overhang, DejaVu's ``∣`` and TeX's zero-width ``\\not`` slash all come
    out wider than they are set, which moves the gaps the equation reader
    reads spaces and fences from. The builtin reader
    (:mod:`pheasant.ingestion.pdf_glyphs`) interprets the content stream and
    takes each advance from the font's ``/Widths``, exactly as MuPDF did, so a
    PDFium glyph takes the builtin's advance where the builtin read the same
    glyph at the same origin. Where it read nothing there (a font it cannot
    decode, a page it cannot parse), PDFium's width stands. Parsed lazily,
    once per document, and only for pages that hold mathematics.
    """

    def __init__(self, content: bytes):
        self.content = content
        self._document: Any = None
        self._pages: list[Any] | None = None
        self._cache: dict = {}
        self._failed = False

    def page(self, number: int) -> dict[tuple[float, float], list[tuple[str, float]]] | None:
        if self._failed:
            return None
        try:
            if self._pages is None:
                from pheasant.ingestion.pdf_objects import lex_pdf, pages, read_document

                self._document = read_document(self.content, lex_pdf)
                self._pages = list(pages(self._document))
            if number >= len(self._pages):
                return None
            from pheasant.ingestion.pdf_glyphs import page_glyphs as builtin_glyphs

            glyphs, _rules = builtin_glyphs(self._document, self._pages[number], self._cache)
        except Exception:  # noqa: BLE001 - PDFium's widths are the fallback
            self._failed = True
            return None
        found: dict[tuple[float, float], list[tuple[str, float]]] = {}
        for glyph in glyphs:
            if glyph.char.strip():
                key = (round(glyph.x0, 1), round(glyph.baseline, 1))
                found.setdefault(key, []).append((glyph.char, glyph.x1 - glyph.x0))
        return found


def _advance(
    known: dict[tuple[float, float], list[tuple[str, float]]], x0: float, baseline: float, char: str
) -> float | None:
    """The builtin's advance for this glyph, if it read one glyph there that is it."""

    candidates = known.get((round(x0, 1), round(baseline, 1)))
    if not candidates:
        return None
    same = [advance for seen, advance in candidates if seen == char]
    if len(same) >= 1 and len(set(same)) == 1:
        return same[0]
    if len(candidates) == 1:
        return candidates[0][1]
    return None


def page_glyphs(
    page: Any,
    textpage: Any,
    advances: dict[tuple[float, float], list[tuple[str, float]]] | None = None,
) -> tuple[list[Any], list[Any]]:
    """A PDFium page as :mod:`pheasant.ingestion.pdf_math` glyphs and rules.

    Characters come from the text page with their font, size, origin and
    loose box: the box starts at the origin and its height is the font's, and
    the advance is the builtin reader's where it has one (``advances``,
    :class:`_BuiltinAdvances`), else the box's width.
    They are put back in **content-stream order** -- each character's text
    object's place among the page's objects, then its place in that object --
    because PDFium's text page reorders characters that overlap (TeX's ``≠``
    is a slash drawn over ``=``, and the slash must come first). Lines and the
    spaces a PDF leaves implicit are then assigned by
    :func:`pheasant.ingestion.pdf_glyphs._visual_lines`, the rule the builtin
    reader uses and the one MuPDF applied, so both providers read one page
    into the same lines. PDFium's own generated spaces and line breaks are
    not used. A character not set left to right is left to the plain text.
    Rules are thin horizontal rectangles and line segments from the page's
    paths, form XObjects included.
    """

    import pypdfium2.raw as raw

    from pheasant.ingestion.math_layout import Rule
    from pheasant.ingestion.pdf_glyphs import _visual_lines
    from pheasant.ingestion.pdf_math import Glyph

    tp = textpage.raw
    left, bottom, right, top = page.get_cropbox()
    width = float(right - left) or 612.0
    count = raw.FPDFText_CountChars(tp)
    font_buffer = ctypes.create_string_buffer(256)
    flags = ctypes.c_int()
    loose = raw.FS_RECTF()
    matrix = raw.FS_MATRIX()
    origin_x, origin_y = ctypes.c_double(), ctypes.c_double()
    loose_ref, flags_ref = ctypes.byref(loose), ctypes.byref(flags)
    matrix_ref = ctypes.byref(matrix)
    origin_x_ref, origin_y_ref = ctypes.byref(origin_x), ctypes.byref(origin_y)
    is_generated = raw.FPDFText_IsGenerated
    text_object = raw.FPDFText_GetTextObject
    loose_box = raw.FPDFText_GetLooseCharBox
    char_origin = raw.FPDFText_GetCharOrigin
    get_unicode = raw.FPDFText_GetUnicode
    as_address = ctypes.c_void_p

    position: dict[int, int] = {}
    for rank, (obj, _chain) in enumerate(_objects(raw, page.raw)):
        position[ctypes.cast(obj, as_address).value or 0] = rank

    # Font, flags, size and matrix belong to a character's text object, so
    # they are read once per object rather than once per character.
    per_object: dict[int, tuple[str, bool, float, bool, bool, int]] = {}
    keyed: list[tuple[tuple[int, int], Glyph]] = []
    # Each glyph's character index, and whether its own character (before a
    # slanted one is blanked) was a code below space.
    indices: list[tuple[int, bool]] = []
    unmapped = False
    index = 0
    while index < count:
        code = get_unicode(tp, index)
        # PDFium generates only spaces and line breaks, so a character that is
        # neither needs no question about it.
        if code in (10, 13) or (code == 32 and is_generated(tp, index)):
            index += 1
            continue
        char = chr(code) if code else "\x00"
        step = 1
        # A character outside the BMP is two indices, its UTF-16 halves.
        if 0xD800 <= code < 0xDC00 and index + 1 < count:
            low = get_unicode(tp, index + 1)
            if 0xDC00 <= low < 0xE000:
                char = chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00))
                step = 2
        address = ctypes.cast(text_object(tp, index), as_address).value or 0
        cached = per_object.get(address)
        if cached is None:
            length = raw.FPDFText_GetFontInfo(tp, index, font_buffer, 256, flags_ref)
            font = _base_font(font_buffer.raw[: max(0, length - 1)].decode("utf-8", "replace"))
            raw.FPDFText_GetMatrix(tp, index, matrix_ref)
            # The size MuPDF gave: the font size times the matrix's expansion,
            # so LibreOffice's parentheses (a 12.3 pt font squeezed to 0.48
            # width) are the 8.55 pt glyphs they are drawn as.
            size = (float(raw.FPDFText_GetFontSize(tp, index)) or 10.0) * (
                _math.sqrt(abs(matrix.a * matrix.d - matrix.b * matrix.c)) or 1.0
            )
            # Set left to right on a horizontal baseline. Not PDFium's
            # "angle", which also reports a synthetic oblique's skew
            # (LibreOffice slants DejaVu's vertical bar that way) as rotation.
            upright = matrix.a > 0 and abs(matrix.b) <= 1e-3 * abs(matrix.a)
            skewed = abs(matrix.c) > 1e-3 * abs(matrix.d or 1.0)
            rank = position.get(address, len(position))
            cached = (font, bool(flags.value & _FONT_ITALIC), size, upright, skewed, rank)
            per_object[address] = cached
        font, italic, size, upright, skewed, rank = cached
        loose_box(tp, index, loose_ref)
        char_origin(tp, index, origin_x_ref, origin_y_ref)
        # The box MuPDF gave. PDFium's loose box is the advance box widened to
        # the glyph's ink; upright text starts at its origin and runs its
        # advance (the builtin's, from /Widths, when it read this glyph). A
        # slanted glyph's box is the slanted advance box, which is PDFium's.
        baseline = top - origin_y.value
        x0 = loose.left - left
        x1 = loose.right - left
        if upright and not skewed:
            x0 = origin_x.value - left
            advance = _advance(advances, x0, baseline, char) if advances is not None else None
            if advance is not None:
                x1 = x0 + advance
        glyph = Glyph(
            char=char if upright else " ",
            font=font,
            size=size,
            x0=x0,
            x1=x1,
            baseline=baseline,
            order=0,
            line=0,
            italic=italic,
            box_top=top - loose.top,
            box_bottom=top - loose.bottom,
        )
        coded = char < " "
        unmapped = unmapped or coded
        keyed.append(((rank, index), glyph))
        indices.append((index, coded))
        index += step
    if unmapped:
        # What MuPDF's second, plain reading showed -- and it ran only on a
        # page holding a code below space: a glyph the font could not map
        # reads as U+FFFD in the prose, its code being for the equations.
        map_error = raw.FPDFText_HasUnicodeMapError
        for (_key, glyph), (char_index, coded) in zip(keyed, indices, strict=True):
            if coded or map_error(tp, char_index):
                glyph.plain = "�"
    keyed.sort(key=lambda item: item[0])
    glyphs = [glyph for _, glyph in keyed]
    for order, glyph in enumerate(glyphs):
        glyph.order = order
    rules = _page_rules(raw, page, left, top, width, Rule)
    return _visual_lines(glyphs), rules


def _page_rules(raw: Any, page: Any, left: float, top: float, width: float, rule: Any) -> list[Any]:
    """Thin horizontal fills and strokes, in the glyphs' coordinates."""

    rules: list[Any] = []
    point_x, point_y = ctypes.c_float(), ctypes.c_float()
    for obj, chain in _objects(raw, page.raw):
        if raw.FPDFPageObj_GetType(obj) != _PAGEOBJ_PATH:
            continue
        matrix = _matrix(raw, obj)
        for form in reversed(chain):
            matrix = _compose(matrix, _matrix(raw, form))
        segments = raw.FPDFPath_CountSegments(obj)
        if segments <= 0:
            continue
        subpaths: list[list[tuple[float, float]]] = []
        closed: list[bool] = []
        for index in range(segments):
            segment = raw.FPDFPath_GetPathSegment(obj, index)
            if not segment:
                continue
            raw.FPDFPathSegment_GetPoint(segment, ctypes.byref(point_x), ctypes.byref(point_y))
            x, y = _apply(matrix, point_x.value, point_y.value)
            point = (x - left, top - y)
            kind = raw.FPDFPathSegment_GetType(segment)
            if kind == _SEGMENT_MOVETO or not subpaths:
                subpaths.append([point])
                closed.append(False)
            else:
                subpaths[-1].append(point)
            if raw.FPDFPathSegment_GetClose(segment):
                closed[-1] = True
        for points, is_closed in zip(subpaths, closed, strict=True):
            rect = _as_rect(points, is_closed)
            if rect is not None:
                x0, y0, x1, y1 = rect
                if 1.0 <= x1 - x0 <= 0.6 * width and y1 - y0 <= 1.6:
                    rules.append(rule(x0, x1, (y0 + y1) / 2))
                continue
            for (sx, sy), (ex, ey) in zip(points, points[1:], strict=False):
                if abs(sy - ey) <= 0.3 and 1.0 <= abs(ex - sx) <= 0.6 * width:
                    rules.append(rule(min(sx, ex), max(sx, ex), sy))
    return rules


def _as_rect(
    points: list[tuple[float, float]], closed: bool
) -> tuple[float, float, float, float] | None:
    """The axis-aligned rectangle a subpath draws, if it draws one."""

    corners = list(points)
    if len(corners) == 5 and corners[0] == corners[-1]:
        corners = corners[:4]
    if len(corners) != 4 or not (closed or len(points) == 5):
        return None
    xs = sorted({round(x, 3) for x, _ in corners})
    ys = sorted({round(y, 3) for _, y in corners})
    if len(xs) > 2 or len(ys) > 2:
        return None
    for (ax, ay), (bx, by) in zip(corners, [*corners[1:], corners[0]], strict=True):
        if abs(ax - bx) > 1e-3 and abs(ay - by) > 1e-3:
            return None
    return (xs[0], ys[0], xs[-1], ys[-1])
