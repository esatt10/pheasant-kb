"""The same operation, through both surfaces, against one corpus.

pheasant has two public APIs. The documentation said they were one facade
exposed twice; they were two implementations, and by the time this test was
written **six** operations had diverged in ways nobody decided:

===========================  ==========================================
``relevant_files``           deduplicated by path over HTTP only
``relevant_files``           honoured the memory policy over HTTP only —
                             so an agent could be served a record the
                             region *knew* was corrected
``relevant_files``           honoured ``section`` over HTTP only
``graph_neighbors``          hierarchy-first, ``max_nodes`` and the two
                             exclusions existed over HTTP only, so the
                             same node returned a different ordering
``explain_node``             returned the node's attributes over HTTP only
``file_summary``             chunk-ordered summary, the file's content and
                             **the ACL check** existed over HTTP only
``search``                   recorded its metrics over HTTP only, so every
                             agent's search was invisible to the counters
                             an operator sizes the region with
===========================  ==========================================

Each is one line that landed on the surface whose bug report arrived. None is
a thing a reviewer could have seen, because seeing it means reading two files
five hundred lines apart and noticing an absence.

So this is a matrix rather than a set of assertions: every operation that
exists on both surfaces is driven through both against one indexed corpus, and
the answers must match. A new operation added to one surface and forgotten on
the other fails here; so does a fix applied to one copy of something.

**Refusal text is part of it.** An agent told only that something failed will
retry the same call; "Unknown source: notes" is what it can act on. Two
spellings of one refusal is the same defect as two implementations of one
operation, one level down.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.mcp_server.tools import PheasantTools
from pheasant.services.errors import ServiceError

CORPUS = {
    "guide/deploy.md": (
        "# Deployment guide\n\n"
        "The gateway rotates credentials nightly. Restart the vault service "
        "after a rotation or the sidecar keeps a stale token.\n"
    ),
    "guide/search.md": (
        "# Search internals\n\n"
        "Hybrid retrieval fuses the text, vector and graph arms with "
        "reciprocal rank fusion.\n"
    ),
    "notes/rotation.md": (
        "# Rotation runbook\n\nCredential rotation runs at 02:00 UTC and the "
        "gateway reloads without downtime.\n"
    ),
}


@pytest.fixture(scope="module")
def region(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """One indexed corpus, one HTTP client, one tool facade.

    Module-scoped: indexing twice would be slower and would prove less — the
    point is that the *same* state answers both surfaces.
    """

    root = tmp_path_factory.mktemp("conformance")
    workspace = root / "workspace"
    for relative, text in CORPUS.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "conformance",
                "state_path": str(root / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(root / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "storage": {"graph_snapshots": False},
            "sources": [
                {
                    "name": "docs",
                    "type": "markdown_folder",
                    "path": str(workspace),
                    "include": ["**/*.md"],
                }
            ],
        }
    )

    tools = PheasantTools(config)
    tools.engine.sync_source("docs", "full")
    # Serve the *published* graph on both surfaces, which is what the
    # generation id on each response says they are serving.
    #
    # Not a workaround for a divergence between the two implementations —
    # there is one implementation now. It equalises an input. A graph built
    # in memory during a sync and the same graph loaded back from disk hold
    # identical nodes and edges in a different insertion order, so
    # `search_graph` enumerates equal-scoring nodes in a different order and
    # the fusion's tie-break carries that into the tail of the results. The
    # process that indexed has the first; a process that started afterwards
    # has the second. Comparing them without this compares two graph
    # *orderings*, not two operations.
    #
    # The underlying tie-order sensitivity is real, pre-existing and worth its
    # own change: a deterministic tie-break inside the graph arm is a ranking
    # change and needs its own evidence, not a line in a refactor.
    tools.engine.reload_graph()
    client = TestClient(create_app(config, config_path=str(root / "pheasant.yaml")))

    return {"config": config, "tools": tools, "client": client, "kb": config.knowledge_base_id}


def _http(region: dict[str, Any], method: str, path: str, **kwargs: Any) -> Any:
    response = getattr(region["client"], method)(path, **kwargs)
    assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text}"
    return response.json()


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    ["credential rotation", "hybrid retrieval fusion", "vault sidecar token"],
)
def test_search_answers_identically_on_both_surfaces(region: dict[str, Any], query: str) -> None:
    """The highest-traffic operation, and the one that already diverged."""

    over_http = _http(
        region, "post", "/search", json={"query": query, "mode": "hybrid", "max_results": 5}
    )
    over_mcp = region["tools"].search_context(region["kb"], query, "hybrid", 5)

    assert _identities(over_http) == _identities(over_mcp)
    assert over_http["counts"] == over_mcp["counts"]
    assert over_http.get("graph_generation") == over_mcp.get("graph_generation")


def test_search_criteria_apply_identically(region: dict[str, Any]) -> None:
    """Criteria are the filters both surfaces used to apply for themselves,
    each with its own over-fetch."""

    body = {
        "query": "rotation",
        "mode": "hybrid",
        "max_results": 3,
        "exclude_sources": ["nothing-matches-this"],
        "node_types": ["chunk"],
    }
    over_http = _http(region, "post", "/search", json=body)
    over_mcp = region["tools"].search_context(
        region["kb"],
        "rotation",
        "hybrid",
        3,
        exclude_sources=["nothing-matches-this"],
        node_types=["chunk"],
    )

    assert _identities(over_http) == _identities(over_mcp)
    assert over_http.get("criteria") == over_mcp.get("criteria")


def test_relevant_files_answers_identically(region: dict[str, Any]) -> None:
    """Three of the six divergences were in this one operation."""

    over_http = _http(
        region, "post", "/relevant-files", json={"query": "credential rotation", "max_results": 5}
    )
    over_mcp = region["tools"].get_relevant_files(region["kb"], "credential rotation", max_files=5)

    http_paths = [item.get("relative_path") for item in over_http["files"]]
    mcp_paths = [item.get("relative_path") for item in over_mcp["files"]]
    assert http_paths == mcp_paths
    # …and the deduplication both surfaces now share.
    assert len(http_paths) == len(set(http_paths))


# ---------------------------------------------------------------------------
# Graph reads
# ---------------------------------------------------------------------------


def _a_node(region: dict[str, Any]) -> str:
    payload = _http(region, "post", "/search", json={"query": "rotation", "max_results": 5})
    for item in payload["results"]:
        node_id = item.get("node_id")
        if node_id:
            return str(node_id)
    pytest.skip("no node id in the corpus to walk from")


def test_graph_neighbors_walk_identically(region: dict[str, Any]) -> None:
    """Two BFS implementations, one of which walked hierarchy edges first."""

    node_id = _a_node(region)
    over_http = _http(region, "get", "/graph/neighbors", params={"node_id": node_id, "depth": 2})
    over_mcp = region["tools"].get_graph_neighbors(region["kb"], node_id, 2)

    assert [item["node_id"] for item in over_http["neighbors"]] == [
        item["node_id"] for item in over_mcp["neighbors"]
    ]
    assert over_http["depth"] == over_mcp["depth"]


def test_graph_slice_answers_identically(region: dict[str, Any]) -> None:
    """The MCP copy reported no `depths` and no `truncated` at all."""

    node_id = _a_node(region)
    over_http = _http(
        region, "get", "/graph/slice", params={"node_id": node_id, "depth": 2, "limit": 10}
    )
    over_mcp = region["tools"].get_graph_slice(region["kb"], node_id, 2, limit=10)

    assert [node.get("id") for node in over_http["nodes"]] == [
        node.get("id") for node in over_mcp["nodes"]
    ]
    assert over_http["depths"] == over_mcp["depths"]
    assert over_http["truncated"] == over_mcp["truncated"]


def test_explain_node_answers_identically(region: dict[str, Any]) -> None:
    """The MCP copy omitted the node's attributes."""

    node_id = _a_node(region)
    over_http = _http(region, "get", "/nodes/explain", params={"node_id": node_id})
    over_mcp = region["tools"].explain_node(region["kb"], node_id)
    assert over_http == over_mcp


def test_file_summary_answers_identically(region: dict[str, Any]) -> None:
    """The MCP copy concatenated in join order and returned no content."""

    over_http = _http(region, "get", "/files/summary", params={"path": "guide/deploy.md"})
    over_mcp = region["tools"].get_file_summary(region["kb"], "guide/deploy.md")
    assert over_http == over_mcp
    assert over_mcp.get("content"), "the shared implementation returns the file's text"


def test_repo_map_answers_identically(region: dict[str, Any]) -> None:
    over_http = _http(region, "get", "/sources/docs/repo-map")
    over_mcp = region["tools"].get_repo_map(region["kb"], "docs")
    assert over_http == over_mcp


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_an_unknown_knowledge_base_is_refused_with_one_text(region: dict[str, Any]) -> None:
    """HTTP used to search the configured region anyway; MCP raised.

    Now both refuse, and both say the same thing — which is what an agent
    needs, since it is the text and not the status that tells it what to fix.
    """

    response = region["client"].post(
        "/search", json={"query": "rotation", "knowledge_base": "somewhere-else"}
    )
    assert response.status_code == 404

    with pytest.raises(ServiceError) as refused:
        region["tools"].search_context("somewhere-else", "rotation")

    assert response.json()["detail"] == str(refused.value)
    assert "somewhere-else" in str(refused.value)


def test_the_refusal_is_a_value_error_so_the_sdk_boundary_still_reads_it(
    region: dict[str, Any],
) -> None:
    """`mcp_server/server.py` translates ValueError/KeyError into an
    informative `ToolError`. A refusal type outside that set would have blanked
    the reason on every tool at once — which is a defect this repository has
    already shipped once, when an SDK upgrade did exactly that."""

    with pytest.raises(ValueError):
        region["tools"].search_context("somewhere-else", "rotation")


# ---------------------------------------------------------------------------
# The matrix stays complete
# ---------------------------------------------------------------------------


#: Operations extracted into `pheasant.services`, with the surfaces that call
#: them. Adding an operation to the layer without adding it here fails below.
CONFORMED = {
    "search": ("POST /search", "search_context"),
    "relevant_files": ("POST /relevant-files", "get_relevant_files"),
    "neighbors": ("GET /graph/neighbors", "get_graph_neighbors"),
    "slice_": ("GET /graph/slice", "get_graph_slice"),
    "explain_node": ("GET /nodes/explain", "explain_node"),
    "file_summary": ("GET /files/summary", "get_file_summary"),
    "repo_map": ("GET /sources/{id}/repo-map", "get_repo_map"),
}


def test_every_extracted_operation_is_in_the_matrix() -> None:
    """A service function with no conformance case is an operation whose two
    callers are free to drift again — which is the whole thing this package
    exists to stop."""

    import inspect

    from pheasant.services import graph as graph_service
    from pheasant.services import retrieval as retrieval_service

    public: set[str] = set()
    for module in (retrieval_service, graph_service):
        for name, value in vars(module).items():
            if name.startswith("_") or not inspect.isfunction(value):
                continue
            if value.__module__ != module.__name__:
                continue
            public.add(name)
    # Predicates called *by* the operations rather than operations themselves,
    # and neither surface exposes either as one. They are public because a
    # second caller legitimately shares them — `memory_enabled` answers the one
    # question `describe_retrieval`, the memory routes and the readiness probes
    # all have to agree on, and a private copy per caller is how a region ends
    # up reporting memory off while it is on.
    public.discard("require_readable")
    public.discard("memory_enabled")

    missing = sorted(public - set(CONFORMED))
    assert not missing, (
        f"these service operations have no conformance case: {missing}. Add one, or — if the "
        "operation genuinely exists on one surface only — say so here, because an operation "
        "with one caller today is one that grows a second implementation tomorrow."
    )


def test_the_tools_facade_still_exposes_every_conformed_operation() -> None:
    for _operation, (_route, tool) in CONFORMED.items():
        assert hasattr(PheasantTools, tool), f"the MCP surface lost {tool}"


def _identities(payload: dict[str, Any]) -> list[str]:
    return [
        str(item.get("chunk_id") or item.get("node_id") or "")
        for item in payload.get("results") or []
    ]


def test_the_corpus_is_real_enough_to_prove_something(region: dict[str, Any]) -> None:
    """A corpus where every query returns nothing would conform perfectly."""

    payload = _http(region, "post", "/search", json={"query": "credential rotation"})
    assert payload["results"], "the fixture corpus answers nothing — the matrix proves nothing"
    assert Path(region["config"].pheasant.workspace_root).exists()
