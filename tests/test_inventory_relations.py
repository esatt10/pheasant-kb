"""One source, one document, the links between documents, and paging through them.

Five claims, each with a way to fail:

1. **The links are the graph's own.** A resolved Markdown link and a resolved
   Python import between two indexed documents are listed, per source pair and
   edge type, in both directions; a link to something the region does not hold
   is an unresolved reference, never a document; structure (``contains``,
   ``has_chunk``) is never a link.
2. **Every graph backend answers alike.** The resident graph and the row
   backend (``SqlGraph``, which answers incoming edges off the target index)
   return identical results.
3. **The rules read the narrower questions and still refuse the content
   ones**, on a labelled set; ``@pheasant`` reads them loosely and a document
   it cannot find is said, not searched.
4. **A listing pages**: ``page N`` reads back, the answer names the next
   page's question, and ``@pheasant more`` continues from the conversation's
   last listing without the region holding any chat state.
5. **ACL holds**: a document the caller may not read is not described, not
   counted, and not named as the far end of a link.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.assistant import inventory
from pheasant.config.schema import PheasantConfig
from pheasant.graph.sql import SqlGraph
from pheasant.mcp_server.tools import PheasantTools
from pheasant.services import ServiceContext, inventory_detail
from pheasant.services.errors import DocumentNotFound, ServiceError

SOURCES = ["notes", "code"]

CORPUS = {
    "notes/deploy.md": (
        "# Deploy\n\n## Steps\n\nThe gateway rotates credentials nightly. See "
        "[sync](sync.py), [rotation](runbooks/rotation.md) and "
        "[the vendor docs](https://example.com/vendor).\n"
    ),
    "notes/runbooks/rotation.md": "# Rotation\n\nRotation runs at 02:00 UTC.\n",
    "notes/search.md": "# Search\n\nHybrid retrieval fuses three arms.\n",
    "code/sync.py": "def sync():\n    return 'sync'\n",
    "code/main.py": "from sync import sync\n\n\ndef main():\n    return sync()\n",
}


def _region(root: Path, **overrides: Any) -> dict[str, Any]:
    workspace = root / "workspace"
    for relative, text in CORPUS.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    raw: dict[str, Any] = {
        "pheasant": {
            "name": "relations",
            "state_path": str(root / "state"),
            "workspace_root": str(workspace),
            "exports_path": str(root / "exports"),
        },
        "server": {"host": "127.0.0.1"},
        "storage": {"graph_snapshots": False},
        "assistant": {"provider": "none", "inventory": {"max_items": 2}},
        "sources": [
            {"name": "notes", "type": "markdown_folder", "path": str(workspace / "notes")},
            {
                "name": "code",
                "type": "repository",
                "path": str(workspace / "code"),
                "include": ["**/*.py"],
            },
        ],
    }
    for section, values in overrides.items():
        raw.setdefault(section, {}).update(values)
    config = PheasantConfig.model_validate(raw)
    tools = PheasantTools(config)
    for source in ("notes", "code"):
        tools.engine.sync_source(source, "full")
    tools.engine.reload_graph()
    client = TestClient(create_app(config, config_path=str(root / "pheasant.yaml")))
    return {"config": config, "tools": tools, "client": client, "kb": config.knowledge_base_id}


@pytest.fixture(scope="module")
def region(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _region(tmp_path_factory.mktemp("relations"))


def _pairs(listed: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (
            f"{link['from']['source']}/{link['from']['path']}",
            ",".join(link["edge_types"]),
            f"{link['to']['source']}/{link['to']['path']}",
        )
        for link in listed["links"]
    }


# ---------------------------------------------------------------------------
# 1. The links are the graph's own
# ---------------------------------------------------------------------------


def test_links_are_document_to_document_edges(region: dict[str, Any]) -> None:
    listed = region["tools"].list_document_links(region["kb"])

    assert _pairs(listed) == {
        ("code/main.py", "imports", "code/sync.py"),
        ("notes/deploy.md", "references", "code/sync.py"),
        ("notes/deploy.md", "references", "notes/runbooks/rotation.md"),
    }
    assert {(r["from_source"], r["to_source"], r["edge_type"]) for r in listed["summary"]} == {
        ("code", "code", "imports"),
        ("notes", "code", "references"),
        ("notes", "notes", "references"),
    }
    crossing = region["tools"].list_document_links(region["kb"], cross_source_only=True)
    assert _pairs(crossing) == {("notes/deploy.md", "references", "code/sync.py")}
    between = region["tools"].list_document_links(
        region["kb"], source_name="code", other_source="notes"
    )
    assert _pairs(between) == _pairs(crossing), "between is either direction"
    imports = region["tools"].list_document_links(region["kb"], edge_types=["imports"])
    assert _pairs(imports) == {("code/main.py", "imports", "code/sync.py")}


def test_one_documents_links_page_like_any_listing(region: dict[str, Any]) -> None:
    tools, kb = region["tools"], region["kb"]
    into = tools.list_document_links(kb, document="sync.py", direction="in")
    assert _pairs(into) == {
        ("code/main.py", "imports", "code/sync.py"),
        ("notes/deploy.md", "references", "code/sync.py"),
    }
    assert tools.list_document_links(kb, document="sync.py", direction="in", limit=1)["pagination"][
        "has_more"
    ]
    out = tools.list_document_links(kb, document="notes/deploy.md", direction="out")
    assert {to for _f, _e, to in _pairs(out)} == {"code/sync.py", "notes/runbooks/rotation.md"}
    both = tools.list_document_links(kb, document="sync.py")
    assert _pairs(both) == _pairs(into)

    with pytest.raises(ServiceError, match="matches 2 documents"):
        tools.list_document_links(kb, document=".py")
    with pytest.raises(DocumentNotFound):
        tools.list_document_links(kb, document="nowhere.md")
    with pytest.raises(ServiceError, match="direction needs document"):
        tools.list_document_links(kb, direction="in")


def test_a_document_is_described_with_its_links_both_ways(region: dict[str, Any]) -> None:
    described = region["tools"].describe_document(region["kb"], "sync.py")

    assert described["document"]["source"] == "code"
    assert [s["name"] for s in described["symbols"]["items"]] == ["sync"]
    assert described["links_to"]["total"] == 0
    linked_from = {
        (i["document"]["source"], i["document"]["path"], tuple(i["edge_types"]), i["cross_source"])
        for i in described["linked_from"]["items"]
    }
    assert linked_from == {
        ("code", "main.py", ("imports",), False),
        ("notes", "deploy.md", ("references",), True),
    }

    deploy = region["tools"].describe_document(region["kb"], "notes/deploy.md")
    assert deploy["links_to"]["total"] == 2
    refs = {r["reference"] for r in deploy["unresolved_references"]["items"]}
    assert "https://example.com/vendor" in refs
    assert "sync.py" not in refs and "runbooks/rotation.md" not in refs, (
        "resolved is not unresolved"
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("deploy.md", "notes/deploy.md"),
        ("DEPLOY.MD", "notes/deploy.md"),
        ("notes/runbooks/rotation.md", "notes/runbooks/rotation.md"),
        ("runbooks/rotation.md", "notes/runbooks/rotation.md"),
        ("rotation", "notes/runbooks/rotation.md"),
        ("./code/main.py", "code/main.py"),
    ],
)
def test_a_document_is_found_by_any_reasonable_path(
    region: dict[str, Any], path: str, expected: str
) -> None:
    found = region["tools"].describe_document(region["kb"], path)["document"]
    assert f"{found['source']}/{found['path']}" == expected


def test_an_ambiguous_path_lists_candidates_and_a_missing_one_refuses(
    region: dict[str, Any],
) -> None:
    several = region["tools"].describe_document(region["kb"], ".py")
    assert several["document"] is None
    assert {d["path"] for d in several["candidates"]} == {"main.py", "sync.py"}
    with pytest.raises(DocumentNotFound, match="Unknown document: nowhere.md"):
        region["tools"].describe_document(region["kb"], "nowhere.md")


def test_a_source_is_described_with_links_out_and_in(region: dict[str, Any]) -> None:
    notes = region["tools"].describe_source(region["kb"], "notes")
    assert notes["totals"]["documents"] == 3
    assert {(r["source"], r["edge_type"], r["links"]) for r in notes["links"]["outgoing"]} == {
        ("code", "references", 1),
        ("notes", "references", 1),
    }
    code = region["tools"].describe_source(region["kb"], "code")
    assert {(r["source"], r["edge_type"]) for r in code["links"]["incoming"]} == {
        ("notes", "references")
    }
    with pytest.raises(ServiceError, match="Unknown source: nowhere"):
        region["tools"].describe_source(region["kb"], "nowhere")


# ---------------------------------------------------------------------------
# 2. Every graph backend answers alike
# ---------------------------------------------------------------------------


def test_the_row_backend_answers_like_the_resident_graph(region: dict[str, Any]) -> None:
    tools = region["tools"]
    resident = tools.services
    stored = ServiceContext(
        config=resident.config,
        state=resident.state,
        searcher=resident.searcher,
        graph=SqlGraph(resident.state, region["kb"]),
    )
    assert callable(getattr(stored.graph, "in_edges_batch", None))

    for context in (resident, stored):
        assert inventory_detail.links(context, inventory_detail.LinksRequest()) == (
            inventory_detail.links(resident, inventory_detail.LinksRequest())
        )
        for path in ("sync.py", "deploy.md"):
            assert inventory_detail.document(context, path) == inventory_detail.document(
                resident, path
            )
        for name in SOURCES:
            assert inventory_detail.source(context, name) == inventory_detail.source(resident, name)


def test_a_graph_with_no_in_edge_lookup_scans_and_agrees(region: dict[str, Any]) -> None:
    """The graph service's proxy offers neither batch method; the fallback is a
    scan of every document's out-edges, slower and identical."""

    graph = region["tools"].services.graph

    class OutOnly:
        def out_edges(self, node_id: str) -> list:
            return graph.out_edges(node_id)

        nodes = graph.nodes

    resident = region["tools"].services
    bare = ServiceContext(
        config=resident.config, state=resident.state, searcher=resident.searcher, graph=OutOnly()
    )
    expected = inventory_detail.document(resident, "sync.py")
    found = inventory_detail.document(bare, "sync.py")
    assert found["linked_from"] == expected["linked_from"]


# ---------------------------------------------------------------------------
# 3. Reading the narrower questions
# ---------------------------------------------------------------------------

LABELLED = [
    # (question, action, filters) — action None means "answer it by retrieval"
    ("tell me about the notes source", "source", {"source_name": "notes"}),
    ("what does the code repo contain", "source", {"source_name": "code"}),
    ("what links to deploy.md", "document", {"path": "deploy.md", "direction": "in"}),
    ("which files import sync.py?", "document", {"path": "sync.py", "direction": "in"}),
    ("what does main.py import", "document", {"path": "main.py", "direction": "out"}),
    ("backlinks to runbooks/rotation.md", "document", {"direction": "in"}),
    (
        "links between notes and code",
        "links",
        {"source_name": "notes", "other_source": "code"},
    ),
    (
        "how are the notes and code sources related?",
        "links",
        {"source_name": "notes", "other_source": "code"},
    ),
    ("how is notes related to code", "links", {"other_source": "code"}),
    ("list cross-source links", "links", {"cross_source_only": True}),
    ("how are my sources connected", "links", {"cross_source_only": True}),
    ("list documents in notes page 2", "documents", {"source_name": "notes", "page": 2}),
    # About the content, however close the words come.
    ("tell me about the code", None, {}),
    ("show me the source code", None, {}),
    ("what does it import", None, {}),
    ("what links to the auth module", None, {}),
    ("which files mention deploy.md", None, {}),
    ("how are credentials and tokens related", None, {}),
    ("what are the links between rotation and downtime", None, {}),
    ("summarize the notes source", None, {}),
    ("what is on page 2 of the runbook", None, {}),
]


@pytest.mark.parametrize(("question", "action", "expected"), LABELLED)
def test_the_rules_read_the_narrower_questions(
    question: str, action: str | None, expected: dict[str, Any]
) -> None:
    read = inventory.read_question(question, sources=SOURCES)
    assert (read.action if read else None) == action, question
    for key, value in expected.items():
        assert getattr(read, key) == value, (question, key)


@pytest.mark.parametrize(
    ("question", "action", "expected"),
    [
        ("@pheasant notes", "source", {"source_name": "notes"}),
        ("@pheasant source code", "source", {"source_name": "code"}),
        ("@pheasant deploy.md", "document", {"path": "deploy.md"}),
        ("@pheasant document runbooks", "document", {"path": "runbooks"}),
        ("@pheasant links", "links", {}),
        ("@pheasant links in notes", "links", {"source_name": "notes"}),
        ("@pheasant imports in code", "links", {"source_name": "code", "edge_types": ("imports",)}),
        ("@pheasant links of deploy.md", "document", {"path": "deploy.md"}),
        ("@pheasant links to deploy.md", "links", {"path": "deploy.md", "direction": "in"}),
        (
            "@pheasant links from code/sync.py",
            "links",
            {"path": "code/sync.py", "direction": "out"},
        ),
        ("@pheasant more", "more", {}),
        ("@pheasant list pdfs page 3", "documents", {"page": 3, "extensions": (".pdf",)}),
        # A one-word command is a command, even if a source shares the word.
        ("@pheasant sync", "sync", {}),
    ],
)
def test_the_keyword_reads_them_loosely(question: str, action: str, expected: dict) -> None:
    read = inventory.read_question(question, sources=[*SOURCES, "sync"])
    assert read is not None and read.action == action and read.trigger == "keyword"
    for key, value in expected.items():
        assert getattr(read, key) == value


def _ask(region: dict[str, Any], question: str, **body: Any) -> tuple[dict, dict]:
    over_mcp = region["tools"].ask_knowledge_base(region["kb"], question, **body)
    response = region["client"].post("/assistant/chat", json={"question": question, **body})
    assert response.status_code == 200, response.text
    return over_mcp, response.json()


def test_chat_answers_are_the_tools_answers_on_both_surfaces(region: dict[str, Any]) -> None:
    for question, tool, call in (
        ("@pheasant source notes", "describe_source", ("describe_source", ("notes",), {})),
        ("what links to sync.py", "describe_document", ("describe_document", ("sync.py",), {})),
        (
            "links between notes and code",
            "list_document_links",
            (
                "list_document_links",
                (),
                {"source_name": "notes", "other_source": "code", "limit": 2},
            ),
        ),
    ):
        over_mcp, over_http = _ask(region, question)
        name, args, kwargs = call
        expected = getattr(region["tools"], name)(region["kb"], *args, **kwargs)
        for payload in (over_mcp, over_http):
            assert payload["route"]["intent"] == "inventory", question
            assert payload["inventory"]["tool"] == tool
            assert payload["inventory"]["result"] == expected
            assert [s["name"] for s in payload["steps"]][-1] == "inventory"
        assert over_mcp["answer"] == over_http["answer"]


def test_a_missing_document_is_said_with_the_keyword_and_searched_without(
    region: dict[str, Any],
) -> None:
    said, _ = _ask(region, "@pheasant document nowhere.md")
    assert said["route"]["intent"] == "inventory"
    assert "No document matches “nowhere.md”" in said["answer"]

    searched, _ = _ask(region, "what links to nowhere.md")
    assert searched["route"]["intent"] != "inventory"
    assert any("answered by searching" in step["detail"] for step in searched["steps"])


def test_the_document_answer_reads_like_the_question(region: dict[str, Any]) -> None:
    incoming, _ = _ask(region, "what links to sync.py")
    assert "It is linked from **2 documents**." in incoming["answer"]
    assert "| notes | `deploy.md` | references |" in incoming["answer"]
    assert "| code | `main.py` | imports |" in incoming["answer"]


# ---------------------------------------------------------------------------
# 4. Paging
# ---------------------------------------------------------------------------


def test_a_listing_pages_and_names_the_next_question(region: dict[str, Any]) -> None:
    first, over_http = _ask(region, "@pheasant list documents")
    page = first["inventory"]["page"]
    assert page == over_http["inventory"]["page"]
    assert (page["number"], page["size"], page["total"], page["pages"]) == (1, 2, 5, 3)
    assert page["next_question"] == "@pheasant list documents page 2"
    assert page["previous_question"] is None
    assert page["endpoint"] == "/documents"
    assert "`@pheasant list documents page 2`" in first["answer"]

    second, _ = _ask(region, page["next_question"])
    assert second["inventory"]["page"]["number"] == 2
    assert second["inventory"]["result"]["pagination"]["offset"] == 2
    assert "showing 3–4" in second["answer"]

    # The endpoint the UI pages with returns the same rows as the question.
    listed = (
        region["client"]
        .get(page["endpoint"], params={**page["params"], "offset": 2, "limit": 2})
        .json()
    )
    assert listed["documents"] == second["inventory"]["result"]["documents"]


def test_a_documents_backlinks_page_through_chat(region: dict[str, Any]) -> None:
    first, over_http = _ask(region, "@pheasant links to sync.py")
    assert first["inventory"]["tool"] == "list_document_links"
    assert first["inventory"]["page"] == over_http["inventory"]["page"]
    assert first["inventory"]["page"]["params"]["direction"] == "in"
    assert (first["inventory"]["page"]["pages"], first["inventory"]["page"]["next_question"]) == (
        1,
        None,
    )
    assert first["answer"].startswith("**2 links** to `sync.py`.")
    second, _ = _ask(region, "@pheasant links to sync.py page 2")
    assert len(second["inventory"]["result"]["links"]) == 0
    assert second["inventory"]["result"]["total"] == 2

    missing, _ = _ask(region, "@pheasant links to nowhere.md")
    assert "No document matches “nowhere.md”" in missing["answer"]


def test_more_continues_from_the_conversation_and_nothing_else(region: dict[str, Any]) -> None:
    turns: list[dict[str, str]] = []
    pages = []
    for question in ("@pheasant links", "@pheasant more"):
        answered = region["tools"].ask_knowledge_base(region["kb"], question, history=turns)
        pages.append(answered["inventory"]["page"]["number"])
        turns.append({"question": question, "answer": answered["answer"]})
    assert pages == [1, 2]
    assert answered["inventory"]["result"]["pagination"]["offset"] == 2

    assert (
        inventory.continue_from(
            [{"question": "list documents"}, {"question": "@more"}, {"question": "@pheasant next"}],
            sources=SOURCES,
        ).page
        == 4
    )
    # A turn that was not about the index ends the walk.
    lost = inventory.continue_from(
        [{"question": "list documents"}, {"question": "how does rotation work?"}],
        sources=SOURCES,
    )
    assert lost.action == "help" and lost.notes

    alone = region["tools"].ask_knowledge_base(region["kb"], "@pheasant more")
    assert "no earlier listing" in alone["answer"]


# ---------------------------------------------------------------------------
# 5. ACL
# ---------------------------------------------------------------------------


def test_an_acl_enforcing_region_describes_only_what_the_caller_may_read(
    tmp_path: Path,
) -> None:
    private = _region(tmp_path, security={"acl_enforced": True, "default_visibility": "private"})
    tools, kb = private["tools"], private["kb"]

    with pytest.raises(DocumentNotFound):
        tools.describe_document(kb, "sync.py")
    assert tools.list_document_links(kb)["total"] == 0
    assert tools.describe_source(kb, "code")["totals"]["documents"] == 0

    alice = tools.describe_document(kb, "sync.py", principal="user:alice")
    assert alice["linked_from"]["total"] == 2
    assert tools.list_document_links(kb, principal="user:alice")["total"] == 3


POSTGRES_DSN = __import__("os").environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()


@pytest.mark.skipif(not POSTGRES_DSN, reason="set PHEASANT_TEST_POSTGRES_DSN to run backend parity")
def test_details_agree_across_backends(tmp_path: Path) -> None:
    """The detail SQL is hand-written for both dialects: `LIKE … ESCAPE`, the
    `GROUP BY` outline, `= ANY(?)` key sets, and the target-index seek."""

    import psycopg

    with psycopg.connect(POSTGRES_DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    lite = _region(tmp_path / "sqlite")
    pg = _region(
        tmp_path / "postgres",
        storage={"backend": "postgres", "dsn_env": "PHEASANT_TEST_POSTGRES_DSN"},
    )

    def same(name: str, *args: Any, **kwargs: Any) -> None:
        a = getattr(lite["tools"], name)(lite["kb"], *args, **kwargs)
        b = getattr(pg["tools"], name)(pg["kb"], *args, **kwargs)
        for payload in (a, b):
            for key in ("document", "source"):
                if isinstance(payload.get(key), dict):
                    # The clock, and the two regions' own temp directories.
                    for varies in ("last_indexed_at", "location"):
                        payload[key].pop(varies, None)
            payload.pop("recent", None)
        assert a == b, (name, args, kwargs)

    same("list_document_links")
    same("list_document_links", cross_source_only=True)
    same("list_document_links", document="sync.py", direction="in")
    for path in ("sync.py", "deploy.md", "rotation", "s_n"):
        try:
            same("describe_document", path)
        except DocumentNotFound:
            pass
    for name in SOURCES:
        same("describe_source", name)
    stored = ServiceContext(
        config=pg["config"],
        state=pg["tools"].services.state,
        searcher=pg["tools"].services.searcher,
        graph=SqlGraph(pg["tools"].services.state, pg["kb"]),
    )
    assert inventory_detail.document(stored, "sync.py")["linked_from"]["total"] == 2
