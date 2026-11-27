"""One implementation, two transports: the harness over HTTP and MCP agree."""
# ruff: noqa: E501, F811

from __future__ import annotations

import asyncio
import json

import pytest

from tests.harness_connection_support import world  # noqa: F401
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


def test_connections_policies_approvals_and_the_audit_agree_over_http_and_mcp(world):
    """The same connection operations, refusals and audit, whichever surface asks."""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    from tests.harness_connection_fakes import SECRETS

    region = world.region
    kb = region.config.knowledge_base_id
    app = create_app(region.config)
    target = "connection://gh/acme/handbook/answers/badges.md?branch=harness/answers"
    seen: list[str] = []

    async def scenario():
        base = "http://localhost:8765"
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url=base
            ) as http:
                transport = streamable_http_client(f"{base}/mcp/", http_client=http)
                async with Client(transport) as client:

                    async def call(name, **arguments):
                        result = await client.call_tool(name, {"knowledge_base": kb, **arguments})
                        seen.append(result.content[0].text if result.content else "")
                        if result.structured_content is None and not result.is_error:
                            result.structured_content = json.loads(result.content[0].text)
                        return result

                    async def get(path, **params):
                        response = await http.get(path, params=params)
                        seen.append(response.text)
                        return response

                    # No policy yet: unusable through either door, with one refusal text.
                    blocked = await http.post(
                        "/harness/registrations", json={"uri": "connection://gh/acme/handbook"}
                    )
                    blocked_mcp = await call(
                        "harness_register", request={"uri": "connection://gh/acme/handbook"}
                    )
                    assert blocked.status_code == 403
                    assert blocked.json()["code"] == "HARNESS_CONNECTION_DENIED"
                    assert blocked_mcp.is_error
                    assert blocked.json()["detail"] in blocked_mcp.content[0].text

                    policy = {
                        "actions": ["list", "read", "write"],
                        "resources": ["acme/handbook:*"],
                        "write_resources": ["acme/handbook@harness/*:answers/*"],
                    }
                    put = await http.put("/harness/connections/gh/policy", json=policy)
                    assert put.status_code == 200 and put.json()["policy"]["version"] == 1
                    again = await call(
                        "harness_set_connection_policy", connection="gh", policy=policy
                    )
                    assert again.structured_content["policy"]["version"] == 2
                    never = await http.put(
                        "/harness/connections/gh/policy",
                        json={**policy, "actions": ["list", "delete"]},
                    )
                    never_mcp = await call(
                        "harness_set_connection_policy",
                        connection="gh",
                        policy={**policy, "actions": ["list", "delete"]},
                    )
                    assert never.status_code == 422
                    assert never.json()["code"] == "HARNESS_OPERATION_UNSUPPORTED"
                    assert never.json()["detail"] in never_mcp.content[0].text

                    listed = (await get("/harness/connections")).json()
                    listed_mcp = (await call("harness_list_connections")).structured_content
                    assert listed == listed_mcp
                    gh = next(c for c in listed["connections"] if c["name"] == "gh")
                    assert gh["policy"]["actions"] == ["list", "read", "write"]
                    history = (await get("/harness/connections/gh/policy/history")).json()
                    assert [v["version"] for v in history["versions"]] == [1, 2]

                    reg = (
                        await http.post(
                            "/harness/registrations",
                            json={"uri": "connection://gh/acme/handbook"},
                        )
                    ).json()["id"]
                    request = {
                        "workload": "answer",
                        "question": "Which orders need an exception badge?",
                        "inputs": [reg],
                        "identifiers": ["exception badge"],
                        "discovery": "off",
                        "destination": {"adapter": "connection", "target": target},
                    }
                    run_id = (
                        await http.post(
                            "/harness/runs", json={"request": request, "client_key": "c1"}
                        )
                    ).json()["run_id"]
                    for _ in range(10):
                        stepped = (await http.post(f"/harness/runs/{run_id}/step")).json()
                        if stepped["state"] == "waiting":
                            break
                    assert stepped["wait"]["kind"] == "approval", stepped
                    op = stepped["wait"]["operation_id"]

                    # The requester is the local operator here: self-approval is refused
                    # on both surfaces until the policy allows it.
                    own = await http.post(
                        f"/harness/runs/{run_id}/approvals/{op}", json={"decision": "approve"}
                    )
                    own_mcp = await call(
                        "harness_decide_approval",
                        run_id=run_id,
                        operation_id=op,
                        decision="approve",
                    )
                    assert (
                        own.status_code == 403 and own.json()["detail"] in own_mcp.content[0].text
                    )
                    await call(
                        "harness_set_connection_policy",
                        connection="gh",
                        policy={**policy, "approval": {"self_approval": True}},
                    )
                    approved = await call(
                        "harness_decide_approval",
                        run_id=run_id,
                        operation_id=op,
                        decision="approve",
                    )
                    assert approved.structured_content["state"] == "delivering"
                    for _ in range(10):
                        stepped = (await http.post(f"/harness/runs/{run_id}/step")).json()
                        if stepped["state"] == "succeeded":
                            break
                    assert stepped["state"] == "succeeded", stepped
                    late = await http.post(
                        f"/harness/runs/{run_id}/approvals/{op}", json={"decision": "approve"}
                    )
                    late_mcp = await call(
                        "harness_decide_approval",
                        run_id=run_id,
                        operation_id=op,
                        decision="approve",
                    )
                    assert late.status_code == 400
                    assert late.json()["detail"] in late_mcp.content[0].text

                    audit = (await get("/harness/connections/calls", run_id=run_id)).json()
                    audit_mcp = (
                        await call("harness_connection_calls", run_id=run_id)
                    ).structured_content
                    assert audit == audit_mcp and audit["summary"]["allowed"] > 0
                    assert {"list", "read", "write"} <= {c["action"] for c in audit["calls"]}
                    detail = (await get(f"/harness/runs/{run_id}/detail")).json()
                    assert detail["connection_calls"]
                    events = (await get(f"/harness/runs/{run_id}/events")).json()
                    assert "effect.approved" in {e["type"] for e in events["events"]}
                    disabled = await http.post("/harness/connections/gh/policy/disable")
                    assert disabled.json()["policy"]["enabled"] is False
                    contract = (await get("/harness/contract")).json()
                    assert "connection" in contract["resolvers"]
                    assert "connection" in contract["delivery_adapters"]
                    assert set(contract["connections"]["never"]) >= {"delete", "share"}

    asyncio.run(scenario())
    assert len([c for c in world.net.writes() if c.method == "PUT"]) == 1
    for text in seen:
        for secret in SECRETS:
            assert secret not in text
