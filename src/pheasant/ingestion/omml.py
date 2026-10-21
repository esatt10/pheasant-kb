"""Office Math (OMML) as LaTeX: the equations a DOCX holds, as text.

A Word equation is not text in ``word/document.xml``. It is an ``m:oMath``
tree — fractions, scripts, radicals, n-ary operators, delimiters, matrices —
whose leaves are ``m:t`` runs, and both DOCX readers skipped it whole: the
builtin read only ``w:t`` and python-docx's ``paragraph.text`` reads only
``w:r``. An equation was therefore not merely garbled, it was absent, and a
document about a formula indexed every word around it and not the formula.

This walks the tree and writes the LaTeX it describes, deterministically: one
OMML element is one TeX construct, so the conversion is a function of the
XML. Inline equations become ``$…$`` and an equation paragraph
(``m:oMathPara``) becomes ``$$…$$``, the delimiters the UI typesets.

It takes any ElementTree-shaped element — ``xml.etree`` from the builtin
reader or lxml from python-docx — and touches only ``tag``, ``text``,
``get`` and child iteration, so both readers share one conversion.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from pheasant.ingestion.tex_symbols import (
    ACCENTS,
    FUNCTION_NAMES,
    LIMIT_FUNCTIONS,
    unicode_to_tex,
)

M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

OMATH = M + "oMath"
OMATH_PARA = M + "oMathPara"

#: Nesting deeper than any real equation; a hostile document cannot recurse
#: the converter into the interpreter's limit.
MAX_DEPTH = 64

_DELIMS = {
    "(": "(", ")": ")", "[": "[", "]": "]", "{": r"\{", "}": r"\}", "|": "|", "‖": r"\|",
    "⟨": r"\langle", "⟩": r"\rangle", "〈": r"\langle", "〉": r"\rangle", "⌊": r"\lfloor",
    "⌋": r"\rfloor", "⌈": r"\lceil", "⌉": r"\rceil", "": ".",
}  # fmt: skip

_MATRIX_ENVS = {("(", ")"): "pmatrix", ("[", "]"): "bmatrix", ("|", "|"): "vmatrix",
                ("‖", "‖"): "Vmatrix", ("{", "}"): "Bmatrix"}  # fmt: skip

#: Typographic spaces an equation carries on purpose, as TeX's spacing
#: commands; an ordinary space is TeX's to set and is dropped.
_SPACES = {"\u2001": r"\quad", "\u2003": r"\quad", "\u2002": r"\enspace", "\u2004": r"\;",
           "\u2005": r"\:", "\u205f": r"\:", "\u2006": r"\,", "\u2009": r"\,",
           "\u3000": r"\qquad"}  # fmt: skip

_STYLES = {"double-struck": r"\mathbb", "fraktur": r"\mathfrak", "script": r"\mathcal",
           "sans-serif": r"\mathsf", "monospace": r"\mathtt"}  # fmt: skip


def _local(element: Any) -> str:
    tag = element.tag if isinstance(element.tag, str) else ""
    return tag.rsplit("}", 1)[-1]


def _children(element: Any) -> list[Any]:
    return [child for child in element if isinstance(child.tag, str)]


def _child(element: Any, name: str) -> Any | None:
    for child in _children(element):
        if child.tag == M + name:
            return child
    return None


def _prop(element: Any, container: str, name: str, default: str | None = None) -> str | None:
    """``<m:{container}><m:{name} m:val="…"/></m:{container}>``'s value."""

    props = _child(element, container)
    if props is None:
        return default
    node = _child(props, name)
    if node is None:
        return default
    value = node.get(M + "val")
    return default if value is None else value


def join(parts: Iterable[str]) -> str:
    """Concatenate TeX, keeping a command from swallowing the letter after it."""

    out = ""
    for part in parts:
        if not part:
            continue
        # A command would swallow the letter after it, and a one-character
        # script reads as running into the next variable (`^nx_i`).
        if re.match(r"[A-Za-z0-9\\]", part) and (
            re.search(r"\\[A-Za-z]+$", out)
            and re.match(r"[A-Za-z0-9]", part)
            or re.search(r"[_^][A-Za-z0-9]$", out)
        ):
            out += " "
        out += part
    return out


def _atomic(tex: str) -> bool:
    """Whether ``tex`` is one atom a script can attach to without braces."""

    if len(tex) == 1 or re.fullmatch(r"\\(?:[A-Za-z]+|[^A-Za-z])", tex):
        return True
    if re.fullmatch(r"\\[A-Za-z]+\{[^{}]*\}", tex):
        return True
    return tex.startswith("{") and tex.endswith("}") and _balanced_group(tex)


def _balanced_group(tex: str) -> bool:
    depth = 0
    for index, char in enumerate(tex):
        depth += char == "{"
        depth -= char == "}"
        if depth == 0 and index < len(tex) - 1:
            return False
    return depth == 0


def group(tex: str) -> str:
    """``tex`` as a script argument: bare when one character, braced otherwise."""

    return tex if len(tex) == 1 else "{" + tex + "}"


def base(tex: str) -> str:
    """``tex`` as the base of a script, braced unless it is one atom."""

    if not tex:
        return "{}"
    return tex if _atomic(tex) else "{" + tex + "}"


class _Converter:
    def __init__(self) -> None:
        self.depth = 0

    def seq(self, element: Any | None) -> str:
        if element is None:
            return ""
        return join(self.node(child) for child in _children(element))

    def arg(self, element: Any, name: str) -> str:
        return self.seq(_child(element, name))

    def node(self, element: Any) -> str:
        self.depth += 1
        try:
            if self.depth > MAX_DEPTH:
                return ""
            handler = getattr(self, "_" + _local(element), None)
            if handler is None:
                # Properties (`…Pr`), bookmarks, proofing marks: not content.
                if _local(element).endswith("Pr"):
                    return ""
                return self.seq(element)
            return handler(element)
        finally:
            self.depth -= 1

    # -- leaves ----------------------------------------------------------------

    def _r(self, element: Any) -> str:
        text = "".join(node.text or "" for node in element.iter() if node.tag in (M + "t", W + "t"))
        if not text:
            return ""
        props = _child(element, "rPr")
        style = _prop(element, "rPr", "sty") if props is not None else None
        script = _prop(element, "rPr", "scr") if props is not None else None
        if props is not None and _child(props, "nor") is not None:
            return r"\text{" + _escape_text(text) + "}"
        return run_tex(text, upright=style in ("p", "b"), bold=style in ("b", "bi"), script=script)

    def _t(self, element: Any) -> str:
        return run_tex(element.text or "")

    # -- structures ------------------------------------------------------------

    def _f(self, element: Any) -> str:
        num, den = self.arg(element, "num"), self.arg(element, "den")
        kind = _prop(element, "fPr", "type", "bar")
        if kind == "lin":
            return join([base(num), "/", base(den)])
        if kind == "noBar":
            return r"\genfrac{}{}{0pt}{}{" + num + "}{" + den + "}"
        return r"\frac{" + num + "}{" + den + "}"

    def _sSup(self, element: Any) -> str:
        return base(self.arg(element, "e")) + "^" + group(self.arg(element, "sup"))

    def _sSub(self, element: Any) -> str:
        return base(self.arg(element, "e")) + "_" + group(self.arg(element, "sub"))

    def _sSubSup(self, element: Any) -> str:
        return (
            base(self.arg(element, "e"))
            + "_"
            + group(self.arg(element, "sub"))
            + "^"
            + group(self.arg(element, "sup"))
        )

    def _sPre(self, element: Any) -> str:
        pre = "{}_" + group(self.arg(element, "sub")) + "^" + group(self.arg(element, "sup"))
        return join([pre, self.arg(element, "e")])

    def _rad(self, element: Any) -> str:
        degree = (
            ""
            if _prop(element, "radPr", "degHide") in ("on", "1", "true")
            else self.arg(element, "deg")
        )
        body = self.arg(element, "e")
        return (r"\sqrt[" + degree + "]" if degree else r"\sqrt") + "{" + body + "}"

    def _nary(self, element: Any) -> str:
        operator = unicode_to_tex(_prop(element, "naryPr", "chr", "∫") or "∫")
        sub = "" if _prop(element, "naryPr", "subHide") in ("on", "1") else self.arg(element, "sub")
        sup = "" if _prop(element, "naryPr", "supHide") in ("on", "1") else self.arg(element, "sup")
        head = operator + ("_" + group(sub) if sub else "") + ("^" + group(sup) if sup else "")
        return join([head, self.arg(element, "e")])

    def _d(self, element: Any) -> str:
        opening = _prop(element, "dPr", "begChr", "(")
        closing = _prop(element, "dPr", "endChr", ")")
        separator = unicode_to_tex(_prop(element, "dPr", "sepChr", "|") or "|")
        parts = [self.seq(child) for child in _children(element) if child.tag == M + "e"]
        cells = [child for child in _children(element) if child.tag == M + "e"]
        if len(cells) == 1:
            inner = _children(cells[0])
            only = [child for child in inner if not _local(child).endswith("Pr")]
            if len(only) == 1 and only[0].tag == M + "m":
                env = _MATRIX_ENVS.get((opening or "", closing or ""))
                if env:
                    return self._matrix(only[0], env)
            if (
                len(only) == 1
                and only[0].tag in (M + "m", M + "eqArr")
                and opening == "{"
                and not closing
            ):
                return self._matrix(only[0], "cases")
            if (
                len(only) == 1
                and only[0].tag == M + "f"
                and _prop(only[0], "fPr", "type") == "noBar"
            ):
                if (opening, closing) == ("(", ")"):
                    return (
                        r"\binom{"
                        + self.arg(only[0], "num")
                        + "}{"
                        + self.arg(only[0], "den")
                        + "}"
                    )
        body = join(_interleave(parts, " " + separator + " "))
        left = _DELIMS.get(opening or "", unicode_to_tex(opening or ""))
        right = _DELIMS.get(closing or "", unicode_to_tex(closing or ""))
        return r"\left" + left + " " + body + r" \right" + right

    def _m(self, element: Any) -> str:
        return self._matrix(element, "matrix")

    def _matrix(self, element: Any, env: str) -> str:
        rows = []
        for row in _children(element):
            if row.tag == M + "mr":
                rows.append(
                    " & ".join(self.seq(cell) for cell in _children(row) if cell.tag == M + "e")
                )
            elif row.tag == M + "e":
                rows.append(self.seq(row))
        return r"\begin{" + env + "} " + r" \\ ".join(rows) + r" \end{" + env + "}"

    def _eqArr(self, element: Any) -> str:
        rows = [self.seq(row) for row in _children(element) if row.tag == M + "e"]
        if len(rows) == 1:
            return rows[0]
        return r"\begin{aligned} " + r" \\ ".join(rows) + r" \end{aligned}"

    def _func(self, element: Any) -> str:
        name = self.arg(element, "fName")
        return join([name, " ", self.arg(element, "e")]).replace("  ", " ")

    def _fName(self, element: Any) -> str:
        return self.seq(element)

    def _limLow(self, element: Any) -> str:
        body, limit = self.arg(element, "e"), self.arg(element, "lim")
        if limit in ("⏟", "⏝", r"\underbrace"):
            return r"\underbrace{" + body + "}"
        if body.lstrip("\\") in LIMIT_FUNCTIONS or body in (r"\lim", r"\max", r"\min"):
            return body + "_" + group(limit)
        if body.startswith(r"\underbrace"):
            return body + "_" + group(limit)
        return r"\underset{" + limit + "}{" + body + "}"

    def _limUpp(self, element: Any) -> str:
        body, limit = self.arg(element, "e"), self.arg(element, "lim")
        if limit in ("⏞", "⏜"):
            return r"\overbrace{" + body + "}"
        if body.startswith(r"\overbrace"):
            return body + "^" + group(limit)
        return r"\overset{" + limit + "}{" + body + "}"

    def _acc(self, element: Any) -> str:
        char = _prop(element, "accPr", "chr", "\u0302") or "\u0302"
        command = ACCENTS.get(char, r"\hat")
        return command + "{" + self.arg(element, "e") + "}"

    def _bar(self, element: Any) -> str:
        position = _prop(element, "barPr", "pos", "bot")
        command = r"\overline" if position == "top" else r"\underline"
        return command + "{" + self.arg(element, "e") + "}"

    def _groupChr(self, element: Any) -> str:
        char = _prop(element, "groupChrPr", "chr", "⏟") or "⏟"
        position = _prop(element, "groupChrPr", "pos", "bot")
        body = self.arg(element, "e")
        if char in "⏞⏜" or position == "top":
            return r"\overbrace{" + body + "}"
        if char in "⏟⏝":
            return r"\underbrace{" + body + "}"
        return r"\underset{" + unicode_to_tex(char) + "}{" + body + "}"

    def _borderBox(self, element: Any) -> str:
        return r"\boxed{" + self.arg(element, "e") + "}"

    def _box(self, element: Any) -> str:
        return self.arg(element, "e")

    def _phant(self, element: Any) -> str:
        if _prop(element, "phantPr", "show") in ("0", "off", "false"):
            return r"\phantom{" + self.arg(element, "e") + "}"
        return self.arg(element, "e")

    def _oMath(self, element: Any) -> str:
        return self.seq(element)


def _interleave(parts: list[str], separator: str) -> list[str]:
    out: list[str] = []
    for index, part in enumerate(parts):
        if index:
            out.append(separator)
        out.append(part)
    return out


def _escape_text(text: str) -> str:
    return re.sub(r"([#$%&_{}])", r"\\\1", text).replace("\\\\", r"\textbackslash ")


def run_tex(text: str, upright: bool = False, bold: bool = False, script: str | None = None) -> str:
    """One math run's characters as TeX.

    A run of upright letters naming a function is the function (``sin`` ->
    ``\\sin``); another upright word is ``\\mathrm``; italic letters are
    variables and stay as they are. Spaces are TeX's to set, so they go.
    """
    text = text.replace("\u00a0", " ")
    stripped = text.strip()
    if upright and re.fullmatch(r"[A-Za-z]{2,}", stripped):
        if stripped in FUNCTION_NAMES:
            return "\\" + stripped
        return r"\mathrm{" + stripped + "}"
    pieces = [
        _SPACES.get(char) or unicode_to_tex(char)
        for char in text
        if char in _SPACES or not char.isspace()
    ]
    tex = join(pieces).replace(r"\quad\quad", r"\qquad")
    if script in _STYLES and tex:
        return _STYLES[script] + "{" + tex + "}"
    if bold and tex:
        return r"\mathbf{" + tex + "}"
    return tex


def omml_to_latex(element: Any) -> str:
    """One ``m:oMath`` (or any OMML element) as LaTeX, without delimiters."""

    return re.sub(r"\s+", " ", _Converter().node(element)).strip()


def math_text(element: Any) -> str:
    """An ``m:oMath`` as inline math, or an ``m:oMathPara`` as display math."""

    if element.tag == OMATH_PARA:
        equations = [omml_to_latex(child) for child in element.iter(OMATH)]
        return "\n".join(f"$${tex}$$" for tex in equations if tex)
    tex = omml_to_latex(element)
    return f"${tex}$" if tex else ""


def contains_math(element: Any) -> bool:
    return any(True for _ in element.iter(OMATH))
