"""pheasant-kb, the default deployment: `compose.yaml` at the repository root.

The stack is one line to start because of three mechanisms, and each is held
here rather than by a live run alone: tokens generated into volumes by
`deploy/compose/init-secrets.sh`, read by every tier through the entrypoint's
`NAME_FILE` convention, and every size read from `.env`. The trust boundary
they must not blur is the worker's: it mounts its own token and nothing else.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from pheasant.config.schema import PheasantConfig

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "compose"
STACK = ROOT / "compose.yaml"
INIT_SECRETS = COMPOSE / "init-secrets.sh"
ENTRYPOINT = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")

TOKENS = {
    "api-token": "PHEASANT_API_TOKEN",
    "graph-token": "PHEASANT_GRAPH_SERVICE_TOKEN",
    "worker-token": "PHEASANT_INDEX_WORKER_TOKEN",
    "ingestion-token": "PHEASANT_INGESTION_SERVICE_TOKEN",
}
PHEASANT_TIERS = ("api", "graph", "indexer", "logger", "db-init")

needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


def _stack() -> dict[str, Any]:
    return yaml.safe_load(STACK.read_text(encoding="utf-8"))


def _volumes(service: dict[str, Any]) -> list[str]:
    return [str(volume) for volume in service.get("volumes", [])]


# -- the compose file ---------------------------------------------------------


def test_every_pheasant_tier_reads_all_four_tokens_from_files() -> None:
    services = _stack()["services"]
    for name in PHEASANT_TIERS:
        service = services[name]
        env = service["environment"]
        for variable in TOKENS.values():
            assert f"{variable}_FILE" in env, (name, variable)
            assert variable not in env, f"{name} carries {variable} raw, outside the volume"
        assert "pheasant-secrets:/run/pheasant/secrets:ro" in _volumes(service), name
        assert "secrets-init" in service.get("depends_on", {}) or "db-init" in service.get(
            "depends_on", {}
        ), f"{name} can start before its tokens exist"


def test_the_worker_holds_its_own_token_and_nothing_else() -> None:
    worker = _stack()["services"]["worker"]
    assert worker["environment"] == {
        "PHEASANT_CONFIG": "/config/pheasant.yaml",
        "PHEASANT_INDEX_WORKER_TOKEN_FILE": "/run/pheasant/secrets/worker-token",
    }
    volumes = _volumes(worker)
    assert "pheasant-worker-secret:/run/pheasant/secrets:ro" in volumes
    assert not any(volume.startswith("pheasant-secrets:") for volume in volumes)
    assert any(
        volume.endswith("deploy/compose/worker.yaml:/config/pheasant.yaml:ro") for volume in volumes
    )


def test_only_the_one_shot_initializer_sees_a_token_from_env() -> None:
    services = _stack()["services"]
    for name, service in services.items():
        env = service.get("environment") or {}
        raw = {variable for variable in TOKENS.values() if variable in env}
        if name == "secrets-init":
            assert raw == set(TOKENS.values())
        else:
            assert not raw, f"{name} receives {raw} straight from .env"


def test_the_stack_mounts_the_shipped_config_unless_told_otherwise() -> None:
    services = _stack()["services"]
    for name in PHEASANT_TIERS:
        assert (
            "${PHEASANT_KB_CONFIG:-./deploy/compose/pheasant-kb.yaml}:/config/pheasant.yaml:ro"
            in _volumes(services[name])
        ), name


def test_every_size_is_an_env_knob_with_a_default() -> None:
    """Scaling pheasant-kb is an `.env` edit; a literal size is one that is not."""

    services = _stack()["services"]
    knob = re.compile(r"^\$\{PHEASANT_[A-Z_]+:-[0-9.]+m?\}$")
    for name, service in services.items():
        for key in ("mem_limit", "cpus"):
            if key in service:
                assert knob.match(str(service[key])), (name, key, service[key])
    assert services["worker"]["deploy"]["replicas"] == "${PHEASANT_WORKERS:-4}"
    assert "${PHEASANT_WORKER_THREADS:-2}" in services["worker"]["command"]


def test_the_logger_starts_with_the_stack() -> None:
    """The shipped config turns the observation queue on; nothing else drains it."""

    services = _stack()["services"]
    assert "profiles" not in services["logger"]
    config = _config()
    assert config.observability.interactions.enabled
    assert config.observability.interactions.queue.enabled


def test_every_size_knob_is_listed_in_the_env_template() -> None:
    text = STACK.read_text(encoding="utf-8")
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    knobs = set(re.findall(r"\$\{(PHEASANT_[A-Z_]+):-", text))
    missing = {knob for knob in knobs if knob not in template}
    assert not missing, f"compose.yaml reads knobs .env.example never mentions: {missing}"


def test_the_env_template_pins_no_token() -> None:
    """A copied template must not replace the generated tokens with placeholders."""

    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        for variable in TOKENS.values():
            assert not line.startswith(f"{variable}="), line


# -- the configuration --------------------------------------------------------


def _config() -> PheasantConfig:
    return PheasantConfig.model_validate(
        yaml.safe_load((COMPOSE / "pheasant-kb.yaml").read_text(encoding="utf-8"))
    )


def test_the_shipped_config_is_its_answer_file_regenerated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pheasant.setup_wizard import Wizard

    answers = json.loads((COMPOSE / "answers" / "pheasant-kb.json").read_text(encoding="utf-8"))
    wizard = Wizard(accept_defaults=True, preset=answers)
    wizard.run()
    capsys.readouterr()
    shipped = yaml.safe_load((COMPOSE / "pheasant-kb.yaml").read_text(encoding="utf-8"))
    assert wizard.config_dict() == shipped, "regenerate pheasant-kb.yaml from its answer file"


def test_host_folders_mount_under_data() -> None:
    """The api mounts /workspace read-only, and Docker cannot create a mount
    point inside a read-only volume, so the documented place is /data."""

    config = _config()
    assert config.pheasant.name == "pheasant-kb"
    assert Path("/data") in config.security.allow_workspace_roots
    assert config.security.api_auth.token_env == TOKENS["api-token"]
    assert config.graph.query_service_token_env == TOKENS["graph-token"]
    assert config.sync.concurrency.remote_worker_token_env == TOKENS["worker-token"]
    assert config.ingestion.landing_service_token_env == TOKENS["ingestion-token"]


# -- init-secrets.sh ----------------------------------------------------------


def _init_secrets(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(INIT_SECRETS)],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "SECRETS_DIR": str(tmp_path / "secrets"),
            "WORKER_SECRET_DIR": str(tmp_path / "worker"),
            "SECRETS_OWNER": f"{os.getuid()}:{os.getgid()}",
            **env,
        },
        capture_output=True,
        text=True,
        check=True,
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


@needs_sh
def test_first_boot_generates_four_distinct_tokens(tmp_path: Path) -> None:
    _init_secrets(tmp_path)
    values = {name: _read(tmp_path / "secrets" / name) for name in TOKENS}
    assert len(set(values.values())) == 4
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in values.values())
    assert sorted(path.name for path in (tmp_path / "worker").iterdir()) == ["worker-token"]
    assert _read(tmp_path / "worker" / "worker-token") == values["worker-token"]


@needs_sh
def test_a_restart_keeps_tokens_and_env_replaces_one(tmp_path: Path) -> None:
    _init_secrets(tmp_path)
    before = {name: _read(tmp_path / "secrets" / name) for name in TOKENS}

    _init_secrets(tmp_path)
    assert {name: _read(tmp_path / "secrets" / name) for name in TOKENS} == before

    _init_secrets(tmp_path, PHEASANT_INDEX_WORKER_TOKEN="rotated")
    assert _read(tmp_path / "secrets" / "worker-token") == "rotated"
    assert _read(tmp_path / "worker" / "worker-token") == "rotated"
    assert _read(tmp_path / "secrets" / "api-token") == before["api-token"]


@needs_sh
def test_tokens_are_readable_by_their_owner_only(tmp_path: Path) -> None:
    _init_secrets(tmp_path)
    for path in [*(tmp_path / "secrets").iterdir(), *(tmp_path / "worker").iterdir()]:
        assert path.stat().st_mode & 0o077 == 0, path


# -- the entrypoint's NAME_FILE convention -----------------------------------


def _load_file_secrets(env: dict[str, str], echo: str) -> subprocess.CompletedProcess[str]:
    match = re.search(r"^load_file_secrets\(\) \{\n.*?^\}\n", ENTRYPOINT, re.M | re.S)
    assert match, "docker-entrypoint.sh no longer defines load_file_secrets"
    script = "set -eu\n" + match.group(0) + "load_file_secrets\n" + echo
    return subprocess.run(
        ["sh", "-c", script],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=True,
    )


@needs_sh
def test_a_file_secret_is_exported_under_its_own_name(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("from-file\n", encoding="utf-8")
    out = _load_file_secrets(
        {"PHEASANT_API_TOKEN_FILE": str(tmp_path / "token")}, 'printf %s "$PHEASANT_API_TOKEN"'
    )
    assert out.stdout == "from-file"


@needs_sh
def test_a_set_variable_wins_over_its_file(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("from-file\n", encoding="utf-8")
    out = _load_file_secrets(
        {"OPENAI_API_KEY": "given", "OPENAI_API_KEY_FILE": str(tmp_path / "token")},
        'printf %s "$OPENAI_API_KEY"',
    )
    assert out.stdout == "given"


@needs_sh
def test_an_unreadable_file_is_reported_not_fatal(tmp_path: Path) -> None:
    out = _load_file_secrets(
        {"PHEASANT_API_TOKEN_FILE": str(tmp_path / "absent")},
        'printf %s "${PHEASANT_API_TOKEN-unset}"',
    )
    assert out.stdout == "unset"
    assert "PHEASANT_API_TOKEN_FILE" in out.stderr


# -- backup.sh and restore.sh -------------------------------------------------
#
# The Postgres client is stood in for by three scripts that move one file's
# contents around, so a backup and a restore are exercised end to end
# (checksums, refusals, keeping the replaced data, the volumes) without a
# server. What the stand-ins cannot prove is pg_dump's own fidelity, which is
# Postgres's job.

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

FAKE_PG = {
    "pg_dump": (
        'for a in "$@"; do case "$a" in --file=*) cp "$FAKE_DB" "${a#--file=}";; esac; done\n'
    ),
    "pg_restore": 'for a in "$@"; do last="$a"; done; cp "$last" "$FAKE_DB"\n',
    "psql": 'cat "$FAKE_TABLES"\n',
}


@pytest.fixture
def region(tmp_path: Path) -> dict[str, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in FAKE_PG.items():
        (bin_dir / name).write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    paths = {
        name: tmp_path / name for name in ("backups", "state", "memory", "secrets", "worker-secret")
    }
    for path in paths.values():
        path.mkdir()
    paths["bin"] = bin_dir
    paths["db"] = tmp_path / "db"
    paths["tables"] = tmp_path / "tables"
    paths["db"].write_text("rows-v1\n", encoding="utf-8")
    paths["tables"].write_text("12\n", encoding="utf-8")
    (paths["state"] / "vectors").mkdir()
    (paths["state"] / "vectors" / "v.lance").write_text("vectors-v1", encoding="utf-8")
    (paths["memory"] / "m.md").write_text("memory-v1", encoding="utf-8")
    (paths["secrets"] / "api-token").write_text("token-v1\n", encoding="utf-8")
    (paths["secrets"] / "worker-token").write_text("worker-v1\n", encoding="utf-8")
    return paths


def _run(script: str, region: dict[str, Path], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(COMPOSE / script), *args],
        env={
            "PATH": f"{region['bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "BACKUPS_DIR": str(region["backups"]),
            "STATE_DIR": str(region["state"]),
            "MEMORY_DIR": str(region["memory"]),
            "SECRETS_DIR": str(region["secrets"]),
            "WORKER_SECRET_DIR": str(region["worker-secret"]),
            "PHEASANT_OWNER": f"{os.getuid()}:{os.getgid()}",
            "PG_LIB_DIR": str(region["bin"] / "no-server-binaries"),
            "PHEASANT_WRITERS": "",
            "PHEASANT_SERVICES": "",
            "FAKE_DB": str(region["db"]),
            "FAKE_TABLES": str(region["tables"]),
        },
        capture_output=True,
        text=True,
    )


def _backup(region: dict[str, Path]) -> str:
    out = _run("backup.sh", region)
    assert out.returncode == 0, out.stderr
    [name] = [path.name for path in region["backups"].iterdir()]
    assert name.startswith("pheasant-kb-")
    return name


@needs_bash
def test_a_backup_holds_the_database_and_every_volume(region: dict[str, Path]) -> None:
    name = _backup(region)
    contents = sorted(path.name for path in (region["backups"] / name).iterdir())
    assert contents == [
        "SHA256SUMS",
        "backup.txt",
        "db.pgdump",
        "memory.tar.gz",
        "secrets.tar.gz",
        "state.tar.gz",
    ]
    assert (region["backups"] / name / "db.pgdump").read_text() == "rows-v1\n"


@needs_bash
def test_restore_refuses_a_populated_stack_without_force(region: dict[str, Path]) -> None:
    name = _backup(region)
    out = _run("restore.sh", region, name)
    assert out.returncode == 1
    assert "already holds data in: database /state /memory" in out.stderr
    assert (region["state"] / "vectors" / "v.lance").read_text() == "vectors-v1"


@needs_bash
def test_restore_with_force_brings_the_backup_back_and_keeps_what_it_replaced(
    region: dict[str, Path],
) -> None:
    name = _backup(region)
    region["db"].write_text("rows-v2\n", encoding="utf-8")
    (region["state"] / "vectors" / "v.lance").write_text("vectors-v2", encoding="utf-8")
    (region["memory"] / "m.md").write_text("memory-v2", encoding="utf-8")
    for token in (region["secrets"] / "api-token", region["secrets"] / "worker-token"):
        token.chmod(0o600)
        token.write_text("rotated\n", encoding="utf-8")

    out = _run("restore.sh", region, name, "--force")
    assert out.returncode == 0, out.stderr

    assert region["db"].read_text() == "rows-v1\n"
    assert (region["state"] / "vectors" / "v.lance").read_text() == "vectors-v1"
    assert (region["memory"] / "m.md").read_text() == "memory-v1"
    assert _read(region["secrets"] / "api-token") == "token-v1"
    assert _read(region["worker-secret"] / "worker-token") == "worker-v1"

    [kept] = [p for p in region["backups"].iterdir() if p.name.startswith("pre-restore-")]
    assert (kept / "db.pgdump").read_text() == "rows-v2\n"
    [aside] = list(region["state"].glob(".replaced-*"))
    assert (aside / "vectors" / "v.lance").read_text() == "vectors-v2"
    [aside] = list(region["memory"].glob(".replaced-*"))
    assert (aside / "m.md").read_text() == "memory-v2"


@needs_bash
def test_restore_refuses_a_backup_that_does_not_verify(region: dict[str, Path]) -> None:
    name = _backup(region)
    with (region["backups"] / name / "state.tar.gz").open("ab") as damaged:
        damaged.write(b"\0")
    out = _run("restore.sh", region, name, "--force")
    assert out.returncode != 0
    assert region["db"].read_text() == "rows-v1\n"
    assert not list(region["state"].glob(".replaced-*"))


@needs_bash
def test_restore_into_an_empty_stack_needs_no_force(region: dict[str, Path]) -> None:
    name = _backup(region)
    for directory in ("state", "memory"):
        shutil.rmtree(region[directory])
        region[directory].mkdir()
    region["tables"].write_text("0\n", encoding="utf-8")
    region["db"].write_text("", encoding="utf-8")
    out = _run("restore.sh", region, name)
    assert out.returncode == 0, out.stderr
    assert (region["state"] / "vectors" / "v.lance").read_text() == "vectors-v1"
    assert not list(region["backups"].glob("pre-restore-*"))


def test_backup_and_restore_are_on_demand_only() -> None:
    services = _stack()["services"]
    for name in ("backup", "restore"):
        assert services[name]["profiles"] == ["tools"], name
    backup_volumes = _volumes(services["backup"])
    assert all(
        volume.endswith(":ro") for volume in backup_volumes if not volume.endswith(":/backups")
    ), "a backup reads the volumes it copies; it must not be able to change them"


@pytest.mark.parametrize("command", ["backup", "restore"])
def test_pheasant_backup_refuses_a_postgres_region(
    tmp_path: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """It used to archive /state without the database and report success."""

    from pheasant.cli import main

    config = tmp_path / "pheasant.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "pheasant": {"state_path": str(tmp_path / "state")},
                "storage": {"backend": "postgres", "dsn_env": "UNSET_FOR_THIS_TEST"},
            }
        ),
        encoding="utf-8",
    )
    archive = tmp_path / "out.tar.zst"
    argv = (
        ["backup", str(archive), "--config", str(config)]
        if command == "backup"
        else ["restore", str(archive), "--config", str(config)]
    )
    assert main(argv) == 2
    assert "docker compose run --rm backup" in capsys.readouterr().err
    assert not archive.exists()
