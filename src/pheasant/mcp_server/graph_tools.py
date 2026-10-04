"""Graph reads over MCP: walk, slice, explain, and read what a hit points at.

Split out of `server.py` for the module ratchet in
`tests/test_module_budget.py`, along the same line `readiness_tools.py` and
`assistant_tools.py` were: by bounded context rather than by a raised ceiling.
These are the tools an agent uses *after* `search_context` -- following a
hit's `node_id` (or an expanded neighbour's) into the graph and reading the
file behind it -- plus the prompt that describes that workflow for a harness
which judges evidence itself.

Registration only. Every tool is an adapter onto `PheasantTools`, which calls
`services.graph`; nothing here decides anything, and this module imports no
SDK, which is why `mcp` and the per-surface `anticipated` decorator are handed
in rather than imported.
"""

from __future__ import annotations

from typing import Any


def register_graph_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
    """Register the graph-read tools and the raw-retrieval prompt."""

    @mcp.tool()
    @anticipated
    def get_graph_neighbors(  # noqa: PLR0913 - additive walk bounds
        knowledge_base: str,
        node_id: str,
        depth: int = 2,
        edge_types: list[str] | None = None,
        max_nodes: int | None = None,
        exclude_edge_types: list[str] | None = None,
        exclude_node_types: list[str] | None = None,
    ) -> dict:
        """Return graph neighbors around a node, breadth-first along outgoing edges.

        node_id is any graph node id: a search hit's node_id or chunk_id, or a
        neighbour returned by search_context's expand. Structural (contains)
        edges are walked first. edge_types keeps only those edges;
        exclude_edge_types and exclude_node_types prune the walk itself, so a
        hub's indexes fan-out does not spend the budget. max_nodes bounds the
        walk (unbounded by default; set it when starting from a directory or
        source node). Each neighbour carries its depth, the edge types it was
        reached by, its path from node_id, and its attributes.
        """

        return tools.get_graph_neighbors(
            knowledge_base,
            node_id,
            depth,
            edge_types,
            max_nodes=max_nodes,
            exclude_edge_types=exclude_edge_types,
            exclude_node_types=exclude_node_types,
        )

    @mcp.tool()
    @anticipated
    def get_graph_slice(  # noqa: PLR0913 - mirrors GET /graph/slice
        knowledge_base: str,
        node_id: str,
        depth: int = 1,
        limit: int = 100,
        edge_types: list[str] | None = None,
        exclude_edge_types: list[str] | None = None,
        exclude_node_types: list[str] | None = None,
    ) -> dict:
        """Return the connected sub-graph around a node: nodes plus every link among them.

        Where get_graph_neighbors returns a list of nodes reached, this returns
        the induced sub-graph (nodes, links between any two of them, and each
        node's hop distance under depths), which is what you want to reason
        about how a set of results relate to each other. truncated is true
        when the slice filled limit before running out of graph.
        """

        return tools.get_graph_slice(
            knowledge_base,
            node_id,
            depth,
            edge_types,
            limit,
            exclude_edge_types=exclude_edge_types,
            exclude_node_types=exclude_node_types,
        )

    @mcp.tool()
    @anticipated
    def get_file_summary(
        knowledge_base: str,
        path: str,
        source_name: str | None = None,
    ) -> dict:
        """Return summary and provenance for one indexed file."""

        return tools.get_file_summary(knowledge_base, path, source_name)

    @mcp.tool()
    @anticipated
    def get_repo_map(knowledge_base: str, source_name: str, depth: int = 3) -> dict:
        """Return a compact repository map for one source."""

        return tools.get_repo_map(knowledge_base, source_name, depth)

    @mcp.tool()
    @anticipated
    def explain_node(knowledge_base: str, node_id: str) -> dict:
        """Explain what an indexed graph node represents."""

        return tools.explain_node(knowledge_base, node_id)

    @mcp.prompt()
    def use_pheasant_for_raw_retrieval(query: str = "") -> str:
        """Guide an agent that judges retrieved evidence itself, with no region answerer."""

        suffix = f"\nQuery: {query}" if query else ""
        return (
            "Call describe_retrieval once to learn the modes, sources and node types here. "
            "Then call search_context (mode=hybrid) with expand=true, or search_context_batch "
            "for several facets; do not call ask_knowledge_base. Judge each hit yourself from "
            "its text, provenance and graph block. Follow promising neighbours with "
            "get_graph_neighbors or get_graph_slice, read whole files with get_file_summary, "
            "and pin repeated runs with snapshot_id so they see one corpus."
            f"{suffix}"
        )
