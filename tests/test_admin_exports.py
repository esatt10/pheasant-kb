"""A diagram request is traced, and the Admin page's measures leave as files.

Acceptance:

1. A diagram asked for on its own (``POST /assistant/visual``, MCP
   ``create_visual``) leaves a ledger row carrying the request as its
   question, its conversation, the model and tokens that drew it and its
   steps, so the Usage drill-down's Questions view shows it.
2. ``export_usage`` writes the Usage tab's measures and this month's spend as
   a ZIP of CSV tables or as one JSON document, identically over HTTP and MCP,
   with the operator's amounts withheld from anyone else.
3. ``export_usage_traces`` pages the same JSON lines HTTP downloads.
4. All of it is an Admin's.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import zipfile
from types import SimpleNamespace
from typing import Any

import pytest

from pheasant.persistence import usage_ledger
from pheasant.services import usage_export, usage_traces
from pheasant.services.errors import InvalidRequest
from pheasant.telemetry.interactions import StateSink, process_buffer
from pheasant.telemetry.usage import Totals
from tests.test_usage_traces import _event, _mcp, _mcp_refused
from tests.test_usage_traces import roles as roles  # noqa: F401 - the shared fixture

ADMIN = {"Authorization": "Bearer admin-token"}
READER = {"Authorization": "Bearer reader-token"}


def _mcp_json(client: Any, name: str, arguments: dict) -> Any:
    reply = _mcp(client, "admin-token", name, arguments)
    assert not _mcp_refused(reply), json.dumps(reply)[:400]
    return json.loads(reply["result"]["content"][0]["text"])


def _unreadable_drawing(url, payload, headers, timeout):
    """A model that answers but draws nothing usable: the graph's edges stand in."""

    return {
        "choices": [{"message": {"content": "no diagram here"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 40},
    }


def test_a_diagram_request_is_traced_as_a_question(roles, monkeypatch):
    from pheasant.assistant import providers

    app, client = roles
    monkeypatch.setattr(providers, "_http_json", _unreadable_drawing)
    drawn = client.post(
        "/assistant/visual",
        json={
            "request": "draw how pheasants roost",
            "kind": "flow",
            "conversation_id": "conv-draw",
        },
        headers=ADMIN,
    )
    assert drawn.status_code == 200, drawn.text
    process_buffer().flush()

    found = client.get(
        "/usage/sessions", params={"days": 1, "match": "pheasants roost"}, headers=ADMIN
    ).json()
    assert [s["session"] for s in found["sessions"]] == ["conv-draw"], (
        "the Questions view shows it, filed under its conversation"
    )
    call = client.get("/usage/sessions/conv-draw", params={"days": 1}, headers=ADMIN).json()[
        "calls"
    ][0]
    assert call["question"] == "draw how pheasants roost"
    assert call["answer"], "a one-line outcome says what was drawn or why not"
    trace = call["answer_trace"]
    assert trace["route"]["intent"] == "visual" and trace["workflow"] == "visual"
    assert [step["name"] for step in trace["steps"]] == ["passages", "draw"]
    status = drawn.json()["visual"]["status"] if drawn.json()["visual"] else "none"
    assert trace["status"] == status


def test_the_annotation_keeps_a_question_already_set():
    """MCP fills ``query_text`` from an argument named ``query``; a row that
    already has its question keeps it."""

    from pheasant.telemetry import interactions

    event = _event(70)
    event.query_text = "already asked"
    reset = interactions._CURRENT_EVENT.set(event)
    try:
        usage_traces.annotate_visual(
            SimpleNamespace(
                request="draw it", node_ids=["a", "b"], kind=None, conversation_id=None
            ),
            {"visual": {"type": "diagram", "status": "declined", "reason": "no passages"}},
        )
    finally:
        interactions._CURRENT_EVENT.reset(reset)
    assert event.query_text == "already asked"
    assert event.answer_text == "answer 70", "an answer already on the row is kept too"
    assert event.attributes["visual"]["status"] == "declined"
    assert event.attributes["visual"]["named_passages"] == 2
    assert "2 named passage(s)" in event.attributes["answer"]["steps"][0]["detail"]


def _seed_usage(app: Any) -> None:
    from datetime import UTC, datetime

    day = datetime.now(UTC).date().isoformat()
    usage_ledger.add(
        app.state.state,
        "kb",
        {
            (day, "chat", "gpt-6-luna", "", "alice@contoso.com", "ui"): Totals(
                requests=3, input_tokens=3000, output_tokens=300
            ),
            (day, "answer", "", "", "alice@contoso.com", "ui"): Totals(
                requests=3, duration_ms=4200.0, buckets=[0, 0, 1, 2, 0, 0, 0, 0, 0, 0]
            ),
        },
    )


@pytest.fixture
def usage_on(roles):
    app, client = roles
    app.state.config.observability.usage = True
    app.state.engine.config.observability.usage = True
    _seed_usage(app)
    return app, client


def test_the_measures_export_as_csv_tables_and_json(usage_on):
    app, client = usage_on
    response = client.get("/usage/export", params={"days": 7, "format": "csv"}, headers=ADMIN)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="kb-usage-' in response.headers["content-disposition"]
    bundle = zipfile.ZipFile(io.BytesIO(response.content))
    names = {name.split("/", 1)[1] for name in bundle.namelist()}
    assert names == {
        "totals.csv",
        "by_day.csv",
        "by_model.csv",
        "by_organization.csv",
        "by_channel.csv",
        "people.csv",
        "latency.csv",
        "spend.csv",
        "report.json",
    }

    def table(name: str) -> list[dict[str, str]]:
        member = next(n for n in bundle.namelist() if n.endswith(f"/{name}"))
        return list(csv.DictReader(io.StringIO(bundle.read(member).decode("utf-8"))))

    assert table("totals.csv")[0]["questions"] == "3"
    assert table("by_model.csv")[0]["model"] == "gpt-6-luna"
    assert table("people.csv")[0]["principal"] == "alice@contoso.com"
    assert {row["operation"] for row in table("latency.csv")} == {"answer", "search"}
    assert "spent" in table("spend.csv")[0] and "access_suspended" in table("spend.csv")[0]

    as_json = client.get("/usage/export", params={"days": 7, "format": "json"}, headers=ADMIN)
    document = as_json.json()
    assert document["usage"] == client.get("/usage", params={"days": 7}, headers=ADMIN).json()
    assert document["controls"]["spend"]["limit"] == 500.0

    envelope = client.get(
        "/usage/export", params={"days": 7, "format": "csv", "download": False}, headers=ADMIN
    ).json()
    over_mcp = _mcp_json(client, "export_usage", {"days": 7, "format": "csv"})
    assert over_mcp["filename"] == envelope["filename"] and over_mcp["files"] == envelope["files"]
    unpacked = zipfile.ZipFile(io.BytesIO(base64.b64decode(over_mcp["content"])))
    assert sorted(unpacked.namelist()) == sorted(bundle.namelist())

    refused = client.get("/usage/export", params={"format": "xlsx"}, headers=ADMIN)
    assert refused.status_code == 422 and "format must be one of csv, json" in refused.text


def test_the_traces_export_in_pages_over_mcp(roles, monkeypatch):
    app, client = roles
    StateSink(app.state.state).write(
        [_event(200 + i, session="conv-p", minutes_ago=5) for i in range(5)]
    )
    monkeypatch.setattr(usage_export, "MAX_TRACE_ROWS", 2)
    first = _mcp_json(client, "export_usage_traces", {"days": 1, "session": "conv-p"})
    assert first["rows"] == 2 and first["has_more"] is True and first["next_offset"] == 2
    rest = []
    offset = first["next_offset"]
    while offset is not None:
        page = _mcp_json(
            client, "export_usage_traces", {"days": 1, "session": "conv-p", "offset": offset}
        )
        rest.append(page["content"])
        offset = page["next_offset"]
    lines = (first["content"] + "".join(rest)).splitlines()
    download = client.get("/usage/sessions/conv-p/export", params={"days": 1}, headers=ADMIN)
    assert lines == download.text.splitlines(), "the same rows, in order, as the download"
    with pytest.raises(InvalidRequest):
        usage_export.export_traces(SimpleNamespace(), limit=0)


def test_exports_are_an_admins(roles):
    app, client = roles
    assert client.get("/usage/export", headers=READER).status_code == 403
    for name, arguments in (("export_usage", {}), ("export_usage_traces", {"days": 1})):
        assert _mcp_refused(_mcp(client, "reader-token", name, arguments)), name
