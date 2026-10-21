"""Characters and font codes as TeX: the one table both math readers share.

Mathematics reaches extraction two ways, and both end in the same alphabet.
A DOCX equation (:mod:`pheasant.ingestion.omml`) carries Unicode characters,
so ``α`` needs to become ``\\alpha``. A PDF (:mod:`pheasant.ingestion.pdf_math`)
carries glyphs, and a TeX-typeset PDF without a ``/ToUnicode`` map carries
*font codes*: the Computer Modern fonts use TeX's own fixed encodings, so
code ``0x0B`` in ``CMMI10`` is α whatever a reader decodes it as. A naive
reader shows ``®`` (Latin-1 for ``0xAE``, the BaKoMa TrueType remap of
``0x0B``), ``X`` for a displayed ∑ and ``¡`` for a minus sign.

Those encodings (OML for math italic, OMS for math symbols, OMX for the
extension font, OT1 for the roman) are fixed and public, so recovering a
symbol from its code is a lookup, not a guess. Which reading applies is
decided once per font (:func:`font_reads_codes`): a font whose characters
include real Unicode mathematics was mapped by the PDF and is read as
Unicode; one that shows only byte-range characters is read by its code.

Pure tables and pure functions: no I/O, no third-party import.
"""

from __future__ import annotations

import re
from functools import lru_cache

# ---------------------------------------------------------------------------
# Unicode -> TeX
# ---------------------------------------------------------------------------

_GREEK = {
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta", "ϵ": r"\epsilon",
    "ε": r"\varepsilon", "ζ": r"\zeta", "η": r"\eta", "θ": r"\theta", "ϑ": r"\vartheta",
    "ι": r"\iota", "κ": r"\kappa", "ϰ": r"\varkappa", "λ": r"\lambda", "μ": r"\mu",
    "µ": r"\mu", "ν": r"\nu", "ξ": r"\xi", "ο": "o", "π": r"\pi", "ϖ": r"\varpi",
    "ρ": r"\rho", "ϱ": r"\varrho", "σ": r"\sigma", "ς": r"\varsigma", "τ": r"\tau",
    "υ": r"\upsilon", "ϕ": r"\phi", "φ": r"\varphi", "χ": r"\chi", "ψ": r"\psi",
    "ω": r"\omega", "Γ": r"\Gamma", "Δ": r"\Delta", "Θ": r"\Theta", "Λ": r"\Lambda",
    "Ξ": r"\Xi", "Π": r"\Pi", "Σ": r"\Sigma", "Υ": r"\Upsilon", "Φ": r"\Phi",
    "Ψ": r"\Psi", "Ω": r"\Omega", "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H",
    "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Χ": "X",
}  # fmt: skip

_OPERATORS = {
    "−": "-", "–": "-", "∗": "*", "·": r"\cdot", "⋅": r"\cdot", "∙": r"\cdot",
    "×": r"\times", "÷": r"\div", "±": r"\pm", "∓": r"\mp", "∘": r"\circ",
    "•": r"\bullet", "⊕": r"\oplus", "⊖": r"\ominus", "⊗": r"\otimes", "⊘": r"\oslash",
    "⊙": r"\odot", "⋆": r"\star", "†": r"\dagger", "‡": r"\ddagger", "∖": r"\setminus",
    "∪": r"\cup", "∩": r"\cap", "⊎": r"\uplus", "⊔": r"\sqcup", "⊓": r"\sqcap",
    "∧": r"\wedge", "∨": r"\vee", "⋄": r"\diamond", "≀": r"\wr", "⨿": r"\amalg",
    "=": "=", "≠": r"\neq", "≤": r"\leq", "⩽": r"\leq", "≥": r"\geq", "⩾": r"\geq",
    "≪": r"\ll", "≫": r"\gg", "≺": r"\prec", "≻": r"\succ", "⪯": r"\preceq",
    "⪰": r"\succeq", "∼": r"\sim", "≃": r"\simeq", "≈": r"\approx", "≅": r"\cong",
    "≡": r"\equiv", "≍": r"\asymp", "∝": r"\propto", "≐": r"\doteq", "≔": ":=",
    "∈": r"\in", "∉": r"\notin", "∋": r"\ni", "⊂": r"\subset", "⊃": r"\supset",
    "⊆": r"\subseteq", "⊇": r"\supseteq", "⊊": r"\subsetneq", "⊑": r"\sqsubseteq",
    "⊒": r"\sqsupseteq", "⊢": r"\vdash", "⊣": r"\dashv", "⊨": r"\models",
    "∣": r"\mid", "∤": r"\nmid", "∥": r"\|", "‖": r"\|", "⊥": r"\perp",
    "⊤": r"\top", "→": r"\to", "←": r"\leftarrow", "↔": r"\leftrightarrow",
    "⇒": r"\Rightarrow", "⇐": r"\Leftarrow", "⇔": r"\Leftrightarrow",
    "⟶": r"\longrightarrow", "⟹": r"\Longrightarrow", "⟺": r"\iff", "↦": r"\mapsto",
    "↑": r"\uparrow", "↓": r"\downarrow", "⇑": r"\Uparrow", "⇓": r"\Downarrow",
    "↗": r"\nearrow", "↘": r"\searrow", "↖": r"\nwarrow", "↙": r"\swarrow",
    "↕": r"\updownarrow", "⇕": r"\Updownarrow", "↪": r"\hookrightarrow",
    "↩": r"\hookleftarrow", "⇀": r"\rightharpoonup", "↼": r"\leftharpoonup",
    "∀": r"\forall", "∃": r"\exists", "∄": r"\nexists", "¬": r"\neg", "∅": r"\emptyset",
    "∞": r"\infty", "∂": r"\partial", "∇": r"\nabla", "ℓ": r"\ell", "℘": r"\wp",
    "ℜ": r"\Re", "ℑ": r"\Im", "ℵ": r"\aleph", "ℏ": r"\hbar", "ı": r"\imath",
    "ȷ": r"\jmath", "′": "'", "″": "''", "‴": "'''", "°": r"^\circ", "△": r"\triangle",
    "∠": r"\angle", "∴": r"\therefore", "∵": r"\because", "…": r"\ldots",
    "⋯": r"\cdots", "⋮": r"\vdots", "⋱": r"\ddots", "♭": r"\flat", "♮": r"\natural",
    "♯": r"\sharp", "♣": r"\clubsuit", "♢": r"\diamondsuit", "♡": r"\heartsuit",
    "♠": r"\spadesuit", "§": r"\S", "¶": r"\P", "⌊": r"\lfloor", "⌋": r"\rfloor",
    "⌈": r"\lceil", "⌉": r"\rceil", "⟨": r"\langle", "⟩": r"\rangle", "〈": r"\langle",
    "〉": r"\rangle", "{": r"\{", "}": r"\}", "∑": r"\sum", "∏": r"\prod",
    "∐": r"\coprod", "∫": r"\int", "∬": r"\iint", "∭": r"\iiint", "∮": r"\oint",
    "⋃": r"\bigcup", "⋂": r"\bigcap", "⨁": r"\bigoplus", "⨂": r"\bigotimes",
    "⨀": r"\bigodot", "⋀": r"\bigwedge", "⋁": r"\bigvee", "⨆": r"\bigsqcup",
    "√": r"\sqrt", "∛": r"\sqrt[3]", "∜": r"\sqrt[4]", "ℕ": r"\mathbb{N}",
    "ℤ": r"\mathbb{Z}", "ℚ": r"\mathbb{Q}", "ℝ": r"\mathbb{R}", "ℂ": r"\mathbb{C}",
    "ℙ": r"\mathbb{P}", "ℍ": r"\mathbb{H}", "ℎ": "h", "ℯ": "e", "ℊ": "g", "ℴ": "o",
    "ℬ": r"\mathcal{B}", "ℰ": r"\mathcal{E}", "ℱ": r"\mathcal{F}", "ℋ": r"\mathcal{H}",
    "ℐ": r"\mathcal{I}", "ℒ": r"\mathcal{L}", "ℳ": r"\mathcal{M}", "ℛ": r"\mathcal{R}",
    "ℭ": r"\mathfrak{C}", "ℌ": r"\mathfrak{H}", "ℨ": r"\mathfrak{Z}", "⊺": r"\top",
    "\u2061": "", "\u2062": "", "\u2063": "", "\u2064": "", "\u200b": "", "\u0338": r"\not",
}  # fmt: skip

#: Accent characters (spacing or combining) and the TeX command they put on
#: the symbol beneath them.
ACCENTS = {
    "^": r"\hat", "ˆ": r"\hat", "\u0302": r"\hat", "ˇ": r"\check",
    "\u030c": r"\check", "~": r"\tilde", "˜": r"\tilde", "\u0303": r"\tilde",
    "¯": r"\bar", "ˉ": r"\bar", "\u0304": r"\bar", "‾": r"\overline", "\u0305": r"\bar",
    "´": r"\acute", "\u0301": r"\acute", "`": r"\grave", "\u0300": r"\grave",
    "˘": r"\breve", "\u0306": r"\breve", "˙": r"\dot", "\u0307": r"\dot", "¨": r"\ddot",
    "\u0308": r"\ddot", "\u20d7": r"\vec", "˚": r"\mathring",
}  # fmt: skip

UNICODE_TO_TEX: dict[str, str] = {**_GREEK, **_OPERATORS}

#: Operators set as large operators, whose limits may sit above and below.
BIG_OPERATORS = {
    r"\sum", r"\prod", r"\coprod", r"\int", r"\iint", r"\iiint", r"\oint", r"\bigcup",
    r"\bigcap", r"\bigoplus", r"\bigotimes", r"\bigodot", r"\bigwedge", r"\bigvee",
    r"\bigsqcup", r"\biguplus",
}  # fmt: skip

#: Of those, the ones whose limits TeX sets as scripts beside them by default.
INTEGRALS = {r"\int", r"\iint", r"\iiint", r"\oint"}

RELATIONS = {
    "=", "<", ">", r"\neq", r"\leq", r"\geq", r"\ll", r"\gg", r"\sim", r"\simeq",
    r"\approx", r"\cong", r"\equiv", r"\propto", r"\in", r"\notin", r"\subset",
    r"\supset", r"\subseteq", r"\supseteq", r"\to", r"\Rightarrow", r"\Leftarrow",
    r"\Leftrightarrow", r"\mapsto", r"\mid", r"\prec", r"\succ", r"\preceq", r"\succeq",
}  # fmt: skip

OPENERS = {"(", "[", r"\{", r"\langle", r"\lfloor", r"\lceil", "|", r"\|"}
CLOSERS = {")", "]", r"\}", r"\rangle", r"\rfloor", r"\rceil", "|", r"\|"}

#: Upright words TeX sets as operators. Inside math, a run of these letters is
#: one command rather than a product of variables.
FUNCTION_NAMES = {
    "arccos", "arcsin", "arctan", "arg", "cos", "cosh", "cot", "coth", "csc", "deg",
    "det", "dim", "exp", "gcd", "hom", "inf", "ker", "lg", "lim", "liminf", "limsup",
    "ln", "log", "max", "min", "Pr", "sec", "sin", "sinh", "sup", "tan", "tanh",
}  # fmt: skip

#: Of those, the ones whose limits sit beneath them in display.
LIMIT_FUNCTIONS = {"lim", "liminf", "limsup", "max", "min", "sup", "inf", "det", "gcd", "Pr"}


# ---------------------------------------------------------------------------
# Mathematical alphanumerics (U+1D400 block)
# ---------------------------------------------------------------------------

_LATIN_STYLES = [
    (0x1D400, r"\mathbf"), (0x1D434, ""), (0x1D468, r"\boldsymbol"),
    (0x1D49C, r"\mathcal"), (0x1D4D0, r"\mathcal"), (0x1D504, r"\mathfrak"),
    (0x1D538, r"\mathbb"), (0x1D56C, r"\mathfrak"), (0x1D5A0, r"\mathsf"),
    (0x1D5D4, r"\mathsf"), (0x1D608, r"\mathsf"), (0x1D63C, r"\mathsf"), (0x1D670, r"\mathtt"),
]  # fmt: skip
_GREEK_STYLES = [
    (0x1D6A8, r"\boldsymbol"), (0x1D6E2, ""), (0x1D71C, r"\boldsymbol"),
    (0x1D756, r"\boldsymbol"), (0x1D790, r"\boldsymbol"),
]  # fmt: skip
_GREEK_ORDER = "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡϴΣΤΥΦΧΨΩ∇αβγδεζηθικλμνξοπρςστυφχψω∂ϵϑϰϕϱϖ"
_DIGIT_STYLES = [
    (0x1D7CE, r"\mathbf"), (0x1D7D8, r"\mathbb"), (0x1D7E2, r"\mathsf"),
    (0x1D7EC, r"\mathsf"), (0x1D7F6, r"\mathtt"),
]  # fmt: skip


def _styled(command: str, inner: str) -> str:
    return f"{command}{{{inner}}}" if command else inner


def math_alphanumeric(char: str) -> str | None:
    """TeX for a U+1D400-block letter or digit (``𝑥`` -> ``x``, ``𝐱`` -> ``\\mathbf{x}``)."""

    code = ord(char)
    if not 0x1D400 <= code <= 0x1D7FF:
        return None
    for start, command in _LATIN_STYLES:
        if start <= code < start + 52:
            offset = code - start
            letter = chr(ord("A") + offset) if offset < 26 else chr(ord("a") + offset - 26)
            return _styled(command, letter)
    for start, command in _GREEK_STYLES:
        if start <= code < start + 58:
            base = _GREEK_ORDER[code - start]
            return _styled(command, UNICODE_TO_TEX.get(base, base))
    for start, command in _DIGIT_STYLES:
        if start <= code < start + 10:
            return _styled(command, chr(ord("0") + code - start))
    return None


def unicode_to_tex(char: str) -> str:
    """One character as TeX; an unknown character passes through as itself."""

    if char in UNICODE_TO_TEX:
        return UNICODE_TO_TEX[char]
    styled = math_alphanumeric(char)
    if styled is not None:
        return styled
    if char in "#$%&_":
        return "\\" + char
    if char == "\\":
        return r"\backslash"
    return char


# ---------------------------------------------------------------------------
# TeX font encodings: code -> TeX
# ---------------------------------------------------------------------------

_GREEK_CAPS = [r"\Gamma", r"\Delta", r"\Theta", r"\Lambda", r"\Xi", r"\Pi", r"\Sigma",
               r"\Upsilon", r"\Phi", r"\Psi", r"\Omega"]  # fmt: skip


def _oml() -> dict[int, str]:
    table = {code: name for code, name in enumerate(_GREEK_CAPS)}
    lower = [r"\alpha", r"\beta", r"\gamma", r"\delta", r"\epsilon", r"\zeta", r"\eta",
             r"\theta", r"\iota", r"\kappa", r"\lambda", r"\mu", r"\nu", r"\xi", r"\pi",
             r"\rho", r"\sigma", r"\tau", r"\upsilon", r"\phi", r"\chi", r"\psi", r"\omega",
             r"\varepsilon", r"\vartheta", r"\varpi", r"\varrho", r"\varsigma", r"\varphi",
             r"\leftharpoonup", r"\leftharpoondown", r"\rightharpoonup", r"\rightharpoondown",
             "", "", r"\triangleright", r"\triangleleft"]  # fmt: skip
    table.update({0x0B + i: name for i, name in enumerate(lower)})
    table.update({0x30 + i: str(i) for i in range(10)})
    table.update({0x3A: ".", 0x3B: ",", 0x3C: "<", 0x3D: "/", 0x3E: ">", 0x3F: r"\star"})
    table[0x40] = r"\partial"
    table.update({0x41 + i: chr(0x41 + i) for i in range(26)})
    table.update({0x5B: r"\flat", 0x5C: r"\natural", 0x5D: r"\sharp", 0x5E: r"\smile",
                  0x5F: r"\frown", 0x60: r"\ell"})  # fmt: skip
    table.update({0x61 + i: chr(0x61 + i) for i in range(26)})
    table.update({0x7B: r"\imath", 0x7C: r"\jmath", 0x7D: r"\wp", 0x7E: "ACCENT:\\vec"})
    return table


def _oms() -> dict[int, str]:
    names = [
        "-", r"\cdot", r"\times", "*", r"\div", r"\diamond", r"\pm", r"\mp", r"\oplus",
        r"\ominus", r"\otimes", r"\oslash", r"\odot", r"\bigcirc", r"\circ", r"\bullet",
        r"\asymp", r"\equiv", r"\subseteq", r"\supseteq", r"\leq", r"\geq", r"\preceq",
        r"\succeq", r"\sim", r"\approx", r"\subset", r"\supset", r"\ll", r"\gg", r"\prec",
        r"\succ", r"\leftarrow", r"\to", r"\uparrow", r"\downarrow", r"\leftrightarrow",
        r"\nearrow", r"\searrow", r"\simeq", r"\Leftarrow", r"\Rightarrow", r"\Uparrow",
        r"\Downarrow", r"\Leftrightarrow", r"\nwarrow", r"\swarrow", r"\propto", "'",
        r"\infty", r"\in", r"\ni", r"\triangle", r"\triangledown", r"\not", r"\mapsto",
        r"\forall", r"\exists", r"\neg", r"\emptyset", r"\Re", r"\Im", r"\top", r"\perp",
        r"\aleph",
    ]  # fmt: skip
    table = dict(enumerate(names))
    table.update({0x41 + i: r"\mathcal{" + chr(0x41 + i) + "}" for i in range(26)})
    rest = [
        r"\cup", r"\cap", r"\uplus", r"\wedge", r"\vee", r"\vdash", r"\dashv", r"\lfloor",
        r"\rfloor", r"\lceil", r"\rceil", r"\{", r"\}", r"\langle", r"\rangle", "|", r"\|",
        r"\updownarrow", r"\Updownarrow", r"\setminus", r"\wr", "RADICAL", r"\amalg",
        r"\nabla", r"\int", r"\sqcup", r"\sqcap", r"\sqsubseteq", r"\sqsupseteq", r"\S",
        r"\dagger", r"\ddagger", r"\P", r"\clubsuit", r"\diamondsuit", r"\heartsuit",
        r"\spadesuit",
    ]  # fmt: skip
    table.update({0x5B + i: name for i, name in enumerate(rest)})
    return table


def _omx() -> dict[int, str]:
    delims = ["(", ")", "[", "]", r"\lfloor", r"\rfloor", r"\lceil", r"\rceil", r"\{", r"\}",
              r"\langle", r"\rangle", "|", r"\|", "/", r"\backslash"]  # fmt: skip
    table = {code: name for code, name in enumerate(delims)}
    big = ["(", ")", "(", ")", "[", "]", r"\lfloor", r"\rfloor", r"\lceil", r"\rceil",
           r"\{", r"\}", r"\langle", r"\rangle", "/", r"\backslash", "(", ")", "[", "]",
           r"\lfloor", r"\rfloor", r"\lceil", r"\rceil", r"\{", r"\}", r"\langle",
           r"\rangle", "/", r"\backslash", "/", r"\backslash"]  # fmt: skip
    table.update({0x10 + i: name for i, name in enumerate(big)})
    pieces = ["PIECE:(", "PIECE:)", "PIECE:[", "PIECE:]", "PIECE:[", "PIECE:]", "PIECE:[",
              "PIECE:]", "PIECE:\\{", "PIECE:\\}", "PIECE:\\{", "PIECE:\\}", "PIECE:\\{",
              "PIECE:\\}", "PIECE:\\{", "PIECE:|", "PIECE:(", "PIECE:)", "PIECE:(",
              "PIECE:)"]  # fmt: skip
    table.update({0x30 + i: name for i, name in enumerate(pieces)})
    ops = [r"\langle", r"\rangle", r"\bigsqcup", r"\bigsqcup", r"\oint", r"\oint",
           r"\bigodot", r"\bigodot", r"\bigoplus", r"\bigoplus", r"\bigotimes",
           r"\bigotimes", r"\sum", r"\prod", r"\int", r"\bigcup", r"\bigcap", r"\biguplus",
           r"\bigwedge", r"\bigvee", r"\sum", r"\prod", r"\int", r"\bigcup", r"\bigcap",
           r"\biguplus", r"\bigwedge", r"\bigvee", r"\coprod", r"\coprod", "ACCENT:\\widehat",
           "ACCENT:\\widehat", "ACCENT:\\widehat", "ACCENT:\\widetilde", "ACCENT:\\widetilde",
           "ACCENT:\\widetilde", "[", "]", r"\lfloor", r"\rfloor", r"\lceil", r"\rceil",
           r"\{", r"\}", "RADICAL", "RADICAL", "RADICAL", "RADICAL", "RADICAL", "PIECE:|",
           "PIECE:|"]  # fmt: skip
    table.update({0x44 + i: name for i, name in enumerate(ops)})
    return table


def _ot1() -> dict[int, str]:
    table = {code: name for code, name in enumerate(_GREEK_CAPS)}
    table.update({0x0B: "ff", 0x0C: "fi", 0x0D: "fl", 0x0E: "ffi", 0x0F: "ffl",
                  0x10: r"\imath", 0x11: r"\jmath", 0x12: "ACCENT:\\grave",
                  0x13: "ACCENT:\\acute", 0x14: "ACCENT:\\check", 0x15: "ACCENT:\\breve",
                  0x16: "ACCENT:\\bar", 0x17: "ACCENT:\\mathring", 0x19: r"\ss"})  # fmt: skip
    table.update({code: chr(code) for code in range(0x21, 0x7B)})
    table.update({0x22: "''", 0x5C: "``", 0x5E: "ACCENT:\\hat", 0x5F: "ACCENT:\\dot",
                  0x60: "`", 0x7B: "-", 0x7C: "-", 0x7D: "ACCENT:\\H",
                  0x7E: "ACCENT:\\tilde", 0x7F: "ACCENT:\\ddot"})  # fmt: skip
    return table


ENCODINGS: dict[str, dict[int, str]] = {"oml": _oml(), "oms": _oms(), "omx": _omx(), "ot1": _ot1()}

_FAMILY_PATTERNS = [
    ("oml", re.compile(r"^(cmmi|cmmib|lmmi|lmmathitalic|eurm|rtxmi|txmi|pxmi|zplmi)", re.I)),
    ("oms", re.compile(r"^(cmsy|cmbsy|lmsy|lmbsy|lmmathsymbols|rtxsy|txsy|pxsy|zplsy)", re.I)),
    ("omx", re.compile(r"^(cmex|lmex|lmmathextension|rtxex|txex|pxex|zplex)", re.I)),
    ("ot1", re.compile(r"^(cmr|cmbx|cmb\d|cmsl|cmss|cmti|cmu|lmr|lmroman)", re.I)),
]

#: Fonts whose letters are always one math alphabet, however they are mapped.
_ALPHABET_FONTS = [
    (re.compile(r"^(msbm|bbold|dsrom|bbm)", re.I), r"\mathbb"),
    (re.compile(r"^(eufm|eufb|yfrak)", re.I), r"\mathfrak"),
    (re.compile(r"^(rsfs|eusm|eusb)", re.I), r"\mathscr"),
    (re.compile(r"^(cmsy|cmbsy|lmsy|lmbsy|lmmathsymbols)", re.I), r"\mathcal"),
]


@lru_cache(maxsize=4096)
def base_font_name(name: str) -> str:
    """A font name without its subset prefix (``ABCDEF+CMMI10`` -> ``CMMI10``)."""

    return re.sub(r"^[A-Z]{6}\+", "", str(name or "")).strip()


@lru_cache(maxsize=4096)
def tex_font_family(name: str) -> str | None:
    """``oml``/``oms``/``omx``/``ot1`` for a TeX-encoded font, else ``None``."""

    base = base_font_name(name)
    for family, pattern in _FAMILY_PATTERNS:
        if pattern.match(base):
            return family
    return None


_MATH_FONT_RE = re.compile(
    r"(math|symbol|^cmmi|^cmsy|^cmex|^cmmib|^cmbsy|^lmmi|^lmsy|^lmex|^msam|^msbm|^eufm|"
    r"^eusm|^rsfs|^stmary|^wasy|mtsy|mtmi|mtex|euclid|mt\s*extra|rtxmi|rtxsy|txmi|txsy|"
    r"txex|pxmi|pxsy|pxex|zplmi|zplsy|zplex|esint|opensymbol)",
    re.I,
)


@lru_cache(maxsize=4096)
def is_math_font(name: str) -> bool:
    """True for a font only mathematics is set in (CMMI, Cambria Math, Symbol …)."""

    return bool(_MATH_FONT_RE.search(base_font_name(name)))


# Characters a reader shows for byte codes it could not map. Only these can
# be a TeX font's raw code; anything else was mapped by the PDF itself.
_ALIASES = {"∙": 0xB7, "€": 0x80, "‚": 0x82, "ƒ": 0x83, "„": 0x84, "…": 0x85, "†": 0x86,
            "‡": 0x87, "ˆ": 0x88, "‰": 0x89, "Š": 0x8A, "‹": 0x8B, "Œ": 0x8C, "Ž": 0x8E,
            "‘": 0x91, "’": 0x92, "“": 0x93, "”": 0x94, "•": 0x95, "–": 0x96, "—": 0x97,
            "˜": 0x98, "™": 0x99, "š": 0x9A, "›": 0x9B, "œ": 0x9C, "ž": 0x9E,
            "Ÿ": 0x9F}  # fmt: skip


def byte_code(char: str) -> int | None:
    """The byte code a reader most likely decoded into ``char``, or ``None``."""

    if len(char) != 1:
        return None
    if char in _ALIASES:
        return _ALIASES[char]
    code = ord(char)
    return code if code < 256 else None


def unshift_bakoma(code: int) -> int:
    """Undo the BaKoMa TrueType remap of TeX's low control codes.

    The BaKoMa Computer Modern fonts (the ones matplotlib and many converters
    embed) move codes 0x00-0x20 and 0x7F out of the control range: 0x00-0x09
    to 0xA1-0xAA, 0x0A to 0xAD, 0x0B-0x1F to 0xAE-0xC2, 0x20 to 0xC3 and 0x7F
    to 0xC4. No TeX encoding uses 0xA1-0xC4 otherwise.
    """
    if 0xA1 <= code <= 0xAA:
        return code - 0xA1
    if code == 0xAD:
        return 0x0A
    if 0xAE <= code <= 0xC2:
        return code - 0xA3
    if code == 0xC3:
        return 0x20
    if code == 0xC4:
        return 0x7F
    return code


def font_reads_codes(family: str | None, chars: str, font: str = "") -> bool:
    """Whether a TeX-encoded font's characters are raw codes, not Unicode.

    Decided per font from everything it showed: Unicode mathematics (α, ∑,
    −) means the PDF mapped the font and its characters are read as they
    are; a character in a TeX encoding's remapped range, a control code, or
    (for the symbol and extension fonts, which hold no letters) a letter or
    digit means they are codes.
    """
    if family is None:
        return False
    if any(ord(c) > 0xFF and c not in _ALIASES for c in chars):
        return False
    # The BaKoMa remap belongs to the TrueType Computer Modern fonts (named
    # `Cmr10` or `cmmi10`); a Type 1 TeX font is named in capitals (`CMR10`)
    # or by family (`LMRoman10-Regular`), and there `¯` really is a macron,
    # as its glyph name said.
    name = base_font_name(font)
    bakoma = bool(re.fullmatch(r"cm[a-z]+\d+", name, re.I)) and not name.isupper()
    for char in chars:
        code = byte_code(char)
        if code is None:
            continue
        if code < 0x20 or bakoma and 0xA1 <= code <= 0xC4:
            return True
        if family in ("oms", "omx") and char.isalnum():
            return True
    return False


def decode_glyph(char: str, font: str, reads_codes: bool) -> str:
    """One glyph as TeX, through its font's encoding when it shows raw codes.

    May return a marker instead of TeX: ``ACCENT:\\hat`` for an accent glyph,
    ``RADICAL`` for a radical sign, ``PIECE:(`` for one piece of a delimiter
    built from parts.
    """
    base = base_font_name(font)
    if (
        len(char) == 1
        and "A" <= char <= "Z"
        or "a" <= char <= "z"
        and not base.lower().startswith("cm")
    ):
        for pattern, command in _ALPHABET_FONTS:
            if pattern.match(base) and (char.isupper() or command != r"\mathcal"):
                return command + "{" + char + "}"
    family = tex_font_family(font)
    # A control character is never Unicode text: it is a code the reader could
    # not map (MuPDF hands one back for a glyph name it does not know), and in
    # a TeX font its encoding says what it is even when the font's other
    # glyphs were mapped.
    # The extension font holds no text at all, so any byte-range character in
    # it is a code: MuPDF maps the glyph names it knows (⎡) and hands back the
    # code for those it does not (`P` for a text-style ∑), in one font.
    if family is not None and (
        reads_codes
        or len(char) == 1
        and (ord(char) < 0x20 or family == "omx" and ord(char) < 0x100)
    ):
        code = byte_code(char)
        if code is not None:
            name = ENCODINGS[family].get(unshift_bakoma(code))
            if name is not None:
                return name
    if char in _ADOBE_PIECES:
        return "PIECE:" + _ADOBE_PIECES[char]
    if "\uf020" <= char <= "\uf0ff" and re.match(r"symbol", base, re.I):
        # The Symbol font as Word and others embed it: code + U+F000.
        char = _SYMBOL_ENCODING.get(ord(char) - 0xF000, char)
    if char in ACCENTS:
        return "ACCENT:" + ACCENTS[char]
    if char == "√":
        return "RADICAL"
    if char in "⎛⎜⎝⎞⎟⎠⎡⎢⎣⎤⎥⎦⎧⎨⎩⎪⎫⎬⎭⎮":
        return "PIECE:" + _PIECE_DELIMS[char]
    return unicode_to_tex(char)


#: The Adobe private-use code points glyph names like `bracketlefttp` map to.
_ADOBE_PIECES = {
    "\uf8eb": "(", "\uf8ec": "(", "\uf8ed": "(", "\uf8ee": "[", "\uf8ef": "[", "\uf8f0": "[",
    "\uf8f1": r"\{", "\uf8f2": r"\{", "\uf8f3": r"\{", "\uf8f4": "|", "\uf8f6": ")",
    "\uf8f7": ")", "\uf8f8": ")", "\uf8f9": "]", "\uf8fa": "]", "\uf8fb": "]",
    "\uf8fc": r"\}", "\uf8fd": r"\}", "\uf8fe": r"\}", "\uf8e6": "|",
}  # fmt: skip

#: The Symbol font's encoding: the character each code draws.
_SYMBOL_ENCODING = {
    **dict(zip(map(ord, "abcdefghijklmnopqrstuvwxyz"), "αβχδεφγηιϕκλμνοπθρστυϖωξψζ", strict=True)),
    **dict(zip(map(ord, "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ", strict=True)),
    0x22: "∀", 0x24: "∃", 0x27: "∋", 0x2A: "∗", 0x2D: "−", 0x40: "≅", 0x5C: "∴", 0x5E: "⊥",
    0xA1: "ϒ", 0xA2: "′", 0xA3: "≤", 0xA5: "∞", 0xA6: "ƒ", 0xAB: "↔", 0xAC: "←", 0xAD: "↑",
    0xAE: "→", 0xAF: "↓", 0xB0: "°", 0xB1: "±", 0xB2: "″", 0xB3: "≥", 0xB4: "×", 0xB5: "∝",
    0xB6: "∂", 0xB7: "•", 0xB8: "÷", 0xB9: "≠", 0xBA: "≡", 0xBB: "≈", 0xBC: "…", 0xC0: "ℵ",
    0xC1: "ℑ", 0xC2: "ℜ", 0xC3: "℘", 0xC4: "⊗", 0xC5: "⊕", 0xC6: "∅", 0xC7: "∩", 0xC8: "∪",
    0xC9: "⊃", 0xCA: "⊇", 0xCB: "⊄", 0xCC: "⊂", 0xCD: "⊆", 0xCE: "∈", 0xCF: "∉", 0xD0: "∠",
    0xD1: "∇", 0xD5: "∏", 0xD6: "√", 0xD7: "⋅", 0xD8: "¬", 0xD9: "∧", 0xDA: "∨", 0xDB: "⇔",
    0xDC: "⇐", 0xDD: "⇑", 0xDE: "⇒", 0xDF: "⇓", 0xE1: "〈", 0xE5: "∑", 0xF1: "〉", 0xF2: "∫",
}  # fmt: skip

_PIECE_DELIMS = {
    **dict.fromkeys("⎛⎜⎝", "("), **dict.fromkeys("⎞⎟⎠", ")"), **dict.fromkeys("⎡⎢⎣", "["),
    **dict.fromkeys("⎤⎥⎦", "]"), **dict.fromkeys("⎧⎨⎩", r"\{"),
    **dict.fromkeys("⎫⎬⎭", r"\}"), "⎪": "|", "⎮": "|",
}  # fmt: skip


#: Unicode characters that only occur in mathematics: a glyph showing one is
#: evidence of an equation whatever font it is set in.
def is_math_char(char: str) -> bool:
    if not char:
        return False
    code = ord(char[0])
    if 0x2200 <= code <= 0x22FF or 0x27C0 <= code <= 0x27EF or 0x2980 <= code <= 0x2AFF:
        return True
    if 0x1D400 <= code <= 0x1D7FF or 0x2190 <= code <= 0x21FF or 0x23A1 <= code <= 0x23B3:
        return True
    return char in "αβγδϵεζηθϑικλμνξπϖρϱσςτυϕφχψωΓΔΘΛΞΠΣΥΦΨΩ∞∂∇±×÷√≤≥≠≈≡∑∏∫ℓℝℕℤℚℂ⟨⟩‖′"
