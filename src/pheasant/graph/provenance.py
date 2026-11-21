"""Where a chunk sits in its document: its neighbours, its section, its pages.

A document's chunks used to be a set: each had an index, a line range and a
section *name* (``heading_path``), and the graph held them as leaves of the
document (`has_chunk`). Nothing could step from a passage to the one before
it, or from a section to the passages that hold its text, without reading
rows and sorting them. Three edge types and four attributes say it in the
graph, all derived from what the indexer already has:

* ``next_chunk`` -- chunk *i* to chunk *i + 1* of the same document. Its
  predecessor is the in-edge.
* ``section_has_chunk`` -- a heading to every chunk holding any of its own
  text: the lines from the heading to the next heading of any level. A chunk
  packed across two sections belongs to both; front matter before the first
  heading belongs to none.
* ``next_heading`` -- a heading to the next one in document order, whatever
  its level (``contains`` already says which encloses which).
* ``chunk_index`` on a chunk, ``order`` on a heading (its position among the
  document's headings, from 0), and ``page_start`` / ``page_end`` on a chunk
  and ``page`` on a heading when the document has pages (a PDF read by
  PDFium; ``ingestion.extractor.PagedText``).

They describe structure, not meaning, and nothing that ranks may read them:
relationship search skips the edges (``search.graph_search`` and the row
store's narrowing), node search skips the attributes and the node index never
holds them, and answer facts treat the edges as structural. So a search
returns what it returned before they existed -- ``tests/test_chunk_provenance.py``
holds that against the same corpus indexed with and without them.

Every edge here belongs to one document (chunk and heading ids carry its
path), so an incremental re-index re-derives them and retracts what the new
text no longer implies, in O(document): see :func:`retract_unwanted`.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Sequence
from typing import Any

NEXT_CHUNK = "next_chunk"
SECTION_HAS_CHUNK = "section_has_chunk"
NEXT_HEADING = "next_heading"

#: The edge types this module draws. Structural: never scored, never a fact.
PROVENANCE_EDGE_TYPES = frozenset({NEXT_CHUNK, SECTION_HAS_CHUNK, NEXT_HEADING})

#: The attributes it adds. Numbers with no search value, which is exactly why
#: node search must not read them: a query for "2024" would otherwise match
#: page 2024 of every long PDF.
PROVENANCE_ATTRIBUTES = frozenset({"chunk_index", "order", "page", "page_start", "page_end"})


def page_starts(page_lines: Sequence[int | None] | None) -> tuple[list[int], list[int]]:
    """``page_lines`` as two parallel ascending lists: first lines, page numbers.

    ``page_lines[n]`` is the line page ``n + 1`` starts on, or ``None`` for a
    page with no text (which therefore holds no line).
    """

    lines: list[int] = []
    numbers: list[int] = []
    for number, line in enumerate(page_lines or (), start=1):
        if line is None:
            continue
        if lines and line < lines[-1]:
            return [], []  # not ascending: not a page map this module trusts
        lines.append(int(line))
        numbers.append(number)
    return lines, numbers


def page_of(starts: tuple[list[int], list[int]], line: int | None) -> int | None:
    """The page a line falls on, or ``None`` when there are no pages."""

    lines, numbers = starts
    if not lines or line is None:
        return None
    position = bisect_right(lines, int(line)) - 1
    return numbers[max(position, 0)]


def chunk_pages(
    starts: tuple[list[int], list[int]], start_line: int, end_line: int
) -> tuple[int | None, int | None]:
    first = page_of(starts, start_line)
    if first is None:
        return None, None
    last = page_of(starts, max(start_line, end_line))
    return first, max(first, last or first)


def section_chunks(
    headings: Sequence[tuple[str, int]],
    chunks: Sequence[tuple[str, int, int]],
) -> dict[str, list[str]]:
    """Each heading's chunks: those holding any line of the heading's own text.

    ``headings`` are ``(heading_id, line)`` in document order (a repeated id,
    two sections with one breadcrumb, unions its spans); ``chunks`` are
    ``(chunk_id, start_line, end_line)`` in index order. A heading's own text
    runs from its line to the line before the next heading.
    """

    found: dict[str, list[str]] = {}
    ordered = sorted(chunks, key=lambda chunk: (chunk[1], chunk[2]))
    starts = [chunk[1] for chunk in ordered]
    for position, (heading_id, line) in enumerate(headings):
        end = headings[position + 1][1] - 1 if position + 1 < len(headings) else None
        if end is not None and end < line:
            end = line  # two headings on one line: each keeps its own line
        targets = found.setdefault(heading_id, [])
        # A chunk starting after `end` cannot overlap, so walk back from the
        # last one that starts in time. Chunks are cut in document order, so
        # their last lines ascend too: the first one ending before `line` ends
        # the walk, which keeps a document of thousands of sections linear
        # rather than headings x chunks.
        stop = len(ordered) if end is None else bisect_right(starts, end)
        span: list[str] = []
        for chunk_id, _start, finish in reversed(ordered[:stop]):
            if finish < line:
                break
            span.append(chunk_id)
        for chunk_id in reversed(span):
            if chunk_id not in targets:
                targets.append(chunk_id)
    return found


def retract_unwanted(
    graph: Any,
    sources: Iterable[str],
    edge_type: str,
    wanted: dict[str, set[str]],
) -> int:
    """Remove ``edge_type`` edges out of ``sources`` that ``wanted`` does not name.

    By type, never by pair: a parallel edge of another type between the same
    two nodes is untouched. An edge still wanted is left as it is, so an
    unchanged document re-indexed keeps its rows and its generation id.
    """

    sources = [node for node in dict.fromkeys(sources) if node in graph]
    if not sources:
        return 0
    batch = getattr(graph, "out_edges_batch", None)
    adjacency = (
        batch(sources)
        if callable(batch)
        else {node: list(graph.out_edges(node)) for node in sources}
    )
    removed = 0
    for source in sources:
        keep = wanted.get(source, set())
        for _src, target, edge_map in adjacency.get(source, ()):
            if target in keep:
                continue
            if any(data.get("type") == edge_type for data in edge_map.values()):
                removed += graph.remove_edges_where(
                    source, target, lambda attrs: attrs.get("type") == edge_type
                )
    return removed
