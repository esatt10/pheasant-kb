"""Where a PDF page's equations are: glyphs grouped into regions.

The finding half of PDF mathematics (:mod:`pheasant.ingestion.pdf_math` turns
each region into TeX and splices it into the page). Seeds are glyphs only
mathematics uses — a math font, an operator — and a region grows from them over
glyphs that touch it; thin rules join what is centred on them (a fraction) or
spanned by them (an overline); regions side by side with nothing but
mathematics between them are one equation; and a tall equation that begins
its lines owns what is on them. Every rule here is geometry in units of the
glyph's own size, so the same page always groups the same way.

The thresholds are measured, not guessed: each one is written beside the
typesetting it has to tell apart (two lines of a tight paragraph a point
apart, a relation's stretched space in a justified column, an office equation
editor's padding around a delimiter).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from statistics import median

from pheasant.ingestion.math_layout import Rule
from pheasant.ingestion.tex_symbols import (
    BIG_OPERATORS,
    FUNCTION_NAMES,
    RELATIONS,
    base_font_name,
    is_math_char,
    is_math_font,
    tex_font_family,
    unicode_to_tex,
)

# Characters that in a body font still only mean mathematics.
_HARD_MATH = set("∑∏∫∮∂∇∞√≤≥≠≈≡∈∉⊂⊃⊆⊇∪∩∀∃∝⊗⊕⟨⟩")


_BULLETS = set("•▪◦‣∙·●○■□▶►–—")


_WEAK = set("0123456789+-=()[]{}<>|/.,;:!'*^_~")


_TRAILING = set(".,;:")


_TALL = {"(", ")", "[", "]", r"\{", r"\}", "|", r"\|", r"\langle", r"\rangle", r"\lfloor",
         r"\rfloor", r"\lceil", r"\rceil"}  # fmt: skip


_PRIVATE_USE_START, _PRIVATE_USE_END = "\ue000", "\uf8ff"


@dataclass
class Glyph:
    """One shown character as a reader saw it (y grows downward)."""

    char: str
    font: str
    size: float
    x0: float
    x1: float
    baseline: float
    #: Reading order and line, as the reader assembles its text.
    order: int
    line: int
    italic: bool = False
    #: The raw byte code, when the reader knows it (the builtin does).
    code: int | None = None
    #: The reader's own vertical extent, when it has one. Only used for glyphs
    #: that can be tall (delimiters, radicals, operators), where the size alone
    #: does not say how tall: LibreOffice draws a matrix's parentheses as one
    #: glyph scaled up.
    box_top: float | None = None
    box_bottom: float | None = None
    #: What the reader's plain text shows for this glyph, when that differs
    #: from ``char`` (which may be a raw code): prose is written as it was.
    plain: str | None = None
    # Filled in by `rewrite_page`.
    tex: str = field(default="", compare=False)
    top: float = field(default=0.0, compare=False)
    bottom: float = field(default=0.0, compare=False)

    @property
    def space(self) -> bool:
        return not self.char.strip()


@dataclass
class Region:
    glyphs: list[Glyph]
    rules: list[Rule]
    #: Prose glyphs taken into the equation (a ``cases`` row's "if").
    text: set[int] = field(default_factory=set)
    display: bool = False
    tag: list[Glyph] = field(default_factory=list)
    tex: str = ""
    anchor: Glyph | None = None


@lru_cache(maxsize=4096)
def _family_key(font: str) -> str:
    """Fonts of one typeface share a key: ``Caladea-Italic`` is ``caladea``."""

    base = base_font_name(font).lower()
    if tex_font_family(base) == "ot1":
        return "cm-text"
    base = re.split(r"[-,+]", base)[0]
    return re.sub(r"(mt|ps|std|pro|\d+|bold|italic|oblique|regular|roman)+$", "", base) or base


@lru_cache(maxsize=4096)
def _bold_font(font: str) -> bool:
    return bool(re.search(r"bold|black|heavy|^cmb|^cmbx|^lmbx", base_font_name(font).lower()))


@lru_cache(maxsize=4096)
def _italic_font(font: str) -> bool:
    return bool(re.search(r"italic|oblique|-it\b|cmmi|cmti|lmmi", base_font_name(font).lower()))


def _bold(glyph: Glyph) -> bool:
    return _bold_font(glyph.font)


def _italic(glyph: Glyph) -> bool:
    return glyph.italic or _italic_font(glyph.font)


class _Grid:
    """A coarse spatial index, so growing a region is not quadratic in a page."""

    def __init__(self, cell: float) -> None:
        self.cell = cell
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)

    def add(self, index: int, x0: float, top: float, x1: float, bottom: float) -> None:
        for cx in range(int(x0 // self.cell), int(x1 // self.cell) + 1):
            for cy in range(int(top // self.cell), int(bottom // self.cell) + 1):
                self.cells[(cx, cy)].append(index)

    def near(self, x0: float, top: float, x1: float, bottom: float) -> set[int]:
        found: set[int] = set()
        for cx in range(int(x0 // self.cell), int(x1 // self.cell) + 1):
            for cy in range(int(top // self.cell), int(bottom // self.cell) + 1):
                found.update(self.cells.get((cx, cy), ()))
        return found


def _touch(a: Glyph, b: Glyph) -> bool:
    size = max(a.size, b.size)
    dx = max(0.0, max(a.x0, b.x0) - min(a.x1, b.x1))
    dy = max(0.0, max(a.top, b.top) - min(a.bottom, b.bottom))
    overlap = min(a.x1, b.x1) - max(a.x0, b.x0)
    if overlap > 0.2 * min(a.x1 - a.x0, b.x1 - b.x0):
        # One over the other. Only an accent, a limit (set smaller than what
        # it limits) or a big operator's limit stacks; two full-size glyphs
        # one above the other are two lines, which in a tight paragraph can
        # come within a point of each other.
        if a.tex in BIG_OPERATORS or b.tex in BIG_OPERATORS:
            return dy <= 0.5 * max(a.size, b.size)
        if a.tex.startswith("PIECE:") and b.tex.startswith("PIECE:"):
            return dy <= 0.3 * max(a.size, b.size)  # one delimiter built from parts
        if a.tex.startswith("ACCENT:") or b.tex.startswith("ACCENT:"):
            return dy <= 0.25 * max(a.size, b.size)
        if min(a.size, b.size) <= 0.8 * max(a.size, b.size):
            return dy <= 0.15 * min(a.size, b.size)
        return False
    # TeX's space around a relation stretches to about half an em in a
    # justified line; around anything else a third of an em is the most.
    reach = (
        0.6
        if a.tex in _SPACED or b.tex in _SPACED
        # Office equation editors pad a delimiter (‖v‖), and the space after
        # a script (Xᵀ y), more than TeX does.
        else (
            0.45
            if a.tex in _DELIMITER_TEX
            or b.tex in _DELIMITER_TEX
            or min(a.size, b.size) <= 0.8 * size
            else 0.3
        )
    )
    # Side by side on one line, boxes overlap vertically (a script overlaps
    # its base); glyphs on two lines do not, though in a tight paragraph they
    # can come within a fraction of a point.
    shared = min(a.bottom, b.bottom) - max(a.top, b.top)
    return dx <= reach * size and shared > 0


def _touches_rule(glyph: Glyph, rule: Rule) -> bool:
    centre = (glyph.x0 + glyph.x1) / 2
    if not rule.x0 - 0.5 <= centre <= rule.x1 + 0.5:
        return False
    return min(abs(glyph.bottom - rule.y), abs(glyph.top - rule.y)) <= 0.5 * glyph.size or (
        glyph.top <= rule.y <= glyph.bottom
    )


def _words(glyphs: list[Glyph]) -> tuple[dict[int, str], set[int]]:
    """For each letter glyph, the letter run it belongs to on its line; and
    the glyphs of runs written straight against an opening parenthesis.

    A run is letters on one baseline at one size with no space between them,
    so a limit set under ``lim`` is not part of the word. A roman word that
    touches ``(`` (``Var(``, ``rank(``) is an operator name: prose leaves a
    space before a parenthesis.
    """

    by_line: dict[int, list[Glyph]] = defaultdict(list)
    for glyph in glyphs:
        by_line[glyph.line].append(glyph)
    word_of: dict[int, str] = {}
    operators: set[int] = set()
    for line in by_line.values():
        ordered = sorted(line, key=lambda g: (round(g.baseline / max(g.size, 1.0) * 4), g.x0))
        run: list[Glyph] = []
        for glyph in [*ordered, None]:
            if (
                glyph is not None
                and glyph.char.isalpha()
                and (
                    not run
                    or glyph.x0 - run[-1].x1 <= 0.12 * glyph.size
                    and abs(glyph.baseline - run[-1].baseline) <= 0.1 * glyph.size
                    and abs(glyph.size - run[-1].size) <= 0.1 * glyph.size
                )
            ):
                run.append(glyph)
                continue
            word = "".join(g.char for g in run)
            for member in run:
                word_of[id(member)] = word
            if (
                run
                and glyph is not None
                and glyph.char == "("
                and len(word) >= 2
                and (glyph.x0 - run[-1].x1 <= 0.08 * glyph.size)
            ):
                operators.update(id(member) for member in run)
            run = [glyph] if glyph is not None and glyph.char.isalpha() else []
    return word_of, operators


def _find_regions(glyphs: list[Glyph], rules: list[Rule]) -> tuple[list[Region], set[int]]:
    """The page's equations, and the ids of every glyph that could be math."""

    ink = [g for g in glyphs if not g.space and g.tex]
    if not ink:
        return [], set()
    # Only upright letters vote: an equation's italic variables can outnumber
    # the prose on a page dense with mathematics.
    letters = [g for g in ink if g.char.isalpha() and not is_math_font(g.font) and not _italic(g)]
    families: dict[str, int] = defaultdict(int)
    for glyph in letters:
        families[_family_key(glyph.font)] += 1
    body = max(families.items(), key=lambda item: item[1])[0] if families else ""
    words, operators = _words(ink)

    def in_body(glyph: Glyph) -> bool:
        return _family_key(glyph.font) == body and not is_math_font(glyph.font)

    def bullet(glyph: Glyph) -> bool:
        # A TeX font's byte that a reader showed as "∙" is ≤, not a bullet;
        # a private-use character nothing decoded is a dingbat.
        if _PRIVATE_USE_START <= glyph.char <= _PRIVATE_USE_END and glyph.tex == glyph.char:
            return True
        if is_math_font(glyph.font) and glyph.char in "·∙":
            return False  # \cdot in a math font
        return glyph.char in _BULLETS and glyph.tex == unicode_to_tex(glyph.char)

    grid = _Grid(24.0)
    for index, glyph in enumerate(ink):
        grid.add(index, glyph.x0, glyph.top, glyph.x1, glyph.bottom)
    scripted = _scripted(ink, grid, in_body)

    def strong(glyph: Glyph) -> bool:
        if bullet(glyph):
            return False
        if is_math_font(glyph.font):
            return glyph.tex not in (".", ",", ";", ":")
        if in_body(glyph):
            return glyph.char in _HARD_MATH
        return is_math_char(glyph.char) or id(glyph) in scripted

    def _admissible(glyph: Glyph) -> bool:
        if (
            strong(glyph)
            or glyph.char in _WEAK
            or glyph.tex.startswith(("\\", "ACCENT:", "RADICAL"))
        ):
            return not bullet(glyph)
        if glyph.char.isalpha():
            word = words.get(id(glyph), "")
            # A bold roman letter alone is \mathbf{E}; a roman word is prose.
            return (
                not in_body(glyph)
                or word in FUNCTION_NAMES
                or id(glyph) in operators
                or (len(word) == 1 and _bold(glyph))
            )
        return False

    # Fixed per glyph for the page: decided once, read many times.
    position = {id(glyph): index for index, glyph in enumerate(ink)}
    seeds = [strong(glyph) for glyph in ink]
    capable = [_admissible(glyph) for glyph in ink]

    def admissible(glyph: Glyph) -> bool:
        return capable[position[id(glyph)]]

    parent = list(range(len(ink)))
    in_math = [False] * len(ink)

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def join(a: int, b: int) -> None:
        parent[root(a)] = root(b)

    def grow(starts: list[int]) -> None:
        """Spread from ``starts`` over touching math-capable glyphs, joining
        any equation met on the way."""

        for index in starts:
            in_math[index] = True
        queue = list(starts)
        while queue:
            index = queue.pop()
            current = ink[index]
            pad = 0.6 * current.size
            for other in grid.near(
                current.x0 - pad, current.top - pad, current.x1 + pad, current.bottom + pad
            ):
                if other == index or not capable[other] or not _touch(current, ink[other]):
                    continue
                join(index, other)
                if not in_math[other]:
                    in_math[other] = True
                    queue.append(other)

    grow([index for index in range(len(ink)) if seeds[index]])

    # Bars, narrowest first so an inner fraction is whole before its outer
    # one is judged. A bar joins what is above and below it when the two are
    # centred on it (a fraction) or one side spans it (an overline, a
    # vinculum); either way its sides are judged as whole equations, since a
    # denominator can touch its bar through an inner numerator alone.
    for rule in sorted(rules, key=lambda r: r.x1 - r.x0):
        near = [i for i, g in enumerate(ink) if _touches_rule(g, rule) and admissible(g)]
        if not near:
            continue
        components: dict[int, list[int]] = defaultdict(list)
        for index in range(len(ink)):
            if in_math[index]:
                components[root(index)].append(index)
        sides: dict[bool, list[Glyph]] = {True: [], False: []}
        seen: set[int] = set()
        for index in near:
            group = components.get(root(index), [index]) if in_math[index] else [index]
            for member in group:
                if member in seen:
                    continue
                seen.add(member)
                glyph = ink[member]
                sides[(glyph.top + glyph.bottom) / 2 < rule.y].append(glyph)
        above = [g for g in sides[True] if _touches_rule(g, rule) or in_math[position[id(g)]]]
        below = [g for g in sides[False] if _touches_rule(g, rule) or in_math[position[id(g)]]]
        if not _bar(rule, above, below, any(in_math[i] for i in near)):
            continue
        fresh = [index for index in near if not in_math[index]]
        for index in near[1:]:
            join(index, near[0])
        grow(fresh)

    out: list[Region] = []
    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(ink)):
        if in_math[index]:
            groups[root(index)].append(index)
    region_of = {index: key for key, members in groups.items() for index in members}
    for members in groups.values():
        group = [ink[i] for i in sorted(members)]
        _absorb_enclosed(group, ink, region_of, admissible, grid)
        group = _trim(group, in_body)
        if not group:
            continue
        x0, x1 = min(g.x0 for g in group), max(g.x1 for g in group)
        top, bottom = min(g.top for g in group), max(g.bottom for g in group)
        own = [r for r in rules if top - 2 <= r.y <= bottom + 2
               and min(r.x1, x1) - max(r.x0, x0) >= 0.6 * (r.x1 - r.x0)]  # fmt: skip
        if len(group) == 1 and group[0].tex in ("-", "|", "/", "'"):
            continue
        if not own and _flat_prose(group, in_body):
            continue
        if not own and not any(seeds[position[id(g)]] for g in group if id(g) in position):
            continue
        out.append(Region(group, own))
    return out, {id(g) for g in ink if admissible(g)}


def _bar(rule: Rule, above: list[Glyph], below: list[Glyph], seeded: bool) -> bool:
    """Whether a rule is an equation's bar rather than a footnote or table line.

    A fraction's sides are centred on it; an overline or a vinculum is
    spanned by what it covers, which must already be mathematics.
    """
    if not above and not below:
        return False
    size = median(g.size for g in [*above, *below])
    width = rule.x1 - rule.x0
    if above and below and width <= 15 * size:
        centre = (rule.x0 + rule.x1) / 2
        tolerance = 0.2 * width + 0.25 * size
        if all(
            abs((min(g.x0 for g in side) + max(g.x1 for g in side)) / 2 - centre) <= tolerance
            for side in (above, below)
        ) and not any(g.char.isalpha() for g in [*above, *below] if not seeded):
            return True
    return seeded and _covers(rule, [*above, *below])


def _scripted(ink: list[Glyph], grid: _Grid, in_body) -> set[int]:
    """Italic letters outside the body font that carry a script.

    An italic letter is emphasis as easily as a variable, but one with a
    smaller glyph raised or lowered right after it is ``x_1`` or ``x^2``.
    Word and TeX set variables in a math font, which is a seed already;
    LibreOffice sets them in the serif italic, and this is what finds them.
    """
    found: set[int] = set()
    for glyph in ink:
        if not glyph.char.isalpha() or in_body(glyph) or not _italic(glyph):
            continue
        reach = 0.25 * glyph.size
        for index in grid.near(glyph.x1 - 1, glyph.top, glyph.x1 + reach, glyph.bottom):
            other = ink[index]
            if other is glyph or other.size > 0.8 * glyph.size:
                continue
            if -0.1 * glyph.size <= other.x0 - glyph.x1 <= reach and (
                abs(other.baseline - glyph.baseline) >= 0.15 * glyph.size
            ):
                found.add(id(glyph))
                found.add(id(other))
    return found


def _merge_bands(regions: list[Region], glyphs: list[Glyph], mathlike: set[int]) -> list[Region]:
    """Equations side by side with nothing between them are one equation.

    A display line holding two formulas (``a = b, \\qquad c = d``), or an
    integral whose limit a typesetter placed past its neighbour's reach, arrives
    as separate regions on one band; with no prose between them they are one.
    Glyphs between them that could be math (a matrix's entries between its
    brackets) come with them; any word, or punctuation alone, keeps them apart.
    """
    owner = {id(g): index for index, region in enumerate(regions) for g in region.glyphs}
    merged = True
    while merged:
        merged = False
        for i, first in enumerate(regions):
            for j in range(i + 1, len(regions)):
                second = regions[j]
                left, right = sorted((first, second), key=lambda r: min(g.x0 for g in r.glyphs))
                gap_start = max(g.x1 for g in left.glyphs)
                gap_end = min(g.x0 for g in right.glyphs)
                # One inside the other's span (a limit its operator's region
                # missed) needs only to touch its band; side by side, to share it.
                nested = gap_end < gap_start and _limit_like(first, second)
                if gap_end < gap_start and not nested:
                    if not _same_band(first, second):
                        continue
                elif not _same_band(first, second, nested=nested):
                    continue
                size = median(g.size for g in [*left.glyphs, *right.glyphs])
                if gap_end - gap_start > 6 * size:
                    continue
                top = max(min(g.top for g in r.glyphs) for r in (left, right))
                bottom = min(max(g.bottom for g in r.glyphs) for r in (left, right))
                between = [
                    g for g in glyphs
                    if not g.space and id(g) not in owner
                    and gap_start < (g.x0 + g.x1) / 2 < gap_end
                    and g.bottom > top and g.top < bottom
                ]  # fmt: skip
                if any(id(g) not in mathlike for g in between) or (
                    between and all(g.char in _TRAILING for g in between)
                ):
                    continue
                first.glyphs.extend(between)
                for glyph in between:
                    owner[id(glyph)] = i
                first.glyphs.extend(second.glyphs)
                first.rules.extend(r for r in second.rules if r not in first.rules)
                for glyph in second.glyphs:
                    owner[id(glyph)] = i
                del regions[j]
                merged = True
                break
            if merged:
                break
    return regions


def _absorb_rows(regions: list[Region], glyphs: list[Glyph]) -> list[Region]:
    """A tall equation that starts its lines owns everything on them.

    ``cases`` and aligned blocks carry prose and further formulas on their
    rows ("if x ≥ 0,", "otherwise."). An equation several lines tall with
    nothing before it on those lines is displayed, so the rest of its rows
    are part of it: formulas merge in, words come in as ``\\text``. An
    equation number at the right margin is left for ``\\tag``.
    """
    owner = {id(g): region for region in regions for g in region.glyphs}
    for region in list(regions):
        if region not in regions:
            continue
        size = median(g.size for g in region.glyphs)
        top = min(g.top for g in region.glyphs)
        bottom = max(g.bottom for g in region.glyphs)
        if bottom - top < 2.2 * size:
            continue
        x0 = min(g.x0 for g in region.glyphs)
        band = [
            g for g in glyphs
            if not g.space and id(g) not in owner or owner.get(id(g)) not in (None, region)
            if top <= (g.top + g.bottom) / 2 <= bottom and not g.space
        ]  # fmt: skip
        if any((g.x0 + g.x1) / 2 < x0 for g in band):
            continue  # an equation inside a paragraph line
        rightmost = sorted(band, key=lambda g: g.x0)
        tagged = _trailing_tag(rightmost, size, max(g.x1 for g in region.glyphs))
        for glyph in band:
            if any(glyph is t for t in tagged):
                continue
            other = owner.get(id(glyph))
            if other is not None and other is not region:
                region.glyphs.extend(other.glyphs)
                region.rules.extend(r for r in other.rules if r not in region.rules)
                for member in other.glyphs:
                    owner[id(member)] = region
                regions.remove(other)
            elif other is None:
                region.glyphs.append(glyph)
                owner[id(glyph)] = region
                if glyph.char.isalpha():
                    region.text.add(id(glyph))
    return regions


def _trailing_tag(ordered: list[Glyph], size: float, edge: float) -> list[Glyph]:
    """The glyphs of a ``(3)`` at the end of a band, well clear of what is
    before it (``edge``, the equation's right side), if there is one."""

    if not ordered or ordered[-1].char != ")":
        return []
    start = max((i for i, g in enumerate(ordered) if g.char == "("), default=None)
    if start is None:
        return []
    tag = ordered[start:]
    before = max([edge, *(g.x1 for g in ordered[:start])])
    label = "".join(g.char for g in tag)
    if re.fullmatch(r"\(\s*[\w.\-]{1,8}\s*\)", label) and tag[0].x0 - before > 1.5 * size:
        return tag
    return []


def _limit_like(a: Region, b: Region) -> bool:
    """Whether one region is a few glyphs (a limit) and the other holds the
    big operator they belong to, which is when overlap at all is enough."""

    small, big = sorted((a, b), key=lambda r: len(r.glyphs))
    return len(small.glyphs) <= 4 and any(g.tex in BIG_OPERATORS for g in big.glyphs)


def _same_band(a: Region, b: Region, nested: bool = False) -> bool:
    top = max(min(g.top for g in a.glyphs), min(g.top for g in b.glyphs))
    bottom = min(max(g.bottom for g in a.glyphs), max(g.bottom for g in b.glyphs))
    if nested:
        return bottom > top
    shortest = min(max(g.bottom for g in r.glyphs) - min(g.top for g in r.glyphs) for r in (a, b))
    return bottom - top >= 0.5 * shortest


_RELATIONS = set("=<>≤≥≠≈≡∈∉⊂⊃⊆⊇∼≃≅∝") | {"\u2264", "\u2265"}


_DELIMITER_TEX = {"(", ")", "[", "]", "|", r"\|", r"\{", r"\}", r"\langle", r"\rangle"}


#: Relations and binary operators, which TeX surrounds with stretchable space.
_SPACED = RELATIONS | {
    "+", "-", r"\pm", r"\mp", r"\times", r"\cdot", r"\cup", r"\cap", r"\setminus",
    r"\wedge", r"\vee", r"\circ", r"\otimes", r"\oplus",
}  # fmt: skip


def _flat_prose(group: list[Glyph], in_body) -> bool:
    """A run of symbols in a fallback font, which office suites use for any
    character the body font lacks: "5 μm", "α-helix", "±5%".

    Flat (one baseline, one size, no rule), with no glyph in a math font, no
    italic letter and no relation, it is the sentence's own text. An inline
    variable is italic, an equation has a relation or a structure, and TeX
    and Word set mathematics in a math font, so each of those is kept.
    """
    size = max(g.size for g in group)
    baseline = group[0].baseline
    if any(abs(g.baseline - baseline) > 0.1 * size or g.size < 0.85 * size for g in group):
        return False
    for glyph in group:
        if is_math_font(glyph.font) or glyph.char in _RELATIONS:
            return False
        if glyph.char.isalpha() and _italic(glyph) and not in_body(glyph):
            return False
    return True


def _covers(rule: Rule, near: list[Glyph]) -> bool:
    """Whether the glyphs on one side of a rule span most of it, as a
    numerator spans its bar and a radicand its vinculum."""

    width = max(rule.x1 - rule.x0, 0.01)
    for above in (True, False):
        side = sorted((g for g in near if (g.top < rule.y) == above), key=lambda g: g.x0)
        # One unbroken run of glyphs, as a numerator is: a footnote mark and a
        # full stop at either end of a line do not make a numerator.
        start = 0
        for index in range(1, len(side) + 1):
            if index == len(side) or side[index].x0 - side[index - 1].x1 > 0.6 * side[index].size:
                run = side[start:index]
                if min(rule.x1, run[-1].x1) - max(rule.x0, run[0].x0) >= 0.6 * width:
                    return True
                start = index
    return False


def _absorb_enclosed(
    group: list[Glyph], ink: list[Glyph], region_of: dict[int, int], admissible, grid: _Grid
) -> None:
    """Pull in glyphs lying wholly inside the region (a matrix's middle column)."""

    x0, x1 = min(g.x0 for g in group), max(g.x1 for g in group)
    top, bottom = min(g.top for g in group), max(g.bottom for g in group)
    if bottom - top < 1.6 * median(g.size for g in group):
        return
    ids = set(map(id, group))
    for index in sorted(grid.near(x0, top, x1, bottom)):
        glyph = ink[index]
        if id(glyph) in ids or index in region_of or not admissible(glyph):
            continue
        if x0 <= glyph.x0 and glyph.x1 <= x1 and top <= glyph.top and glyph.bottom <= bottom:
            group.append(glyph)
            region_of[index] = -1


def _trim(group: list[Glyph], in_body) -> list[Glyph]:
    """Sentence punctuation after an equation belongs to the sentence."""

    group = sorted(group, key=lambda g: g.x0)
    while group and group[-1].char in _TRAILING and in_body(group[-1]):
        group.pop()
    return group
