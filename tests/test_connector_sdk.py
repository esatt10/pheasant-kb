"""Product Framework Step 31.1 — connector SDK (registry + plugin types).

Acceptance (Flock docs/PRODUCT_FRAMEWORK.md §3):

- registry resolution via programmatic registration AND entry points;
- an unknown ``sources[].type`` string survives config load as a
  ``PluginSourceType`` and resolves at dispatch time;
- the engine syncs a plugin-connector source end to end — artifacts
  indexed, second sync zero-work (the idempotency spine, extended);
- an unresolvable type fails with an error naming installed plugins;
- the zero-plugin built-in path is unchanged.
"""

from __future__ import annotations

import sys
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from pheasant.config.loader import load_config
from pheasant.config.schema import PluginSourceType, SourceType
from pheasant.persistence.paths import StatePaths
from pheasant.persistence.state_store import StateStore
from pheasant.sync import connector_registry
from pheasant.sync.connectors import ConnectorUnavailable, connector_for_source
from pheasant.sync.engine import SyncEngine

EXAMPLE_PKG = Path(__file__).resolve().parent / "fixtures" / "pheasant-connector-example"


@pytest.fixture(autouse=True)
def clean_registry():
    connector_registry.reset_connector_registry()
    yield
    connector_registry.reset_connector_registry()


@pytest.fixture()
def example_connector_class():
    sys.path.insert(0, str(EXAMPLE_PKG))
    try:
        from pheasant_connector_example import StaticDirConnector

        yield StaticDirConnector
    finally:
        sys.path.remove(str(EXAMPLE_PKG))


def _write_config(tmp_path: Path, source_type: str, content_dir: Path) -> Path:
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: sdk-test
  state_path: {tmp_path / "state"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
sources:
  - name: plugin-src
    type: {source_type}
    path: {content_dir}
""",
        encoding="utf-8",
    )
    return config_path


def _engine_for(config_path: Path) -> SyncEngine:
    cfg = load_config(config_path)
    paths = StatePaths.from_config(cfg)
    paths.ensure()
    state = StateStore()
    state.migrate()
    return SyncEngine(cfg, paths, state)


def _content_dir(tmp_path: Path) -> Path:
    content = tmp_path / "content"
    content.mkdir()
    (content / "alpha.txt").write_text("Alpha document about sync engines.", encoding="utf-8")
    (content / "beta.txt").write_text("Beta document about registries.", encoding="utf-8")
    return content


def test_unknown_source_type_loads_as_plugin_type(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "staticdir", _content_dir(tmp_path))
    cfg = load_config(config_path)
    source = cfg.sources[0]
    assert isinstance(source.type, PluginSourceType)
    assert source.type.value == "staticdir"
    assert source.type == "staticdir"  # plain-string behavior preserved
    # Plugin types get no workspace-root anchoring — the path is theirs.
    assert source.path == tmp_path / "content"


def test_builtin_types_still_load_as_enum(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "document_folder", _content_dir(tmp_path))
    assert load_config(config_path).sources[0].type is SourceType.document_folder


def test_programmatic_registration_resolves_at_dispatch(
    tmp_path: Path, example_connector_class
) -> None:
    connector_registry.register_connector_class("staticdir", example_connector_class)
    config_path = _write_config(tmp_path, "staticdir", _content_dir(tmp_path))
    cfg = load_config(config_path)
    state = StateStore()
    state.migrate()
    connector = connector_for_source(cfg.sources[0], state)
    assert isinstance(connector, example_connector_class)
    assert connector.connector_type == "staticdir"


def test_entry_point_registration_resolves(
    tmp_path: Path, example_connector_class, monkeypatch: pytest.MonkeyPatch
) -> None:
    ep = EntryPoint(
        name="staticdir",
        value="pheasant_connector_example:StaticDirConnector",
        group=connector_registry.ENTRY_POINT_GROUP,
    )
    monkeypatch.setattr(
        connector_registry, "entry_points", lambda group: [ep] if group == ep.group else []
    )
    assert connector_registry.get_connector_class("staticdir") is example_connector_class
    assert connector_registry.list_connector_types() == ["staticdir"]


def test_register_rejects_non_connector_classes() -> None:
    with pytest.raises(TypeError, match="SourceConnector subclass"):
        connector_registry.register_connector_class("bogus", dict)
    with pytest.raises(ValueError, match="non-empty string"):
        connector_registry.register_connector_class("", object)


def test_unresolvable_type_errors_name_installed_plugins(
    tmp_path: Path, example_connector_class
) -> None:
    connector_registry.register_connector_class("otherplugin", example_connector_class)
    config_path = _write_config(tmp_path, "staticdir", _content_dir(tmp_path))
    cfg = load_config(config_path)
    state = StateStore()
    state.migrate()
    with pytest.raises(ConnectorUnavailable) as excinfo:
        connector_for_source(cfg.sources[0], state)
    assert "staticdir" in str(excinfo.value)
    assert "otherplugin" in str(excinfo.value)


def test_engine_syncs_plugin_source_end_to_end_and_idempotently(
    tmp_path: Path, example_connector_class
) -> None:
    connector_registry.register_connector_class("staticdir", example_connector_class)
    config_path = _write_config(tmp_path, "staticdir", _content_dir(tmp_path))
    engine = _engine_for(config_path)
    try:
        first = engine.sync_source("plugin-src", "incremental")
        assert first.indexed_artifacts == 2
        second = engine.sync_source("plugin-src", "incremental")
        assert second.indexed_artifacts == 0
        assert second.skipped_artifacts == 2
    finally:
        engine.close()
