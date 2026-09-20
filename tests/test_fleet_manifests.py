"""Phase 35.6c — the role-split deployment manifests.

A manifest is code that runs on someone else's cluster, where a typo surfaces
as a CrashLoopBackOff rather than a stack trace. These tests check the things
that would only fail there: that the embedded configs are valid for the role
their workload passes, that the roles named in `args` exist, and that the
timing values which must agree across two files actually do.

Deliberately not tested: that Kubernetes accepts the manifests. That needs a
cluster or a schema bundle, and `kubeconform` in CI is the right place for it.
What is testable offline is the part that couples the manifests to *this*
codebase, and that is what is here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from pheasant.config.schema import PheasantConfig
from pheasant.deployment.roles import Role, resolve_role, validate_role

REPO_ROOT = Path(__file__).resolve().parents[1]
SCALED = REPO_ROOT / "deploy" / "kubernetes" / "scaled"
KUBERNETES = REPO_ROOT / "deploy" / "kubernetes"
HELM = REPO_ROOT / "deploy" / "helm"
BASE = REPO_ROOT / "deploy" / "kubernetes"


def _documents(path: Path) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def _all_documents(directory: Path) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        docs.extend(_documents(path))
    return docs


def _by_kind(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc.get("kind") == kind]


def _workloads(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _by_kind(docs, "Deployment") + _by_kind(docs, "StatefulSet")


def _pod_spec(workload: dict[str, Any]) -> dict[str, Any]:
    return workload["spec"]["template"]["spec"]


def _container(workload: dict[str, Any]) -> dict[str, Any]:
    return _pod_spec(workload)["containers"][0]


@pytest.fixture(scope="module")
def scaled() -> list[dict[str, Any]]:
    return _all_documents(SCALED)


# --------------------------------------------------------------------------
# The manifests describe a fleet this code can actually run
# --------------------------------------------------------------------------


def test_every_manifest_parses(scaled: list[dict[str, Any]]) -> None:
    assert scaled, "no manifests found"
    for doc in scaled:
        assert doc.get("kind"), f"document without a kind: {doc}"
        assert doc.get("metadata", {}).get("name")


def test_the_image_installs_every_extra_the_manifests_configure(
    scaled: list[dict[str, Any]],
) -> None:
    """The manifests and the image they run must agree.

    `PHEASANT_EXTRAS` defaulted to `mcp,agent,vector,wasm,a2a` while the scaled
    ConfigMaps select `storage.backend: postgres` — so the published image
    could not run the topology this repo publishes: every indexer would have
    come up and raised "psycopg is not installed". Mechanical, so adding a
    backend without adding its extra fails here rather than in a cluster.
    """

    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    line = next(raw for raw in dockerfile.splitlines() if raw.startswith("ARG PHEASANT_EXTRAS="))
    extras = {item.strip() for item in line.split("=", 1)[1].split(",")}

    #: config value -> the extra that supplies it
    needed = {
        ("storage", "backend", "postgres"): "postgres",
        ("sync", "queue", "nats"): "queue",
        ("sync", "concurrency", "grpc"): "grpc",
    }
    required: set[str] = set()
    for doc in _by_kind(scaled, "ConfigMap"):
        blob = str(doc.get("data") or {})
        for (_section, _key, value), extra in needed.items():
            if f": {value}" in blob:
                required.add(extra)

    assert required, "no scale-out backend is configured in any manifest"
    missing = required - extras
    assert not missing, (
        f"the manifests select {sorted(required)} but the image installs {sorted(extras)}; "
        f"missing: {sorted(missing)}"
    )


def test_every_workload_passes_a_role_this_code_knows(scaled: list[dict[str, Any]]) -> None:
    """A role typo in `args` is a CrashLoopBackOff on someone else's cluster."""

    valid = {role.value for role in Role}
    seen = set()
    for workload in _workloads(scaled):
        args = _container(workload)["args"]
        assert args[0] == "serve", f"{workload['metadata']['name']} does not run `serve`"
        assert args[1] == "--role"
        assert args[2] in valid, f"unknown role {args[2]!r}"
        seen.add(args[2])
    # No "logger": the log tier lives in scaled/observability/, which
    # `kubectl apply -f scaled/` does not recurse into — the Kubernetes
    # equivalent of the Compose `observability` profile. Applying it means
    # first editing the shared ConfigMap to record queries and principals,
    # which must be a decision rather than a default.
    assert seen == {"api", "graph", "indexer", "worker"}


def _env_from_manifest(container: dict[str, Any]) -> dict[str, str]:
    """The variables a workload actually wires, secret values stubbed.

    A manifest names a Secret key; what a startup check reads is whatever that
    key resolves to. Stubbing each with its own distinct value is what lets the
    tests below assert on both — that a workload wires the variable at all, and
    that two boundaries do not resolve to one string.
    """

    resolved: dict[str, str] = {}
    for entry in container.get("env") or []:
        name = entry["name"]
        if "value" in entry:
            resolved[name] = str(entry["value"])
        else:
            key = entry["valueFrom"]["secretKeyRef"]["key"]
            resolved[name] = f"stub-value-for-{key}"
    return resolved


def test_the_embedded_configs_are_valid_for_the_role_that_uses_them(
    scaled: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The api role refuses to start without a queue — so the config must have one.

    This is the check that would have caught shipping a fleet ConfigMap with
    `sync.queue.enabled` left at its default: `validate_role` is the same
    function the process runs at startup.

    Since 35.8 it is also the check that would have caught shipping a serving
    workload with no `PHEASANT_API_TOKEN` wired, or with the graph and worker
    tokens pointed at one Secret key — so the workload's own `env` block is
    what the config is validated against, rather than an empty environment
    that no pod ever has.
    """

    configs = {
        doc["metadata"]["name"]: PheasantConfig.model_validate(yaml.safe_load(raw))
        for doc in _by_kind(scaled, "ConfigMap")
        for raw in doc["data"].values()
    }
    assert set(configs) == {"pheasant-fleet-config", "pheasant-worker-config"}

    for workload in _workloads(scaled):
        container = _container(workload)
        role = container["args"][2]
        volumes = {volume["name"]: volume for volume in _pod_spec(workload)["volumes"]}
        name = volumes["config"]["configMap"]["name"]
        config = configs[name]
        with monkeypatch.context() as patched:
            for key in ("PHEASANT_API_TOKEN", "PHEASANT_GRAPH_SERVICE_TOKEN"):
                patched.delenv(key, raising=False)
            for key, value in _env_from_manifest(container).items():
                patched.setenv(key, value)
            validate_role(resolve_role(config, role), config)


def test_every_serving_workload_authenticates_its_api(scaled: list[dict[str, Any]]) -> None:
    """A pod binds 0.0.0.0 by necessity; nothing else stands in front of it.

    The finding this closes: the single-container posture — unauthenticated,
    published on loopback — shipped unchanged into the fleet profile, where the
    API is a multi-replica Service and the only control was a port-publishing
    decision an operator can reasonably change.
    """

    for workload in _workloads(scaled):
        container = _container(workload)
        role = container["args"][2]
        env = _env_from_manifest(container)
        if role == "worker":
            # The one workload that needs no token, because it serves no
            # knowledge-base API at all — asserted directly below.
            assert "PHEASANT_API_TOKEN" not in env
            continue
        assert "PHEASANT_API_TOKEN" in env, f"{role} serves an API with nothing in front of it"


def test_the_worker_serves_no_knowledge_base_api(scaled: list[dict[str, Any]]) -> None:
    """`server.api.enabled: false`, and it is enforced, not decorative.

    A worker holds no state, no keys and no source list. Serving the
    register-a-source API from the tier that scales hardest and runs
    third-party parse code was surface bought for nothing.
    """

    worker = next(
        PheasantConfig.model_validate(yaml.safe_load(raw))
        for doc in _by_kind(scaled, "ConfigMap")
        if doc["metadata"]["name"] == "pheasant-worker-config"
        for raw in doc["data"].values()
    )
    assert worker.server.api.enabled is False
    assert worker.server.ui.enabled is False
    assert worker.server.mcp.enabled is False
    # …and the preparation endpoints it does serve are still switched on.
    assert worker.sync.concurrency.remote_worker_enabled is True


def test_the_two_internal_boundaries_do_not_share_a_secret(scaled: list[dict[str, Any]]) -> None:
    """One value across two boundaries makes the weaker holder set the strength.

    Workers hold the indexing token by necessity and are the least-trusted
    tier in the fleet. The Compose file used to wire the graph token to the
    worker token's value, so compromising any worker also yielded the
    credential for the internal graph API — which serves the whole graph.
    """

    for workload in _workloads(scaled):
        env = _env_from_manifest(_container(workload))
        graph, worker = (
            env.get("PHEASANT_GRAPH_SERVICE_TOKEN"),
            env.get("PHEASANT_INDEX_WORKER_TOKEN"),
        )
        if graph and worker:
            assert graph != worker, "the graph and worker tokens resolve to one Secret key"


def test_the_scaled_manifests_restrict_the_internal_tiers(scaled: list[dict[str, Any]]) -> None:
    """A token check is the last line, not the only one.

    The graph service returns nodes and edges; the workers accept parse work.
    On a flat pod network every workload in the cluster gets to try, so the
    scaled bundle ships a default-deny with one allowance per real caller.
    """

    policies = {doc["metadata"]["name"]: doc for doc in _by_kind(scaled, "NetworkPolicy")}
    assert "pheasant-default-deny-ingress" in policies
    default = policies["pheasant-default-deny-ingress"]
    assert default["spec"]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "pheasant"}
    assert default["spec"]["policyTypes"] == ["Ingress"]
    # A default-deny that names ingress rules is not a default-deny.
    assert not default["spec"].get("ingress")

    for component in ("graph", "worker"):
        policy = policies[f"pheasant-{component}-ingress"]
        sources = [source for rule in policy["spec"]["ingress"] for source in rule["from"]]
        assert any("podSelector" in source for source in sources), (
            f"the {component} tier accepts traffic from outside the namespace"
        )
        assert not any(source.get("namespaceSelector") == {} for source in sources), (
            f"the {component} tier is open to every namespace"
        )


def test_the_api_and_indexer_share_one_knowledge_base(scaled: list[dict[str, Any]]) -> None:
    """Two names would be two knowledge bases, and every stable ID starts with it."""

    fleet = next(
        PheasantConfig.model_validate(yaml.safe_load(raw))
        for doc in _by_kind(scaled, "ConfigMap")
        if doc["metadata"]["name"] == "pheasant-fleet-config"
        for raw in doc["data"].values()
    )
    api, indexer = (
        next(w for w in _workloads(scaled) if _container(w)["args"][2] == role)
        for role in ("api", "indexer")
    )
    for workload in (api, indexer):
        volumes = {volume["name"]: volume for volume in _pod_spec(workload)["volumes"]}
        assert volumes["config"]["configMap"]["name"] == "pheasant-fleet-config"
    assert fleet.pheasant.name


def test_scaled_assistant_avoids_redundant_text_fanout(
    scaled: list[dict[str, Any]],
) -> None:
    """Hybrid already includes lexical search; a text arm repeats that query."""

    fleet = next(
        PheasantConfig.model_validate(yaml.safe_load(raw))
        for doc in _by_kind(scaled, "ConfigMap")
        if doc["metadata"]["name"] == "pheasant-fleet-config"
        for raw in doc["data"].values()
    )

    assert fleet.assistant.retrieval.retrieval_modes == ["vector", "graph", "hybrid"]


# --------------------------------------------------------------------------
# The trust boundary
# --------------------------------------------------------------------------


def test_workers_never_receive_the_database_dsn(scaled: list[dict[str, Any]]) -> None:
    """A worker parses bytes. It has no reason to reach the knowledge base.

    The whole argument for autoscaling workers hard and killing them freely is
    that they hold nothing; handing them a DSN would quietly retract it.
    """

    worker = next(w for w in _workloads(scaled) if _container(w)["args"][2] == "worker")
    env_names = {entry["name"] for entry in _container(worker).get("env", [])}

    assert "PHEASANT_DATABASE_URL" not in env_names
    assert "PHEASANT_INDEX_WORKER_TOKEN" in env_names, "a worker cannot authenticate without it"


def test_no_secret_values_are_baked_into_a_configmap(scaled: list[dict[str, Any]]) -> None:
    """Only env-var *names* reach YAML — the house rule for every credential."""

    for doc in _by_kind(scaled, "ConfigMap"):
        for raw in doc["data"].values():
            config = PheasantConfig.model_validate(yaml.safe_load(raw))
            assert getattr(config.storage, "dsn", None) in (None, "")
            assert "postgresql://" not in raw, "a DSN was written into a ConfigMap"
            # The env var is referenced by name, which is the whole point.
            if config.storage.backend == "postgres":
                assert config.storage.dsn_env


def test_every_pod_drops_capabilities_and_runs_as_non_root(scaled: list[dict[str, Any]]) -> None:
    for workload in _workloads(scaled):
        pod = _pod_spec(workload)
        assert pod["securityContext"]["runAsNonRoot"] is True
        container = _container(workload)["securityContext"]
        assert container["allowPrivilegeEscalation"] is False
        assert container["readOnlyRootFilesystem"] is True
        assert container["capabilities"]["drop"] == ["ALL"]


def test_api_replicas_mount_state_read_only(scaled: list[dict[str, Any]]) -> None:
    """One writer of /state. The graph is a file, and torn writes are forever."""

    api = next(w for w in _workloads(scaled) if _container(w)["args"][2] == "api")
    mounts = {mount["name"]: mount for mount in _container(api)["volumeMounts"]}
    assert mounts["state"].get("readOnly") is True

    graph = next(w for w in _workloads(scaled) if _container(w)["args"][2] == "graph")
    graph_mounts = {mount["name"]: mount for mount in _container(graph)["volumeMounts"]}
    assert graph_mounts["state"].get("readOnly") is True

    indexer = next(w for w in _workloads(scaled) if _container(w)["args"][2] == "indexer")
    indexer_mounts = {mount["name"]: mount for mount in _container(indexer)["volumeMounts"]}
    assert not indexer_mounts["state"].get("readOnly"), "the indexer must be able to write /state"


def test_the_shared_state_volume_is_readwritemany(scaled: list[dict[str, Any]]) -> None:
    """RWO attaches to one node, so api replicas elsewhere cannot read the graph.

    A hard requirement rather than a preference, and the one most likely to be
    skipped — most default StorageClasses are RWO.
    """

    claims = {doc["metadata"]["name"]: doc for doc in _by_kind(scaled, "PersistentVolumeClaim")}
    assert claims["pheasant-state"]["spec"]["accessModes"] == ["ReadWriteMany"]


def test_the_exports_volume_is_durable_and_shared(scaled: list[dict[str, Any]]) -> None:
    """`/exports` exists to be read by something that is not pheasant.

    It was an `emptyDir` in all three workloads, which failed that purpose
    three ways at once: an emptyDir dies with its pod, nothing outside the pod
    can mount it, and — because each workload declared its own — the export
    the indexer wrote landed somewhere the api replicas could not see. Three
    empty directories all called `/exports`.
    """

    claims: set[str] = set()
    for workload in _workloads(scaled):
        volumes = {volume["name"]: volume for volume in _pod_spec(workload)["volumes"]}
        exports = volumes["exports"]
        name = workload["metadata"]["name"]
        assert "emptyDir" not in exports, (
            f"{name} backs /exports with an emptyDir: it dies with the pod and "
            "nothing outside the pod can read it"
        )
        assert "persistentVolumeClaim" in exports, name
        claims.add(exports["persistentVolumeClaim"]["claimName"])

    assert len(claims) == 1, (
        f"the workloads mount different export claims ({sorted(claims)}); whoever "
        "runs the export must write where the readers look"
    )
    declared = {doc["metadata"]["name"] for doc in _by_kind(scaled, "PersistentVolumeClaim")}
    assert claims <= declared, f"{claims - declared} is mounted but never declared"


def test_the_shared_exports_volume_is_readwritemany(scaled: list[dict[str, Any]]) -> None:
    """Same argument as `/state`: RWO attaches to one node, so a reader pod
    scheduled elsewhere cannot mount the export at all."""

    claims = {doc["metadata"]["name"]: doc for doc in _by_kind(scaled, "PersistentVolumeClaim")}
    assert claims["pheasant-exports"]["spec"]["accessModes"] == ["ReadWriteMany"]


def test_the_single_container_deployment_persists_exports() -> None:
    """The plain install has the same requirement for the same reason, minus
    the sharing: an export nothing can reach is an export nobody has."""

    docs = _all_documents(KUBERNETES)
    deployment = next(iter(_workloads(docs)))
    volumes = {volume["name"]: volume for volume in _pod_spec(deployment)["volumes"]}
    assert "persistentVolumeClaim" in volumes["exports"]
    claim = volumes["exports"]["persistentVolumeClaim"]["claimName"]
    assert claim in {doc["metadata"]["name"] for doc in _by_kind(docs, "PersistentVolumeClaim")}


def test_the_helm_chart_can_persist_exports_and_defaults_to_doing_so() -> None:
    """The chart's own values, since the chart is not rendered here.

    Checked rather than assumed because the failure is silent: a chart that
    quietly falls back to an emptyDir produces exports that look fine inside
    the pod and are unreachable from anywhere else.
    """

    values = yaml.safe_load((HELM / "values.yaml").read_text(encoding="utf-8"))
    exports = values["persistence"]["exports"]
    assert exports["enabled"] is True
    assert exports["accessMode"] in {"ReadWriteOnce", "ReadWriteMany"}
    assert "existingClaim" in exports, "operators must be able to point at their own claim"

    template = (HELM / "templates" / "deployment.yaml").read_text(encoding="utf-8")
    assert "persistence.exports.enabled" in template
    assert "existingClaim" in template


# --------------------------------------------------------------------------
# Values that must agree across two files
# --------------------------------------------------------------------------


def test_the_drain_delay_fits_inside_the_termination_grace_period(
    scaled: list[dict[str, Any]],
) -> None:
    """A drain longer than the grace period is a pod killed mid-drain.

    The two numbers live in different files — one in a ConfigMap, one in a pod
    spec — which is exactly the kind of coupling nothing else would notice.
    """

    configs = {
        doc["metadata"]["name"]: PheasantConfig.model_validate(yaml.safe_load(raw))
        for doc in _by_kind(scaled, "ConfigMap")
        for raw in doc["data"].values()
    }
    for workload in _workloads(scaled):
        volumes = {volume["name"]: volume for volume in _pod_spec(workload)["volumes"]}
        config = configs[volumes["config"]["configMap"]["name"]]
        drain = config.server.api.drain_seconds
        grace = _pod_spec(workload)["terminationGracePeriodSeconds"]
        assert drain < grace, (
            f"{workload['metadata']['name']}: drain_seconds={drain} is not shorter than "
            f"terminationGracePeriodSeconds={grace}, so the pod is killed mid-drain"
        )


def test_the_indexer_is_a_single_unscaled_replica(scaled: list[dict[str, Any]]) -> None:
    """Indexing one source is lease-serialized; a second replica loses races."""

    indexer = next(
        doc for doc in _by_kind(scaled, "StatefulSet") if _container(doc)["args"][2] == "indexer"
    )
    assert indexer["spec"]["replicas"] == 1

    scaled_names = {
        doc["spec"]["scaleTargetRef"]["name"] for doc in _by_kind(scaled, "HorizontalPodAutoscaler")
    } | {doc["spec"]["scaleTargetRef"]["name"] for doc in _by_kind(scaled, "ScaledObject")}
    assert "pheasant-indexer" not in scaled_names


def test_the_indexer_pdb_permits_a_node_drain(scaled: list[dict[str, Any]]) -> None:
    """minAvailable: 1 on a single replica blocks every drain, forever.

    Safe because an evicted indexer's in-flight task is redelivered by the
    queue's visibility timeout — a drain costs latency, not work.
    """

    pdbs = {doc["metadata"]["name"]: doc for doc in _by_kind(scaled, "PodDisruptionBudget")}
    assert pdbs["pheasant-indexer"]["spec"]["minAvailable"] == 0
    assert pdbs["pheasant-api"]["spec"]["minAvailable"] == 1


def test_the_worker_autoscaler_reads_source_and_file_backlog(scaled: list[dict[str, Any]]) -> None:
    """CPU is a lagging signal here; the backlog is the leading one.

    Also asserts the metric name against the registry, so renaming the series
    without updating the manifests fails here rather than in production.
    """

    from pheasant.telemetry import metrics

    metrics.register_default_metrics("test")
    assert "pheasant_index_queue_depth" in metrics.REGISTRY.render()
    assert "pheasant_index_inflight" in metrics.REGISTRY.render()
    assert "pheasant_index_preparation_backlog" in metrics.REGISTRY.render()

    # Select by name: the log tier has its own ScaledObject, on its own
    # backlog. Taking "the first ScaledObject" would silently start asserting
    # about whichever manifest sorted first.
    keda = next(
        doc
        for doc in _by_kind(scaled, "ScaledObject")
        if doc["spec"]["scaleTargetRef"]["name"] == "pheasant-worker"
    )
    assert "pheasant_index_queue_depth" in keda["spec"]["triggers"][0]["metadata"]["query"]
    assert "pheasant_index_inflight" in keda["spec"]["triggers"][0]["metadata"]["query"]
    assert "pheasant_index_preparation_backlog" in keda["spec"]["triggers"][0]["metadata"]["query"]
    assert keda["spec"]["minReplicaCount"] == 0, "an idle fleet should not pay for workers"

    hpa = next(
        doc
        for doc in _by_kind(scaled, "HorizontalPodAutoscaler")
        if doc["spec"]["scaleTargetRef"]["name"] == "pheasant-worker"
    )
    external = [entry for entry in hpa["spec"]["metrics"] if entry["type"] == "External"]
    external_names = {entry["external"]["metric"]["name"] for entry in external}
    assert external_names == {
        "pheasant_index_queue_depth",
        "pheasant_index_inflight",
        "pheasant_index_preparation_backlog",
    }


def test_the_indexer_points_at_the_worker_service(scaled: list[dict[str, Any]]) -> None:
    """The coordinator must address workers by Service, not by pod.

    Pod IPs change on every reschedule; the Service is what lets the HPA add
    and remove replicas without anyone editing config.
    """

    fleet = next(
        PheasantConfig.model_validate(yaml.safe_load(raw))
        for doc in _by_kind(scaled, "ConfigMap")
        if doc["metadata"]["name"] == "pheasant-fleet-config"
        for raw in doc["data"].values()
    )
    urls = fleet.sync.concurrency.remote_worker_urls
    assert urls == ["http://pheasant-worker:8765"]

    services = {doc["metadata"]["name"] for doc in _by_kind(scaled, "Service")}
    assert "pheasant-worker" in services


def test_workers_actually_enable_the_preparation_endpoints(scaled: list[dict[str, Any]]) -> None:
    """Without this the routes 404 and every batch falls back to local parsing.

    Which would still *work* — that is the durability guarantee — and so would
    be invisible except as a fleet that costs money and does nothing.
    """

    worker_config = next(
        PheasantConfig.model_validate(yaml.safe_load(raw))
        for doc in _by_kind(scaled, "ConfigMap")
        if doc["metadata"]["name"] == "pheasant-worker-config"
        for raw in doc["data"].values()
    )
    assert worker_config.sync.concurrency.remote_worker_enabled is True
    assert worker_config.sync.watcher.enabled is False
    assert worker_config.sync.scheduler.enabled is False


# --------------------------------------------------------------------------
# Schema validation, when a validator is available
# --------------------------------------------------------------------------


def _kubeconform() -> str | None:
    import shutil

    return shutil.which("kubeconform") or (
        "/tmp/kubeconform" if Path("/tmp/kubeconform").exists() else None
    )


kubeconform = pytest.mark.skipif(
    _kubeconform() is None,
    reason="install kubeconform to validate manifests against Kubernetes schemas",
)


@kubeconform
@pytest.mark.parametrize("directory", [BASE, SCALED], ids=["base", "scaled"])
def test_manifests_validate_against_kubernetes_schemas(directory: Path) -> None:
    """The check I previously said belonged in CI, run here when it can be.

    The other tests in this file check the coupling to *pheasant* — that a
    role exists, that a config passes `validate_role`. This one checks the
    coupling to *Kubernetes*, which nothing in Python can: a misspelled field
    or a wrong apiVersion is a `kubectl apply` failure on someone else's
    cluster, and the offline tests would never see it.

    `-ignore-missing-schemas` because ServiceMonitor and ScaledObject are CRDs
    whose schemas ship with their operators; those four are skipped, and the
    assertion below pins that number so a *core* resource silently becoming
    unvalidatable is caught rather than absorbed.
    """

    import subprocess

    binary = _kubeconform()
    assert binary is not None
    result = subprocess.run(
        [binary, "-summary", "-strict", "-ignore-missing-schemas", *_manifest_paths(directory)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    summary = result.stdout.strip().splitlines()[-1]
    assert "Invalid: 0" in summary, summary
    assert "Errors: 0" in summary, summary
    expected_skips = "Skipped: 4" if directory == SCALED else "Skipped: 0"
    assert expected_skips in summary, f"unexpected skip count: {summary}"


def _manifest_paths(directory: Path) -> list[str]:
    return [str(path) for path in sorted(directory.glob("*.yaml"))]


# --------------------------------------------------------------------------
# Compose
# --------------------------------------------------------------------------

COMPOSE_FLEET = REPO_ROOT / "deploy" / "compose" / "docker-compose.scale.yml"
COMPOSE_CONFIG = REPO_ROOT / "deploy" / "compose"


def test_compose_manifests_live_under_the_deployment_directory() -> None:
    assert not list(REPO_ROOT.glob("docker-compose*.yml"))
    assert {
        "docker-compose.yml",
        "docker-compose.advanced.yml",
        "docker-compose.scale.yml",
        "docker-compose.fresh.yml",
    }.issubset({path.name for path in COMPOSE_CONFIG.glob("docker-compose*.yml")})


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


def test_the_three_compose_profiles_cover_small_advanced_and_fleet() -> None:
    small = PheasantConfig.model_validate(
        yaml.safe_load((COMPOSE_CONFIG / "local-small.yaml").read_text(encoding="utf-8"))
    )
    advanced = PheasantConfig.model_validate(
        yaml.safe_load((COMPOSE_CONFIG / "local-advanced.yaml").read_text(encoding="utf-8"))
    )
    fleet = PheasantConfig.model_validate(
        yaml.safe_load((COMPOSE_CONFIG / "fleet.yaml").read_text(encoding="utf-8"))
    )

    assert small.storage.backend == "sqlite"
    assert small.search.embeddings.enabled is False
    assert small.assistant.provider == "none"

    for config in (advanced, fleet):
        assert config.search.embeddings.model == "text-embedding-3-small"
        assert config.search.vector_store.provider == "lancedb"
        assert config.search.wasm_relationship_search is True
        assert config.graph.wasm_cross_source_resolution is True
        assert config.graph.memory_entity_bridging is True
        assert config.assistant.model == "gpt-5.6-luna"
        assert config.assistant.workflow == "agentic"
        assert config.search.default_mode == "hybrid"
        assert config.assistant.retrieval.expand_graph is True
        assert config.server.mcp.enabled is True

    assert advanced.assistant.retrieval.retrieval_modes == ["hybrid", "graph"]
    assert fleet.assistant.retrieval.retrieval_modes == [
        "vector",
        "graph",
        "hybrid",
    ]

    assert any(source.type.value == "memory" for source in advanced.sources)
    assert not any(source.type.value == "memory" for source in fleet.sources)
    assert fleet.ingestion.extractor.provider == "auto"
    assert fleet.search.embeddings.rate_limit_max_wait_seconds == 900.0

    assert advanced.storage.backend == "sqlite"
    assert advanced.sync.queue.enabled is False
    assert fleet.storage.backend == "postgres"
    assert fleet.sync.queue.enabled is True
    assert fleet.sync.queue.backend == "nats"


def test_the_default_compose_file_is_still_one_container() -> None:
    """Rule 7: `docker compose up` must keep needing no infrastructure."""

    compose = yaml.safe_load(
        (REPO_ROOT / "deploy" / "compose" / "docker-compose.yml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    assert set(services) == {"pheasant"}
    assert "postgres" not in services
    assert "worker" not in services
    assert "command" not in services["pheasant"], "the default container must not pass a role"
    assert services["pheasant"]["volumes"][:2] == [
        "pheasant-config:/config",
        "${PHEASANT_WORKSPACE_PATH:-pheasant-workspace}:/workspace",
    ]
    assert services["pheasant"]["environment"]["PHEASANT_WORKSPACE"] == ("/ui-managed-sources")


def test_the_advanced_compose_profile_is_one_node_and_uses_its_generated_config() -> None:
    compose = yaml.safe_load(
        (COMPOSE_CONFIG / "docker-compose.advanced.yml").read_text(encoding="utf-8")
    )
    assert set(compose["services"]) == {"pheasant"}
    service = compose["services"]["pheasant"]
    assert "postgres" not in compose["services"]
    assert "nats" not in compose["services"]
    assert "./local-advanced.yaml:/config/pheasant.yaml:ro" in service["volumes"]
    assert "OPENAI_API_KEY" in service["environment"]


# --------------------------------------------------------------------------
# The single-container install stays the default
# --------------------------------------------------------------------------


def test_the_base_install_still_needs_no_infrastructure() -> None:
    """Rule 7 at the deployment layer: `kubectl apply -f deploy/kubernetes/`
    must keep working with no Postgres, no broker and no RWX class."""

    docs = _all_documents(BASE)
    config = next(
        PheasantConfig.model_validate(yaml.safe_load(raw))
        for doc in _by_kind(docs, "ConfigMap")
        for raw in doc["data"].values()
    )
    assert config.storage.backend == "sqlite"
    assert config.sync.queue.enabled is False
    assert config.server.role == "all"
    validate_role(resolve_role(config), config)

    claims = _by_kind(docs, "PersistentVolumeClaim")
    assert all(claim["spec"]["accessModes"] == ["ReadWriteOnce"] for claim in claims)

    # And no workload in the base install passes --role: the default is `all`.
    for workload in _workloads(docs):
        assert "--role" not in (_container(workload).get("args") or [])


# --------------------------------------------------------------------------
# The log tier's CI topology
# --------------------------------------------------------------------------

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
    assert set(services) == {"postgres", "db-init", "api", "logger"}
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
