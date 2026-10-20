"""``[fig:n]`` markers: numbering the figures an answer may show, and checking them.

Moved out of ``assistant.answering`` (which re-exports both) to keep that
module under its size line; the seam is real — these read and rewrite
answer text and know nothing about the request around it.
"""

from __future__ import annotations

import re

_FIGURE_RE = re.compile(r"\[fig:(\d{1,2})\]")
_STANDALONE_FIGURE_RE = re.compile(r"(?m)^\s*\[fig:(\d{1,2})\]\s*$")


def number_figures(figures: list[dict], citations: list[dict]) -> list[dict]:
    """Number figures and name the citations whose documents show them.

    ``[fig:n]`` is a second marker space beside ``[n]`` rather than a reuse of
    it: a figure is not a passage and must not be counted as one when the
    answer's citations are verified.
    """
    by_node = {c.get("node_id"): c["index"] for c in citations if c.get("node_id")}
    numbered = []
    for position, figure in enumerate(figures, start=1):
        shown_in = sorted(
            {by_node[node] for node in figure.get("embedded_in") or [] if node in by_node}
        )
        if figure.get("node_id") in by_node:
            shown_in = sorted({*shown_in, by_node[figure["node_id"]]})
        numbered.append({**figure, "figure": position, "cited_in": shown_in, "shown": False})
    return numbered


def verify_figures(answer: str, figures: list[dict]) -> tuple[str, int]:
    """Drop unknown ``[fig:n]`` markers; flag figures rendered inline.

    The same rule ``verify_node`` applies to ``[n]``: a marker that resolves
    to nothing would render as a broken image, which is worse than none.
    Only a standalone marker becomes an inline image; a marker inside a
    sentence remains a text chip, so it must not suppress the image gallery.
    Returns ``(answer, dropped)``.
    """
    valid = {int(f["figure"]) for f in figures}
    dropped = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal dropped
        if int(match.group(1)) in valid:
            return match.group(0)
        dropped += 1
        return ""

    cleaned = _FIGURE_RE.sub(replace, answer or "")
    shown = {int(n) for n in _STANDALONE_FIGURE_RE.findall(cleaned)}
    for figure in figures:
        figure["shown"] = int(figure["figure"]) in shown
    return cleaned, dropped
