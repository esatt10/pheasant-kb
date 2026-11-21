"""Extracted text made stable, and where its pages start.

``_tidy`` is every reader's last step, so the same bytes always chunk the same
way; ``paged_text`` tidies a document's pages as one text and says where each
page starts in it (`graph/provenance.py`). Split out of
``ingestion/extractor.py``, which re-exports all three.
"""

from __future__ import annotations

import re


class PagedText(str):
    """Extracted text that knows where each of its pages starts.

    The text is exactly what the reader produced without it -- the pages
    joined and tidied as one, so chunks, their hashes and every index over
    them are unchanged -- and ``page_lines[n]`` is the line page ``n + 1``
    starts on (``None`` for a page with no text). A ``str`` so every caller
    that only wants text keeps getting text; the pipeline reads the pages
    and drops the subclass at once (``ingestion.pipeline``), because a str
    subclass is not plain data anywhere past that.
    """

    page_lines: tuple[int | None, ...] = ()


def paged_text(pages: list[str]) -> str:
    """``_tidy`` of the pages joined, carrying the line each page starts on.

    Each page's own tidied text is found, in order, in the tidied whole:
    tidying is local to runs of whitespace, so a page's text appears intact
    and only the whitespace between pages differs. Anything else between two
    pages, or a page that cannot be found, means this rule does not hold for
    the document, and the text comes back with no pages rather than wrong
    ones.
    """

    text = _tidy("\n\n".join(pages))
    starts: list[int | None] = []
    cursor = 0
    line = 1
    for page in pages:
        piece = _tidy(page)
        if not piece:
            starts.append(None)
            continue
        found = text.find(piece, cursor)
        if found < 0 or text[cursor:found].strip():
            return text
        line += text.count("\n", cursor, found)
        starts.append(line)
        line += piece.count("\n")
        cursor = found + len(piece)
    if not any(start is not None for start in starts):
        return text
    out = PagedText(text)
    out.page_lines = tuple(starts)
    return out


def _tidy(text: str) -> str:
    """Collapse extraction artifacts into stable, chunkable text.

    Deterministic and idempotent: ``_tidy(_tidy(x)) == _tidy(x)``, so the same
    bytes always chunk identically across runs and platforms.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
