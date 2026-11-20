"""An operator's kill switch and monthly spend limit over one knowledge base.

Acceptance:

1. This calendar month's chat, judgement and embedding spend is costed at the
   catalog's stated prices from the ledger plus this process's unwritten calls;
   last month and unpriced models do not count, and unpriced ones are named.
2. The limit is ``models.monthly_spend_limit`` (500 by default) unless an
   operator set another; at it every chat, judgement and embedding call is
   refused before it is sent, an answer is refused before it starts, and a
   sync that would embed is skipped rather than failed.
3. The kill switch refuses every request but an operator admin's, on HTTP and
   MCP alike, while ``/auth/me`` still answers; turning it off restores access.
4. Only an operator admin changes either; an Admin a customer granted reads
   whether access is suspended and the share of the limit used, never amounts.
5. Both surfaces read the same controls.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.persistence import usage_ledger
from pheasant.persistence.state_store import StateStore
from pheasant.security.identity import ENTRA_TOKEN, Identity, bind_identity, reset_identity
from pheasant.services import ServiceContext, region_controls
from pheasant.services.errors import InvalidRequest, OperatorRequired, SpendLimitRefused
from pheasant.sync import limits
from pheasant.telemetry import spend
from pheasant.telemetry.usage import METER, Totals
from tests.test_usage_metering import _config

TENANT = "11111111-2222-3333-4444-555555555555"
PINNED = "aaaaaaaa-0000-0000-0000-000000000001"


def _spend_rows(state: Any, *, day: str, model: str, input_tokens: int, output_tokens: int = 0):
    """Ledger rows worth a known cost: gpt-6-luna is 1.0 in / 4.0 out per million."""

    usage_ledger.add(
        state,
        "kb",
        {
            (day, "chat", model, "", "someone", "ui"): Totals(
                requests=1, input_tokens=input_tokens, output_tokens=output_tokens
            )
        },
    )


@pytest.fixture
def region(tmp_path: Path):
    config = _config(tmp_path)
    state = StateStore(tmp_path / "controls.db")
    state.migrate()
    spend.GUARD.bind(config, state)
    yield ServiceContext(config, state, None)
    spend.GUARD.unbind(state)
    state.close()


@pytest.fixture
def operator():
    token = bind_identity(Identity("shared-token", "api_token", roles=("Admin",)))
    yield
    reset_identity(token)


def _granted_admin(config: Any) -> Any:
    """An Admin a customer granted: a person, not one the deployment pinned."""

    config.security.entra.delegated_access = True
    config.security.entra.tenant_id = TENANT
    config.security.entra.role_assignments = {PINNED: "Admin"}
    return bind_identity(
        Identity(
            "ada@contoso.com", ENTRA_TOKEN, subject="b" * 8, roles=("Admin",), tenant_id=TENANT
        )
    )


TODAY = datetime.now(UTC).date()
LAST_MONTH = (TODAY.replace(day=1) - timedelta(days=1)).isoformat()


def test_the_month_is_costed_at_stated_prices(region):
    state = region.state
    _spend_rows(state, day=TODAY.isoformat(), model="gpt-6-luna", input_tokens=10_000_000)
    _spend_rows(
        state, day=TODAY.isoformat(), model="gpt-6-luna", input_tokens=0, output_tokens=2_500_000
    )
    _spend_rows(state, day=LAST_MONTH, model="gpt-6-luna", input_tokens=900_000_000)
    _spend_rows(state, day=TODAY.isoformat(), model="gpt-6-sol", input_tokens=5_000_000)
    month = spend.month_spend(region.config, state, pending=False)
    assert month["spent"] == pytest.approx(10.0 + 10.0), "this month only, at 1.0 in / 4.0 out"
    assert month["unpriced_models"] == ["gpt-6-sol"], "an unpriced model is named, not costed"


def test_the_default_limit_is_500_and_refuses_every_model_call(region, operator):
    from pheasant.assistant import providers

    config, state = region.config, region.state
    assert config.models.monthly_spend_limit == 500.0
    _spend_rows(state, day=TODAY.isoformat(), model="gpt-6-luna", input_tokens=499_000_000)
    spend.GUARD.forget()
    providers.note_model_call("gpt-6-luna")  # $499 of $500: still allowed
    region_controls.require_budget()
    _spend_rows(state, day=TODAY.isoformat(), model="gpt-6-luna", input_tokens=2_000_000)
    spend.GUARD.forget()
    with pytest.raises(spend.SpendLimitReached, match=r"\$501\.00 of \$500\.00"):
        providers.note_model_call("gpt-6-luna")
    with pytest.raises(SpendLimitRefused) as refused:
        region_controls.require_budget()
    assert refused.value.code == "SPEND_LIMIT_REACHED" and refused.value.status == 402


def test_an_embedding_request_is_refused_before_it_is_sent(region, monkeypatch):
    from pheasant.search import embedding_client

    _spend_rows(region.state, day=TODAY.isoformat(), model="gpt-6-luna", input_tokens=600_000_000)
    spend.GUARD.forget()
    sent: list[Any] = []
    monkeypatch.setattr(embedding_client, "_transport", lambda *a, **k: sent.append(a))
    monkeypatch.setenv("TEST_EMBED_KEY", "k")
    client = embedding_client.OpenAISpecEmbedder(
        base_url="https://example.invalid/v1",
        model="text-embedding-3-small",
        api_key_env="TEST_EMBED_KEY",
    )
    with pytest.raises(spend.SpendLimitReached):
        client.embed(["a roosting pheasant"])
    assert sent == [], "nothing reached the provider"


def test_this_processs_unwritten_calls_count_at_once(region, monkeypatch):
    """A call counts from the moment it is metered: while it waits for a flush,
    while a flush is writing it, and once the ledger holds it, even with a
    ledger total cached from before. CI caught the middle case: a background
    flush had taken the rows and not yet written them, and the guard saw
    them in neither place."""

    state = region.state
    METER.bind(state, "kb")
    try:
        _spend_rows(state, day=TODAY.isoformat(), model="gpt-6-luna", input_tokens=499_500_000)
        spend.GUARD.forget()
        assert spend.GUARD.over() is None, "$499.50 of $500, and the total is now cached"
        monkeypatch.setattr(METER, "_maybe_flush", lambda: None)  # this test drives the flush
        METER.model_call("chat", "gpt-6-luna", 1_000_000)  # $1, not yet written
        assert spend.GUARD.over() is not None, "pending: the guard adds what the meter holds"

        seen_while_writing: list[Any] = []
        write = usage_ledger.add

        def writing(target: Any, kb_id: str, rows: Any, *rest: Any) -> None:
            seen_while_writing.append(spend.GUARD.over())
            write(target, kb_id, rows, *rest)

        monkeypatch.setattr(usage_ledger, "add", writing)
        assert METER.flush() == 1
        assert seen_while_writing and seen_while_writing[0] is not None, "in flight: still counted"
        assert METER.pending_model_rows(spend.month_start()) == [], "written, so no longer pending"
        assert spend.GUARD.over() is not None, "written: the flush cleared the cached total"
    finally:
        METER.unbind(state)


def test_a_sync_that_would_embed_is_skipped_at_the_limit(region):
    config, state = region.config, region.state
    _spend_rows(state, day=TODAY.isoformat(), model="gpt-6-luna", input_tokens=600_000_000)
    spend.GUARD.forget()
    assert limits.blocks(config, state, "incremental", "notes") is None, "the stub embedder is free"
    config.search.embeddings.enabled = True
    config.search.embeddings.provider = "openai-spec"
    skipped = limits.blocks(config, state, "incremental", "notes")
    assert skipped is not None and skipped["reason"] == "spend limit reached"
    assert limits.blocks(config, state, "validate_only", "notes") is None, "adds nothing"
    assert limits.blocks(config, state, "repair", "notes") is None


def test_an_operator_raises_lowers_and_resets_the_limit(region, operator):
    state = region.state
    _spend_rows(state, day=TODAY.isoformat(), model="gpt-6-luna", input_tokens=600_000_000)
    described = region_controls.describe(region)
    assert described["can_change"] is True
    assert described["spend"]["spent"] == 600.0 and described["spend"]["limit"] == 500.0
    assert described["spend"]["reached"] is True and described["spend"]["custom_limit"] is False
    assert described["spend"]["resets_on"].endswith("-01")

    raised = region_controls.update(region, {"monthly_spend_limit": 1000})
    assert raised["spend"]["limit"] == 1000.0 and raised["spend"]["reached"] is False
    assert raised["spend"]["remaining"] == 400.0 and raised["spend"]["percent_used"] == 60.0
    assert spend.GUARD.over() is None, "every check reads the new limit at once in this process"

    lowered = region_controls.update(region, {"monthly_spend_limit": "50.5"})
    assert lowered["spend"]["limit"] == 50.5 and lowered["spend"]["reached"] is True

    back = region_controls.update(region, {"monthly_spend_limit": None})
    assert back["spend"]["limit"] == 500.0 and back["spend"]["custom_limit"] is False

    for bad in (-1, "lots", True, float("inf"), 20_000_000):
        with pytest.raises(InvalidRequest):
            region_controls.update(region, {"monthly_spend_limit": bad})
    with pytest.raises(InvalidRequest, match="unknown control"):
        region_controls.update(region, {"budget": 5})


def test_only_an_operator_admin_changes_the_controls(region):
    token = _granted_admin(region.config)
    try:
        _spend_rows(
            region.state, day=TODAY.isoformat(), model="gpt-6-luna", input_tokens=250_000_000
        )
        seen = region_controls.describe(region)
        assert seen["can_change"] is False
        assert seen["spend"]["spent"] is None and seen["spend"]["limit"] is None, (
            "amounts are the operator's"
        )
        assert seen["spend"]["percent_used"] == 50.0, "the share is not an amount"
        with pytest.raises(OperatorRequired):
            region_controls.update(region, {"suspended": True})
        with pytest.raises(OperatorRequired):
            region_controls.update(region, {"monthly_spend_limit": 10_000})
    finally:
        reset_identity(token)
    pinned = bind_identity(
        Identity("op@example.com", ENTRA_TOKEN, subject=PINNED, roles=("Admin",), tenant_id=TENANT)
    )
    try:
        assert region_controls.update(region, {"suspended": True})["access"]["suspended"] is True
    finally:
        reset_identity(pinned)


def test_a_negative_configured_limit_is_refused(tmp_path):
    from pheasant.config.models import ModelsSettings

    with pytest.raises(ValueError, match="monthly_spend_limit"):
        ModelsSettings(monthly_spend_limit=-1)
    assert ModelsSettings(monthly_spend_limit=None).monthly_spend_limit is None


# ------------------------------------------------------------- over HTTP / MCP


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from pheasant.api.app import create_app

    monkeypatch.setenv("TEST_FOUNDRY_KEY", "k")
    monkeypatch.setenv("PHEASANT_API_TOKEN", "admin-token")
    monkeypatch.setenv("READER_TOKEN", "reader-token")
    config = _config(tmp_path, api_auth={"role_tokens": {"READER_TOKEN": "Reader"}})
    app = create_app(config, config_path=str(tmp_path / "pheasant.yaml"))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        yield app, client
    app.state.engine.close()


ADMIN = {"Authorization": "Bearer admin-token"}
READER = {"Authorization": "Bearer reader-token"}


def _mcp(client: TestClient, headers: dict, name: str, arguments: dict) -> Any:
    pytest.importorskip("mcp")
    response = client.post(
        "/mcp/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": {"knowledge_base": "kb", **arguments}},
        },
        headers={**headers, "accept": "application/json, text/event-stream"},
    )
    return response


def _mcp_body(response: Any) -> Any:
    body = response.text
    if "data:" in body:
        body = next(line[5:].strip() for line in body.splitlines() if line.startswith("data:"))
    return json.loads(body)


def test_the_kill_switch_cuts_everyone_but_the_operator(served):
    app, client = served
    assert client.get("/sources", headers=READER).status_code == 200
    turned = client.put(
        "/region/controls", json={"suspended": True, "reason": "billing review"}, headers=READER
    )
    assert turned.status_code == 403, "a Reader cannot reach the controls"
    on = client.put(
        "/region/controls", json={"suspended": True, "reason": "billing review"}, headers=ADMIN
    )
    assert on.status_code == 200 and on.json()["access"]["suspended"] is True
    assert on.json()["access"]["by"] == "shared-token"

    refused = client.get("/sources", headers=READER)
    assert refused.status_code == 403
    assert refused.json()["code"] == "ACCESS_SUSPENDED"
    assert "billing review" in refused.json()["detail"]
    assert client.get("/auth/me", headers=READER).status_code == 200, "who you are still answers"
    assert _mcp(client, READER, "list_sources", {}).status_code == 403, "MCP is cut off too"

    assert client.get("/sources", headers=ADMIN).status_code == 200, "the operator keeps access"
    assert (
        client.get("/region/controls", headers=ADMIN).json()["access"]["reason"] == "billing review"
    )

    off = client.put("/region/controls", json={"suspended": False}, headers=ADMIN)
    assert off.json()["access"] == {"suspended": False, "reason": None, "since": None, "by": None}
    assert client.get("/sources", headers=READER).status_code == 200


def test_an_answer_at_the_limit_is_refused_on_both_surfaces(served):
    app, client = served
    _spend_rows(
        app.state.state, day=TODAY.isoformat(), model="gpt-6-luna", input_tokens=600_000_000
    )
    spend.GUARD.forget()
    http = client.post(
        "/assistant/chat", json={"question": "Where do pheasants roost?"}, headers=READER
    )
    assert http.status_code == 402, http.text
    assert http.json()["code"] == "SPEND_LIMIT_REACHED"
    reply = _mcp_body(
        _mcp(client, READER, "ask_knowledge_base", {"question": "Where do pheasants roost?"})
    )
    text = json.dumps(reply)
    assert "monthly model spend limit" in text and http.json()["detail"][:60] in text
    searched = client.post("/search", json={"query": "pheasants roost"}, headers=READER)
    assert searched.status_code == 200 and searched.json()["results"], "text search still works"


def test_both_surfaces_read_and_set_the_same_controls(served):
    app, client = served
    http = client.get("/region/controls", headers=ADMIN).json()
    mcp = json.loads(
        _mcp_body(_mcp(client, ADMIN, "get_region_controls", {}))["result"]["content"][0]["text"]
    )
    assert mcp == http
    set_ = _mcp_body(_mcp(client, ADMIN, "set_region_controls", {"monthly_spend_limit": 750}))
    assert json.loads(set_["result"]["content"][0]["text"])["spend"]["limit"] == 750.0
    reset = _mcp_body(
        _mcp(client, ADMIN, "set_region_controls", {"monthly_spend_limit": "default"})
    )
    assert json.loads(reset["result"]["content"][0]["text"])["spend"]["limit"] == 500.0
    refused = _mcp_body(_mcp(client, READER, "set_region_controls", {"suspended": True}))
    assert "permission required" in json.dumps(refused)


# ------------------------------------------- the ledger behind the number


def _ledger_rows(state: Any) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in state.rows(
            "SELECT model, requests, input_tokens FROM usage_daily ORDER BY model", ()
        )
    ]


def _assert_append_only(state: Any) -> None:
    """What the spend limit reads can be added to and never taken away."""

    day = TODAY.isoformat()
    _spend_rows(state, day=day, model="gpt-6-luna", input_tokens=1000)
    _spend_rows(state, day=day, model="gpt-6-luna", input_tokens=500)
    assert _ledger_rows(state) == [{"model": "gpt-6-luna", "requests": 2, "input_tokens": 1500}]
    attempts = (
        "DELETE FROM usage_daily",
        "DELETE FROM usage_daily WHERE model='gpt-6-luna'",
        "UPDATE usage_daily SET input_tokens=0",
        "UPDATE usage_daily SET requests=requests-1",
        "UPDATE usage_daily SET day='1999-01-01'",
        "UPDATE usage_daily SET kb_id='elsewhere'",
    )
    for statement in attempts:
        with pytest.raises(Exception, match="append-only"):
            state.execute(statement)
        state.backend.rollback() if hasattr(state.backend, "rollback") else None
    assert _ledger_rows(state) == [{"model": "gpt-6-luna", "requests": 2, "input_tokens": 1500}]
    state.execute("UPDATE usage_daily SET input_tokens=input_tokens+1")  # raising is allowed
    usage_ledger.add(
        state,
        "kb",
        {(day, "chat", "gpt-6-luna", "", "someone", "ui"): Totals(requests=1, input_tokens=-900)},
    )
    assert _ledger_rows(state)[0]["input_tokens"] == 1501, "a negative report adds nothing"


def test_the_usage_ledger_is_append_only(region):
    _assert_append_only(region.state)


def test_a_dropped_trigger_comes_back_on_the_next_start(region):
    state = region.state
    state.execute("DROP TRIGGER usage_daily_no_delete")
    state.migrate()
    _spend_rows(state, day=TODAY.isoformat(), model="gpt-6-luna", input_tokens=10)
    with pytest.raises(Exception, match="append-only"):
        state.execute("DELETE FROM usage_daily")


@pytest.mark.skipif(
    not __import__("os").environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip(),
    reason="set PHEASANT_TEST_POSTGRES_DSN to a throwaway database",
)
def test_the_usage_ledger_is_append_only_on_a_real_postgres(tmp_path):
    from pheasant.persistence.backends import open_backend

    backend = open_backend(
        backend="postgres", dsn=__import__("os").environ["PHEASANT_TEST_POSTGRES_DSN"]
    )
    state = StateStore(backend=backend)
    try:
        state.migrate()
        state.migrate()  # the second boot finds the triggers and leaves them
        state.execute("ALTER TABLE usage_daily DISABLE TRIGGER usage_daily_append_only")
        state.execute("DELETE FROM usage_daily WHERE kb_id='kb'")
        state.execute("ALTER TABLE usage_daily ENABLE TRIGGER usage_daily_append_only")
        _assert_append_only(state)
        with pytest.raises(Exception, match="append-only"):
            state.execute("TRUNCATE usage_daily")
    finally:
        state.close()


def test_nothing_in_the_code_deletes_or_lowers_usage(tmp_path):
    """The only statement that writes ``usage_daily`` is the additive upsert."""

    import re

    from tests.conftest import REPO_ROOT

    pattern = re.compile(r"(DELETE\s+FROM|TRUNCATE(\s+TABLE)?|UPDATE)\s+usage_daily", re.I)
    offenders = []
    for folder in ("src", "deploy", "scripts"):
        for path in (REPO_ROOT / folder).rglob("*"):
            if path.suffix not in {".py", ".sh", ".sql", ".tftpl"} or not path.is_file():
                continue
            for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if pattern.search(line) and "usage_ledger.py" not in str(path):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
    assert offenders == []


def test_a_failed_flush_keeps_what_it_did_not_write(region, monkeypatch):
    """A briefly unreachable database must not make spend disappear, and a
    retry must not count a row twice."""

    state = region.state
    METER.bind(state, "kb")
    try:
        monkeypatch.setattr(METER, "_maybe_flush", lambda: None)
        METER.model_call("chat", "gpt-6-luna", 1000)
        METER.model_call("embedding", "text-embedding-3-small", 400)
        write = usage_ledger._add_one
        calls = {"n": 0}

        def flaky(state_: Any, kb_id: str, key: Any, totals: Any, *rest: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise ConnectionError("database went away")
            write(state_, kb_id, key, totals, *rest)

        monkeypatch.setattr(usage_ledger, "_add_one", flaky)
        assert METER.flush() == 1, "the first row landed, the second did not"
        assert len(METER.pending_model_rows(spend.month_start())) == 1, "kept, not dropped"
        monkeypatch.setattr(usage_ledger, "_add_one", write)
        assert METER.flush() == 1
        rows = {
            row["model"]: row["input_tokens"]
            for row in state.rows("SELECT model, input_tokens FROM usage_daily", ())
        }
        assert rows == {"gpt-6-luna": 1000, "text-embedding-3-small": 400}, "each exactly once"
    finally:
        METER.unbind(state)


def test_the_fleet_directorys_mirror_is_append_only_too(tmp_path):
    from types import SimpleNamespace

    from pheasant.persistence import fleet_store

    config = SimpleNamespace(
        fleet=SimpleNamespace(enabled=True, slot="kb-01", path=tmp_path / "fleet.db", dsn_env="")
    )
    store = fleet_store.open_store(config)
    try:
        _spend_rows(store, day=TODAY.isoformat(), model="gpt-6-luna", input_tokens=5)
        with pytest.raises(Exception, match="append-only"):
            store.execute("DELETE FROM usage_daily")
    finally:
        fleet_store.close_all()
