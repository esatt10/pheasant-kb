"""Connect memory records to the corpus they are about (Step 33.7).

Memory was a graph island. A record naming a file, a class or a contract
section produced no edge to any of them, so the graph arm could never reach
corpus content *through* a memory or a memory through corpus content — the one
thing that would make remembered context more than a parallel store.

**`entity` cannot be the bridge on its own**, which is why this is a ladder
rather than a single rule. Entity extraction only runs for `.md/.txt/.html/.xml`
(`MarkdownDocumentEnrichmentPass`), so all seven extracted document formats
produce zero entity nodes; inside Python only class names become entities;
other languages get neither pass. An entity-only bridge looks fine on a
Markdown corpus and is a silent no-op on a corpus of PDFs.

So each record is matched by the strongest signal that fires:

1. **reference** — the record names a path, a `[[wikilink]]` or a URL. Already
   works: records are `.md`, so `_markdown_links` emits an `external_reference`
   and `resolve_cross_source_edges` resolves it to the real file. This module
   only has to *notice* it happened, so a record with an explicit pointer is
   not also matched by a weaker rung.
2. **symbol** — the record names something in the `symbols` table
   (`name` or `qualified_name`). Covers code, where entities barely exist.
3. **heading** — the record names an extracted section. Covers structured
   documents, which is exactly where entities are absent.
4. **entity** — normalized label equality against a *corpus* entity.

Rung 5 in the design — a lexical BM25 anchor — is deliberately **not**
materialized here. Writing lexical edges is how the retired concept layer became
87.2% of nodes and 98.6% of edges; the search index already answers that
question at query time, for free and always fresh.

Everything is deterministic string matching over already-indexed rows: no
model, no network, rule 1 by construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Edge type emitted for every rung. Documented in `docs/graph_model.md`.
ABOUT_EDGE = "about"

#: Phase 3: a near-duplicate cluster's medoid promotion, distinct from
#: `supersedes` — a subsumed record is redundant but still TRUE, so this
#: edge (and the `subsumed_by` field it is drawn from) must never feed
#: `memory.projection.effective_valid_until`. Documented in
#: `docs/graph_model.md`.
SUBSUMES_EDGE = "subsumes"

#: Signals, strongest first. A record takes the first rung that yields targets.
SIGNALS = ("reference", "symbol", "heading", "entity")

#: Confidence stamped on the edge, so a consumer can tell an explicit pointer
#: from an inferred one. `upsert_edge` already carries `confidence`.
CONFIDENCE = {"reference": 1.0, "symbol": 0.8, "heading": 0.7, "entity": 0.5}

#: Hard cap on targets per record per rung. The concept layer had no ceiling
#: and that is precisely how it exploded; here the total is bounded by
#: `targets x records`, and records are themselves bounded by
#: `memory.max_records`.
DEFAULT_MAX_TARGETS = 3

#: Identifier-shaped words worth looking up as symbols: CamelCase, snake_case,
#: dotted paths, or anything with an underscore. A bare lowercase word like
#: "deploy" is prose and would match half a codebase.
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]+)*\b")

#: Quoted spans and backticked spans are how people cite a heading or a section
#: title inside prose, so they are read as one candidate rather than as words.
_QUOTED_RE = re.compile(r"[\"'`]([^\"'`]{3,120})[\"'`]")


def _looks_like_identifier(word: str) -> bool:
    if len(word) < 3:
        return False
    if "_" in word or "." in word:
        return True
    # CamelCase / PascalCase: an internal capital after a lowercase letter.
    return bool(re.search(r"[a-z][A-Z]", word))


@dataclass(frozen=True)
class BridgeEdge:
    """One `memory_record --about--> target` edge, with why it was drawn."""

    record_id: str
    artifact_id: str
    target_id: str
    signal: str
    matched: str

    @property
    def confidence(self) -> float:
        return CONFIDENCE.get(self.signal, 0.5)


def candidate_identifiers(text: str, limit: int = 40) -> list[str]:
    """Identifier-shaped tokens in a memory's text, in order, deduplicated."""
    out: list[str] = []
    seen: set[str] = set()
    for word in _IDENTIFIER_RE.findall(text or ""):
        if not _looks_like_identifier(word) or word.lower() in seen:
            continue
        seen.add(word.lower())
        out.append(word)
        if len(out) >= limit:
            break
    return out


def candidate_phrases(text: str, limit: int = 12) -> list[str]:
    """Quoted or backticked spans — how a heading gets cited in prose."""
    out: list[str] = []
    seen: set[str] = set()
    for phrase in _QUOTED_RE.findall(text or ""):
        cleaned = phrase.strip()
        if len(cleaned) < 3 or cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        out.append(cleaned)
        if len(out) >= limit:
            break
    return out


#: Leading articles are dropped when comparing labels. The extractor's Title
#: Case pattern happily starts a run at "The", so a corpus sentence reading
#: "The Kestrel Gateway is operated by..." yields the entity *The Kestrel
#: Gateway* while a memory saying "Kestrel Gateway needs..." yields *Kestrel
#: Gateway*. Those are the same thing, and exact equality said they were not.
_LEADING_ARTICLES = ("the ", "a ", "an ")


def normalize_label(value: str) -> str:
    """The comparable form of an entity label or heading title."""
    normalized = " ".join(str(value or "").split()).strip().lower()
    for article in _LEADING_ARTICLES:
        if normalized.startswith(article):
            return normalized[len(article) :]
    return normalized


@dataclass(frozen=True)
class BridgeInputs:
    """Everything the ladder needs, already read out of state and the graph.

    Gathered by the caller so the resolution itself stays pure and testable:
    the interesting behaviour is which rung fires, and that should be provable
    without a sync.
    """

    #: `memory_records` rows: record_id, artifact_id, supersedes.
    records: list[dict[str, Any]]
    #: artifact_id -> the record's indexed text.
    texts: dict[str, str]
    #: normalized symbol name -> corpus artifact ids defining it.
    symbols: dict[str, list[str]]
    #: normalized heading title -> corpus heading node ids.
    headings: dict[str, list[str]]
    #: normalized entity label -> corpus entity node ids.
    entities: dict[str, list[str]]
    #: memory artifact_id -> targets an explicit reference already resolved to.
    referenced: dict[str, list[str]]


def resolve_bridges(
    inputs: BridgeInputs, max_targets: int = DEFAULT_MAX_TARGETS
) -> tuple[list[BridgeEdge], list[str]]:
    """`(edges, unbridged_record_ids)` — the ladder, strongest rung first.

    A record takes the **first** rung that yields any target and stops. That is
    the point of a precedence order: a record that names `docs/deploy.md`
    outright should link to that file, not also to every symbol whose name it
    happens to mention while explaining itself.

    Deterministic — inputs are sorted and the cap is applied after sorting — so
    two runs over the same corpus emit identical edges and re-syncing is a
    no-op through `upsert_edge`.
    """
    edges: list[BridgeEdge] = []
    unbridged: list[str] = []

    for record in sorted(inputs.records, key=lambda r: str(r.get("record_id") or "")):
        record_id = str(record.get("record_id") or "")
        artifact_id = str(record.get("artifact_id") or "")
        if not record_id or not artifact_id:
            continue
        text = inputs.texts.get(artifact_id, "")
        found: list[BridgeEdge] = []

        for signal in SIGNALS:
            matches = _match(signal, artifact_id, text, inputs)
            if not matches:
                continue
            for target_id, matched in matches[:max_targets]:
                found.append(
                    BridgeEdge(
                        record_id=record_id,
                        artifact_id=artifact_id,
                        target_id=target_id,
                        signal=signal,
                        matched=matched,
                    )
                )
            break

        if found:
            edges.extend(found)
        else:
            unbridged.append(record_id)

    return edges, unbridged


def _match(signal: str, artifact_id: str, text: str, inputs: BridgeInputs) -> list[tuple[str, str]]:
    """`[(target_id, what_matched)]` for one rung, deterministically ordered."""
    if signal == "reference":
        # Already resolved by the cross-source pass; this rung only has to
        # notice it, so an explicit pointer outranks any inferred match.
        return [
            (target, "explicit reference")
            for target in sorted(inputs.referenced.get(artifact_id, []))
        ]

    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    if signal == "symbol":
        for word in candidate_identifiers(text):
            for target in inputs.symbols.get(word.lower(), []):
                if target not in seen:
                    seen.add(target)
                    out.append((target, word))
        return out

    lookup = inputs.headings if signal == "heading" else inputs.entities
    candidates = candidate_phrases(text) if signal == "heading" else _entity_candidates(text)
    for phrase in candidates:
        for target in lookup.get(normalize_label(phrase), []):
            if target not in seen:
                seen.add(target)
                out.append((target, phrase))
    return out


def _entity_candidates(text: str) -> list[str]:
    """Capitalised runs, the same shapes the entity enrichment pass extracts.

    Matching the extractor's own notion of an entity is what makes the labels
    line up; inventing a second one here would silently miss half of them.
    """
    out: list[str] = []
    seen: set[str] = set()
    for pattern in (
        r"\b[A-Z][A-Za-z0-9]+(?:[A-Z][A-Za-z0-9]+)+\b",
        r"\b[A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,3}\b",
        r"\b[A-Z]{2,}\b",
    ):
        for match in re.findall(pattern, text or ""):
            key = normalize_label(match)
            if key and key not in seen:
                seen.add(key)
                out.append(match)
    return out


def supersedes_edges(records: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """`(newer_artifact_id, older_artifact_id)` for every correction.

    `supersedes` is a documented edge type (`docs/graph_model.md`) that nothing
    emitted: a correction existed only as a frontmatter string resolved in
    Python, so the graph could not answer "what replaced this". Records naming
    an id that is not in the store are skipped rather than pointing at nothing.
    """
    by_record = {
        str(record.get("record_id")): str(record.get("artifact_id") or "") for record in records
    }
    out: list[tuple[str, str]] = []
    for record in sorted(records, key=lambda r: str(r.get("record_id") or "")):
        target = record.get("supersedes")
        newer = str(record.get("artifact_id") or "")
        older = by_record.get(str(target or ""))
        if target and newer and older:
            out.append((newer, older))
    return out


def subsumes_edges(records: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """`(canonical_artifact_id, member_artifact_id)` for every subsumption
    (Phase 3). Same shape as :func:`supersedes_edges`, keyed off
    `subsumed_by` instead of `supersedes` — kept as a **separate**
    function rather than a shared helper, because the two fields must never
    be conflated: `supersedes` means *corrected* and feeds
    `memory.projection.effective_valid_until`; `subsumed_by` means
    *redundant but still true* and must not. A record naming a canonical id
    that is not in the store (e.g. one that was itself later archived) is
    skipped rather than pointing at nothing — the ledger
    (`memory_compactions`) is the durable record of that decision, not
    this edge.
    """
    by_record = {
        str(record.get("record_id")): str(record.get("artifact_id") or "") for record in records
    }
    out: list[tuple[str, str]] = []
    for record in sorted(records, key=lambda r: str(r.get("record_id") or "")):
        target = record.get("subsumed_by")
        member = str(record.get("artifact_id") or "")
        canonical = by_record.get(str(target or ""))
        if target and member and canonical:
            out.append((canonical, member))
    return out
