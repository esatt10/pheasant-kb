"""Raw retrieval plus graph structure: `search_context(expand=...)`.

The pattern this protects is an agent harness that runs its own evaluation:
it wants the region's hybrid retrieval and the graph around each hit, and it
does not want the region's answering workflow deciding for it which evidence
matters. So expansion is a walk, never a judgement — and four things about it
are contracts:

* **Off by default, and off means byte-identical.** A caller that does not ask
  receives exactly the payload it always did.
* **One implementation, two surfaces.** `POST /search` and the MCP tool expand
  identically and refuse a malformed value with one text.
* **It adds structure, it does not change retrieval.** The hits, their order
  and the query id are the same with and without it.
* **It cannot leak past ACLs.** A chunk node carries the opening of its text,
  so a walk from a hit the caller may read must not surface a neighbour they
  may not — nor anything reached *through* one.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.loader import load_config
from pheasant.config.schema import PheasantConfig
from pheasant.mcp_server.tools import PheasantTools
from pheasant.services.errors import InvalidRequest
from pheasant.sync import connector_registry
from pheasant.sync.connectors import ConnectorItem, ConnectorPayload, SourceConnector

CORPUS = {
    "app/main.py": (
        "from app import util\nfrom app.util import helper\n\n\ndef run():\n    return helper()\n"
    ),
    "app/util.py": "def helper():\n    '''Rotate credentials.'''\n    return 1\n",
    "docs/rotation.md": (
        "# Rotation\n\nCredential rotation is done by `helper` in app/util.py.\n\n"
        "## Schedule\n\nNightly.\n"
    ),
}

QUERY = "credential rotation helper"
MAIN = "file:code:app/main.py:branch=none"
UTIL = "file:code:app/util.py:branch=none"


@pytest.fixture(scope="module")
def region(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("expansion")
    workspace = root / "workspace"
    for relative, text in CORPUS.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "expansion",
                "state_path": str(root / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(root / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "storage": {"graph_snapshots": False},
            "sources": [
                {
                    "name": "code",
                    "type": "repository",
                    "path": str(workspace),
                    "include": ["**/*.py", "**/*.md"],
                }
            ],
        }
    )
    tools = PheasantTools(config)
    tools.engine.sync_source("code", "full")
    # Both surfaces serve the published graph; see test_surface_conformance.
    tools.engine.reload_graph()
    client = TestClient(create_app(config, config_path=str(root / "pheasant.yaml")))
    return {"tools": tools, "client": client, "kb": config.knowledge_base_id}


def _search(region: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return region["tools"].search_context(region["kb"], QUERY, "hybrid", 5, **kwargs)


def _hit(payload: dict[str, Any], node_id: str) -> dict[str, Any]:
    for item in payload["results"]:
        if item.get("node_id") == node_id:
            return item
    returned = [r.get("node_id") for r in payload["results"]]
    pytest.fail(f"{node_id} is not among the hits: {returned}")


def _neighbor_ids(hit: dict[str, Any]) -> list[str]:
    return [item["node_id"] for item in hit["graph"]["neighbors"]]


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("off", [None, False, 0])
def test_no_expansion_is_the_payload_it_always_was(region: dict[str, Any], off: Any) -> None:
    plain = _search(region)
    asked = _search(region, expand=off)

    assert "expansion" not in plain and "expansion" not in asked
    assert not any("graph" in item for item in plain["results"])
    plain["lineage"].pop("timing", None)
    asked["lineage"].pop("timing", None)
    assert plain == asked


def test_the_dead_flags_still_do_nothing(region: dict[str, Any]) -> None:
    """`include_graph_neighbors` was accepted and never read. It stays inert —
    turning on a walk for every caller that passes its default would change
    every deployed agent's payload — and `expand` is the switch."""

    payload = region["tools"].search_context(
        region["kb"], QUERY, "hybrid", 5, include_chunks=True, include_graph_neighbors=True
    )
    assert not any("graph" in item for item in payload["results"])


# ---------------------------------------------------------------------------
# What an expansion holds
# ---------------------------------------------------------------------------


def test_a_hit_reaches_the_file_its_import_resolves_to(region: dict[str, Any]) -> None:
    payload = _search(region, expand=True)
    main = _hit(payload, MAIN)

    assert main["graph"]["seed"] == MAIN
    by_id = {item["node_id"]: item for item in main["graph"]["neighbors"]}
    assert UTIL in by_id
    assert "imports" in by_id[UTIL]["edge_types"]
    assert by_id[UTIL]["via"] == MAIN
    assert by_id[UTIL]["depth"] == 1
    assert by_id[UTIL]["relative_path"] == "app/util.py"
    assert payload["expansion"]["depth"] == 1
    assert payload["expansion"]["seeds"] >= 1
    assert payload["expansion"]["nodes"] == sum(
        len(item["graph"]["neighbors"])
        for item in {r["node_id"]: r for r in payload["results"] if "graph" in r}.values()
    )


def test_expansion_changes_no_retrieval(region: dict[str, Any]) -> None:
    plain = _search(region)
    expanded = _search(region, expand={"depth": 2, "max_neighbors": 20})

    assert [r.get("node_id") for r in plain["results"]] == [
        r.get("node_id") for r in expanded["results"]
    ]
    assert plain["lineage"]["query_id"] == expanded["lineage"]["query_id"]


def test_a_files_own_passages_are_skipped_unless_asked_for(region: dict[str, Any]) -> None:
    default = _hit(_search(region, expand=True), MAIN)
    everything = _hit(_search(region, expand={"exclude_edge_types": []}), MAIN)

    assert not any("has_chunk" in item["edge_types"] for item in default["graph"]["neighbors"])
    assert any("has_chunk" in item["edge_types"] for item in everything["graph"]["neighbors"])


def test_edge_types_walk_only_those(region: dict[str, Any]) -> None:
    main = _hit(_search(region, expand={"edge_types": ["imports"]}), MAIN)

    # The resolved file and the import stubs, and nothing reached any other way.
    assert UTIL in _neighbor_ids(main)
    assert all(item["edge_types"] == ["imports"] for item in main["graph"]["neighbors"])


def test_max_neighbors_bounds_and_reports_truncation(region: dict[str, Any]) -> None:
    roomy = _hit(_search(region, expand={"max_neighbors": 50}), MAIN)
    tight = _hit(_search(region, expand={"max_neighbors": 1}), MAIN)

    assert len(roomy["graph"]["neighbors"]) > 1, "the fixture must have more than one neighbour"
    assert roomy["graph"]["truncated"] is False
    assert len(tight["graph"]["neighbors"]) == 1
    assert tight["graph"]["truncated"] is True
    # Truncation is in walk order, so the bounded list is a prefix.
    assert _neighbor_ids(tight) == _neighbor_ids(roomy)[:1]


def test_a_deeper_walk_names_how_each_node_was_reached(region: dict[str, Any]) -> None:
    main = _hit(_search(region, expand={"depth": 2, "max_neighbors": 50}), MAIN)
    neighbors = main["graph"]["neighbors"]
    seen = {MAIN} | {item["node_id"] for item in neighbors}

    assert any(item["depth"] == 2 for item in neighbors)
    for item in neighbors:
        assert item["via"] in seen
        assert "path" not in item and "node" not in item


def test_hits_sharing_a_seed_share_one_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two passages of one file have one seed, and are walked once."""

    from pheasant.graph import traversal
    from pheasant.graph.simple import SimpleMultiDiGraph

    graph = SimpleMultiDiGraph()
    graph.add_node("file:a", type="file", label="a")
    graph.add_node("file:b", type="file", label="b")
    graph.add_edge("file:a", "file:b", type="imports")
    walks: list[str] = []
    real = traversal.neighbors

    def counting(graph_obj: Any, node_id: str, *args: Any, **kwargs: Any) -> Any:
        walks.append(node_id)
        return real(graph_obj, node_id, *args, **kwargs)

    monkeypatch.setattr(traversal, "neighbors", counting)
    expanded = traversal.expand(graph, ["file:a", "file:a", "file:b"])

    assert walks == ["file:a", "file:b"]
    assert [n["node_id"] for n in expanded["file:a"]["neighbors"]] == ["file:b"]
    assert expanded["file:b"]["neighbors"] == []


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        (4, "depth must be an integer from 1 to 3"),
        (-1, "depth must be an integer from 1 to 3"),
        ("yes", "expand must be true, a depth"),
        ({"depth": 0}, "depth must be an integer from 1 to 3"),
        ({"max_neighbors": 0}, "max_neighbors must be an integer from 1 to 50"),
        ({"max_neighbors": 51}, "max_neighbors must be an integer from 1 to 50"),
        ({"edge_types": "imports"}, "edge_types must be a list"),
        ({"hops": 2}, "expand does not take hops"),
    ],
)
def test_a_malformed_expansion_is_refused_with_one_text(
    region: dict[str, Any], value: Any, fragment: str
) -> None:
    with pytest.raises(InvalidRequest) as refused:
        _search(region, expand=value)
    assert fragment in str(refused.value)

    response = region["client"].post(
        "/search", json={"query": QUERY, "max_results": 5, "expand": value}
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == str(refused.value)
    assert response.json()["code"] == "INVALID_REQUEST"


# ---------------------------------------------------------------------------
# Both surfaces, and the batch
# ---------------------------------------------------------------------------


def test_http_and_mcp_expand_identically(region: dict[str, Any]) -> None:
    expand = {"depth": 2, "max_neighbors": 6}
    over_mcp = _search(region, expand=expand)
    response = region["client"].post(
        "/search", json={"query": QUERY, "max_results": 5, "expand": expand}
    )
    assert response.status_code == 200, response.text
    over_http = response.json()

    assert over_http["expansion"] == over_mcp["expansion"]
    assert [r.get("graph") for r in over_http["results"]] == [
        r.get("graph") for r in over_mcp["results"]
    ]


def test_a_batch_expands_its_merged_context_once(region: dict[str, Any]) -> None:
    queries = [QUERY, "rotation schedule", QUERY]
    batch = region["tools"].search_context_batch(region["kb"], queries, "hybrid", 3, expand=True)

    assert batch["expansion"]["seeds"] == len(
        {r["node_id"] for r in batch["results"] if r.get("node_id")}
    )
    assert any("graph" in item for item in batch["results"])
    for search in batch["searches"]:
        assert "expansion" not in search
        assert not any("graph" in item for item in search["results"])

    response = region["client"].post(
        "/search/batch", json={"queries": queries, "max_results": 3, "expand": True}
    )
    assert response.status_code == 200, response.text
    assert response.json()["expansion"] == batch["expansion"]
    assert [r.get("graph") for r in response.json()["results"]] == [
        r.get("graph") for r in batch["results"]
    ]


def test_the_walk_tools_take_the_exclusions_http_has(region: dict[str, Any]) -> None:
    tools, kb = region["tools"], region["kb"]
    walk = tools.get_graph_neighbors(kb, MAIN, 1, exclude_edge_types=["has_chunk"])
    assert walk["neighbors"]
    assert not any("has_chunk" in item["edge_types"] for item in walk["neighbors"])

    over_http = (
        region["client"]
        .get(
            "/graph/neighbors",
            params={"node_id": MAIN, "depth": 1, "exclude_types": "symbol", "max_nodes": 2},
        )
        .json()
    )
    over_mcp = tools.get_graph_neighbors(kb, MAIN, 1, max_nodes=2, exclude_node_types=["symbol"])
    assert [n["node_id"] for n in over_http["neighbors"]] == [
        n["node_id"] for n in over_mcp["neighbors"]
    ]
    assert len(over_mcp["neighbors"]) <= 2
    assert not any(n["node"].get("type") == "symbol" for n in over_mcp["neighbors"])

    sliced = tools.get_graph_slice(kb, MAIN, 1, exclude_edge_types=["has_chunk"])
    assert not any(link.get("type") == "has_chunk" for link in sliced["links"])


def test_the_mcp_server_publishes_expansion_and_the_slice_tool(tmp_path: Path) -> None:
    import asyncio

    pytest.importorskip("mcp")
    from pheasant.mcp_server.server import create_mcp_server

    # Paths under tmp_path: the default state_path is /state, which only a
    # root test runner can create.
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "schema",
                "state_path": str(tmp_path / "state"),
                "exports_path": str(tmp_path / "exports"),
                "workspace_root": str(tmp_path),
            },
            "sources": [],
        }
    )
    server = create_mcp_server(config)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    prompts = {prompt.name for prompt in asyncio.run(server.list_prompts())}

    assert "expand" in tools["search_context"].input_schema["properties"]
    assert "expand" in tools["search_context_batch"].input_schema["properties"]
    assert {"max_nodes", "exclude_edge_types", "exclude_node_types"} <= set(
        tools["get_graph_neighbors"].input_schema["properties"]
    )
    assert "get_graph_slice" in tools
    assert "use_pheasant_for_raw_retrieval" in prompts


# ---------------------------------------------------------------------------
# ACLs
# ---------------------------------------------------------------------------

#: A public file that imports a file only alice may read.
ACL_DOCS = {
    "app/main.py": (
        "from app.secret import launch\n\n\ndef run():\n    '''Rotation entry point.'''\n"
        "    return launch()\n",
        {"public": True},
    ),
    "app/secret.py": (
        "def launch():\n    '''Mercury launch codes rotation.'''\n    return 42\n",
        {"allow": ["user:alice"]},
    ),
}
SECRET = "file:docs:app/secret.py:branch=none"
PUBLIC_MAIN = "file:docs:app/main.py:branch=none"


class AclCodeConnector(SourceConnector):
    connector_type = "aclcode"

    def list_items(self) -> list[ConnectorItem]:
        return [
            ConnectorItem(
                identity=f"aclcode:{self.source.name}:{name}",
                relative_path=name,
                uri=f"aclcode://{name}",
                mime_type="text/x-python",
                sha256=hashlib.sha256(text.encode()).hexdigest(),
                metadata={"acl": dict(acl), "text": text},
            )
            for name, (text, acl) in sorted(ACL_DOCS.items())
        ]

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        return ConnectorPayload(
            item=item, content=str(item.metadata["text"]).encode(), mime_type="text/x-python"
        )


def _acl_tools(tmp_path: Path, *, enforced: bool) -> PheasantTools:
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: acl-expand
  state_path: {tmp_path / "state"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
security:
  acl_enforced: {str(enforced).lower()}
sources:
  - name: docs
    type: aclcode
    path: /unused
    include: []
""",
        encoding="utf-8",
    )
    tools = PheasantTools(load_config(config_path))
    tools.sync_source("acl-expand", "docs", "incremental")
    return tools


@pytest.fixture()
def acl_registry():
    connector_registry.reset_connector_registry()
    connector_registry.register_connector_class("aclcode", AclCodeConnector)
    yield
    connector_registry.reset_connector_registry()


def _main_neighbors(tools: PheasantTools, **kwargs: Any) -> list[dict[str, Any]]:
    payload = tools.search_context(
        "acl-expand",
        "rotation entry point",
        "text",
        10,
        expand={"depth": 2, "max_neighbors": 50},
        **kwargs,
    )
    hits = [r for r in payload["results"] if r.get("node_id") == PUBLIC_MAIN]
    assert hits, "the public file must be a hit for every caller"
    return hits[0]["graph"]["neighbors"]


def test_without_enforcement_the_walk_crosses_into_every_file(
    tmp_path: Path, acl_registry: None
) -> None:
    tools = _acl_tools(tmp_path, enforced=False)
    try:
        assert SECRET in {item["node_id"] for item in _main_neighbors(tools)}
    finally:
        tools.engine.close()


def test_an_expansion_withholds_what_the_caller_may_not_read(
    tmp_path: Path, acl_registry: None
) -> None:
    tools = _acl_tools(tmp_path, enforced=True)
    try:
        anonymous = _main_neighbors(tools)
        alice = _main_neighbors(tools, principal="user:alice")
    finally:
        tools.engine.close()

    hidden = {item["node_id"] for item in anonymous}
    assert SECRET not in hidden
    # Nothing that came from the secret file — its symbols, its chunks — and
    # nothing reached *through* it, whose `via` would name it.
    for item in anonymous:
        assert item.get("artifact_id") != SECRET
        assert item.get("relative_path") != "app/secret.py"
        assert item["via"] != SECRET
    assert "mercury" not in str(anonymous).lower()

    assert SECRET in {item["node_id"] for item in alice}


def test_a_node_reached_only_through_a_hidden_one_is_hidden_too() -> None:
    """Its `via` would name the hidden node — and a node id carries a path.

    Ownership alone does not cover it: `c` here is readable in its own right
    and still must not appear, because the only way the walk reached it is
    through `b`.
    """

    from pheasant.graph.simple import SimpleMultiDiGraph
    from pheasant.graph.traversal import expand

    graph = SimpleMultiDiGraph()
    for node in ("a", "b", "c", "d"):
        graph.add_node(node, type="file", label=node)
    graph.add_edge("a", "b", type="imports")
    graph.add_edge("b", "c", type="imports")
    graph.add_edge("a", "d", type="imports")

    def hide_b(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [item for item in found if item["node_id"] != "b"]

    expanded = expand(graph, ["a"], depth=2, max_neighbors=10, admit=hide_b)

    assert [n["node_id"] for n in expanded["a"]["neighbors"]] == ["d"]
