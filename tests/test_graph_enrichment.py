from __future__ import annotations

from pathlib import Path

from pheasant.mcp_server.tools import PheasantTools
from tests.conftest import result_items, run_sync


def test_pathological_enrichment_node_ids_are_bounded_and_deterministic() -> None:
    from pheasant.graph.enrichment import MAX_ENRICHMENT_NODE_ID_LENGTH, _node_id

    ordinary = _node_id("external_reference", "kb", "source", "url", "example.com/readme")
    assert ordinary == "external_reference:kb:source:url:example.com-readme"

    reference = "https://example.test/" + "very-long-reference/" * 500
    first = _node_id("external_reference", "kb", "source", "url", reference)
    second = _node_id("external_reference", "kb", "source", "url", reference)
    different = _node_id("external_reference", "kb", "source", "url", reference + "other")

    assert len(first) <= MAX_ENRICHMENT_NODE_ID_LENGTH
    assert first.startswith("external_reference:kb:source:url:")
    assert ":sha256=" in first
    assert first == second
    assert first != different


def test_full_sync_creates_enriched_graph_nodes_and_edges(
    workspace_copy: Path,
    loaded_config: object,
    sync_engine: object,
) -> None:
    readme = workspace_copy / "pheasant-repo" / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        + "\nSee [API docs](https://example.com/pheasant/api) and [@pheasant-paper].\n",
        encoding="utf-8",
    )
    (workspace_copy / "pheasant-repo" / "pheasant" / "cross_file.py").write_text(
        "import json\n\n"
        "HEALTH_PATH = '/health'\n\n"
        "class SearchAgent:\n"
        "    def render(self) -> str:\n"
        "        return json.dumps({'path': HEALTH_PATH})\n",
        encoding="utf-8",
    )

    run_sync(sync_engine, source_name="pheasant-repo", mode="full")
    graph = sync_engine.graph_builder.graph
    node_types = {node["type"] for node in graph.to_node_link()["nodes"]}
    edge_types = {edge["type"] for edge in graph.to_node_link()["links"]}

    # `concept` is deliberately absent: concept extraction was retired after it
    # measured as 87% of the graph while contributing nothing to retrieval,
    # graph facts or similarity (graph.enrichment._add_concept).
    assert {"directory", "symbol", "entity", "external_reference"} <= node_types
    assert "concept" not in node_types
    assert any(
        node["type"] == "directory" and node.get("relative_path") == "pheasant"
        for node in graph.to_node_link()["nodes"]
    )
    expected_edges = {
        "contains",
        "imports",
        "calls",
        "references",
        "mentions",
    }
    assert expected_edges <= edge_types
    # `similar_to` went with concept extraction: the similarity pass scored
    # artifacts by shared `concept_terms`, and with no concepts there is
    # nothing for it to compare. This is not a silent loss — it never worked.
    # The live 2,132-file graph contained ZERO similar_to edges before the
    # removal, so the pass was already producing nothing while costing a
    # pairwise scan of every artifact against every other on each sync.
    assert "similar_to" not in edge_types


def test_graph_neighbors_honor_depth_and_edge_filters(
    loaded_config: object,
    sync_engine: object,
) -> None:
    run_sync(sync_engine, source_name="pheasant-repo", mode="full")
    tools = PheasantTools(loaded_config)
    node_id = "file:pheasant-repo:pheasant/sync_engine.py:branch=none"
    result = tools.get_graph_neighbors(
        loaded_config.knowledge_base_id,
        node_id,
        depth=2,
        edge_types=["mentions", "derived_from"],
    )

    # A file reaches its symbols and entities in one hop, and those are the
    # connectivity that replaced concept extraction (graph.enrichment
    # ._add_concept) — narrower, and worth traversing.
    bridges = {
        neighbor["node"].get("type") for neighbor in result["neighbors"] if neighbor["depth"] == 1
    }
    assert bridges == {"symbol", "entity"}, bridges
    assert not any(neighbor["node"].get("type") == "concept" for neighbor in result["neighbors"])

    # This used to assert `any(depth == 2)`, described as "two hops still
    # bridge documents". It never did: every `derived_from` edge out of those
    # symbols and entities points back at the file that defines them, so the
    # depth-2 sighting was the walk returning to its own starting node. It
    # passed only because the traversal appended a node once per edge into it
    # rather than once per node, so the root was re-listed as a "neighbour" of
    # itself. Deduplicating the walk removed the illusion.
    #
    # What the traversal must guarantee is asserted instead: a node appears at
    # most once, and never the node you started from.
    seen = [neighbor["node_id"] for neighbor in result["neighbors"]]
    assert len(seen) == len(set(seen)), "the walk listed a node more than once"
    assert node_id not in seen, "the walk returned its own starting node as a neighbour"


def test_graph_terms_improve_cross_file_search(
    sync_engine: object,
) -> None:
    run_sync(sync_engine, source_name="pheasant-repo", mode="full")

    search_result = sync_engine.search_context("SyncEngine HEALTH_PATH", max_results=5)
    paths = {
        item["relative_path"] for item in result_items(search_result) if isinstance(item, dict)
    }

    assert "pheasant/sync_engine.py" in paths
    assert "pheasant/api.py" in paths


def test_similarity_index_matches_all_pairs_exactly() -> None:
    """The term index is an optimization, so it must emit the same edges.

    Two artifacts sharing no concept term score zero and are dropped by the
    threshold, so restricting candidates to "shares at least one term" cannot
    change the outcome — only the cost. This asserts that against a literal
    all-pairs implementation.

    **This class is unreachable from a sync.** It keys off ``concept_terms``,
    and concept extraction was retired, so nothing has fed it since — the
    terms below are hand-written by the test.
    ``GraphBuilder.add_similarity_edges`` is a no-op for that reason
    (`tests/test_graph_working_set.py`). These tests are kept because the
    class is the seam a real similarity pass would reuse, and this is what it
    would owe; they are not evidence that similarity edges are being emitted.
    """

    import random

    from pheasant.graph.enrichment import SemanticSimilarityPass

    def all_pairs(artifacts):
        edges = []
        for index, (left_id, left) in enumerate(artifacts):
            left_terms = set(left.get("concept_terms") or [])
            if len(left_terms) < 2:
                continue
            for right_id, right in artifacts[index + 1 :]:
                right_terms = set(right.get("concept_terms") or [])
                if len(right_terms) < 2:
                    continue
                shared = left_terms & right_terms
                score = len(shared) / len(left_terms | right_terms)
                if len(shared) < 2 and score < 0.25:
                    continue
                edges.append((left_id, right_id, round(min(1.0, score), 3)))
                edges.append((right_id, left_id, round(min(1.0, score), 3)))
        return sorted(edges)

    random.seed(11)
    vocabulary = [f"term{i}" for i in range(40)]
    artifacts = []
    for i in range(120):
        terms = random.sample(vocabulary, random.randint(0, 6))
        artifacts.append((f"file:src:{i}.py", {"concept_terms": terms}))

    produced = sorted(
        (edge.source, edge.target, edge.attrs["confidence"])
        for edge in SemanticSimilarityPass().run(artifacts)
    )
    assert produced == all_pairs(artifacts)


def test_similarity_can_be_limited_to_what_changed() -> None:
    """An incremental sync only needs pairs touching a rewritten artifact."""

    from pheasant.graph.enrichment import SemanticSimilarityPass

    artifacts = [
        ("a", {"concept_terms": ["alpha", "beta", "gamma"]}),
        ("b", {"concept_terms": ["alpha", "beta", "delta"]}),
        ("c", {"concept_terms": ["alpha", "beta", "epsilon"]}),
    ]
    every = SemanticSimilarityPass().run(artifacts)
    assert {(edge.source, edge.target) for edge in every} == {
        ("a", "b"),
        ("b", "a"),
        ("a", "c"),
        ("c", "a"),
        ("b", "c"),
        ("c", "b"),
    }

    only_a = SemanticSimilarityPass().run(artifacts, changed_ids={"a"})
    touched = {(edge.source, edge.target) for edge in only_a}
    assert touched == {("a", "b"), ("b", "a"), ("a", "c"), ("c", "a")}
    assert ("b", "c") not in touched, "untouched pair was re-derived"
