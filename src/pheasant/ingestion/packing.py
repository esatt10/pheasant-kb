"""Turn a document's own units into chunks of a planned size.

`chunk_text` cuts fixed windows: every chunk is ``max_chars`` long whatever
the document looks like, and a window routinely starts mid-section and ends
mid-sentence. `_section_aligned_chunks` fixed the boundaries for structured
sources and over-corrected: one chunk per section, never merged, so a document
of short clauses became thousands of tiny chunks. On this repository's own
Markdown it produced 627 chunks against 203 fixed windows, 14% of them under
300 characters, and every one of those is an embedding request, an FTS row and
a graph node.

This packs instead. A *unit* is the smallest span the document itself
declares -- a section, a paragraph, a top-level code block, a chat message, a
spreadsheet row -- and units are merged in order until the next one would pass
``target_chars``, then flushed. So small units share a chunk, a chunk never
spans two top-level sections once it is big enough to stand alone, and only a
unit larger than ``max_chars`` is ever split, at a paragraph, line or sentence
break. Overlap is applied only inside such a split: two neighbouring sections
share no text, so overlapping across them would only duplicate embedding work.

Every function here is pure and deterministic, and every non-blank line of the
input lands in at least one chunk (asserted by `tests/test_chunk_packing.py`).
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pheasant.ingestion.chunking import TextChunk
from pheasant.ingestion.taxonomy import PATH_SEPARATOR

if TYPE_CHECKING:
    from pheasant.ingestion.chunk_plan import ChunkPlan
    from pheasant.ingestion.taxonomy import SectionHeading

#: A Slack line as `connectors/slack.py` writes it: ``**user** (ts): text``.
MESSAGE_LINE = re.compile(r"^\*\*[^*\n]+\*\* \(\d")


@dataclass(frozen=True)
class _Unit:
    start: int  # first line, 1-based
    stop: int  # one past the last line
    path: str | None


def pack(
    text: str, plan: ChunkPlan, headings: list[SectionHeading] | None = None
) -> list[TextChunk]:
    """Chunk ``text`` by ``plan``. ``headings`` are used when it splits on them."""

    if not text.strip():
        return []
    lines = text.splitlines()
    if plan.boundary == "rows":
        return _pack_rows(lines, plan)
    if plan.boundary == "headings" and headings:
        units = _heading_units(lines, headings)
    elif plan.boundary == "code":
        units = _code_units(lines)
    elif plan.boundary == "messages":
        units = _message_units(lines)
    elif plan.boundary == "whole":
        units = [_Unit(1, len(lines) + 1, None)]
    else:
        units = _paragraph_units(lines)
    return _number(_pack_units(lines, units, plan))


# -- units ------------------------------------------------------------------


def _heading_units(lines: list[str], headings: list[SectionHeading]) -> list[_Unit]:
    units: list[_Unit] = []
    first = headings[0].line
    if first > 1:
        units.append(_Unit(1, first, None))
    for position, heading in enumerate(headings):
        stop = headings[position + 1].line if position + 1 < len(headings) else len(lines) + 1
        if stop > heading.line:
            units.append(_Unit(heading.line, stop, heading.path))
    return units


def _paragraph_units(lines: list[str]) -> list[_Unit]:
    """Maximal runs of non-blank lines."""

    units: list[_Unit] = []
    start: int | None = None
    for number, line in enumerate(lines, start=1):
        if line.strip():
            if start is None:
                start = number
        elif start is not None:
            units.append(_Unit(start, number, None))
            start = None
    if start is not None:
        units.append(_Unit(start, len(lines) + 1, None))
    return units


def _code_units(lines: list[str]) -> list[_Unit]:
    """Top-level blocks: a unit starts at an unindented line after a blank one.

    Language-agnostic on purpose. A definition, its decorators and its leading
    comment form one block because the blank line comes before all of them,
    and an indented blank line inside a function body never starts a unit.
    """

    starts = [1]
    previous_blank = False
    for number, line in enumerate(lines, start=1):
        blank = not line.strip()
        if previous_blank and not blank and not line[:1].isspace() and number > 1:
            starts.append(number)
        previous_blank = blank
    starts = sorted(set(starts))
    bounds = starts + [len(lines) + 1]
    return [_Unit(a, b, None) for a, b in zip(bounds, bounds[1:], strict=False) if b > a]


def _message_units(lines: list[str]) -> list[_Unit]:
    starts = [n for n, line in enumerate(lines, start=1) if MESSAGE_LINE.match(line)]
    if not starts:
        return _paragraph_units(lines)
    if starts[0] > 1:
        starts.insert(0, 1)
    bounds = starts + [len(lines) + 1]
    return [_Unit(a, b, None) for a, b in zip(bounds, bounds[1:], strict=False)]


# -- packing ----------------------------------------------------------------


def _segment(lines: list[str], start: int, stop: int) -> str:
    return "\n".join(lines[start - 1 : stop - 1])


def _top(path: str | None) -> str | None:
    return path.split(PATH_SEPARATOR, 1)[0] if path else None


def _label(paths: list[str]) -> str | None:
    """Every merged section, under the heading they share.

    A chunk holding sections 4.1 and 4.2 is labelled
    ``Article 4 > 4.1 Scope; 4.2 Term``. Labelling it ``4.1`` would be true of
    half of it, and labelling it ``Article 4`` alone would hide both sections
    from the ``section`` criterion -- a substring match on this label -- and
    from the label column's double BM25 weight, which is how "what does 4.2
    say" finds 4.2. Measured on this repository's docs, the parent-only label
    cost a quarter of the section lookups that one-chunk-per-section answered.
    """

    distinct = list(dict.fromkeys(paths))
    if not distinct:
        return None
    if len(distinct) == 1:
        return distinct[0]
    split = [path.split(PATH_SEPARATOR) for path in distinct]
    common: list[str] = []
    for parts in zip(*split, strict=False):
        if all(part == parts[0] for part in parts):
            common.append(parts[0])
        else:
            break
    own = list(dict.fromkeys(parts[-1] for parts in split if len(parts) > len(common)))
    tail = "; ".join(own)
    if not common:
        return tail
    return PATH_SEPARATOR.join(common) + (PATH_SEPARATOR + tail if tail else "")


def _pack_units(lines: list[str], units: list[_Unit], plan: ChunkPlan) -> list[TextChunk]:
    out: list[TextChunk] = []
    group: list[_Unit] = []
    size = 0

    def flush() -> None:
        nonlocal group, size
        if group:
            out.extend(_emit(lines, group, plan))
        group, size = [], 0

    for unit in units:
        length = len(_segment(lines, unit.start, unit.stop).strip())
        if not length:
            continue
        if group:
            grown = size + 1 + length
            if (
                grown > plan.max_chars
                or (grown > plan.target_chars and size >= plan.min_chars)
                or (size >= plan.min_chars and _top(unit.path) != _top(group[0].path))
                # A section is a unit somebody asks for by name, so a chunk
                # that can stand alone ends where the next section begins:
                # only sections under `min_chars` are folded into a neighbour.
                or (size >= plan.min_chars and unit.path is not None)
            ):
                flush()
        group.append(unit)
        size = size + (1 if size else 0) + length
    flush()
    return out


def _emit(lines: list[str], group: list[_Unit], plan: ChunkPlan) -> list[TextChunk]:
    start, stop = group[0].start, group[-1].stop
    label = _label([unit.path for unit in group if unit.path])
    body = _segment(lines, start, stop)
    if len(body.strip()) <= plan.max_chars:
        first, last = _content_lines(lines, start, stop)
        return [TextChunk(0, body.strip(), first, last, label)]
    return [
        TextChunk(0, piece, start + a - 1, start + b - 1, label)
        for piece, a, b in split_text(body, plan.target_chars, plan.overlap_chars)
    ]


def _content_lines(lines: list[str], start: int, stop: int) -> tuple[int, int]:
    first, last = start, stop - 1
    while first < last and not lines[first - 1].strip():
        first += 1
    while last > first and not lines[last - 1].strip():
        last -= 1
    return first, last


def split_text(text: str, target: int, overlap: int) -> list[tuple[str, int, int]]:
    """Cut ``text`` into ``(piece, first_line, last_line)`` of at most ``target``.

    Prefers, in order, a paragraph break, a line break, then a sentence end in
    the second half of the window; only a run with none of them is cut hard.
    Consecutive pieces share up to ``overlap`` characters, restarted at a line
    or word boundary so no piece begins mid-word. Lines are 1-based and relative
    to ``text``.
    """

    offsets: list[int] = []
    position = 0
    for line in text.splitlines():
        offsets.append(position)
        position += len(line) + 1
    pieces: list[tuple[str, int, int]] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(length, start + target)
        if end < length:
            floor = start + target // 2
            for marker in ("\n\n", "\n", ". "):
                cut = text.rfind(marker, floor, end)
                if cut > start:
                    end = cut + (1 if marker == ". " else 0)
                    break
        piece = text[start:end]
        if piece.strip():
            lead = len(piece) - len(piece.lstrip())
            tail = len(piece.rstrip())
            first = bisect_right(offsets, start + lead) or 1
            last = bisect_right(offsets, start + max(lead, tail - 1)) or first
            pieces.append((piece.strip(), first, last))
        if end >= length:
            break
        restart = max(start + 1, end - overlap) if overlap else end
        if overlap and restart < end:
            newline = text.find("\n", restart, end)
            space = text.find(" ", restart, end)
            for boundary in (newline, space):
                if boundary != -1:
                    restart = boundary + 1
                    break
        start = max(start + 1, min(restart, end))
    return pieces


def _pack_rows(lines: list[str], plan: ChunkPlan) -> list[TextChunk]:
    """Spreadsheet text: a sheet's header row is repeated in each of its chunks.

    `ingestion/office.py` writes each sheet as its name, then one
    tab-separated line per row, sheets separated by a blank line. A row read
    without its header is a list of bare values, so every chunk after a
    sheet's first carries the sheet name and the header again; the chunk's
    line span is still the rows it adds.
    """

    out: list[TextChunk] = []
    for block in _paragraph_units(lines):
        rows = list(range(block.start, block.stop))
        name = None
        if "\t" not in lines[rows[0] - 1] and len(rows) > 1:
            name = lines[rows[0] - 1].strip()
            rows = rows[1:]
        prefix = "\n".join(([name] if name else []) + [lines[rows[0] - 1]])
        body = rows[1:]
        if not body:
            out.append(TextChunk(0, prefix.strip(), block.start, block.stop - 1, name))
            continue
        groups: list[list[int]] = [[]]
        size = len(prefix)
        for row in body:
            length = len(lines[row - 1]) + 1
            if groups[-1] and size + length > plan.target_chars:
                groups.append([])
                size = len(prefix)
            groups[-1].append(row)
            size += length
        for position, group in enumerate(groups):
            text = (prefix + "\n" + "\n".join(lines[row - 1] for row in group)).strip()
            first = block.start if position == 0 else group[0]
            if len(text) <= plan.max_chars:
                out.append(TextChunk(0, text, first, group[-1], name))
                continue
            # One row longer than a whole chunk: cut it like prose rather
            # than emit a chunk past the ceiling every other profile keeps.
            for piece, _first, _last in split_text(text, plan.target_chars, 0):
                out.append(TextChunk(0, piece, first, group[-1], name))
    return _number(out)


def _number(chunks: list[TextChunk]) -> list[TextChunk]:
    return [
        TextChunk(index, chunk.text, chunk.start_line, chunk.end_line, chunk.heading_path)
        for index, chunk in enumerate(chunks)
    ]
