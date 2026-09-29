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
                    return " ".join(str(chunk["summary"]).split())
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
