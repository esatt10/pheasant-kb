"""The traces behind the usage totals, and an Admin's default models.

Acceptance:

1. An answer leaves its models, route, tokens and steps on its ledger row,
   its organization as usage keys it, and the web UI's conversation as its
   session.
2. Sessions are read when asked, newest first, filtered by person,
   organization or model, bounded by the window; a call outside a session is
   its own, keyed ``trace:<id>``; a window past the ledger's retention says so.
3. A session's calls come back in order with a KQL query naming their trace
   ids and the session's own time span; JSON lines export the same rows.
4. Admin only, on both surfaces; a region with no ledger says so.
5. An Admin's default models are laid over the catalog for every answer and
   listing, and ``null`` returns to the deployment's.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from pheasant.persistence.state_store import StateStore
from pheasant.security.authorization import PermissionDenied
from pheasant.security.identity import (
    PERSONAL_KEY,
    Identity,
    bind_identity,
    reset_identity,
)
from pheasant.services import ServiceContext, model_routing, models, usage_traces
from pheasant.telemetry.interactions import (
    InteractionBuffer,
    InteractionContext,
    InteractionEvent,
    Modality,
    NullSink,
    StateSink,
    observe,
)
from tests.test_model_catalog import ADMIN, READER, _config
from tests.test_model_catalog import region as region

KB = "kb"


def _at(minutes_ago: float) -> str:
    moment = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _event(index: int, *, minutes_ago: float = 5, **attributes) -> InteractionEvent:
    session = attributes.pop("session", None)
    principal = attributes.pop("principal", "alice@contoso.com")
    return InteractionEvent(
        kb_id=KB,
        operation="/assistant",
        modality="ui",
        trace_id=f"{index:032x}",
        span_id=f"{index:016x}",
        started_at=_at(minutes_ago),
        duration_ms=1000.0 + index,
        principal=principal,
        session_id=session,
        query_text=f"question {index}",
        answer_text=f"answer {index}",
        attributes=attributes,
    )


@pytest.fixture
def context(tmp_path):
    config = _config(tmp_path)
    config.observability.interactions.enabled = True
    config.observability.interactions.hot_retention_days = 7
    state = StateStore(tmp_path / "traces.db")
    state.migrate()
    yield ServiceContext(config, state, None)
    state.close()


@pytest.fixture
def admin():
    token = bind_identity(Identity("admin", PERSONAL_KEY, roles=("Admin",)))
    yield
    reset_identity(token)


def _write(context, *events):
    StateSink(context.state).write(list(events))


# ------------------------------------------------------------------ writing


def test_an_answer_leaves_its_shape_on_its_ledger_row():
    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)
    token = bind_identity(
        Identity("alice@contoso.com", PERSONAL_KEY, roles=("Reader",), tenant_id="contoso")
    )
    try:
        with observe(
            buffer, InteractionContext.create(Modality.UI), kb_id=KB, operation="/a"
        ) as event:
            usage_traces.annotate_answer(
                SimpleNamespace(conversation_id="conv-1"),
                {
                    "models": {"chat": "gpt-6-sol", "decision": "ms-decision-1"},
                    "model_routing": {
                        "mode": "openai",
                        "outcome": "decided",
                        "selected": "gpt-6-sol",
                    },
                    "route": {"intent": "knowledge", "depth": "long", "visual": "none"},
                    "steps": [
                        {
                            "name": "plan",
                            "detail": "x" * 1000,
                            "model": "gpt-6-sol",
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "duration_seconds": 0.5,
                        },
                        {
                            "name": "synthesize",
                            "detail": "wrote",
                            "input_tokens": 100,
                            "output_tokens": 50,
                        },
                    ],
                },
            )
    finally:
        reset_identity(token)
    assert event.session_id == "conv-1", "the UI's conversation is the session"
    assert event.attributes["chat_model"] == "gpt-6-sol"
    assert event.attributes["decision_model"] == "ms-decision-1"
    assert event.attributes["tenant_id"] == "contoso", "keyed as the usage ledger keys it"
    answer = event.attributes["answer"]
    assert answer["tokens"] == {"input_tokens": 110, "output_tokens": 55}
    assert answer["route"]["depth"] == "long" and answer["routing"]["outcome"] == "decided"
    assert len(answer["steps"][0]["detail"]) == usage_traces.MAX_STEP_DETAIL


def test_a_call_without_a_person_is_the_regions_own():
    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)
    with observe(
        buffer, InteractionContext.create(Modality.MCP), kb_id=KB, operation="/a"
    ) as event:
        pass
    assert event.attributes["tenant_id"] == ""


# ------------------------------------------------------------------ reading


def test_sessions_are_grouped_filtered_and_bounded_by_the_window(context, admin):
    _write(
        context,
        _event(1, session="conv-a", minutes_ago=50, tenant_id="contoso", chat_model="gpt-6-luna"),
        _event(2, session="conv-a", minutes_ago=40, tenant_id="contoso", chat_model="gpt-6-sol"),
        _event(
            3,
            session="conv-b",
            minutes_ago=30,
            tenant_id="fabrikam",
            chat_model="gpt-6-luna",
            principal="bob@fabrikam.com",
        ),
        _event(4, minutes_ago=20, tenant_id="", chat_model="gpt-6-luna", principal=None),
        _event(5, session="conv-old", minutes_ago=60 * 24 * 10, tenant_id="contoso"),
    )
    listed = usage_traces.sessions(context, days=7)
    assert [s["session"] for s in listed["sessions"]] == [
        f"trace:{4:032x}",
        "conv-b",
        "conv-a",
    ], "newest first; a call outside a session is its own; the 10-day-old one is outside"
    conv_a = next(s for s in listed["sessions"] if s["session"] == "conv-a")
    assert conv_a["calls"] == 2 and conv_a["principal"] == "alice@contoso.com"

    by_org = usage_traces.sessions(context, days=7, organization="fabrikam")
    assert [s["session"] for s in by_org["sessions"]] == ["conv-b"]
    own = usage_traces.sessions(context, days=7, organization="")
    assert [s["session"] for s in own["sessions"]] == [f"trace:{4:032x}"]
    by_person = usage_traces.sessions(context, days=7, principal="alice@contoso.com")
    assert [s["session"] for s in by_person["sessions"]] == ["conv-a"]
    by_model = usage_traces.sessions(context, days=7, model="gpt-6-sol")
    assert [s["session"] for s in by_model["sessions"]] == ["conv-a"]
    assert f"'{2:032x}'" in by_model["kql"], "a selection's KQL names its traces"
    assert by_model["kql_note"] is None
    assert f"'{3:032x}'" in by_org["kql"] and f"'{1:032x}'" not in by_org["kql"]

    deeper = usage_traces.sessions(context, days=30)
    assert "conv-old" in [s["session"] for s in deeper["sessions"]]
    assert deeper["retention"]["complete"] is False and "7 days" in deeper["retention"]["note"]
    assert listed["retention"]["complete"] is True


def test_a_session_comes_back_in_order_with_a_kql_query(context, admin):
    _write(
        context,
        _event(7, session="conv-x", minutes_ago=30, answer={"steps": [{"name": "plan"}]}),
        _event(6, session="conv-x", minutes_ago=31),
    )
    found = usage_traces.session(context, "conv-x", days=7)
    assert [call["trace_id"] for call in found["calls"]] == [f"{6:032x}", f"{7:032x}"]
    assert found["calls"][1]["answer_trace"] == {"steps": [{"name": "plan"}]}
    kql = found["kql"]
    assert f"'{6:032x}'" in kql and f"'{7:032x}'" in kql
    assert "OperationId in (traces)" in kql and "between (datetime(" in kql
    assert "AppRequests" in kql and "AppDependencies" in kql
    with pytest.raises(usage_traces.SessionNotFound):
        usage_traces.session(context, "conv-none", days=7)


def test_a_call_outside_a_session_is_found_by_its_trace(context, admin):
    _write(context, _event(9, minutes_ago=3))
    found = usage_traces.session(context, f"trace:{9:032x}", days=1)
    assert [call["question"] for call in found["calls"]] == ["question 9"]


def test_the_export_is_one_json_line_per_row(context, admin):
    _write(
        context,
        _event(11, session="conv-e", minutes_ago=3, chat_model="gpt-6-luna"),
        _event(12, session="conv-e", minutes_ago=2),
        _event(13, session="conv-f", minutes_ago=1),
    )
    lines = list(usage_traces.export_lines(context, days=1, session_key="conv-e"))
    rows = [json.loads(line) for line in lines]
    assert [row["trace_id"] for row in rows] == [f"{11:032x}", f"{12:032x}"]
    assert rows[0]["attributes"]["chat_model"] == "gpt-6-luna"
    assert all(line.endswith("\n") for line in lines)
    assert len(list(usage_traces.export_lines(context, days=1))) == 3


def test_traces_are_admin_only_and_need_a_ledger(context):
    token = bind_identity(Identity("reader", PERSONAL_KEY, roles=("Reader",)))
    try:
        with pytest.raises(PermissionDenied):
            usage_traces.sessions(context, days=7)
    finally:
        reset_identity(token)
    context.config.observability.interactions.enabled = False
    token = bind_identity(Identity("admin", PERSONAL_KEY, roles=("Admin",)))
    try:
        with pytest.raises(usage_traces.TracesNotKept):
            usage_traces.sessions(context, days=7)
    finally:
        reset_identity(token)


@pytest.mark.parametrize("days", [0, 367, "seven"])
def test_a_window_outside_the_range_is_refused(context, admin, days):
    with pytest.raises(usage_traces.InvalidRequest):
        usage_traces.sessions(context, days=days)


def test_the_kql_for_a_window_starts_with_the_time_filter():
    query = usage_traces.kql(days=30)
    lines = query.splitlines()
    assert lines[0].startswith("union isfuzzy=true")
    assert lines[1] == "| where TimeGenerated > ago(30d)"
    assert "it's" not in usage_traces.kql(trace_ids=["it's"])


# ------------------------------------------------------------- default models


def test_an_admin_default_model_is_laid_over_the_catalog(context, admin):
    provisioned = context.config.models.default_chat
    other = next(m.id for m in context.config.models.chat if m.id != provisioned)
    described = models.set_defaults(context, {"chat": other})
    assert described["default_chat"] == other
    assert described["deployment_defaults"]["chat"] == provisioned
    assert described["can_set_defaults"] is True
    assert next(m for m in described["chat"] if m["id"] == other)["default"] is True
    effective = models.effective(context)
    assert effective.config.models.default_chat == other
    assert context.config.models.default_chat == provisioned, "the shared config is untouched"
    assert models.chosen(effective.config)["chat"] == other
    assert model_routing.defaults(context)["mode"] in {"openai", "anthropic", "off"}

    back = models.set_defaults(context, {"chat": None})
    assert back["default_chat"] == provisioned
    assert models.effective(context) is context


def test_a_default_model_must_be_in_the_catalog_and_set_by_an_admin(context, admin):
    with pytest.raises(models.UnknownModel):
        models.set_defaults(context, {"chat": "invented"})
    with pytest.raises(models.InvalidRequest):
        models.set_defaults(context, {"embedding": "x"})
    token = bind_identity(Identity("reader", PERSONAL_KEY, roles=("Reader",)))
    try:
        with pytest.raises(PermissionDenied):
            models.set_defaults(context, {"chat": context.config.models.default_chat})
    finally:
        reset_identity(token)


def test_a_saved_default_the_catalog_no_longer_lists_is_ignored(context, admin):
    other = next(
        m.id for m in context.config.models.chat if m.id != context.config.models.default_chat
    )
    models.set_defaults(context, {"chat": other})
    context.config.models.chat = [m for m in context.config.models.chat if m.id != other]
    assert models.effective(context) is context


# ------------------------------------------------------------------ surfaces


def test_both_surfaces_drill_down_and_set_defaults_alike(region):
    app, client = region
    app.state.config.observability.interactions.enabled = True
    StateSink(app.state.state).write(
        [_event(21, session="conv-h", minutes_ago=2, chat_model="gpt-6-luna")]
    )
    assert client.get("/usage/sessions", headers=READER).status_code == 403
    listed = client.get("/usage/sessions", params={"days": 1}, headers=ADMIN)
    assert listed.status_code == 200, listed.text
    assert [s["session"] for s in listed.json()["sessions"]] == ["conv-h"]
    found = client.get("/usage/sessions/conv-h", params={"days": 1}, headers=ADMIN)
    assert found.status_code == 200 and found.json()["calls"][0]["question"] == "question 21"
    exported = client.get("/usage/sessions/conv-h/export", params={"days": 1}, headers=ADMIN)
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in exported.headers["content-disposition"]
    assert json.loads(exported.text.splitlines()[0])["session_id"] == "conv-h"
    missing = client.get("/usage/sessions/conv-none", params={"days": 1}, headers=ADMIN)
    assert missing.status_code == 404 and missing.json()["code"] == "SESSION_NOT_FOUND"

    from pheasant.mcp_server.model_tools import ModelTools

    context = ServiceContext(app.state.config, app.state.state, None)

    class Tools(ModelTools):
        services = context

        def _require_knowledge_base(self, knowledge_base):
            context.knowledge_base(knowledge_base)

    tools = Tools()
    token = bind_identity(Identity("admin", PERSONAL_KEY, roles=("Admin",)))
    try:
        assert tools.list_usage_sessions("kb", days=1)["sessions"] == listed.json()["sessions"]
        assert tools.get_usage_session("kb", "conv-h", days=1)["calls"] == found.json()["calls"]
        catalog = app.state.config.models
        other = next(m.id for m in catalog.chat if m.id != catalog.default_chat)
        via_mcp = tools.set_default_models("kb", chat=other)
        assert via_mcp["default_chat"] == other
        assert (
            tools.set_default_models("kb", chat="deployment")["default_chat"]
            == catalog.default_chat
        )
    finally:
        reset_identity(token)
    assert client.put("/models/defaults", json={"chat": other}, headers=READER).status_code == 403
    saved = client.put("/models/defaults", json={"chat": other}, headers=ADMIN)
    assert saved.status_code == 200 and saved.json()["default_chat"] == other
    assert client.get("/models", headers=READER).json()["default_chat"] == other


def test_a_selection_past_the_trace_cap_filters_by_time_and_model(context, admin, monkeypatch):
    monkeypatch.setattr(usage_traces, "MAX_KQL_TRACES", 1)
    _write(
        context,
        _event(31, session="conv-m", minutes_ago=3, chat_model="gpt-6-sol"),
        _event(32, session="conv-n", minutes_ago=2, chat_model="gpt-6-sol"),
    )
    listed = usage_traces.sessions(context, days=1, model="gpt-6-sol")
    assert "OperationId in" not in listed["kql"]
    assert "pheasant.chat_model" in listed["kql"] and "'gpt-6-sol'" in listed["kql"]
    assert "More than 1 calls" in listed["kql_note"]


def test_a_streamed_answer_is_annotated_on_its_own_row_not_late_on_the_request():
    """A stream's request row is handed to the buffer before the answer exists;
    writing to it then races the flush. The answer goes on its child row."""
    from pheasant.telemetry.interactions import child_event

    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)
    token = bind_identity(
        Identity("alice@contoso.com", PERSONAL_KEY, roles=("Reader",), tenant_id="contoso")
    )
    try:
        with observe(
            buffer, InteractionContext.create(Modality.UI), kb_id=KB, operation="/a"
        ) as parent:
            pass
    finally:
        reset_identity(token)
    assert parent.handed_over
    payload = {"models": {"chat": "gpt-6-sol"}, "steps": [{"name": "plan"}]}
    from pheasant.telemetry import interactions

    reset = interactions._CURRENT_EVENT.set(parent)  # a worker that copied the request's context
    try:
        usage_traces.annotate_answer(SimpleNamespace(conversation_id="conv-s"), payload)
    finally:
        interactions._CURRENT_EVENT.reset(reset)
    assert "answer" not in parent.attributes and parent.session_id is None

    child = child_event(parent, "/assistant/chat/stream")
    assert child.attributes["tenant_id"] == "contoso", "the child keeps its caller's organization"
    usage_traces.annotate_answer(SimpleNamespace(conversation_id="conv-s"), payload, event=child)
    assert child.attributes["chat_model"] == "gpt-6-sol" and child.session_id == "conv-s"
    assert child.attributes["answer"]["steps"][0]["name"] == "plan"
    assert "handed_over" not in child.as_json(), "the marker never reaches the queue"


def test_a_stream_is_one_call_of_its_session(context, admin):
    request = _event(41, session="conv-t", minutes_ago=2)
    answer = _event(42, session="conv-t", minutes_ago=1, chat_model="gpt-6-luna")
    answer.trace_id = request.trace_id
    answer.parent_span_id = request.span_id
    answer.operation = "/assistant/chat/stream"
    _write(context, request, answer)
    listed = usage_traces.sessions(context, days=1)
    assert next(s for s in listed["sessions"] if s["session"] == "conv-t")["calls"] == 1
    found = usage_traces.session(context, "conv-t", days=1)
    assert [c["operation"] for c in found["calls"]] == ["/assistant/chat/stream"]
    assert len(list(usage_traces.export_lines(context, days=1, session_key="conv-t"))) == 2
