"""Grounded chat over the knowledge base (UI chat layer).

Acceptance:

1. With no provider configured the answer is *extractive* and offline —
   real citations, no network call. This is the default the offline test
   suite and any air-gapped deployment run in.
2. With a provider, the request carries the right wire shape per vendor
   (OpenAI / Anthropic / Gemini) and the answer's ``[n]`` markers are
   reflected back onto the citations.
3. Facts come off the graph, are one hop from a cited node, and skip
   structural edges.
4. Session-supplied keys live in memory only, expire, and are never echoed.
5. Chat never runs during indexing — the indexing path is untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.assistant import chat as chat_module
from pheasant.assistant import providers as providers_module
from pheasant.assistant.credentials import SessionKeyStore
from pheasant.graph.simple import SimpleMultiDiGraph


class _FakeSearch:
    """Stands in for HybridSearch with a fixed, inspectable result set."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def search_context(self, kb, query, mode, max_results, source_name, **kwargs):
        self.calls.append((kb, query, mode, max_results, source_name))
        return {
            "query": query,
            "mode": mode,
            "results": self.results[:max_results],
            "counts": {"returned": min(len(self.results), max_results)},
        }


def _results():
    return [
        {
            "node_id": "file:kb:docs/sync.md:branch=none",
            "chunk_id": "chunk:kb:docs/sync.md:0",
            "type": "chunk",
            "title": "docs/sync.md",
            "relative_path": "docs/sync.md",
            "score": 1.5,
            "chunks": [{"text_preview": "Sync is idempotent because content is hashed."}],
        },
        {
            "node_id": "file:kb:docs/graph.md:branch=none",
            "type": "chunk",
            "title": "docs/graph.md",
            "relative_path": "docs/graph.md",
            "score": 1.1,
            "summary": "The graph uses stable IDs.",
        },
    ]


def _graph() -> SimpleMultiDiGraph:
    graph = SimpleMultiDiGraph()
    graph.add_node("file:kb:docs/sync.md:branch=none", id="x", type="file", label="docs/sync.md")
    graph.add_node("concept:kb:idempotency", id="c", type="concept", label="idempotency")
    graph.add_node("dir:kb:docs", id="d", type="directory", label="docs")
    graph.add_node("chunk:kb:docs/sync.md:0", id="ch", type="chunk", label="chunk 0")
    graph.add_edge("file:kb:docs/sync.md:branch=none", "concept:kb:idempotency", type="mentions")
    # Structural edges must never surface as "facts".
    graph.add_edge("file:kb:docs/sync.md:branch=none", "dir:kb:docs", type="contains")
    graph.add_edge("file:kb:docs/sync.md:branch=none", "chunk:kb:docs/sync.md:0", type="has_chunk")
    return graph


class _Config:
    class assistant:  # noqa: N801 - mirrors the dataclass attribute access
        enabled = True
        provider = "auto"
        model = None
        base_url = None
        allow_session_keys = True
        session_key_ttl_minutes = 60
        max_context_chunks = 8
        max_output_tokens = 1024
        request_timeout_seconds = 5.0
        max_facts = 12

    security = None


def test_no_provider_yields_offline_extractive_answer_with_citations() -> None:
    search = _FakeSearch(_results())

    payload = chat_module.answer_question(
        "how does sync stay idempotent",
        search=search,
        knowledge_base="kb",
        config=_Config(),
        graph=_graph(),
        env={},  # no provider keys anywhere
    )

    assert payload["mode"] == "extractive"
    assert payload["provider"] is None
    assert "hashed" in payload["answer"]
    assert [c["index"] for c in payload["citations"]] == [1, 2]
    assert payload["citations"][0]["title"] == "docs/sync.md"
    # The extractive answer cites what it quotes.
    assert payload["citations"][0]["used"] is True


def test_facts_are_one_hop_and_skip_structural_edges() -> None:
    facts = chat_module.collect_facts(_graph(), ["file:kb:docs/sync.md:branch=none"])

    assert [(f["subject"], f["predicate"], f["object"]) for f in facts] == [
        ("docs/sync.md", "mentions", "idempotency")
    ]
    assert facts[0]["object_type"] == "concept"


def test_facts_are_deterministic_across_repeated_calls() -> None:
    graph = _graph()
    first = chat_module.collect_facts(graph, ["file:kb:docs/sync.md:branch=none"])
    second = chat_module.collect_facts(graph, ["file:kb:docs/sync.md:branch=none"])
    assert first == second


def test_facts_are_spread_across_cited_nodes_not_drained_from_the_first() -> None:
    """A hub document must not consume the whole fact budget."""
    graph = SimpleMultiDiGraph()
    for doc in ("hub", "other"):
        graph.add_node(f"file:{doc}", id=doc, type="file", label=f"{doc}.md")
    # The hub mentions five concepts; the other document mentions one.
    for i in range(5):
        graph.add_node(f"concept:h{i}", id=f"h{i}", type="concept", label=f"hub-concept-{i}")
        graph.add_edge("file:hub", f"concept:h{i}", type="mentions")
    graph.add_node("concept:o", id="o", type="concept", label="other-concept")
    graph.add_edge("file:other", "concept:o", type="mentions")

    facts = chat_module.collect_facts(graph, ["file:hub", "file:other"], limit=3)

    subjects = [fact["subject"] for fact in facts]
    assert "other.md" in subjects, "the second cited node must be represented"
    assert subjects.count("hub.md") <= 2


@pytest.mark.parametrize(
    ("provider", "env_var", "expected_url_tail", "response"),
    [
        (
            "anthropic",
            "ANTHROPIC_API_KEY",
            "/v1/messages",
            {"content": [{"type": "text", "text": "Because content is hashed [1]."}]},
        ),
        (
            "openai",
            "OPENAI_API_KEY",
            "/chat/completions",
            {"choices": [{"message": {"content": "Because content is hashed [1]."}}]},
        ),
        (
            "gemini",
            "GEMINI_API_KEY",
            ":generateContent",
            {"candidates": [{"content": {"parts": [{"text": "Because content is hashed [1]."}]}}]},
        ),
    ],
)
def test_each_provider_is_called_with_its_own_wire_shape(
    monkeypatch: pytest.MonkeyPatch, provider, env_var, expected_url_tail, response
) -> None:
    seen: dict = {}

    def fake_http(url, payload, headers, timeout):
        seen.update(url=url, payload=payload, headers=headers)
        return response

    monkeypatch.setattr(providers_module, "_http_json", fake_http)
    config = _Config()
    config.assistant.provider = provider

    payload = chat_module.answer_question(
        "why is sync idempotent",
        search=_FakeSearch(_results()),
        knowledge_base="kb",
        config=config,
        graph=_graph(),
        env={env_var: "test-key"},
    )

    assert payload["mode"] == "llm"
    assert payload["provider"] == provider
    assert seen["url"].endswith(expected_url_tail)
    # The citation the model used is flagged; the other is not.
    assert payload["citations"][0]["used"] is True
    assert payload["citations"][1]["used"] is False
    # Grounding actually reached the prompt.
    body = str(seen["payload"])
    assert "docs/sync.md" in body and "idempotency" in body

    if provider == "anthropic":
        assert seen["headers"]["x-api-key"] == "test-key"
        assert seen["headers"]["anthropic-version"] == "2023-06-01"
        # Current Anthropic models reject sampling parameters with a 400.
        assert "temperature" not in seen["payload"]
    elif provider == "openai":
        assert seen["headers"]["authorization"] == "Bearer test-key"
    else:
        assert seen["headers"]["x-goog-api-key"] == "test-key"

    config.assistant.provider = "auto"  # restore the shared class attribute


def test_gpt_6_luna_uses_supported_chat_completion_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def fake_http(url, payload, headers, timeout):
        calls.append(payload)
        return {"choices": [{"message": {"content": "A grounded answer."}}]}

    monkeypatch.setattr(providers_module, "_http_json", fake_http)
    answer = providers_module.complete(
        "openai", api_key="test-key", system="Ground the answer.", prompt="Question?"
    )

    assert answer == "A grounded answer."
    assert len(calls) == 1
    assert calls[0]["model"] == "gpt-6-luna"
    assert calls[0]["max_completion_tokens"] == 4096
    assert "max_tokens" not in calls[0]
    assert "temperature" not in calls[0]


def test_provider_failure_degrades_to_extractive_rather_than_erroring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(url, payload, headers, timeout):
        raise providers_module.ProviderError("429 rate limited")

    monkeypatch.setattr(providers_module, "_http_json", boom)
    config = _Config()
    config.assistant.provider = "openai"

    payload = chat_module.answer_question(
        "anything",
        search=_FakeSearch(_results()),
        knowledge_base="kb",
        config=config,
        graph=_graph(),
        env={"OPENAI_API_KEY": "k"},
    )

    assert payload["mode"] == "extractive"
    assert "rate limited" in payload["error"]
    assert payload["citations"], "retrieval results survive a provider outage"
    # A configured-but-failing model must not be reported as "no model
    # connected" — that sends the user to re-enter a working key.
    assert "could not be reached" in payload["answer"]
    assert "No chat model is connected" not in payload["answer"]
    config.assistant.provider = "auto"


def test_auto_provider_prefers_the_first_populated_key() -> None:
    assert providers_module.resolve_auto_provider({}) is None
    assert providers_module.resolve_auto_provider({"OPENAI_API_KEY": "k"}) == "openai"
    assert (
        providers_module.resolve_auto_provider({"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "k"})
        == "anthropic"
    )


def test_session_keys_are_memory_only_and_never_echoed() -> None:
    store = SessionKeyStore(ttl_minutes=60)
    token, credential = store.put("anthropic", "sk-ant-secret", model="claude-sonnet-5")

    assert store.get(token).api_key == "sk-ant-secret"
    assert "sk-ant-secret" not in str(credential.redacted())
    assert credential.redacted()["provider"] == "anthropic"
    assert store.revoke(token) is True
    assert store.get(token) is None


def test_session_keys_expire() -> None:
    store = SessionKeyStore(ttl_minutes=1)
    token, _ = store.put("openai", "sk-x")
    # Force expiry rather than sleeping.
    from datetime import UTC, datetime, timedelta

    entry = store._entries[token]
    store._entries[token] = type(entry)(
        provider=entry.provider,
        api_key=entry.api_key,
        model=entry.model,
        base_url=entry.base_url,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert store.get(token) is None


def test_chat_http_round_trip_and_key_lifecycle(loaded_config, workspace_copy: Path) -> None:
    loaded_config.pheasant.workspace_root = workspace_copy
    app = create_app(config=loaded_config)
    app.state.engine.sync_source("architecture-notes", "full")
    client = TestClient(app)

    status = client.get("/assistant/status").json()
    assert status["enabled"] is True
    assert {p["id"] for p in status["providers"]} == {"anthropic", "openai", "gemini"}

    chat = client.post("/assistant/chat", json={"question": "sync engine"})
    assert chat.status_code == 200
    body = chat.json()
    assert body["mode"] == "extractive"  # offline by default
    assert "citations" in body and "facts" in body

    stored = client.post(
        "/assistant/key", json={"provider": "gemini", "api_key": "AIza-secret"}
    ).json()
    session_id = stored["session_id"]
    assert "AIza-secret" not in chat.text
    assert "api_key" not in stored["session"]
    assert client.get("/assistant/status", params={"session_id": session_id}).json()["ready"]

    assert client.request("DELETE", "/assistant/key", params={"session_id": session_id}).json()[
        "revoked"
    ]
    assert (
        client.get("/assistant/status", params={"session_id": session_id}).json()["session"] is None
    )


def test_empty_question_is_rejected(loaded_config) -> None:
    client = TestClient(create_app(config=loaded_config))
    assert client.post("/assistant/chat", json={"question": "   "}).status_code == 400


def test_status_ready_requires_a_resolvable_credential_not_just_a_provider_name(
    loaded_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named provider with no key must not report "model connected"."""
    for spec in providers_module.PROVIDERS.values():
        monkeypatch.delenv(spec.api_key_env, raising=False)
    loaded_config.assistant.provider = "openai"
    client = TestClient(create_app(config=loaded_config))

    status = client.get("/assistant/status").json()
    assert status["configured_provider"] == "openai"
    assert status["ready"] is False, "no OPENAI_API_KEY is set, so nothing can answer"
    assert status["credential_source"] is None

    monkeypatch.setenv("OPENAI_API_KEY", "sk-present")
    ready = client.get("/assistant/status").json()
    assert ready["ready"] is True
    assert ready["credential_source"] == "environment"


def test_chat_stream_reports_steps_before_the_answer(loaded_config) -> None:
    """Progress must arrive as work happens, not bundled with the answer.

    Without a model configured the workflow is extractive, which still walks
    retrieve → synthesize — enough to assert the stream's shape: one or more
    `step` frames, then exactly one `answer`, then close.
    """

    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    app = create_app(config=loaded_config)
    app.state.engine.sync_source("architecture-notes", "full")
    client = TestClient(app)

    with client.stream(
        "POST", "/assistant/chat/stream", json={"question": "what is the sync engine?"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = []
        for line in response.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))

    kinds = [event["type"] for event in events]
    assert kinds.count("answer") == 1, kinds
    assert kinds[-1] == "answer", "the answer must be the last frame"
    assert "step" in kinds, "no progress was reported"
    # Every step frame names a stage and carries a human-readable detail.
    for event in events:
        if event["type"] == "step":
            assert event["name"]
            assert isinstance(event["detail"], str)

    answer = next(event["answer"] for event in events if event["type"] == "answer")
    assert answer["question"] == "what is the sync engine?"
    assert "citations" in answer


def test_chat_stream_uses_an_async_generator_not_a_threadpool_wrapped_one(
    loaded_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the old sync ``def publish()`` generator.

    Starlette's ``StreamingResponse`` wraps a *sync* generator in
    ``iterate_in_threadpool`` (``isinstance(content, AsyncIterable)`` is
    ``False`` for a plain generator object) — which dispatches each
    ``next()`` call through anyio's thread pool and holds a worker-thread
    token for as long as that call blocks (``events.get()`` in the old
    code, i.e. until the workflow finishes or the client gives up). An
    *async* generator is already an ``AsyncIterable`` and passes straight
    through, never touching the pool.

    Checked directly against that dispatch decision rather than by timing:
    a timing-based test here would need to actually hold a real streaming
    connection open while inspecting anyio's thread-pool state from the
    same event loop the app runs requests on — fragile through several
    layers of test-client plumbing — where this is a two-line, deterministic
    check of the exact fork in the road that decides whether the pool gets
    touched at all.
    """

    from collections.abc import AsyncIterable

    from fastapi.testclient import TestClient
    from starlette.responses import StreamingResponse

    from pheasant.api.app import create_app

    captured: dict[str, bool] = {}
    original_init = StreamingResponse.__init__

    def spy_init(self, content, *args, **kwargs):
        original_init(self, content, *args, **kwargs)
        captured["content_was_already_async"] = isinstance(content, AsyncIterable)

    monkeypatch.setattr(StreamingResponse, "__init__", spy_init)

    app = create_app(config=loaded_config)
    app.state.engine.sync_source("architecture-notes", "full")
    client = TestClient(app)

    with client.stream(
        "POST", "/assistant/chat/stream", json={"question": "what is the sync engine?"}
    ) as response:
        assert response.status_code == 200
        for _ in response.iter_lines():
            pass  # drain so the connection closes cleanly

    assert captured.get("content_was_already_async") is True, (
        "publish() is a sync generator, so Starlette wraps it in "
        "iterate_in_threadpool — an open or abandoned stream then holds a "
        "worker-thread token for as long as the connection stays open"
    )
