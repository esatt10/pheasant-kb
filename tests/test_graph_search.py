"""Tests for graph-aware search across nodes, relationships and attributes."""

from __future__ import annotations

from pheasant.graph.simple import SimpleMultiDiGraph
from pheasant.search.graph_search import search_graph
from pheasant.search.hybrid import HybridSearch


def _sample_graph() -> SimpleMultiDiGraph:
    graph = SimpleMultiDiGraph()
    graph.add_node(
        "file:repo:sync_engine.py",
        id="file:repo:sync_engine.py",
        type="file",
        label="pheasant/sync_engine.py",
        relative_path="pheasant/sync_engine.py",
        source_id="repo",
    )
    graph.add_node(
        "symbol:repo:SyncEngine",
        id="symbol:repo:SyncEngine",
        type="symbol",
        label="SyncEngine",
        name="SyncEngine",
        symbol_type="class",
        source_id="repo",
    )
    graph.add_node(
        "concept:repo:debounce",
        id="concept:repo:debounce",
        type="concept",
        label="debounce window",
        source_id="repo",
    )
    graph.add_node(
        "external_reference:repo:httpx",
        id="external_reference:repo:httpx",
        type="external_reference",
        label="httpx",
        reference="httpx",
        reference_type="python_import",
        source_id="repo",
    )
    graph.add_edge(
        "file:repo:sync_engine.py",
        "external_reference:repo:httpx",
        type="imports",
        source_id="repo",
    )
    graph.add_edge(
        "file:repo:sync_engine.py",
        "symbol:repo:SyncEngine",
        type="mentions",
        source_id="repo",
    )
    return graph


def test_graph_search_matches_node_label() -> None:
    results = search_graph(_sample_graph(), "SyncEngine", max_results=5)
    assert results, "expected a node hit for SyncEngine"
    top = results[0]
    assert top["kind"] == "node"
    assert top["node_id"] == "symbol:repo:SyncEngine"
    assert top["type"] == "symbol"


def test_graph_search_matches_attribute_value() -> None:
    # 'httpx' lives only in an attribute (reference), not the chunk text.
    results = search_graph(_sample_graph(), "httpx", max_results=5)
    node_ids = {r["node_id"] for r in results if r["kind"] == "node"}
    assert "external_reference:repo:httpx" in node_ids


def test_graph_search_matches_relationship_type() -> None:
    results = search_graph(_sample_graph(), "imports", max_results=10)
    rels = [r for r in results if r["kind"] == "relationship"]
    assert any(r["edge_type"] == "imports" for r in rels)


def test_graph_search_respects_source_scope() -> None:
    results = search_graph(_sample_graph(), "SyncEngine", max_results=5, source_name="other")
    assert results == []


def test_graph_search_honours_max_results() -> None:
    results = search_graph(_sample_graph(), "engine", max_results=2)
    assert len(results) <= 2


class _EmptyStore:
    def search(self, *args, **kwargs):  # noqa: D401, ANN002, ANN003
        return []


def test_hybrid_graph_mode_uses_graph_when_text_empty() -> None:
    hybrid = HybridSearch(_EmptyStore())
    payload = hybrid.search_context(
        "kb", "SyncEngine", mode="graph", max_results=5, graph=_sample_graph()
    )
    assert payload["mode"] == "graph"
    assert payload["counts"]["graph"] >= 1
    assert any(r["node_id"] == "symbol:repo:SyncEngine" for r in payload["results"])


def test_hybrid_without_graph_falls_back_to_text() -> None:
    hybrid = HybridSearch(_EmptyStore())
    payload = hybrid.search_context("kb", "SyncEngine", mode="hybrid", max_results=5)
    assert payload["counts"]["graph"] == 0
    assert payload["results"] == []


def test_fast_reject_never_changes_results() -> None:
    """The blob pre-filter is an optimization, so it must be invisible.

    Scoring every node is the reference behaviour; the pre-filter only skips
    nodes that provably cannot score. Any divergence is a lost search hit, so
    this compares the two over queries that hit labels, paths, types,
    attribute values and nothing at all.
    """

    import pheasant.search.graph_search as gs

    graph = _sample_graph()
    # Add bulk the queries must not accidentally match, so the filter is
    # actually doing work rather than passing everything through.
    for i in range(200):
        graph.add_node(
            f"concept:noise:{i}",
            id=f"concept:noise:{i}",
            type="concept",
            label=f"unrelated-topic-{i}",
            source_id="repo",
        )

    queries = [
        "SyncEngine",
        "sync engine",
        "sync_engine.py",
        "repo",
        "file",
        "graph builder",
        "zzz-no-such-thing",
        "",
        "  ",
    ]

    original = gs._may_match
    try:
        for query in queries:
            filtered = search_graph(graph, query, max_results=25)
            gs._may_match = lambda *_args, **_kwargs: True  # score everything
            unfiltered = search_graph(graph, query, max_results=25)
            assert filtered == unfiltered, f"fast reject changed results for {query!r}"
    finally:
        gs._may_match = original


def test_search_blob_tracks_node_updates() -> None:
    """A stale blob would silently hide a node from search."""

    graph = SimpleMultiDiGraph()
    graph.add_node("n1", id="n1", type="file", label="alpha.py", source_id="repo")
    assert [hit["node_id"] for hit in search_graph(graph, "alpha")] == ["n1"]

    # Rename: the old term stops matching, the new one starts.
    graph.add_node("n1", id="n1", type="file", label="beta.py", source_id="repo")
    assert search_graph(graph, "alpha") == []
    assert [hit["node_id"] for hit in search_graph(graph, "beta")] == ["n1"]

    graph.remove_nodes_from(["n1"])
    assert search_graph(graph, "beta") == []


def test_framing_words_are_not_search_terms() -> None:
    from pheasant.search.graph_search import _tokens

    assert _tokens("how does the vector index get rebuilt") == ["index", "rebuilt", "vector"]
    # A question made only of framing words keeps them rather than matching nothing.
    assert _tokens("what is it") == ["is", "it", "what"]


def test_a_question_ranks_its_subject_above_headings_sharing_a_framing_word() -> None:
    graph = _sample_graph()
    graph.add_node(
        "heading:docs:sign",
        type="heading",
        label="Sign the contract",
        source_id="docs",
    )
    hits = search_graph(graph, "what does the debounce window do", max_results=5)
    assert hits[0]["node_id"] == "concept:repo:debounce"
    assert "heading:docs:sign" not in {hit["node_id"] for hit in hits}
