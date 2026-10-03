"""Open Knowledge Format bundles: which directories are bundles, and their graph.

``ingestion.okf`` reads one file at a time and records what it says about
itself on its artifact node. Whether a *directory* is an OKF bundle is a
property of every file in it — a single ``type:`` frontmatter key is also how
Hugo routes a page — so it is decided here, once a source's artifacts are all
in the graph, and only then do the bundle's relationships become edges.

Registration does not change. A folder, repository, Obsidian vault or
connector source that happens to hold a bundle is detected; one that does not
produces no node and no edge, so its graph is exactly what it was.

**Detection** (all of it deterministic, OKF v0.2 §3, §8, §11, §12):

1. ``explicit`` — a directory whose ``index.md`` declares ``okf_version``.
2. ``index`` — a directory whose ``index.md`` is an OKF listing (no
   frontmatter, §8) and whose subtree is conformant (below).
3. ``conformant_tree`` — failing both, the shallowest directory whose subtree
   is conformant *and* corroborated by a key only an OKF producer writes
   (``sources``, ``generated``, ``verified``, …) or by an OKF ``log.md``.

A subtree is *conformant* when it holds at least ``MIN_CONCEPTS`` concepts
and at least ``MIN_CONCEPT_SHARE`` of its Markdown is concepts — not all of
it, because a bundle shipped as a git repository usually carries a README
the spec never asked for, and the spec tells consumers to treat everything
past the required ``type`` as soft guidance. Repository boilerplate
(``README.md``, ``CHANGELOG.md`` …) is not counted either way. The shallowest
qualifying directory wins and nothing nested under a bundle is a second
bundle, so a sub-``index.md`` is a listing inside its bundle rather than a
bundle of its own.

**Relationships.** The spec has one relationship — an untyped Markdown link —
plus the path-valued frontmatter fields. Each becomes the edge type the graph
already uses for that meaning where one exists (``links_to``, ``derived_from``,
``tagged_with``, ``contains``, ``supersedes``) and a new one only where none
does (``executed_by``, ``attested_by``, ``computed_by``). Everything this
module emits carries ``enrichment_pass: "okf"``, which is how
:func:`apply_source` finds its own previous output to retract.

Staleness (``now >= stale_after``) is deliberately **not** stored: it is a
function of the clock, and a stored boolean would move the content-addressed
graph generation on the day it flipped with no byte of the corpus changed.
``stale_after`` is stored; a reader compares.
"""

from __future__ import annotations

import logging
import posixpath
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from pheasant.graph.enrichment import EnrichmentEdge, EnrichmentNode, _node_id, _slug_part
from pheasant.ingestion.okf import INDEX_FILENAME, LOG_FILENAME

logger = logging.getLogger(__name__)

ENRICHMENT_PASS = "okf"

#: Node types only this pass creates. ``tag`` was documented in
#: ``docs/graph_model.md`` from the initial build and never emitted.
BUNDLE_NODE_TYPE = "okf_bundle"
CONCEPT_TYPE_NODE_TYPE = "okf_type"
TAG_NODE_TYPE = "tag"

#: Edge types new with this pass. The rest are existing vocabulary.
COMPUTATION_EDGE_TYPES = {
    "executor": "executed_by",
    "attester": "attested_by",
    "computation": "computed_by",
}
OKF_EDGE_TYPES = frozenset(
    {
        "contains",
        "links_to",
        "derived_from",
        "tagged_with",
        "supersedes",
        *COMPUTATION_EDGE_TYPES.values(),
    }
)

MIN_CONCEPTS = 2
MIN_CONCEPT_SHARE = 0.8

#: Files a repository carries whatever it holds. Neither concepts nor
#: evidence against a bundle.
BOILERPLATE_FILENAMES = frozenset(
    {
        "readme.md",
        "changelog.md",
        "contributing.md",
        "license.md",
        "code_of_conduct.md",
        "security.md",
    }
)


@dataclass(frozen=True)
class SourceArtifact:
    """One artifact of the source, as the planner needs it."""

    node_id: str
    relative_path: str
    okf: dict[str, Any] | None


@dataclass(frozen=True)
class Bundle:
    root: str
    detection: str
    okf_version: str | None


@dataclass
class OkfPlan:
    """The whole OKF graph of one source: what should exist after this pass."""

    nodes: list[EnrichmentNode] = field(default_factory=list)
    edges: list[EnrichmentEdge] = field(default_factory=list)
    bundles: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _DirStats:
    concepts: int = 0
    others: int = 0
    signals: int = 0
    has_log: bool = False


def detect_bundles(artifacts: list[SourceArtifact]) -> list[Bundle]:
    """The bundle roots in one source, shallowest first. Pure and deterministic."""

    markdown = [a for a in artifacts if a.relative_path.lower().endswith(".md")]
    stats: dict[str, _DirStats] = {}
    listings: dict[str, dict[str, Any]] = {}
    for artifact in markdown:
        path = _norm(artifact.relative_path)
        name = posixpath.basename(path).lower()
        role = (artifact.okf or {}).get("role")
        directory = posixpath.dirname(path)
        if name == INDEX_FILENAME:
            if role == "index":
                listings[directory] = artifact.okf or {}
            continue
        if name == LOG_FILENAME:
            if role == "log":
                for ancestor in _ancestors(directory):
                    stats.setdefault(ancestor, _DirStats()).has_log = True
            continue
        if name in BOILERPLATE_FILENAMES:
            continue
        is_concept = role == "concept"
        signalled = bool((artifact.okf or {}).get("signals")) if is_concept else False
        for ancestor in _ancestors(directory):
            entry = stats.setdefault(ancestor, _DirStats())
            if is_concept:
                entry.concepts += 1
                entry.signals += int(signalled)
            else:
                entry.others += 1

    def conformant(directory: str, minimum: int = MIN_CONCEPTS) -> bool:
        entry = stats.get(directory)
        if entry is None or entry.concepts < minimum:
            return False
        return entry.concepts / (entry.concepts + entry.others) >= MIN_CONCEPT_SHARE

    roots: list[Bundle] = []

    def claimed(directory: str) -> bool:
        return any(_within(directory, bundle.root) for bundle in roots)

    explicit = sorted(
        (d for d, listing in listings.items() if listing.get("okf_version")), key=_depth_key
    )
    for directory in explicit:
        if not claimed(directory) and conformant(directory, minimum=1):
            roots.append(Bundle(directory, "explicit", str(listings[directory]["okf_version"])))
    for directory in sorted(listings, key=_depth_key):
        if not claimed(directory) and conformant(directory):
            roots.append(Bundle(directory, "index", None))
    for directory in sorted(stats, key=_depth_key):
        if claimed(directory) or any(_within(bundle.root, directory) for bundle in roots):
            continue
        entry = stats[directory]
        if conformant(directory) and (entry.signals or entry.has_log):
            roots.append(Bundle(directory, "conformant_tree", None))
    return sorted(roots, key=lambda bundle: _depth_key(bundle.root))


def plan_source(
    kb_id: str,
    source_name: str,
    artifacts: list[SourceArtifact],
) -> OkfPlan:
    """Detect a source's bundles and derive every node and edge they imply."""

    plan = OkfPlan()
    bundles = detect_bundles(artifacts)
    if not bundles:
        return plan
    by_path = {_norm(a.relative_path): a for a in artifacts}
    by_lower: dict[str, SourceArtifact] = {}
    for path, artifact in sorted(by_path.items()):
        by_lower.setdefault(path.lower(), artifact)
    source_node = f"source:{kb_id}:{source_name}"
    for bundle in bundles:
        members = sorted(
            (a for path, a in by_path.items() if _within(posixpath.dirname(path), bundle.root)),
            key=lambda a: _norm(a.relative_path),
        )
        planner = _BundlePlanner(kb_id, source_name, source_node, bundle, by_path, by_lower)
        planner.plan(members)
        plan.nodes.extend(planner.nodes)
        plan.edges.extend(planner.edges)
        plan.bundles.append(planner.report)
    return plan


def artifact_attrs(graph: Any, artifact: Any) -> dict[str, Any]:
    """The ``okf`` attribute an artifact node should carry after this index of it.

    A file that carries no OKF structure gets no key, so its node is
    byte-identical to before. The reading is persisted because bundle
    detection runs over the source's artifacts on later syncs, most of which
    re-read nothing. And ``add_node`` merges over the existing attributes, so
    a file that *stopped* being an OKF document would keep its old reading
    forever: it is cleared to ``None`` instead -- a merge cannot delete.
    """

    okf = getattr(artifact, "okf", None)
    if okf:
        return {"okf": okf}
    previous = graph.nodes.get(artifact.id) or {}
    return {"okf": None} if previous.get("okf") else {}


def apply_source(builder: Any, source_name: str) -> dict[str, Any]:
    """Detect a source's OKF bundles and bring their graph up to date.

    Per source, not global: a bundle is a directory tree, so everything
    it implies lives inside the source that indexed it. The walk starts at
    the source node's ``indexes`` edges and goes no further than those
    artifacts' own out-edges and the bundle hubs, so it costs what the
    source costs rather than what the region costs
    (`tests/test_okf_bundles.py` bounds it the way
    `tests/test_graph_working_set.py` bounds the other passes).

    The plan is applied as a **diff**: wanted nodes and edges are upserted
    — a no-op for any that already exist unchanged, so an untouched
    bundle leaves the graph generation alone — and only what this pass
    emitted before and no longer wants is retracted, edge by edge, so a
    `references` edge sharing a pair with a retracted `links_to` stays.
    With ``graph.okf_bundles`` off the plan is empty, which retracts
    whatever an earlier run drew. A source holding no bundle costs one
    walk and writes nothing.
    """

    source_node = f"source:{builder.kb_id}:{source_name}"
    if source_node not in builder.graph:
        return {"bundles": [], "edges": 0, "removed_edges": 0, "removed_nodes": 0}
    artifacts, owned_edges, owned_nodes = _source_inputs(builder.graph, source_node)
    plan = (
        plan_source(builder.kb_id, source_name, artifacts)
        if builder.config.graph.okf_bundles
        else OkfPlan()
    )
    for node in plan.nodes:
        builder.upsert_node(node.id, node.type, node.label, dict(node.attrs))
    for edge in plan.edges:
        builder.upsert_edge(edge.source, edge.target, edge.type, dict(edge.attrs))

    wanted_edges = {(edge.source, edge.target, edge.type) for edge in plan.edges}
    stale_by_pair: dict[tuple[str, str], set[str]] = {}
    for source, target, edge_type in owned_edges - wanted_edges:
        stale_by_pair.setdefault((source, target), set()).add(edge_type)
    removed_edges = 0
    for (source, target), types in sorted(stale_by_pair.items()):
        removed_edges += builder.graph.remove_edges_where(
            source,
            target,
            lambda attrs, types=types: (
                attrs.get("type") in types and attrs.get("enrichment_pass") == ENRICHMENT_PASS
            ),
        )
    stale_nodes = owned_nodes - {node.id for node in plan.nodes}
    if stale_nodes:
        builder.graph.remove_nodes_from(stale_nodes)
    for bundle in plan.bundles:
        logger.info(
            "OKF bundle %s in %s (%s): %s concepts, types %s, trust %s",
            bundle["okf_root"] or ".",
            source_name,
            bundle["detection"],
            bundle["concept_count"],
            bundle["type_counts"],
            bundle["trust_counts"],
        )
    return {
        "bundles": plan.bundles,
        "edges": len(plan.edges),
        "removed_edges": removed_edges,
        "removed_nodes": len(stale_nodes),
    }


def _source_inputs(
    graph: Any, source_node: str
) -> tuple[list[SourceArtifact], set[tuple[str, str, str]], set[str]]:
    """The source's artifacts, plus every edge and node the OKF pass owns.

    Ownership is ``enrichment_pass == "okf"`` and nothing else, so the
    retraction in :func:`apply_source` can never take an edge or node
    another pass drew. Everything it owns is reachable from here: bundles
    hang off the source node, type hubs off bundles, and every other OKF
    edge starts at one of the source's artifacts.
    """

    artifacts: list[SourceArtifact] = []
    owned_edges: set[tuple[str, str, str]] = set()
    owned_nodes: set[str] = set()

    def collect(node_id: str) -> list[str]:
        targets = []
        for source, target, edge_map in graph.out_edges(node_id):
            for data in edge_map.values():
                if data.get("enrichment_pass") == ENRICHMENT_PASS:
                    owned_edges.add((source, target, str(data.get("type"))))
                    targets.append(target)
        return targets

    with graph.reading():
        artifact_ids: list[str] = []
        for _, target, edge_map in graph.out_edges(source_node):
            if any(data.get("type") == "indexes" for data in edge_map.values()):
                artifact_ids.append(target)
        frontier = collect(source_node)
        while frontier:
            node_id = frontier.pop()
            attrs = graph.nodes.get(node_id) or {}
            if attrs.get("enrichment_pass") != ENRICHMENT_PASS or node_id in owned_nodes:
                continue
            owned_nodes.add(node_id)
            frontier.extend(collect(node_id))
        for artifact_id in artifact_ids:
            attrs = graph.nodes.get(artifact_id) or {}
            relative_path = attrs.get("relative_path")
            if not relative_path:
                continue
            artifacts.append(
                SourceArtifact(artifact_id, str(relative_path), attrs.get("okf") or None)
            )
            for target in collect(artifact_id):
                target_attrs = graph.nodes.get(target) or {}
                if target_attrs.get("enrichment_pass") == ENRICHMENT_PASS:
                    owned_nodes.add(target)
    return artifacts, owned_edges, owned_nodes


class _BundlePlanner:
    def __init__(
        self,
        kb_id: str,
        source_name: str,
        source_node: str,
        bundle: Bundle,
        by_path: dict[str, SourceArtifact],
        by_lower: dict[str, SourceArtifact],
    ):
        self.kb_id = kb_id
        self.source_name = source_name
        self.source_node = source_node
        self.bundle = bundle
        self.by_path = by_path
        self.by_lower = by_lower
        self.bundle_id = f"okf_bundle:{source_name}:{bundle.root or '.'}"
        self.nodes: list[EnrichmentNode] = []
        self.edges: list[EnrichmentEdge] = []
        self._edge_index: dict[tuple[str, str, str], int] = {}
        self._node_ids: set[str] = set()
        self.broken_links = 0
        self.report: dict[str, Any] = {}

    # -- assembly -----------------------------------------------------------

    def plan(self, members: list[SourceArtifact]) -> None:
        concepts = [a for a in members if (a.okf or {}).get("role") == "concept"]
        reserved = [
            a
            for a in members
            if posixpath.basename(_norm(a.relative_path)).lower() in {INDEX_FILENAME, LOG_FILENAME}
            and (a.okf or {}).get("role") in {"index", "log"}
        ]
        type_counts = Counter(str(a.okf["type"]) for a in concepts)
        type_labels: dict[str, str] = {}
        for label in sorted(type_counts):
            type_labels.setdefault(_slug_part(label), label)
        tag_counts = Counter(tag for a in concepts for tag in a.okf.get("tags") or [])

        for slug, label in sorted(type_labels.items()):
            count = sum(n for name, n in type_counts.items() if _slug_part(name) == slug)
            self._node(
                self._type_id(slug),
                CONCEPT_TYPE_NODE_TYPE,
                label,
                {"okf_type": label, "okf_root": self.bundle.root, "concept_count": count},
            )
            self._edge(self.bundle_id, self._type_id(slug), "contains", {})
        for tag in sorted(tag_counts):
            self._node(
                self._tag_id(tag),
                TAG_NODE_TYPE,
                tag,
                {"tag": tag, "okf_root": self.bundle.root, "concept_count": tag_counts[tag]},
            )

        for artifact in reserved:
            self._edge(
                self.bundle_id,
                artifact.node_id,
                "contains",
                {"okf_role": artifact.okf["role"]},
            )
            if artifact.okf["role"] == "index":
                self._index_edges(artifact)
            else:
                self._log_edges(artifact)

        for artifact in concepts:
            self._concept_edges(artifact)
        self._supersedes(concepts)

        trust = Counter(str(a.okf.get("trust_tier") or "unverified") for a in concepts)
        status = Counter(str(a.okf.get("status") or "stable") for a in concepts)
        computations = sum(1 for a in concepts if a.okf.get("computation"))
        label = posixpath.basename(self.bundle.root) or self.source_name
        bundle_attrs = {
            "okf_root": self.bundle.root,
            "detection": self.bundle.detection,
            "okf_version": self.bundle.okf_version,
            "concept_count": len(concepts),
            "type_counts": dict(sorted(type_counts.items())),
            "trust_counts": dict(sorted(trust.items())),
            "status_counts": dict(sorted(status.items())),
            "attested_computations": computations,
            "tag_count": len(tag_counts),
            "broken_links": self.broken_links,
        }
        # The bundle node goes first so a reader of the plan sees it before
        # the hubs that hang off it; the edge from the source closes the tree.
        self.nodes.insert(
            0,
            EnrichmentNode(
                self.bundle_id,
                BUNDLE_NODE_TYPE,
                label,
                {"source_id": self.source_name, "enrichment_pass": ENRICHMENT_PASS, **bundle_attrs},
            ),
        )
        self._edge(self.source_node, self.bundle_id, "contains", {})
        self.report = {"bundle_id": self.bundle_id, "label": label, **bundle_attrs}

    def _concept_edges(self, artifact: SourceArtifact) -> None:
        okf = artifact.okf or {}
        path = _norm(artifact.relative_path)
        concept_id = _concept_id(path, self.bundle.root)
        self._edge(
            self._type_id(_slug_part(str(okf["type"]))),
            artifact.node_id,
            "contains",
            {
                "concept_id": concept_id,
                "status": okf.get("status"),
                "trust_tier": okf.get("trust_tier"),
            },
        )
        for tag in okf.get("tags") or []:
            self._edge(artifact.node_id, self._tag_id(tag), "tagged_with", {"tag": tag})

        for link in okf.get("links") or []:
            target = self._resolve(link["target"], path)
            if target is None:
                if not _is_external(link["target"]):
                    self.broken_links += 1
                continue
            self._edge(
                artifact.node_id,
                target.node_id,
                "links_to",
                {"relation": "body_link", "text": link.get("text") or None},
            )

        citations = okf.get("citations") or {}
        for source in okf.get("sources") or []:
            resource = str(source["resource"])
            target_id = self._source_target(resource, path)
            attrs = {
                "relation": "okf_source",
                "source_key": source.get("id"),
                "title": source.get("title"),
                "author": source.get("author"),
                "usage_count": source.get("usage_count"),
                "last_modified": source.get("last_modified"),
                "usage_window": source.get("usage_window"),
                "citations": citations.get(source.get("id"), 0) if source.get("id") else 0,
            }
            self._edge(artifact.node_id, target_id, "derived_from", attrs)

        computation = okf.get("computation") or {}
        for key, edge_type in COMPUTATION_EDGE_TYPES.items():
            value = computation.get(key)
            resource = value.get("resource") if isinstance(value, dict) else value
            if not resource:
                continue
            target_id = self._source_target(str(resource), path, reference_type=f"okf_{key}")
            attrs: dict[str, Any] = {"runtime": computation.get("runtime")}
            if key == "executor" and isinstance(value, dict):
                attrs["receipt"] = value.get("receipt") or []
            self._edge(artifact.node_id, target_id, edge_type, attrs)

    def _index_edges(self, artifact: SourceArtifact) -> None:
        path = _norm(artifact.relative_path)
        for entry in (artifact.okf or {}).get("entries") or []:
            target = self._resolve(entry["target"], path)
            if target is None:
                if not _is_external(entry["target"]):
                    self.broken_links += 1
                continue
            self._edge(
                artifact.node_id,
                target.node_id,
                "links_to",
                {
                    "relation": "index_entry",
                    "title": entry.get("title") or None,
                    "description": entry.get("description"),
                    "section": entry.get("section"),
                },
            )

    def _log_edges(self, artifact: SourceArtifact) -> None:
        path = _norm(artifact.relative_path)
        history: dict[str, list[dict[str, Any]]] = {}
        resolved: dict[str, SourceArtifact] = {}
        for entry in (artifact.okf or {}).get("entries") or []:
            for raw in entry.get("targets") or []:
                target = self._resolve(raw, path)
                if target is None:
                    continue
                resolved[target.node_id] = target
                history.setdefault(target.node_id, []).append(
                    {"date": entry.get("date"), "action": entry.get("action")}
                )
        for node_id in sorted(history):
            # One edge per log/target pair, carrying every dated mention: the
            # graph keys an edge by (source, target, type), so a concept named
            # on three dates is one relationship with a history, not three.
            entries = history[node_id]
            self._edge(
                artifact.node_id,
                node_id,
                "links_to",
                {
                    "relation": "log_entry",
                    "entries": entries,
                    "last_date": max(str(e.get("date") or "") for e in entries) or None,
                },
            )

    def _supersedes(self, concepts: list[SourceArtifact]) -> None:
        """``current --supersedes--> deprecated`` when the bundle says so in links.

        OKF has no supersession field: a deprecated concept is "kept for links
        and history" (§5.4) and, by convention, points at what replaced it.
        The rule is narrow on purpose — a deprecated concept linking to
        **exactly one** non-deprecated concept **of its own type** — because a
        deprecated metric that links to two current metrics is naming related
        work, not its successor, and a guessed edge is worse than none.
        """

        by_node = {a.node_id: a for a in concepts}
        for artifact in concepts:
            okf = artifact.okf or {}
            if okf.get("status") != "deprecated":
                continue
            successors: set[str] = set()
            for link in okf.get("links") or []:
                target = self._resolve(link["target"], _norm(artifact.relative_path))
                candidate = by_node.get(target.node_id) if target else None
                if (
                    candidate is not None
                    and candidate.node_id != artifact.node_id
                    and (candidate.okf or {}).get("status") != "deprecated"
                    and (candidate.okf or {}).get("type") == okf.get("type")
                ):
                    successors.add(candidate.node_id)
            if len(successors) == 1:
                self._edge(
                    successors.pop(),
                    artifact.node_id,
                    "supersedes",
                    {"rule": "deprecated_links_to_current_of_same_type"},
                )

    # -- resolution ---------------------------------------------------------

    def _resolve(self, target: str, document: str) -> SourceArtifact | None:
        """An in-bundle link target, resolved to an artifact of this source.

        Tried relative to the linking document first (standard Markdown), then
        relative to the bundle root — the spec's own examples write
        ``references/skills/run-on-bq.md`` from inside ``computations/``, and
        a leading ``/`` is bundle-relative by definition (§6.1). A directory
        target resolves to its ``index.md``. Nothing outside the bundle root
        resolves: a link that escapes the bundle is not a bundle relationship.
        """

        if _is_external(target):
            return None
        cleaned = unquote(target.split("#", 1)[0].split("?", 1)[0].strip())
        if not cleaned:
            return None
        root = self.bundle.root
        if cleaned.startswith("/"):
            candidates = [posixpath.join(root, cleaned.lstrip("/"))]
        else:
            candidates = [
                posixpath.join(posixpath.dirname(document), cleaned),
                posixpath.join(root, cleaned),
            ]
        for candidate in candidates:
            normalized = _norm(posixpath.normpath(candidate)) if candidate else ""
            if normalized in {"", "."}:
                normalized = ""
            if normalized.startswith("..") or not _within(
                posixpath.dirname(normalized) if normalized else "", root
            ):
                continue
            for option in (
                normalized,
                posixpath.join(normalized, INDEX_FILENAME) if normalized else INDEX_FILENAME,
                f"{normalized}.md",
            ):
                found = self.by_path.get(option) or self.by_lower.get(option.lower())
                if found is not None and _within(
                    posixpath.dirname(_norm(found.relative_path)), root
                ):
                    return found
        return None

    def _source_target(
        self, resource: str, document: str, reference_type: str = "okf_source"
    ) -> str:
        """A provenance target: the in-bundle artifact, or a stub that names it.

        ``sources[].resource`` may be a URL, a bundle path, or a scope
        descriptor nothing can follow ("all queries in project X", §5.1). The
        last two still deserve a node: two concepts derived from one wiki page
        meet at that page's node, which is the provenance a reader wants to
        walk. Stubs are scoped to the source, as the enrichment pass's are.
        """

        target = None if _is_external(resource) else self._resolve(resource, document)
        if target is not None:
            return target.node_id
        stub_id = _node_id(
            "external_reference", self.kb_id, self.source_name, reference_type, resource
        )
        kind = (
            "url"
            if _is_external(resource)
            else "scope"
            if any(ch.isspace() for ch in resource)
            else "unresolved_path"
        )
        self._node(
            stub_id,
            "external_reference",
            resource,
            {
                "reference": resource,
                "reference_type": reference_type,
                "resource_kind": kind,
                "okf_root": self.bundle.root,
            },
        )
        return stub_id

    # -- primitives ---------------------------------------------------------

    def _type_id(self, slug: str) -> str:
        return f"okf_type:{self.source_name}:{self.bundle.root or '.'}:{slug}"

    def _tag_id(self, tag: str) -> str:
        return f"tag:{self.source_name}:{self.bundle.root or '.'}:{_slug_part(tag)}"

    def _node(self, node_id: str, node_type: str, label: str, attrs: dict[str, Any]) -> None:
        if node_id in self._node_ids:
            return
        self._node_ids.add(node_id)
        self.nodes.append(
            EnrichmentNode(
                node_id,
                node_type,
                label,
                {"source_id": self.source_name, "enrichment_pass": ENRICHMENT_PASS, **attrs},
            )
        )

    def _edge(self, source: str, target: str, edge_type: str, attrs: dict[str, Any]) -> None:
        if source == target:
            return
        key = (source, target, edge_type)
        # `None` values are kept: `upsert_edge` merges into an existing edge,
        # so a key that vanished from the plan would otherwise keep its old
        # value. A fixed key set per relation makes the merge a replacement.
        payload = {"source_id": self.source_name, "enrichment_pass": ENRICHMENT_PASS, **attrs}
        if key in self._edge_index:
            existing = self.edges[self._edge_index[key]]
            self.edges[self._edge_index[key]] = EnrichmentEdge(
                source, target, edge_type, {**payload, **existing.attrs}
            )
            return
        self._edge_index[key] = len(self.edges)
        self.edges.append(EnrichmentEdge(source, target, edge_type, payload))


def _concept_id(path: str, root: str) -> str:
    """OKF §2: the concept's path within the bundle, ``.md`` removed."""

    relative = path[len(root) + 1 :] if root else path
    return relative[:-3] if relative.lower().endswith(".md") else relative


def _is_external(target: str) -> bool:
    return "://" in target or target.lower().startswith(("mailto:", "data:"))


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def _ancestors(directory: str) -> list[str]:
    """``a/b`` → ``["", "a", "a/b"]``: every directory a file is under."""

    parts = [part for part in directory.split("/") if part]
    return [""] + ["/".join(parts[: index + 1]) for index in range(len(parts))]


def _within(directory: str, root: str) -> bool:
    return not root or directory == root or directory.startswith(root + "/")


def _depth_key(directory: str) -> tuple[int, str]:
    return (0 if not directory else directory.count("/") + 1, directory)
