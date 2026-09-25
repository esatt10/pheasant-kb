from __future__ import annotations

import ast
import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from pheasant.config.schema import SourceConfig
from pheasant.ingestion.content_types import ARTIFACT_TYPES
from pheasant.ingestion.pipeline import ParsedArtifact

ENRICHED_NODE_TYPES = {"symbol", "entity", "concept", "external_reference"}
ENRICHED_EDGE_TYPES = {
    "references",
    "imports",
    "calls",
    "similar_to",
    "derived_from",
    "mentions",
}

# PostgreSQL B-tree keys are bounded, while a URL, citation, or generated
# import string is not. Keep two maximum-size enrichment IDs comfortably below
# the graph-edge primary-key limit after the knowledge-base and edge fields are
# included too.
MAX_ENRICHMENT_NODE_ID_LENGTH = 512

STOPWORDS = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "but",
    "can",
    "for",
    "from",
    "has",
    "into",
    "its",
    "not",
    "the",
    "this",
    "with",
    "your",
}

# Synapse 21.6B: concept normalization. A small, deterministic stoplist of
# words whose trailing "s"/"es"/"ies" must NOT be singularized — either
# because the singular changes meaning (status -> statu) or the word is not a
# plural at all (its, this, analysis, basis, ...). No NLP dependency is used;
# the rules below are pure-python and reproducible so concept ids stay stable.
SINGULARIZE_STOPLIST = frozenset(
    {
        "analysis",
        "axis",
        "basis",
        "bias",
        "bus",
        "canvas",
        "class",
        "css",
        "data",
        "dns",
        "focus",
        "gas",
        "https",
        "ies",
        "index",
        "ios",
        "is",
        "its",
        "less",
        "lens",
        "news",
        "os",
        "pass",
        "process",
        "series",
        "species",
        "status",
        "this",
        "thus",
        "tls",
        "ux",
        "yes",
    }
)


@dataclass(frozen=True)
class EnrichmentNode:
    id: str
    type: str
    label: str
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EnrichmentEdge:
    source: str
    target: str
    type: str
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArtifactEnrichment:
    nodes: list[EnrichmentNode] = field(default_factory=list)
    edges: list[EnrichmentEdge] = field(default_factory=list)
    terms: list[dict[str, Any]] = field(default_factory=list)
    symbols: list[dict[str, Any]] = field(default_factory=list)
    concept_terms: set[str] = field(default_factory=set)

    def extend(self, other: ArtifactEnrichment) -> None:
        self.nodes.extend(other.nodes)
        self.edges.extend(other.edges)
        self.terms.extend(other.terms)
        self.symbols.extend(other.symbols)
        self.concept_terms.update(other.concept_terms)


class ArtifactEnrichmentPass(Protocol):
    name: str

    def run(
        self,
        kb_id: str,
        source: SourceConfig,
        artifact: ParsedArtifact,
    ) -> ArtifactEnrichment: ...


class CodeEnrichmentPass:
    name = "code"

    def run(
        self,
        kb_id: str,
        source: SourceConfig,
        artifact: ParsedArtifact,
    ) -> ArtifactEnrichment:
        if Path(artifact.relative_path).suffix.lower() != ".py":
            return ArtifactEnrichment()
        text = artifact_text(artifact)
        enrichment = _base_concepts(kb_id, source, artifact, text)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return enrichment

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    _add_external_reference(
                        enrichment,
                        kb_id,
                        source,
                        artifact,
                        alias.name,
                        "imports",
                        "python_import",
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                _add_external_reference(
                    enrichment,
                    kb_id,
                    source,
                    artifact,
                    node.module,
                    "imports",
                    "python_import",
                )
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                symbol_type = "class" if isinstance(node, ast.ClassDef) else "function"
                _add_symbol(
                    enrichment,
                    kb_id,
                    source,
                    artifact,
                    node.name,
                    symbol_type,
                    node.lineno,
                    getattr(node, "end_lineno", node.lineno),
                )
                if isinstance(node, ast.ClassDef):
                    _add_entity(enrichment, kb_id, source, artifact, node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                for target in _assignment_targets(node):
                    if target.isupper():
                        _add_symbol(
                            enrichment,
                            kb_id,
                            source,
                            artifact,
                            target,
                            "constant",
                            node.lineno,
                            getattr(node, "end_lineno", node.lineno),
                        )
            elif isinstance(node, ast.Call):
                call_name = _call_name(node.func)
                if call_name:
                    _add_call(enrichment, kb_id, source, artifact, call_name, node.lineno)
        return enrichment


class MarkdownDocumentEnrichmentPass:
    name = "markdown_document"

    def run(
        self,
        kb_id: str,
        source: SourceConfig,
        artifact: ParsedArtifact,
    ) -> ArtifactEnrichment:
        suffix = Path(artifact.relative_path).suffix.lower()
        if suffix not in {".md", ".txt", ".html", ".xml"}:
            return ArtifactEnrichment()
        text = artifact_text(artifact)
        enrichment = _base_concepts(kb_id, source, artifact, text)
        for heading in re.findall(r"(?m)^#{1,6}\s+(.+?)\s*$", text):
            _add_concept(enrichment, kb_id, source, artifact, _clean_inline(heading), 2.0)
        for target in _markdown_links(text):
            ref_type = "url" if target.startswith(("http://", "https://")) else "document_link"
            _add_external_reference(
                enrichment,
                kb_id,
                source,
                artifact,
                target,
                "references",
                ref_type,
            )
        for citation in _citation_candidates(text):
            _add_external_reference(
                enrichment,
                kb_id,
                source,
                artifact,
                citation,
                "references",
                "citation",
            )
        for entity in _entity_candidates(text):
            _add_entity(enrichment, kb_id, source, artifact, entity)
        return enrichment


class SemanticSimilarityPass:
    name = "semantic_similarity"

    def run(
        self,
        artifacts: list[tuple[str, dict[str, Any]]],
        changed_ids: set[str] | None = None,
    ) -> list[EnrichmentEdge]:
        """Similarity edges between artifacts that share concept terms.

        Candidates come from an inverted term index rather than every pair.
        Two artifacts with no term in common score exactly zero and are
        dropped by the threshold below, so indexing the terms produces the
        *same* edges as the old all-pairs walk — it just skips the pairs whose
        answer was never in doubt. That matters: all-pairs is quadratic, and at
        a few thousand artifacts it was the single most expensive thing a sync
        did, re-run in full on every pass.

        ``changed_ids`` narrows the walk further, to pairs where at least one
        side was touched this sync. Untouched pairs already have their edges in
        the graph and re-deriving them is pure waste.
        """

        terms_by_id: dict[str, set[str]] = {}
        postings: dict[str, list[str]] = {}
        for node_id, attrs in artifacts:
            terms = set(attrs.get("concept_terms") or [])
            if len(terms) < 2:
                continue
            terms_by_id[node_id] = terms
            for term in terms:
                postings.setdefault(term, []).append(node_id)

        edges: list[EnrichmentEdge] = []
        seen: set[tuple[str, str]] = set()
        walk = (
            [node_id for node_id in terms_by_id if node_id in changed_ids]
            if changed_ids is not None
            else list(terms_by_id)
        )
        for left_id in walk:
            left_terms = terms_by_id[left_id]
            candidates = {
                candidate
                for term in left_terms
                for candidate in postings.get(term, ())
                if candidate != left_id
            }
            for right_id in candidates:
                pair = (left_id, right_id) if left_id < right_id else (right_id, left_id)
                if pair in seen:
                    continue
                seen.add(pair)
                right_terms = terms_by_id[right_id]
                shared = left_terms & right_terms
                union = left_terms | right_terms
                score = len(shared) / len(union)
                if len(shared) < 2 and score < 0.25:
                    continue
                attrs = {
                    "confidence": round(min(1.0, score), 3),
                    "shared_concepts": sorted(shared)[:12],
                    "enrichment_pass": self.name,
                }
                edges.append(EnrichmentEdge(pair[0], pair[1], "similar_to", attrs))
                edges.append(EnrichmentEdge(pair[1], pair[0], "similar_to", attrs))
        return edges


#: Re-exported under its long-standing name; the definition now lives in
#: `ingestion.content_types` so the four modules that need it cannot drift.
ARTIFACT_NODE_TYPES = ARTIFACT_TYPES


@dataclass(frozen=True)
class _ArtifactRef:
    node_id: str
    source_id: str
    relative_path: str


def resolve_cross_source_edges(
    nodes: list[tuple[str, dict[str, Any]]],
    edges: list[tuple[str, str, str, str | None]],
) -> list[EnrichmentEdge]:
    """Resolve references whose targets live in a *different* source.

    Synapse 21.6B cross-source pass. Deterministic and rule-based (no LLM,
    rule 1). Runs over the *whole* graph after every source's enrichment has
    been applied — references can only resolve once both the referencing and
    the target source are present, so this is a global post-pass (mirroring
    the post-hoc :class:`SemanticSimilarityPass`).

    ``nodes`` is ``(node_id, attrs)`` for every node; ``edges`` is
    ``(source, target, edge_type, reference_type)`` for the artifact ->
    external_reference edges that carry a resolvable target. For each such
    edge whose ``reference`` resolves to an artifact in a *different* source,
    an ``imports`` (python imports) or ``references`` (links) edge is emitted
    from the referencing artifact to the resolved artifact. Edges are upserted
    by the caller, so re-running is idempotent.
    """

    by_path: dict[str, list[_ArtifactRef]] = {}
    for node_id, attrs in nodes:
        if attrs.get("type") not in ARTIFACT_NODE_TYPES:
            continue
        rel = attrs.get("relative_path")
        src = attrs.get("source_id")
        if not rel or not src:
            continue
        by_path.setdefault(_norm_rel(rel), []).append(_ArtifactRef(node_id, src, rel))

    ext_nodes = {
        node_id: attrs for node_id, attrs in nodes if attrs.get("type") == "external_reference"
    }
    out: list[EnrichmentEdge] = []
    seen: set[tuple[str, str, str]] = set()
    for artifact_id, ext_id, edge_type, reference_type in edges:
        ext = ext_nodes.get(ext_id)
        if ext is None:
            continue
        source_id = ext.get("source_id")
        reference = ext.get("reference")
        if not reference or not source_id:
            continue
        targets = _resolve_reference(reference, reference_type, by_path)
        for target in targets:
            if target.node_id == artifact_id:
                continue
            # Same-source targets are resolved too. This used to `continue`
            # here on the belief that "intra-source enrichment already covers
            # it" — it does not. Per-artifact enrichment emits `imports` edges
            # to an *external_reference* node named after the module, and
            # nothing ever turned those into a link to the file that module
            # actually is. So a single-source knowledge base (the common case)
            # had ZERO file->file import edges: on a 2,132-file repository the
            # graph carried 1,871 `imports` edges and every one of them ended
            # at a name rather than at a document.
            #
            # That is the connectivity a reader wants — "_checkpoint.py
            # imports _runner.py" — and it is what the graph-facts panel and
            # the agent's graph walk had nothing better to offer than concept
            # co-occurrence. Resolution is the same deterministic longest-
            # suffix path match used across sources, so it costs one flag.
            same_source = target.source_id == source_id
            key = (artifact_id, target.node_id, edge_type)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                EnrichmentEdge(
                    artifact_id,
                    target.node_id,
                    edge_type,
                    {
                        "source_id": source_id,
                        "target_source_id": target.source_id,
                        "reference": reference,
                        "reference_type": reference_type,
                        "cross_source": not same_source,
                        "enrichment_pass": (
                            "internal_resolution" if same_source else "cross_source_resolution"
                        ),
                    },
                )
            )
    # Deterministic ordering so re-runs / snapshots are byte-stable.
    out.sort(key=lambda e: (e.source, e.target, e.type))
    return out


def _resolve_reference(
    reference: str,
    reference_type: str | None,
    by_path: dict[str, list[_ArtifactRef]],
) -> list[_ArtifactRef]:
    if reference_type == "python_import":
        return _resolve_python_import(reference, by_path)
    if reference_type in {"document_link", "url"}:
        return _resolve_document_link(reference, by_path)
    return []


def _resolve_python_import(
    module: str,
    by_path: dict[str, list[_ArtifactRef]],
) -> list[_ArtifactRef]:
    parts = [part for part in module.replace("\\", "/").split(".") if part]
    if not parts:
        return []
    base = "/".join(parts)
    candidates = (f"{base}.py", f"{base}/__init__.py")
    for candidate in candidates:
        matches = _match_suffix(candidate, by_path)
        if matches:
            return matches
    return []


def _resolve_document_link(
    target: str,
    by_path: dict[str, list[_ArtifactRef]],
) -> list[_ArtifactRef]:
    if target.startswith(("http://", "https://", "mailto:")):
        return []
    cleaned = target.split("#", 1)[0].split("?", 1)[0].strip()
    if not cleaned:
        return []
    cleaned = cleaned.lstrip("./")
    norm = _norm_rel(cleaned)
    direct = by_path.get(norm)
    if direct:
        return direct
    # Obsidian-style wiki link without extension -> try common doc suffixes.
    if "." not in Path(norm).name:
        for suffix in (".md", ".txt"):
            matches = by_path.get(norm + suffix)
            if matches:
                return matches
    return _match_suffix(norm, by_path)


def _match_suffix(
    candidate: str,
    by_path: dict[str, list[_ArtifactRef]],
) -> list[_ArtifactRef]:
    candidate = _norm_rel(candidate)
    direct = by_path.get(candidate)
    if direct:
        return direct
    matches: list[_ArtifactRef] = []
    needle = "/" + candidate
    for path, refs in by_path.items():
        if path == candidate or path.endswith(needle):
            matches.extend(refs)
    matches.sort(key=lambda ref: (ref.source_id, ref.relative_path, ref.node_id))
    return matches


def _norm_rel(value: str) -> str:
    return value.replace("\\", "/").strip("/").lower()


def artifact_text(artifact: ParsedArtifact) -> str:
    return "\n\n".join(chunk.text for chunk in artifact.chunks)


def _base_concepts(
    kb_id: str,
    source: SourceConfig,
    artifact: ParsedArtifact,
    text: str,
) -> ArtifactEnrichment:
    # Short-circuited with `_add_concept` (see its docstring for the
    # measurements). Returning early also skips `_concept_candidates`, which
    # tokenized and counted every word of every artifact on every sync — real
    # CPU spent producing rows nothing read.
    return ArtifactEnrichment()


def _add_symbol(
    enrichment: ArtifactEnrichment,
    kb_id: str,
    source: SourceConfig,
    artifact: ParsedArtifact,
    name: str,
    symbol_type: str,
    start_line: int,
    end_line: int,
) -> None:
    symbol_id = _node_id(
        "symbol",
        kb_id,
        source.name,
        artifact.relative_path,
        f"{name}-{start_line}",
    )
    attrs = {
        "source_id": source.name,
        "artifact_id": artifact.id,
        "relative_path": artifact.relative_path,
        "language": "python",
        "symbol_type": symbol_type,
        "name": name,
        "qualified_name": name,
        "start_line": start_line,
        "end_line": end_line,
        "enrichment_pass": "code",
    }
    enrichment.nodes.append(EnrichmentNode(symbol_id, "symbol", name, attrs))
    enrichment.edges.append(
        EnrichmentEdge(artifact.id, symbol_id, "mentions", {"source_id": source.name})
    )
    enrichment.edges.append(
        EnrichmentEdge(symbol_id, artifact.id, "derived_from", {"source_id": source.name})
    )
    enrichment.terms.append(_term(artifact, symbol_id, "symbol", name, 3.0))
    enrichment.symbols.append(
        {
            "id": symbol_id,
            "artifact_id": artifact.id,
            "source_id": source.name,
            "language": "python",
            "symbol_type": symbol_type,
            "name": name,
            "qualified_name": name,
            "start_line": start_line,
            "end_line": end_line,
            "signature": None,
            "docstring_summary": None,
        }
    )
    for concept in _identifier_terms(name):
        _add_concept(enrichment, kb_id, source, artifact, concept, 1.5)


def _add_call(
    enrichment: ArtifactEnrichment,
    kb_id: str,
    source: SourceConfig,
    artifact: ParsedArtifact,
    name: str,
    line: int,
) -> None:
    symbol_id = _node_id("symbol", kb_id, source.name, "call", name)
    attrs = {
        "source_id": source.name,
        "symbol_type": "call_target",
        "name": name,
        "language": "python",
        "line": line,
        "enrichment_pass": "code",
    }
    enrichment.nodes.append(EnrichmentNode(symbol_id, "symbol", name, attrs))
    enrichment.edges.append(
        EnrichmentEdge(
            artifact.id,
            symbol_id,
            "calls",
            {"source_id": source.name, "line": line},
        )
    )
    enrichment.terms.append(_term(artifact, symbol_id, "symbol", name, 1.5))


def _add_external_reference(
    enrichment: ArtifactEnrichment,
    kb_id: str,
    source: SourceConfig,
    artifact: ParsedArtifact,
    target: str,
    edge_type: str,
    reference_type: str,
) -> None:
    label = _reference_label(target)
    node_id = _node_id("external_reference", kb_id, source.name, reference_type, target)
    attrs = {
        "source_id": source.name,
        "artifact_id": artifact.id,
        "reference": target,
        "reference_type": reference_type,
        "enrichment_pass": "reference_extraction",
    }
    enrichment.nodes.append(EnrichmentNode(node_id, "external_reference", label, attrs))
    enrichment.edges.append(
        EnrichmentEdge(
            artifact.id,
            node_id,
            edge_type,
            {"source_id": source.name, "reference_type": reference_type},
        )
    )
    enrichment.terms.append(_term(artifact, node_id, "external_reference", label, 1.25))


def _add_concept(
    enrichment: ArtifactEnrichment,
    kb_id: str,
    source: SourceConfig,
    artifact: ParsedArtifact,
    concept: str,
    weight: float,
) -> None:
    """Retired. Concept extraction produced nothing any surface could use.

    Kept as a no-op rather than deleted so the call sites above still read as
    the passes they are, and so re-enabling is a one-function change if a
    corpus ever turns up where this pays off. On the corpus it was measured
    against (2,132 files, microsoft/agent-framework) it did not:

    * **Retrieval** — the concept-term expansion in ``SearchStore.search``
      only ran when FTS returned fewer than ``max_results``. Every real query
      matched hundreds to thousands of chunks, so it never fired once.
    * **Graph facts** — concepts were 87.2% of nodes and ``mentions`` +
      ``derived_from`` 98.6% of edges, so the facts panel filled all twelve
      slots with "this file mentions <term>" every time. The terms were
      "request info", "limit", "false policy" — and "request information"
      alongside "request info", which the normalizer failed to merge.
    * **Similarity** — ``_similarity_edges`` keys off ``concept_terms``, and
      the live graph contained **zero** ``similar_to`` edges. It produced
      nothing at all.

    The cost was 141,529 nodes, ~1.53M edges and 1.27M ``artifact_terms``
    rows, which is graph memory, traversal budget and sync time spent
    connecting nothing. Structure that a reader can act on — imports, calls,
    references — was 0.57% of edges and permanently crowded out.
    """
    return


def _add_entity(
    enrichment: ArtifactEnrichment,
    kb_id: str,
    source: SourceConfig,
    artifact: ParsedArtifact,
    entity: str,
) -> None:
    normalized = entity.strip()
    if not normalized or normalized.lower() in STOPWORDS:
        return
    node_id = _node_id("entity", kb_id, source.name, normalized)
    attrs = {
        "source_id": source.name,
        "artifact_id": artifact.id,
        "entity_type": "named_mention",
        "enrichment_pass": "entity_extraction",
    }
    enrichment.nodes.append(EnrichmentNode(node_id, "entity", normalized, attrs))
    enrichment.edges.append(
        EnrichmentEdge(artifact.id, node_id, "mentions", {"source_id": source.name})
    )
    enrichment.edges.append(
        EnrichmentEdge(node_id, artifact.id, "derived_from", {"source_id": source.name})
    )
    enrichment.terms.append(_term(artifact, node_id, "entity", normalized, 1.5))


def _concept_candidates(text: str, relative_path: str) -> set[str]:
    # Synapse 21.6B: normalize + singularize candidates up front so plural and
    # singular surface forms count toward the same concept (e.g. "systems" and
    # "system" both increment one bucket) and collapse to one node.
    tokens = [_normalize_concept(token) for token in _split_identifier(Path(relative_path).stem)]
    normalized_tokens = [token for token in tokens if token and token not in STOPWORDS]
    words = [_normalize_concept(match) for match in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", text)]
    words = [word for word in words if word and word not in STOPWORDS]
    counts = Counter(words)
    concepts = set(normalized_tokens)
    concepts.update(word for word, count in counts.items() if count >= 2)
    concepts.update(_keyword_bigrams(words))
    return {concept for concept in concepts if concept and concept not in STOPWORDS}


def _keyword_bigrams(words: list[str]) -> set[str]:
    concepts: set[str] = set()
    for left, right in zip(words, words[1:], strict=False):
        if left in STOPWORDS or right in STOPWORDS:
            continue
        if left == right:
            continue
        concepts.add(f"{left} {right}")
    return concepts


def _entity_candidates(text: str) -> set[str]:
    # `[ \t]+`, not `\s+`: `\s` matches newlines, so a Title Case heading fused
    # with the opening words of the next paragraph into a single "entity" whose
    # label literally contained the line breaks — e.g.
    # "Runbook\r\n\r\nThe Kestrel Gateway". That is not one entity, it is two,
    # and the junk label meant nothing could ever match the real one by name
    # (found by Step 33.7's graph bridge, which matches entities by label).
    candidates = set(re.findall(r"\b[A-Z][A-Za-z0-9]+(?:[A-Z][A-Za-z0-9]+)+\b", text))
    candidates.update(re.findall(r"\b[A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,3}\b", text))
    return candidates


# Bracketed things in Markdown that are not citations. `[x]` and `[ ]` are
# task-list checkboxes; `[Unreleased]`, `[X.Y.Z]` and bare version labels are
# changelog headings and reference-link definitions. The old pattern captured
# all of them as external references, which put "x", "provider" and
# "Unreleased" into the graph — and, once artifact facts started surfacing,
# straight into the facts panel as things a document "references".
_CITATION_RE = re.compile(r"\[@?([A-Za-z0-9_.:-]+)\]")
_CITATION_NOISE = frozenset(
    {"x", "X", "ok", "tbd", "todo", "na", "n/a", "unreleased", "yes", "no", "y", "n"}
)
_VERSION_PLACEHOLDER_RE = re.compile(r"^[vV]?[\dxXyYzZ]+([._-][\dxXyYzZ]+)*$")


def _citation_candidates(text: str) -> list[str]:
    """Bracketed citation labels worth recording, in document order.

    Deterministic and rule-based (rule 1): a fixed stoplist plus two shape
    tests, no NLP and no sampling, so the same document always yields the
    same references.
    """
    out: list[str] = []
    seen: set[str] = set()
    for match in _CITATION_RE.findall(text):
        label = match.strip()
        lowered = label.lower()
        if len(label) < 3 or lowered in _CITATION_NOISE:
            continue
        if _VERSION_PLACEHOLDER_RE.match(label):
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(label)
    return out


def _markdown_links(text: str) -> set[str]:
    links = set(re.findall(r"\[[^\]]+\]\(([^)]+)\)", text))
    links.update(re.findall(r"\[\[([^\]]+)\]\]", text))
    links.update(re.findall(r"https?://[^\s)>\]]+", text))
    return {link.strip() for link in links if link.strip()}


def _assignment_targets(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Assign):
        return [target.id for target in node.targets if isinstance(target, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _term(
    artifact: ParsedArtifact,
    node_id: str,
    node_type: str,
    value: str,
    weight: float,
) -> dict[str, Any]:
    return {
        "artifact_id": artifact.id,
        "source_id": artifact.source_id,
        "node_id": node_id,
        "node_type": node_type,
        "term": value,
        "normalized_term": _normalize_term(value),
        "weight": weight,
    }


def _identifier_terms(value: str) -> set[str]:
    terms = {_normalize_term(value)}
    parts = [_normalize_term(part) for part in _split_identifier(value)]
    parts = [part for part in parts if part and part not in STOPWORDS]
    terms.update(parts)
    if len(parts) > 1:
        terms.add(" ".join(parts))
    return {term for term in terms if term}


def _split_identifier(value: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return re.split(r"[^A-Za-z0-9]+", spaced)


def _clean_inline(value: str) -> str:
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    return value.strip(" #\t")


def _normalize_term(value: str) -> str:
    parts = [_slug_part(part) for part in _split_identifier(value)]
    return " ".join(part for part in parts if part)


def _singularize_word(word: str) -> str:
    """Lemma-light singularization of a single lowercased word.

    Deterministic, pure-python, no NLP dependency (rule 1 / "no new deps").
    Rules, applied to words length >= 4 not in ``SINGULARIZE_STOPLIST``:
    ``…ies`` -> ``…y`` (libraries -> library), ``…(s|x|z|ch|sh)es`` -> drop
    ``es`` (boxes -> box, classes is stoplisted), other ``…s`` -> drop ``s``
    (systems -> system). Words ending in ``ss`` are left untouched. The
    transform is idempotent: singularizing an already-singular word is a
    no-op, so a concept's stable id derivation stays consistent across syncs.
    """

    if len(word) < 4 or word in SINGULARIZE_STOPLIST or word.endswith("ss"):
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("es") and word[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def _normalize_concept(value: str) -> str:
    """Normalize a concept surface term before node creation (Synapse 21.6B).

    Lowercase + slug via ``_normalize_term`` (existing behavior), then
    singularize each token so "Systems"/"system"/"systems" collapse to a
    single ``concept`` node. Because ``_node_id`` derives the concept id from
    this normalized term, the id stays deterministic for a given input.
    """

    normalized = _normalize_term(value)
    if not normalized:
        return ""
    return " ".join(_singularize_word(token) for token in normalized.split(" "))


def _reference_label(value: str) -> str:
    # Cosmetic only — node identity comes from _node_id, not this label.
    # `urlparse` raises ValueError on some malformed-but-real reference text
    # (found live on mlflow's real corpus: a markdown reference containing a
    # bracketed sequence `urlsplit` mistakes for an unterminated IPv6 host
    # literal, e.g. `[...]` with no closing bracket before further `:`/`/`
    # characters) rather than returning an unparsed result the way it does
    # for other malformed input — fail open to the raw value.
    try:
        parsed = urlparse(value)
    except ValueError:
        return value
    if parsed.netloc:
        return parsed.netloc + parsed.path
    return value


def _node_id(prefix: str, *parts: str) -> str:
    node_id = prefix + ":" + ":".join(_slug_part(part) for part in parts if part)
    if len(node_id) <= MAX_ENRICHMENT_NODE_ID_LENGTH:
        return node_id
    digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:16]
    suffix = f":sha256={digest}"
    return node_id[: MAX_ENRICHMENT_NODE_ID_LENGTH - len(suffix)].rstrip(":-._") + suffix


def _slug_part(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    return value.strip("-._") or "item"
