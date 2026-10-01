"""``chunk_text`` must stay linear in the size of the text it cuts.

Every chunk records the line it starts and ends on. That lookup was a reverse
linear scan of every line offset, per chunk, so chunking cost chunks x lines —
invisible on a source file, and 360 s of a 400 s sync for one 8,000-page PDF
(a utility tariff), whose last taxonomy section held most of the document.

The bound counts what the code touches rather than timing it: the scan
executed a Python line per offset inspected, so the lines executed during one
``chunk_text`` are the cost, and per chunk they must not grow with the line
count. The equivalence half pins the line numbers to the old scan's
answers, because they reach provenance and the change must not move one.
"""

from __future__ import annotations

import random
import sys

from pheasant.ingestion.chunking import chunk_text


def _reference_lines(text: str, start: int, end: int) -> tuple[int, int]:
    """The original lookup, kept as the specification of the answer."""
    lines = text.splitlines()
    offsets: list[tuple[int, int]] = []
    pos = 0
    for idx, line in enumerate(lines, start=1):
        offsets.append((pos, idx))
        pos += len(line) + 1
    start_line = next((line for offset, line in reversed(offsets) if offset <= start), 1)
    end_line = next((line for offset, line in reversed(offsets) if offset <= end), len(lines) or 1)
    return start_line, end_line


def _reference_chunks(text: str, max_chars: int, overlap_chars: int) -> list[tuple]:
    out: list[tuple] = []
    start = 0
    index = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline > start + max_chars // 2:
                end = newline
        chunk = text[start:end].strip()
        if chunk:
            out.append((index, chunk, *_reference_lines(text, start, end)))
            index += 1
        if end >= len(text):
            break
        start = max(end - overlap_chars, start + 1)
    return out


def test_line_numbers_match_the_linear_scan_on_awkward_separators() -> None:
    # ``splitlines`` splits on more than ``\n`` and the offsets assume a
    # one-character separator, so the recorded lines are approximate on
    # ``\r\n``/``\x0c``/`` `` text. The fix must be approximate in exactly
    # the same way, or every such chunk's provenance moves on the next sync.
    rng = random.Random(1)
    alphabet = ["a", "b", " ", "\n", "\n\n", "\r\n", "\r", "\x0c", " ", "word ", "x" * 50]
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 120)))
        max_chars = rng.randint(2, 60)
        overlap = rng.randint(0, max_chars - 1)
        got = [
            (c.index, c.text, c.start_line, c.end_line)
            for c in chunk_text(text, max_chars, overlap)
        ]
        assert got == _reference_chunks(text, max_chars, overlap), (text, max_chars, overlap)


def _lines_executed_during(fn) -> tuple[int, object]:
    executed = 0

    # Line events, not call events: the scan ran inside one generator
    # activation per chunk -- ``next()`` drives the filter internally -- so it
    # is a single call and a line event per offset it inspected.
    def trace(frame, event, arg):  # noqa: ARG001 - sys.settrace signature
        nonlocal executed
        if event == "line":
            executed += 1
        return trace

    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        result = fn()
    finally:
        sys.settrace(previous)
    return executed, result


def test_chunking_work_per_chunk_does_not_grow_with_the_line_count() -> None:
    # One long section of short lines: the shape a large PDF's text takes.
    text = "\n".join(f"line {i} of the tariff text" for i in range(6000))
    executed, chunks = _lines_executed_during(lambda: chunk_text(text, 400, 40))
    assert len(chunks) > 400
    # A fixed number of lines per chunk (the loop body and the constructor)
    # plus the offset pass, which is linear. The scan added a line event per
    # offset inspected per chunk -- millions here.
    assert executed <= 40 * len(chunks) + 4 * 6000, (executed, len(chunks))
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == 6000
