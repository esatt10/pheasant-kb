"""Usage totals: what the region spent and did, counted where it happens.

Offline. Model calls go through the real wires with their one network hop
(`assistant.providers._http_json`) answering with a provider's `usage` block,
so what is asserted is what each wire actually reports: OpenAI's input figure
contains its cached tokens, Anthropic's does not, and the ledger stores the
same thing for both.

What must hold: off, nothing is counted and nothing is served; on, tokens are
attributed to the model and the caller and survive a flush from two writers;
cost uses only prices the operator stated and names what has none; and a
customer's Admin sees activity but not the operator's cost.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.persistence import usage_ledger
from pheasant.security.identity import Identity, bind_identity, reset_identity
from pheasant.services import models, usage
from pheasant.telemetry import usage as metering
from pheasant.telemetry.usage import METER, Totals

FOUNDRY = "https://res.services.ai.azure.com"
ORG = "99999999-8888-7777-6666-555555555555"

CATALOG = {
    "chat": [
        {
            "id": "gpt-6-luna",
            "provider": "azure",
            "model": "gpt-6-luna",
            "base_url": f"{FOUNDRY}/openai/v1",
            "api_key_env": "TEST_FOUNDRY_KEY",
            "input_price": 1.0,
            "output_price": 4.0,
            "cached_input_price": 0.1,
        },
        {
            "id": "claude-opus-5-5",
            "provider": "anthropic",
            "model": "claude-opus-5-5",
            "base_url": f"{FOUNDRY}/anthropic",
            "api_key_env": "TEST_FOUNDRY_KEY",
            "input_price": 4.0,
            "output_price": 20.0,
        },
        {
            "id": "gpt-6-sol",
            "provider": "azure",
            "model": "gpt-6-sol",
            "base_url": f"{FOUNDRY}/openai/v1",
            "api_key_env": "TEST_FOUNDRY_KEY",
        },
    ],
    "default_chat": "gpt-6-luna",
}


def _config(tmp_path: Path, *, usage_on: bool = True, **security: Any) -> PheasantConfig:
    for name in ("ws", "state", "exports"):
        (tmp_path / name).mkdir(exist_ok=True)
    (tmp_path / "ws" / "notes").mkdir(exist_ok=True)
    (tmp_path / "ws" / "notes" / "a.md").write_text("# Pheasants\n\nThey roost in trees.\n")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "kb",
                "defaults": "standalone",
                "workspace_root": str(tmp_path / "ws"),
                "state_path": str(tmp_path / "state"),
                "exports_path": str(tmp_path / "exports"),
            },
            "observability": {"usage": usage_on},
            "assistant": {
                "provider": "azure",
                "model": "gpt-6-luna",
                "base_url": f"{FOUNDRY}/openai/v1",
                "api_key_env": "TEST_FOUNDRY_KEY",
            },
            "models": CATALOG,
            "security": security,
            "sources": [
                {
                    "name": "notes",
                    "type": "document_folder",
                    "path": "notes",
                    "include": ["**/*.md"],
                }
            ],
        }
    )
    config.artifacts.index_on_save = False
    return config


def _reply(url, payload, headers, timeout):
    if url.endswith("/v1/messages"):
        return {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            # Anthropic: input excludes what was read from cache.
            "usage": {"input_tokens": 500, "output_tokens": 50, "cache_read_input_tokens": 100},
        }
    return {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        # OpenAI: prompt_tokens includes the cached 400.
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 400},
            "completion_tokens_details": {"reasoning_tokens": 30},
        },
    }


@pytest.fixture
def region(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from pheasant.assistant import providers

    monkeypatch.setenv("TEST_FOUNDRY_KEY", "k")
    monkeypatch.setenv("PHEASANT_API_TOKEN", "admin-token")
    monkeypatch.setenv("READER_TOKEN", "reader-token")
    monkeypatch.setattr(providers, "_http_json", _reply)
    config = _config(tmp_path)
    config.security.api_auth.role_tokens = {"READER_TOKEN": "Reader"}
    app = create_app(config, config_path=str(tmp_path / "pheasant.yaml"))
    with TestClient(app) as client:
        yield app, client
    app.state.engine.close()
    # The app's MCP facade holds a second engine for the life of the process;
    # in a test that is this fixture, so stop the meter by hand.
    METER.unbind()


ADMIN = {"Authorization": "Bearer admin-token"}
READER = {"Authorization": "Bearer reader-token"}


def _call(config: PheasantConfig, model: str | None, who: Identity | None = None) -> None:
    from pheasant.assistant.answering import resolve_llm

    token = bind_identity(who) if who is not None else None
    try:
        assert resolve_llm(models.config_for(config, model)).complete("s", "p") == "ok"
    finally:
        if token is not None:
            reset_identity(token)


def test_tokens_are_counted_per_model_and_caller_as_each_wire_bills_them(region):
    app, client = region
    config = app.state.config
    kim = Identity("kim@contoso.com", "entra_sso", roles=("Reader",), tenant_id=ORG)
    _call(config, None, kim)
    _call(config, None, kim)
    _call(config, "claude-opus-5-5", kim)
    _call(config, "gpt-6-sol")  # no price stated

    body = client.get("/usage", headers=ADMIN).json()
    by_model = {m["model"]: m for m in body["by_model"]}
    luna, opus, sol = by_model["gpt-6-luna"], by_model["claude-opus-5-5"], by_model["gpt-6-sol"]
    # 1000 prompt tokens of which 400 cached, twice: 600 fresh each.
    assert (luna["input_tokens"], luna["cached_input_tokens"], luna["output_tokens"]) == (
        1200,
        800,
        400,
    )
    assert (opus["input_tokens"], opus["cached_input_tokens"], opus["output_tokens"]) == (
        500,
        100,
        50,
    )
    assert luna["model_calls"] == 2 and luna["kind"] == "chat"
    # Cost is tokens times the stated price per million; cache reads at their
    # own price when one is stated, at the input price when not.
    assert luna["cost"] == pytest.approx((1200 * 1.0 + 800 * 0.1 + 400 * 4.0) / 1e6, abs=1e-4)
    assert opus["cost"] == pytest.approx((500 * 4.0 + 100 * 4.0 + 50 * 20.0) / 1e6, abs=1e-4)
    assert (luna["priced"], sol["priced"], sol["cost"]) == (True, False, 0.0)
    assert body["unpriced_models"] == ["gpt-6-sol"] and body["currency"] == "USD"
    assert body["totals"]["cost"] == pytest.approx(luna["cost"] + opus["cost"], abs=1e-4)

    person = next(p for p in body["people"] if p["principal"] == "kim@contoso.com")
    assert (person["tenant_id"], person["model_calls"], person["input_tokens"]) == (ORG, 3, 1700)
    assert len(body["by_day"]) == 30 and body["by_day"][-1]["model_calls"] == 4


def test_questions_searches_and_latency_are_counted(region):
    _, client = region
    assert client.post("/sync/notes", headers=ADMIN).status_code == 200
    for _ in range(3):
        assert client.post("/search", json={"query": "roost"}, headers=READER).status_code == 200
    asked = client.post(
        "/assistant/chat", json={"question": "where do they roost?"}, headers=READER
    )
    assert asked.status_code == 200
    body = client.get("/usage", params={"days": 7}, headers=ADMIN).json()
    assert body["totals"]["searches"] == 3 and body["totals"]["questions"] == 1
    assert body["totals"]["active_people"] == 1
    assert body["latency"]["search"]["count"] == 3
    assert body["latency"]["search"]["p95_ms"] in metering.BUCKETS_MS
    assert body["latency"]["answer"]["count"] == 1 and body["latency"]["answer"]["errors"] == 0
    assert body["size"]["documents"] >= 1 and body["size"]["chunks"] >= 1
    assert [c["channel"] for c in body["by_channel"]] == ["api"]
    # A refused question is not an answer that failed.
    client.post("/assistant/chat", json={"question": "q", "model": "nope"}, headers=READER)
    assert client.get("/usage", headers=ADMIN).json()["latency"]["answer"]["count"] == 1


def test_only_an_admin_reads_usage_and_days_are_bounded(region):
    _, client = region
    denied = client.get("/usage", headers=READER)
    assert denied.status_code == 403 and denied.json()["code"] == "PERMISSION_DENIED"
    assert client.get("/usage", params={"days": 0}, headers=ADMIN).status_code == 422
    assert client.get("/usage", params={"days": 9999}, headers=ADMIN).status_code == 422


def test_a_customers_admin_sees_activity_and_not_the_operators_cost(tmp_path, monkeypatch):
    from pheasant.services import ServiceContext

    monkeypatch.setenv("TEST_FOUNDRY_KEY", "k")
    home = "11111111-2222-3333-4444-555555555555"
    operator = "00000000-0000-0000-0000-0000000000a0"
    config = _config(
        tmp_path,
        entra={
            "enabled": True,
            "tenant_id": home,
            "client_id": "c",
            "delegated_access": True,
            "role_assignments": {operator: "Admin"},
        },
    )
    from pheasant.sync.engine import SyncEngine

    engine = SyncEngine(config)
    engine.state.migrate()
    context = ServiceContext(config=config, state=engine.state, searcher=None)
    METER.model_call("chat", "gpt-6-luna", 1000, 100)

    def as_admin(subject: str, tenant: str) -> dict:
        token = bind_identity(
            Identity(
                "a@x.example", "entra_sso", subject=subject, roles=("Admin",), tenant_id=tenant
            )
        )
        try:
            return usage.describe(context)
        finally:
            reset_identity(token)

    theirs = as_admin("00000000-0000-0000-0000-00000000beef", ORG)
    assert theirs["cost_visible"] is False and theirs["currency"] is None
    assert theirs["totals"]["cost"] is None and theirs["totals"]["input_tokens"] == 1000
    assert theirs["by_model"][0]["cost"] is None and theirs["unpriced_models"] == []
    ours = as_admin(operator, home)
    assert ours["cost_visible"] is True and ours["totals"]["cost"] > 0
    engine.close()
    assert not METER.bound  # closing the engine that started the meter stops it


def test_two_writers_add_rather_than_overwrite(region):
    app, _ = region
    state, key = app.state.state, ("2026-01-02", "chat", "m", "", "p", "api")
    for _ in range(2):
        usage_ledger.add(
            state, "kb", {key: Totals(requests=1, input_tokens=10, buckets=[1] + [0] * 9)}
        )
    (row,) = usage_ledger.window(state, "kb", "2026-01-02", "2026-01-02")
    assert (row["requests"], row["input_tokens"], row["b0"]) == (2, 20, 2)


def test_off_nothing_is_counted_and_nothing_is_served(tmp_path, monkeypatch):
    from pheasant.assistant import providers

    monkeypatch.setenv("TEST_FOUNDRY_KEY", "k")
    monkeypatch.setenv("PHEASANT_API_TOKEN", "admin-token")
    monkeypatch.setattr(providers, "_http_json", _reply)
    config = _config(tmp_path, usage_on=False)
    METER.unbind()
    app = create_app(config, config_path=str(tmp_path / "pheasant.yaml"))
    with TestClient(app) as client:
        assert not METER.bound
        _call(config, None)
        assert client.post("/search", json={"query": "roost"}, headers=ADMIN).status_code == 200
        assert METER.flush() == 0
        assert app.state.state.rows("SELECT COUNT(*) AS n FROM usage_daily")[0]["n"] == 0
        refused = client.get("/usage", headers=ADMIN)
        assert refused.status_code == 404 and refused.json()["code"] == "USAGE_NOT_ENABLED"
    app.state.engine.close()


def test_a_percentile_is_the_bound_of_the_bucket_it_falls_in():
    buckets = [0] * 10
    buckets[metering.bucket_of(300)] += 90  # 250 < 300 <= 500
    buckets[metering.bucket_of(5000)] += 9
    buckets[metering.bucket_of(900_000)] += 1  # slower than every bound
    assert usage._percentile(buckets, 0.5) == 500
    assert usage._percentile(buckets, 0.95) == 8000
    assert usage._percentile(buckets, 1.0) == metering.BUCKETS_MS[-1] * 2
    assert usage._percentile([0] * 10, 0.5) is None
