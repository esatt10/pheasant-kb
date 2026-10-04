"""Product Framework Step 31.3 — the Google Drive connector.

The one first-party SaaS connector kept for the initial product (Notion,
Slack, Confluence and IMAP were removed). It rides the 31.1 SDK exactly as a
third-party plugin would: a fixture-backed fake at the module's single
network touchpoint, so every test is offline and deterministic. Acceptance:
listing with ACL capture, deterministic rendering, per-item incremental
skip, engine e2e with an idempotent second sync, a ConnectorConformance
pass, and the pyproject entry-point guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pheasant.config.loader import load_config
from pheasant.config.schema import PluginSourceType, SourceConfig
from pheasant.connectors.gdrive import GDriveConnector
from pheasant.persistence.paths import StatePaths
from pheasant.persistence.state_store import StateStore
from pheasant.sync import connector_registry
from pheasant.sync.connectors import ItemNotModified
from pheasant.sync.engine import SyncEngine
from pheasant.testing import ConnectorConformance


def _source(type_name: str, **overrides) -> SourceConfig:
    return SourceConfig(
        name=overrides.pop("name", f"team-{type_name}"),
        type=PluginSourceType(type_name),
        path=overrides.pop("path", Path("/unused")),
        include=[],
        **overrides,
    )


def _state(tmp_path: Path) -> StateStore:
    state = StateStore(tmp_path / "state.db")
    state.migrate()
    return state


def _engine_sync_twice(
    tmp_path: Path,
    type_name: str,
    connector_class,
    expected: int,
    *,
    endpoint: str = "https://example.test",
    source_path: str = "/unused",
    expected_second_skip: int | None = None,
) -> None:
    """Engine e2e: index `expected` artifacts, then an idempotent second sync."""
    connector_registry.reset_connector_registry()
    connector_registry.register_connector_class(type_name, connector_class)
    try:
        config_path = tmp_path / "pheasant.yaml"
        config_path.write_text(
            f"""pheasant:
  name: {type_name}-test
  state_path: {tmp_path / "state-dir"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
sources:
  - name: team-{type_name}
    type: {type_name}
    path: {source_path}
    include: []
    connector:
      api_endpoint: {endpoint}
""",
            encoding="utf-8",
        )
        cfg = load_config(config_path)
        paths = StatePaths.from_config(cfg)
        paths.ensure()
        state = StateStore(paths.sqlite)
        state.migrate()
        engine = SyncEngine(cfg, paths, state)
        try:
            first = engine.sync_source(f"team-{type_name}", "incremental")
            assert first.indexed_artifacts == expected
            second = engine.sync_source(f"team-{type_name}", "incremental")
            assert second.indexed_artifacts == 0
            skip = expected if expected_second_skip is None else expected_second_skip
            assert second.skipped_artifacts == skip
        finally:
            engine.close()
    finally:
        connector_registry.reset_connector_registry()


def test_pyproject_declares_only_the_kept_first_party_connectors() -> None:
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as fh:
        eps = tomllib.load(fh)["project"]["entry-points"]["pheasant.connectors"]
    assert eps == {"gdrive": "pheasant.connectors.gdrive:GDriveConnector"}


# ---------------------------------------------------------------------------
# Google Drive (31.3)
# ---------------------------------------------------------------------------

GDRIVE_FILES = {
    "files": [
        {
            "id": "doc-alpha",
            "name": "Team Charter",
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-07-10T09:00:00Z",
            "shared": True,
            "owners": [{"emailAddress": "ada@example.com"}],
            "webViewLink": "https://docs.google.com/doc-alpha",
        },
        {
            "id": "txt-beta",
            "name": "notes.txt",
            "mimeType": "text/plain",
            "modifiedTime": "2026-07-11T10:00:00Z",
            "md5Checksum": "cafe01",
            "owners": [{"emailAddress": "curie@example.com"}],
        },
        {"id": "img-gamma", "name": "logo.png", "mimeType": "image/png"},
    ]
}
GDRIVE_BODIES = {
    "doc-alpha": b"Charter: we ship small and often.",
    "txt-beta": b"remember the retro on friday",
}


@pytest.fixture()
def fake_gdrive(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake(url: str, token: str, timeout: int, *, raw: bool = False):
        assert token == "gdrive-token"
        calls.append(url)
        if "/files?" in url:
            return json.loads(json.dumps(GDRIVE_FILES))
        for file_id, body in GDRIVE_BODIES.items():
            if f"/files/{file_id}" in url:
                return body
        raise AssertionError(f"unexpected Drive URL: {url}")

    monkeypatch.setattr("pheasant.connectors.gdrive._gdrive_request", fake)
    monkeypatch.setenv("GDRIVE_TOKEN", "gdrive-token")
    return calls


def test_gdrive_lists_text_files_with_acl_and_renders(tmp_path: Path, fake_gdrive) -> None:
    connector = GDriveConnector(_source("gdrive"), _state(tmp_path))
    items = connector.list_items()
    assert [i.metadata["file_id"] for i in items] == ["doc-alpha", "txt-beta"]  # png filtered
    assert items[0].metadata["acl"] == {"owners": ["ada@example.com"], "shared": True}
    payload = connector.read_item(items[0])
    assert payload.content == b"# Team Charter\n\nCharter: we ship small and often."
    assert connector.read_item(items[0]).content == payload.content  # deterministic

    cursor, watermark = connector.checkpoint_from_items(items)
    connector.set_checkpoint(cursor, watermark, "healthy")
    connector.begin_sync("incremental")
    with pytest.raises(ItemNotModified):
        connector.read_item(items[0])


def test_gdrive_engine_e2e(tmp_path: Path, fake_gdrive) -> None:
    _engine_sync_twice(tmp_path, "gdrive", GDriveConnector, expected=2)


class TestGDriveConformance(ConnectorConformance):
    @pytest.fixture(autouse=True)
    def _wire(self, fake_gdrive) -> None:
        pass

    def make_connector(self, tmp_path: Path, state: StateStore) -> GDriveConnector:
        return GDriveConnector(_source("gdrive"), state)
