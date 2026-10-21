"""Equations in a PDF page, found from its glyphs and written back as LaTeX.

The page half of PDF mathematics (``ingestion.extractor.math: latex``); the
equation half is :mod:`pheasant.ingestion.math_layout`. Every PDF provider
reduces a page to the same two lists — :class:`Glyph` (a character with its
font, size, advance and baseline, in the reader's order) and thin horizontal
rules — and :func:`rewrite_page` returns the page's text with each equation
replaced by ``$…$`` (inline) or ``$$…$$`` (displayed), or ``None`` when the
page holds no mathematics, in which case the caller keeps the text it
already had, byte for byte.

Finding an equation
-------------------
Seeds are glyphs that only occur in mathematics: anything set in a math font
(CMMI, CMSY, CMEX, Cambria Math, OpenSymbol, …), or a mathematical operator
in any font. A Greek letter or an arrow set in the body font is *not* a seed —
"5 μm" and "A → B" are prose. A region grows from its seeds over glyphs that
touch it (side by side within a thin space, or stacked over a rule or an
operator), admitting digits, operators and brackets freely, letters in a
non-body font, and body-font letters only when they spell a function name
(``sin``, ``lim``): TeX sets variables in math italic and words in roman, so a
roman word next to an equation is the sentence around it. A region is
displayed when nothing else shares its lines, apart from an equation number,
which becomes ``\\tag``.

Determinism
-----------
Geometry in, TeX out, through fixed thresholds expressed in units of the
glyph's own size: no model, no rendering, no font file. A page produces the
same text on every run and on every provider that reads the same glyphs.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from statistics import median

from pheasant.ingestion.math_layout import Atom, Rule, layout_to_tex
from pheasant.ingestion.pdf_regions import (
    _TALL,
    Glyph,
    Region,
    _absorb_rows,
    _bold,
    _find_regions,
    _italic,
    _merge_bands,
)
from pheasant.ingestion.tex_symbols import (
    BIG_OPERATORS,
    byte_code,
    decode_glyph,
    font_reads_codes,
    is_math_char,
    is_math_font,
    tex_font_family,
    unshift_bakoma,
)

#: Bumped whenever a change here or in ``math_layout`` changes the text a PDF
#: produces. Folded into the fingerprint of document sources with math on, and
#: into the reader version a split PDF's workers must match.
MATH_VERSION = "1"


#: Extension-font glyphs' height and depth in em (cmex10.tfm), by code: a
#: displayed ∑ hangs well below its origin, which is where its limit sits.
_OMX_BOX = {
    **{code: (0.04, 1.16) for code in range(0x00, 0x10)},
    **{code: (0.04, 1.76) for code in (0x10, 0x11)},
    **{code: (0.04, 2.36) for code in range(0x12, 0x20)},
    **{code: (0.04, 2.96) for code in range(0x20, 0x30)},
    **{code: (0.0, 1.8) for code in range(0x30, 0x44)},
    **{code: (0.0, 1.0) for code in range(0x44, 0x58)},
    **{code: (0.0, 1.4) for code in range(0x58, 0x62)},
    0x52: (0.0, 1.11), 0x5A: (0.0, 2.22), 0x48: (0.0, 1.11), 0x49: (0.0, 2.22),
    0x70: (0.04, 1.16), 0x71: (0.04, 1.76), 0x72: (0.04, 2.36), 0x73: (0.04, 2.96),
}  # fmt: skip


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _decode(glyphs: list[Glyph]) -> None:
    chars: dict[str, list[str]] = defaultdict(list)
    for glyph in glyphs:
        chars[glyph.font].append(glyph.char)
    reads = {
        font: font_reads_codes(tex_font_family(font), "".join(seen), font)
        for font, seen in chars.items()
    }
    for glyph in glyphs:
        reads_codes = reads[glyph.font]
        glyph.tex = "" if glyph.space else decode_glyph(glyph.char, glyph.font, reads_codes)
        height, depth = 0.72, 0.22
        family = tex_font_family(glyph.font)
        if family == "omx":
            code = glyph.code if glyph.code is not None else byte_code(glyph.char)
            if code is not None and (reads_codes or ord(glyph.char) < 0x20):
                height, depth = _OMX_BOX.get(unshift_bakoma(code), (0.04, 1.16))
            else:
                height, depth = (0.0, 1.4)
        elif family == "oms" and glyph.tex == "RADICAL":
            height, depth = 0.04, 0.96  # hangs from its origin, like CMEX's
        glyph.top = glyph.baseline - height * glyph.size
        glyph.bottom = glyph.baseline + depth * glyph.size
        if family is None and glyph.box_top is not None and glyph.box_bottom is not None:
            if glyph.tex in _TALL or glyph.tex in BIG_OPERATORS or glyph.tex == "RADICAL":
                glyph.top = min(glyph.top, glyph.box_top)
                glyph.bottom = max(glyph.bottom, glyph.box_bottom)


# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Display, numbering, TeX
# ---------------------------------------------------------------------------


def _classify(region: Region, glyphs: list[Glyph]) -> None:
    members = set(map(id, region.glyphs))
    top = min(g.top for g in region.glyphs)
    bottom = max(g.bottom for g in region.glyphs)
    x1 = max(g.x1 for g in region.glyphs)
    size = median(g.size for g in region.glyphs)
    others = [
        g for g in glyphs
        if id(g) not in members and not g.space and g.bottom > top + 0.3 * size
        and g.top < bottom - 0.3 * size
    ]  # fmt: skip
    if not others:
        region.display = True
        return
    tag = [g for g in others if g.x0 > x1 + 1.5 * size]
    text = "".join(g.char for g in sorted(tag, key=lambda g: g.x0))
    if len(tag) == len(others) and re.fullmatch(r"\(\s*[\w.\-]{1,8}\s*\)", text):
        region.display = True
        region.tag = tag


def _atom(glyph: Glyph, text: bool = False) -> Atom:
    tex = glyph.tex
    if len(tex) == 1 and tex.isalpha() and _bold(glyph) and not is_math_font(glyph.font):
        tex = r"\mathbf{" + tex + "}"
    return Atom(
        tex=tex,
        x0=glyph.x0,
        x1=glyph.x1,
        top=glyph.top,
        bottom=glyph.bottom,
        baseline=glyph.baseline,
        size=glyph.size,
        upright=not _italic(glyph) and not is_math_font(glyph.font),
        limits=glyph.tex in BIG_OPERATORS,
        text=text,
    )


def _clean(tex: str) -> str:
    tex = tex.replace("RADICAL", r"\sqrt{}")
    # TeX draws ≠ and ∉ as a slash over the relation.
    tex = re.sub(r"\\not\s*=", r"\\neq ", tex)
    tex = re.sub(r"\\not\s*\\in(?![A-Za-z])", r"\\notin ", tex)
    tex = re.sub(r"(ACCENT|PIECE):\\?[A-Za-z]*\{?", "", tex)
    tex = re.sub(r"\s+", " ", tex).strip()
    depth = 0
    for char in re.sub(r"\\[{}]", "", tex):
        depth += {"{": 1, "}": -1}.get(char, 0)
        if depth < 0:
            return ""
    return tex + "}" * depth


def _region_tex(region: Region) -> str:
    atoms = [_atom(g, id(g) in region.text) for g in region.glyphs]
    tex = _clean(layout_to_tex(atoms, region.rules))
    if region.tag:
        label = "".join(g.char for g in sorted(region.tag, key=lambda g: g.x0)).strip("() ")
        tex += r" \tag{" + label + "}"
    return tex


def _anchor(region: Region) -> Glyph:
    """Where the equation is written: its leftmost glyph not stacked over or
    under a bar (a leading fraction's numerator is read on the line above)."""

    def under(rule: Rule, glyph: Glyph) -> bool:
        centre = (glyph.x0 + glyph.x1) / 2
        return (
            rule.x0 - 0.5 <= centre <= rule.x1 + 0.5
            and abs(glyph.baseline - rule.y) <= 1.6 * glyph.size
        )

    # Fraction bars only (glyphs on both sides): a radicand under its
    # vinculum is on the equation's baseline.
    bars = [
        rule for rule in region.rules
        if any(under(rule, g) and g.baseline < rule.y for g in region.glyphs)
        and any(under(rule, g) and g.baseline > rule.y + 0.3 * g.size for g in region.glyphs)
    ]  # fmt: skip

    def stacked(glyph: Glyph) -> bool:
        return any(under(rule, glyph) for rule in bars)

    level = [g for g in region.glyphs if not stacked(g)] or region.glyphs
    biggest = max(g.size for g in level)
    level = [g for g in level if g.size >= 0.8 * biggest]
    # On the equation's own baseline, so it is read where its sentence is: a
    # radical sign or a grown parenthesis hangs from an origin above it.
    ordinary = [g for g in level if g.tex not in _TALL and g.tex != "RADICAL"] or level
    weights: dict[int, float] = defaultdict(float)
    for glyph in ordinary:
        weights[round(glyph.baseline)] += glyph.x1 - glyph.x0
    main = max(weights.items(), key=lambda item: item[1])[0]
    on_main = [g for g in ordinary if abs(g.baseline - main) <= 0.25 * g.size] or level
    return min(on_main, key=lambda g: (g.x0, g.order))


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def page_may_hold_math(text: str, fonts: list[str]) -> bool:
    """A cheap gate before reading a page's glyphs: a math font among its
    fonts, or a mathematical character in its plain text. A page with
    neither (most prose) costs one font listing, not a layout analysis."""

    return any(is_math_font(font) for font in fonts) or any(
        is_math_char(char) or char < " " and char not in "\n\t" for char in text
    )


def find_equations(glyphs: list[Glyph], rules: list[Rule]) -> list[Region]:
    """Every equation on a page, each with its TeX and its place in the text."""

    _decode(glyphs)
    found, mathlike = _find_regions(glyphs, rules)
    regions = _absorb_rows(_merge_bands(found, glyphs, mathlike), glyphs)
    for region in regions:
        _classify(region, glyphs)
        region.tex = _region_tex(region)
        region.anchor = _anchor(region)
    return [region for region in regions if region.tex]


def rewrite_page(glyphs: list[Glyph], rules: list[Rule]) -> str | None:
    """The page's text with equations as LaTeX, or ``None`` when it has none.

    Lines are the reader's (``Glyph.line``, in order of first appearance) and
    characters keep the reader's order, so the prose around an equation reads
    exactly as the reader assembled it. An equation is written once, where
    its first atom on the main baseline was read, and its other glyphs —
    scripts, limits, a numerator on the line above — are taken out of the
    lines they were read into.
    """
    regions = find_equations(glyphs, rules)
    if not regions:
        return None
    owner: dict[int, Region] = {}
    for region in regions:
        for glyph in [*region.glyphs, *region.tag]:
            owner[id(glyph)] = region
    lines: dict[int, list[Glyph]] = defaultdict(list)
    for glyph in sorted(glyphs, key=lambda g: g.order):
        lines[glyph.line].append(glyph)
    out: list[_Line] = []
    for line in lines.values():
        current = _Line()
        for position, glyph in enumerate(line):
            region = owner.get(id(glyph))
            if region is None:
                if glyph.space and _inside(line, position, owner):
                    continue
                char = glyph.char if glyph.plain is None else glyph.plain
                current.add(char, glyph.x0, glyph.x1, glyph.baseline, glyph.size)
                continue
            if glyph is not region.anchor:
                continue
            x0 = min(g.x0 for g in region.glyphs)
            x1 = max(g.x1 for g in region.glyphs)
            if region.display:
                current.display = True
                current.add("\n$$" + region.tex + "$$\n", x0, x1, glyph.baseline, glyph.size)
            else:
                current.math = True
                current.add("$" + region.tex + "$", x0, x1, glyph.baseline, glyph.size)
        if current.text.strip():
            if out and out[-1].continued_by(current):
                out[-1].extend(current)
            else:
                out.append(current)
    return "\n".join(line.text for line in out)


@dataclass
class _Line:
    """One output line, with where it starts and ends on the page."""

    text: str = ""
    x0: float | None = None
    x1: float = 0.0
    first_baseline: float = 0.0
    last_baseline: float = 0.0
    size: float = 10.0
    math: bool = False
    display: bool = False

    def add(self, text: str, x0: float, x1: float, baseline: float, size: float) -> None:
        if self.x0 is None and not text.strip():
            self.text += text  # a leading space says nothing about position
            return
        if self.x0 is None:
            self.x0, self.first_baseline, self.size = x0, baseline, size
        self.text += text
        self.x1 = max(self.x1, x1)
        self.last_baseline = baseline

    def continued_by(self, other: _Line) -> bool:
        """Whether a reader split one visual line around an equation.

        pymupdf breaks a line where a stacked fraction interrupts it, leaving
        "." or "for data …" on a line of its own; a line that carries inline
        math and the line read after it, on the same baseline and to its
        right, are one line.
        """
        if self.display or other.display or not (self.math or other.math) or other.x0 is None:
            return False
        same = abs(other.first_baseline - self.last_baseline) <= 0.35 * max(self.size, other.size)
        return same and other.x0 >= self.x1 - 2

    def extend(self, other: _Line) -> None:
        head, tail = self.text.rstrip(), other.text.lstrip()
        joiner = "" if tail[:1] in ".,;:)" or head.endswith("(") else " "
        self.text = head + joiner + tail
        self.x1 = max(self.x1, other.x1)
        self.last_baseline = other.last_baseline
        self.math = self.math or other.math


def _inside(line: list[Glyph], position: int, owner: dict[int, Region]) -> bool:
    """A space between two glyphs of one equation is the equation's spacing."""

    before = next((g for g in reversed(line[:position]) if not g.space), None)
    after = next((g for g in line[position + 1 :] if not g.space), None)
    if before is None or after is None:
        return False
    region = owner.get(id(before))
    return region is not None and owner.get(id(after)) is region
