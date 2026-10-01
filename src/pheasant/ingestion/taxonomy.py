"""Structural taxonomy extraction for books, procedures and legal documents.

Highly structured documents carry their own outline — Part / Chapter /
Article / Section / § / 1.2.3 / (a) — and that outline is the most useful
retrieval signal they have. "What does § 12.3 say about termination?" is a
different question from "find the word termination", and answering the first
needs the document's hierarchy, not just its text.

Three things already existed for this and none of them were connected:

1. :class:`pheasant.ingestion.chunking.TextChunk` has a ``heading_path``
   field, ``chunks`` has a ``heading_path`` column, and ``chunks_fts``
   indexes it **with BM25 weight 2.0 — double the body text**. It was
   ``NULL`` for every chunk ever indexed, because ``chunks_for_source``
   never passed one.
2. ``docs/graph_model.md`` documents a ``heading`` node type and a
   ``has_heading`` edge type. Neither was ever emitted anywhere in the code.
3. ``graph/enrichment.py`` regexes Markdown headings and hands them to
   ``_add_concept`` — which has been a no-op since concept extraction was
   retired (2026-08-03). The regex still runs and the result is discarded.

So this module fills in a contract the rest of the system already describes,
rather than adding a new one.

Deterministic and offline
-------------------------
Rule-based only: a set of ordered line patterns plus a stack walk to nest
them. No model, no network, no LLM (rule 1 holds by construction). The same
bytes always yield the same taxonomy, so a re-sync of unchanged content
reproduces identical ``heading_path`` values and identical graph nodes.

Opt-in, per source, for a reason
--------------------------------
``sources[].taxonomy.enabled`` defaults to **false**. Two reasons, both real:

- Populating ``heading_path`` changes what the FTS index contains for a
  source, and a weight-2.0 column going from empty to full changes ranking.
  That is an improvement on structured corpora and an unwanted surprise on
  arbitrary ones, so it is an explicit choice per source.
- The numbering rules are genuinely ambiguous on prose. ``1. Introduction``
  in a standards document is a section; ``1. Buy milk`` in a note is a list
  item, and nothing in the line distinguishes them. Heuristics below reduce
  the confusion but cannot remove it — which is exactly why this is enabled
  per source, on the sources where the structure is real.

Level assignment
----------------
Each rule carries its own natural depth, and nesting is then a stack walk,
so a document may mix conventions (``ARTICLE IV`` then ``4.1`` then ``(a)``)
and still nest correctly:

===========================  =====
Pattern                      Level
===========================  =====
Markdown ``#``…``######``    1-6
PART / TITLE / BOOK / DIVISION / SCHEDULE / APPENDIX / ANNEX / EXHIBIT  1
CHAPTER / SUBPART            2
ALL-CAPS heading line        2
ARTICLE / SECTION / RULE / § 3
``N``                        3
``N.M``                      4
CLAUSE / STEP / PARAGRAPH    4
``N.M.P`` and deeper         5+
``(a)`` / ``(iv)``           6
===========================  =====

Skipped levels are fine: the stack nests a level-4 heading under a level-2
one when no level-3 heading intervenes.

Ordinal reconciliation
----------------------
Pattern depth alone cannot tell two *independent* numbering series apart: in a
contract whose Articles are roman and whose cross-references are ``§`` codes,
``§ 12.3`` is deeper than ``ARTICLE IV`` by pattern and so used to land under
it. So a heading's **own number** decides its parent wherever it can
(:func:`parse_ordinal`, :func:`_reconcile`):

- ``4.2`` attaches to whichever heading *is* ``4`` — which also reconciles
  across conventions, since ``IV`` parses to ``(4,)`` and is therefore a valid
  parent for ``4.1``. The document's two spellings of "four" are recognised as
  one number.
- ``§ 12.3`` refuses an ancestor whose ordinal is not a prefix of its own, so
  it climbs past ``ARTICLE IV`` to the unnumbered title above.
- ``(a)`` is *relative* — a position among siblings, no absolute path — so it
  is placed by nesting only.

``SectionHeading.level`` is the **reconciled** depth (what the tree and the
graph's ``contains`` edges use); ``pattern_level`` keeps the rule's natural
depth, which is what ``max_depth`` filters on.

:func:`reconcile_issues` is the other half: once ordinals are parsed, checking
that a series actually runs 1, 2, 3 is nearly free, and "is anything missing?"
is the question people ask of a contract or a procedure.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any

logger = logging.getLogger(__name__)

#: Separator between breadcrumb components in ``heading_path``. Punctuation
#: the FTS5 tokenizer discards, so a path contributes its words to the index
#: without contributing a junk term of its own.
PATH_SEPARATOR = " > "

#: Bounds. A pathological or misdetected document must not be able to add
#: unbounded nodes to the graph or an unbounded string to every chunk row.
MAX_HEADINGS_PER_DOCUMENT = 2_000
MAX_TITLE_CHARS = 200
MAX_PATH_CHARS = 500

#: Longest a line may be and still be considered a heading. Real headings are
#: short; this is the cheapest single filter against prose false positives.
MAX_HEADING_LINE_CHARS = 120
#: Most words a heading's title may carry. "1. Introduction" passes;
#: "1. Take the lid off and then pour the contents into the mixing bowl" does
#: not. Ten is chosen against real headings rather than guessed: "Limitation
#: of Liability and Exclusion of Consequential Damages" is eight words, and
#: legal/procedural headings longer than ten are rare while numbered list
#: items longer than ten are common.
MAX_TITLE_WORDS = 10

_KEYWORD_LEVELS: dict[str, int] = {
    "book": 1,
    "part": 1,
    "title": 1,
    "division": 1,
    "schedule": 1,
    "appendix": 1,
    "annex": 1,
    "exhibit": 1,
    "chapter": 2,
    "subpart": 2,
    "article": 3,
    "section": 3,
    "rule": 3,
    "regulation": 3,
    "clause": 4,
    "step": 4,
    "paragraph": 4,
    "subsection": 5,
}

#: Which numbering family a keyword belongs to. Aliases collapse (``PART`` and
#: ``TITLE`` are one series), which is what lets :func:`reconcile_issues` check
#: "do the Articles run 1, 2, 3?" without mixing them in with the Schedules.
#: It is a *label* — it does not gate parentage; see the note above
#: ``_compatible_parent`` for why that gate was removed.
_KEYWORD_SERIES: dict[str, str] = {
    "book": "division",
    "part": "division",
    "title": "division",
    "division": "division",
    "schedule": "annex",
    "appendix": "annex",
    "annex": "annex",
    "exhibit": "annex",
    "chapter": "chapter",
    "subpart": "chapter",
    "article": "article",
    "section": "section",
    "rule": "rule",
    "regulation": "rule",
    "clause": "clause",
    "step": "step",
    "paragraph": "paragraph",
    "subsection": "section",
}

_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
_LETTER_SERIES = "abcdefghijklmnopqrstuvwxyz"

_ORDINAL = r"[0-9]+(?:\.[0-9]+)*[A-Za-z]?|[IVXLCDM]+|[A-Z]"
_RE_DECIMAL_ORDINAL = re.compile(r"^([0-9]+(?:\.[0-9]+)*)([A-Za-z])?$")
_RE_ROMAN_ORDINAL = re.compile(r"^[IVXLCDM]+$")


@dataclass(frozen=True)
class Ordinal:
    """A heading's own number, parsed into something comparable.

    ``parts`` is the numeric path: ``4.2`` is ``(4, 2)``, ``IV`` is ``(4,)``,
    ``§ 12.3`` is ``(12, 3)``. That is what makes hierarchy *derivable* rather
    than guessed — ``4.2`` belongs under ``4`` because its path says so, and
    ``§ 12.3`` does not belong under ``ARTICLE IV`` because ``(4,)`` is not a
    prefix of ``(12, 3)``.

    ``relative`` marks a lettered ordinal (``(a)``, ``(iv)``): it has a
    position among its siblings but no absolute path, so it can only be placed
    by nesting, never by prefix.

    ``suffix`` carries an inserted-section letter (``§ 12A``). A suffixed
    ordinal is a *sibling* of its unsuffixed root, not a child of it — that is
    what inserting a section into an existing numbering means — so it parents
    where ``12`` would and never collides with it.
    """

    parts: tuple[int, ...]
    series: str
    raw: str
    relative: bool = False
    suffix: str = ""

    @property
    def absolute(self) -> bool:
        return bool(self.parts) and not self.relative

    @property
    def prefix(self) -> tuple[int, ...]:
        """Numeric path of the section this one sits under, if any.

        A suffixed ordinal shares its root's prefix rather than descending from
        it: ``§ 12A`` hangs where ``§ 12`` hangs, because inserting a section is
        not the same as nesting one.
        """
        return self.parts[:-1]

    def is_prefix_of(self, other: Ordinal) -> bool:
        if not (self.absolute and other.absolute) or self.suffix:
            return False
        return len(self.parts) < len(other.parts) and other.parts[: len(self.parts)] == self.parts

    def key(self) -> tuple:
        return (self.parts, self.suffix)


def _roman_to_int(text: str) -> int | None:
    total = 0
    previous = 0
    for char in reversed(text.upper()):
        value = _ROMAN_VALUES.get(char)
        if value is None:
            return None
        total += -value if value < previous else value
        previous = max(previous, value)
    return total or None


#: Single characters that are both a letter and a roman numeral. `C`, `D`, `L`
#: and `M` never *start* a sub-list, so a lone one is the letter; `i`, `v` and
#: `x` plausibly do, so a lone one is the numeral.
_ROMAN_STARTERS = frozenset("ivx")


def _lettered_position(inner: str) -> int | None:
    """Position of a parenthesised sub-item — ``(c)`` is 3, ``(iv)`` is 4.

    Genuinely ambiguous, because seven letters are also roman numerals: is
    ``(c)`` the third item or numeral 100? Resolved by two observations about
    how these lists are actually written:

    - Multiple roman characters (``(ii)``, ``(iv)``) are always a numeral.
    - A lone ``c``/``d``/``l``/``m`` is a letter, because no roman sub-list
      starts at 100. A lone ``i``/``v``/``x`` is a numeral, because those do
      start one.

    This is the character-only reading, used when there is nothing to compare
    against. :func:`_disambiguate_lettered` revisits it with the surrounding
    run, which is what actually resolves a letter list that reaches ``(i)``.
    """
    if not inner:
        return None
    if len(inner) > 1 and _RE_ROMAN_ORDINAL.match(inner.upper()):
        return _roman_to_int(inner)
    if len(inner) == 1:
        if inner in _ROMAN_STARTERS:
            return _roman_to_int(inner)
        if inner in _LETTER_SERIES:
            return _LETTER_SERIES.index(inner) + 1
    return None


def parse_ordinal(raw: str | None, kind: str, keyword: str | None = None) -> Ordinal | None:
    """Parse a heading's number into an :class:`Ordinal`, or ``None``.

    Handles the forms these document classes actually use: decimal paths
    (``4``, ``4.2.1``), roman numerals (``IV`` — near-universal for Articles
    and Parts), single letters, inserted-section suffixes (``12A``) and
    parenthesised sub-items (``(a)``, ``(iv)``).
    """
    if not raw:
        return None
    series = _KEYWORD_SERIES.get((keyword or "").lower(), kind)
    text = raw.strip()

    if kind == "lettered":
        position = _lettered_position(text.strip("()").lower())
        if position is None:
            return None
        # Relative: "(a)" is the first child of whatever encloses it, and
        # carries no absolute path of its own.
        return Ordinal(parts=(position,), series="lettered", raw=text, relative=True)

    match = _RE_DECIMAL_ORDINAL.match(text)
    if match:
        parts = tuple(int(piece) for piece in match.group(1).split("."))
        return Ordinal(parts=parts, series=series, raw=text, suffix=(match.group(2) or "").upper())

    if _RE_ROMAN_ORDINAL.match(text.upper()):
        value = _roman_to_int(text)
        if value is not None:
            return Ordinal(parts=(value,), series=series, raw=text)

    if len(text) == 1 and text.lower() in _LETTER_SERIES:
        return Ordinal(parts=(_LETTER_SERIES.index(text.lower()) + 1,), series=series, raw=text)
    return None


_RE_MARKDOWN = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_RE_KEYWORD = re.compile(
    rf"^\s*({'|'.join(_KEYWORD_LEVELS)})\s+({_ORDINAL})\s*[-–—:.)]?\s*(.*)$",
    re.IGNORECASE,
)
_RE_SECTION_SYMBOL = re.compile(r"^\s*§{1,2}\s*([0-9]+(?:\.[0-9]+)*[A-Za-z]?)\s*[-–—:.)]?\s*(.*)$")
_RE_NUMBERED = re.compile(r"^\s*([0-9]+(?:\.[0-9]+){0,5})\.?\s+(\S.*)$")
_RE_LETTERED = re.compile(r"^\s*\(([a-z]{1,2}|[ivxlcdm]{1,5})\)\s*(\S.*)$")
_RE_CAPS = re.compile(r"^\s*([A-Z][A-Z0-9][A-Z0-9 ,'&/()\[\].:;–—-]{2,78})\s*$")

#: Rule names, for ``TaxonomySettings.detect``. Ordered by how specific the
#: rule is: the first that matches a line wins.
RULE_NAMES = ("markdown", "keyword", "code", "numbered", "lettered", "caps")
DEFAULT_RULES: tuple[str, ...] = RULE_NAMES


@dataclass(frozen=True)
class SectionHeading:
    """One detected structural heading.

    ``number`` is the document's own code for the section (``"12.3"``,
    ``"IV"``, ``"(a)"``) and is kept separate from ``title`` so a caller can
    look a section up by its citation rather than by its wording — the
    question "what does § 12.3 say" is answerable from ``number`` alone.
    """

    line: int
    level: int
    number: str | None
    title: str
    kind: str
    path: str
    #: Depth the *pattern* implies, before ordinal reconciliation. ``level`` is
    #: the reconciled tree depth (``parent.level + 1``) and is what
    #: :func:`taxonomy_tree` nests by; ``pattern_level`` is what ``max_depth``
    #: filters on, so "ignore lettered sub-items" stays a statement about the
    #: pattern rather than about where the document happened to put it.
    pattern_level: int = 0
    #: Parsed number, when the heading has one. Present for keyword, code,
    #: numbered and lettered headings; ``None`` for Markdown and ALL-CAPS.
    ordinal: Ordinal | None = None

    @property
    def label(self) -> str:
        if self.number and self.title:
            return f"{self.number} {self.title}"
        return self.number or self.title

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "line": self.line,
            "level": self.level,
            "number": self.number,
            "title": self.title,
            "kind": self.kind,
            "path": self.path,
        }
        if self.ordinal is not None:
            payload["ordinal"] = {
                "parts": list(self.ordinal.parts),
                "series": self.ordinal.series,
                "raw": self.ordinal.raw,
                "relative": self.ordinal.relative,
                "suffix": self.ordinal.suffix,
            }
        return payload


def _plausible_heading(title: str, line_text: str) -> bool:
    """Whether a numbered/lettered/caps line reads like a heading, not prose.

    Cheap structural filters only — deliberately not a classifier. Anything
    smarter would be a judgement call that varies by corpus, and this feature
    is enabled per source precisely so the operator makes that call.
    """
    if len(line_text) > MAX_HEADING_LINE_CHARS:
        return False
    if len(title.split()) > MAX_TITLE_WORDS:
        return False
    # A line ending in a clause separator is mid-sentence, not a heading.
    if title.rstrip().endswith((",", ";", ":", "and", "or")):
        return False
    # A heading rarely contains sentence-ending punctuation followed by more
    # words ("1. Do this. Then do that." is an instruction, not a heading).
    if re.search(r"[.!?]\s+\S", title):
        return False
    return True


def _plausible_citation(ordinal: str) -> bool:
    """Whether a keyword's ordinal is really an ordinal — ``PART A``, not ``Part o``.

    Found on a real producer-generated PDF, not by reading the rules: PDF text
    is laid out in lines, so a paragraph arrives pre-broken and *any* line can
    begin with the word "Section" or "Part". One line began ``Section o f this
    Agreement unless stated otherwise.`` — the extractor had rendered "of" as
    "o f", which is ordinary PDF kerning — and since the keyword pattern is
    case-insensitive, its single-letter alternative read ``o`` as ordinal 15.
    The invented heading then became a *parent*, so it re-parented genuine
    sections rather than merely adding a spurious one.

    A letter-only citation is written in capitals in every real document
    (``PART A``, ``Annex B``, ``ARTICLE IV``), and prose words are not, so
    requiring capitals separates them exactly and costs nothing real. A numeric
    ordinal is unambiguous and keeps no case rule.
    """
    text = ordinal.strip()
    if not text:
        return False
    if any(char.isdigit() for char in text):
        return True
    return text.isupper()


def _classify(raw_line: str) -> tuple[int, str | None, str, str] | None:
    """``(level, number, title, kind)`` for a heading line, else ``None``."""
    line = raw_line.rstrip()
    if not line.strip():
        return None

    match = _RE_MARKDOWN.match(line)
    if match:
        return len(match.group(1)), None, match.group(2).strip()[:MAX_TITLE_CHARS], "markdown"

    match = _RE_KEYWORD.match(line)
    if match and _plausible_citation(match.group(2)) and _plausible_heading(match.group(3), line):
        keyword, ordinal, title = match.group(1), match.group(2), match.group(3).strip()
        level = _KEYWORD_LEVELS[keyword.lower()]
        number = f"{keyword.title()} {ordinal}"
        return level, number, title[:MAX_TITLE_CHARS], "keyword"

    match = _RE_SECTION_SYMBOL.match(line)
    if match:
        ordinal, title = match.group(1), match.group(2).strip()
        # A § citation is a heading regardless of what follows it on the line;
        # legal drafting routinely runs the text straight on from the number.
        return 3 + ordinal.count("."), f"§ {ordinal}", title[:MAX_TITLE_CHARS], "code"

    match = _RE_NUMBERED.match(line)
    if match:
        ordinal, title = match.group(1), match.group(2).strip()
        if _plausible_heading(title, line):
            return 3 + ordinal.count("."), ordinal, title[:MAX_TITLE_CHARS], "numbered"

    match = _RE_LETTERED.match(line)
    if match:
        ordinal, title = match.group(1), match.group(2).strip()
        if _plausible_heading(title, line):
            return 6, f"({ordinal})", title[:MAX_TITLE_CHARS], "lettered"

    match = _RE_CAPS.match(line)
    if match:
        title = match.group(1).strip()
        # Require two words so a stray acronym or a shouted single word does
        # not become a section.
        if len(title.split()) >= 2 and _plausible_heading(title, line):
            return 2, None, title[:MAX_TITLE_CHARS], "caps"

    return None


def _continuation_title(lines: list[str], heading_line: int) -> str:
    """Title carried on the line *after* a bare ``ARTICLE IV`` / ``§ 5``.

    Legal drafting routinely puts the citation and its caption on separate
    lines:

    .. code-block:: text

        ARTICLE IV
        Term and Termination

    Without this, that Article's path reads "Article IV" and its actual
    subject — the words someone would search for — is dropped on the floor.
    Only a short, non-heading, non-sentence next line is taken, so body text
    immediately following a bare citation is not mistaken for a caption.
    """
    for offset in range(heading_line, min(heading_line + 2, len(lines))):
        candidate = lines[offset].strip()
        if not candidate:
            continue
        if len(candidate) > MAX_HEADING_LINE_CHARS or len(candidate.split()) > MAX_TITLE_WORDS:
            return ""
        # A line that is itself a heading belongs to the outline, not to this
        # heading's caption.
        if _classify(lines[offset]) is not None:
            return ""
        if candidate.endswith((".", ";", ",")):
            return ""
        return candidate[:MAX_TITLE_CHARS]
    return ""


def detect_headings(
    text: str,
    *,
    rules: tuple[str, ...] | list[str] = DEFAULT_RULES,
    max_depth: int = 6,
    max_headings: int = MAX_HEADINGS_PER_DOCUMENT,
) -> list[SectionHeading]:
    """Detect the structural outline of ``text``, deepest-first nested.

    Returns headings in document order, each carrying the full breadcrumb
    ``path`` of its ancestors. Deterministic: same text, same rules, same
    result.
    """
    if not text:
        return []
    enabled = {name for name in rules if name in RULE_NAMES}
    if not enabled:
        return []

    candidates = _disambiguate_lettered(_scan(text, enabled, max_depth, max_headings))
    return _reconcile(candidates)


@dataclass(frozen=True)
class _Candidate:
    """A matched heading line, before its place in the tree is decided."""

    line: int
    pattern_level: int
    number: str | None
    title: str
    kind: str
    ordinal: Ordinal | None

    @property
    def label(self) -> str:
        if self.number and self.title:
            return f"{self.number} {self.title}"
        return self.number or self.title


def _scan(
    text: str,
    enabled: set[str],
    max_depth: int,
    max_headings: int,
) -> list[_Candidate]:
    """Match heading lines. No hierarchy decisions here — that is `_reconcile`."""
    lines = text.splitlines()
    candidates: list[_Candidate] = []
    for index, raw_line in enumerate(lines, start=1):
        classified = _classify(raw_line)
        if classified is None:
            continue
        pattern_level, number, title, kind = classified
        if kind not in enabled:
            continue
        if not title and kind in ("keyword", "code"):
            title = _continuation_title(lines, index)
        # `max_depth` filters on the *pattern's* depth, so "ignore lettered
        # sub-items" stays a statement about the pattern rather than about
        # wherever reconciliation ends up putting it.
        if pattern_level > max_depth:
            continue
        if not (number or title):
            continue
        keyword = number.split()[0] if (kind == "keyword" and number) else None
        raw_ordinal = _raw_ordinal(number, kind)
        candidates.append(
            _Candidate(
                line=index,
                pattern_level=pattern_level,
                number=number,
                title=title,
                kind=kind,
                ordinal=parse_ordinal(raw_ordinal, kind, keyword),
            )
        )
        if len(candidates) >= max_headings:
            logger.warning("taxonomy detection hit the %d-heading cap; truncating", max_headings)
            break
    return candidates


def _ambiguous_readings(inner: str) -> tuple[int | None, int | None]:
    """Both readings of a one-character sub-item label: ``(letter, roman)``."""
    letter = _LETTER_SERIES.index(inner) + 1 if inner in _LETTER_SERIES else None
    roman = _roman_to_int(inner) if inner.upper() in _ROMAN_VALUES else None
    return letter, roman


def _disambiguate_lettered(candidates: list[_Candidate]) -> list[_Candidate]:
    """Re-read one-character sub-item labels using the run they sit in.

    Seven letters are also roman numerals, and the character alone cannot say
    which is meant. :func:`_lettered_position` guesses by convention — a lone
    ``i``/``v``/``x`` is a numeral — which is right for a sub-list that opens
    with ``(i)`` and wrong for a letter list that has simply counted up to its
    ninth item. ``(h)`` then ``(i)`` is 8 then 9, not 8 then 1.

    The run says which. Both readings are computed, and whichever *continues*
    the previous item in the run wins:

    - ``(h)`` ``(i)``  -> 8, 9: the letter reading continues, so ``i`` is a letter.
    - ``(iv)`` ``(v)`` -> 4, 5: the roman reading continues, so ``v`` is a numeral.
    - ``(u)`` ``(v)``  -> 21, 22: letter again — the case the old convention
      always got wrong, since ``v`` is not the 5th item of anything here.
    - ``(b)`` ``(i)``  -> neither 9 nor 1 follows 2, so this is a nested list
      opening at roman 1, and the convention stands.

    A run is a stretch of consecutive lettered candidates; any other heading
    between them starts a new one. Deterministic and local — one backward look,
    no lookahead, so the same bytes always resolve the same way.
    """
    resolved: list[_Candidate] = []
    previous: int | None = None  # value of the previous item in the current run
    for candidate in candidates:
        if candidate.kind != "lettered" or candidate.ordinal is None:
            resolved.append(candidate)
            previous = None  # any other heading breaks the run
            continue
        inner = (candidate.number or "").strip("()").lower()
        letter, roman = _ambiguous_readings(inner) if len(inner) == 1 else (None, None)
        chosen = candidate.ordinal.parts[0] if candidate.ordinal.parts else None
        if previous is not None and letter is not None and roman is not None:
            if letter == previous + 1:
                chosen = letter
            elif roman == previous + 1:
                chosen = roman
        if chosen is not None and chosen != (candidate.ordinal.parts or (None,))[0]:
            candidate = replace(candidate, ordinal=replace(candidate.ordinal, parts=(chosen,)))
        resolved.append(candidate)
        previous = chosen
    return resolved


def _raw_ordinal(number: str | None, kind: str) -> str | None:
    """The bare ordinal inside a rendered number (``Article IV`` -> ``IV``)."""
    if not number:
        return None
    if kind == "keyword":
        pieces = number.split(None, 1)
        return pieces[1] if len(pieces) > 1 else None
    if kind == "code":
        return number.lstrip("§ ").strip() or None
    return number


def _reconcile(candidates: list[_Candidate]) -> list[SectionHeading]:
    """Decide each heading's parent, preferring the **ordinal** over the level.

    Level-only nesting cannot tell two independent numbering series apart. In a
    contract whose Articles are roman and whose cross-references are ``§``
    codes, ``§ 12.3`` is deeper than ``ARTICLE IV`` by level and so lands
    *under* it — wrong, and it was a documented limitation of the first cut.

    An ordinal says where a section belongs, so this resolves parentage in
    three passes of preference:

    1. **Prefix match.** ``4.2`` goes under whichever heading *is* ``4``. This
       also reconciles across conventions for free: ``4.1`` lands under
       ``ARTICLE IV``, because ``IV`` parses to ``(4,)`` and ``(4,)`` is a
       prefix of ``(4, 1)``. The document's two ways of writing "four" are
       recognised as the same number.
    2. **Compatibility walk.** With no prefix match, climb the open ancestors
       and refuse any whose absolute ordinal is *not* a prefix of ours — that
       is the conflicting-series case. ``§ 12.3`` therefore skips past
       ``4.1`` and ``ARTICLE IV`` and attaches to the unnumbered document
       title above them.
    3. **Level nesting.** Headings with no ordinal at all (Markdown,
       ALL-CAPS) and relative ones (``(a)``) nest exactly as before.

    ``level`` on the result is the reconciled depth, so `taxonomy_tree` and the
    graph's `contains` edges agree with the ordinals.
    """
    resolved: list[SectionHeading] = []
    parents: list[int | None] = []
    open_chain: list[int] = []  # indices of currently-open ancestors
    # Latest heading carrying each absolute, suffix-free ordinal: what the
    # prefix match looks up. It used to scan every resolved heading backwards,
    # which is O(headings) per heading and quadratic in a document whose
    # numbers rarely have a parent present (8 s at 10,720 tariff headings).
    latest: dict[tuple[int, ...], int] = {}

    for candidate in candidates:
        parent = _prefix_parent(candidate, latest)
        if parent is None:
            parent = _compatible_parent(candidate, resolved, open_chain)

        level = (resolved[parent].level + 1) if parent is not None else 1
        crumbs = [resolved[i].label for i in _ancestry(parents, parent)] + [candidate.label]
        path = PATH_SEPARATOR.join(crumb for crumb in crumbs if crumb)[:MAX_PATH_CHARS]

        resolved.append(
            SectionHeading(
                line=candidate.line,
                level=level,
                number=candidate.number,
                title=candidate.title,
                kind=candidate.kind,
                path=path,
                pattern_level=candidate.pattern_level,
                ordinal=candidate.ordinal,
            )
        )
        index = len(resolved) - 1
        parents.append(parent)
        ordinal = candidate.ordinal
        if ordinal is not None and ordinal.absolute and not ordinal.suffix:
            latest[ordinal.parts] = index
        # Reopen the chain down to this heading's parent, then push it.
        open_chain = _ancestry(parents, parent) + [index]
    return resolved


def _ancestry(parents: list[int | None], index: int | None) -> list[int]:
    """Indices from the root down to ``index`` inclusive."""
    chain: list[int] = []
    cursor = index
    guard = 0
    while cursor is not None and guard <= len(parents):
        chain.append(cursor)
        cursor = parents[cursor] if cursor < len(parents) else None
        guard += 1
    return list(reversed(chain))


def _prefix_parent(candidate: _Candidate, latest: dict[tuple[int, ...], int]) -> int | None:
    """Index of the latest heading whose ordinal is exactly this one's prefix."""
    ordinal = candidate.ordinal
    if ordinal is None or not ordinal.absolute:
        return None
    prefix = ordinal.prefix
    if not prefix:
        return None
    return latest.get(tuple(prefix))


# A `_series_compatible` gate used to sit on the match above, refusing a prefix
# match when the two keywords named different series (`CHAPTER 4` vs
# `SECTION 4.1`). Mutation testing showed it was **inert**: forcing it to always
# return True changed no test, because the compatibility walk below does not
# check series and re-made the identical attachment. It was also arguably wrong
# — `SECTION 4.1` genuinely *is* within `CHAPTER 4`. Removed rather than kept
# with a docstring claiming a protection it did not provide.
#
# What actually keeps `CHAPTER 4` and `ARTICLE 4` from parenting each other is
# simpler: both have a single-component ordinal, so neither has a prefix for the
# other to match.


def _compatible_parent(
    candidate: _Candidate,
    resolved: list[SectionHeading],
    open_chain: list[int],
) -> int | None:
    """Deepest open ancestor that does not conflict with this ordinal."""
    ordinal = candidate.ordinal
    for index in reversed(open_chain):
        ancestor = resolved[index]
        # Compare **pattern** depth to pattern depth. Reconciled `level` is a
        # compressed tree depth (1, 2, 3…) while `pattern_level` is the rule's
        # natural depth (6 for a lettered item), so mixing the two scales makes
        # the sibling test meaningless: a lettered `(b)` compared against a
        # reconciled level of 2 looks *deeper* than its own sibling `(a)` and
        # nests inside it. Caught by the duplicate-`(b)` case.
        if ancestor.pattern_level >= candidate.pattern_level:
            # Same or shallower pattern depth: this is a sibling boundary.
            continue
        if ordinal is not None and ordinal.absolute:
            other = ancestor.ordinal
            if (
                other is not None
                and other.absolute
                and not other.is_prefix_of(ordinal)
                and not _contains_a_fresh_series(other, ordinal)
            ):
                # A numbered ancestor from a different series or branch cannot
                # contain us — keep climbing.
                continue
        return index
    return None


#: Keyword series that partition a document rather than enumerate within it.
#: `PART`/`BOOK`/`TITLE`/`DIVISION` and the `SCHEDULE`/`APPENDIX`/`ANNEX`/
#: `EXHIBIT` family are the outermost containers, which is why they sit at
#: pattern level 1.
_CONTAINER_SERIES = frozenset({"division", "annex"})


def _contains_a_fresh_series(ancestor: Ordinal, candidate: Ordinal) -> bool:
    """May a container hold a numbering that has nothing to do with its own?

    ``PART I`` followed by ``1. Scope`` is the case. Neither number is a prefix
    of the other — they are both "one" — so the conflicting-series rule above
    refuses the attachment and the section flattens out of its Part, losing the
    Part from every ``heading_path`` beneath it.

    That rule is right for two *enumerations* competing for the same spine
    (``CHAPTER 4`` does not contain ``ARTICLE 4``) and wrong for a *container*:
    a Part or an Annex exists precisely to hold a section numbering it does not
    participate in, and that numbering restarts inside each one. So a container
    may hold a different series — but not its own, since ``PART II`` is beside
    ``PART I``, never inside it.
    """
    return ancestor.series in _CONTAINER_SERIES and candidate.series != ancestor.series


def _parent_path(heading: SectionHeading) -> str:
    """Breadcrumb of the heading above this one, or ``""`` at the top."""
    if PATH_SEPARATOR not in heading.path:
        return ""
    return heading.path.rsplit(PATH_SEPARATOR, 1)[0]


def reconcile_issues(headings: list[SectionHeading]) -> list[dict[str, Any]]:
    """Sequence problems in the numbering: gaps, duplicates, out-of-order.

    The second half of reconciliation. Once ordinals are parsed, checking that
    a numbering series actually runs 1, 2, 3 is nearly free — and for the
    document classes this feature targets it is the question people ask of a
    document: *is anything missing?* A contract that jumps from § 12.3 to
    § 12.5, or repeats ``(b)``, is either an extraction failure or a drafting
    defect, and both are worth surfacing rather than silently indexing.

    Only gaps *between observed members of one series* are reported. A series
    that starts at 3 is not flagged: an excerpt legitimately begins
    mid-document, and guessing otherwise would cry wolf on every extract.
    Suffixed inserts (``§ 12A``) never advance the sequence, since inserting one
    is precisely how a numbering absorbs a new section without renumbering.

    What counts as "one series" is the subtle part, and it is decided by the
    **numeric prefix** rather than by the resolved parent:

    - A numbering with a prefix (``§ 12.1``, ``§ 12.2``, ``§ 12.4``) is one
      series wherever its members land in the tree. Grouping those by resolved
      parent instead would hide the 12.2 -> 12.4 gap the moment an unnumbered
      heading interrupted them and re-parented the tail, which is exactly when
      a reader most wants to be told.
    - A **top-level** numbering has no prefix to identify it, so it is grouped
      by parent — which is what makes a per-part restart legal. ``PART I`` with
      sections 1, 2 followed by ``PART II`` with sections 1, 2 is two series,
      not one series that jumped backwards, and only the parent distinguishes
      them.

    Deterministic, and read-only over the reconciled headings.
    """
    groups: dict[tuple[str, str, str], list[SectionHeading]] = {}
    for heading in headings:
        ordinal = heading.ordinal
        if ordinal is None or not ordinal.parts:
            continue
        parent = _parent_path(heading)
        if ordinal.relative:
            key = ("rel", ordinal.series, parent)
        elif ordinal.prefix:
            key = ("path", ordinal.series, ".".join(str(part) for part in ordinal.prefix))
        else:
            key = ("root", ordinal.series, parent)
        groups.setdefault(key, []).append(heading)

    issues: list[dict[str, Any]] = []
    for _key, members in sorted(groups.items()):
        previous: SectionHeading | None = None
        for heading in members:
            assert heading.ordinal is not None
            if heading.ordinal.suffix:
                continue
            if previous is None:
                previous = heading
                continue
            assert previous.ordinal is not None
            before = previous.ordinal.parts[-1]
            current = heading.ordinal.parts[-1]
            # `parent` is the breadcrumb of the heading the defect was observed
            # at, not the group's — for a prefix-identified series those can
            # differ, and where it showed up is the useful half.
            common = {
                "series": heading.ordinal.series,
                "parent": _parent_path(heading),
                "after": previous.number,
                "at": heading.number,
                "line": heading.line,
            }
            if current == before:
                issues.append({"kind": "duplicate", **common})
            elif current < before:
                issues.append({"kind": "out_of_order", **common})
            elif current > before + 1:
                issues.append(
                    {
                        "kind": "gap",
                        "missing": list(range(before + 1, current)),
                        **common,
                    }
                )
            if current >= before:
                previous = heading
    return issues


def heading_path_for_line(headings: list[SectionHeading], line: int) -> str | None:
    """Breadcrumb of the innermost heading at or above ``line``.

    ``headings`` must be in document order (as :func:`detect_headings`
    returns them). Used to label a chunk with the section it falls inside
    **without moving the chunk's boundaries**, so enabling the feature adds a
    column value rather than re-cutting the corpus.
    """
    found: str | None = None
    for heading in headings:
        if heading.line > line:
            break
        found = heading.path
    return found


def taxonomy_tree(headings: list[SectionHeading]) -> list[dict[str, Any]]:
    """Nest a flat heading list into a tree of ``{...,"children":[...]}``.

    The shape the taxonomy is actually *read* in — one node per section with
    its children under it — as opposed to the flat document-order list the
    detector produces and the chunk labelling needs.
    """
    roots: list[dict[str, Any]] = []
    stack: list[tuple[int, dict[str, Any]]] = []
    for heading in headings:
        node = heading.as_dict()
        node["children"] = []
        while stack and stack[-1][0] >= heading.level:
            stack.pop()
        if stack:
            stack[-1][1]["children"].append(node)
        else:
            roots.append(node)
        stack.append((heading.level, node))
    return roots


def source_taxonomy_enabled(source: Any) -> bool:
    """Whether a source has taxonomy extraction switched on.

    Tolerant of a source object without the block (a config written before
    this feature, or a duck-typed test double), which reads as disabled.
    """
    settings = getattr(source, "taxonomy", None)
    return bool(getattr(settings, "enabled", False))


def rules_for_source(source: Any) -> tuple[str, ...]:
    settings = getattr(source, "taxonomy", None)
    configured = list(getattr(settings, "detect", None) or ())
    valid = tuple(name for name in configured if name in RULE_NAMES)
    if configured and not valid:
        logger.warning(
            "taxonomy.detect for source %r names no known rule (%s); using defaults",
            getattr(source, "name", "?"),
            ", ".join(RULE_NAMES),
        )
    return valid or DEFAULT_RULES


def max_depth_for_source(source: Any) -> int:
    settings = getattr(source, "taxonomy", None)
    depth = getattr(settings, "max_depth", 6)
    try:
        return max(1, min(6, int(depth)))
    except (TypeError, ValueError):
        return 6


def headings_for_source(source: Any, text: str) -> list[SectionHeading]:
    """Detect headings for ``source``, honouring its config; ``[]`` when off."""
    if not source_taxonomy_enabled(source):
        return []
    return detect_headings(
        text,
        rules=rules_for_source(source),
        max_depth=max_depth_for_source(source),
    )
