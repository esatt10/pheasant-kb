"""The suite runs on PostgreSQL in every job that runs it.

The state store is PostgreSQL and `tests/conftest.py` clones a migrated
template database per test, so a pytest job without a server exits before the
first test. These tests pin the CI shape that follows from it: every job that
runs pytest starts `deploy/postgres` (PostgreSQL 17 with pg_textsearch
preloaded) and names it in ``PHEASANT_TEST_POSTGRES_DSN``; the main quality
job runs the whole suite, unfiltered, on every pull request, with
``PHEASANT_REQUIRE_PG_TEXTSEARCH`` turning that suite's skips into failures.

History it keeps closed: Postgres once ran only in path-filtered workflows, so
whether the fleet's backend was tested depended on which files a change
touched — and `sync/locks.py`, the per-source lease, matched no filter at all.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

DSN_VARIABLE = "PHEASANT_TEST_POSTGRES_DSN"

#: Images that are a Postgres server, for a job that runs one as a service.
POSTGRES_IMAGE = re.compile(r"postgres")

#: The step that builds and starts pheasant's own PostgreSQL image
#: (``deploy/postgres``: PostgreSQL 17 with pg_textsearch preloaded, nothing
#: else). A step rather than a service, because a service starts before the
#: job's first step and so cannot run an image the job builds.
POSTGRES_ACTION = "./.github/actions/pheasant-postgres"
ACTION_FILE = REPO_ROOT / ".github" / "actions" / "pheasant-postgres" / "action.yml"

#: Set by the job that runs Postgres, so the pg_textsearch suite fails rather
#: than skips on a server that cannot load the extension.
REQUIRE_PG_TEXTSEARCH = "PHEASANT_REQUIRE_PG_TEXTSEARCH"


def _workflow(name: str) -> dict[str, Any]:
    # `on:` is the YAML 1.1 boolean True once parsed, which is a trap worth
    # absorbing here rather than in every caller.
    document = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    document["on"] = document.get("on", document.get(True))
    return document


def _pull_request_trigger(document: dict[str, Any]) -> dict[str, Any] | None:
    triggers = document.get("on") or {}
    if not isinstance(triggers, dict):
        return None
    trigger = triggers.get("pull_request")
    return trigger if isinstance(trigger, dict) else ({} if trigger is None else None)


def _builds_pheasant_postgres(job: dict[str, Any]) -> bool:
    return any(str(step.get("uses", "")) == POSTGRES_ACTION for step in job.get("steps") or [])


def _runs_postgres(job: dict[str, Any]) -> bool:
    services = job.get("services") or {}
    return _builds_pheasant_postgres(job) or any(
        POSTGRES_IMAGE.search(str(spec.get("image", ""))) for spec in services.values()
    )


def _sets_the_dsn(job: dict[str, Any]) -> bool:
    for step in job.get("steps") or []:
        if DSN_VARIABLE in (step.get("env") or {}):
            return True
    return DSN_VARIABLE in (job.get("env") or {})


def _unfiltered_postgres_jobs() -> list[tuple[str, str, dict[str, Any]]]:
    """(workflow, job name, job) for every Postgres job no path filter narrows."""

    found: list[tuple[str, str, dict[str, Any]]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        document = _workflow(path.name)
        trigger = _pull_request_trigger(document)
        if trigger is None or trigger.get("paths") or trigger.get("paths-ignore"):
            continue
        for name, job in (document.get("jobs") or {}).items():
            if _runs_postgres(job) and _sets_the_dsn(job):
                found.append((path.name, name, job))
    return found


def test_some_job_runs_postgres_on_every_pull_request() -> None:
    """The property the path filters failed to provide."""

    jobs = _unfiltered_postgres_jobs()
    assert jobs, (
        f"no workflow runs a real Postgres with {DSN_VARIABLE} set on an unfiltered "
        "pull_request trigger. Backend coverage is then a function of which files a "
        "change happens to touch, which is how sync/locks.py — the per-source lease — "
        "ended up with none."
    )


def _quality_job() -> dict[str, Any]:
    return _workflow("ci.yml")["jobs"]["python-quality"]


def _job_env(job: dict[str, Any]) -> dict[str, Any]:
    env = dict(job.get("env") or {})
    for step in job.get("steps") or []:
        env.update(step.get("env") or {})
    return env


def test_the_quality_job_runs_the_lexical_default_rather_than_skipping_it() -> None:
    """The default lexical arm needs an extension stock Postgres lacks.

    On a server without pg_textsearch its suite skips, and a skip reads like a
    pass. The job must run an image that has it and set the variable that
    turns those skips into failures.
    """

    job = _quality_job()
    assert str(_job_env(job).get(REQUIRE_PG_TEXTSEARCH)) == "1"
    assert _builds_pheasant_postgres(job), "python-quality does not run deploy/postgres"
    step = next(s for s in job["steps"] if s.get("uses") == POSTGRES_ACTION)
    assert "if" not in step, "python-quality starts Postgres only conditionally"


def test_every_job_that_runs_pytest_runs_postgres() -> None:
    """Without a server the conftest exits; a job without one tests nothing."""

    missing = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for name, job in (_workflow(path.name).get("jobs") or {}).items():
            commands = " ".join(str(step.get("run") or "") for step in job.get("steps") or [])
            if "pytest" not in commands:
                continue
            if not (_runs_postgres(job) and _sets_the_dsn(job)):
                missing.append(f"{path.name}:{name}")
    assert not missing, f"these jobs run pytest with no Postgres: {missing}"


def test_the_postgres_action_builds_the_shipped_image() -> None:
    action = yaml.safe_load(ACTION_FILE.read_text(encoding="utf-8"))
    builds = [s for s in action["runs"]["steps"] if "build-push-action" in str(s.get("uses"))]
    assert builds and builds[0]["with"]["context"] == "deploy/postgres"
    dockerfile = (REPO_ROOT / "deploy" / "postgres" / "Dockerfile").read_text(encoding="utf-8")
    assert "shared_preload_libraries = 'pg_textsearch'" in dockerfile
    assert "timescale/timescaledb" not in dockerfile


#: Everything this repository ships or runs a database from.
_SHIPPED = [
    "compose.yaml",
    "deploy/compose/*.yml",
    "deploy/compose/**/*.yml",
    "deploy/compose/**/*.yaml",
    ".github/workflows/*.yml",
    ".github/actions/**/*.yml",
    "deploy/compose/*.sh",
    "Dockerfile",
]


def test_no_shipped_path_runs_the_timescale_image() -> None:
    """TimescaleDB's image carries Timescale-licensed components pheasant
    never used; the shipped stack runs deploy/postgres instead. A manifest
    that brings it back is caught here, not by a licence review later."""

    offenders: list[str] = []
    for pattern in _SHIPPED:
        for path in REPO_ROOT.glob(pattern):
            text = path.read_text(encoding="utf-8", errors="replace")
            if re.search(r"timescale/timescaledb|timescaledb-ha", text):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"the Timescale image is back in: {sorted(set(offenders))}"


def test_the_quality_job_runs_the_whole_suite_rather_than_a_curated_list() -> None:
    """A list of tests to run is a list somebody has to remember to update."""

    job = _quality_job()
    commands = " ".join(str(step.get("run") or "") for step in job.get("steps") or [])
    assert "pytest" in commands
    narrowed = re.search(r"pytest[^\n|&]*\s(-k\s|tests/)", commands)
    assert not narrowed, f"python-quality narrows the suite ({narrowed.group(0).strip()!r})"
    trigger = _pull_request_trigger(_workflow("ci.yml"))
    assert trigger is not None and not trigger.get("paths") and not trigger.get("paths-ignore")


@pytest.mark.parametrize("workflow", ["evaluation.yml", "memory.yml", "tuning.yml"])
def test_the_filtered_workflows_keep_their_postgres_leg(workflow: str) -> None:
    """The new job is additive.

    Those three run Postgres against *their* plane's deeper scenarios — a whole
    evaluation batch, a log-tier roll, a tuning experiment — and dropping them
    on the grounds that CI now covers the backend would trade depth for
    breadth. The point was to stop breadth depending on a path filter.
    """

    document = _workflow(workflow)
    matrices = [
        job.get("strategy", {}).get("matrix", {}) for job in (document.get("jobs") or {}).values()
    ]
    assert any("postgres" in str(matrix.get("backend") or "") for matrix in matrices), (
        f"{workflow} no longer runs a postgres backend leg"
    )


# ---------------------------------------------------------------------------
# the `agent` extra: both sides of `workflow: auto`, on every pull request
# ---------------------------------------------------------------------------

_MATRIX_REF = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")


def _legs(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Each matrix leg of ``job`` as its own job, ``${{ matrix.x }}`` substituted.

    Only ``include`` legs and single-axis lists are expanded — what this repo
    writes. A job with no matrix is one leg.
    """

    matrix = (job.get("strategy") or {}).get("matrix") or {}
    entries: list[dict[str, Any]] = list(matrix.get("include") or [])
    if not entries:
        axes = {key: value for key, value in matrix.items() if isinstance(value, list)}
        entries = [{key: item} for key, values in axes.items() for item in values] or [{}]

    def substitute(value: Any, entry: dict[str, Any]) -> Any:
        if isinstance(value, str):
            return _MATRIX_REF.sub(lambda m: str(entry.get(m.group(1), "")), value)
        if isinstance(value, dict):
            return {key: substitute(inner, entry) for key, inner in value.items()}
        if isinstance(value, list):
            return [substitute(inner, entry) for inner in value]
        return value

    return [substitute(job, entry) for entry in entries]


def _installs_agent(leg: dict[str, Any]) -> bool:
    for step in leg.get("steps") or []:
        for extras in re.findall(r"pip install[^\n]*\.\[([^\]]*)\]", str(step.get("run") or "")):
            if "agent" in {extra.strip() for extra in extras.split(",")}:
                return True
    return False


def _runs_whole_suite(leg: dict[str, Any]) -> dict[str, Any] | None:
    """The step that runs every test, unnarrowed, or ``None``."""

    for step in leg.get("steps") or []:
        command = str(step.get("run") or "")
        if "pytest" in command and not re.search(r"pytest[^\n|&]*\s(-k\s|tests/)", command):
            return step
    return None


def _unfiltered_suite_legs() -> list[tuple[str, dict[str, Any]]]:
    legs: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        document = _workflow(path.name)
        trigger = _pull_request_trigger(document)
        if trigger is None or trigger.get("paths") or trigger.get("paths-ignore"):
            continue
        for name, job in (document.get("jobs") or {}).items():
            for leg in _legs(job):
                if _runs_whole_suite(leg) is not None:
                    legs.append((f"{path.name}:{name}", leg))
    return legs


def test_the_agentic_workflow_is_tested_on_every_pull_request() -> None:
    """The image installs the `agent` extra, so `auto` runs the agent loop.

    Every agentic test importorskips langgraph, and until a leg installed it
    none of them had ever run in CI — while the planner, the grader and the
    long-answer outline were exactly what a model switch had silently broken.
    The leg must also set PHEASANT_REQUIRE_AGENT, or a broken install turns
    every one of those tests into a skip and the leg into a green check.
    """

    agent_legs = [
        (where, leg)
        for where, leg in _unfiltered_suite_legs()
        if _installs_agent(leg)
        and str((_runs_whole_suite(leg) or {}).get("env", {}).get("PHEASANT_REQUIRE_AGENT")) == "1"
    ]
    assert agent_legs, (
        "no unfiltered pull-request job installs the `agent` extra and runs the whole "
        "suite with PHEASANT_REQUIRE_AGENT=1, so the workflow every image runs by "
        "default is untested"
    )


def test_the_workflow_without_the_extra_is_tested_too() -> None:
    """The other side of `auto`: a region without langgraph falls back to simple.

    Adding the extra everywhere would stop testing the fallback, which is what a
    minimal `pip install pheasant` runs.
    """

    minimal = [where for where, leg in _unfiltered_suite_legs() if not _installs_agent(leg)]
    assert minimal, "every suite run installs the `agent` extra; the fallback is untested"


def test_matrix_legs_are_expanded_with_their_own_values() -> None:
    """The two tests above read legs, so the reader is checked against the real file."""

    job = _workflow("ci.yml")["jobs"]["python-quality"]
    legs = _legs(job)
    assert len(legs) >= 2
    assert all("${{" not in str(leg.get("steps")) for leg in legs), "a matrix ref survived"
    assert sorted(_installs_agent(leg) for leg in legs) == [False, True]


def test_every_compose_file_names_the_database_image_deploy_postgres_builds() -> None:
    """One tag, in one file, named everywhere a stack starts its database.

    The tag names the image's contents (PostgreSQL minor, pg_textsearch,
    revision); `container.yml` publishes exactly deploy/postgres/TAG. A compose
    file pinned to another tag would pull an image nothing published, or one
    built from other sources.
    """

    tag = (REPO_ROOT / "deploy" / "postgres" / "TAG").read_text(encoding="utf-8").strip()
    expected = f"ghcr.io/esatt10/pheasant-postgres:{tag}"
    files = [
        "compose.yaml",
        "deploy/compose/docker-compose.scale.yml",
        "deploy/compose/ci/docker-compose.evaluation.yml",
        "deploy/compose/ci/docker-compose.log-tier.yml",
    ]
    for name in files:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        found = set(re.findall(r"ghcr\.io/esatt10/pheasant-postgres:[\w.\-]+", text))
        assert found == {expected}, f"{name} names {sorted(found)}, not {expected}"
    container = (WORKFLOWS / "container.yml").read_text(encoding="utf-8")
    assert "deploy/postgres/TAG" in container and "context: deploy/postgres" in container
