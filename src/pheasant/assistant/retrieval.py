"""Every pheasant retrieval capability, as one typed object.

This is the toolbelt a question-answering workflow is handed. It is
deliberately **framework-agnostic** — no LangGraph, no LangChain, no LLM —
so the same surface backs the built-in single-shot workflow, the LangGraph
agent, and anything a user registers of their own. A workflow that wants a
different agent framework re-uses this rather than re-deriving how to reach
pheasant's index.

It covers the full retrieval surface, not just "search":

* ``search`` — the hybrid self-search in every mode (``text`` / ``graph`` /
  ``vector`` / ``hybrid``), the same call the MCP ``search_context`` tool
  and ``POST /search`` make.
* ``neighbors`` / ``slice`` — typed graph traversal, so a workflow can walk
  from a hit into related material that lexical search never surfaces.
* ``documents`` — cited chunks joined back up into the **files** they came
  from, in order, with line spans, headings and artifact metadata. Search
  scores chunks; questions are answered by files, and a 500-character chunk
  preview is how you get an answer that names exactly the right file and
  says nothing about it.
* ``metadata`` — the cheap half of that: what the index knows *about* a set
  of files without reading them, for the grade step deciding whether to
  search again.
* ``content`` — full indexed text for a single node.
* ``facts`` — one-hop subject–predicate–object triples off the graph.
* ``figures`` — the images cited documents show (``embeds`` edges), numbered
  for ``[fig:n]`` markers.
* ``capabilities`` — what this region can do right now *and how it is
  shaped*: sources and their types, directory layout, languages, the
  vocabulary its own documents use, the symbols its code defines. A planner
  handed a row count writes generic queries; one handed the structure writes
  queries that hit the lexical index exactly.

Every method is read-only and side-effect free.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from pheasant.graph.traversal import neighbors as _graph_neighbors
from pheasant.graph.traversal import slice_ as _graph_slice
from pheasant.ingestion.content_types import ARTIFACT_TYPES

logger = logging.getLogger(__name__)

VALID_MODES = ("hybrid", "text", "graph", "vector")

#: ``(kb, artifacts, chunks)`` → :class:`RetrievalStructure`. The corpus's
#: shape only changes when a sync does, so deriving it per question would be
#: a fixed tax on every plan step for an answer that did not move.
_STRUCTURE_CACHE: dict[tuple, Any] = {}


def _top_directories_sql(state: Any) -> str:
    """Directory roll-up in the configured database's SQL dialect."""

    dialect = getattr(state, "dialect", None)
    if bool(dialect is not None and dialect.is_postgres):
        return (
            "SELECT CASE WHEN strpos(relative_path, '/') > 0 "
            "THEN substr(relative_path, 1, strpos(relative_path, '/') - 1) "
            "ELSE '(root)' END AS dir, COUNT(*) AS n "
            "FROM artifacts WHERE relative_path IS NOT NULL "
            "GROUP BY dir ORDER BY n DESC LIMIT 12"
        )
    return (
        "SELECT CASE WHEN instr(relative_path, '/') > 0 "
        "THEN substr(relative_path, 1, instr(relative_path, '/') - 1) "
        "ELSE '(root)' END AS dir, COUNT(*) AS n "
        "FROM artifacts WHERE relative_path IS NOT NULL "
        "GROUP BY dir ORDER BY n DESC LIMIT 12"
    )


@dataclass
class Passage:
    """One retrieved piece of evidence, normalized across search modes."""

    node_id: str | None
    chunk_id: str | None
    title: str
    relative_path: str | None
    source_id: str | None
    type: str | None
    score: float
    snippet: str
    mode: str
    # The section this evidence sits in, for sources that extract a taxonomy.
    # The answering prompt already labels each block with it; carrying it here
    # is what lets a citation say *which section* answered rather than only
    # which file.
    heading_path: str | None = None
    # The *kind* of source this evidence came from (repository, notion, …),
    # carried for the same reason as heading_path: a citation that can only
    # name a file cannot say whether the claim came out of the codebase or out
    # of a Slack thread, and that is usually what decides how much to trust it.
    source_type: str | None = None
    # Set when this evidence is a remembered assertion rather than a document
    # (Step 33.6). Carried here for the same reason as heading_path: the
    # answering prompt and the citation both need to say so, and neither can
    # infer it from a path that merely happens to start with a scope directory.
    memory: dict | None = None
    raw: dict = field(default_factory=dict, repr=False)

    def key(self) -> str:
        return str(self.chunk_id or self.node_id or self.title)


# Semantic edge types a graph walk can follow. The structural three
# (contains / indexes / has_chunk) are deliberately omitted: they say "this
# file is in this folder", which is never the reason to traverse.
TRAVERSABLE_EDGES = ("mentions", "references", "imports", "calls", "similar_to")


@dataclass
class RetrievalStructure:
    """How this knowledge base is *shaped*, not merely how big it is.

    A planner told only "2,132 indexed files" writes generic queries against
    a corpus it is guessing about. A planner told the corpus is a **git
    repository** of Python under ``python/packages/``, whose own recurring
    vocabulary is "workflow", "executor", "checkpoint", writes queries that
    land on exact identifiers and real paths — which is what the lexical
    half of the index rewards.

    Every field is read straight off the index: SQL aggregates and the
    graph's maintained type counts. No LLM, no sampling, no guessing, so the
    same corpus always describes itself the same way.
    """

    sources: list[dict] = field(default_factory=list)
    content_types: list[tuple[str, int]] = field(default_factory=list)
    languages: list[tuple[str, int]] = field(default_factory=list)
    top_directories: list[tuple[str, int]] = field(default_factory=list)
    node_types: dict[str, int] = field(default_factory=dict)
    concepts: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.sources or self.content_types or self.top_directories)

    def as_lines(self) -> list[str]:
        """The structural half of the planner's context."""
        lines: list[str] = []
        if self.sources:
            lines.append("Sources:")
            for source in self.sources:
                kind = source.get("type") or "source"
                lines.append(f"  - {source['name']} ({kind}, {source['artifacts']} files)")
        if self.top_directories:
            lines.append(
                "Layout — top directories by file count: "
                + ", ".join(f"{path}/ ({count})" for path, count in self.top_directories)
            )
        if self.content_types:
            lines.append(
                "File types: "
                + ", ".join(f"{name} ({count})" for name, count in self.content_types)
            )
        if self.languages:
            lines.append(
                "Code languages: "
                + ", ".join(f"{name} ({count})" for name, count in self.languages)
            )
        if self.node_types:
            ordered = sorted(self.node_types.items(), key=lambda kv: -kv[1])
            lines.append(
                "Knowledge graph nodes: "
                + ", ".join(f"{name} ({count})" for name, count in ordered)
            )
            lines.append(f"Traversable edges: {', '.join(TRAVERSABLE_EDGES)}")
        if self.concepts:
            lines.append(
                "Recurring vocabulary in this corpus (use these words): " + ", ".join(self.concepts)
            )
        if self.symbols:
            lines.append("Prominent code symbols: " + ", ".join(self.symbols))
        return lines


@dataclass
class Document:
    """A file reassembled from its chunks, with the metadata that frames it.

    Search scores chunks; questions are answered by files. This is the
    join-back: ordered chunk text under one heading, plus what the index
    already knows about the artifact it came from.
    """

    node_id: str
    text: str
    relative_path: str | None = None
    source_id: str | None = None
    type: str | None = None
    language: str | None = None
    size_bytes: int | None = None
    git_branch: str | None = None
    chunk_count: int = 0
    included_chunks: int = 0
    line_span: tuple[int, int] | None = None
    symbols: list[str] = field(default_factory=list)
    truncated: bool = False

    def describe(self) -> str:
        """One line of provenance, for the prompt header above the text."""
        bits: list[str] = []
        if self.type:
            bits.append(str(self.type))
        if self.language and self.language != self.type:
            bits.append(str(self.language))
        if self.line_span:
            bits.append(f"lines {self.line_span[0]}-{self.line_span[1]}")
        if self.chunk_count:
            if self.truncated:
                bits.append(f"{self.included_chunks} of {self.chunk_count} chunks shown")
            else:
                bits.append(f"complete file, {self.chunk_count} chunk(s)")
        if self.source_id:
            bits.append(f"source: {self.source_id}")
        if self.git_branch:
            bits.append(f"branch: {self.git_branch}")
        line = " · ".join(bits)
        if self.symbols:
            line += "\ndefines: " + ", ".join(self.symbols)
        return line


# Extensions whose files are read whole or not at all. Source and config
# files are *structurally* meaningful: an excerpt of a Python module with the
# imports cut off, or of a YAML file with half the keys, is not a smaller
# answer — it is a misleading one, and it is what makes a model invent the
# import it cannot see. They are also small: the threshold below exists for
# the pathological vendored bundle, not for anything a human wrote.
CODE_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".java",
        ".go",
        ".rs",
        ".rb",
        ".cs",
        ".cpp",
        ".cc",
        ".c",
        ".h",
        ".hpp",
        ".swift",
        ".kt",
        ".scala",
        ".php",
        ".sh",
        ".bash",
        ".ps1",
        ".sql",
        ".r",
        ".m",
        ".lua",
        ".pl",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".ini",
        ".cfg",
        ".env",
        ".tf",
        ".dockerfile",
        ".gradle",
        ".proto",
        ".graphql",
    }
)

#: Original-file size above which a *prose* document is excerpted rather than
#: read whole. Deliberately measured against ``artifacts.size_bytes`` — what
#: the file actually is — not against the reassembled text, so the policy is a
#: property of the corpus rather than of whatever the budget happened to be.
LARGE_FILE_BYTES = 40_000

#: Chunks kept either side of a match when excerpting a large document.
ADJACENT_CHUNKS = 1


def _is_code(relative_path: str | None, language: str | None) -> bool:
    """Whether a file is source/config, and so must never be excerpted."""
    if language:  # the pipeline extracted symbols: something parsed it as code
        return True
    if not relative_path:
        return False
    suffix = relative_path[relative_path.rfind(".") :].lower() if "." in relative_path else ""
    name = relative_path.rsplit("/", 1)[-1].lower()
    return suffix in CODE_EXTENSIONS or name in ("dockerfile", "makefile")


def _reassemble(
    rows: list,
    anchor_ids: set[str],
    allowance: int,
    *,
    focused: bool = False,
) -> tuple[str, int, bool]:
    """Join a file's chunks back together, inside ``allowance`` characters.

    Returns ``(text, chunks_included, truncated)``.

    ``focused`` is the large-document policy: keep the chunks search matched
    plus their immediate neighbours and stop, **even when the allowance would
    permit more**. Filling the remaining budget with unrelated chunks of a
    400 KB document is not free — it dilutes the evidence the answer is
    supposed to be grounded in, and buries the matched region in the middle
    of the prompt. Small files and every code/config file are assembled whole
    instead (``focused=False``): there, the surrounding lines *are* the
    context.
    """
    blocks: list[str] = []
    for row in rows:
        label_bits = []
        if row["start_line"] is not None and row["end_line"] is not None:
            label_bits.append(f"lines {row['start_line']}-{row['end_line']}")
        heading = str(row["heading_path"] or "").strip()
        if heading:
            label_bits.append(heading)
        label = f"--- {' · '.join(label_bits)} ---\n" if label_bits else ""
        blocks.append(label + str(row["text"] or ""))

    anchors = [i for i, row in enumerate(rows) if str(row["id"]) in anchor_ids]

    if focused:
        # Only the matched neighbourhood, plus chunk 0 so the reader can still
        # tell which document this is (title, frontmatter, opening).
        wanted = {0}
        for anchor in anchors:
            for offset in range(-ADJACENT_CHUNKS, ADJACENT_CHUNKS + 1):
                if 0 <= anchor + offset < len(rows):
                    wanted.add(anchor + offset)
        order = sorted(wanted)
    else:
        total = sum(len(block) + 2 for block in blocks)
        if total <= allowance:
            return "\n\n".join(blocks), len(blocks), False
        # Over budget: claim positions by value rather than lopping off the
        # tail. Head first (what this file is), then what search matched (why
        # it came back), then outward from those.
        order = [0, *anchors]
        for anchor in anchors:
            for offset in (1, -1, 2, -2):
                if 0 <= anchor + offset < len(rows):
                    order.append(anchor + offset)
        order.extend(range(len(rows)))

    chosen: set[int] = set()
    spent = 0
    for index in order:
        if index in chosen:
            continue
        cost = len(blocks[index]) + 2
        if spent + cost > allowance:
            continue
        chosen.add(index)
        spent += cost
    if not chosen:  # a single chunk larger than the whole allowance
        return blocks[order[0]][:allowance], 1, True

    parts: list[str] = []
    previous = -1
    for index in sorted(chosen):
        gap = index - previous - 1
        if gap > 0 and previous >= 0:
            parts.append(f"--- … {gap} chunk(s) omitted … ---")
        parts.append(blocks[index])
        previous = index
    if previous < len(rows) - 1:
        parts.append(f"--- … {len(rows) - 1 - previous} chunk(s) omitted … ---")
    return "\n\n".join(parts), len(chosen), len(chosen) < len(rows)


@dataclass
class RetrievalCapabilities:
    """What this knowledge base can answer with, right now."""

    knowledge_base: str
    sources: list[str]
    modes: list[str]
    vector_enabled: bool
    vector_count: int
    chunk_count: int
    artifact_count: int
    node_counts: dict[str, int]
    structure: RetrievalStructure = field(default_factory=RetrievalStructure)

    def as_prompt_context(self) -> str:
        """A compact description a planner LLM can reason over."""
        lines = [
            f"Knowledge base: {self.knowledge_base}",
            f"Indexed files: {self.artifact_count}; passages: {self.chunk_count}",
        ]
        structural = self.structure.as_lines()
        if structural:
            lines.extend(structural)
        else:
            # No structure available (no state store) — fall back to the flat
            # source list rather than saying nothing about the corpus.
            lines.append(f"Sources: {', '.join(self.sources) if self.sources else '(none)'}")
        lines.append(f"Search modes available: {', '.join(self.modes)}")
        if self.vector_enabled:
            lines.append(f"Semantic (vector) index: {self.vector_count} vectors built")
        else:
            lines.append("Semantic (vector) index: not enabled — lexical + graph only")
        return "\n".join(lines)


class PheasantRetriever:
    """Read-only access to a knowledge base's full retrieval surface."""

    def __init__(
        self,
        *,
        search: Any,
        knowledge_base: str,
        graph: Any = None,
        state: Any = None,
        config: Any = None,
        memory: Any = None,
        source_types: list[str] | None = None,
        exclude_source_types: list[str] | None = None,
    ) -> None:
        self.search_engine = search
        self.knowledge_base = knowledge_base
        self.graph = graph
        self.state = state
        self.config = config
        # Step 33.10 — how memory takes part in *this* question. Held on the
        # retriever rather than passed per call: an answering loop issues many
        # searches and every one of them must see the same policy, or a chat
        # turn could half-honour a toggle the user set.
        self.memory = memory
        # Scope this answer to (or away from) kinds of source — the same
        # reasoning as `memory` above: an answering loop issues many searches
        # and every one of them has to see the same scope, or a single turn
        # could half-honour it. Applied in `search`, so every workflow and
        # every `multi_search` fan-out inherits it without threading a
        # parameter through each call signature.
        self.source_types = list(source_types) if source_types else None
        self.exclude_source_types = list(exclude_source_types) if exclude_source_types else None
        # Per-request memo: an agent loop re-issues overlapping queries, and
        # paying twice for the identical (query, mode, limit, source) tuple is
        # pure waste — pheasant's index does not change mid-answer.
        self._cache: dict[tuple, list[Passage]] = {}

    # ---------------------------------------------------------------- search

    def search(
        self,
        query: str,
        *,
        mode: str = "hybrid",
        limit: int = 8,
        source_name: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> list[Passage]:
        """Run the hybrid self-search and normalize the hits."""
        query = (query or "").strip()
        if not query:
            return []
        mode = mode if mode in VALID_MODES else "hybrid"
        cache_key = (
            query,
            mode,
            limit,
            source_name,
            principal,
            str(self.memory),
            tuple(self.source_types or ()),
            tuple(self.exclude_source_types or ()),
        )
        if cache_key in self._cache:
            return self._cache[cache_key]

        # The same over-fetch the two surfaces do, for the same reason and
        # through the same parameter. This path had none at all: it filtered
        # by source type *after* retrieval and returned whatever survived, so
        # a grounded answer built on a type-filtered corpus silently had fewer
        # passages to work from than the caller asked for — the failure mode
        # is a thinner answer, which reads as the corpus being thin.
        filtering = bool(self.source_types or self.exclude_source_types)
        resolve = getattr(self.search_engine, "ranking_parameters", None)
        fetch = resolve().overfetch(limit, filtering=filtering) if callable(resolve) else limit
        payload = self.search_engine.search_context(
            self.knowledge_base,
            query,
            mode,
            fetch,
            source_name,
            graph=self.graph,
            principal=principal,
            principal_groups=principal_groups,
            security=getattr(self.config, "security", None),
            memory=self.memory,
        )
        hits = payload.get("results", [])
        if filtering:
            from pheasant.search.criteria import apply_retrieval_criteria

            hits = apply_retrieval_criteria(
                hits,
                source_types=self.source_types,
                exclude_source_types=self.exclude_source_types,
            )[:limit]
        passages = [self._passage(item, mode) for item in hits]
        self._cache[cache_key] = passages
        return passages

    def multi_search(
        self,
        queries: list[str],
        *,
        modes: list[str] | None = None,
        limit: int = 8,
        source_name: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> list[Passage]:
        """Fan out over queries × modes and merge, de-duplicated.

        Ordering is deterministic: results keep their best score, ties break
        on first appearance, so the same plan over an unchanged index
        produces the same evidence in the same order.
        """
        modes = [m for m in (modes or ["hybrid"]) if m in VALID_MODES] or ["hybrid"]
        merged: dict[str, Passage] = {}
        # Every (query, mode) is an independent read, and the vector arm waits
        # on a remote embedding — running them one after another made the plan
        # cost the sum of its parts. Results are merged in the original
        # deterministic order below, so concurrency changes the latency and
        # nothing else.
        pairs = [(query, mode) for query in queries for mode in modes]

        def run(pair: tuple[str, str]) -> list[Passage]:
            query, mode = pair
            return self.search(
                query,
                mode=mode,
                limit=limit,
                source_name=source_name,
                principal=principal,
                principal_groups=principal_groups,
            )

        # Only the arms that actually benefit are run concurrently. Text
        # searches are SQLite reads that release the GIL and parallelize well
        # (measured 3.45s → 0.16s over four queries); vector searches are
        # numpy similarity scans that do not, and racing them made things
        # *worse* (5.10s → 8.65s). So: everything else in a pool, vector in
        # sequence.
        concurrent = [pair for pair in pairs if pair[1] != "vector"]
        sequential = [pair for pair in pairs if pair[1] == "vector"]
        batches: list[list[Passage]] = []
        if len(concurrent) > 1:
            # SQLite benefits strongly from broad read fan-out.  Postgres text
            # ranking is CPU work inside the database; sending four planner
            # queries at once made each one take minutes on a two-core local
            # container and left the stream parked on its last "plan" event.
            # Two keeps network/vector overlap without turning query latency
            # into CPU contention.
            postgres = bool(getattr(self.state, "dialect", None) and self.state.dialect.is_postgres)
            max_workers = 2 if postgres else 8
            with ThreadPoolExecutor(max_workers=min(len(concurrent), max_workers)) as pool:
                batches.extend(pool.map(run, concurrent))
        else:
            batches.extend(run(pair) for pair in concurrent)
        batches.extend(run(pair) for pair in sequential)
        for batch in batches:
            for passage in batch:
                existing = merged.get(passage.key())
                if existing is None:
                    merged[passage.key()] = passage
                    continue
                # Found more than one way — that is a relevance signal, and it
                # holds whichever mode scored higher (often they tie, since the
                # modes share one corpus). Record the union of modes, keep the
                # strongest score.
                modes_seen = _merge_modes(existing.mode, passage.mode)
                winner = passage if passage.score > existing.score else existing
                winner.mode = modes_seen
                merged[passage.key()] = winner
        # Sort by score, then by key so ties are stable across runs.
        return sorted(merged.values(), key=lambda p: (-p.score, p.key()))

    def _passage(self, item: dict, mode: str) -> Passage:
        chunks = item.get("chunks") or []
        snippet = ""
        for chunk in chunks:
            text = (chunk.get("text_preview") or "").strip()
            if text:
                snippet = text
                break
        if not snippet:
            snippet = str(item.get("summary") or item.get("label") or "").strip()
        provenance = item.get("provenance") or {}
        return Passage(
            node_id=item.get("node_id"),
            chunk_id=item.get("chunk_id"),
            title=str(
                item.get("title")
                or item.get("relative_path")
                or item.get("label")
                or item.get("node_id")
                or "untitled"
            ),
            relative_path=item.get("relative_path"),
            source_id=item.get("source_id") or provenance.get("source_id"),
            type=item.get("type"),
            score=float(item.get("score") or 0.0),
            snippet=snippet[:900],
            mode=mode,
            heading_path=(item.get("heading_path") or provenance.get("heading_path")) or None,
            source_type=(item.get("source_type") or provenance.get("source_type")) or None,
            memory=item.get("memory") or None,
            raw=item,
        )

    # ----------------------------------------------------------------- graph

    def neighbors(
        self, node_id: str, depth: int = 1, edge_types: list[str] | None = None
    ) -> list[dict]:
        """Breadth-first neighbours of a node (same shape as the MCP tool)."""
        if self.graph is None:
            return []
        return _graph_neighbors(self.graph, node_id, depth, edge_types).get("neighbors", [])

    def slice(self, node_id: str, depth: int = 1, limit: int = 40) -> dict:
        """Connected sub-graph around a node."""
        if self.graph is None:
            return {"node_id": node_id, "depth": depth, "nodes": [], "links": []}
        return _graph_slice(self.graph, node_id, depth, None, limit)

    def expand(
        self, passages: list[Passage], *, depth: int = 1, per_node: int = 4
    ) -> list[Passage]:
        """Follow the graph out of each passage into related documents.

        This is the capability a purely lexical RAG loop does not have: a
        question whose answer lives in a document that shares no vocabulary
        with the query is still reachable through a shared concept or an
        import/call edge. Returned passages carry ``mode="graph-expand"`` so
        a caller can tell derived evidence from direct hits.
        """
        if self.graph is None:
            return []
        seen = {p.node_id for p in passages if p.node_id}
        found: list[Passage] = []
        for passage in passages:
            if not passage.node_id:
                continue
            added = 0
            for entry in self.neighbors(passage.node_id, depth):
                node = entry.get("node") or {}
                node_id = str(entry.get("node_id") or "")
                if not node_id or node_id in seen:
                    continue
                # Only documents carry answerable prose; concept/chunk nodes
                # are navigation, not evidence.
                if node.get("type") not in ARTIFACT_TYPES:
                    continue
                seen.add(node_id)
                found.append(
                    Passage(
                        node_id=node_id,
                        chunk_id=None,
                        title=str(node.get("label") or node_id),
                        relative_path=node.get("relative_path"),
                        source_id=node.get("source_id"),
                        type=node.get("type"),
                        # Derived evidence ranks below any direct hit.
                        score=max(0.0, passage.score * 0.4),
                        snippet=str(node.get("summary") or "")[:600],
                        mode="graph-expand",
                        raw=node,
                    )
                )
                added += 1
                if added >= per_node:
                    break
        return found

    def figures(self, citations: list[dict], limit: int = 8) -> list[dict]:
        """Images the cited documents show, numbered against ``citations``.

        Best-effort: a graph that cannot answer degrades the answer to one
        without figures, never to an error.
        """
        from pheasant.assistant.answering import number_figures
        from pheasant.graph.figures import collect_figures, with_full_captions

        node_ids: list[str] = []
        for citation in citations:
            node_id = citation.get("node_id")
            if node_id and node_id not in node_ids:
                node_ids.append(str(node_id))
        try:
            found = with_full_captions(self.state, collect_figures(self.graph, node_ids, limit))
        except Exception:  # pragma: no cover - figures are never load-bearing
            logger.debug("could not collect figures", exc_info=True)
            return []
        return number_figures(found, citations)

    def facts(self, node_ids: list[str], limit: int = 12) -> list[dict]:
        """One-hop subject–predicate–object triples around these nodes."""
        from pheasant.assistant.chat import collect_facts

        return collect_facts(self.graph, node_ids, limit)

    # --------------------------------------------------------------- content

    def content(self, node_id: str, max_chars: int = 6000) -> str | None:
        """Full indexed text for a node, when a preview is not enough."""
        if self.state is None:
            return None
        rows = self.state.rows("SELECT text FROM chunks WHERE id=? LIMIT 1", (node_id,))
        if rows:
            return str(rows[0]["text"])[:max_chars]
        rows = self.state.rows(
            "SELECT GROUP_CONCAT(text, '\n\n') AS content FROM "
            "(SELECT text FROM chunks WHERE artifact_id=? ORDER BY chunk_index)",
            (node_id,),
        )
        content = rows[0]["content"] if rows else None
        return str(content)[:max_chars] if content else None

    def metadata(self, node_ids: list[str]) -> dict[str, dict[str, Any]]:
        """What the index knows *about* these files, without reading them.

        The cheap half of :meth:`documents` — three indexed queries and no
        chunk text — because it is wanted at a different moment. The grader is
        deciding whether to search *again*, and the shape of what came back is
        most of that decision: eight markdown notes under ``docs/`` in answer
        to "how do I call this" is a miss even when every snippet reads
        plausibly, and no amount of snippet text says so. Feeding path, type,
        language, size and the symbols each file defines into the grade step
        turns that into something it can see and name in ``next_query``.
        """
        if self.state is None or not node_ids:
            return {}
        wanted = list(dict.fromkeys(node_id for node_id in node_ids if node_id))
        if not wanted:
            return {}
        placeholders = ",".join("?" * len(wanted))
        params = tuple(wanted)
        out: dict[str, dict[str, Any]] = {}
        try:
            for row in self.state.rows(
                "SELECT id, relative_path, source_id, type, size_bytes, git_branch "
                f"FROM artifacts WHERE id IN ({placeholders})",
                params,
            ):
                out[str(row["id"])] = {
                    "relative_path": row["relative_path"],
                    "source_id": row["source_id"],
                    "type": row["type"],
                    "size_bytes": row["size_bytes"],
                    "git_branch": row["git_branch"],
                    "symbols": [],
                    "language": None,
                    "chunk_count": 0,
                    "lines": None,
                }
            for row in self.state.rows(
                "SELECT artifact_id, COUNT(*) AS n, MAX(end_line) AS last_line FROM chunks "
                f"WHERE artifact_id IN ({placeholders}) GROUP BY artifact_id",
                params,
            ):
                entry = out.get(str(row["artifact_id"]))
                if entry is not None:
                    entry["chunk_count"] = int(row["n"])
                    entry["lines"] = int(row["last_line"]) if row["last_line"] else None
            for row in self.state.rows(
                "SELECT artifact_id, name, language FROM symbols "
                f"WHERE artifact_id IN ({placeholders}) ORDER BY artifact_id, start_line",
                params,
            ):
                entry = out.get(str(row["artifact_id"]))
                if entry is None:
                    continue
                if row["language"] and not entry["language"]:
                    entry["language"] = str(row["language"])
                if row["name"] and len(entry["symbols"]) < 8:
                    entry["symbols"].append(str(row["name"]))
        except Exception:  # pragma: no cover - context is a bonus, never a blocker
            return out
        return out

    def documents(
        self,
        node_ids: list[str],
        *,
        anchors: dict[str, list[str]] | None = None,
        max_chars: int = 6000,
        code_max_chars: int = 24_000,
        large_file_bytes: int = LARGE_FILE_BYTES,
        budget_chars: int = 60_000,
    ) -> dict[str, Document]:
        """Reassemble whole files from their chunks, with metadata attached.

        Search retrieves *chunks* — that is what scoring works over — but a
        question like "what does this repository do" or "how do I use this
        tool" is answered by the **file**, not by one 500-character window
        into it. This walks back up: the chunks of each cited artifact are
        re-joined in ``chunk_index`` order, each labelled with the line span
        and heading path recorded at index time, and wrapped in the file's
        own metadata (path, type, language, size, the symbols it defines).

        Three batched queries regardless of how many documents are asked
        for — a per-node loop over ``content()`` was 2 queries *each*.

        How much of each file comes back is decided by **what the file is**,
        from ``artifacts.size_bytes`` — the original on-disk size — rather
        than by how long the reassembled text happens to run:

        * **Code and config** (``_is_code``) is never treated as large. A
          Python module with its imports cut off, or half a YAML file, is not
          a smaller answer but a wrong one, and it is exactly what makes a
          model invent the symbol it could not see. These are capped only by
          ``code_max_chars``, which is a guard against a vendored bundle, not
          a policy for anything a person wrote.
        * **Prose over ``large_file_bytes``** is excerpted to the matched
          neighbourhood: the chunks search hit, their immediate neighbours,
          and chunk 0 for orientation. Spending the rest of the budget on
          unrelated chunks of a 400 KB document dilutes the evidence instead
          of adding to it.
        * **Everything else** is assembled whole up to ``max_chars``, and if
          it still does not fit, claimed head → matched chunks → outward.

        Omitted stretches are marked inline, so the model can see it is
        reading an excerpt and say so rather than assume it saw the file.

        ``budget_chars`` caps the whole batch; documents are funded in the
        order given, which callers should keep as citation order so the
        best-scoring hit is never the one that gets starved.
        """
        if self.state is None or not node_ids:
            return {}
        wanted = list(dict.fromkeys(node_id for node_id in node_ids if node_id))
        if not wanted:
            return {}

        placeholders = ",".join("?" * len(wanted))
        params = tuple(wanted)
        meta = {
            str(row["id"]): row
            for row in self.state.rows(
                "SELECT id, relative_path, source_id, type, size_bytes, git_branch "
                f"FROM artifacts WHERE id IN ({placeholders})",
                params,
            )
        }
        chunks: dict[str, list[Any]] = {}
        for row in self.state.rows(
            "SELECT artifact_id, id, chunk_index, heading_path, start_line, end_line, text "
            f"FROM chunks WHERE artifact_id IN ({placeholders}) ORDER BY artifact_id, chunk_index",
            params,
        ):
            chunks.setdefault(str(row["artifact_id"]), []).append(row)
        symbols: dict[str, list[str]] = {}
        languages: dict[str, str] = {}
        for row in self.state.rows(
            "SELECT artifact_id, name, symbol_type, language "
            f"FROM symbols WHERE artifact_id IN ({placeholders}) ORDER BY artifact_id, start_line",
            params,
        ):
            artifact_id = str(row["artifact_id"])
            name = str(row["name"] or "").strip()
            if name and len(symbols.setdefault(artifact_id, [])) < 12:
                symbols[artifact_id].append(name)
            if row["language"] and artifact_id not in languages:
                languages[artifact_id] = str(row["language"])

        out: dict[str, Document] = {}
        spent = 0
        for node_id in wanted:
            rows = chunks.get(node_id)
            if not rows or spent >= budget_chars:
                continue
            row = meta.get(node_id)
            relative_path = str(row["relative_path"]) if row and row["relative_path"] else None
            language = languages.get(node_id)
            size_bytes = int(row["size_bytes"]) if row and row["size_bytes"] else 0

            if _is_code(relative_path, language):
                allowance, focused = min(code_max_chars, budget_chars - spent), False
            elif size_bytes > large_file_bytes:
                allowance, focused = min(max_chars, budget_chars - spent), True
            else:
                allowance, focused = min(max_chars, budget_chars - spent), False

            text, included, truncated = _reassemble(
                rows,
                set(anchors.get(node_id, []) if anchors else []),
                allowance,
                focused=focused,
            )
            if not text:
                continue
            spent += len(text)
            starts = [r["start_line"] for r in rows if r["start_line"] is not None]
            ends = [r["end_line"] for r in rows if r["end_line"] is not None]
            out[node_id] = Document(
                node_id=node_id,
                relative_path=relative_path,
                source_id=str(row["source_id"]) if row and row["source_id"] else None,
                type=str(row["type"]) if row and row["type"] else None,
                language=language,
                size_bytes=size_bytes or None,
                git_branch=str(row["git_branch"]) if row and row["git_branch"] else None,
                chunk_count=len(rows),
                included_chunks=included,
                line_span=(int(min(starts)), int(max(ends))) if starts and ends else None,
                symbols=symbols.get(node_id, []),
                text=text,
                truncated=truncated,
            )
        return out

    # ---------------------------------------------------------- capabilities

    def capabilities(self) -> RetrievalCapabilities:
        """What this knowledge base can answer with, right now."""
        modes = ["hybrid", "text"]
        if self.graph is not None:
            modes.append("graph")
        vector_enabled = getattr(self.search_engine, "vector", None) is not None
        vector_count = 0
        if vector_enabled:
            modes.append("vector")
            try:
                vector_count = int(self.search_engine.vector.store.count())
            except Exception:  # a missing/corrupt store must not break planning
                vector_count = 0

        sources: list[str] = []
        chunk_count = artifact_count = 0
        if self.state is not None:
            try:
                sources = [
                    str(row["source_id"])
                    for row in self.state.rows(
                        "SELECT DISTINCT source_id FROM artifacts ORDER BY source_id"
                    )
                ]
                chunk_count = int(self.state.rows("SELECT COUNT(*) AS n FROM chunks")[0]["n"])
                artifact_count = int(self.state.rows("SELECT COUNT(*) AS n FROM artifacts")[0]["n"])
            except Exception:
                pass

        node_counts: dict[str, int] = {}
        if self.graph is not None:
            node_counts = self.graph.type_counts()

        return RetrievalCapabilities(
            knowledge_base=self.knowledge_base,
            sources=sources,
            modes=modes,
            vector_enabled=vector_enabled,
            vector_count=vector_count,
            chunk_count=chunk_count,
            artifact_count=artifact_count,
            node_counts=node_counts,
            structure=self.structure(artifact_count, chunk_count, node_counts),
        )

    def structure(
        self,
        artifact_count: int = 0,
        chunk_count: int = 0,
        node_counts: dict[str, int] | None = None,
    ) -> RetrievalStructure:
        """The corpus's own shape and vocabulary, for grounding a plan.

        Cached process-wide against ``(artifacts, chunks)`` — the same
        signature trick the vector store uses. The aggregates are cheap per
        row but the concept roll-up touches every ``artifact_terms`` row of
        its type, and re-deriving that on every question would put a fixed
        cost on the plan step for a fact that only changes when a sync does.
        """
        if self.state is None:
            return RetrievalStructure(node_types=dict(node_counts or {}))
        signature = (self.knowledge_base, artifact_count, chunk_count)
        cached = _STRUCTURE_CACHE.get(signature)
        if cached is not None:
            cached.node_types = dict(node_counts or {})
            return cached

        from pheasant.search.sqlite_store import corpus_vocabulary as _corpus_vocabulary

        def aggregate(sql: str, params: tuple = ()) -> list[tuple[str, int]]:
            try:
                return [
                    (str(row[0]), int(row[1]))
                    for row in self.state.rows(sql, params)
                    if row[0] not in (None, "")
                ]
            except Exception:  # a structural nicety must never fail a question
                return []

        configured_types = {}
        for source in getattr(self.config, "sources", None) or []:
            source_type = getattr(source, "type", None)
            configured_types[str(getattr(source, "name", ""))] = str(
                getattr(source_type, "value", source_type) or ""
            )
        sources = [
            {"name": name, "type": configured_types.get(name) or "source", "artifacts": count}
            for name, count in aggregate(
                "SELECT source_id, COUNT(*) FROM artifacts GROUP BY source_id ORDER BY 2 DESC"
            )
        ]
        structure = RetrievalStructure(
            sources=sources,
            content_types=aggregate(
                "SELECT type, COUNT(*) FROM artifacts GROUP BY type ORDER BY 2 DESC LIMIT 10"
            ),
            languages=aggregate(
                "SELECT language, COUNT(*) FROM symbols WHERE language IS NOT NULL "
                "GROUP BY language ORDER BY 2 DESC LIMIT 6"
            ),
            top_directories=aggregate(_top_directories_sql(self.state)),
            node_types=dict(node_counts or {}),
            # The terms this corpus actually uses, by document frequency,
            # read off the FTS index's own vocabulary table. Previously this
            # came from `artifact_terms` concept rows; those were retired
            # (graph.enrichment._add_concept) and this is the same question
            # answered from the index SQLite already maintains.
            concepts=[term for term, _ in _corpus_vocabulary(self.state, 24)],
            symbols=[
                label
                for label, _ in aggregate(
                    "SELECT name, COUNT(DISTINCT artifact_id) AS n FROM symbols "
                    "WHERE name IS NOT NULL AND symbol_type IN ('class', 'function', 'method') "
                    "GROUP BY name ORDER BY n DESC, name LIMIT 20"
                )
            ],
        )
        _STRUCTURE_CACHE.clear()  # one knowledge base per process; keep it bounded
        _STRUCTURE_CACHE[signature] = structure
        return structure


def _merge_modes(*modes: str) -> str:
    """Union of retrieval modes, de-duplicated and canonically ordered.

    Sorted rather than discovery-ordered on purpose: this string is a label
    ("retrieved by hybrid+vector"), and two passages found by the same pair
    of modes must read the same way regardless of which query hit first.
    """
    seen = {mode for group in modes for mode in str(group).split("+") if mode}
    return "+".join(sorted(seen))
