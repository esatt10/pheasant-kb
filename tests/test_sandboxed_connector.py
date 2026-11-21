"""Step 34.2 acceptance: reference sandboxed connector.

The sandboxed reference connector (``SandboxedConnector``, the
`StaticDirConnector` shape ported to run its per-item transform inside a
fuel/memory-capped WASM guest) must pass the identical
``ConnectorConformance`` suite as the native connector it mirrors, and its
sandboxed transform must be doing real work, not a pass-through.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("wasmtime", reason="wasmtime not installed (pip install 'pheasant-kb[wasm]')")

from pheasant.config.schema import SourceConfig, SourceType  # noqa: E402
from pheasant.persistence.state_store import StateStore  # noqa: E402
from pheasant.sandbox.connector import SandboxedConnector  # noqa: E402
from pheasant.sync.connectors import connector_for_source  # noqa: E402
from pheasant.testing import ConnectorConformance  # noqa: E402


def _make_source(root: Path) -> SourceConfig:
    source = SourceConfig(
        name="sandboxed-example", type=SourceType.repository, path=root, include=[]
    )
    source.connector.runtime = "sandboxed"
    return source


class TestSandboxedStaticDirConformance(ConnectorConformance):
    def make_connector(self, tmp_path: Path, state: StateStore) -> SandboxedConnector:
        root = tmp_path / "content"
        root.mkdir(exist_ok=True)
        (root / "sample.txt").write_text("hello from the sandboxed connector")
        return SandboxedConnector(_make_source(root), state)


def test_connector_for_source_routes_to_the_sandboxed_tier(tmp_path: Path) -> None:
    root = tmp_path / "content"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    state = StateStore()
    state.migrate()
    connector = connector_for_source(_make_source(root), state)
    assert isinstance(connector, SandboxedConnector)


def test_native_runtime_default_does_not_route_to_the_sandbox(tmp_path: Path) -> None:
    root = tmp_path / "content"
    root.mkdir()
    state = StateStore()
    state.migrate()
    source = SourceConfig(name="native-example", type=SourceType.repository, path=root)
    connector = connector_for_source(source, state)
    assert not isinstance(connector, SandboxedConnector)


def test_the_guest_actually_transforms_content_not_a_pass_through(tmp_path: Path) -> None:
    root = tmp_path / "content"
    root.mkdir()
    (root / "crlf.txt").write_bytes(b"line one\r\nline two\r\n")
    state = StateStore()
    state.migrate()
    connector = SandboxedConnector(_make_source(root), state)
    items = connector.list_items()
    assert len(items) == 1
    payload = connector.read_item(items[0])
    assert payload.content == b"line one\nline two\n"
    assert b"\r" not in payload.content
