"""The source form must offer exactly what ``pheasant.yaml`` accepts.

Connector plugins (Step 31.1) add source types by entry point, so the set of
valid ``sources[].type`` strings is a property of the *deployment*, not of
the built-in enum. Before this, YAML happily loaded ``type: notion`` while
``POST /sources`` rejected it — the low-code path could not express what the
config file could.

Acceptance:

1. ``GET /sources/types`` lists built-ins *and* installed plugins, and says
   for each whether the schema's mandatory ``path`` is real.
2. A plugin type registers through the API, with no local path invented.
3. An unknown type is still rejected, with an error naming what *is*
   available.
4. Built-in types keep their existing path policy exactly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.sync.connector_registry import (
    register_connector_class,
    reset_connector_registry,
)
from pheasant.sync.connectors import SourceConnector


class DemoConnector(SourceConnector):
    """Minimal plugin connector: enough to be registerable, nothing more."""

    def list_items(self, *args, **kwargs):  # pragma: no cover - never synced here
        return []

    def read_item(self, *args, **kwargs):  # pragma: no cover - never synced here
        raise NotImplementedError


@pytest.fixture
def client(loaded_config, workspace_copy: Path):
    loaded_config.pheasant.workspace_root = workspace_copy
    return TestClient(create_app(config=loaded_config))


@pytest.fixture
def plugin_client(loaded_config, workspace_copy: Path):
    reset_connector_registry()
    register_connector_class("demo-saas", DemoConnector)
    loaded_config.pheasant.workspace_root = workspace_copy
    yield TestClient(create_app(config=loaded_config))
    reset_connector_registry()


def test_catalog_lists_builtins_with_their_path_role(client: TestClient) -> None:
    body = client.get("/sources/types").json()

    by_id = {entry["id"]: entry for entry in body["types"]}
    assert by_id["document_folder"]["path_role"] == "required"
    assert by_id["document_folder"]["builtin"] is True
    # Web/API carry a path only because the schema demands one.
    assert by_id["web_collection"]["path_role"] == "unused"
    assert by_id["api"]["path_role"] == "unused"
    # Retired types are not offered.
    assert "s3" not in by_id and "obsidian_vault" not in by_id
    # Agent-memory sources are owned by the memory store, never hand-made.
    assert "memory" not in by_id
    assert body["placeholder_path"]


def test_catalog_includes_installed_connector_plugins(plugin_client: TestClient) -> None:
    body = plugin_client.get("/sources/types").json()

    entry = next(item for item in body["types"] if item["id"] == "demo-saas")
    assert entry["builtin"] is False
    assert entry["path_role"] == "unused", "a plugin pulls from its own service"


def test_a_plugin_type_registers_without_a_local_path(plugin_client: TestClient) -> None:
    """The whole point: no directory has to exist for a SaaS source."""
    response = plugin_client.post(
        "/sources",
        json={
            "name": "team-saas",
            "type": "demo-saas",
            "path": "",
            "include": [],
            "connector": {"api_key_env": "DEMO_TOKEN"},
        },
    )

    assert response.status_code == 200, response.text
    source = response.json()["source"]
    assert source["type"] == "demo-saas"
    assert source["connector"]["api_key_env"] == "DEMO_TOKEN"
    assert plugin_client.get("/sources").json()[-1]["name"] == "team-saas"


def test_a_plugin_source_can_be_edited_without_resurrecting_the_path(
    plugin_client: TestClient,
) -> None:
    plugin_client.post(
        "/sources", json={"name": "team-saas", "type": "demo-saas", "path": "", "include": []}
    )

    response = plugin_client.put(
        "/sources/team-saas",
        json={"description": "team knowledge", "connector": {"api_key_env": "OTHER_TOKEN"}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["source"]["description"] == "team knowledge"


def test_an_unknown_type_is_rejected_and_says_what_is_available(client: TestClient) -> None:
    response = client.post("/sources", json={"name": "nope", "type": "not-a-connector", "path": ""})

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "not-a-connector" in detail
    assert "document_folder" in detail, "the error must list the built-in types"


def test_builtin_types_keep_their_path_policy(client: TestClient) -> None:
    """The placeholder escape hatch is for plugins only."""
    response = client.post(
        "/sources", json={"name": "bad", "type": "document_folder", "path": "/unused"}
    )

    assert response.status_code == 400
