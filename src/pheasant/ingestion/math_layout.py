"""One typeset equation's glyphs as TeX: the two-dimensional half of PDF math.

A PDF does not say "fraction" or "subscript". It says: draw this glyph here,
at this size, and fill this thin rectangle there. TeX (or Word, or
LibreOffice) turned structure into position, and this module turns position
back into structure, deterministically, from rules a typesetter cannot help
following:

- a thin horizontal **rule** with glyphs centred above and below it is a
  fraction bar; with glyphs only beneath it, an overline;
- a glyph **overlapping another horizontally** sits above or below it — an
  accent over its base, a limit over its operator;
- a **smaller glyph off the baseline** after an atom is that atom's script,
  raised or lowered;
- a radical sign owns what its overline covers; a tall delimiter owns what
  lies between it and its partner, in rows when there is more than one.

Each rule replaces a set of atoms with one composite atom carrying its TeX and
its box, and what remains is read left to right along the main baseline. The
input is a list of :class:`Atom` (one per glyph, already decoded) and
:class:`Rule`; the output is a TeX string. No font files, no rendering, no
model: the same geometry always produces the same TeX.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median

from pheasant.ingestion.omml import base as _base
from pheasant.ingestion.omml import group as _group
from pheasant.ingestion.omml import join
from pheasant.ingestion.tex_symbols import (
    BIG_OPERATORS,
    CLOSERS,
    FUNCTION_NAMES,
    LIMIT_FUNCTIONS,
    OPENERS,
)

#: Recursion bound: a region nested deeper than this is not an equation.
MAX_DEPTH = 24

_NAMES_LONGEST_FIRST = sorted(FUNCTION_NAMES, key=len, reverse=True)

#: Glyphs whose size says nothing about where the baseline is: a delimiter
#: grown to fit, a radical sign, a big operator.
_NOT_BASELINE = OPENERS | CLOSERS | BIG_OPERATORS | {"RADICAL"}


@dataclass
class Atom:
    """A glyph (or a composite of glyphs) with its box, y growing downward."""

    tex: str
    x0: float
    x1: float
    top: float
    bottom: float
    baseline: float
    size: float
    upright: bool = False
    composite: bool = False
    #: For a big operator: whether its limits may sit above and below it.
    limits: bool = False
    #: Prose inside an equation (``cases``' "if"), set as ``\text``.
    text: bool = False

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def width(self) -> float:
        return max(self.x1 - self.x0, 0.01)

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 0.01)


@dataclass
class Rule:
    """A thin horizontal line: a fraction bar, an overline, a radical's vinculum."""

    x0: float
    x1: float
    y: float
    used: bool = field(default=False, compare=False)

    @property
    def width(self) -> float:
        return self.x1 - self.x0


def _hoverlap(a: Atom, x0: float, x1: float) -> float:
    return max(0.0, min(a.x1, x1) - max(a.x0, x0))


def _union(tex: str, atoms: list[Atom], rules: list[Rule], **extra: object) -> Atom:
    xs0 = [a.x0 for a in atoms] + [r.x0 for r in rules]
    xs1 = [a.x1 for a in atoms] + [r.x1 for r in rules]
    tops = [a.top for a in atoms] + [r.y for r in rules]
    bottoms = [a.bottom for a in atoms] + [r.y for r in rules]
    size = median([a.size for a in atoms]) if atoms else 10.0
    baseline = extra.pop("baseline", None)
    return Atom(
        tex=tex,
        x0=min(xs0),
        x1=max(xs1),
        top=min(tops),
        bottom=max(bottoms),
        baseline=float(baseline) if baseline is not None else max(a.baseline for a in atoms),
        size=size,
        composite=True,
        **extra,  # type: ignore[arg-type]
    )


class _Parser:
    def __init__(self) -> None:
        self.depth = 0

    def parse(self, atoms: list[Atom], rules: list[Rule]) -> str:
        if not atoms:
            return ""
        self.depth += 1
        try:
            if self.depth > MAX_DEPTH:
                return join(a.tex for a in sorted(atoms, key=lambda a: a.x0))
            atoms = list(atoms)
            rules = [r for r in rules if not r.used]
            atoms = self._words(atoms)
            atoms = self._fractions(atoms, rules)
            atoms = self._radicals(atoms, rules)
            atoms = self._accents(atoms, rules)
            atoms = self._limits(atoms)
            atoms = self._delimited(atoms, rules)
            return self._row(atoms, rules)
        finally:
            self.depth -= 1

    # -- upright words -----------------------------------------------------------

    def _words(self, atoms: list[Atom]) -> list[Atom]:
        """Adjacent upright letters naming a function become that function."""

        ordered = sorted(atoms, key=lambda a: (round(a.baseline), a.x0))
        out: list[Atom] = []
        index = 0
        while index < len(ordered):
            atom = ordered[index]
            run = [atom]
            if atom.tex.isalpha() and len(atom.tex) == 1:
                cursor = index + 1
                while cursor < len(ordered):
                    nxt = ordered[cursor]
                    if not (
                        nxt.upright == atom.upright and nxt.tex.isalpha() and len(nxt.tex) == 1
                    ):
                        break
                    if abs(nxt.baseline - atom.baseline) > 0.1 * atom.size:
                        break
                    if nxt.x0 - run[-1].x1 > 0.12 * atom.size:
                        break
                    run.append(nxt)
                    cursor += 1
            word = "".join(a.tex for a in run)
            # Italic letters are variables unless they spell a longer name:
            # `det` set in italic (LibreOffice does) is still the determinant.
            prefix = next((name for name in _NAMES_LONGEST_FIRST if word.startswith(name)
                           and (atom.upright or len(name) >= 3)), None)  # fmt: skip
            if all(a.text for a in run):
                out.append(_union(r"\text{ " + word + " }", run, [], baseline=atom.baseline))
            elif len(run) > 1 and atom.upright and not prefix:
                out.append(_union(r"\operatorname{" + word + "}", run, [], baseline=atom.baseline))
            elif len(run) > 1 and prefix:
                head = run[: len(prefix)]
                out.append(_union("\\" + prefix, head, [], baseline=atom.baseline,
                                  limits=prefix in LIMIT_FUNCTIONS))  # fmt: skip
                out.extend(run[len(prefix) :])
            else:
                out.extend(run)
            index += len(run)
        return out

    # -- fractions ---------------------------------------------------------------

    def _stack(self, atoms: list[Atom], rule: Rule, above: bool, gap: float) -> list[Atom]:
        """Atoms stacked on one side of ``rule``, grown outward while they touch."""

        pad = 0.15 * gap
        span = [a for a in atoms if rule.x0 - pad <= a.cx <= rule.x1 + pad]
        if above:
            side = [a for a in span if a.cy < rule.y]
            edge = rule.y
            taken: list[Atom] = []
            for atom in sorted(side, key=lambda a: -a.bottom):
                if edge - atom.bottom <= gap:
                    taken.append(atom)
                    edge = min(edge, atom.top)
            return taken
        side = [a for a in span if a.cy > rule.y]
        edge = rule.y
        taken = []
        for atom in sorted(side, key=lambda a: a.top):
            if atom.top - edge <= gap:
                taken.append(atom)
                edge = max(edge, atom.bottom)
        return taken

    def _fractions(self, atoms: list[Atom], rules: list[Rule]) -> list[Atom]:
        for rule in sorted(rules, key=lambda r: -r.width):
            if rule.used:
                continue
            size = median([a.size for a in atoms]) if atoms else 10.0
            gap = 0.55 * size
            num = self._stack(atoms, rule, True, gap)
            den = self._stack(atoms, rule, False, gap)
            if not num or not den:
                continue
            rule.used = True
            inner = [r for r in rules if not r.used and rule.x0 - 1 <= r.x0 and r.x1 <= rule.x1 + 1]
            top_rules = [r for r in inner if r.y < rule.y]
            bottom_rules = [r for r in inner if r.y > rule.y]
            tex = (
                r"\frac{" + self.parse(num, top_rules) + "}{" + self.parse(den, bottom_rules) + "}"
            )
            for r in inner:
                r.used = True
            members = set(map(id, num + den))
            atoms = [a for a in atoms if id(a) not in members]
            # The bar sits on the math axis, about a quarter em above the baseline.
            atoms.append(_union(tex, num + den, [rule], baseline=rule.y + 0.25 * size))
        return atoms

    # -- radicals ----------------------------------------------------------------

    def _radicals(self, atoms: list[Atom], rules: list[Rule]) -> list[Atom]:
        for radical in sorted([a for a in atoms if a.tex == "RADICAL"], key=lambda a: a.x0):
            if radical not in atoms:
                continue
            tol = 0.35 * radical.size
            vinculum = next(
                (r for r in rules if not r.used and abs(r.x0 - radical.x1) <= tol
                 and radical.top - tol <= r.y <= radical.cy),
                None,
            )  # fmt: skip
            rest = [a for a in atoms if a is not radical]
            if vinculum is not None:
                vinculum.used = True
                body = [a for a in rest if vinculum.x0 - 0.5 <= a.cx <= vinculum.x1 + 0.5
                        and a.cy > vinculum.y and a.top <= radical.bottom]  # fmt: skip
            else:
                after = sorted([a for a in rest if a.x0 >= radical.x1 - tol], key=lambda a: a.x0)
                body = after[:1]
            body_size = median([a.size for a in body]) if body else radical.size
            index = [a for a in rest if a not in body
                     and _hoverlap(a, radical.x0 - 0.2 * radical.width, radical.cx) > 0
                     and a.bottom <= radical.top + 0.6 * radical.height
                     and a.size <= 0.8 * body_size]  # fmt: skip
            inner_rules = [r for r in rules if not r.used and vinculum is not None
                           and vinculum.x0 - 1 <= r.x0 and r.x1 <= vinculum.x1 + 1
                           and r.y > vinculum.y]  # fmt: skip
            tex = r"\sqrt"
            if index:
                tex += "[" + self.parse(index, []) + "]"
            tex += "{" + self.parse(body, inner_rules) + "}"
            used = set(map(id, [radical, *body, *index]))
            baseline = body[0].baseline if body else radical.baseline
            atoms = [a for a in atoms if id(a) not in used]
            lowest = max((a.baseline for a in body), default=baseline)
            members = [radical, *body, *index]
            atoms.append(_union(tex, members, [vinculum] if vinculum else [], baseline=lowest))
        return atoms

    # -- accents and overlines ---------------------------------------------------

    def _accents(self, atoms: list[Atom], rules: list[Rule]) -> list[Atom]:
        for accent in [a for a in atoms if a.tex.startswith("ACCENT:")]:
            targets = [a for a in atoms if a is not accent and not a.tex.startswith("ACCENT:")
                       and _hoverlap(a, accent.x0, accent.x1) >= 0.4 * min(accent.width, a.width)
                       and a.cy >= accent.cy - 0.05 * a.size]  # fmt: skip
            if not targets:
                continue
            target = min(targets, key=lambda a: abs(a.cx - accent.cx))
            command = accent.tex.split(":", 1)[1]
            atoms = [a for a in atoms if a is not accent and a is not target]
            atoms.append(_union(command + "{" + target.tex + "}", [target, accent], [],
                                baseline=target.baseline))  # fmt: skip
        for rule in [r for r in rules if not r.used]:
            under = [a for a in atoms if rule.x0 - 0.5 <= a.cx <= rule.x1 + 0.5 and a.cy > rule.y
                     and a.top - rule.y <= 0.4 * a.size]  # fmt: skip
            over = [a for a in atoms if rule.x0 - 0.5 <= a.cx <= rule.x1 + 0.5 and a.cy < rule.y
                    and rule.y - a.bottom <= 0.4 * a.size]  # fmt: skip
            if under and not over:
                rule.used = True
                tex = r"\overline{" + self.parse(under, []) + "}"
                atoms = [a for a in atoms if a not in under]
                atoms.append(_union(tex, under, [rule], baseline=max(a.baseline for a in under)))
            elif over and not under and len(over) > 1:
                rule.used = True
                tex = r"\underline{" + self.parse(over, []) + "}"
                atoms = [a for a in atoms if a not in over]
                atoms.append(_union(tex, over, [rule], baseline=max(a.baseline for a in over)))
        return atoms

    # -- limits over and under big operators ---------------------------------------

    def _limits(self, atoms: list[Atom]) -> list[Atom]:
        for op in sorted([a for a in atoms if a.limits], key=lambda a: a.x0):
            if op not in atoms:
                continue
            pad = 0.3 * op.width + 0.1 * op.size
            reach = 0.2 * op.height
            stacked = [a for a in atoms if a is not op and op.x0 - pad <= a.cx <= op.x1 + pad
                       and (a.bottom <= op.top + reach or a.top >= op.bottom - reach)
                       and a.size <= op.size * 1.05]  # fmt: skip
            above = self._chain(stacked, op, upward=True)
            below = self._chain(stacked, op, upward=False)
            if not above and not below:
                continue
            tex = op.tex
            if below:
                tex += "_" + _group(self.parse(below, []))
            if above:
                tex += "^" + _group(self.parse(above, []))
            used = set(map(id, [op, *above, *below]))
            atoms = [a for a in atoms if id(a) not in used]
            atoms.append(_union(tex, [op, *above, *below], [], baseline=op.baseline,
                                limits=False))  # fmt: skip
        return atoms

    @staticmethod
    def _chain(candidates: list[Atom], op: Atom, upward: bool) -> list[Atom]:
        gap = 0.6 * op.size
        taken: list[Atom] = []
        if upward:
            edge = op.top
            for atom in sorted([a for a in candidates if a.cy < op.cy], key=lambda a: -a.bottom):
                if edge - atom.bottom <= gap:
                    taken.append(atom)
                    edge = min(edge, atom.top)
        else:
            edge = op.bottom
            for atom in sorted([a for a in candidates if a.cy > op.cy], key=lambda a: a.top):
                if atom.top - edge <= gap:
                    taken.append(atom)
                    edge = max(edge, atom.bottom)
        return taken

    # -- tall delimiters: matrices, cases, binomials ----------------------------------

    def _delimited(self, atoms: list[Atom], rules: list[Rule]) -> list[Atom]:
        atoms = self._assemble_pieces(atoms)
        if not atoms:
            return atoms
        line = median([a.size for a in atoms if not a.composite] or [a.size for a in atoms])
        tall = sorted([a for a in atoms if a.tex in OPENERS | CLOSERS and a.height >= 1.9 * line],
                      key=lambda a: a.x0)  # fmt: skip
        for opener in [a for a in tall if a.tex in OPENERS]:
            if opener not in atoms:
                continue
            closer = _partner(opener, [t for t in tall if t in atoms])
            # A brace with no partner (cases) ends where the next tall delimiter
            # starts: `\begin{cases}…\end{cases} \qquad A = (…)` is two things.
            right = (
                closer.x0
                if closer
                else min(
                    [t.x0 for t in tall if t.x0 > opener.x1 and t is not opener]
                    + [max(a.x1 for a in atoms) + 1]
                )
            )
            low, high = opener.top - 0.1 * line, opener.bottom + 0.1 * line
            left = opener.x1 - 0.5
            inside = [a for a in atoms if a is not opener and a is not closer
                      and left <= a.cx <= right + 0.5 and low <= a.cy <= high]  # fmt: skip
            if closer is None and opener.tex != r"\{":
                continue
            rows = _rows(inside, line)
            if closer is None:
                # A cases row starts at its brace; anything else in its band
                # (what follows it on the line) is not part of it.
                rows = [row for row in rows if min(a.x0 for a in row) <= opener.x1 + line]
                inside = [a for row in rows for a in row]
            closing = closer.tex if closer else "."
            if (
                len(rows) > 1
                and all(len(_cells(row, line)) == 1 for row in rows)
                and len(rows) == 2
                and (opener.tex, closing) == ("(", ")")
            ):
                tex = r"\binom{" + self.parse(rows[0], []) + "}{" + self.parse(rows[1], []) + "}"
            elif len(rows) > 1:
                env = {("(", ")"): "pmatrix", ("[", "]"): "bmatrix", ("|", "|"): "vmatrix",
                       (r"\|", r"\|"): "Vmatrix", (r"\{", r"\}"): "Bmatrix",
                       (r"\{", "."): "cases"}.get((opener.tex, closing))  # fmt: skip
                body = r" \\ ".join(" & ".join(self.parse(cell, []) for cell in _cells(row, line))
                                    for row in rows)  # fmt: skip
                if env is None:
                    tex = (
                        r"\left"
                        + opener.tex
                        + r" \begin{matrix} "
                        + body
                        + r" \end{matrix} \right"
                        + closing
                    )
                else:
                    tex = r"\begin{" + env + "} " + body + r" \end{" + env + "}"
            else:
                inner_rules = [
                    r for r in rules if not r.used and opener.x1 <= r.x0 and r.x1 <= right
                ]
                tex = (
                    r"\left"
                    + opener.tex
                    + " "
                    + self.parse(inside, inner_rules)
                    + r" \right"
                    + closing
                )
            members = [opener, *inside, *([closer] if closer else [])]
            used = set(map(id, members))
            atoms = [a for a in atoms if id(a) not in used]
            atoms.append(_union(tex, members, [], baseline=opener.cy + 0.25 * line))
        return atoms

    @staticmethod
    def _assemble_pieces(atoms: list[Atom]) -> list[Atom]:
        """Stacked pieces of one big delimiter (⎛ ⎜ ⎝) become one tall atom."""

        pieces = [a for a in atoms if a.tex.startswith("PIECE:")]
        if not pieces:
            return atoms
        rest = [a for a in atoms if not a.tex.startswith("PIECE:")]
        groups: list[list[Atom]] = []
        for piece in sorted(pieces, key=lambda a: (a.tex, a.x0, a.top)):
            for group in groups:
                last = group[-1]
                if last.tex == piece.tex and _hoverlap(piece, last.x0, last.x1) > 0.3 * piece.width:
                    group.append(piece)
                    break
            else:
                groups.append([piece])
        for group in groups:
            tex = group[0].tex.split(":", 1)[1]
            rest.append(_union(tex, group, [], baseline=max(a.baseline for a in group)))
            rest[-1].composite = False
        return rest

    # -- the baseline ---------------------------------------------------------------

    def _row(self, atoms: list[Atom], rules: list[Rule]) -> str:
        atoms = sorted(atoms, key=lambda a: (a.x0, a.top))
        if not atoms:
            return ""
        main = _main_baseline(atoms)
        line = max((a.size for a in atoms if a.tex not in _NOT_BASELINE and not a.composite),
                   default=atoms[0].size)  # fmt: skip
        out: list[str] = []
        index = 0
        previous: Atom | None = None
        reach = atoms[0].x0  # right edge of the last base and its scripts
        while index < len(atoms):
            atom = atoms[index]
            if (
                _on_baseline(atom, main, line)
                or previous is None
                and not _on_baseline(atom, main, line)
            ):
                if previous is not None and atom.x0 - reach > 0.9 * atom.size:
                    out.append(r"\qquad" if atom.x0 - reach > 1.8 * atom.size else r"\quad")
                following = atoms[index + 1] if index + 1 < len(atoms) else None
                if (
                    atom.tex == "|"
                    and previous is not None
                    and following is not None
                    and (atom.x0 - reach > 0.15 * line and following.x0 - atom.x1 > 0.15 * line)
                ):
                    out.append(r"\mid")  # spaced as a relation, not an absolute value
                else:
                    out.append(atom.tex)
                previous = atom
                reach = atom.x1
                index += 1
                continue
            # Scripts: every following atom off the baseline belongs to the last base.
            scripts = []
            while index < len(atoms) and not _on_baseline(atoms[index], main, line):
                scripts.append(atoms[index])
                reach = max(reach, atoms[index].x1)
                index += 1
            anchor = previous
            # Raised or lowered relative to the base: for a big operator or a
            # grown delimiter that is its middle, not its origin.
            if anchor is None:
                middle = main - 0.1 * scripts[0].size
            elif anchor.tex in _NOT_BASELINE or re.split(r"[_^]", anchor.tex)[0] in BIG_OPERATORS:
                middle = anchor.cy
            else:
                middle = anchor.baseline - 0.1 * anchor.size
            sups = [a for a in scripts if a.baseline < middle]
            subs = [a for a in scripts if a.baseline >= middle]
            head = out.pop() if out and anchor is not None else "{}"
            text = _base(head) if anchor is not None else head
            if subs:
                text += "_" + _group(self.parse(subs, []))
            if sups:
                raised = self.parse(sups, [])
                # A prime is a superscript TeX writes without the caret.
                text += raised if re.fullmatch(r"'+", raised) else "^" + _group(raised)
            out.append(text)
        return join(out)


def _partner(opener: Atom, tall: list[Atom]) -> Atom | None:
    """The tall delimiter closing ``opener``, skipping any pair nested inside.

    Of a similar height and centre, at the same depth: in
    ``\\{ \\ldots \\qquad (\\ldots)`` the parenthesis closes its own opening one, and
    the brace (a ``cases``) has none.
    """
    depth = 0
    height = opener.height
    for atom in sorted(tall, key=lambda a: a.x0):
        if atom.x0 <= opener.x1 or abs(atom.cy - opener.cy) >= 0.3 * height:
            continue
        if abs(atom.height - height) >= 0.4 * height:
            continue
        if atom.tex in CLOSERS and atom.tex not in OPENERS or atom.tex in CLOSERS and depth > 0:
            if depth == 0:
                return atom
            depth -= 1
        elif atom.tex in OPENERS:
            depth += 1
    return None


def _main_baseline(atoms: list[Atom]) -> float:
    """The baseline most of the equation's largest atoms sit on."""

    ordinary = [a for a in atoms if not a.composite and a.tex not in _NOT_BASELINE] or atoms
    biggest = max(a.size for a in ordinary)
    weights: dict[float, float] = {}
    for atom in ordinary:
        if atom.size < 0.8 * biggest:
            continue
        key = round(atom.baseline * 2) / 2
        weights[key] = weights.get(key, 0.0) + atom.width + (5 if atom.composite else 0)
    return max(weights.items(), key=lambda item: (item[1], -item[0]))[0]


def _on_baseline(atom: Atom, main: float, line: float) -> bool:
    """Whether ``atom`` sits on the main baseline rather than in a script.

    A script is set smaller, so a smaller atom needs only a slight offset to
    be one (an inline subscript drops by about 0.15 em); a full-size atom
    off the baseline is more likely the typesetter's rounding.
    """
    if atom.tex == r"\not":
        return True  # the slash over a relation, drawn wherever the font puts it
    if atom.composite or atom.limits or atom.tex in _NOT_BASELINE:
        return atom.top - 0.2 * atom.size <= main <= atom.bottom + 0.3 * atom.size
    offset = abs(atom.baseline - main)
    if atom.size <= 0.85 * line:
        return offset <= 0.08 * line
    return offset <= 0.18 * line


def _rows(atoms: list[Atom], line: float) -> list[list[Atom]]:
    rows: list[list[Atom]] = []
    for atom in sorted(atoms, key=lambda a: a.cy):
        if rows and abs(atom.cy - median(a.cy for a in rows[-1])) < 0.6 * line:
            rows[-1].append(atom)
        else:
            rows.append([atom])
    return rows


def _cells(row: list[Atom], line: float) -> list[list[Atom]]:
    cells: list[list[Atom]] = []
    for atom in sorted(row, key=lambda a: a.x0):
        if cells and atom.x0 - max(a.x1 for a in cells[-1]) < 0.75 * line:
            cells[-1].append(atom)
        else:
            cells.append([atom])
    return cells


def layout_to_tex(atoms: list[Atom], rules: list[Rule]) -> str:
    """The TeX one equation's atoms and rules describe."""

    return _Parser().parse(atoms, [Rule(r.x0, r.x1, r.y) for r in rules])
