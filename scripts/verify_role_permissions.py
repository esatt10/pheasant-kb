"""Live HTTP/MCP role smoke checks and repeatable text-search latency samples.

Credentials come from environment variables. Reports never contain credentials.
Run baseline and candidate against the same corpus and compare the distributions.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import uuid
from pathlib import Path

import httpx


def verify(
    base_url: str,
    tokens: dict[str, str],
    *,
    writes: bool = False,
    samples: int = 30,
    require_roles: bool = True,
):
    report: dict = {"base_url": base_url, "roles": {}, "search_ms": {}}
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=120, follow_redirects=True) as client:
        for path in ("/health", "/ready"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code)
        assert client.get("/sources").status_code == 401
        for role, token in tokens.items():
            auth = {"Authorization": f"Bearer {token}"}
            response = client.get("/auth/me", headers=auth)
            assert response.status_code == 200, (role, response.status_code)
            identity = response.json()
            if require_roles:
                assert identity.get("role") == role and identity.get("permissions"), (
                    role,
                    "server does not expose the required role policy",
                )
            if "role" in identity:
                assert identity["role"] == role, (role, identity.get("role"))
            assert client.get("/sources", headers=auth).status_code == 200
            assert client.get("/knowledge-base/overview", headers=auth).status_code == 200
            if role == "Reader":
                for method, path, body in (
                    ("POST", "/sources", {}),
                    ("POST", "/sources/quick-add", {}),
                    ("POST", "/sources/upload", {}),
                    ("POST", "/ingest/submit", {}),
                    ("POST", "/artifacts", {}),
                    ("POST", "/artifacts/records", {}),
                    ("POST", "/memory", {}),
                    ("POST", "/tuning/run", {}),
                    ("POST", "/tuning/bundles/apply", {}),
                    ("POST", "/documents/archive", {}),
                    ("POST", "/assistant/chat", {"question": "test", "save": True}),
                ):
                    denied = client.request(method, path, headers=auth, json=body)
                    assert denied.status_code == 403, (role, path, denied.status_code)
            if role == "Contributor":
                denied = client.post("/connections", headers=auth, json={})
                assert denied.status_code == 403
            # MCP uses the exact same bearer credential. Initialize and invoke
            # a tool directly, including one absent from a client's cached list.
            mcp_headers = {**auth, "Accept": "application/json, text/event-stream"}
            initialize = client.post(
                "/mcp/",
                headers=mcp_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "pheasant-role-verifier", "version": "1.0"},
                    },
                },
            )
            assert initialize.status_code == 200, (role, "MCP initialize", initialize.status_code)
            session = initialize.headers.get("mcp-session-id")
            if session:
                mcp_headers["Mcp-Session-Id"] = session
            client.post(
                "/mcp/",
                headers=mcp_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            tool = "register_source" if role == "Reader" else "list_sources"
            arguments = {"knowledge_base": "pheasant-kb"}
            if role == "Reader":
                arguments.update(
                    name="forbidden-role-check",
                    source_type="web_collection",
                    urls=["https://example.com/"],
                )
            call = client.post(
                "/mcp/",
                headers=mcp_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
            )
            assert call.status_code == 200, (role, "MCP call", call.status_code)
            if role == "Reader":
                assert "permission required: sources.manage" in call.text, call.text[:300]
            report["roles"][role] = {"http": "passed", "mcp": "passed"}
            timings = []
            for number in range(samples + 3):
                started = time.perf_counter()
                search = client.post(
                    "/search",
                    headers=auth,
                    json={"query": "knowledge base", "mode": "text", "max_results": 5},
                )
                assert search.status_code == 200, (role, "search", search.status_code)
                if number >= 3:
                    timings.append((time.perf_counter() - started) * 1000)
            ordered = sorted(timings)
            report["search_ms"][role] = {
                "samples": samples,
                "p50": round(statistics.median(timings), 3),
                "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
            }
            if writes and role in {"Admin", "Contributor"}:
                name = "rbac-verify-" + uuid.uuid4().hex[:12]
                registered = False
                artifact_id = None
                try:
                    created = client.post(
                        "/sources",
                        headers=auth,
                        json={
                            "name": name,
                            "type": "web_collection",
                            "path": "",
                            "urls": ["https://example.com/"],
                            "sync_now": False,
                        },
                    )
                    assert created.status_code < 400, (role, "register", created.status_code)
                    registered = True
                    saved = client.post(
                        "/artifacts",
                        headers=auth,
                        json={
                            "title": name,
                            "markdown": "Role verification. Safe to remove.",
                            "sync": False,
                        },
                    )
                    assert saved.status_code < 400, (role, "save", saved.status_code)
                    payload = saved.json()
                    artifact_id = payload.get("id") or payload.get("artifact", {}).get("id")
                    assert artifact_id, list(payload)
                    report["roles"][role]["writes"] = "source and artifact passed"
                finally:
                    if artifact_id:
                        removed = client.delete(f"/artifacts/{artifact_id}", headers=auth)
                        assert removed.status_code < 400, (
                            role,
                            "delete artifact",
                            removed.status_code,
                        )
                    if registered:
                        removed = client.delete(f"/sources/{name}", headers=auth)
                        assert removed.status_code < 400, (
                            role,
                            "delete source",
                            removed.status_code,
                        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--admin-token-env", default="PHEASANT_API_TOKEN")
    parser.add_argument("--reader-token-env", default="PHEASANT_READER_API_TOKEN")
    parser.add_argument("--contributor-token-env", default="PHEASANT_CONTRIBUTOR_API_TOKEN")
    parser.add_argument("--writes", action="store_true")
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    tokens = {
        role: value
        for role, env in [
            ("Admin", args.admin_token_env),
            ("Contributor", args.contributor_token_env),
            ("Reader", args.reader_token_env),
        ]
        if (value := os.environ.get(env))
    }
    if not tokens or args.samples < 1:
        parser.error("provide at least one token and a positive sample count")
    result = verify(args.base_url, tokens, writes=args.writes, samples=args.samples)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
