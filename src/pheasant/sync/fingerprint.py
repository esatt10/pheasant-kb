"""What counts as a change big enough to invalidate what is already indexed.

Restarting a container must not re-index anything: the state directory holds
the artifacts, chunks, vectors and graph, and an unchanged config over
unchanged content is zero work. But some config edits *do* invalidate stored
state — re-chunking with different bounds produces different chunk ids,
widening an include glob admits files that were never read, and a different
embedding model produces vectors that are not comparable with the old ones.

So each scope carries a fingerprint of exactly the settings whose change would
make stored state wrong. Same fingerprint → resume. Different → redo, and only
the part that is actually invalid:

* **source fingerprint** covers what the *content* pipeline does (globs, depth,
  chunking, connector identity). A change re-reads that source in full.
* **embedding fingerprint** covers the vector space (provider, model,
  dimensions, store). A change re-embeds from the chunks already in SQLite —
  no file is re-read, because the text did not change, only its projection.

Everything else — a new chat model, a scheduler interval, log level, Obsidian
options — is deliberately *not* in either fingerprint: it cannot make one
stored byte wrong, so it must never trigger re-indexing.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pheasant.config.schema import PheasantConfig, SourceConfig

SOURCE_SCOPE = "source:{name}"
EMBEDDING_SCOPE = "embeddings"


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


#: Bumped when the *code* — not the config — changes what text a memory source
#: produces. Step 33.5 stopped indexing record frontmatter as prose, so chunks
#: written before it are wrong, and a re-read is the fix. Carrying it in the
#: fingerprint means the existing "config changed → escalate to full" path does
#: the migration itself: no bespoke marker, no one-shot script, idempotent
#: because the new fingerprint is stored once the full pass succeeds.
MEMORY_TEXT_PIPELINE = "33.5-frontmatter-stripped"


#: The same, for web collections. Listed URLs stopped being filtered through
#: the folder-walk include globs, and HTML is now recognised by the served
#: content type as well as by extension — so a page indexed before as raw
#: markup, or never indexed at all, is wrong in the store. One full pass per
#: web source fixes both and is then recorded, so it never recurs.
WEB_TEXT_PIPELINE = "listed-urls-html-by-content-type"


def source_fingerprint(source: SourceConfig, *, html_text: bool | None = None) -> str:
    """Fingerprint the settings that decide *what text* a source produces.

    ``html_text`` is the region-wide HTML extraction switch. It is folded in
    for web collections only, where every item is a page and turning it on
    changes all of their text; a folder source keeps its fingerprint, so
    flipping it does not re-read every repository in the region.
    """

    chunking = getattr(source, "chunking", None)
    source_type = getattr(source.type, "value", str(source.type))
    payload: dict[str, Any] = {
        "type": source_type,
        "path": str(getattr(source, "path", "") or ""),
        "url": str(getattr(source, "url", "") or ""),
        "include": sorted(source.include or []),
        "exclude": sorted(source.exclude or []),
        "max_depth": getattr(source, "max_depth", None),
        "chunking": {
            "enabled": getattr(chunking, "enabled", None),
            "strategy": getattr(chunking, "strategy", None),
            "max_chars": getattr(chunking, "max_chars", None),
            "overlap_chars": getattr(chunking, "overlap_chars", None),
        },
    }
    if source_type == "memory":
        # Scoped to memory on purpose. Every other source's text pipeline is
        # unchanged, and adding this key unconditionally would invalidate every
        # fingerprint in every deployment — re-reading a 2,000-file repository
        # to fix agent memory is a re-index nobody asked for.
        payload["text_pipeline"] = MEMORY_TEXT_PIPELINE
    if source_type == "web_collection":
        payload["text_pipeline"] = WEB_TEXT_PIPELINE
        payload["html_text"] = bool(html_text)
    return _digest(payload)


def embedding_fingerprint(config: PheasantConfig) -> str:
    """Fingerprint the vector space. ``"disabled"`` when embeddings are off.

    ``base_url`` is excluded on purpose: pointing at a mirror of the same model
    yields the same vectors, and re-embedding a large corpus because someone
    switched hostnames is exactly the kind of surprise cost this guards
    against.
    """

    embeddings = config.search.embeddings
    if not embeddings.enabled:
        return "disabled"
    return _digest(
        {
            "provider": embeddings.provider,
            "model": embeddings.model,
            "dimensions": embeddings.dimensions,
            "store": config.search.vector_store.provider,
        }
    )


def embedding_change(previous: str | None, current: str) -> str:
    """Classify an embedding-config transition.

    * ``"none"``      — same space (or still disabled): nothing to do.
    * ``"introduced"`` — embeddings are new (or were never fingerprinted), so
      existing chunks have no vectors yet: embed them, keep what is there.
    * ``"invalidated"`` — the space changed under us: drop the old vectors
      first, they are not comparable with the new ones.
    * ``"disabled"``  — embeddings were turned off: leave stored vectors alone
      (turning them back on is then a no-op rather than a re-embed).
    """

    if current == previous:
        return "none"
    if current == "disabled":
        return "disabled"
    if previous is None or previous == "disabled":
        return "introduced"
    return "invalidated"
