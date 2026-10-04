"""Retired source types load from user data and say what happened.

Configs and the runtime registry in ``/state`` are user data (rule 2): a type
that was removed must not fail a load, and must not fail silently either.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pheasant.config.retired import REMOVED_SOURCE_TYPES
from pheasant.config.schema import PheasantConfig, SourceType
from pheasant.persistence.state_store import StateStore
from pheasant.sync.connectors import ConnectorUnavailable, connector_for_source


def _load(source: dict) -> object:
    return PheasantConfig.model_validate({"sources": [source]}).sources[0]


def test_obsidian_vault_loads_as_markdown_folder(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        source = _load({"name": "vault", "type": "obsidian_vault", "path": str(tmp_path)})
    assert source.type is SourceType.markdown_folder
    assert "obsidian_vault" not in {member.value for member in SourceType}


@pytest.mark.parametrize("type_name", sorted(REMOVED_SOURCE_TYPES))
def test_removed_type_loads_and_is_refused_at_sync_with_the_reason(
    tmp_path: Path, type_name: str
) -> None:
    source = _load({"name": f"old-{type_name}", "type": type_name})
    state = StateStore(tmp_path / "state.db")
    state.migrate()
    with pytest.raises(ConnectorUnavailable, match="removed type"):
        connector_for_source(source, state)


def test_removed_s3_settings_are_dropped_not_fatal() -> None:
    source = _load({"name": "b", "type": "s3", "connector": {"s3_bucket": "x", "s3_prefix": "y"}})
    assert not hasattr(source.connector, "s3_bucket")
