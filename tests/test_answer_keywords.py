"""First-word keywords: the reader says what kind of answer they want.

Four claims, each with a way to fail:

1. **Only the first word counts** (several may lead a message); ``@pheasant``
   still counts anywhere; a leading ``@word`` that is no keyword stays in the
   question and is reported, and an email address is never a keyword.
2. **A keyword reaches the answer and nothing else.** The shape keyword adds
   its FORMAT instruction to the system prompt the model is sent, the keyword
   itself is not in the question that is searched or written about, and a
   length or picture keyword wins over the request's own pin and says so.
3. **``@search`` is deterministic**: the ranked hits as a table, every row a
   citation, no model call, no history rewrite, identical on both surfaces and
   on a second asking, and ``page N`` goes deeper.
4. **The catalog is the region's own**: ``/assistant/status`` lists what this
   region answers, a keyword alone gets help, and ``assistant.keywords: false``
   leaves the words alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.assistant import keywords
from pheasant.assistant.llm import LLM
from pheasant.config.schema import PheasantConfig
from pheasant.mcp_server.tools import PheasantTools

# ---------------------------------------------------------------------------
# 1. Reading
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "used", "text", "axes"),
    [
        (
            "@table the services and their ports",
            ["@table"],
            "the services and their ports",
            {"form": "table"},
        ),
        ("@TABLE: ports", ["@table"], "ports", {"form": "table"}),
        (
            "@detailed @table compare a and b",
            ["@detailed", "@table"],
            "compare a and b",
            {"form": "table", "depth": "long"},
        ),
        (
            "@brief how does rotation work",
            ["@brief"],
            "how does rotation work",
            {"form": "brief", "depth": "short"},
        ),
        ("@bullets prerequisites", ["@list"], "prerequisites", {"form": "list"}),
        ("@overview the pipeline", ["@overview"], "the pipeline", {"depth": "medium"}),
        ("@timeline releases", ["@timeline"], "releases", {"visual": "timeline"}),
        ("@flowchart deploy", ["@flow"], "deploy", {"visual": "flow"}),
        ("@diagram sync", ["@diagram"], "sync", {"visual": "diagram"}),
        ("@search credential rotation", ["@search"], "credential rotation", {"search": True}),
        ("@doc deploy.md", ["@doc"], "deploy.md", {}),
        # Not the first word: not a keyword.
        ("show it as @table", [], "show it as @table", {}),
        ("mail ops@example.com about it", [], "mail ops@example.com about it", {}),
        # `@pheasant` stops the reading and stays in the text for the inventory.
        ("@table @pheasant list sources", ["@table"], "@pheasant list sources", {"form": "table"}),
    ],
)
def test_only_leading_keywords_are_read(
    question: str, used: list[str], text: str, axes: dict[str, Any]
) -> None:
    read = keywords.read(question)
    assert list(read.used) == used
    assert read.text == text
    for axis, value in axes.items():
        assert getattr(read, axis) == value


def test_an_unknown_leading_word_is_reported_and_kept() -> None:
    read = keywords.read("@tabel the ports")
    assert read.used == () and read.unknown == "@tabel"
    assert read.text == "@tabel the ports"


def test_index_shorthands_are_spelled_as_pheasant() -> None:
    assert keywords.inventory_form("@doc deploy.md") == "@pheasant document deploy.md"
    assert keywords.inventory_form("@links between a and b") == "@pheasant links between a and b"
    assert keywords.inventory_form("@more") == "@pheasant more"
    assert keywords.inventory_form("@table ports") == "@table ports"


def test_table_is_the_written_shape_and_every_other_diagram_kind_is_a_keyword() -> None:
    from pheasant.assistant.visual_specs import KINDS

    assert keywords.read("@table x").form == "table"
    for kind in KINDS:
        if kind != "table":
            assert keywords.read(f"@{kind} x").visual == kind, kind
    assert set(keywords.FORM_INSTRUCTIONS) == {
        k.value for k in keywords.KEYWORDS if k.kind == "form"
    }


# ---------------------------------------------------------------------------
# 2. Through the assistant
# ---------------------------------------------------------------------------

CORPUS = {
    "notes/deploy.md": "# Deploy\n\nThe gateway rotates credentials nightly at 02:00.\n",
    "notes/search.md": "# Search\n\nHybrid retrieval fuses three arms.\n",
    "notes/vault.md": "# Vault\n\nRestart the vault after the gateway rotates credentials.\n",
}


def _region(root: Path, **assistant: Any) -> dict[str, Any]:
    workspace = root / "workspace"
    for relative, text in CORPUS.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "keywords",
                "state_path": str(root / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(root / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "storage": {"graph_snapshots": False},
            "assistant": {"provider": "none", **assistant},
            "sources": [
                {"name": "notes", "type": "markdown_folder", "path": str(workspace / "notes")}
            ],
        }
    )
    tools = PheasantTools(config)
    tools.engine.sync_source("notes", "full")
    tools.engine.reload_graph()
    client = TestClient(create_app(config, config_path=str(root / "pheasant.yaml")))
    return {"config": config, "tools": tools, "client": client, "kb": config.knowledge_base_id}


@pytest.fixture(scope="module")
def region(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _region(tmp_path_factory.mktemp("keywords"))


@dataclass
class _Model(LLM):
    """A connected model that records what it is sent. A dataclass because
    ``LLM.with_deadline`` copies it with ``replace``; the copy shares ``sent``."""

    sent: list[tuple[str, str]] = field(default_factory=list)

    def complete(self, system, prompt, **kwargs):  # type: ignore[override]
        self.sent.append((system, prompt))
        return "The gateway rotates credentials nightly [1]."


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> _Model:
    connected = _Model(provider="openai", api_key="k", model="m")
    monkeypatch.setattr("pheasant.assistant.answering.resolve_llm", lambda *a, **k: connected)
    return connected


@pytest.mark.parametrize("form", ["table", "list", "steps", "compare", "quotes", "brief"])
def test_a_shape_keyword_reaches_the_prompt_and_only_the_prompt(
    region: dict[str, Any], model: _Model, form: str
) -> None:
    question = "how does the gateway rotate credentials?"
    payload = region["tools"].ask_knowledge_base(
        region["kb"], f"@{form} {question}", workflow="simple"
    )

    system, prompt = model.sent[-1]
    assert system.endswith(keywords.FORM_INSTRUCTIONS[form])
    assert f"Question: {question}" in prompt
    assert "@" + form not in prompt, "the keyword is not part of the question"
    assert payload["question"] == f"@{form} {question}"
    assert payload["keywords"]["used"] == [f"@{form}"]
    assert payload["route"]["form"] == form
    assert payload["route"]["decided_by"]["form"] == "keyword"


def test_no_keyword_sends_the_prompt_it_always_sent(region: dict[str, Any], model: _Model) -> None:
    from pheasant.assistant.chat import system_prompt_for

    payload = region["tools"].ask_knowledge_base(
        region["kb"], "how does the gateway rotate credentials?", workflow="simple"
    )
    assert model.sent[-1][0] == system_prompt_for("knowledge", "short", figures=False)
    assert "keywords" not in payload and "form" not in payload["route"]


def test_a_length_keyword_wins_over_the_request_pin(region: dict[str, Any], model: _Model) -> None:
    payload = region["tools"].ask_knowledge_base(
        region["kb"], "@overview credential rotation", workflow="simple", depth="short"
    )
    assert payload["route"]["depth"] == "medium"
    assert payload["route"]["decided_by"]["depth"] == "keyword"


def test_a_picture_keyword_pins_the_visual_and_its_shape(region: dict[str, Any]) -> None:
    payload = region["tools"].ask_knowledge_base(region["kb"], "@timeline credential rotation")
    assert payload["route"]["visual"] == "diagram"
    assert payload["route"]["shape"] == "timeline"
    assert payload["route"]["decided_by"]["visual"] == "keyword"


def test_an_unknown_keyword_is_searched_as_written_and_reported(region: dict[str, Any]) -> None:
    payload = region["tools"].ask_knowledge_base(region["kb"], "@tabel gateway credentials")
    assert payload["keywords"] == {
        "used": [],
        "unknown": "@tabel",
        "form": None,
        "depth": None,
        "visual": None,
        "search": False,
    }


# ---------------------------------------------------------------------------
# 3. @search
# ---------------------------------------------------------------------------


def test_search_lists_the_hits_without_a_model(region: dict[str, Any], model: _Model) -> None:
    question = "@search gateway rotates credentials"
    over_mcp = region["tools"].ask_knowledge_base(
        region["kb"], question, history=[{"question": "q", "answer": "a"}]
    )
    response = region["client"].post("/assistant/chat", json={"question": question})
    over_http = response.json()

    assert model.sent == [], "no rewrite, no answer: nothing asks the model"
    assert over_mcp["mode"] == "search" and over_mcp["route"]["intent"] == "search"
    assert over_mcp["answer"] == over_http["answer"]
    assert (
        over_mcp["answer"] == region["tools"].ask_knowledge_base(region["kb"], question)["answer"]
    )
    rows = re.findall(r"^\| \[(\d+)\] \| `notes/(\w+\.md)` \|", over_mcp["answer"], re.MULTILINE)
    assert rows and [int(n) for n, _ in rows] == [c["index"] for c in over_mcp["citations"]]
    assert {path for _, path in rows} >= {"deploy.md", "vault.md"}
    assert all(c["used"] for c in over_mcp["citations"])
    assert over_mcp["steps"][0]["detail"].startswith("skipped: @search")


def test_search_pages_go_deeper(region: dict[str, Any]) -> None:
    from pheasant.assistant import search_answer

    assert search_answer.split_page("rotation page 2") == ("rotation", 2)
    assert search_answer.split_page("page") == ("page", 1)
    deep = region["tools"].ask_knowledge_base(region["kb"], "@search gateway page 9")
    assert "past the last of them" in deep["answer"]


def test_search_quotes_passages_rather_than_rendering_them() -> None:
    from pheasant.assistant.search_answer import render

    text = render(
        "q",
        [
            {
                "index": 1,
                "source_id": "s",
                "relative_path": "a.md",
                "snippet": "See [the guide](../x.md) and **this** [2].",
                "score": 0.5,
            }
        ],
        total=1,
        page=1,
        depth=10,
    )
    assert "](../x.md)" not in text and "[2]" not in text
    assert "See the guide and \\*\\*this\\*\\*" in text


# ---------------------------------------------------------------------------
# 4. The catalog
# ---------------------------------------------------------------------------


def test_status_lists_the_keywords_this_region_answers(tmp_path: Path, region: dict) -> None:
    listed = region["client"].get("/assistant/status").json()["keywords"]
    names = {row["keyword"] for row in listed}
    assert {"@pheasant", "@table", "@doc", "@search", "@detailed"} <= names
    assert [row["anywhere"] for row in listed if row["keyword"] == "@pheasant"] == [True]

    settings = PheasantConfig.model_validate(
        {"assistant": {"inventory": {"mode": "off"}, "keywords": False}}
    ).assistant
    assert keywords.keyword_catalog(settings) == []


def test_a_keyword_alone_gets_help(region: dict[str, Any]) -> None:
    payload = region["tools"].ask_knowledge_base(region["kb"], "@table")
    assert payload["route"]["intent"] == "inventory"
    assert "add a question after @table" in payload["answer"]
    assert "| `@steps` |" in payload["answer"], "help lists the keywords"


def test_keywords_off_leaves_the_words_alone(tmp_path: Path) -> None:
    off = _region(tmp_path, keywords=False)
    payload = off["tools"].ask_knowledge_base(off["kb"], "@table gateway credentials")
    assert "keywords" not in payload
    assert payload["route"]["intent"] != "inventory"


def test_the_ui_knows_every_keyword_kind() -> None:
    from tests.conftest import REPO_ROOT

    types = (REPO_ROOT / "ui" / "src" / "api" / "types.ts").read_text(encoding="utf-8")
    union = types.split("export interface AnswerKeyword", 1)[1].split("}", 1)[0]
    for kind in {keyword.kind for keyword in keywords.KEYWORDS}:
        assert f'"{kind}"' in union, kind


def test_the_reference_cards_phrases_are_still_read_from_the_index() -> None:
    """The chat's quick-reference card lists `@pheasant` phrasings with `<…>`
    slots. Filled in, every one must still be answered from the index: a card
    that advertises a phrasing the reader no longer understands is worse than
    no card."""

    from pheasant.assistant import inventory
    from tests.conftest import REPO_ROOT

    card = (REPO_ROOT / "ui" / "src" / "chat" / "KeywordReference.tsx").read_text(encoding="utf-8")
    block = card.split("export const PHEASANT_PHRASES", 1)[1].split("];", 1)[0]
    phrases = re.findall(r'phrase: "([^"]+)"', block)
    assert len(phrases) >= 10
    slots = {"<name>": "notes", "<source>": "code", "<path>": "deploy.md", "<text>": "deploy"}
    for phrase in phrases:
        filled = phrase
        for slot, value in slots.items():
            filled = filled.replace(slot, value, 1)
        filled = filled.replace("<source>", "notes")
        assert "<" not in filled, phrase
        read = inventory.read_question(filled, sources=["notes", "code"])
        assert read is not None and read.action != "help", phrase
