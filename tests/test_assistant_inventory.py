"""Questions about the knowledge base itself, answered from the index.

Four claims, each with a way to fail:

1. **The rules read questions the way a person would**, on a labelled set
   holding both sides. "List all documents" is an inventory question; "what is
   this knowledge base about" and "which files mention rotation" are about the
   content and must still be searched. A false positive replaces a grounded
   answer with a listing, so the negatives matter as much as the positives.
2. **``@pheasant`` always works**, whatever the rules think, and it is never
   sent to a search. A request it cannot read gets the list of what it can.
   ``mode: keyword`` turns the rules off and leaves it working; ``mode: off``
   turns both off.
3. **The chat answer is the tool's answer.** Asked through either surface, the
   ``inventory`` block is exactly what ``list_documents`` /
   ``describe_knowledge_base`` return, and nothing was searched and no model
   was called, including the history rewrite.
4. **It degrades to retrieval, never to nothing.** A lookup that fails is
   answered by the workflow, and an ACL-enforcing region counts only what the
   caller may read.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.assistant import inventory
from pheasant.assistant.llm import LLM
from pheasant.config.schema import PheasantConfig
from pheasant.mcp_server.tools import PheasantTools

SOURCES = ["notes", "code"]

# (question, action or None for "answer it by retrieval")
LABELLED = [
    ("list all documents", "documents"),
    ("List all sources in the knowledge base", "sources"),
    ("what sources do you have?", "sources"),
    ("which sources are configured", "sources"),
    ("can you list all the documents in the kb please", "documents"),
    ("list all pdfs", "documents"),
    ("show me the markdown files in notes", "documents"),
    ("what documents are indexed", "documents"),
    ("how many documents are there?", "counts"),
    ("how many pdfs are in notes", "counts"),
    ("how many sources", "counts"),
    ("how big is the knowledge base", "counts"),
    ("what kinds of files are in the kb", "types"),
    ("list file types", "types"),
    ("show me the 5 most recent files", "recent"),
    ("recent documents", "recent"),
    ("what was added recently", "recent"),
    ("sync status", "sync"),
    ("is the knowledge base up to date", "sync"),
    ("when was the kb last synced", "sync"),
    ("what's in this knowledge base?", "overview"),
    ("kb stats", "overview"),
    # About the content, however close the words come.
    ("What is this knowledge base about?", None),
    ("Summarize the main themes across my sources.", None),
    ("list all files in the auth module", None),
    ("list the types", None),
    ("show me the code", None),
    ("which files mention rotation?", None),
    ("how many files mention credentials", None),
    ("what are the data sources for the dashboard", None),
    ("list the steps to deploy", None),
    ("what files does it touch", None),
    ("show me the docs for the deploy command", None),
    ("what file types does pheasant support", None),
    ("what sources do you have on credential rotation", None),
]


@pytest.mark.parametrize(("question", "action"), LABELLED)
def test_the_rules_read_the_labelled_set(question: str, action: str | None) -> None:
    read = inventory.read_question(question, sources=SOURCES)
    assert (read.action if read else None) == action
    if read:
        assert read.trigger == "rule"


def test_the_ui_suggestions_are_still_searched() -> None:
    """The chat panel's own starter prompts are content questions. Routing one
    of them to a listing would make the first thing a new user tries wrong."""

    import re

    from tests.conftest import REPO_ROOT

    panel = (REPO_ROOT / "ui" / "src" / "chat" / "ChatPanel.tsx").read_text(encoding="utf-8")
    block = panel.split("const SUGGESTIONS = [", 1)[1].split("];", 1)[0]
    suggestions = re.findall(r'"([^"]+)"', block)
    assert suggestions
    for suggestion in suggestions:
        if suggestion.startswith(inventory.KEYWORD):
            continue
        assert inventory.read_question(suggestion, sources=SOURCES) is None, suggestion


def test_filters_are_read_and_unknown_qualifiers_fall_through() -> None:
    read = inventory.read_question("show me the markdown files in notes", sources=SOURCES)
    assert read and read.source_name == "notes" and ".md" in read.extensions
    assert inventory.read_question("list python files", sources=SOURCES).extensions == (
        ".py",
        ".pyi",
    )
    assert inventory.read_question("show me the 3 latest docs", sources=SOURCES).limit == 3
    # "notes" is a source; "auth" is not, so the second is about the content.
    assert inventory.read_question("list documents in notes", sources=SOURCES)
    assert inventory.read_question("list documents in auth", sources=SOURCES) is None


def test_source_names_are_looked_up_only_after_a_pattern_matched() -> None:
    calls: list[int] = []

    def sources() -> list[str]:
        calls.append(1)
        return SOURCES

    assert inventory.read_question("how does credential rotation work?", sources=sources) is None
    assert calls == []
    assert inventory.read_question("list documents in notes", sources=sources).source_name
    assert calls == [1]


@pytest.mark.parametrize(
    ("question", "action", "expected"),
    [
        ("@pheasant", "help", {}),
        ("@pheasant help", "help", {}),
        ("@Pheasant, list sources", "sources", {}),
        ("@pheasant sources", "sources", {}),
        ("@pheasant pdfs in notes", "documents", {"source_name": "notes"}),
        ("@pheasant list files in auth", "documents", {"path_contains": "auth"}),
        ("@pheasant documents matching deploy", "documents", {"path_contains": "deploy"}),
        ("@pheasant stats", "overview", {}),
        ("hey @pheasant what is the sync status?", "sync", {}),
        # Unread, but still never searched: the keyword is a promise.
        ("@pheasant how does sync work?", "help", {"unread": "how does sync work"}),
    ],
)
def test_the_keyword_always_routes(question: str, action: str, expected: dict) -> None:
    read = inventory.read_question(question, sources=SOURCES)
    assert read is not None and read.trigger == "keyword"
    assert read.action == action
    for key, value in expected.items():
        assert getattr(read, key) == value


def test_modes() -> None:
    assert inventory.read_question("list all documents", mode="keyword") is None
    assert inventory.read_question("@pheasant list all documents", mode="keyword").action == (
        "documents"
    )
    assert inventory.read_question("@pheasant list all documents", mode="off") is None
    assert inventory.read_question("list all documents", mode="off") is None


def test_a_bad_mode_is_refused_at_load() -> None:
    with pytest.raises(ValueError, match="assistant.inventory.mode"):
        PheasantConfig.model_validate({"assistant": {"inventory": {"mode": "sometimes"}}})


def test_a_question_that_asks_for_a_picture_is_left_to_the_workflow() -> None:
    assert inventory.route("list all sources", mode="auto", state=None, visual="diagram") is None
    keyword = inventory.route("@pheasant list sources", mode="auto", state=None, visual="diagram")
    assert keyword is not None


# ---------------------------------------------------------------------------
# Through the assistant, on both surfaces
# ---------------------------------------------------------------------------

CORPUS = {
    "notes/deploy.md": "# Deploy\n\nThe gateway rotates credentials nightly.\n",
    "notes/search.md": "# Search\n\nHybrid retrieval fuses three arms.\n",
    "notes/runbooks/rotation.md": "# Rotation\n\nRotation runs at 02:00 UTC.\n",
    "code/sync.py": "def sync():\n    return 'sync'\n",
    "code/notes.txt": "plain text about the code\n",
}


def _region(root: Path, **overrides: Any) -> dict[str, Any]:
    workspace = root / "workspace"
    for relative, text in CORPUS.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    raw: dict[str, Any] = {
        "pheasant": {
            "name": "inventory",
            "description": "A test region.",
            "state_path": str(root / "state"),
            "workspace_root": str(workspace),
            "exports_path": str(root / "exports"),
        },
        "server": {"host": "127.0.0.1"},
        "storage": {"graph_snapshots": False},
        "assistant": {"provider": "none"},
        "sources": [
            {"name": "notes", "type": "markdown_folder", "path": str(workspace / "notes")},
            {
                "name": "code",
                "type": "repository",
                "path": str(workspace / "code"),
                "include": ["**/*.py", "**/*.txt"],
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
    return _region(tmp_path_factory.mktemp("inventory"))


def _ask(region: dict[str, Any], question: str, **body: Any) -> tuple[dict, dict]:
    over_mcp = region["tools"].ask_knowledge_base(region["kb"], question, **body)
    response = region["client"].post("/assistant/chat", json={"question": question, **body})
    assert response.status_code == 200, response.text
    return over_mcp, response.json()


def test_list_documents_is_answered_from_the_tool_on_both_surfaces(
    region: dict[str, Any],
) -> None:
    over_mcp, over_http = _ask(region, "list all documents")
    listed = region["tools"].list_documents(region["kb"], limit=50)

    for payload in (over_mcp, over_http):
        assert payload["route"]["intent"] == "inventory"
        assert payload["route"]["decided_by"]["intent"] == "rule"
        assert payload["workflow"] == "inventory"
        assert payload["inventory"]["tool"] == "list_documents"
        assert payload["inventory"]["result"] == listed
        assert payload["citations"] == []
        assert payload["termination_reason"] == "completed"
        assert payload["degraded"] is False
        # Nothing was searched: no retrieval step, only the lookup.
        assert [step["name"] for step in payload["steps"]] == [
            "history_rewrite",
            "classify",
            "inventory",
        ]
        for path in CORPUS:
            relative = path.split("/", 1)[1]
            assert relative in payload["answer"]
        assert "@pheasant" in payload["answer"], "every inventory answer names the keyword"
    assert over_mcp["answer"] == over_http["answer"]


def test_sources_counts_and_filters(region: dict[str, Any]) -> None:
    sources, _ = _ask(region, "which sources are there?")
    assert sources["inventory"]["tool"] == "describe_knowledge_base"
    assert "| notes | markdown_folder | 3 |" in sources["answer"]
    assert "| code | repository | 2 |" in sources["answer"]

    counted, _ = _ask(region, "how many markdown files are in notes?")
    assert counted["inventory"]["result"]["total"] == 3
    assert "**3 Markdown documents** in `notes`" in counted["answer"]

    typed, _ = _ask(region, "@pheasant python files")
    assert [d["path"] for d in typed["inventory"]["result"]["documents"]] == ["sync.py"]
    assert typed["route"]["decided_by"]["intent"] == "keyword"


def test_the_keyword_with_an_unreadable_request_explains_and_searches_nothing(
    region: dict[str, Any],
) -> None:
    payload, _ = _ask(region, "@pheasant how does the gateway rotate credentials?")
    assert payload["route"]["intent"] == "inventory"
    assert payload["inventory"]["action"] == "help"
    assert "nothing was searched" in payload["answer"]
    assert "`@pheasant list sources`" in payload["answer"]


def test_a_content_question_is_untouched(region: dict[str, Any]) -> None:
    payload, _ = _ask(region, "how does the gateway rotate credentials?")
    assert payload["route"]["intent"] != "inventory"
    assert "inventory" not in payload and "inventory_hint" not in payload
    assert payload["citations"]


def test_a_near_miss_carries_a_hint_and_its_answer_is_untouched(region: dict[str, Any]) -> None:
    payload, _ = _ask(region, "list the documents about credential rotation")
    assert payload["route"]["intent"] != "inventory"
    assert "inventory" not in payload
    assert inventory.KEYWORD in payload["inventory_hint"]
    assert inventory.KEYWORD not in payload["answer"]


@dataclass
class _Model(LLM):
    """A connected model that records every call and rewrites on cue.

    A dataclass, because ``LLM.with_deadline`` copies it with ``replace``; the
    copy shares ``calls``, so what the copy is asked is still recorded here.
    """

    rewrite: str = ""
    calls: list[str] = field(default_factory=list)

    def complete(self, system, prompt, **kwargs):  # type: ignore[override]
        self.calls.append(system)
        if "rewrite a follow-up" in system:
            return self.rewrite
        return "Answer [1]."


def test_with_history_the_rewrite_is_skipped_and_no_model_is_called(
    region: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _Model(provider="openai", api_key="k", model="m", rewrite="should not be asked")
    monkeypatch.setattr("pheasant.assistant.answering.resolve_llm", lambda *a, **k: model)
    history = [{"question": "how does rotation work?", "answer": "Nightly [1]."}]

    payload = region["tools"].ask_knowledge_base(region["kb"], "list all sources", history=history)

    assert payload["route"]["intent"] == "inventory"
    assert model.calls == [], "a question about the index costs no model call"
    assert payload["steps"][0]["detail"].startswith("skipped")


def test_a_follow_up_the_model_resolves_into_an_inventory_question_routes(
    region: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _Model(
        provider="openai", api_key="k", model="m", rewrite="list all markdown files in notes"
    )
    monkeypatch.setattr("pheasant.assistant.answering.resolve_llm", lambda *a, **k: model)
    history = [{"question": "list all documents", "answer": "Five documents."}]

    payload = region["tools"].ask_knowledge_base(
        region["kb"], "and only the markdown ones?", history=history
    )

    assert payload["route"]["intent"] == "inventory"
    assert payload["inventory"]["filters"]["source_name"] == "notes"
    assert payload["inventory"]["result"]["total"] == 3
    assert len(model.calls) == 1, "the rewrite, and nothing else"


def test_a_failed_lookup_is_answered_by_retrieval(
    region: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("state store went away")

    monkeypatch.setattr("pheasant.assistant.inventory_answer.lookup", broken)
    payload = region["tools"].ask_knowledge_base(region["kb"], "list all documents")

    assert payload["route"]["intent"] != "inventory"
    assert "inventory" not in payload
    assert any(
        step["name"] == "inventory" and "answered by searching" in step["detail"]
        for step in payload["steps"]
    )
    assert payload["answer"]


def test_mode_off_answers_everything_by_retrieval(tmp_path: Path) -> None:
    off = _region(tmp_path, assistant={"provider": "none", "inventory": {"mode": "off"}})
    payload = off["tools"].ask_knowledge_base(off["kb"], "@pheasant list all documents")
    assert payload["route"]["intent"] != "inventory"
    assert "inventory_hint" not in payload


def test_an_acl_enforcing_region_counts_only_what_the_caller_may_read(tmp_path: Path) -> None:
    private = _region(tmp_path, security={"acl_enforced": True, "default_visibility": "private"})
    tools, kb = private["tools"], private["kb"]

    anonymous = tools.describe_knowledge_base(kb)
    alice = tools.describe_knowledge_base(kb, principal="user:alice")
    assert anonymous["totals"]["documents"] == 0
    assert alice["totals"]["documents"] == len(CORPUS)
    assert tools.list_documents(kb)["total"] == 0
    assert tools.list_documents(kb, principal="user:alice", limit=2)["pagination"] == {
        "limit": 2,
        "offset": 0,
        "returned": 2,
        "has_more": True,
    }

    answered = tools.ask_knowledge_base(kb, "how many documents are there?")
    assert answered["inventory"]["result"]["totals"]["documents"] == 0
    answered = tools.ask_knowledge_base(kb, "how many documents are there?", principal="user:alice")
    assert answered["inventory"]["result"]["totals"]["documents"] == len(CORPUS)


def test_paths_cannot_impersonate_citation_markers() -> None:
    asked = inventory.InventoryQuestion(action="documents", trigger="rule", why="test")
    found = {
        "tool": "list_documents",
        "result": {
            "documents": [
                {
                    "id": "x",
                    "source": "notes",
                    "path": "a|b[1].md",
                    "size_bytes": 10,
                    "last_indexed_at": None,
                }
            ],
            "total": 1,
            "pagination": {"offset": 0},
        },
    }
    from pheasant.assistant.inventory_answer import render

    text = render(asked, found)
    assert "[1]" not in text
    assert "a\\|b(1).md" in text


POSTGRES_DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()


@pytest.mark.skipif(not POSTGRES_DSN, reason="set PHEASANT_TEST_POSTGRES_DSN to run backend parity")
def test_listings_agree_across_backends(tmp_path: Path) -> None:
    """The listing SQL is hand-written for both dialects: `substr`, `LOWER … LIKE
    … ESCAPE`, a `NULL`-last ordering spelled as a boolean, and patterns passed
    as parameters because a literal `%` is a psycopg placeholder."""

    import psycopg

    with psycopg.connect(POSTGRES_DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    lite = _region(tmp_path / "sqlite")
    pg = _region(
        tmp_path / "postgres",
        storage={"backend": "postgres", "dsn_env": "PHEASANT_TEST_POSTGRES_DSN"},
    )

    def shape(listed: dict) -> list[tuple]:
        return [(d["source"], d["path"], d["extension"]) for d in listed["documents"]]

    for kwargs in (
        {},
        {"extensions": ["md"]},
        {"path_contains": "RUN"},
        {"path_contains": "s_n"},  # `_` is a LIKE wildcard unless escaped
        {"source_name": "code", "limit": 1, "offset": 1},
    ):
        a = lite["tools"].list_documents(lite["kb"], **kwargs)
        b = pg["tools"].list_documents(pg["kb"], **kwargs)
        assert shape(a) == shape(b), kwargs
        assert a["total"] == b["total"] and a["pagination"] == b["pagination"]
    assert lite["tools"].list_documents(lite["kb"], path_contains="s_n")["total"] == 0
    recent = pg["tools"].list_documents(pg["kb"], order="recent")
    assert sorted(shape(recent)) == sorted(shape(lite["tools"].list_documents(lite["kb"])))

    a, b = (r["tools"].describe_knowledge_base(r["kb"]) for r in (lite, pg))
    assert a["totals"] == b["totals"] and a["by_extension"] == b["by_extension"]
    assert [(s["name"], s["documents"]) for s in a["sources"]] == [
        (s["name"], s["documents"]) for s in b["sources"]
    ]
    answered = pg["tools"].ask_knowledge_base(pg["kb"], "list all documents")
    assert answered["inventory"]["result"]["total"] == len(CORPUS)
