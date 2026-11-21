"""Every config pheasant reads or writes must survive a real YAML round trip.

The repository used to carry a hand-rolled ``yaml.py`` at its root — a lenient
YAML subset for "dependency-light" installs. PyYAML is a declared dependency
and the shipped image has always used it, but the shim sat earlier on
``sys.path`` for anything run from a checkout, so **the test suite validated
against a different parser than the product ships with**. It concealed at least
four bugs before it was deleted. These tests pin the round trip on the parser
that actually runs in production, so that gap cannot reopen.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from pheasant.config.loader import config_hash, dump_config_yaml
from pheasant.config.schema import PheasantConfig

REPO = Path(__file__).resolve().parents[1]


def test_the_shipped_example_config_parses() -> None:
    """`pheasant.example.yaml` is baked into the image and parsed at startup."""

    data = yaml.safe_load((REPO / "pheasant.example.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data["pheasant"]["name"]
    # A misindented key under a scalar sibling is the mistake that once reached
    # the container smoke test; assert the parser we use here would reject it.
    try:
        yaml.safe_load("storage:\n  max_state_size_gb: 10\n    keep_event_days: 30\n")
    except yaml.YAMLError:
        pass
    else:  # pragma: no cover - would mean the guard cannot bite
        raise AssertionError("the parser accepted a misindented mapping")


def test_the_dumper_round_trips_and_indents_its_sequences() -> None:
    payload = {"a": {"b": ["x:y", "z"], "c": 1}, "d": True}
    text = dump_config_yaml(payload)

    assert yaml.safe_load(text) == payload
    # Indented under the key, not at the parent's indent.
    assert "  b:\n    - x:y\n" in text, text


def test_a_url_in_a_list_stays_a_string(tmp_path: Path) -> None:
    """`cors_origins` decides which browser origins may drive the API.

    A parser that split `http://localhost:5173` on its colon turned every
    entry into a mapping — wrong data, no error, on a security setting.
    """

    payload = {"server": {"api": {"cors_origins": ["http://localhost:5173"]}}}
    assert yaml.safe_load(dump_config_yaml(payload)) == payload


def test_a_connector_plugin_source_can_be_hashed_and_written(tmp_path: Path) -> None:
    """A plugin source type is a `str` *subclass*, which PyYAML refuses.

    `config_hash` dumps the config to YAML, and PyYAML's representer dispatches
    on the exact type — so `PluginSourceType` raised `RepresenterError:
    ('cannot represent an object', ...)`. That broke every config using a
    connector plugin: all five first-party SaaS connectors and every
    third-party one. The old shim called `str()` and never noticed.
    """

    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "plugins",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [{"name": "wiki", "type": "notion", "path": str(tmp_path)}],
        }
    )

    source_type = config.sources[0].type
    assert isinstance(source_type, str) and type(source_type) is not str, (
        "this test is only meaningful while plugin types are a str subclass"
    )

    payload = config.model_dump(mode="json")
    assert type(payload["sources"][0]["type"]) is str
    assert yaml.safe_load(dump_config_yaml(payload))["sources"][0]["type"] == "notion"
    assert len(config_hash(config)) == 64
