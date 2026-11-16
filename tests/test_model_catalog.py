"""Several models per region: picked per question, and one embedding switched by an admin.

Offline. The catalog is plain config; the wires are the real ones with their
single network hop (`assistant.providers._http_json`) captured, and the
embedding models are two stub embedders of different width, which is exactly
what makes a switch invalidate the index.

What must hold: a region with no catalog is untouched; a selection reaches the
wire it names and nothing else; an unknown or disallowed choice is refused in
the same words on both surfaces; the catalog never shows an endpoint or a
variable name; and after an embedding switch the index is whole again in the
new space without a source being re-read.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.security.identity import Identity, bind_identity, reset_identity
from pheasant.services import models

FOUNDRY = "https://res.services.ai.azure.com"

CATALOG = {
    "chat": [
        {
            "id": "gpt-6-luna",
            "label": "GPT-6 Luna",
            "family": "OpenAI",
            "provider": "azure",
            "model": "gpt-6-luna",
            "base_url": f"{FOUNDRY}/openai/v1",
            "api_key_env": "TEST_FOUNDRY_KEY",
        },
        {
            "id": "claude-opus-5-5",
            "label": "Claude Opus 5.5",
            "family": "Anthropic",
            "provider": "anthropic",
            "model": "opus-deploy",
            "base_url": f"{FOUNDRY}/anthropic",
            "api_key_env": "TEST_FOUNDRY_KEY",
        },
    ],
    "default_chat": "gpt-6-luna",
    "decisions": [
        {"id": "luna", "api": "decisions", "model": "gpt-6-luna", "base_url": FOUNDRY},
        {
            "id": "microsoft-decision-1",
            "api": "systemone",
            "model": "Microsoft-Decision-1",
            "base_url": FOUNDRY,
            "sufficient_threshold": 0.4,
        },
    ],
    "default_decision": "luna",
    "embeddings": [
        {"id": "small", "provider": "stub", "model": "stub-small", "dimensions": 32},
        {"id": "large", "provider": "stub", "model": "stub-large", "dimensions": 96},
    ],
    "default_embedding": "small",
}


def _config(tmp_path: Path, catalog: dict | None = CATALOG, **extra: Any) -> PheasantConfig:
    for name in ("ws", "state", "exports"):
        (tmp_path / name).mkdir(exist_ok=True)
    (tmp_path / "ws" / "notes").mkdir(exist_ok=True)
    (tmp_path / "ws" / "notes" / "pheasants.md").write_text(
        "# Pheasants\n\nA pheasant roosts in trees at night and forages on the ground by day.\n",
        encoding="utf-8",
    )
    data: dict[str, Any] = {
        "pheasant": {
            "name": "kb",
            "defaults": "standalone",
            "workspace_root": str(tmp_path / "ws"),
            "state_path": str(tmp_path / "state"),
            "exports_path": str(tmp_path / "exports"),
        },
        "search": {"vector_store": {"provider": "numpy"}},
        "assistant": {
            "provider": "azure",
            "model": "gpt-6-luna",
            "base_url": f"{FOUNDRY}/openai/v1",
            "api_key_env": "TEST_FOUNDRY_KEY",
            "reasoning_effort": "low",
            "retrieval": {"grader_model": "gpt-6-luna"},
        },
        "sources": [
            {"name": "notes", "type": "document_folder", "path": "notes", "include": ["**/*.md"]}
        ],
        **extra,
    }
    if catalog is not None:
        data["models"] = catalog
    return PheasantConfig.model_validate(data)


@pytest.fixture
def region(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_FOUNDRY_KEY", "k-123")
    monkeypatch.setenv("PHEASANT_API_TOKEN", "admin-token")
    monkeypatch.setenv("READER_TOKEN", "reader-token")
    config = _config(tmp_path)
    config.artifacts.index_on_save = False
    config.security.api_auth.role_tokens = {"READER_TOKEN": "Reader"}
    app = create_app(config, config_path=str(tmp_path / "pheasant.yaml"))
    with TestClient(app) as client:
        yield app, client
    app.state.engine.close()


ADMIN = {"Authorization": "Bearer admin-token"}
READER = {"Authorization": "Bearer reader-token"}


def test_a_catalog_is_validated_when_it_loads(tmp_path):
    with pytest.raises(ValueError, match="ids must be unique"):
        _config(tmp_path, {"chat": [{"id": "a", "model": "x"}, {"id": "a", "model": "y"}]})
    with pytest.raises(ValueError, match="default 'b' is not one of a"):
        _config(tmp_path, {"chat": [{"id": "a", "model": "x"}], "default_chat": "b"})
    config = _config(tmp_path)
    again = PheasantConfig.model_validate(config.model_dump(mode="json"))
    assert again.models.model_dump() == config.models.model_dump()


def test_no_catalog_is_the_config_it_was_given(tmp_path):
    config = _config(tmp_path, catalog=None)
    assert models.config_for(config) is config
    with pytest.raises(models.UnknownModel):
        models.config_for(config, "claude-opus-5-5")


def test_a_selection_is_a_view_and_the_region_config_is_untouched(tmp_path):
    config = _config(tmp_path)
    # The default names the configured model: its tuned settings stay.
    default = models.config_for(config)
    assert default.assistant.reasoning_effort == "low"
    assert default.assistant.retrieval.grader_model == "gpt-6-luna"

    view = models.config_for(config, "claude-opus-5-5", "microsoft-decision-1")
    assistant = view.assistant
    assert (assistant.provider, assistant.model) == ("anthropic", "opus-deploy")
    assert assistant.base_url == f"{FOUNDRY}/anthropic"
    # Settings tuned for the configured model do not follow another one: a
    # Luna deployment named as Claude's grader, an effort only Luna takes.
    assert assistant.reasoning_effort is None
    assert assistant.retrieval.grader_model is None
    assert assistant.decisions.api == "systemone"
    assert assistant.decisions.model == "Microsoft-Decision-1"
    assert assistant.decisions.sufficient_threshold == 0.4
    assert assistant.decisions.accept_threshold == config.assistant.decisions.accept_threshold
    assert view.search is config.search and view is not config
    assert (config.assistant.provider, config.assistant.decisions.api) == ("azure", "decisions")


def test_each_selection_reaches_its_own_wire(tmp_path, monkeypatch):
    from pheasant.assistant import providers
    from pheasant.assistant.answering import resolve_llm

    monkeypatch.setenv("TEST_FOUNDRY_KEY", "k-123")
    calls: list[tuple[str, dict, dict]] = []

    def fake(url, payload, headers, timeout):
        calls.append((url, payload, headers))
        if url.endswith("/v1/messages"):
            return {"content": [{"type": "text", "text": "from claude"}], "stop_reason": "end_turn"}
        return {"choices": [{"message": {"content": "from luna"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(providers, "_http_json", fake)
    config = _config(tmp_path)

    assert resolve_llm(models.config_for(config)).complete("s", "p") == "from luna"
    url, payload, headers = calls[-1]
    assert url == f"{FOUNDRY}/openai/v1/chat/completions"
    assert payload["model"] == "gpt-6-luna" and headers == {"api-key": "k-123"}

    claude = resolve_llm(models.config_for(config, "claude-opus-5-5"))
    assert claude.complete("s", "p") == "from claude"
    url, payload, headers = calls[-1]
    assert url == f"{FOUNDRY}/anthropic/v1/messages"
    assert payload["model"] == "opus-deploy"
    # Claude on Foundry is Anthropic's wire behind the resource's credential.
    assert headers["api-key"] == "k-123" and headers["anthropic-version"]


def test_the_catalog_lists_what_may_be_used_and_never_where_it_lives(region):
    _, client = region
    listed = client.get("/models", headers=READER)
    assert listed.status_code == 200
    body = listed.json()
    assert [m["id"] for m in body["chat"]] == ["gpt-6-luna", "claude-opus-5-5"]
    assert body["default_chat"] == "gpt-6-luna" and body["selectable"] is True
    assert [m["api"] for m in body["decisions"]] == ["decisions", "systemone"]
    assert body["embedding"]["active"] == "small"
    assert body["embedding"]["can_change"] is False  # a Reader's view
    assert client.get("/models", headers=ADMIN).json()["embedding"]["can_change"] is True
    for secret in ("services.ai.azure.com", "TEST_FOUNDRY_KEY", "k-123", "base_url"):
        assert secret not in listed.text


def test_an_answer_reports_the_models_it_ran_with_and_refuses_an_unknown_one(region):
    app, client = region
    answered = client.post(
        "/assistant/chat",
        json={"question": "where do pheasants roost?", "model": "claude-opus-5-5"},
        headers=READER,
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["models"] == {"chat": "claude-opus-5-5", "decision": "luna"}
    default = client.post("/assistant/chat", json={"question": "pheasants?"}, headers=READER)
    assert default.json()["models"] == {"chat": "gpt-6-luna", "decision": "luna"}

    refused = client.post(
        "/assistant/chat", json={"question": "q", "model": "gpt-9"}, headers=READER
    )
    assert refused.status_code == 404
    text = "Unknown chat model: gpt-9. Available: gpt-6-luna, claude-opus-5-5"
    assert refused.json()["detail"] == text
    # The stream refuses with a status, before it opens.
    stream = client.post(
        "/assistant/chat/stream", json={"question": "q", "model": "gpt-9"}, headers=READER
    )
    assert stream.status_code == 404

    # MCP's facade gives the same refusal, in the same words.
    from pheasant.mcp_server.assistant_tools import AssistantTools
    from pheasant.services import ServiceContext

    class Tools(AssistantTools):
        def _require_knowledge_base(self, knowledge_base):
            return None

    tools = Tools()
    tools.services = ServiceContext(
        config=app.state.config, state=app.state.state, searcher=None, engine=app.state.engine
    )
    with pytest.raises(models.UnknownModel) as raised:
        tools.ask_knowledge_base("kb", "q", model="gpt-9")
    assert str(raised.value) == text


def test_selection_can_be_turned_off_and_the_default_still_answers(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_FOUNDRY_KEY", "k-123")
    config = _config(tmp_path, {**CATALOG, "selectable": False})
    assert models.config_for(config, "gpt-6-luna") is not None  # naming the default is fine
    with pytest.raises(models.ModelSelectionDisabled):
        models.config_for(config, "claude-opus-5-5")
    with pytest.raises(models.ModelSelectionDisabled):
        models.config_for(config, None, "microsoft-decision-1")


def _wait(predicate, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("timed out waiting for the re-embed")


def test_an_admin_switches_the_embedding_model_and_the_index_follows(region):
    app, client = region
    engine = app.state.engine
    assert client.post("/sync/notes", headers=ADMIN).status_code == 200
    _wait(lambda: engine.vectors is not None and engine.vectors.store.count() > 0)
    chunks = engine.vectors.store.count()
    assert engine.config.search.embeddings.dimensions == 32

    denied = client.put("/models/embedding", json={"embedding": "large"}, headers=READER)
    assert denied.status_code == 403 and denied.json()["code"] == "PERMISSION_DENIED"
    unknown = client.put("/models/embedding", json={"embedding": "huge"}, headers=ADMIN)
    assert unknown.status_code == 404 and unknown.json()["code"] == "UNKNOWN_MODEL"

    switched = client.put("/models/embedding", json={"embedding": "large"}, headers=ADMIN)
    assert switched.status_code == 200, switched.text
    assert switched.json()["status"] == "selected" and switched.json()["previous"] == "small"
    assert switched.json()["embedding"]["active"] == "large"
    _wait(lambda: client.get("/models", headers=ADMIN).json()["embedding"]["state"] == "ready")

    # Every chunk is back, in the new space, and no source was re-read.
    assert engine.config.search.embeddings.dimensions == 96
    assert engine.vectors.store.count() == chunks
    found = client.post(
        "/search", json={"query": "roosting in trees", "mode": "vector"}, headers=READER
    )
    assert found.status_code == 200 and found.json()["results"], found.text

    again = client.put("/models/embedding", json={"embedding": "large"}, headers=ADMIN).json()
    assert again["status"] == "unchanged" and "reindex" not in again


def test_a_region_that_never_recorded_its_space_still_re_embeds_on_a_switch(region):
    """A fleet that does not sync at startup never reconciles, so it never
    records which model built its vectors. Read as "never embedded", a switch
    was an introduction: the old vectors kept and no re-embed. Found on a live
    region reporting ``not_indexed`` over eleven thousand embedded chunks."""

    from pheasant.sync.fingerprint import EMBEDDING_SCOPE

    app, client = region
    engine = app.state.engine
    assert client.post("/sync/notes", headers=ADMIN).status_code == 200
    _wait(lambda: engine.vectors is not None and engine.vectors.store.count() > 0)
    chunks = engine.vectors.store.count()
    engine.state.execute("DELETE FROM sync_fingerprints WHERE scope=?", (EMBEDDING_SCOPE,))
    assert engine.state.get_fingerprint(EMBEDDING_SCOPE) is None

    # Unrecorded and still on the default: the space it was built in.
    assert client.get("/models", headers=ADMIN).json()["embedding"]["state"] == "ready"

    switched = client.put("/models/embedding", json={"embedding": "large"}, headers=ADMIN)
    assert switched.status_code == 200, switched.text
    _wait(lambda: client.get("/models", headers=ADMIN).json()["embedding"]["state"] == "ready")
    assert engine.state.get_fingerprint(EMBEDDING_SCOPE) is not None
    assert engine.config.search.embeddings.dimensions == 96
    assert engine.vectors.store.count() == chunks
    found = client.post(
        "/search", json={"query": "roosting in trees", "mode": "vector"}, headers=READER
    )
    assert found.status_code == 200 and found.json()["results"], found.text


def test_a_new_process_starts_on_the_selected_embedding(tmp_path, monkeypatch):
    from pheasant.search import embedding_selection
    from pheasant.sync.engine import SyncEngine

    monkeypatch.setenv("TEST_FOUNDRY_KEY", "k-123")
    first = SyncEngine(_config(tmp_path))
    first.state.migrate()
    assert first.config.search.embeddings.model == "stub-small"
    embedding_selection.select(first.state, first.config.knowledge_base_id, "large", "test")
    first.close()

    second = SyncEngine(_config(tmp_path))
    assert second.config.search.embeddings.model == "stub-large"
    assert second.config.search.embeddings.dimensions == 96
    second.close()


def test_without_embedding_entries_nothing_about_embeddings_changes(tmp_path):
    from pheasant.search import embedding_selection
    from pheasant.sync.engine import SyncEngine

    config = _config(tmp_path, {"chat": CATALOG["chat"]})
    before = config.search.embeddings.model_dump()
    engine = SyncEngine(config)
    assert embedding_selection.ensure_space(engine) is None
    assert config.search.embeddings.model_dump() == before
    engine.close()
    token = bind_identity(Identity("a", "personal_key", roles=("Admin",)))
    try:
        from pheasant.services import ServiceContext

        context = ServiceContext(config=config, state=engine.state, searcher=None)
        with pytest.raises(ValueError, match="lists no embedding models"):
            models.select_embedding(context, "small")
    finally:
        reset_identity(token)


def test_a_foundry_model_with_no_key_is_called_with_the_managed_identity(tmp_path, monkeypatch):
    """A model deployed in a second Foundry resource needs no second key."""

    from pheasant.assistant import providers
    from pheasant.assistant.answering import resolve_llm
    from pheasant.security import entra

    monkeypatch.delenv("TEST_FOUNDRY_KEY", raising=False)
    config = _config(tmp_path)
    # No key and no identity: no model, the extractive path, exactly as before.
    monkeypatch.setattr(entra, "managed_identity_available", lambda: False)
    assert resolve_llm(models.config_for(config, "claude-opus-5-5")) is None

    monkeypatch.setattr(entra, "managed_identity_available", lambda: True)
    monkeypatch.setattr(entra, "managed_identity_token", lambda resource, client_id=None: "mi-tok")
    seen: list[dict] = []

    def fake(url, payload, headers, timeout):
        seen.append({"url": url, **headers})
        if url.endswith("/v1/messages"):
            return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(providers, "_http_json", fake)
    for chosen in ("claude-opus-5-5", "gpt-6-luna"):
        llm = resolve_llm(models.config_for(config, chosen))
        assert llm is not None and llm.source == "managed_identity"
        assert llm.complete("s", "p") == "ok"
        assert seen[-1]["authorization"] == "Bearer mi-tok"
        assert "x-api-key" not in seen[-1] and "api-key" not in seen[-1]
    # Off a Foundry host, no key is still no model.
    config.assistant.base_url = "https://api.openai.com/v1"
    config.models.chat[0].base_url = "https://api.openai.com/v1"
    assert resolve_llm(models.config_for(config, "gpt-6-luna")) is None
