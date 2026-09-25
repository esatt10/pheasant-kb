from __future__ import annotations

import hashlib
import logging
from typing import Any

from pheasant.config.schema import PheasantConfig, SourceConfig
from pheasant.graph.enrichment import (
    ArtifactEnrichment,
    CodeEnrichmentPass,
    MarkdownDocumentEnrichmentPass,
    resolve_cross_source_edges,
)
from pheasant.graph.simple import SimpleMultiDiGraph
from pheasant.ingestion.content_types import ARTIFACT_TYPES
from pheasant.ingestion.pipeline import ParsedArtifact, utc_now
from pheasant.sync.pacing import serve_yield

logger = logging.getLogger(__name__)


def _same_content(existing: dict, candidate: dict) -> bool:
    """Do these two versions of a node or edge differ only in when they were seen?

    ``updated_at`` is excluded on both sides, and so is nothing else: any
    other difference is a real change and must move the stamp, or the graph
    generation would stop tracking the graph.
    """

    return {key: value for key, value in existing.items() if key != "updated_at"} == {
        key: value for key, value in candidate.items() if key != "updated_at"
    }


class GraphBuilder:
    def __init__(self, config: PheasantConfig):
        self.config = config
        self.graph = SimpleMultiDiGraph()
        self.kb_id = config.knowledge_base_id
        self.upsert_node(self.kb_id, "knowledge_base", self.kb_id, {})
        self.enrichment_passes = [
            CodeEnrichmentPass(),
            MarkdownDocumentEnrichmentPass(),
        ]

    def upsert_node(self, node_id: str, node_type: str, label: str, attrs: dict) -> None:
        """Write a node, moving ``updated_at`` only when something changed.

        The stamp used to move on every upsert, which made it a record of when
        a sync last *ran* rather than of when the node last changed — and that
        turned the published graph generation into something that changed on
        every re-sync of an unchanged corpus. The generation id is
        content-addressed precisely so it does not (pillar 1, and
        ``generation_id``'s own docstring), so a timestamp nothing reads was
        quietly the one input making it move: every replica reloaded a graph
        identical to the one it held, on every scheduler beat.

        Held on both backends and predated both: the whole-file store digests
        bytes that contain this stamp, and the row store digests a row that
        contains it. `tests/test_graph_backends.py` is the first thing to
        assert the property directly, which is how it surfaced.
        """

        now = utc_now()
        existing = dict(self.graph.nodes[node_id]) if self.graph.has_node(node_id) else None
        merged = {
            "id": node_id,
            "type": node_type,
            "label": label,
            "created_at": (existing or {}).get("created_at", now),
            "knowledge_base_id": self.kb_id,
            **attrs,
        }
        # Compared against what `add_node` will actually store, which *merges*
        # over the existing attributes rather than replacing them. Comparing
        # against the payload alone would call every node changed as soon as
        # one call site attached an attribute another does not — a false
        # "changed", which moves the stamp for nothing.
        if existing is not None and _same_content(existing, {**existing, **merged}):
            merged["updated_at"] = existing.get("updated_at", now)
        else:
            merged["updated_at"] = now
        self.graph.add_node(node_id, **merged)

    def upsert_edge(
        self,
        source: str,
        target: str,
        edge_type: str,
        attrs: dict | None = None,
    ) -> None:
        attrs = attrs or {}
        for _key, data in self.graph.get_edge_data(source, target, default={}).items():
            if data.get("type") == edge_type:
                before = dict(data)
                data.update(attrs)
                # Same rule as `upsert_node`: an edge re-asserted unchanged has
                # not been updated, and stamping it would move the published
                # generation id for a graph nobody touched.
                if _same_content(before, data):
                    data["updated_at"] = before.get("updated_at", before.get("created_at"))
                else:
                    data["updated_at"] = utc_now()
                    self.graph.touch_edge(source, target)
                return
        self.graph.add_edge(
            source,
            target,
            type=edge_type,
            created_at=utc_now(),
            confidence=attrs.pop("confidence", 1.0),
            **attrs,
        )

    def add_source(self, source: SourceConfig) -> str:
        node_id = f"source:{self.kb_id}:{source.name}"
        source_type = source.type.value
        self.upsert_node(
            node_id,
            "source",
            source.name,
            {
                "source_id": source.name,
                "source_type": source_type,
                "path": str(source.path),
            },
        )
        self.upsert_edge(self.kb_id, node_id, "contains", {"source_id": source.name})
        self.add_source_type(source_type, node_id, source.name)
        return node_id

    def add_source_type(self, source_type: str, source_node: str, source_id: str) -> str:
        """A hub node grouping every source of one kind.

        The type was already an *attribute* of each source node, which meant it
        could be read but never navigated: nothing in the graph connected the
        two Confluence spaces to each other, so "show me everything that came
        out of a wiki" was a question the picture could not answer. A hub makes
        that a visible structure — one node, one hop from each source of that
        kind — which is what the graph is for.

        Hung off the knowledge base **alongside** sources rather than between
        them: `kb contains source` stays exactly as it was, so a source is
        still one hop from the root and nothing already on screen at the
        default depth gets pushed past the horizon. The hub is a sibling, not
        a new tier.

        `contains` rather than a new edge type, because it is the same
        structural grouping the graph already uses for kb→source,
        directory→file and section→subsection — and because the assistant's
        semantic walk deliberately skips the structural edges, which is right:
        a type hub carries no content to retrieve.
        """

        node_id = f"source_type:{self.kb_id}:{source_type}"
        self.upsert_node(
            node_id,
            "source_type",
            source_type,
            {"source_type": source_type},
        )
        self.upsert_edge(self.kb_id, node_id, "contains", {"source_type": source_type})
        self.upsert_edge(node_id, source_node, "contains", {"source_id": source_id})
        return node_id

    def add_directory_chain(
        self, source: SourceConfig, relative_path: str, branch: str | None
    ) -> str:
        source_node = self.add_source(source)
        parent = source_node
        parts = [part for part in relative_path.replace("\\", "/").split("/")[:-1] if part]
        prefix: list[str] = []
        for depth, part in enumerate(parts, start=1):
            prefix.append(part)
            relative = "/".join(prefix)
            node_id = f"directory:{source.name}:{relative}:branch={branch or 'none'}"
            self.upsert_node(
                node_id,
                "directory",
                part,
                {
                    "source_id": source.name,
                    "relative_path": relative,
                    "path": str(source.path / relative),
                    "depth": depth,
                    "provenance": {
                        "path": str(source.path / relative),
                        "relative_path": relative,
                        "git_branch": branch,
                    },
                },
            )
            self.upsert_edge(parent, node_id, "contains", {"source_id": source.name})
            parent = node_id
        return parent

    def add_artifact(
        self,
        source: SourceConfig,
        artifact: ParsedArtifact,
    ) -> ArtifactEnrichment:
        source_node = self.add_source(source)
        parent_node = self.add_directory_chain(source, artifact.relative_path, artifact.git_branch)
        enrichment = self.enrich_artifact(source, artifact)
        self.upsert_node(
            artifact.id,
            artifact.type,
            artifact.relative_path,
            {
                "source_id": source.name,
                "hash": f"sha256:{artifact.sha256}",
                "path": str(artifact.path),
                "relative_path": artifact.relative_path,
                "size_bytes": artifact.size_bytes,
                "git_branch": artifact.git_branch,
                "git_commit": artifact.git_commit,
                "concept_terms": sorted(enrichment.concept_terms),
                "provenance": {
                    "path": str(artifact.path),
                    "relative_path": artifact.relative_path,
                    "git_branch": artifact.git_branch,
                    "git_commit": artifact.git_commit,
                },
            },
        )
        self.upsert_edge(source_node, artifact.id, "indexes", {"source_id": source.name})
        self.upsert_edge(parent_node, artifact.id, "contains", {"source_id": source.name})
        for chunk in artifact.chunks:
            chunk_id = (
                f"chunk:{source.name}:{artifact.relative_path}:"
                f"sha256={chunk.text_hash}:chunk={chunk.index:04d}"
            )
            self.upsert_node(
                chunk_id,
                "chunk",
                f"{artifact.relative_path}#{chunk.index}",
                {
                    "source_id": source.name,
                    "artifact_id": artifact.id,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "text_hash": chunk.text_hash,
                    "summary": chunk.text[:180],
                    "token_estimate": chunk.token_estimate,
                },
            )
            self.upsert_edge(artifact.id, chunk_id, "has_chunk", {"source_id": source.name})
        self.add_headings(source, artifact)
        self.apply_enrichment(enrichment)
        return enrichment

    def add_memory_edges(self, state: Any, max_targets: int | None = None) -> dict[str, Any]:
        """Wire memory records into the graph (Step 33.7). Returns a report.

        Three kinds of edge:

        * ``supersedes`` — a documented edge type nothing emitted, so a
          correction existed only as a frontmatter string and the graph could
          not answer "what replaced this".
        * ``subsumes`` (Phase 3) — a near-duplicate cluster's medoid to the
          records it absorbed. Deliberately drawn straight from
          ``subsumed_by``, never through the ``about`` ladder below: that
          ladder is corpus-only by design (a memory must not be matched
          against symbols/headings/entities extracted from *another
          memory*), and a subsumption is memory-to-memory by definition.
        * ``about`` — the record to what it is *about*, by the strongest signal
          that fires (see :mod:`pheasant.memory.bridge`).

        A no-op returning zeros when the region has no memory records, so a
        graph without agent memory is byte-identical to before. Idempotent via
        ``upsert_edge``: re-running over unchanged content re-derives the same
        edges and changes nothing.
        """
        from pheasant.memory.bridge import (
            ABOUT_EDGE,
            DEFAULT_MAX_TARGETS,
            SUBSUMES_EDGE,
            resolve_bridges,
            subsumes_edges,
            supersedes_edges,
        )

        report = {
            "records": 0,
            "about": 0,
            "supersedes": 0,
            "subsumes": 0,
            "unbridged": [],
            "by_signal": {},
        }
        records = self._memory_rows(state)
        if not records:
            return report
        report["records"] = len(records)

        for newer, older in supersedes_edges(records):
            self.upsert_edge(newer, older, "supersedes", {"enrichment_pass": "memory_bridge"})
            report["supersedes"] += 1

        for canonical, member in subsumes_edges(records):
            self.upsert_edge(canonical, member, SUBSUMES_EDGE, {"enrichment_pass": "memory_bridge"})
            report["subsumes"] += 1

        inputs = self._bridge_inputs(state, records)
        edges, unbridged = resolve_bridges(
            inputs, max_targets if max_targets is not None else DEFAULT_MAX_TARGETS
        )
        for edge in edges:
            self.upsert_edge(
                edge.artifact_id,
                edge.target_id,
                ABOUT_EDGE,
                {
                    "record_id": edge.record_id,
                    "match_signal": edge.signal,
                    "matched": edge.matched,
                    "confidence": edge.confidence,
                    "enrichment_pass": "memory_bridge",
                },
            )
            report["by_signal"][edge.signal] = report["by_signal"].get(edge.signal, 0) + 1
        report["about"] = len(edges)
        # Reported, not silent: a corpus where no rung fires is a real outcome
        # an operator should be able to see, not a feature that quietly does
        # nothing. Surfaced through describe_retrieval's memory block.
        report["unbridged"] = unbridged
        return report

    @staticmethod
    def _memory_rows(state: Any) -> list[dict[str, Any]]:
        try:
            rows = state.rows(
                "SELECT record_id, artifact_id, source_id, supersedes, subsumed_by "
                "FROM memory_records"
            )
        except Exception:
            # Either a state store older than 33.5 (no memory_records at
            # all) or one that predates Phase 3's subsumed_by column —
            # retry without it rather than losing supersedes/about bridging
            # entirely over one missing column.
            try:
                rows = state.rows(
                    "SELECT record_id, artifact_id, source_id, supersedes FROM memory_records"
                )
            except Exception:  # pragma: no cover - state store older than 33.5
                return []
        return [dict(row) for row in rows]

    def _bridge_inputs(self, state: Any, records: list[dict[str, Any]]) -> Any:
        """Read the ladder's inputs out of SQLite and the graph.

        Corpus-only on purpose: a memory must not be matched against symbols,
        headings or entities extracted from *another memory*, or a store of
        related notes would knit itself together and call that grounding.
        """
        from pheasant.memory.bridge import BridgeInputs, normalize_label

        memory_sources = {str(row.get("source_id") or "") for row in records}
        memory_artifacts = {str(row.get("artifact_id") or "") for row in records}

        texts: dict[str, str] = {}
        placeholders = ",".join("?" for _ in memory_sources)
        for row in state.rows(
            "SELECT artifact_id, GROUP_CONCAT(text, ' ') AS body FROM chunks "
            f"WHERE source_id IN ({placeholders}) GROUP BY artifact_id",
            tuple(memory_sources),
        ):
            texts[str(row["artifact_id"])] = str(row["body"] or "")

        symbols: dict[str, list[str]] = {}
        for row in state.rows(
            "SELECT name, qualified_name, artifact_id FROM symbols "
            f"WHERE source_id NOT IN ({placeholders})",
            tuple(memory_sources),
        ):
            for value in (row["name"], row["qualified_name"]):
                if value:
                    symbols.setdefault(str(value).lower(), []).append(str(row["artifact_id"]))

        headings: dict[str, list[str]] = {}
        entities: dict[str, list[str]] = {}
        referenced: dict[str, list[str]] = {}
        # Targets whose type rung 1 will actually ask about, resolved after the
        # edge walk rather than during the node walk. The first version built
        # `node_types` for **every node in the graph** to answer that question
        # about the handful of targets a memory record's own edges reach — a
        # whole-graph dict for a bounded lookup, and one of the structures
        # keeping the indexer resident.
        candidate_targets: dict[str, list[str]] = {}
        with self.graph.reading():
            for node_id, attrs in self.graph.iter_nodes():
                node_type = attrs.get("type")
                if attrs.get("source_id") in memory_sources:
                    continue
                if node_type == "heading":
                    key = normalize_label(attrs.get("title") or attrs.get("label") or "")
                    if key:
                        headings.setdefault(key, []).append(node_id)
                elif node_type == "entity":
                    key = normalize_label(attrs.get("label") or "")
                    if key:
                        entities.setdefault(key, []).append(node_id)
            for (source, target), edge_map in self.graph.iter_edges():
                # Rung 1: an explicit link the cross-source pass already
                # resolved to a real artifact. One pair can carry several edge
                # types, so the map is what has to be inspected.
                if source not in memory_artifacts:
                    continue
                for data in edge_map.values():
                    if data.get("type") not in {"references", "imports"}:
                        continue
                    # Only the *resolved* artifact, not the `external_reference`
                    # stub the link itself produced. Both edges exist and both
                    # are `references`, so taking either spent half the
                    # per-record cap pointing at a node that stands for the
                    # link rather than for what the record is about — visible
                    # on a live run, where one record drew two edges and only
                    # one of them reached a file.
                    candidate_targets.setdefault(source, []).append(target)
                    break
            # One pass over the candidates, not over the graph. `node_map()`
            # is the live mapping under the lock already held, so this is the
            # same lookup the old dict served — without materializing it.
            nodes = self.graph.node_map()
            for source, targets in candidate_targets.items():
                for target in targets:
                    if (nodes.get(target) or {}).get("type") in ARTIFACT_TYPES:
                        referenced.setdefault(source, []).append(target)

        return BridgeInputs(
            records=records,
            texts=texts,
            symbols={key: sorted(set(value)) for key, value in symbols.items()},
            headings={key: sorted(set(value)) for key, value in headings.items()},
            entities={key: sorted(set(value)) for key, value in entities.items()},
            referenced={key: sorted(set(value)) for key, value in referenced.items()},
        )

    def add_headings(self, source: SourceConfig, artifact: ParsedArtifact) -> int:
        """Emit the artifact's structural outline as `heading` nodes.

        `heading` and `has_heading` are both **documented** in
        `docs/graph_model.md` ("Retrieval and document structure units" /
        "Hierarchy and indexing relationships") and were never emitted
        anywhere — this connects them. Nothing happens unless the source
        enables `taxonomy` (and `taxonomy.graph_nodes`), so a graph without
        the feature is byte-identical to before.

        Two edge types, both existing: the artifact `has_heading` each of its
        sections, and a section `contains` its subsections — the same edge the
        directory/file hierarchy uses, so the taxonomy is walkable by the
        graph traversal that already knows how to follow `contains` (see
        `graph/traversal.py:HIERARCHY_EDGE_TYPES`).
        """
        headings = getattr(artifact, "headings", None)
        if not headings:
            return 0
        if not getattr(getattr(source, "taxonomy", None), "graph_nodes", True):
            return 0

        # Stack of (level, node_id) so a subsection is parented to the section
        # that encloses it rather than to the artifact.
        stack: list[tuple[int, str]] = []
        emitted = 0
        for heading in headings:
            # Keyed on the section's *breadcrumb*, hashed — deliberately not on
            # its line number. A line-numbered ID churns the whole graph on any
            # edit: inserting one paragraph shifts every heading below it, so
            # every section downstream would be dropped and re-created despite
            # nothing about it changing. The breadcrumb is the section's
            # identity ("Article IV > 4.2 Termination" is that section wherever
            # it sits), and hashing keeps the ID bounded when a deep path runs
            # to hundreds of characters. Matches the `chunk:` ID's existing use
            # of a content hash in the context field.
            #
            # Trade-off, accepted: two sections with a byte-identical breadcrumb
            # in one document collapse to one node. That needs a document to
            # repeat the same caption under the same parent, and when it happens
            # the two really are the same section by name — whereas edit churn
            # would be constant.
            digest = hashlib.sha256(heading.path.encode("utf-8")).hexdigest()[:16]
            heading_id = f"heading:{source.name}:{artifact.relative_path}:sha256={digest}"
            self.upsert_node(
                heading_id,
                "heading",
                heading.label,
                {
                    "source_id": source.name,
                    "artifact_id": artifact.id,
                    "level": heading.level,
                    "pattern_level": heading.pattern_level,
                    "number": heading.number,
                    "title": heading.title,
                    "kind": heading.kind,
                    "heading_path": heading.path,
                    "start_line": heading.line,
                    "relative_path": artifact.relative_path,
                    # The parsed ordinal, persisted so a reader can query by
                    # citation ("§ 12.3" -> parts [12, 3]) and so the taxonomy
                    # endpoint can re-derive sequence issues without reparsing
                    # the document.
                    "ordinal_parts": list(heading.ordinal.parts) if heading.ordinal else [],
                    "ordinal_series": heading.ordinal.series if heading.ordinal else None,
                    "ordinal_suffix": heading.ordinal.suffix if heading.ordinal else "",
                    "ordinal_relative": bool(heading.ordinal.relative)
                    if heading.ordinal
                    else False,
                },
            )
            while stack and stack[-1][0] >= heading.level:
                stack.pop()
            parent = stack[-1][1] if stack else None
            if parent is None:
                self.upsert_edge(artifact.id, heading_id, "has_heading", {"source_id": source.name})
            else:
                self.upsert_edge(parent, heading_id, "contains", {"source_id": source.name})
                # Also link the artifact to every section, not just the roots,
                # so "which sections does this document have?" is one hop
                # rather than a full descent.
                self.upsert_edge(artifact.id, heading_id, "has_heading", {"source_id": source.name})
            stack.append((heading.level, heading_id))
            emitted += 1
        return emitted

    def enrich_artifact(
        self,
        source: SourceConfig,
        artifact: ParsedArtifact,
    ) -> ArtifactEnrichment:
        enrichment = ArtifactEnrichment()
        for enrichment_pass in self.enrichment_passes:
            enrichment.extend(enrichment_pass.run(self.kb_id, source, artifact))
        return enrichment

    def apply_enrichment(self, enrichment: ArtifactEnrichment) -> None:
        for node in enrichment.nodes:
            self.upsert_node(node.id, node.type, node.label, node.attrs)
        for edge in enrichment.edges:
            self.upsert_edge(edge.source, edge.target, edge.type, edge.attrs)

    def add_similarity_edges(
        self,
        source_name: str | None = None,
        changed_ids: set[str] | None = None,
    ) -> None:
        """Retired. Kept as a no-op because callers still name it.

        It linked artifacts sharing ``concept_terms`` — and concept extraction
        was retired (``enrichment._add_concept``), so ``_base_concepts``
        returns an empty enrichment and no node has carried a ``concept_term``
        since. The pass therefore built an inverted index over an empty term
        set and emitted nothing: measured on a real sync, **zero** nodes with
        `concept_terms` and **zero** `similar_to` edges, which is the same
        "the live graph contained zero similar_to edges" the concept
        retirement already recorded as its third justification.

        What it still cost was a walk over every node in the graph **plus a
        `dict(attrs)` copy of every artifact node**, on every sync, to produce
        that nothing. A no-op rather than a deletion because ``sync/engine.py``
        calls it and the signature is the seam a future similarity pass would
        re-use; deleting the call sites too would make re-introducing one a
        larger diff than it needs to be.
        """

        return

    def add_cross_source_edges(self) -> int:
        """Resolve references whose targets resolve into a *different* source.

        Synapse 21.6B. A global post-pass over the whole graph (sources sync
        independently, so a reference can only resolve once both the
        referencing and the target source are indexed). Python imports resolve
        ``imports`` edges; markdown/document links resolve ``references`` edges.
        Edges are upserted, so re-running is idempotent and deterministic.
        Returns the number of cross-source edges resolved this pass.
        """

        ref_edges: list[tuple[str, str, str, str | None]] = []
        # One lock hold, no copying: this walks every edge in the graph, so
        # snapshotting them first (1.5M dict copies on a real index) cost more
        # than the pass itself.
        #
        # The *nodes* are narrowed to the two kinds the resolver reads, which
        # is what both `resolve_cross_source_edges` and its WASM twin already
        # filter to on arrival: artifacts carrying a path, and the
        # `external_reference` stubs a link produces. It used to hand over
        # `dict(attrs)` for **every node in the graph** — measured 2.26s and
        # roughly double peak memory at 100k files, to build a list whose
        # first act was to throw three quarters of it away. Chunks are 55% of
        # a real graph and symbols 20%; neither is looked at here.
        nodes: list[tuple[str, dict[str, Any]]] = []
        with self.graph.reading():
            for node_id, attrs in self.graph.iter_nodes():
                node_type = attrs.get("type")
                if node_type == "external_reference" or node_type in ARTIFACT_TYPES:
                    nodes.append((node_id, dict(attrs)))
            node_map = self.graph.node_map()
            for (source, target), edge_map in self.graph.iter_edges():
                target_attrs = node_map.get(target)
                if not target_attrs or target_attrs.get("type") != "external_reference":
                    continue
                for data in edge_map.values():
                    edge_type = data.get("type")
                    if edge_type not in {"imports", "references"}:
                        continue
                    ref_edges.append((source, target, edge_type, data.get("reference_type")))
        resolved = self._resolve_cross_source_edges(nodes, ref_edges)
        for index, edge in enumerate(resolved):
            self.upsert_edge(edge.source, edge.target, edge.type, dict(edge.attrs))
            if index % 500 == 499:
                serve_yield()
        return len(resolved)

    def _resolve_cross_source_edges(
        self,
        nodes: list[tuple[str, dict[str, Any]]],
        ref_edges: list[tuple[str, str, str, str | None]],
    ) -> list[Any]:
        """Synapse Step 34.5a: optional WASM acceleration, pure-Python default.

        Opt-in via ``graph.wasm_cross_source_resolution`` (default off — see
        the 34.4 benchmark spike for why this one is conditional rather than
        a clear win). Any failure — the ``[wasm]`` extra missing, a sandbox
        error, anything — falls back to the pure-Python function rather than
        failing the sync; acceleration is a performance path, never a
        correctness dependency.
        """
        if not self.config.graph.wasm_cross_source_resolution:
            return resolve_cross_source_edges(nodes, ref_edges)
        try:
            from pheasant.sandbox.accel import resolve_cross_source_edges_wasm

            return resolve_cross_source_edges_wasm(nodes, ref_edges)
        except Exception:
            logger.warning(
                "WASM cross-source resolution failed; falling back to pure Python", exc_info=True
            )
            return resolve_cross_source_edges(nodes, ref_edges)

    def remove_source_content(self, source_name: str) -> None:
        """Drop everything one source contributed.

        Iterated under ``reading()`` rather than through ``nodes(data=True)``,
        which materializes ``(node_id, attrs)`` for the **whole graph** before
        the filter looks at the first one — measured **1.16s** at 100k files,
        an order of magnitude more than the removal it was preparing for. The
        live mapping is safe here because the lock is held for the walk and the
        result is a plain list of ids.
        """

        source_node = f"source:{self.kb_id}:{source_name}"
        with self.graph.reading():
            nodes = [
                node_id
                for node_id, attrs in self.graph.iter_nodes()
                if attrs.get("source_id") == source_name or node_id == source_node
            ]
        self.graph.remove_nodes_from(nodes)
        self.prune_orphan_source_type_hubs()

    def prune_orphan_source_type_hubs(self) -> int:
        """Remove type hubs no remaining source reaches.

        Source-type nodes are shared structural state, so they cannot carry an
        individual ``source_id``. Pruning them from the surviving source nodes
        prevents the last removal of a type — and an old interrupted removal —
        from leaving a misleading graph fragment behind.
        """

        with self.graph.reading():
            active_types: set[str] = set()
            hubs: list[tuple[str, str | None]] = []
            for node_id, attrs in self.graph.iter_nodes():
                node_type = attrs.get("type")
                source_type = attrs.get("source_type")
                if node_type == "source" and source_type:
                    active_types.add(str(source_type))
                elif node_type == "source_type":
                    hubs.append((node_id, str(source_type) if source_type else None))
            orphans = [node_id for node_id, source_type in hubs if source_type not in active_types]
        self.graph.remove_nodes_from(orphans)
        return len(orphans)

    def remove_artifact_nodes(self, artifact_ids: list[str]) -> None:
        """Remove specific artifacts' nodes (and anything derived from them)
        without touching the rest of the source (Phase 0).

        An artifact's own node id *is* its artifact id (`add_artifact`
        upserts on `artifact.id` directly); a few derived node types
        (heading, entity) instead carry it as an `artifact_id` attribute.
        Either match removes the node and, via `remove_nodes_from`, every
        edge incident to it — the same shape `remove_source_content` uses,
        narrowed from a whole source to a specific id set so a small batch
        of archived memory records does not force a whole-source rebuild.
        """
        ids = set(artifact_ids)
        if not ids:
            return
        # Same reasoning as `remove_source_content`: under the lock, not
        # through a whole-graph snapshot. This one is called from memory
        # maintenance for a handful of archived records, so the snapshot was
        # the entire cost of the call.
        with self.graph.reading():
            nodes = [
                node_id
                for node_id, attrs in self.graph.iter_nodes()
                if node_id in ids or attrs.get("artifact_id") in ids
            ]
        self.graph.remove_nodes_from(nodes)


def _batches(items: list[str], size: int):
    """Chunk ids so an IN (...) clause stays inside SQLite's parameter limit."""

    for start in range(0, len(items), size):
        yield items[start : start + size]
