from __future__ import annotations

from pathlib import Path

from pheasant.config.loader import load_layered_config, parse_override_pairs
from tests.conftest import call_cli


def test_layered_config_profile_and_overrides(config_path: Path) -> None:
    cfg = load_layered_config(
        config_path,
        profile="team",
        overrides=parse_override_pairs(["server.port=9999", "search.default_mode=vector"]),
    )

    assert cfg.server.port == 9999
    assert cfg.pheasant.environment == "team"
    assert cfg.search.default_mode == "vector"
    assert {source.name for source in cfg.sources} == {
        "pheasant-repo",
        "architecture-notes",
        "product-documents",
    }


def test_cli_config_show_and_doctor(config_path: Path) -> None:
    show_result = call_cli(
        [
            "config",
            "show",
            "--effective",
            "--config",
            str(config_path),
            "--profile",
            "dev",
            "--set",
            "server.port=9001",
        ]
    )
    doctor_result = call_cli(["doctor", "--config", str(config_path), "--profile", "quickstart"])

    assert show_result.returncode == 0, show_result.stderr or show_result.stdout
    assert "port: 9001" in show_result.stdout
    assert "pheasant-repo" in show_result.stdout
    assert doctor_result.returncode == 0, doctor_result.stderr or doctor_result.stdout
    assert "Doctor ok" in doctor_result.stdout
