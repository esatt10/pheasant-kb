"""The compose fleet: the role-split topology pheasant-kb runs.

A deployment file is code that runs somewhere else, where a typo surfaces as a
crash loop rather than a stack trace. These tests check what couples the
compose files to *this* codebase: the embedded configs are valid for the role
each service passes, the boundaries keep their own secrets, and the defaults
the answer files regenerate are the ones the YAML ships.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from pheasant.config.schema import PheasantConfig
from pheasant.deployment.roles import resolve_role, validate_role

REPO_ROOT = Path(__file__).resolve().parents[1]


def _documents(path: Path) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


# --------------------------------------------------------------------------
# Compose
# --------------------------------------------------------------------------

COMPOSE_FLEET = REPO_ROOT / "deploy" / "compose" / "docker-compose.scale.yml"
COMPOSE_CONFIG = REPO_ROOT / "deploy" / "compose"


def test_compose_manifests_live_under_the_deployment_directory() -> None:
    assert not list(REPO_ROOT.glob("docker-compose*.yml"))
    assert {path.name for path in COMPOSE_CONFIG.glob("docker-compose*.yml")} == {
        "docker-compose.gtm.yml",
        "docker-compose.scale.yml",
    }


def test_the_compose_fleet_has_the_high_throughput_topology() -> None:
    """Four Pheasant tiers, one migrator, and durable infrastructure."""

    compose = yaml.safe_load(COMPOSE_FLEET.read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {
        "postgres",
        "nats",
        "db-init",
        "workspace-init",
        "api",
        "graph",
        "indexer",
        "logger",
        "worker",
    }
    # The log tier is defined but not part of the default `up`: recording
    # queries and principals is an operator's decision, not a default, and a
    # logger with observation off refuses to start rather than idling green.
    assert services["logger"]["profiles"] == ["observability"]
    assert all("profiles" not in services[name] for name in set(services) - {"logger"})

    roles = {}
    for name in ("api", "graph", "indexer"):
        command = services[name]["command"]
        assert command[:2] == ["serve", "--role"]
        roles[name] = command[2]
    assert roles == {"api": "api", "graph": "graph", "indexer": "indexer"}

    worker_command = services["worker"]["command"]
    assert worker_command[:3] == ["worker", "--transport", "grpc"]
    assert "--max-workers" in worker_command
    assert "scale" not in services["indexer"]

    assert services["db-init"]["restart"] == "no"
    for name in ("api", "graph", "indexer"):
        assert services[name]["depends_on"]["db-init"] == {
            "condition": "service_completed_successfully"
        }
        assert services[name]["depends_on"]["workspace-init"] == {
            "condition": "service_completed_successfully"
        }


def test_the_compose_fleet_starts_from_an_isolated_workspace_volume() -> None:
    compose = yaml.safe_load(COMPOSE_FLEET.read_text(encoding="utf-8"))

    assert "pheasant-workspace" in compose["volumes"]
    assert (
        "${PHEASANT_FLEET_WORKSPACE_PATH:-pheasant-workspace}:/workspace:ro"
        in compose["services"]["api"]["volumes"]
    )
    assert (
        "${PHEASANT_FLEET_WORKSPACE_PATH:-pheasant-workspace}:/workspace"
        in compose["services"]["indexer"]["volumes"]
    )
    initializer = compose["services"]["workspace-init"]
    assert initializer["user"] == "0:0"
    init_command = initializer["command"]
    assert len(init_command) == 1
    assert "mkdir -p /workspace/sources" in init_command[0]
    assert "chown 10001:10001 /workspace /workspace/sources" in init_command[0]
    assert initializer["volumes"] == [
        "${PHEASANT_FLEET_WORKSPACE_PATH:-pheasant-workspace}:/workspace"
    ]


def test_the_compose_worker_gets_no_database_url() -> None:
    """Same trust boundary as the Kubernetes manifests, checked the same way."""

    compose = yaml.safe_load(COMPOSE_FLEET.read_text(encoding="utf-8"))
    env = compose["services"]["worker"]["environment"]
    assert "PHEASANT_DATABASE_URL" not in env
    assert "PHEASANT_INDEX_WORKER_TOKEN" in env


def _compose_env(path: Path, service: str) -> dict[str, str]:
    """The environment one Compose service resolves, `${VAR:?...}` included.

    Written out rather than shelled to `docker compose config` so the suite
    stays offline. It only has to understand what these files use: a literal
    value, `${VAR}`, `${VAR:-default}` and `${VAR:?message}` — the last of
    which is the one that matters, because it is how the fleet refuses to come
    up without a secret.
    """

    import re

    compose = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw = compose["services"][service].get("environment") or {}
    resolved: dict[str, str] = {}
    for key, value in raw.items():
        text = str(value)
        for reference, default in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:[?-][^}]*)?\}", text):
            # The operator has not set anything, so `:-` gives its default and
            # `:?` gives a stand-in named after the variable — distinct per
            # variable, which is what makes the shared-secret check below real.
            substitute = default[2:] if default.startswith(":-") else f"unset-{reference}"
            text = re.sub(r"\$\{" + reference + r"(:[?-][^}]*)?\}", substitute, text)
        resolved[key] = text
    return resolved


def test_the_compose_fleet_gives_each_boundary_its_own_secret() -> None:
    """One value across two boundaries makes the weakest holder set the strength.

    The shipped file wired `PHEASANT_GRAPH_SERVICE_TOKEN` to the worker
    token's value. Workers are the least-trusted tier in the fleet and hold
    the indexing token by necessity, so any compromised worker also held the
    credential for the internal graph API — which serves the whole graph.
    """

    fleet_env = _compose_env(COMPOSE_FLEET, "api")
    worker_env = _compose_env(COMPOSE_FLEET, "worker")

    for required in (
        "PHEASANT_API_TOKEN",
        "PHEASANT_GRAPH_SERVICE_TOKEN",
        # The write boundary: holding it means being able to put bytes into the
        # corpus, which is why it is not the graph token and not the API token.
        "PHEASANT_INGESTION_SERVICE_TOKEN",
    ):
        assert required in fleet_env
        # `${VAR:?...}` — the fleet refuses to come up rather than defaulting.
        assert fleet_env[required].startswith("unset-"), f"{required} has a default"
    assert fleet_env["PHEASANT_GRAPH_SERVICE_TOKEN"] != fleet_env["PHEASANT_INDEX_WORKER_TOKEN"]
    assert fleet_env["PHEASANT_INGESTION_SERVICE_TOKEN"] != fleet_env["PHEASANT_INDEX_WORKER_TOKEN"]
    assert (
        fleet_env["PHEASANT_INGESTION_SERVICE_TOKEN"] != fleet_env["PHEASANT_GRAPH_SERVICE_TOKEN"]
    )

    # And the worker still gets exactly one secret.
    assert set(worker_env) == {"PHEASANT_CONFIG", "PHEASANT_INDEX_WORKER_TOKEN"}


def test_the_compose_configs_are_valid_for_their_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    fleet = PheasantConfig.model_validate(
        yaml.safe_load((COMPOSE_CONFIG / "fleet.yaml").read_text(encoding="utf-8"))
    )
    worker = PheasantConfig.model_validate(
        yaml.safe_load((COMPOSE_CONFIG / "worker.yaml").read_text(encoding="utf-8"))
    )

    # The environment the compose file actually supplies each tier — a serving
    # role refuses an unauthenticated non-loopback bind, and a worker refuses
    # to hold a credential it can never use, so validating against an empty
    # environment would test neither.
    for role in ("api", "graph", "indexer"):
        with monkeypatch.context() as patched:
            for key, value in _compose_env(COMPOSE_FLEET, role).items():
                patched.setenv(key, value)
            validate_role(resolve_role(fleet, role), fleet)
    with monkeypatch.context() as patched:
        for key in ("PHEASANT_DATABASE_URL", "PHEASANT_API_TOKEN", "OPENAI_API_KEY"):
            patched.delenv(key, raising=False)
        for key, value in _compose_env(COMPOSE_FLEET, "worker").items():
            patched.setenv(key, value)
        validate_role(resolve_role(worker, "worker"), worker)

    assert fleet.security.acl_enforced is True
    assert fleet.security.api_auth.token_env == "PHEASANT_API_TOKEN"
    assert worker.server.api.enabled is False

    assert fleet.sync.queue.enabled is True
    assert fleet.sync.queue.backend == "nats"
    assert fleet.sync.queue.nats_servers == ["nats://nats:4222"]
    assert fleet.storage.backend == "postgres"
    # Addressed by Compose service name, so `--scale worker=N` needs no edit.
    assert fleet.sync.concurrency.remote_worker_urls == ["grpc://worker:8766"]
    assert fleet.sync.concurrency.worker_transport == "grpc"
    assert worker.sync.concurrency.remote_worker_enabled is True
    assert fleet.server.api.drain_seconds > 0
    assert fleet.graph.query_service_url == "http://graph:8765"
    assert fleet.graph.query_service_token_env == "PHEASANT_GRAPH_SERVICE_TOKEN"


CI_COMPOSE = REPO_ROOT / "deploy" / "compose" / "ci" / "docker-compose.log-tier.yml"
CI_CONFIG = REPO_ROOT / "deploy" / "compose" / "ci" / "pheasant.log-tier.yaml"


def test_the_log_tier_topology_splits_producing_from_draining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of that compose file.

    `all` would drain what it publishes -- correct for one container, and it
    would make the CI smoke test prove nothing, because the producer would
    consume its own batches. The roles have to be split or the tier is
    untested.
    """

    compose = yaml.safe_load(CI_COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {"postgres", "nats", "db-init", "api", "logger"}
    assert services["api"]["command"] == ["serve", "--role", "api"]
    assert services["logger"]["command"] == ["serve", "--role", "logger"]

    config = PheasantConfig.model_validate(yaml.safe_load(CI_CONFIG.read_text(encoding="utf-8")))
    # Both roles must actually be startable against this config: `validate_role`
    # is the same function the process runs, and it refuses an api without an
    # index queue, a logger without an observation queue, and either of them
    # unauthenticated on a routable bind -- which is why the stack supplies a
    # token rather than configuring the guard away.
    env = _compose_env(CI_COMPOSE, "api")
    assert env.get("PHEASANT_API_TOKEN")
    for role in ("api", "logger"):
        with monkeypatch.context() as patched:
            patched.setenv("PHEASANT_API_TOKEN", env["PHEASANT_API_TOKEN"])
            validate_role(resolve_role(config, role), config)

    # And the producer must genuinely not drain, which is what
    # `_owns_log_upkeep` decides.
    from pheasant.cli import _owns_log_upkeep

    assert _owns_log_upkeep(config, resolve_role(config, "api")) is False
    assert _owns_log_upkeep(config, resolve_role(config, "logger")) is False


def test_the_ci_topology_exercises_the_whole_ledger_path() -> None:
    """A smoke test with cold storage off, or formation off, would pass while
    testing half of what it claims to."""

    config = PheasantConfig.model_validate(yaml.safe_load(CI_CONFIG.read_text(encoding="utf-8")))
    interactions = config.observability.interactions

    assert interactions.enabled
    assert interactions.queue.enabled
    assert interactions.cold_enabled
    assert config.memory.formation.enabled
    # PostgreSQL, because that is the backend the fleet actually runs and the
    # one whose migration path differs.
    assert config.storage.backend == "postgres"


# -- dynamic chunking and fleet PDF reading are the fleet's defaults ---------

COMPOSE = REPO_ROOT / "deploy" / "compose"


def _fleet_configs() -> dict[str, Any]:
    import json

    return {
        "compose fleet.yaml": PheasantConfig.model_validate(
            yaml.safe_load((COMPOSE / "fleet.yaml").read_text(encoding="utf-8"))
        ),
        **{
            f"answers/{name}.json": json.loads((COMPOSE / "answers" / f"{name}.json").read_text())
            for name in ("scalable", "pheasant-kb")
        },
    }


def _probe_source(config: PheasantConfig) -> Any:
    probe = PheasantConfig.model_validate(
        {"sources": [{"name": "probe", "type": "document_folder", "path": "/tmp"}]}
    ).sources[0]
    return config.effective_source(probe)


@pytest.mark.parametrize("name", ["compose fleet.yaml"])
def test_every_fleet_chunks_by_plan_and_reads_long_pdfs_on_its_workers(name: str) -> None:
    from pheasant.ingestion.chunk_plan import strategy_of
    from pheasant.sync.pdf_split import remote_pdf_pages

    config = _fleet_configs()[name]
    assert strategy_of(_probe_source(config)) == "auto"
    reader = remote_pdf_pages(config)
    assert reader is not None and reader.pages_per_task == 500
    assert reader.transport == config.sync.concurrency.worker_transport


@pytest.mark.parametrize("name", ["answers/scalable.json", "answers/pheasant-kb.json"])
def test_the_fleet_answer_files_regenerate_the_same_defaults(name: str) -> None:
    """`fleet.yaml` is regenerated from its answer file; a default that lives
    only in the YAML is one regeneration away from disappearing."""

    answers = _fleet_configs()[name]
    assert answers["sync.source_processing.chunk_strategy"] == "auto"
    assert answers["sync.concurrency.remote_worker_pdf_pages_per_task"] == 500


def _wizard_config(name: str) -> dict[str, Any]:
    import json

    from pheasant.setup_wizard import Wizard

    answers = json.loads((COMPOSE / "answers" / f"{name}.json").read_text(encoding="utf-8"))
    wizard = Wizard(accept_defaults=True, preset=answers)
    wizard.run()
    return wizard.config_dict()


def test_the_lab_fleet_admits_what_pheasant_swarm_search_needs() -> None:
    """pheasant-swarm-search's container reaches the fleet as `api:8765`, and
    the MCP host guard is derived from `cors_origins`: an origin missing there
    is a 421 on every call, on a region every in-process test reaches by
    loopback. The answers listed `host.docker.internal` and not `api`. The rest
    is what the lab's first sync and its P1 arm need."""

    from pheasant.mcp_server.server import _transport_security

    config = PheasantConfig.model_validate(_wizard_config("pheasant-kb"))
    assert "api:8765" in _transport_security(config).allowed_hosts
    assert config.pheasant.name == "pheasant-kb"
    assert Path("/state") in config.security.allow_workspace_roots
    assert config.readiness.enabled
    assert config.security.api_auth.token_env == "PHEASANT_API_TOKEN"
    assert not any(source.name == "agent-memory" for source in config.sources)
