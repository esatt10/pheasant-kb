"""Documents that show images: finding the links, and resolving them.

A Markdown or HTML document references images — ``![Architecture](img/arch.png)``,
Obsidian's ``![[flow.png]]``, ``<img src alt>``. :func:`image_links` finds them
during enrichment (each becomes an ``external_reference`` of type
``image_link`` with the author's alt text), and :func:`resolve_image_edges`
turns each into an ``embeds`` edge to the ``image`` artifact it names once
both are indexed — the same two-phase shape document links have.

Separate from ``enrichment`` on purpose, and not only for that module's size:
image links resolve to ``image`` nodes, which the document resolver and its
optional WASM twin were never specified or measured on, so they are resolved
by their own pure-Python pass rather than by widening that one. Deterministic
and rule-based throughout (CLAUDE.md rule 1).
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any

from pheasant.ingestion.content_types import IMAGE_EXTENSIONS

_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_WIKI_IMAGE_RE = re.compile(r"!\[\[([^\]|#]+)(?:[|#]([^\]]*))?\]\]")
_HTML_IMAGE_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_HTML_ATTR_RE = re.compile(r"\b(src|alt)\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
MAX_IMAGE_LINKS = 50


def image_links(text: str) -> tuple[list[tuple[str, str]], str]:
    """Local image references in document order, and the text without them.

    ``![alt](img/a.png)``, Obsidian's ``![[a.png]]`` and HTML ``<img src alt>``.
    Only targets that name an image pheasant can index, and never a remote
    URL or a ``data:`` URI: those are not in the corpus, so there is nothing to
    resolve them to and nothing this region could show. Those stay in the text
    for :func:`_markdown_links`, exactly as before.

    The returned text has the *local* image syntax removed so the ordinary
    link pass does not also record ``img/a.png`` as a ``document_link`` — one
    reference, one edge type. Deterministic, like every rule here (rule 1).
    """

    found: list[tuple[int, str, str]] = []
    spans: list[tuple[int, int]] = []

    def keep(position: int, span: tuple[int, int], target: str, alt: str) -> None:
        target = target.strip()
        path = target.split("#", 1)[0].split("?", 1)[0]
        if not path or path.startswith(("http://", "https://", "data:", "//")):
            return
        if Path(path).suffix.lower() not in IMAGE_EXTENSIONS:
            return
        found.append((position, path, " ".join(alt.split())[:200]))
        spans.append(span)

    for match in _MD_IMAGE_RE.finditer(text):
        keep(match.start(), match.span(), match.group(2), match.group(1))
    for match in _WIKI_IMAGE_RE.finditer(text):
        # `![[a.png|300]]` / `|300x200` is Obsidian's size, not alt text.
        alt = match.group(2) or ""
        if re.fullmatch(r"\s*\d+(?:x\d+)?\s*", alt):
            alt = ""
        keep(match.start(), match.span(), match.group(1), alt)
    for match in _HTML_IMAGE_RE.finditer(text):
        attributes = {key.lower(): value for key, value in _HTML_ATTR_RE.findall(match.group(0))}
        if "src" in attributes:
            keep(match.start(), match.span(), attributes["src"], attributes.get("alt", ""))

    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _position, path, alt in sorted(found):
        if path.lower() in seen:
            continue
        seen.add(path.lower())
        links.append((path, alt))
        if len(links) >= MAX_IMAGE_LINKS:
            break
    remaining = text
    for start, end in sorted(spans, reverse=True):
        remaining = remaining[:start] + " " + remaining[end:]
    return links, remaining


#: The node types an ``image_link`` may resolve to. Kept apart from
#: ``ARTIFACT_TYPES`` on purpose: that set is what similarity, facts and the
#: neighbour walk treat as a *document*, and a caption is not one (see
#: ``content_types.ARTIFACT_TYPES``). An image is only ever the far end of an
#: ``embeds`` edge.
MEDIA_NODE_TYPES = frozenset({"image"})


def resolve_image_edges(
    referrers: dict[str, dict[str, Any]],
    media: list[tuple[str, dict[str, Any]]],
    ext_nodes: dict[str, dict[str, Any]],
    edges: list[tuple[str, str]],
) -> list[Any]:
    """``embeds`` edges from documents to the images they show.

    Pure Python and separate from :func:`resolve_cross_source_edges` so the
    optional WASM twin of that function keeps exactly the inputs it was
    measured on. ``edges`` is ``(artifact_id, external_reference_id)`` for
    each ``image_link``. Resolution tries the path relative to the referring
    document first — ``img/a.png`` from ``docs/guide.md`` is
    ``docs/img/a.png`` — and falls back to the same longest-suffix match
    document links use, preferring the referrer's own source on a tie.
    Deterministic and idempotent: edges are upserted and sorted.
    """

    # Imported here: `enrichment` imports this module for `image_links`, and
    # these are its path-matching primitives, shared rather than copied.
    from pheasant.graph.enrichment import EnrichmentEdge, _ArtifactRef, _match_suffix, _norm_rel

    by_path: dict[str, list[_ArtifactRef]] = {}
    for node_id, attrs in media:
        rel, src = attrs.get("relative_path"), attrs.get("source_id")
        if rel and src:
            by_path.setdefault(_norm_rel(rel), []).append(_ArtifactRef(node_id, src, rel))

    out: list[EnrichmentEdge] = []
    seen: set[tuple[str, str]] = set()
    for artifact_id, ext_id in edges:
        ext = ext_nodes.get(ext_id)
        referrer = referrers.get(artifact_id)
        if ext is None or referrer is None:
            continue
        reference = str(ext.get("reference") or "")
        cleaned = reference.split("#", 1)[0].split("?", 1)[0].strip()
        if not cleaned:
            continue
        base = posixpath.dirname(str(referrer.get("relative_path") or ""))
        relative = posixpath.normpath(posixpath.join(base, cleaned)) if base else cleaned
        candidates = by_path.get(_norm_rel(relative)) or _match_suffix(
            cleaned.lstrip("./"), by_path
        )
        own_source = [ref for ref in candidates if ref.source_id == referrer.get("source_id")]
        for target in (own_source or candidates)[:1]:
            key = (artifact_id, target.node_id)
            if key in seen:
                continue
            seen.add(key)
            attrs: dict[str, Any] = {
                "source_id": referrer.get("source_id"),
                "target_source_id": target.source_id,
                "reference": reference,
                "reference_type": "image_link",
                "cross_source": target.source_id != referrer.get("source_id"),
                "enrichment_pass": "image_resolution",
            }
            if ext.get("alt"):
                attrs["alt"] = ext["alt"]
            out.append(EnrichmentEdge(artifact_id, target.node_id, "embeds", attrs))
    out.sort(key=lambda e: (e.source, e.target))
    return out
