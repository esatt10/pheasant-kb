"""Each trace step names the model it used.

A region can run three models in one answer — an embedding model for the
vector arm, a decision model for the judgements, a chat model for the writing
— and a trace that showed only seconds and tokens could not say which one a
slow or expensive step was waiting on.

1. The model is recorded where a call is made (the chat wires and the
   Decider), so a step has it because its node called something, not because
   the node remembered to copy a name.
2. An embedding model is named without counting a provider call: the query
   cache can serve the vectors, and ``provider_calls`` keeps meaning requests.
3. A node that used several models names them all, in first-use order.
4. The answer and its stream carry the field through one serializer.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pheasant.assistant import decisions
from pheasant.assistant import providers as providers_module
from pheasant.assistant.providers import collect_token_usage, note_model_call, note_model_used

QUESTION = "How do I enable staged retrieval?"


@pytest.fixture(autouse=True)
def _clean_circuit():
    decisions.reset_circuit()
    yield
    decisions.reset_circuit()


def test_a_node_names_its_models_in_first_use_order_without_inventing_calls() -> None:
    with collect_token_usage() as usage:
        note_model_used("text-embedding-3-small")
        note_model_call("judge-1")
        note_model_call("gpt-6-luna")
        note_model_call("judge-1")

    assert usage.models == ["text-embedding-3-small", "judge-1", "gpt-6-luna"]
    assert usage.model == "text-embedding-3-small, judge-1, gpt-6-luna"
    assert usage.calls == 3, "naming the embedding model is not a provider call"


def test_a_node_that_used_no_model_names_none() -> None:
    with collect_token_usage() as usage:
        note_model_call()

    assert usage.model is None
    assert usage.calls == 1


def test_the_chat_wire_records_the_model_it_was_asked_for(monkeypatch) -> None:
    def fake(url, payload, headers, timeout):
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }

    monkeypatch.setattr(providers_module, "_http_json", fake)
    with collect_token_usage() as usage:
        providers_module.complete(
            "openai", api_key="sk-test", model="gpt-6-luna", system="s", prompt="p"
        )

    assert usage.model == "gpt-6-luna"


# ------------------------------------------------------------ end to end


class _Vector:
    embedder = SimpleNamespace(model="text-embedding-3-small")

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        return [[1.0] for _ in queries]


class _Search:
    vector = _Vector()

    def search_context(self, _kb, query, _mode, _limit, _source_name, **_kwargs):
        node_id = "file:kb:docs/config.md"
        hit = {
            "node_id": node_id,
            "chunk_id": f"{node_id}#c0",
            "type": "chunk",
            "title": "docs/config.md",
            "relative_path": "docs/config.md",
            "source_id": "notes",
            "score": 1.0,
            "chunks": [{"text_preview": "Set staged_retrieval: true to search once first."}],
        }
        return {"results": [hit] if query == QUESTION else [], "counts": {}}


def _wire(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    if url.endswith("/decisions"):
        return {
            "model": payload["model"],
            "answers": [
                {"type": "predicate", "name": q["name"], "probability": 0.95}
                for q in payload["questions"]
            ],
            "usage": {"input_tokens": 40, "output_tokens": 1},
        }
    return {
        "choices": [{"message": {"content": "Set it [1]."}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _answer(monkeypatch, stream: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    pytest.importorskip("langgraph")
    from pheasant.assistant.answering import answer_question
    from pheasant.assistant.workflows import step_payload
    from pheasant.config.schema import PheasantConfig

    monkeypatch.setattr(providers_module, "_http_json", _wire)
    config = PheasantConfig.model_validate(
        {
            "pheasant": {"defaults": "standalone"},
            "assistant": {
                "provider": "openai",
                "model": "gpt-6-luna",
                "workflow": "agentic",
                "decisions": {"mode": "beta", "model": "judge-1"},
                "workflow_options": {
                    "agentic": {
                        "staged_retrieval": True,
                        "combine_grade_and_answer": True,
                        "retrieval_modes": ["hybrid"],
                        "expand_graph": False,
                    }
                },
            },
        }
    )
    return answer_question(
        QUESTION,
        search=_Search(),
        knowledge_base="kb",
        config=config,
        env={"OPENAI_API_KEY": "sk-test"},
        on_step=None if stream is None else (lambda step: stream.append(step_payload(step))),
    )


def test_every_step_names_the_model_it_used_and_only_that(monkeypatch) -> None:
    payload = _answer(monkeypatch)

    models = {step["name"]: step["model"] for step in payload["steps"]}
    assert models["retrieve"] == "text-embedding-3-small"
    assert models["decide"] == "judge-1"
    assert models["synthesize"] == "gpt-6-luna"
    assert models["accept"] == "judge-1"
    for name in ("history_rewrite", "classify", "expand", "verify"):
        assert models[name] is None, f"{name} called no model"
    retrieve = next(step for step in payload["steps"] if step["name"] == "retrieve")
    assert retrieve["provider_calls"] == 0, "an embedding is named, never counted as a call"


def test_the_stream_reports_each_step_exactly_as_the_answer_does(monkeypatch) -> None:
    streamed: list[dict[str, Any]] = []
    payload = _answer(monkeypatch, streamed)

    by_name = {step["name"]: step for step in payload["steps"]}
    assert streamed, "the workflow published its steps as they finished"
    for step in streamed:
        assert step["model"] == by_name[step["name"]]["model"]
