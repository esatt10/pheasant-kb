"""One implementation, two transports: the harness over HTTP and MCP agree."""
# ruff: noqa: E501, F811

from __future__ import annotations

import asyncio
import json

import pytest

from tests.harness_support import build_region, write_tree

pytest.importorskip("pheasant_harness")
pytest.importorskip("mcp")

from pheasant_harness import coordinator as coordinator_mod  # noqa: E402
from pheasant_harness.permissions import HTTP, MCP  # noqa: E402

from pheasant.api.app import create_app  # noqa: E402


def _request(reg_id: str, question: str = "Who approves large refunds?") -> dict:
    return {
        "workload": "answer",
        "question": question,
        "inputs": [reg_id],
        "identifiers": ["manager approval"],
        "discovery": "off",
    }


def test_http_and_mcp_drive_the_same_runs_with_the_same_refusals(tmp_path, monkeypatch):
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    region = build_region(tmp_path, monkeypatch)
    docs = write_tree(
        region.workspace / "docs",
        {"refunds.md": "# Refunds\n\nRefunds above 500 USD need manager approval.\n"},
    )
    kb = region.config.knowledge_base_id
    app = create_app(region.config)
    paths = {getattr(route, "path", None) for route in app.routes}
    assert {path for _method, path in HTTP} <= paths

    async def scenario():
        base = "http://localhost:8765"
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url=base
            ) as http:
                transport = streamable_http_client(f"{base}/mcp/", http_client=http)
                async with Client(transport) as client:
                    tools = {tool.name for tool in (await client.list_tools()).tools}
                    assert set(MCP) <= tools

                    async def call(name, **arguments):
                        result = await client.call_tool(name, {"knowledge_base": kb, **arguments})
                        if result.structured_content is None and not result.is_error:
                            result.structured_content = json.loads(result.content[0].text)
                        return result

                    registered = (
                        await http.post("/harness/registrations", json={"uri": f"file://{docs}"})
                    ).json()
                    reply = await call("harness_register", request={"uri": f"file://{docs}"})
                    assert not reply.is_error, reply.content
                    via_mcp = reply.structured_content
                    assert via_mcp["id"] == registered["id"] and via_mcp["created"] is False
                    reg = registered["id"]

                    grep_body = {"registration_id": reg, "pattern": "approval"}
                    grep_http = (await http.post("/harness/grep", json=grep_body)).json()
                    grep_mcp = (await call("harness_grep", request=grep_body)).structured_content
                    assert grep_http["coverage"]["statement"] == grep_mcp["coverage"]["statement"]
                    assert grep_http["matches"] == grep_mcp["matches"]

                    started = await http.post(
                        "/harness/runs", json={"request": _request(reg), "client_key": "s1"}
                    )
                    assert started.status_code == 201
                    run_id = started.json()["run_id"]
                    same = (
                        await call("harness_start_run", request=_request(reg), client_key="s1")
                    ).structured_content
                    assert same["run_id"] == run_id and same["reused"]

                    for _ in range(10):
                        stepped = (await http.post(f"/harness/runs/{run_id}/step")).json()
                        if stepped["state"] == "succeeded":
                            break
                    assert stepped["state"] == "succeeded", stepped
                    status_mcp = (
                        await call("harness_run_status", run_id=run_id)
                    ).structured_content
                    status_http = (await http.get(f"/harness/runs/{run_id}")).json()
                    assert status_mcp["state"] == status_http["state"] == "succeeded"
                    assert status_mcp["result"] == status_http["result"]

                    page = (
                        await http.get(f"/harness/runs/{run_id}/events", params={"after_seq": 3})
                    ).json()
                    assert [e["seq"] for e in page["events"]][0] == 4
                    stream = await http.get(
                        f"/harness/runs/{run_id}/events/stream",
                        params={"after_seq": page["next_after_seq"] - 2},
                    )
                    assert "event: run.succeeded" in stream.text and "event: end" in stream.text

                    digest = status_http["result"]["candidates"]["primary"]
                    doc = (await http.get(f"/harness/runs/{run_id}/artifacts/{digest}")).json()
                    doc_mcp = (
                        await call("harness_get_artifact", run_id=run_id, digest=digest)
                    ).structured_content
                    assert doc == doc_mcp and "manager approval" in doc["content"]
                    raw = await http.get(f"/harness/runs/{run_id}/artifacts/{digest}/raw")
                    assert raw.headers["content-security-policy"].startswith("sandbox")

                    # Refusals: same words, same code, on both surfaces.
                    missing = await http.get("/harness/runs/run_nope")
                    refused = await call("harness_run_status", run_id="run_nope")
                    assert (
                        missing.status_code == 404 and missing.json()["code"] == "HARNESS_NOT_FOUND"
                    )
                    assert refused.is_error and missing.json()["detail"] in refused.content[0].text
                    conflict = await http.post(
                        "/harness/runs",
                        json={"request": _request(reg, "Other?"), "client_key": "s1"},
                    )
                    assert (
                        conflict.status_code == 409
                        and conflict.json()["code"] == "HARNESS_RUN_CONFLICT"
                    )

                    monkeypatch.setattr(coordinator_mod, "MAX_ACTIVE_PER_TENANT", 0)
                    shed = await http.post(
                        "/harness/runs",
                        json={"request": _request(reg, "Shed?"), "client_key": "s2"},
                    )
                    assert shed.status_code == 429 and int(shed.headers["retry-after"]) > 0
                    assert shed.json()["retryable"] is True

                    contract = (await http.get("/harness/contract")).json()
                    assert (
                        contract["workloads"]["code_change"]
                        and "blind_sink" in contract["delivery_adapters"]
                    )
                    assert contract["delivery_adapters"]["blind_sink"]["idempotent"] is False
                    discovered = json.loads(json.dumps((await http.get("/extensions")).json()))
                    assert {"id": "harness", "ui_routes": ["/harness"]} in discovered["extensions"]

    asyncio.run(scenario())
