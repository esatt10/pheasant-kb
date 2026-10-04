"""Lexical groundwork for :mod:`pheasant.graph.code_analysis`.

Pattern-based code analysis is only as good as what it is allowed to see. A
``foo(`` inside a comment is not a call, an ``import`` in a docstring is not a
dependency, and a ``}`` inside a string does not close a block. So before any
pattern runs, :func:`mask` blanks comments (and, for the symbol and call
passes, string contents) with spaces. Every offset and every newline survives,
so a match's position still names the line it came from.

Nothing here parses. It is a small, deterministic scanner per comment and
string syntax, which is what lets the indexing path stay free of a native
grammar dependency (CLAUDE.md rule 1 is about determinism; this is also about
the image staying one ``pip install``).
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from functools import cache


@dataclass(frozen=True)
class Syntax:
    """How one language spells comments and strings."""

    line_comments: tuple[str, ...] = ("//",)
    block_comments: tuple[tuple[str, str], ...] = (("/*", "*/"),)
    #: Longest first: ``\"\"\"`` must be tried before ``\"``.
    quotes: tuple[str, ...] = ('"', "'")
    #: A ``#`` comment only at a word boundary (shell: ``$#`` is not one).
    hash_needs_space: bool = False


C_LIKE = Syntax()
JS = Syntax(quotes=('"', "'", "`"))
GO = Syntax(quotes=('"', "`", "'"))
TRIPLE = Syntax(quotes=('"""', '"', "'"))
DART = Syntax(quotes=("'''", '"""', '"', "'"))
RUST = Syntax(quotes=('"',))
CSHARP = Syntax(quotes=('"', "'"))
HASH = Syntax(line_comments=("#",), block_comments=(), quotes=('"', "'"))
ELIXIR = Syntax(line_comments=("#",), block_comments=(), quotes=('"""', '"', "'"))
SHELL = Syntax(line_comments=("#",), block_comments=(), quotes=('"', "'"), hash_needs_space=True)
PHP = Syntax(line_comments=("//", "#"), quotes=('"', "'"))
HASKELL = Syntax(line_comments=("--",), block_comments=(("{-", "-}"),), quotes=('"',))
LUA = Syntax(line_comments=("--",), block_comments=(("--[[", "]]"),), quotes=('"', "'"))
LISP = Syntax(line_comments=(";",), block_comments=(), quotes=('"',))
ERLANG = Syntax(line_comments=("%",), block_comments=(), quotes=('"',))
ZIG = Syntax(block_comments=(), quotes=('"', "'"))


def mask(text: str, syntax: Syntax) -> tuple[str, str]:
    """``(comments_masked, comments_and_strings_masked)`` for ``text``.

    Both are the same length as ``text`` with the same newlines. One pass
    produces both: a compiled pattern jumps from one comment or string opener
    to the next, so the cost is the number of tokens rather than the number of
    characters, which matters on the indexing path.
    """

    token, kinds = _tokens(syntax)
    keep: list[str] = []
    bare: list[str] = []
    n = len(text)
    pos = 0
    while pos < n:
        match = token.search(text, pos)
        if match is None:
            keep.append(text[pos:])
            bare.append(text[pos:])
            break
        start = match.start()
        if start > pos:
            keep.append(text[pos:start])
            bare.append(text[pos:start])
        opener = match.group()
        kind, closer = kinds[opener]
        if kind == "block":
            end = text.find(closer, match.end())
            end = n if end < 0 else end + len(closer)
            blanked = _blank(text[start:end])
            keep.append(blanked)
            bare.append(blanked)
        elif kind == "line":
            end = text.find("\n", start)
            end = n if end < 0 else end
            blanked = _blank(text[start:end])
            keep.append(blanked)
            bare.append(blanked)
        else:
            body_end, end = _string_end(text, match.end(), opener)
            keep.append(text[start:end])
            bare.append(opener + _blank(text[match.end() : body_end]) + text[body_end:end])
        pos = end
    return "".join(keep), "".join(bare)


@cache
def _tokens(syntax: Syntax) -> tuple[re.Pattern[str], dict[str, tuple[str, str]]]:
    kinds: dict[str, tuple[str, str]] = {}
    for opener, closer in syntax.block_comments:
        kinds.setdefault(opener, ("block", closer))
    for marker in syntax.line_comments:
        kinds.setdefault(marker, ("line", ""))
    for quote in syntax.quotes:
        kinds.setdefault(quote, ("string", quote))
    alternatives = []
    # Longest first: at one position the first listed alternative wins, so
    # `"""` must precede `"` and `--[[` must precede `--`.
    for opener in sorted(kinds, key=len, reverse=True):
        escaped = re.escape(opener)
        if opener == "#" and syntax.hash_needs_space:
            escaped = r"(?:(?<=\s)|^)#"  # shell: `$#` is not a comment
        alternatives.append(escaped)
    return re.compile("|".join(alternatives), re.MULTILINE), kinds


@cache
def _string_body(quote: str) -> re.Pattern[str]:
    if len(quote) == 1:
        # A single-character quote never spans a line, so an unbalanced
        # apostrophe (a lifetime, a contraction) costs one line, not the file.
        return re.compile(rf"(?:\\[\s\S]|[^\\{re.escape(quote)}\n])*")
    return re.compile(rf"(?:\\[\s\S]|(?!{re.escape(quote)})[\s\S])*")


def _string_end(text: str, start: int, quote: str) -> tuple[int, int]:
    """``(body_end, end)``: where the string's contents stop, and the offset
    just past its closing quote (equal when it is unterminated)."""

    body_end = _string_body(quote).match(text, start).end()
    if text.startswith(quote, body_end):
        return body_end, body_end + len(quote)
    return body_end, body_end


_NOT_NEWLINE = re.compile(r"[^\n]")


def _blank(value: str) -> str:
    return _NOT_NEWLINE.sub(" ", value)


class Lines:
    """Offset -> 1-based line number, by bisection over newline offsets."""

    def __init__(self, text: str) -> None:
        self._starts = [0] + [k + 1 for k, ch in enumerate(text) if ch == "\n"]

    def of(self, offset: int) -> int:
        return bisect_right(self._starts, offset)

    @property
    def count(self) -> int:
        return len(self._starts)


#: How far a definition's name may be from the ``{`` that opens its body.
#: Generous for a long signature; the bound is what keeps a file full of
#: bodiless definitions (arrow functions, prototypes) linear.
MAX_SIGNATURE_CHARS = 2000


def pairs(masked: str, opener: str, closer: str) -> dict[int, int]:
    """Every balanced ``opener`` offset mapped to its ``closer``, in one pass.

    Matching each definition's block by scanning forward is quadratic when the
    blocks never close; a stack over the whole file is linear whatever it
    holds.
    """

    matched: dict[int, int] = {}
    stack: list[int] = []
    for k, ch in enumerate(masked):
        if ch == opener:
            stack.append(k)
        elif ch == closer and stack:
            matched[stack.pop()] = k
    return matched


def brace_end(masked: str, start: int, braces: dict[int, int]) -> int | None:
    """Offset of the ``}`` closing the first ``{`` at or after ``start``.

    ``None`` when a ``;`` or a blank line comes first, or nothing within
    :data:`MAX_SIGNATURE_CHARS`: a prototype, a forward declaration, an
    abstract method or an expression body (Kotlin's ``fun f() = 1``), none of
    which has a block to end. Without the blank-line stop, an
    expression-bodied definition would borrow the next one's body.
    """

    window = masked[start : start + MAX_SIGNATURE_CHARS]
    found = _BODY_OR_STOP.search(window)
    if found is None or found.group() != "{":
        return None
    return braces.get(start + found.start())


_BODY_OR_STOP = re.compile(r"\{|;|\n[ \t]*\n")


def paren_end(open_at: int, parens: dict[int, int]) -> int | None:
    return parens.get(open_at)


def indent_end(lines: list[str], start_line: int) -> int:
    """Last line of an indentation-delimited block starting at ``start_line``.

    The block runs until the next non-blank line indented no deeper than the
    definition; a closing ``end`` at that indent (Ruby, Elixir, Lua) belongs to
    it.
    """

    head = lines[start_line - 1]
    indent = len(head) - len(head.lstrip())
    last = start_line
    for number in range(start_line + 1, len(lines) + 1):
        line = lines[number - 1]
        stripped = line.strip()
        if not stripped:
            continue
        current = len(line) - len(line.lstrip())
        if current <= indent:
            if stripped == "end" or stripped.startswith("end "):
                return number
            return last
        last = number
    return last
