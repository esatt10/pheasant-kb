"""Figures: the images a set of cited nodes shows, read off ``embeds`` edges.

An answer cites documents. When a cited document embeds an image the corpus
holds — ``![Architecture](img/arch.png)`` resolved to the ``image`` artifact
by ``enrichment.resolve_image_edges`` — that image is a figure the answer can
show and an agent can fetch. A cited node that *is* an image (its caption
matched the question) is a figure of itself.

Deterministic: figures keep the order their first citing node was given in,
and each lists every cited node that embeds it. The caption is the image's own
indexed text (the first chunk's summary), which is what search matched it on;
the document's alt text rides along separately because it is the author's
words for the image *in that document*.

Works against a resident graph, the row-backed ``SqlGraph``, and — through
``remote_figures`` — the fleet's graph service, the same three the facts panel
already supports.
"""

from __future__ import annotations

from typing import Any

MAX_FIGURES = 8
#: The graph stores ``chunk.text[:180]`` as a chunk's summary
#: (``GraphBuilder``), so a caption read from it is cut wherever character
#: 180 fell — mid-clause, in practice.
SUMMARY_CHARS = 180
#: How much of an image's own text a caption shows.
CAPTION_CHARS = 400


def tidy_caption(text: str, *, truncated: bool = False, limit: int = CAPTION_CHARS) -> str:
    """A caption that ends where a reader expects one to.

    Cut at the last sentence end inside ``limit`` when there is one late
    enough to keep most of the text, else at a word boundary, and say it was
    cut with an ellipsis. ``truncated`` marks text that was already cut
    upstream (a graph summary), which needs the same treatment at any length.
    """

    text = " ".join(str(text or "").split())
    if not text or (len(text) <= limit and not truncated):
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(". "), cut.rfind("; "))
    if boundary >= len(cut) * 0.5:
        return cut[: boundary + 1].rstrip(";") + " …"
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else cut).rstrip(" ,;:") + " …"


def with_full_captions(state: Any, figures: list[dict]) -> list[dict]:
    """Replace graph-summary captions with each image's own indexed text.

    The graph holds a 180-character summary; the state store holds the text
    the captioner (or an authored sidecar) produced, whole. Every serving
    role can read the state store, so this is one small query for up to
    :data:`MAX_FIGURES` images. Best-effort: without a state store, or on any
    failure, the tidied summary stands.
    """

    if state is None or not figures:
        return figures
    ids = [str(f["node_id"]) for f in figures if f.get("node_id")]
    if not ids:
        return figures
    try:
        rows = state.rows(
            "SELECT artifact_id, text FROM chunks WHERE chunk_index=0 AND artifact_id IN ("
            + ",".join("?" for _ in ids)
            + ")",
            tuple(ids),
        )
    except Exception:  # pragma: no cover - a caption is never load-bearing
        return figures
    texts = {str(row["artifact_id"]): str(row["text"] or "") for row in rows}
    for figure in figures:
        text = texts.get(str(figure.get("node_id")))
        if text and text.strip():
            figure["caption"] = tidy_caption(text)
    return figures


def collect_figures(graph: Any, node_ids: list[str], limit: int = MAX_FIGURES) -> list[dict]:
    if graph is None or not node_ids:
        return []
    remote = getattr(graph, "remote_figures", None)
    if callable(remote):
        return list(remote(node_ids=node_ids, limit=limit) or [])

    def attrs_of(node_id: str) -> dict[str, Any] | None:
        try:
            return dict(graph.nodes[node_id])
        except (KeyError, AttributeError, TypeError):
            return None

    def caption_of(image_id: str) -> str:
        for _source, target, edge_map in graph.out_edges(image_id):
            if any(data.get("type") == "has_chunk" for data in edge_map.values()):
                chunk = attrs_of(target) or {}
                if chunk.get("summary"):
                    summary = str(chunk["summary"])
                    return tidy_caption(summary, truncated=len(summary) >= SUMMARY_CHARS)
        return ""

    figures: dict[str, dict[str, Any]] = {}

    def add(image_id: str, image: dict[str, Any], via: str | None, alt: str | None) -> None:
        entry = figures.get(image_id)
        if entry is None:
            entry = figures[image_id] = {
                "node_id": image_id,
                "relative_path": image.get("relative_path"),
                "source_id": image.get("source_id"),
                "caption": caption_of(image_id),
                "alt": "",
                "embedded_in": [],
            }
        if via and via not in entry["embedded_in"]:
            entry["embedded_in"].append(via)
        if alt and not entry["alt"]:
            entry["alt"] = str(alt)

    for node_id in node_ids:
        if len(figures) >= limit:
            break
        if node_id not in graph:
            continue
        own = attrs_of(node_id) or {}
        if own.get("type") == "image":
            add(node_id, own, None, None)
            continue
        for _source, target, edge_map in graph.out_edges(node_id):
            for data in edge_map.values():
                if data.get("type") != "embeds":
                    continue
                image = attrs_of(target)
                if image and image.get("type") == "image":
                    add(target, image, node_id, data.get("alt"))
    return list(figures.values())[:limit]
