"""pheasant's defaults are pheasant-kb's (``pheasant.config.flagship``).

The rest of the suite runs on the standalone table (``conftest.py`` sets
``PHEASANT_DEFAULTS=standalone``), because it is offline by design. This module
is where the flagship is held:

* an empty config built on it *is* ``deploy/compose/pheasant-kb.yaml``;
* the standalone table is exactly the values written in ``schema.py``;
* a config's own choice beats the environment, and a dump reloads on the table
  it was built on;
* every config this repository ships names its table, because a partial
  config that names none would quietly pick up Postgres and NATS;
* and, against a real Postgres, pheasant-kb's defaults index and search a
  corpus with only the infrastructure swapped for in-process equivalents and
  the models stubbed.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from pheasant.config.flagship import (
    DEFAULTS_ENV,
    FLAGSHIP,
    STANDALONE,
    default_config,
    resolve_defaults,
    using_defaults,
)
from pheasant.config.loader import load_layered_config
from pheasant.config.schema import PheasantConfig

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "compose"


def _dump(config: PheasantConfig) -> dict[str, Any]:
    return config.model_dump(mode="json")


def test_an_empty_config_is_pheasant_kb() -> None:
    shipped = yaml.safe_load((COMPOSE / "pheasant-kb.yaml").read_text(encoding="utf-8"))
    with using_defaults(FLAGSHIP):
        assert _dump(default_config()) == _dump(PheasantConfig.model_validate(shipped))


def test_the_standalone_table_is_the_values_written_in_the_schema() -> None:
    with using_defaults(STANDALONE):
        built = _dump(default_config())
    written = _dump(PheasantConfig())
    written["pheasant"]["defaults"] = STANDALONE
    # The three state paths are derived from pheasant.state_path on load.
    for key in ("sqlite_path", "graph_path", "manifest_path"):
        written["storage"][key] = built["storage"][key]
    assert built == written


def test_without_a_selection_the_flagship_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DEFAULTS_ENV, raising=False)
    config = default_config()
    assert config.pheasant.defaults == FLAGSHIP
    assert config.storage.backend == "postgres"
    assert config.sync.queue.backend == "nats"
    assert config.search.embeddings.enabled
    assert [source.name for source in config.sources] == ["agent-memory", "uploads"]


def test_a_config_choice_beats_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEFAULTS_ENV, FLAGSHIP)
    config = PheasantConfig.model_validate({"pheasant": {"defaults": STANDALONE}})
    assert (config.pheasant.defaults, config.storage.backend) == (STANDALONE, "sqlite")
    monkeypatch.setenv(DEFAULTS_ENV, STANDALONE)
    config = PheasantConfig.model_validate({"pheasant": {"defaults": FLAGSHIP}})
    assert (config.pheasant.defaults, config.storage.backend) == (FLAGSHIP, "postgres")


def test_a_set_value_beats_the_table() -> None:
    with using_defaults(FLAGSHIP):
        config = PheasantConfig.model_validate(
            {"storage": {"backend": "sqlite"}, "sources": [], "assistant": {"model": None}}
        )
    assert config.storage.backend == "sqlite"
    assert config.sources == []
    assert config.assistant.model is None
    assert config.sync.queue.backend == "nats"


def test_an_unknown_table_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEFAULTS_ENV, "tiny")
    with pytest.raises(ValueError, match="flagship, standalone"):
        resolve_defaults()


def test_a_dump_reloads_on_its_own_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEFAULTS_ENV, STANDALONE)
    dumped = _dump(PheasantConfig.model_validate({"pheasant": {"name": "laptop"}}))
    monkeypatch.delenv(DEFAULTS_ENV)
    reloaded = PheasantConfig.model_validate(dumped)
    assert reloaded.pheasant.defaults == STANDALONE
    assert _dump(reloaded) == dumped


def test_the_layered_loader_starts_from_the_files_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It begins from a dump of the defaults; a standalone file laid over a
    flagship dump would inherit every flagship value it does not set."""

    monkeypatch.delenv(DEFAULTS_ENV, raising=False)
    path = tmp_path / "pheasant.yaml"
    path.write_text("pheasant:\n  defaults: standalone\n", encoding="utf-8")
    config = load_layered_config(path)
    assert (config.storage.backend, config.sync.queue.backend) == ("sqlite", "local")
    assert not config.search.embeddings.enabled


def test_the_wizard_records_the_table_it_answered_on(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from pheasant.setup_wizard import Wizard

    for table, backend in ((FLAGSHIP, "postgres"), (STANDALONE, "sqlite")):
        wizard = Wizard(accept_defaults=True, preset={"pheasant.defaults": table})
        wizard.run()
        capsys.readouterr()
        data = wizard.config_dict()
        assert data["pheasant"]["defaults"] == table
        # Read back with the environment pointing the other way.
        monkeypatch.setenv(DEFAULTS_ENV, STANDALONE if table == FLAGSHIP else FLAGSHIP)
        assert PheasantConfig.model_validate(data).storage.backend == backend


# -- every shipped config names its table -------------------------------------


def _shipped_configs() -> dict[str, dict[str, Any]]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "*.yaml", "*.yml", "*.json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    found: dict[str, dict[str, Any]] = {}

    def consider(label: str, data: Any) -> None:
        if isinstance(data, dict) and isinstance(data.get("pheasant"), dict):
            if "name" in data["pheasant"]:
                found[label] = data

    for name in tracked:
        if not name or name.startswith(("tests/", "ui/", "contracts/", ".github/")):
            continue
        if "/templates/" in name:  # Helm templates; their config is values.yaml
            continue
        text = (ROOT / name).read_text(encoding="utf-8")
        if name.endswith(".json"):
            data = json.loads(text)
            if isinstance(data, dict) and "pheasant.name" in data:
                found[name] = {
                    "pheasant": {
                        "name": data["pheasant.name"],
                        **(
                            {"defaults": data["pheasant.defaults"]}
                            if "pheasant.defaults" in data
                            else {}
                        ),
                    }
                }
            continue
        try:
            documents = list(yaml.safe_load_all(text))
        except yaml.YAMLError:  # mkdocs.yml's python tags; not a pheasant config
            continue
        for document in documents:
            consider(name, document)
            if not isinstance(document, dict):
                continue
            for key, value in (document.get("data") or {}).items():
                if isinstance(value, str) and key.endswith((".yaml", ".yml")):
                    consider(f"{name}:{key}", yaml.safe_load(value))
            embedded = (document.get("config") or {}).get("pheasantYaml")
            if isinstance(embedded, str):
                consider(f"{name}:config.pheasantYaml", yaml.safe_load(embedded))
    return found


def test_every_shipped_config_names_its_table() -> None:
    configs = _shipped_configs()
    assert "deploy/compose/pheasant-kb.yaml" in configs
    assert "deploy/helm/values.yaml:config.pheasantYaml" in configs
    unnamed = sorted(label for label, data in configs.items() if "defaults" not in data["pheasant"])
    assert not unnamed, (
        "these configs name no defaults table, so they would be built on pheasant-kb's "
        f"(Postgres, NATS, OpenAI): {unnamed}. Add pheasant.defaults: standalone or flagship."
    )
    flagship = {
        label for label, data in configs.items() if data["pheasant"]["defaults"] == FLAGSHIP
    }
    assert flagship == {
        "deploy/compose/pheasant-kb.yaml",
        "deploy/compose/answers/pheasant-kb.json",
        # pheasant-kb with its model calls on Azure AI Foundry: a partial
        # overlay whose whole point is the flagship table underneath it.
        "deploy/compose/foundry/pheasant-kb.foundry.yaml",
    }


# -- pheasant-kb's defaults, run, on stub models ------------------------------

DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()


@pytest.mark.skipif(not DSN, reason="set PHEASANT_TEST_POSTGRES_DSN to run the flagship")
def test_the_flagship_indexes_and_searches_on_stub_models(tmp_path: Path) -> None:
    """Everything is pheasant-kb's default except what cannot exist in a test
    process: the database is the test's, the queue, workers, graph service and
    landing service are in-process, and every model is the offline stub."""

    pytest.importorskip("lancedb")
    from pheasant.sync.engine import SyncEngine

    workspace = tmp_path / "corpus"
    workspace.mkdir()
    (workspace / "atlas.md").write_text(
        "# Atlas\n\nAtlas is the billing service, owned by the payments team.\n",
        encoding="utf-8",
    )
    (workspace / "kestrel.md").write_text(
        "# Kestrel\n\nKestrel runs the nightly export of the ledger.\n", encoding="utf-8"
    )
    lexical = (
        "pg_textsearch" if os.environ.get("PHEASANT_REQUIRE_PG_TEXTSEARCH") == "1" else "tsvector"
    )
    with using_defaults(FLAGSHIP):
        config = PheasantConfig.model_validate(
            {
                "pheasant": {
                    "name": "flagship-stub",
                    "state_path": str(tmp_path / "state"),
                    "workspace_root": str(tmp_path),
                    "exports_path": str(tmp_path / "exports"),
                },
                "storage": {"dsn_env": "PHEASANT_TEST_POSTGRES_DSN", "lexical_index": lexical},
                "search": {
                    "embeddings": {"provider": "stub"},
                    "vector_store": {"path": str(tmp_path / "state" / "vectors")},
                },
                "ingestion": {
                    "captioner": {"provider": "stub"},
                    "transcriber": {"provider": "stub"},
                    "landing_service_url": None,
                },
                "sync": {
                    "queue": {"enabled": False},
                    "watcher": {"enabled": False},
                    "scheduler": {"enabled": False},
                    "concurrency": {
                        "file_executor": "thread",
                        "remote_worker_enabled": False,
                        "remote_worker_urls": [],
                    },
                },
                "graph": {"query_service_url": None},
                "observability": {"interactions": {"queue": {"enabled": False}}},
                "security": {"allow_workspace_roots": [str(tmp_path)]},
                "sources": [{"name": "docs", "type": "markdown_folder", "path": str(workspace)}],
            }
        )
    # Still the flagship wherever the test did not have to step in.
    assert config.storage.backend == "postgres"
    assert config.search.vector_store.provider == "lancedb"
    assert config.search.vector_store.resident
    assert config.sync.source_processing.chunk_strategy == "auto"
    assert config.security.acl_enforced

    engine = SyncEngine(config)
    try:
        result = engine.sync_source("docs", "full")
        assert result.indexed_artifacts == 2
        hits = engine.search_context("who owns billing", max_results=3)["results"]
        assert hits and Path(hits[0]["path"]).name == "atlas.md"
    finally:
        engine.close()


# -- one-process generators write standalone ----------------------------------
#
# The flagship is the default, and it cannot run in one process. So the two
# things that build a one-process deployment (`pheasant up`, and the image
# writing its own config when nothing is mounted) build on standalone and
# record it, unless PHEASANT_DEFAULTS asks otherwise.


def test_pheasant_up_writes_a_standalone_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pheasant.quickstart import render_up_config

    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("# A\n", encoding="utf-8")
    monkeypatch.delenv(DEFAULTS_ENV, raising=False)
    rendered = yaml.safe_load(render_up_config(tmp_path / "notes", tmp_path / "pheasant.yaml"))
    assert rendered["pheasant"]["defaults"] == STANDALONE
    config = PheasantConfig.model_validate(rendered)
    assert (config.storage.backend, config.sync.queue.enabled) == ("sqlite", False)

    monkeypatch.setenv(DEFAULTS_ENV, FLAGSHIP)
    rendered = yaml.safe_load(render_up_config(tmp_path / "notes", tmp_path / "pheasant.yaml"))
    assert rendered["pheasant"]["defaults"] == FLAGSHIP


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs a POSIX sh")
def test_the_image_writes_a_standalone_config_when_none_is_mounted(tmp_path: Path) -> None:
    import re

    entrypoint = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
    function = re.search(r"^ensure_config\(\) \{\n.*?^\}\n", entrypoint, re.M | re.S)
    assert function, "docker-entrypoint.sh no longer defines ensure_config"
    fake = tmp_path / "bin"
    fake.mkdir()
    # Records the table `setup` was asked to build on; refuses `import wasmtime`.
    (fake / "python").write_text(
        '#!/bin/sh\ncase "$*" in *setup*) printf %s "${PHEASANT_DEFAULTS-}" > "$SEEN";'
        ' touch "$CONFIG_PATH";; *) exit 1;; esac\n',
        encoding="utf-8",
    )
    (fake / "python").chmod(0o755)
    seen = tmp_path / "seen"
    for given, expected in (("", STANDALONE), (FLAGSHIP, FLAGSHIP)):
        config = tmp_path / f"config-{expected}" / "pheasant.yaml"
        env = {
            "PATH": f"{fake}:/usr/bin:/bin",
            "CONFIG_PATH": str(config),
            "SEEN": str(seen),
        }
        if given:
            env["PHEASANT_DEFAULTS"] = given
        subprocess.run(
            ["sh", "-c", "set -eu\n" + function.group(0) + "ensure_config\n"],
            env=env,
            check=True,
            capture_output=True,
        )
        assert seen.read_text() == expected


def test_the_configuration_reference_lists_the_current_defaults() -> None:
    from pheasant.config.flagship import defaults_table

    text = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    start = text.index("<!-- defaults-table:start")
    start = text.index("\n", start) + 1
    end = text.index("<!-- defaults-table:end -->")
    assert text[start:end] == defaults_table(), (
        "docs/configuration.md's defaults table is stale; regenerate it from "
        "pheasant.config.flagship.defaults_table()"
    )


def test_pheasant_init_writes_a_standalone_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from pheasant.config.loader import render_init_config

    monkeypatch.delenv(DEFAULTS_ENV, raising=False)
    rendered = yaml.safe_load(render_init_config("quickstart"))
    assert rendered["pheasant"]["defaults"] == STANDALONE
    assert rendered["storage"]["backend"] == "sqlite"


def test_a_default_dump_does_not_pin_the_derived_state_paths() -> None:
    """`pheasant up` dumps the defaults, moves `state_path`, and builds again;
    a dumped `/state/pheasant.db` would keep every write in the old place."""

    from pheasant.config.flagship import default_config_data

    for table in (FLAGSHIP, STANDALONE):
        data = default_config_data(table)
        assert all(
            data["storage"][key] is None for key in ("sqlite_path", "graph_path", "manifest_path")
        )
        data["pheasant"]["state_path"] = "/elsewhere"
        assert str(PheasantConfig.model_validate(data).storage.sqlite_path) == (
            "/elsewhere/pheasant.db"
        )
