"""pheasant's MCP App, as a real 2026-era client sees it.

The MCP Apps extension links a tool to a ``ui://`` resource through the
tool's ``_meta.ui.resourceUri``; the resource is HTML served as
``text/html;profile=mcp-app``. Driven through the SDK's own client against the
mounted ``/mcp`` endpoint, because every one of those facts is a field the SDK
serializes — a facade-level test would pass with a key the wire never carries.

What must hold:

* the three assistant tools declare the view, in the current *and* the
  deprecated flat spelling (hosts built against the draft read the latter);
* the view is listed and readable with the mcp-app MIME type, and is
  self-contained — no script, style or image from anywhere but itself;
* ``get_image`` returns MCP image content that decodes to the indexed bytes,
  because the view (and any vision-capable agent) reads it that way;
* the view never turns a result into markup: no ``innerHTML``, no
  ``insertAdjacentHTML``, no ``document.write`` — the answer and the diagram
  labels are model output.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from pheasant.mcp_server.assistant_tools import APP_MIME, APP_URI, app_html

FIXTURE_IMAGE = Path(__file__).parent / "fixtures" / "sample_workspace" / "images" / "diagram.png"


def test_the_view_is_self_contained_and_builds_no_markup_from_data() -> None:
    html = app_html()

    assert html.lstrip().lower().startswith("<!doctype html>")
    # Nothing loaded from anywhere: the view runs in a sandbox with no network.
    assert not re.search(r"<(script|link|img)[^>]+(src|href)\s*=\s*[\"']?(https?:)?//", html)
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in html, f"the view must not use {sink}"
    # It speaks the protocol revision it was written against.
    assert '"2026-01-26"' in html and "ui/initialize" in html
    assert "ui/notifications/tool-result" in html


@pytest.mark.asyncio
async def test_a_real_client_sees_the_app_the_tools_and_the_image(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    from pheasant.api.app import create_app
    from pheasant.config.loader import load_config

    workspace = tmp_path / "workspace"
    (workspace / "images").mkdir(parents=True)
    (workspace / "images" / "diagram.png").write_bytes(FIXTURE_IMAGE.read_bytes())
    (workspace / "guide.md").write_text(
        "# Guide\n\n![Topology](images/diagram.png)\n", encoding="utf-8"
    )
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: mcp-apps
  state_path: {tmp_path / "state"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {workspace}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
sources:
  - name: docs
    type: document_folder
    path: {workspace}
    include: ["**/*.md", "**/*.png"]
""",
        encoding="utf-8",
    )
    # Indexed first, as a deployment would be: the app (and the MCP facade it
    # mounts) loads the graph it serves at startup.
    from pheasant.sync.engine import SyncEngine

    indexer = SyncEngine(load_config(config_path))
    indexer.sync_source("docs", "full")
    indexer.close()
    app = create_app(load_config(config_path), config_path=str(config_path))
    base = "http://localhost:8765"
    try:
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url=base
            ) as http_client:
                transport = streamable_http_client(f"{base}/mcp/", http_client=http_client)
                async with Client(transport) as client:
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    for name in ("ask_knowledge_base", "create_visual", "get_image"):
                        meta = tools[name].meta or {}
                        assert meta.get("ui", {}).get("resourceUri") == APP_URI, name
                        assert meta.get("ui/resourceUri") == APP_URI, name

                    resources = {str(r.uri): r for r in (await client.list_resources()).resources}
                    assert resources[APP_URI].mime_type == APP_MIME
                    read = await client.read_resource(APP_URI)
                    assert read.contents[0].mime_type == APP_MIME
                    assert read.contents[0].text == app_html()

                    result = await client.call_tool(
                        "get_image",
                        {
                            "knowledge_base": "mcp-apps",
                            "node_id": "file:docs:images/diagram.png:branch=none",
                        },
                    )
                    assert not result.is_error, result
                    image = next(block for block in result.content if block.type == "image")
                    assert image.mime_type == "image/png"
                    assert base64.b64decode(image.data) == FIXTURE_IMAGE.read_bytes()
                    assert result.structured_content["relative_path"] == "images/diagram.png"

                    answer = await client.call_tool(
                        "ask_knowledge_base",
                        {
                            "knowledge_base": "mcp-apps",
                            "question": "show me the topology image",
                            "workflow": "simple",
                        },
                    )
                    assert not answer.is_error, answer
                    # A dict-returning tool arrives as JSON text, which is the
                    # fallback the view reads when a host sends no structured
                    # content — so this is the path the view depends on.
                    payload = json.loads(
                        next(block.text for block in answer.content if block.type == "text")
                    )
                    assert payload["route"]["visual"] == "image"
                    assert payload["figures"][0]["relative_path"] == "images/diagram.png"
    finally:
        app.state.engine.close()
