"""The MCP transport must admit receipted batches beyond the SDK's 4 MiB default."""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.mcp_server.server import _streamable_http_options, run_mcp_server


def test_mcp_body_limit_is_configured_for_both_http_entry_points(monkeypatch) -> None:
    assert PheasantConfig().server.mcp.max_request_body_size_mb == 64
    config = PheasantConfig.model_validate({"server": {"mcp": {"max_request_body_size_mb": 8}}})
    expected = 8 * 1024 * 1024
    assert _streamable_http_options(config)["max_request_body_size"] == expected

    class Server:
        def run(self, **kwargs):
            calls.append(kwargs)

    calls: list[dict] = []
    monkeypatch.setattr("pheasant.mcp_server.server.create_mcp_server", lambda config: Server())
    run_mcp_server(config, "streamable-http")
    run_mcp_server(config, "sse")
    assert [call["max_request_body_size"] for call in calls] == [expected, expected]


def test_mcp_body_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_request_body_size_mb must be positive"):
        PheasantConfig.model_validate({"server": {"mcp": {"max_request_body_size_mb": 0}}})


@pytest.mark.skipif(
    importlib.util.find_spec("mcp.server.mcpserver") is None,
    reason="requires MCP SDK 2.x",
)
def test_mounted_mcp_accepts_large_receipted_batch_and_rejects_oversized_body(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    state_path = tmp_path / "state"
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "mcp-body-limit",
                "state_path": str(state_path),
                "exports_path": str(tmp_path / "exports"),
                "workspace_root": str(tmp_path),
            },
            "server": {"mcp": {"max_request_body_size_mb": 8}},
            "sync": {"watcher": {"enabled": False}, "scheduler": {"enabled": False}},
        }
    )
    app = create_app(config)
    encoded = base64.b64encode(b"%PDF-1.7\n" + b"x" * (256 * 1024)).decode("ascii")
    documents = [{"relative_path": f"part-{index:02d}.pdf", "text": encoded} for index in range(25)]

    def request(items: list[dict]) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "submit_documents",
                "arguments": {
                    "knowledge_base": config.knowledge_base_id,
                    "source_name": "mcp-large",
                    "content_encoding": "base64",
                    "documents": items,
                },
            },
        }

    headers = {"accept": "application/json, text/event-stream"}
    try:
        with TestClient(app, base_url="http://localhost:8765") as client:
            handshake = client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "pheasant-tests", "version": "1"},
                    },
                },
                headers=headers,
            )
            assert handshake.status_code == 200, handshake.text[:300]
            accepted = client.post("/mcp/", json=request(documents[:20]), headers=headers)
            assert len(accepted.request.content) > 4 * 1024 * 1024
            assert accepted.status_code == 200, accepted.text[:300]
            result = accepted.json()["result"]
            assert not result.get("isError", False)
            receipt_batch = result.get("structuredContent") or json.loads(
                result["content"][0]["text"]
            )
            assert len(receipt_batch["accepted"]) == 20
            assert receipt_batch["rejected"] == []
            directory = state_path / "uploads" / "mcp-large"
            assert len(list(directory.glob("*.pdf"))) == 20

            oversized = client.post("/mcp/", json=request(documents), headers=headers)
            assert len(oversized.request.content) > 8 * 1024 * 1024
            assert oversized.status_code == 413
            assert len(list(directory.glob("*.pdf"))) == 20
    finally:
        app.state.engine.close()
