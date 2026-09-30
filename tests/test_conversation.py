"""Chat continuity: what earlier turns may do, and the one thing they may not.

* A question with **no history is answered exactly as before** — the prompt
  is byte-identical. Continuity is additive or it is a regression.
* A follow-up is **searched in context** (rewritten by a model when one is
  connected, joined to the previous question when not) and keeps the previous
  question's evidence at a lower weight.
* Earlier answers reach the model **with their ``[n]`` markers stripped**:
  those numbers index a citation list that no longer exists.
* Carried evidence is **re-searched, never fetched by id** — ids come from the
  caller — and re-searching must not corrupt the retriever's memo.
* Malformed history is refused with the **same text on both surfaces**.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.assistant import conversation
from pheasant.assistant.chat import build_prompt
from pheasant.assistant.llm import LLM
from pheasant.assistant.retrieval import PheasantRetriever
from pheasant.assistant.workflows import WorkflowRequest
from pheasant.assistant.workflows.simple import SimpleWorkflow
from pheasant.config.schema import PheasantConfig
from pheasant.mcp_server.tools import PheasantTools
from pheasant.services.errors import ServiceError


class _Search:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search_context(self, kb, query, mode, max_results, source_name, **kwargs):
        self.queries.append(query)
        corpus = {
            "rotation": ("docs/rotation.md", "Credentials rotate nightly at 02:00."),
            "vault": ("docs/vault.md", "Restart the vault sidecar after a rotation."),
        }
        hits = [
            {
                "node_id": f"file:kb:{path}",
                "chunk_id": f"chunk:kb:{path}:0",
                "type": "chunk",
                "title": path,
                "relative_path": path,
                "score": 2.0,
                "chunks": [{"text_preview": text}],
            }
            for word, (path, text) in corpus.items()
            if word in query.lower()
        ]
        return {"results": hits[:max_results]}


class _Recorder(LLM):
    def __init__(self, rewrite: str | None = None) -> None:
        super().__init__(provider="openai", api_key="k", model="m")
        self.prompts: list[tuple[str, str]] = []
        self.rewrite = rewrite

    def complete(self, system, prompt, **kwargs):  # type: ignore[override]
        self.prompts.append((system, prompt))
        if "rewrite a follow-up" in system:
            return self.rewrite or ""
        return "Answer [1]."


def _run(question: str, history: list, llm: Any = None, search: _Search | None = None):
    search = search or _Search()
    retriever = PheasantRetriever(search=search, knowledge_base="kb", graph=None, state=None)
    turns = conversation.normalize_history(history)
    searched, how = conversation.standalone_question(question, turns, llm)
    request = WorkflowRequest(
        question=question, history=turns, search_question=searched if how else None
    )
    return SimpleWorkflow().run(request, retriever, llm), search, how


def test_with_no_history_the_prompt_is_byte_identical() -> None:
    llm = _Recorder()
    result, _search, how = _run("how does credential rotation work?", [], llm)

    citations = result.citations
    assert citations, "the fixture must answer, or there is no prompt to compare"
    expected = build_prompt("how does credential rotation work?", citations, result.facts, {})
    assert how is None
    assert llm.prompts[-1][1] == expected
    assert "Earlier in this conversation" not in expected


def test_a_follow_up_is_joined_to_the_previous_question_offline() -> None:
    history = [{"question": "how does credential rotation work?", "answer": "Nightly [1]."}]
    result, search, how = _run("and what about the vault?", history)

    assert "searched together with the previous question" in how
    assert search.queries[0] == "how does credential rotation work? and what about the vault?"
    paths = [c["relative_path"] for c in result.citations]
    assert {"docs/rotation.md", "docs/vault.md"} <= set(paths)


def test_a_connected_model_rewrites_the_follow_up_and_sees_the_conversation() -> None:
    history = [{"question": "how does credential rotation work?", "answer": "Nightly [1][2]."}]
    llm = _Recorder(rewrite="How does the vault sidecar handle credential rotation?")
    result, search, how = _run("what about the sidecar?", history, llm)

    assert "rewritten as" in how
    assert search.queries[0] == "How does the vault sidecar handle credential rotation?"
    answering_prompt = llm.prompts[-1][1]
    assert answering_prompt.startswith("Earlier in this conversation")
    assert "A: Nightly." in answering_prompt, "old [n] markers are stripped"
    assert "Question: what about the sidecar?" in answering_prompt, "the user's words are answered"
    assert result.steps[0].detail.startswith("hybrid search for “How does the vault")


def test_carried_evidence_ranks_below_the_follow_ups_own_and_the_memo_is_untouched() -> None:
    search = _Search()
    retriever = PheasantRetriever(search=search, knowledge_base="kb", graph=None, state=None)
    own = retriever.search("vault")
    previous = retriever.search("rotation")
    before = previous[0].score

    merged = conversation.carry(own, previous)

    carried = next(p for p in merged if p.relative_path == "docs/rotation.md")
    assert carried.mode == "carried" and carried.score == before * conversation.CARRIED_WEIGHT
    assert merged[0].relative_path == "docs/vault.md"
    assert retriever.search("rotation")[0].score == before, "the memoized passage was mutated"


@pytest.mark.parametrize(
    ("question", "history", "is_follow_up"),
    [
        ("what about it?", [{"question": "q"}], True),
        ("and the second one", [{"question": "q"}], True),
        ("tell me more", [{"question": "q"}], True),
        ("how do tokens expire after rotation?", [{"question": "q"}], False),
        ("what about it?", [], False),
    ],
)
def test_follow_ups_are_recognised_only_with_history(question, history, is_follow_up) -> None:
    turns = conversation.normalize_history(history)
    assert (conversation.follow_up_reason(question, turns) is not None) is is_follow_up


def test_history_is_trimmed_to_the_recent_turns_and_long_answers_are_cut() -> None:
    turns = conversation.normalize_history(
        [{"question": f"q{i}", "answer": "x" * 5000} for i in range(10)]
    )
    assert [t.question for t in turns] == [f"q{i}" for i in range(4, 10)]
    assert len(turns[-1].answer) <= conversation.MAX_ANSWER_CHARS + 1


@pytest.mark.parametrize(
    ("history", "message"),
    [
        ("not a list", "history must be a list"),
        ([{"answer": "no question"}], "history[0] needs a non-empty question"),
        ([{"question": "q", "answer": 3}], "history[0].answer must be text"),
        ([{"question": "q"}] * 51, "history holds at most 50 turns"),
    ],
)
def test_malformed_history_is_refused_with_one_text_on_both_surfaces(
    tmp_path: Path, history: Any, message: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.md").write_text("# A\n\nrotation\n", encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "chat-history",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "sources": [{"name": "docs", "type": "markdown_folder", "path": str(workspace)}],
        }
    )
    tools = PheasantTools(config)
    client = TestClient(create_app(config, config_path=str(tmp_path / "pheasant.yaml")))

    response = client.post("/assistant/chat", json={"question": "q", "history": history})
    with pytest.raises(ServiceError) as refused:
        tools.ask_knowledge_base(config.knowledge_base_id, "q", history=history)

    if isinstance(history, str):
        # A string is not even a list: the HTTP model refuses it before the
        # service sees it. The service still refuses it for MCP.
        assert response.status_code == 422
    else:
        assert response.status_code == 422
        assert response.json()["detail"] == str(refused.value)
    assert message in str(refused.value)
