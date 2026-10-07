"""The profiles pheasant-swarm-search drives: what the lab needs from a region.

Two Compose shapes run under the lab. ``swarm-lab.yaml`` is the single
container pheasant-swarm-search's own ``docker-compose.yml`` bundles (it
vendors this file); ``answers/pheasant-lab.json`` is the role-split lab fleet
its ``docker-compose.fleet.yml`` joins. In both, the lab reaches the region
**by Compose service name**, which is the case the MCP DNS-rebinding guard
refuses unless the origin is admitted: every MCP call answered 421 Misdirected
Request. That was found by running the lab's container against the bundled
region; nothing in-process could see it, because a test client is loopback.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from pheasant.config.schema import PheasantConfig

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "compose"


def _swarm_lab() -> PheasantConfig:
    return PheasantConfig.model_validate(
        yaml.safe_load((COMPOSE / "swarm-lab.yaml").read_text(encoding="utf-8"))
    )


def _generated(answers: str, tmp_path: Path) -> tuple[str, PheasantConfig]:
    output = tmp_path / f"{answers}.yaml"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pheasant",
            "setup",
            "--answers",
            str(COMPOSE / "answers" / f"{answers}.json"),
            "--accept-defaults",
            "--plain",
            "--target",
            "compose",
            "--output",
            str(output),
            "--force",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    text = output.read_text(encoding="utf-8")
    return text, PheasantConfig.model_validate(yaml.safe_load(text))


def test_the_swarm_lab_profile_is_what_its_answer_file_generates(tmp_path: Path) -> None:
    text, _ = _generated("swarm-lab", tmp_path)
    assert text == (COMPOSE / "swarm-lab.yaml").read_text(encoding="utf-8"), (
        "regenerate deploy/compose/swarm-lab.yaml from answers/swarm-lab.json "
        "(and copy it to pheasant-swarm-search's deploy/pheasant/pheasant.yaml)"
    )


def test_the_swarm_lab_region_takes_what_the_lab_sends() -> None:
    config = _swarm_lab()
    assert config.pheasant.name == "pheasant-lab"
    assert config.server.role == "all"
    assert config.server.mcp.enabled and config.server.mcp.transports["streamable_http"]
    # The lab registers <state>/uploads/<source> as a document_folder source.
    assert Path("/state") in config.security.allow_workspace_roots
    assert Path(config.pheasant.state_path) == Path("/state")
    assert config.security.api_auth.token_env == "PHEASANT_API_TOKEN"
    # The P1 arm writes memory; steering records only act with steering on.
    assert any(source.type == "memory" for source in config.sources)
    assert config.memory.steering_enabled
    # Standalone: no queue, no broker, no model key (rule 7).
    assert not config.sync.queue.enabled
    assert not config.search.embeddings.enabled


@pytest.mark.parametrize(
    ("answers", "host"),
    [("swarm-lab", "pheasant:8765"), ("pheasant-lab", "api:8765")],
)
def test_the_mcp_guard_admits_the_service_name_the_lab_uses(
    answers: str, host: str, tmp_path: Path
) -> None:
    pytest.importorskip("mcp")
    from pheasant.mcp_server.server import _transport_security

    _, config = _generated(answers, tmp_path)
    guard = _transport_security(config)
    assert guard.enable_dns_rebinding_protection is True
    assert host in guard.allowed_hosts
    # Widened, never replaced: a browser on the host still reaches it.
    assert "127.0.0.1:8765" in guard.allowed_hosts


def test_the_answer_files_keep_the_default_origins() -> None:
    """An answer replaces the whole list, so the defaults are restated."""

    defaults = PheasantConfig().server.api.cors_origins
    for name in ("swarm-lab", "pheasant-lab"):
        answers = json.loads((COMPOSE / "answers" / f"{name}.json").read_text(encoding="utf-8"))
        assert set(defaults) <= set(answers["server.api.cors_origins"]), name
