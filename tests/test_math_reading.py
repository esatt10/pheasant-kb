"""The parts under document math, each on input small enough to read.

``tests/test_document_math.py`` holds the whole path on real files; this holds
the rules it is made of, so a failure names the rule: one OMML element per
TeX construct, a TeX font code per symbol, one geometric relation per layout
rule, and the region rules on the cases that separate prose from mathematics
(a fallback-font μ, a footnote rule, two lines of a tight paragraph).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from pheasant.ingestion.math_layout import Atom, Rule, layout_to_tex
from pheasant.ingestion.omml import math_text, omml_to_latex
from pheasant.ingestion.pdf_math import Glyph, rewrite_page
from pheasant.ingestion.tex_symbols import (
    decode_glyph,
    font_reads_codes,
    math_alphanumeric,
    unicode_to_tex,
)

M = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def _omml(body: str) -> str:
    return omml_to_latex(ET.fromstring(f'<m:oMath xmlns:m="{M}">{body}</m:oMath>'))


def _r(text: str, style: str | None = None) -> str:
    props = f'<m:rPr><m:sty m:val="{style}"/></m:rPr>' if style else ""
    return f"<m:r>{props}<m:t>{text}</m:t></m:r>"


# ---------------------------------------------------------------------------
# Office Math
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "tex"),
    [
        (f"<m:f><m:num>{_r('a')}</m:num><m:den>{_r('b')}</m:den></m:f>", r"\frac{a}{b}"),
        (f"<m:sSup><m:e>{_r('x')}</m:e><m:sup>{_r('2')}</m:sup></m:sSup>", "x^2"),
        (
            f"<m:sSubSup><m:e>{_r('x')}</m:e><m:sub>{_r('i')}</m:sub><m:sup>{_r('10')}</m:sup></m:sSubSup>",
            "x_i^{10}",
        ),
        (
            '<m:rad><m:radPr><m:degHide m:val="on"/></m:radPr><m:deg/>'
            f"<m:e>{_r('x')}</m:e></m:rad>",
            r"\sqrt{x}",
        ),
        (f"<m:rad><m:deg>{_r('3')}</m:deg><m:e>{_r('y')}</m:e></m:rad>", r"\sqrt[3]{y}"),
        (
            f'<m:nary><m:naryPr><m:chr m:val="∑"/></m:naryPr><m:sub>{_r("i=1")}</m:sub>'
            f"<m:sup>{_r('n')}</m:sup><m:e>{_r('a')}</m:e></m:nary>",
            r"\sum_{i=1}^n a",
        ),
        (f"<m:d><m:e>{_r('x')}</m:e></m:d>", r"\left( x \right)"),
        (
            f'<m:acc><m:accPr><m:chr m:val="̂"/></m:accPr><m:e>{_r("β")}</m:e></m:acc>',
            r"\hat{\beta}",
        ),
        (
            f"<m:func><m:fName>{_r('sin', 'p')}</m:fName><m:e>{_r('θ')}</m:e></m:func>",
            r"\sin \theta",
        ),
        (
            f"<m:limLow><m:e>{_r('lim', 'p')}</m:e><m:lim>{_r('n→∞')}</m:lim></m:limLow>",
            r"\lim_{n\to\infty}",
        ),
        (_r("rank", "p"), r"\mathrm{rank}"),
        (_r("α β"), r"\alpha\,\beta"),
        (
            '<m:d><m:dPr><m:begChr m:val="["/><m:endChr m:val="]"/></m:dPr><m:e><m:m>'
            f"<m:mr><m:e>{_r('1')}</m:e><m:e>{_r('0')}</m:e></m:mr>"
            f"<m:mr><m:e>{_r('0')}</m:e><m:e>{_r('1')}</m:e></m:mr></m:m></m:e></m:d>",
            r"\begin{bmatrix} 1 & 0 \\ 0 & 1 \end{bmatrix}",
        ),
    ],
)
def test_each_office_math_element_is_one_tex_construct(body: str, tex: str) -> None:
    assert _omml(body) == tex


def test_an_equation_paragraph_is_displayed_and_an_inline_one_is_not() -> None:
    inline = ET.fromstring(f'<m:oMath xmlns:m="{M}">{_r("x")}</m:oMath>')
    para = ET.fromstring(f'<m:oMathPara xmlns:m="{M}"><m:oMath>{_r("y")}</m:oMath></m:oMathPara>')
    assert math_text(inline) == "$x$"
    assert math_text(para) == "$$y$$"


def test_hostile_nesting_is_bounded() -> None:
    body = _r("x")
    for _ in range(500):
        body = f"<m:sSup><m:e>{body}</m:e><m:sup>{_r('2')}</m:sup></m:sSup>"
    assert isinstance(_omml(body), str)


# ---------------------------------------------------------------------------
# Characters and TeX font codes
# ---------------------------------------------------------------------------


def test_bakoma_codes_decode_through_the_tex_encodings() -> None:
    # matplotlib's Computer Modern shows α as ® (0xAE, the BaKoMa shift of 0x0B).
    chars = "®¯µ¼"
    assert font_reads_codes("oml", chars, "Cmmi10")
    assert [decode_glyph(c, "Cmmi10", True) for c in chars] == [
        r"\alpha",
        r"\beta",
        r"\theta",
        r"\pi",
    ]
    assert decode_glyph("1", "Cmsy10", True) == r"\infty"
    assert decode_glyph("X", "Cmex10", True) == r"\sum"


def test_a_type1_tex_font_macron_is_a_macron_not_a_shifted_code() -> None:
    assert not font_reads_codes("ot1", "The ¯x", "CMR10")
    assert decode_glyph("¯", "CMR10", False) == r"ACCENT:\bar"


def test_the_extension_font_is_read_by_code_even_beside_mapped_glyphs() -> None:
    assert decode_glyph("P", "CMEX10", False) == r"\sum"  # text-style sum, name unknown
    assert decode_glyph("\x12", "CMEX10", False) == "("  # bigg parenthesis
    assert decode_glyph("", "CMEX10", False) == "PIECE:["  # Adobe bracketlefttp


@pytest.mark.parametrize(
    ("char", "font", "tex"),
    [
        ("R", "MSBM10", r"\mathbb{R}"),
        ("L", "CMSY10", r"\mathcal{L}"),
        ("", "SymbolMT", r"\alpha"),
        ("≤", "CambriaMath", r"\leq"),
    ],
)
def test_alphabet_and_symbol_fonts(char: str, font: str, tex: str) -> None:
    assert decode_glyph(char, font, False) == tex


def test_unicode_math_alphanumerics() -> None:
    assert math_alphanumeric("𝑥") == "x"
    assert math_alphanumeric("𝐱") == r"\mathbf{x}"
    assert math_alphanumeric("𝜃") == r"\theta"
    assert unicode_to_tex("∂") == r"\partial"


# ---------------------------------------------------------------------------
# Layout: geometry to TeX
# ---------------------------------------------------------------------------


def _a(tex: str, x0: float, x1: float, baseline: float, size: float = 10.0, **kw) -> Atom:
    return Atom(tex, x0, x1, baseline - 0.72 * size, baseline + 0.22 * size, baseline, size, **kw)


def test_a_bar_with_glyphs_over_and_under_it_is_a_fraction() -> None:
    atoms = [_a("a", 10, 15, 95, 7), _a("b", 10, 15, 106, 7)]
    assert layout_to_tex(atoms, [Rule(9, 16, 99)]) == r"\frac{a}{b}"


def test_a_smaller_raised_glyph_is_a_superscript_and_a_lowered_one_a_subscript() -> None:
    atoms = [
        _a("x", 0, 6, 100),
        _a("2", 6, 10, 96, 7),
        _a("y", 14, 20, 100),
        _a("i", 20, 23, 102, 7),
    ]
    assert layout_to_tex(atoms, []) == "x^2 y_i"


def test_limits_stacked_on_a_big_operator() -> None:
    op = Atom(r"\sum", 0, 14, 92, 108, 100, 14, limits=True)
    atoms = [
        op,
        _a("n", 4, 9, 89, 7),
        _a("i", 2, 5, 115, 7),
        _a("=", 5, 9, 115, 7),
        _a("1", 9, 12, 115, 7),
    ]
    assert layout_to_tex(atoms, []) == r"\sum_{i=1}^n"


def test_a_radical_owns_what_its_vinculum_covers_and_its_index() -> None:
    radical = Atom("RADICAL", 0, 8, 90, 101, 100, 10)
    atoms = [radical, _a("3", 1, 4, 94, 5), _a("x", 8, 14, 100)]
    assert layout_to_tex(atoms, [Rule(8, 15, 90.5)]) == r"\sqrt[3]{x}"


def test_rows_inside_tall_parentheses_are_a_matrix() -> None:
    tall = {"top": 80.0, "bottom": 112.0, "baseline": 100.0, "size": 10.0}
    left = Atom("(", 0, 4, **tall)
    right = Atom(")", 30, 34, **tall)
    cells = [_a("a", 6, 11, 92), _a("b", 22, 27, 92), _a("c", 6, 11, 106), _a("d", 22, 27, 106)]
    assert (
        layout_to_tex([left, *cells, right], []) == r"\begin{pmatrix} a & b \\ c & d \end{pmatrix}"
    )


# ---------------------------------------------------------------------------
# Regions: what is mathematics on a page
# ---------------------------------------------------------------------------


def _glyphs(text: str, font: str, x: float, baseline: float, line: int, size: float = 10.0,
            start: int = 0) -> list[Glyph]:  # fmt: skip
    out = []
    for index, char in enumerate(text):
        out.append(
            Glyph(char, font, size, x + 5 * index, x + 5 * index + 5, baseline, start + index, line)
        )
    return out


def test_an_italic_variable_in_a_math_font_is_an_equation() -> None:
    page = _glyphs("Let ", "Body", 0, 100, 0) + _glyphs("x", "CMMI10", 20, 100, 0, start=4)
    page += _glyphs(" be.", "Body", 25, 100, 0, start=5)
    assert rewrite_page(page, []) == "Let $x$ be."


def test_a_fallback_font_greek_letter_in_prose_is_not_an_equation() -> None:
    page = _glyphs("Samples of 5 ", "Caladea-Regular", 0, 100, 0)
    page += _glyphs("μ", "DejaVuSans", 65, 100, 0, start=13)
    page += _glyphs("m were kept.", "Caladea-Regular", 70, 100, 0, start=14)
    assert rewrite_page(page, []) is None


def test_a_footnote_rule_does_not_pull_the_footnote_into_its_formula() -> None:
    page = _glyphs("1", "CMR6", 10, 106, 0, size=6)
    page += _glyphs("We assume ", "CMR8", 15, 108, 0, size=8, start=1)
    page += _glyphs("n>p", "CMMI8", 65, 108, 0, size=8, start=11)
    page += _glyphs(".", "CMR8", 80, 108, 0, size=8, start=14)
    text = rewrite_page(page, [Rule(0, 144, 98)])
    assert text == "1We assume $n>p$."


def test_two_tight_lines_are_two_equations() -> None:
    first = _glyphs("a=b", "CMMI10", 0, 100, 0)
    second = _glyphs("c=d", "CMMI10", 0, 109.5, 1, start=3)  # boxes a point apart
    text = rewrite_page(first + second, [])
    assert text is not None and "$a=b$" in text and "$c=d$" in text
