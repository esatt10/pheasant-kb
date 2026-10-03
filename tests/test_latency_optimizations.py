"""Focused correctness tests for the assistant latency candidate."""

from __future__ import annotations

import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest


def test_authorship_uses_retrieved_byline_before_later_chunk_in_same_file() -> None:
    from pheasant.assistant.workflows.agentic import _authorship_front_matter_first

    later = SimpleNamespace(node_id="file:book", chunk_id="chunk:book:chunk=0006")
    byline = SimpleNamespace(node_id="file:book", chunk_id="chunk:book:chunk=0000")
    other = SimpleNamespace(node_id="file:other", chunk_id="chunk:other:chunk=0001")

    assert _authorship_front_matter_first("Who wrote this book?", [later, byline, other]) == [
        byline,
        later,
        other,
    ]
    assert _authorship_front_matter_first("What is this book about?", [later, byline]) == [
        later,
        byline,
    ]


def test_combined_json_preview_only_emits_sufficient_answer_text() -> None:
    from pheasant.assistant.streaming import JsonAnswerPreview

    deltas: list[str] = []
    preview = JsonAnswerPreview(deltas.append)
    for part in (
        '{"suff',
        'icient":true,"answer":"A grounded',
        " answer [1].\\nNext",
        ' line","missing":"","next_queries":[]}',
    ):
        preview.feed(part)
    assert "".join(deltas) == "A grounded answer [1].\nNext line"

    rejected: list[str] = []
    insufficient = JsonAnswerPreview(rejected.append)
    insufficient.feed('{"sufficient":false,"answer":"unsupported [1]","missing":"source"}')
    assert rejected == []


def test_openai_chat_stream_reassembles_text_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    from pheasant.assistant import providers

    chunks = [
        {"choices": [{"index": 0, "delta": {"content": '{"sufficient":true,'}}]},
        {"choices": [{"index": 0, "delta": {"content": '"answer":"Yes [1]."}'}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4}},
    ]
    wire = (
        "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    ).encode()
    seen: list[dict] = []

    def fake_open(request, timeout):
        seen.append(json.loads(request.data))
        return io.BytesIO(wire)

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_open)
    parts: list[str] = []
    with providers.collect_token_usage() as usage:
        answer = providers.complete(
            "openai",
            api_key="test-key",
            system="Answer from evidence.",
            prompt="Question?",
            on_delta=parts.append,
            json_mode=True,
        )
    assert answer == '{"sufficient":true,"answer":"Yes [1]."}'
    assert "".join(parts) == answer
    assert usage.calls == 1
    assert usage.reported_input == 10
    assert usage.reported_output == 4
    assert seen[0]["stream"] is True
    assert seen[0]["stream_options"] == {"include_usage": True}


def test_openai_chat_stream_refuses_incomplete_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    from pheasant.assistant import providers

    monkeypatch.setattr(
        providers.urllib.request,
        "urlopen",
        lambda request, timeout: io.BytesIO(
            b'data: {"choices":[{"index":0,"delta":{"content":"partial"},'
            b'"finish_reason":null}]}\n\n'
        ),
    )
    with pytest.raises(providers.ProviderError, match="before completion"):
        providers.complete(
            "openai",
            api_key="test-key",
            system="Answer.",
            prompt="Question?",
            on_delta=lambda _part: None,
        )


def test_openai_chat_stream_marks_length_limited_answer_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pheasant.assistant import providers

    chunks = [
        {"choices": [{"index": 0, "delta": {"content": "Partial [1]"}, "finish_reason": "length"}]}
    ]
    wire = (
        "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    ).encode()
    monkeypatch.setattr(
        providers.urllib.request,
        "urlopen",
        lambda request, timeout: io.BytesIO(wire),
    )
    with pytest.raises(providers.OutputTruncated):
        providers.complete(
            "openai",
            api_key="test-key",
            system="Answer.",
            prompt="Question?",
            on_delta=lambda _part: None,
        )


def _hit(name: str) -> dict[str, Any]:
    node_id = f"file:kb:docs/{name}.md"
    return {
        "node_id": node_id,
        "chunk_id": f"{node_id}#c0",
        "type": "chunk",
        "title": f"docs/{name}.md",
        "relative_path": f"docs/{name}.md",
        "source_id": "notes",
        "score": 1.0,
        "chunks": [{"text_preview": f"Evidence from {name}."}],
    }


class _Search:
    vector = None

    def __init__(self, results: dict[str, list[dict[str, Any]]] | None = None):
        self.results = results or {}
        self.calls: list[str] = []

    def search_context(self, _kb, query, _mode, _limit, _source_name, **_kwargs):
        self.calls.append(query)
        return {"results": self.results.get(query, []), "counts": {}}


class _ScriptedLLM:
    provider = "openai"
    model_id = "gpt-6-luna"

    def __init__(self, replies: list[Any]):
        self.replies = list(replies)
        self.calls = 0

    def complete(self, _system, _prompt, **_kwargs):
        self.calls += 1
        if not self.replies:
            raise AssertionError("unexpected model call")
        reply = self.replies.pop(0)
        return reply if isinstance(reply, str) else json.dumps(reply)


def _agentic(search: _Search, llm: _ScriptedLLM, **options: Any):
    pytest.importorskip("langgraph")
    from pheasant.assistant.retrieval import PheasantRetriever
    from pheasant.assistant.workflows import WorkflowRequest
    from pheasant.assistant.workflows.agentic import AgenticWorkflow

    retriever = PheasantRetriever(search=search, knowledge_base="kb", graph=None, state=None)
    request_options = {
        "staged_retrieval": True,
        "combine_grade_and_answer": True,
        "retrieval_modes": ["hybrid"],
        "expand_graph": False,
        "max_rounds": 2,
        **options,
    }
    return AgenticWorkflow().run(
        WorkflowRequest(question="What does the system do?", options=request_options),
        retriever,
        llm,
    )


def test_sufficient_first_round_uses_one_model_call_after_hybrid_retrieval() -> None:
    search = _Search({"What does the system do?": [_hit("overview")]})
    llm = _ScriptedLLM(
        [{"sufficient": True, "answer": "It manages indexed notes [1].", "missing": ""}]
    )

    result = _agentic(search, llm)

    assert llm.calls == 1
    assert search.calls == ["What does the system do?"]
    assert result.mode == "llm"
    assert result.counts["rounds"] == 1
    assert result.counts["retrieval_rounds"] == 1
    assert any(step.name == "verify" for step in result.steps)


def test_deferred_graph_first_round_uses_only_direct_search() -> None:
    class ModeSearch(_Search):
        def __init__(self):
            super().__init__()
            self.modes: list[str] = []

        def search_context(self, kb, query, mode, limit, source, **kwargs):
            self.modes.append(mode)
            return {"results": [_hit("overview")] if mode == "text" else [], "counts": {}}

    search = ModeSearch()
    llm = _ScriptedLLM(
        [{"sufficient": True, "answer": "It manages indexed notes [1].", "missing": ""}]
    )

    result = _agentic(search, llm, defer_graph_until_insufficient=True, expand_graph=True)

    assert llm.calls == 1
    assert search.modes == ["text"]
    assert result.mode == "llm"
    assert any("deferred" in step.detail for step in result.steps if step.name == "expand")


def test_deferred_graph_retries_hybrid_on_insufficient_evidence_without_new_query() -> None:
    class ModeSearch(_Search):
        def __init__(self):
            super().__init__()
            self.modes: list[str] = []

        def search_context(self, kb, query, mode, limit, source, **kwargs):
            self.modes.append(mode)
            name = "overview" if mode == "text" else "deployment"
            return {"results": [_hit(name)], "counts": {}}

    search = ModeSearch()
    llm = _ScriptedLLM(
        [
            {"sufficient": False, "answer": "", "missing": "deployment detail", "next_queries": []},
            {
                "sufficient": True,
                "answer": "It manages notes and deployment [1] [2].",
                "missing": "",
            },
        ]
    )

    result = _agentic(search, llm, defer_graph_until_insufficient=True)

    assert search.modes == ["text", "hybrid"]
    assert llm.calls == 2
    assert result.mode == "llm"
    assert result.counts["retrieval_rounds"] == 2


def test_second_round_can_recover_missing_evidence() -> None:
    search = _Search(
        {
            "What does the system do?": [_hit("overview")],
            "deployment details": [_hit("deployment")],
        }
    )
    llm = _ScriptedLLM(
        [
            {
                "sufficient": False,
                "answer": "",
                "missing": "deployment details",
                "next_queries": ["deployment details"],
            },
            {
                "sufficient": True,
                "answer": "It manages notes and deployment [1] [2].",
                "missing": "",
            },
        ]
    )

    result = _agentic(search, llm)

    assert llm.calls == 2
    assert result.mode == "llm"
    assert result.counts["rounds"] == 2
    assert result.counts["retrieval_rounds"] == 2
    assert len(result.retrieved_evidence_ids) == 2


def test_repeated_followup_query_stops_without_repeating_retrieval() -> None:
    question = "What does the system do?"
    search = _Search({question: [_hit("overview")]})
    llm = _ScriptedLLM(
        [
            {
                "sufficient": False,
                "answer": "The current passage is incomplete [1].",
                "missing": "a more specific explanation",
                "next_queries": ["  what   DOES the system do?  "],
            }
        ]
    )

    result = _agentic(search, llm)

    assert llm.calls == 1
    assert search.calls == [question]
    assert result.counts["retrieval_rounds"] == 1
    assert result.counts["insufficient_evidence"] is True
    assert "Evidence gap:" in result.answer


def test_followup_with_no_new_evidence_stops_before_another_model_call() -> None:
    question = "What does the system do?"
    search = _Search({question: [_hit("overview")], "missing detail": [_hit("overview")]})
    llm = _ScriptedLLM(
        [
            {
                "sufficient": False,
                "answer": "The passage gives a partial answer [1].",
                "missing": "missing detail",
                "next_queries": ["missing detail"],
            }
        ]
    )

    result = _agentic(search, llm)

    assert llm.calls == 1
    assert result.counts["retrieval_rounds"] == 2
    assert result.counts["insufficient_evidence"] is True
    assert "no new evidence" in result.answer
    assert any("no new evidence" in step.detail for step in result.steps)


def test_malformed_sufficiency_boolean_is_never_treated_as_true() -> None:
    search = _Search({"What does the system do?": [_hit("overview")]})
    llm = _ScriptedLLM(
        [{"sufficient": "false", "answer": "It is a service [1].", "missing": "unknown"}]
    )

    result = _agentic(search, llm)

    assert result.mode == "extractive"
    assert result.error
    assert result.counts["insufficient_evidence"] is True


def test_no_evidence_cannot_be_marked_sufficient_without_a_valid_reference() -> None:
    search = _Search()
    llm = _ScriptedLLM([{"sufficient": True, "answer": "Unsupported answer [1].", "missing": ""}])

    result = _agentic(search, llm)

    assert result.mode == "extractive"
    assert result.error
    assert result.citations == []
    assert result.counts["insufficient_evidence"] is True


def test_exhausted_rounds_return_an_explicit_evidence_gap() -> None:
    search = _Search(
        {
            "What does the system do?": [_hit("overview")],
            "deployment details": [_hit("deployment")],
        }
    )
    llm = _ScriptedLLM(
        [
            {
                "sufficient": False,
                "answer": "The overview describes indexed notes [1].",
                "missing": "deployment details",
                "next_queries": ["deployment details"],
            },
            {
                "sufficient": False,
                "answer": "Deployment details remain unavailable [1].",
                "missing": "the deployment configuration",
                "next_queries": ["deployment configuration"],
            },
        ]
    )

    result = _agentic(search, llm)

    assert llm.calls == 2
    assert len(search.calls) == 2
    assert result.counts["retrieval_rounds"] == 2
    assert result.counts["insufficient_evidence"] is True
    assert "Evidence gap:" in result.answer


def test_hybrid_queries_batch_embeddings_and_keep_request_context_in_workers() -> None:
    from pheasant.assistant.retrieval import PheasantRetriever
    from pheasant.request_budget import RequestBudget, activate, remaining_seconds

    class Vector:
        def __init__(self):
            self.batches: list[list[str]] = []
            self.context_remaining: list[float | None] = []

        def embed_queries(self, queries: list[str]):
            self.batches.append(queries)
            self.context_remaining.append(remaining_seconds())
            return [[1.0] for _ in queries]

    class HybridSearch:
        def __init__(self, vector):
            self.vector = vector
            self.worker_remaining: list[float | None] = []

        def search_context(self, _kb, query, *_args, **_kwargs):
            self.worker_remaining.append(remaining_seconds())
            hit = _hit(query.replace(" ", "-"))
            return {"results": [hit], "counts": {}}

    vector = Vector()
    search = HybridSearch(vector)
    retriever = PheasantRetriever(search=search, knowledge_base="kb")

    with activate(RequestBudget(3)):
        passages = retriever.multi_search(["first query", "second query"], modes=["hybrid"])

    assert len(passages) == 2
    assert vector.batches == [["first query", "second query"]]
    assert vector.context_remaining[0] is not None
    assert len(search.worker_remaining) == 2
    assert all(value is not None for value in search.worker_remaining)


def test_concurrent_identical_query_embeddings_share_one_provider_request() -> None:
    from pheasant.search.vector_store import VectorSearcher

    started = threading.Event()
    release = threading.Event()

    class Embedder:
        provider = "stub-test"
        model = "test-embedder"
        base_url = "https://embedding.invalid"
        dimensions = 1
        timeout = 2.0

        def __init__(self):
            self.calls: list[list[str]] = []

        def embed(self, texts: list[str]):
            self.calls.append(texts)
            started.set()
            assert release.wait(2)
            return [[float(len(texts))] for _ in texts]

    embedder = Embedder()
    searcher = VectorSearcher(embedder, store=None, state=None)
    barrier = threading.Barrier(4)

    def run():
        barrier.wait()
        return searcher.embed_query("same query")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run) for _ in range(4)]
        assert started.wait(2)
        time.sleep(0.05)
        release.set()
        vectors = [future.result(timeout=2) for future in futures]

    assert embedder.calls == [["same query"]]
    assert vectors == [[1.0]] * 4


def test_query_embedding_failures_release_singleflight_waiters() -> None:
    from pheasant.search.vector_store import VectorSearcher

    started = threading.Event()
    release = threading.Event()

    class Embedder:
        provider = "stub-test"
        model = "failure-test"
        dimensions = 1
        timeout = 2.0

        def __init__(self):
            self.calls = 0

        def embed(self, _texts):
            self.calls += 1
            started.set()
            assert release.wait(2)
            raise RuntimeError("provider down")

    embedder = Embedder()
    searcher = VectorSearcher(embedder, store=None, state=None)
    barrier = threading.Barrier(3)

    def run():
        barrier.wait()
        return searcher.embed_query("same query")

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run) for _ in range(3)]
        assert started.wait(2)
        time.sleep(0.05)
        release.set()
        for future in futures:
            with pytest.raises(RuntimeError, match="provider down"):
                future.result(timeout=2)

    assert embedder.calls == 1


def test_answer_admission_is_shared_bounded_and_idempotent() -> None:
    from types import SimpleNamespace

    from pheasant.services.assistant import acquire
    from pheasant.services.errors import AssistantBusy

    context = SimpleNamespace(
        config=SimpleNamespace(
            assistant=SimpleNamespace(
                latency=SimpleNamespace(max_concurrent_answers=1),
            )
        )
    )
    first = acquire(context)
    with pytest.raises(AssistantBusy):
        acquire(context)

    first.release()
    first.release()
    second = acquire(context)
    second.release()


def test_generated_visual_uses_the_long_request_budget() -> None:
    from types import SimpleNamespace

    from pheasant.services.assistant import AnswerRequest, create_request_budget

    context = SimpleNamespace(
        config=SimpleNamespace(
            assistant=SimpleNamespace(
                latency=SimpleNamespace(
                    short_deadline_seconds=1,
                    medium_deadline_seconds=10,
                    long_deadline_seconds=180,
                )
            )
        )
    )
    visual_request = AnswerRequest(question="Explain the deployment", visual="diagram")

    budget = create_request_budget(context, visual_request)

    assert budget.deadline - budget.started == pytest.approx(180)
