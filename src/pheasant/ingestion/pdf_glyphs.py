"""A content stream as positioned glyphs and rules, for the builtin readers.

The native reader gets glyph positions from MuPDF. The builtin and sandboxed
readers get them here, by interpreting a page's content stream the way a
viewer does — enough of it to place every glyph: the graphics state
(``q``/``Q``/``cm``), the text state (``Tf``, ``Tc``, ``Tw``, ``Tz``, ``TL``,
``Ts``), the text matrices (``BT``, ``Tm``, ``Td``, ``TD``, ``T*``), the show
operators with each glyph's real advance from the font's widths, thin filled
or stroked lines (fraction bars), and form XObjects (``Do``). The result is
the same :class:`~pheasant.ingestion.pdf_math.Glyph` and
:class:`~pheasant.ingestion.math_layout.Rule` lists the native reader
produces, so one equation reader serves every provider.

It walks tokens, never bytes (:mod:`pheasant.ingestion.pdf_objects`), so the
sandboxed provider hands it tokens its guest produced.

``Glyph.line`` follows the builtin scanner's own line breaks — a new line at
every show operator and text move — so a page with an equation reads like the
builtin's text everywhere but the equation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pheasant.ingestion.math_layout import Rule
from pheasant.ingestion.pdf_math import Glyph
from pheasant.ingestion.pdf_objects import (
    ARRAY_CLOSE,
    ARRAY_OPEN,
    HEXSTRING,
    KEYWORD,
    NAME,
    NUMBER,
    STRING,
    Font,
    PdfDocument,
    Token,
    page_fonts,
)

MAX_FORM_DEPTH = 4
MAX_GLYPHS = 200_000

Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def multiply(a: Matrix, b: Matrix) -> Matrix:
    return (
        a[0] * b[0] + a[1] * b[2],
        a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2],
        a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4],
        a[4] * b[1] + a[5] * b[3] + b[5],
    )


def apply(m: Matrix, x: float, y: float) -> tuple[float, float]:
    return x * m[0] + y * m[2] + m[4], x * m[1] + y * m[3] + m[5]


@dataclass
class _TextState:
    font: Font | None = None
    size: float = 10.0
    char_spacing: float = 0.0
    word_spacing: float = 0.0
    scale: float = 1.0
    leading: float = 0.0
    rise: float = 0.0


class _Interpreter:
    def __init__(self, document: PdfDocument, height: float, cache: dict) -> None:
        self.document = document
        self.height = height
        self.cache = cache
        self.glyphs: list[Glyph] = []
        self.rules: list[Rule] = []
        self.line = 0
        self.order = 0

    def run(self, tokens: list[Token], resources: dict, ctm: Matrix, depth: int = 0) -> None:
        fonts = page_fonts(self.document, resources, self.cache)
        xobjects = self.document.get(resources.get(b"XObject")) or {}
        stack: list[tuple[Matrix, _TextState, float]] = []
        state = _TextState()
        line_width = 1.0
        tm = tlm = IDENTITY
        operands: list[Any] = []
        path: list[tuple[str, float, float, float, float]] = []
        array: list[Any] | None = None
        for kind, raw in tokens:
            if len(self.glyphs) >= MAX_GLYPHS:
                return
            if kind == ARRAY_OPEN:
                array = []
                continue
            if kind == ARRAY_CLOSE:
                operands.append(array or [])
                array = None
                continue
            if kind in (NUMBER, STRING, HEXSTRING, NAME):
                value: Any = raw if kind != NUMBER else _number(raw)
                if kind == NAME:
                    value = ("name", raw)
                (array if array is not None else operands).append(value)
                continue
            if kind != KEYWORD:
                continue
            op = raw
            args = operands
            operands = []
            if op == b"q":
                stack.append((ctm, _copy(state), line_width))
            elif op == b"Q":
                if stack:
                    ctm, state, line_width = stack.pop()
            elif op == b"cm" and _numbers(args, 6):
                ctm = multiply(tuple(args[-6:]), ctm)  # type: ignore[arg-type]
            elif op == b"w" and _numbers(args, 1):
                line_width = float(args[-1])
            elif op == b"BT":
                tm = tlm = IDENTITY
            elif op == b"ET":
                self.line += 1
            elif op == b"Tf" and len(args) >= 2 and isinstance(args[-2], tuple):
                state.font = fonts.get(args[-2][1])
                state.size = float(args[-1]) if isinstance(args[-1], (int, float)) else state.size
            elif op == b"Tc" and _numbers(args, 1):
                state.char_spacing = float(args[-1])
            elif op == b"Tw" and _numbers(args, 1):
                state.word_spacing = float(args[-1])
            elif op == b"Tz" and _numbers(args, 1):
                state.scale = float(args[-1]) / 100.0
            elif op == b"TL" and _numbers(args, 1):
                state.leading = float(args[-1])
            elif op == b"Ts" and _numbers(args, 1):
                state.rise = float(args[-1])
            elif op in (b"Td", b"TD") and _numbers(args, 2):
                tx, ty = float(args[-2]), float(args[-1])
                if op == b"TD":
                    state.leading = -ty
                tlm = multiply((1, 0, 0, 1, tx, ty), tlm)
                tm = tlm
                self.line += 1
            elif op == b"Tm" and _numbers(args, 6):
                tm = tlm = tuple(float(a) for a in args[-6:])  # type: ignore[assignment]
                self.line += 1
            elif op == b"T*":
                tlm = multiply((1, 0, 0, 1, 0, -state.leading), tlm)
                tm = tlm
                self.line += 1
            elif op in (b"Tj", b"'", b'"', b"TJ"):
                if op in (b"'", b'"'):
                    if op == b'"' and _numbers(args[:2], 2):
                        state.word_spacing, state.char_spacing = float(args[0]), float(args[1])
                    tlm = multiply((1, 0, 0, 1, 0, -state.leading), tlm)
                    tm = tlm
                    self.line += 1
                parts = (
                    args[-1]
                    if op == b"TJ" and args and isinstance(args[-1], list)
                    else ([args[-1]] if args and isinstance(args[-1], bytes) else [])
                )
                tm = self._show(parts, state, tm, ctm)
                self.line += 1
            elif op == b"re" and _numbers(args, 4):
                x, y, w, h = (float(a) for a in args[-4:])
                path.append(("re", x, y, w, h))
            elif op == b"m" and _numbers(args, 2):
                path.append(("m", float(args[-2]), float(args[-1]), 0, 0))
            elif op == b"l" and _numbers(args, 2):
                path.append(("l", float(args[-2]), float(args[-1]), 0, 0))
            elif op in (b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"S", b"s"):
                self._paint(path, op, ctm, line_width)
                path = []
            elif op == b"n":
                path = []
            elif op == b"Do" and args and isinstance(args[-1], tuple) and depth < MAX_FORM_DEPTH:
                self._form(xobjects, args[-1][1], ctm, resources, depth)

    def _show(self, parts: list[Any], state: _TextState, tm: Matrix, ctm: Matrix) -> Matrix:
        font = state.font
        if font is None:
            return tm
        for part in parts:
            if isinstance(part, (int, float)):
                shift = -float(part) / 1000.0 * state.size * state.scale
                tm = multiply((1, 0, 0, 1, shift, 0), tm)
                continue
            if not isinstance(part, bytes):
                continue
            for code, text in font.decode(part):
                advance = font.width(code) * state.size + state.char_spacing
                if code == 32 and not font.two_byte:
                    advance += state.word_spacing
                advance *= state.scale
                # The glyph runs from its origin to the advance, both in text
                # space, raised by the rise; the size is the font size scaled
                # by the matrices' vertical factor.
                device = multiply(tm, ctm)
                x0, y0 = apply(device, 0, state.rise)
                x1, _ = apply(device, advance, state.rise)
                size = abs(state.size * (device[3] or device[1])) or state.size
                pieces = text or "�"
                step = (x1 - x0) / len(pieces)
                for index, char in enumerate(pieces):
                    self.glyphs.append(
                        Glyph(
                            char=char,
                            font=font.name,
                            size=size,
                            x0=min(x0 + step * index, x0 + step * (index + 1)),
                            x1=max(x0 + step * index, x0 + step * (index + 1)),
                            baseline=self.height - y0,
                            order=self.order,
                            line=self.line,
                            code=code if not font.two_byte else None,
                        )
                    )
                    self.order += 1
                tm = multiply((1, 0, 0, 1, advance, 0), tm)
        return tm

    def _paint(self, path: list[tuple[str, float, float, float, float]], op: bytes, ctm: Matrix,
               line_width: float) -> None:  # fmt: skip
        filled = op not in (b"S", b"s")
        start: tuple[float, float] | None = None
        for kind, x, y, w, h in path:
            if kind == "re" and filled:
                x0, y0 = apply(ctm, x, y)
                x1, y1 = apply(ctm, x + w, y + h)
                if abs(y1 - y0) <= 1.6 and abs(x1 - x0) >= 1.0:
                    self.rules.append(Rule(min(x0, x1), max(x0, x1), self.height - (y0 + y1) / 2))
            elif kind == "m":
                start = (x, y)
            elif kind == "l" and start is not None:
                x0, y0 = apply(ctm, *start)
                x1, y1 = apply(ctm, x, y)
                thickness = line_width * abs(ctm[3] or 1.0)
                if abs(y1 - y0) <= 0.3 and abs(x1 - x0) >= 1.0 and thickness <= 1.6 and not filled:
                    self.rules.append(Rule(min(x0, x1), max(x0, x1), self.height - y0))
                start = (x, y)

    def _form(self, xobjects: Any, name: bytes, ctm: Matrix, resources: dict, depth: int) -> None:
        if not isinstance(xobjects, dict):
            return
        ref = xobjects.get(name)
        entry = self.document.objects.get(getattr(ref, "number", -1))
        header = entry.value if entry is not None else None
        if not isinstance(header, dict) or header.get(b"Subtype") != b"Form":
            return
        data = self.document.stream(ref)
        if not data:
            return
        matrix = self.document.get(header.get(b"Matrix"))
        form_ctm = ctm
        if (
            isinstance(matrix, list)
            and len(matrix) == 6
            and all(isinstance(v, (int, float)) for v in matrix)
        ):
            form_ctm = multiply(tuple(float(v) for v in matrix), ctm)  # type: ignore[arg-type]
        own = self.document.get(header.get(b"Resources"))
        self.run(
            self.document.lexer(data),
            own if isinstance(own, dict) else resources,
            form_ctm,
            depth + 1,
        )


def _number(raw: bytes) -> float | int:
    try:
        return float(raw) if b"." in raw else int(raw)
    except ValueError:
        return 0


def _numbers(args: list[Any], count: int) -> bool:
    return len(args) >= count and all(isinstance(a, (int, float)) for a in args[-count:])


def _copy(state: _TextState) -> _TextState:
    return _TextState(**vars(state))


def page_glyphs(document: PdfDocument, page: Any, cache: dict) -> tuple[list[Glyph], list[Rule]]:
    """One page of :func:`pheasant.ingestion.pdf_objects.pages` as glyphs and rules."""

    interpreter = _Interpreter(document, page.height, cache)
    tokens: list[Token] = []
    for data in page.contents:
        # A page's content streams are one program split across objects.
        tokens.extend(document.lexer(data + b"\n"))
    interpreter.run(tokens, page.resources, IDENTITY)
    return _visual_lines(interpreter.glyphs), interpreter.rules


def _visual_lines(glyphs: list[Glyph]) -> list[Glyph]:
    """Lines as a reader sees them, with the spaces a PDF leaves implicit.

    A content stream's operators say little about lines: matplotlib shows
    each glyph with its own move, and TeX writes a word space as a kerning
    number inside ``TJ``. In reading order, a glyph starts a new line when it
    steps back to the left, or moves up or down without carrying on to the
    right; a gap of a fifth of its size between two glyphs on a line is a
    space — the rule pymupdf uses for the same job. Only a page holding an equation is
    rebuilt this way; any other page keeps the scanner's text.
    """
    out: list[Glyph] = []
    line = 0
    previous: Glyph | None = None
    order = 0
    for glyph in sorted(glyphs, key=lambda g: g.order):
        if previous is not None:
            moved = abs(glyph.baseline - previous.baseline) > 0.5 * max(glyph.size, previous.size)
            gap = glyph.x0 - previous.x1
            # Moving up or down while carrying on to the right is a script, a
            # radical or a numerator on the same line; a new line starts back
            # to the left or well clear of where the last one ended.
            continuing = -0.5 * glyph.size <= gap <= 2 * glyph.size
            if glyph.x0 < previous.x0 - 2 * glyph.size or moved and not continuing:
                line += 1
            elif (
                gap > 0.2 * glyph.size
                and not moved
                and glyph.char.strip()
                and previous.char.strip()
            ):
                out.append(
                    Glyph(" ", previous.font, previous.size, previous.x1, glyph.x0,
                          previous.baseline, order, line)
                )  # fmt: skip
                order += 1
        glyph.line = line
        glyph.order = order
        order += 1
        out.append(glyph)
        previous = glyph
    return out


def math_text(content: bytes, lexer: Any = None, scan: Any = None) -> str | None:
    """A PDF's text with equations as LaTeX, or ``None`` when it has none.

    ``None`` sends the caller back to the text it always produced, so a
    document with no equation in it is byte-identical with math on. A page
    with none is its streams' scanned text, as before; a page with one is
    rewritten from its glyphs. ``lexer`` and ``scan`` are the sandboxed
    provider's guest-backed twins when it calls this.
    """
    from pheasant.ingestion.extractor import scan_pdf_content_stream
    from pheasant.ingestion.pdf_math import page_may_hold_math, rewrite_page
    from pheasant.ingestion.pdf_objects import lex_pdf, pages, read_document

    lexer = lexer or lex_pdf
    scan = scan or scan_pdf_content_stream
    document = read_document(content, lexer)
    cache: dict = {}
    parts: list[str] = []
    found = False
    for page in pages(document):
        scanned = "\n".join(text for text in (scan(data) for data in page.contents) if text.strip())
        fonts = [font.name for font in page_fonts(document, page.resources, cache).values()]
        rewritten = None
        if page_may_hold_math(scanned, fonts):
            glyphs, rules = page_glyphs(document, page, cache)
            rewritten = rewrite_page(glyphs, rules) if glyphs else None
        if rewritten is None:
            parts.append(scanned)
        else:
            found = True
            parts.append(rewritten)
    return "\n".join(part for part in parts if part.strip()) if found else None
