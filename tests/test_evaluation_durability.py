"""What survives the container being turned off.

A batch is minutes of work. Everything in this file is about the gap between
that fact and the two things people expect anyway: that a progress bar keeps
meaning something when the process behind it dies, and that restarting does not
start over.

Four properties, each with a failure mode that has a name:

* **Progress is a row, not a process.** A watcher in *another* process -- a
  browser talking to an API replica that did not start the run, a CLI in
  another terminal -- reads the same state. An in-memory registry answers
  neither case.
* **A dead run stops claiming to be alive.** Without reclamation a killed batch
  leaves `status='running'` forever: a spinner nobody will ever stop, and a
  scheduler that sees work apparently in flight.
* **A restart resumes.** Finished (cohort, variant) replays are checkpointed as
  they complete, so an interrupted batch redoes only what it had not done.
* **A resumed run computes the same numbers.** Reproducibility cannot depend on
  whether the container happened to restart.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import pheasant.evaluation as evaluation
from pheasant.evaluation import store as evaluation_store
from pheasant.evaluation.replay import QueryReplay, ReplayEngine, VariantReplay
from pheasant.evaluation.runner import EVALUATION_LEASE, reclaim_interrupted_runs
from pheasant.evaluation.variants import default_matrix
from tests.test_evaluation_batch import _artifact, _engine, _seed


class Killed(RuntimeError):
    """Stands in for the process going away mid-replay."""


@pytest.fixture()
def seeded(tmp_path: Path):
    engine = _engine(tmp_path)
    _seed(engine)
    try:
        yield engine
    finally:
        engine.close()


def _interrupt_after(monkeypatch: pytest.MonkeyPatch, replays: int) -> dict[str, int]:
    """Make the (replays + 1)-th cohort/variant replay die, as a kill would."""

    counter = {"n": 0}
    real = ReplayEngine.replay_variant

    def flaky(self: Any, cohort: Any, variant: Any) -> Any:
        counter["n"] += 1
        if counter["n"] > replays:
            raise Killed("container stopped")
        return real(self, cohort, variant)

    monkeypatch.setattr(ReplayEngine, "replay_variant", flaky)
    return counter


# --------------------------------------------------------------------------
# Progress is a row
# --------------------------------------------------------------------------


def test_progress_is_readable_from_another_process(seeded: Any, tmp_path: Path) -> None:
    """The whole point of putting it in `/state`.

    A second `StateStore` over the same directory is what a different container
    is: it shares the database and nothing else. If progress lived in the
    process, this would see nothing.
    """

    from pheasant.persistence.state_store import StateStore

    outcome = evaluation.run(seeded)
    other = StateStore.from_config(seeded.config)
    try:
        seen = evaluation.progress(other, "kb")
        assert seen["run_id"] == outcome.run_id
        assert seen["status"] == "completed"
        assert seen["total_units"] == seen["completed_units"] > 0
        assert seen["fraction"] == 1.0
    finally:
        other.close()


def test_progress_records_the_phase_and_the_units_as_it_goes(
    seeded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bar that only moves at the end is not a progress bar."""

    observed: list[dict[str, Any]] = []
    real = ReplayEngine.replay_variant

    def watching(self: Any, cohort: Any, variant: Any) -> Any:
        row = evaluation_store.active_run(seeded.state, "kb")
        if row:
            observed.append(row)
        return real(self, cohort, variant)

    monkeypatch.setattr(ReplayEngine, "replay_variant", watching)
    evaluation.run(seeded)

    assert observed, "no in-flight progress was visible at all"
    assert {row["status"] for row in observed} == {"running"}
    assert any(row["phase"] == "replay" for row in observed)
    # It advances rather than sitting at zero, and it never claims more units
    # than it planned.
    units = [row["completed_units"] for row in observed]
    assert units == sorted(units)
    assert units[-1] > units[0]
    assert all(row["completed_units"] <= row["total_units"] for row in observed)
    # The detail names the pair being replayed, which is what makes a stuck run
    # diagnosable rather than merely visible.
    assert any("/" in (row["phase_detail"] or "") for row in observed)


def test_a_finished_run_reports_its_terminal_status_not_a_phase(seeded: Any) -> None:
    outcome = evaluation.run(seeded)
    status = evaluation_store.run_status(seeded.state, outcome.run_id)
    assert status is not None
    assert status["status"] == "completed"
    assert status["phase"] == "completed"
    assert status["active"] is False
    assert status["finished_at"]


# --------------------------------------------------------------------------
# A dead run stops claiming to be alive
# --------------------------------------------------------------------------


def test_a_stale_run_is_reclaimed_as_interrupted(seeded: Any) -> None:
    """`interrupted`, not `failed`: it did not fail, it was cut off — and the
    distinction decides whether resuming it makes sense."""

    evaluation_store.open_run(
        seeded.state,
        run_id="run-zombie",
        kb_id="kb",
        snapshot_id="kb-x",
        started_at="2026-01-01T00:00:00Z",
        mode="current_state",
        config_digest="c",
        owner="dead-host:1",
        total_units=36,
    )
    evaluation_store.heartbeat_run(
        seeded.state,
        run_id="run-zombie",
        now="2026-01-01T00:00:10Z",
        phase="replay",
        detail="anchor/B3",
        completed_units=11,
    )
    assert evaluation_store.active_run(seeded.state, "kb")["run_id"] == "run-zombie"

    assert reclaim_interrupted_runs(seeded.state, "kb") == ["run-zombie"]
    after = evaluation_store.run_status(seeded.state, "run-zombie")
    assert after["status"] == "interrupted"
    assert after["error"]
    # How far it got survives, because that is what tells a reader whether to
    # resume or start again.
    assert after["completed_units"] == 11
    assert after["fraction"] == pytest.approx(11 / 36, abs=1e-3)
    assert evaluation_store.active_run(seeded.state, "kb") is None


def test_a_live_run_is_never_reclaimed(seeded: Any) -> None:
    """A slow batch must not be declared dead out from under itself."""

    from pheasant.evaluation.contracts import utc_now

    now = utc_now()
    evaluation_store.open_run(
        seeded.state,
        run_id="run-live",
        kb_id="kb",
        snapshot_id="kb-x",
        started_at=now,
        mode="current_state",
        config_digest="c",
        owner="me:1",
    )
    evaluation_store.heartbeat_run(
        seeded.state, run_id="run-live", now=now, phase="replay", completed_units=1
    )
    assert reclaim_interrupted_runs(seeded.state, "kb") == []
    assert evaluation_store.run_status(seeded.state, "run-live")["status"] == "running"


def test_the_heartbeat_keeps_a_long_phase_alive(
    seeded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single replay can run for minutes with nothing else writing.

    Without the beat, a healthy run looks exactly like a dead one and would be
    reclaimed mid-flight. This asserts the clock actually moves *between* phase
    transitions.
    """

    # The beat is derived from the configured stale window and clamped at both
    # ends, so making it fast means moving the *cap* and the *floor*. Patching
    # only the cap leaves `MINIMUM_RUN_HEARTBEAT_SECONDS` (1.0s) governing, and
    # this test would be asserting on a clock slower than its own sleep.
    monkeypatch.setattr(evaluation_store, "RUN_HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(evaluation_store, "MINIMUM_RUN_HEARTBEAT_SECONDS", 0.01)
    beats: list[str] = []
    real = ReplayEngine.replay_variant

    def slow(self: Any, cohort: Any, variant: Any) -> Any:
        import time

        before = evaluation_store.active_run(seeded.state, "kb")
        time.sleep(0.2)
        after = evaluation_store.active_run(seeded.state, "kb")
        if before and after and after["heartbeat_at"] != before["heartbeat_at"]:
            beats.append(after["heartbeat_at"])
        return real(self, cohort, variant)

    monkeypatch.setattr(ReplayEngine, "replay_variant", slow)
    evaluation.run(seeded)
    assert beats, "the heartbeat never fired inside a replay"


def test_a_crash_marks_the_run_failed_with_its_reason(
    seeded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything but `running` forever. A watcher cannot tell that apart from
    work still in flight."""

    _interrupt_after(monkeypatch, replays=2)
    with pytest.raises(Killed):
        evaluation.run(seeded)

    rows = seeded.state.rows("SELECT run_id, status, error FROM evaluation_runs")
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "Killed" in str(rows[0]["error"])
    assert evaluation_store.active_run(seeded.state, "kb") is None


# --------------------------------------------------------------------------
# A restart resumes
# --------------------------------------------------------------------------


def test_an_interrupted_batch_resumes_instead_of_starting_over(
    seeded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property the whole checkpoint table exists for."""

    _interrupt_after(monkeypatch, replays=3)
    with pytest.raises(Killed):
        evaluation.run(seeded)
    assert seeded.state.rows("SELECT COUNT(*) AS c FROM evaluation_replays")[0]["c"] == 3

    monkeypatch.undo()
    second = _interrupt_after(monkeypatch, replays=10_000)
    outcome = evaluation.run(seeded)

    assert outcome.status == "completed"
    assert outcome.attempts == 2
    assert outcome.resumed_replays == 3
    # Six cohorts x six variants is 36 pairs; three were already done, so the
    # second attempt replayed exactly the 33 that were not.
    assert second["n"] == 33
    # One run, not two: the id is content-addressed, so a restart re-derives
    # the same row rather than forking the history.
    assert seeded.state.rows("SELECT COUNT(*) AS c FROM evaluation_runs")[0]["c"] == 1


def test_checkpoints_are_cleared_only_once_the_report_is_committed(
    seeded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """They are the recovery path for a run that did not get that far.

    Dropping them earlier would mean a crash *during* persistence had to replay
    everything again — which is precisely the case the checkpoints exist for.
    """

    _interrupt_after(monkeypatch, replays=2)
    with pytest.raises(Killed):
        evaluation.run(seeded)
    assert seeded.state.rows("SELECT COUNT(*) AS c FROM evaluation_replays")[0]["c"] == 2

    monkeypatch.undo()
    evaluation.run(seeded)
    assert seeded.state.rows("SELECT COUNT(*) AS c FROM evaluation_replays")[0]["c"] == 0


def test_a_resumed_run_computes_the_same_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_database: Callable[[], str]
) -> None:
    """Reproducibility cannot depend on whether the container restarted.

    Two regions, byte-identical seeds. One runs straight through; the other is
    killed in the middle and resumed. Their health vectors must match.
    """

    straight = _engine(tmp_path / "a")
    _seed(straight)
    try:
        clean = evaluation.run(straight).report["health_vector"]
    finally:
        straight.close()

    # A region of its own: a run id addresses the state it measures, so on one
    # database the second region would resume the first one's finished run.
    with pytest.MonkeyPatch.context() as scoped:
        scoped.setenv("PHEASANT_DATABASE_URL", fresh_database())
        interrupted = _engine(tmp_path / "b")
    _seed(interrupted)
    try:
        _interrupt_after(monkeypatch, replays=5)
        with pytest.raises(Killed):
            evaluation.run(interrupted)
        monkeypatch.undo()
        resumed_outcome = evaluation.run(interrupted)
    finally:
        interrupted.close()

    assert resumed_outcome.resumed_replays == 5
    assert {k: v["value"] for k, v in resumed_outcome.report["health_vector"].items()} == {
        k: v["value"] for k, v in clean.items()
    }


def test_a_checkpoint_round_trips_everything_the_metrics_read() -> None:
    """A checkpoint that dropped a field would make the answer depend on
    whether the container happened to restart."""

    variant = next(v for v in default_matrix() if v.variant_id == "B5")
    original = VariantReplay(variant=variant, cohort_id="cohort-1")
    original.results["q1"] = QueryReplay(
        query_id="q1",
        variant_id="B5",
        text="where is invoice retry configured",
        ranked_ids=["a", "b", "c"],
        ranked_paths=["a.md", "b.md", "c.md"],
        memory_record_ids=["mem-1"],
        scores={"a": 0.031, "b": 0.02},
        contributing_arms={"a": ["text", "vector"], "b": ["text"]},
        result_count=3,
        duration_ms=12.5,
        steering_applied={"aliases": {"invoice retry": ["InvoiceRetryPolicy"]}},
    )
    original.results["q2"] = QueryReplay(
        query_id="q2", variant_id="B5", text="broken", failed="RuntimeError: boom"
    )
    original.failures["q2"] = "RuntimeError: boom"

    restored = VariantReplay.from_dict(variant, json.loads(json.dumps(original.as_dict())))

    assert restored.cohort_id == original.cohort_id
    assert restored.failures == original.failures
    assert restored.completed_ids == original.completed_ids
    assert restored.latencies() == original.latencies()
    for query_id, source in original.results.items():
        target = restored.results[query_id]
        assert target.ranked_ids == source.ranked_ids
        assert target.ranked_paths == source.ranked_paths
        assert target.memory_record_ids == source.memory_record_ids
        assert target.scores == source.scores
        assert target.contributing_arms == source.contributing_arms
        assert target.rank_of("a") == source.rank_of("a")
        assert target.failed == source.failed


def test_a_completed_run_is_never_rewritten(seeded: Any) -> None:
    """History. Re-running it would destroy the report it published."""

    first = evaluation.run(seeded)
    claim = evaluation_store.open_run(
        seeded.state,
        run_id=first.run_id,
        kb_id="kb",
        snapshot_id=first.snapshot_id,
        started_at="2026-05-05T00:00:00Z",
        mode="current_state",
        config_digest="c",
    )
    assert claim["claimed"] is False
    assert claim["resumed"] is False
    assert claim["previous_status"] == "completed"
    assert evaluation_store.run_status(seeded.state, first.run_id)["status"] == "completed"


def test_a_first_attempt_does_not_claim_to_be_a_recovery(seeded: Any) -> None:
    """An insert that reported success either way could not tell them apart."""

    claim = evaluation_store.open_run(
        seeded.state,
        run_id="run-new",
        kb_id="kb",
        snapshot_id="kb-x",
        started_at="2026-01-01T00:00:00Z",
        mode="current_state",
        config_digest="c",
    )
    assert claim == {"claimed": True, "resumed": False, "attempts": 1, "previous_status": ""}


# --------------------------------------------------------------------------
# The surfaces a watcher actually uses
# --------------------------------------------------------------------------


def test_the_http_status_endpoint_reports_a_reclaimed_run(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app
    from tests.test_evaluation_batch import _write_config

    config, path = _write_config(tmp_path)
    app = create_app(config, config_path=str(path))
    engine = app.state.engine
    engine.sync_source("docs", "full")
    _seed(engine)

    with TestClient(app) as client:
        empty = client.get("/evaluation/status").json()
        assert empty["status"] == "none"
        assert empty["enabled"] is True

        evaluation_store.open_run(
            engine.state,
            run_id="run-zombie",
            kb_id="kb",
            snapshot_id="kb-x",
            started_at="2026-01-01T00:00:00Z",
            mode="current_state",
            config_digest="c",
            owner="dead:1",
            total_units=12,
        )
        evaluation_store.heartbeat_run(
            engine.state,
            run_id="run-zombie",
            now="2026-01-01T00:00:05Z",
            phase="replay",
            completed_units=4,
        )
        running = client.get("/evaluation/status").json()
        assert running["status"] == "running"
        assert running["fraction"] == pytest.approx(4 / 12, abs=1e-3)

        reclaim_interrupted_runs(engine.state, "kb")
        after = client.get("/evaluation/status").json()
        assert after["status"] == "interrupted"
        assert after["completed_units"] == 4
        assert after["error"]


def test_a_reclaimed_run_is_closed_out_at_api_startup(tmp_path: Path) -> None:
    """`--role api` never runs the scheduler beat, and the API is exactly where
    somebody is watching a bar that would otherwise spin forever."""

    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app
    from tests.test_evaluation_batch import _write_config

    config, path = _write_config(tmp_path)
    first = create_app(config, config_path=str(path))
    engine = first.state.engine
    engine.sync_source("docs", "full")
    evaluation_store.open_run(
        engine.state,
        run_id="run-zombie",
        kb_id="kb",
        snapshot_id="kb-x",
        started_at="2026-01-01T00:00:00Z",
        mode="current_state",
        config_digest="c",
        owner="stopped-container:1",
    )
    engine.close()

    restarted = create_app(config, config_path=str(path))
    with TestClient(restarted) as client:
        assert client.get("/evaluation/status").json()["status"] == "interrupted"
    restarted.state.engine.close()


def test_the_mcp_facade_starts_and_watches_a_run(tmp_path: Path) -> None:
    from pheasant.mcp_server.tools import PheasantTools
    from tests.test_evaluation_batch import _write_config

    config, path = _write_config(tmp_path)
    tools = PheasantTools(config)
    tools.engine.sync_source("docs", "full")
    _seed(tools.engine)
    try:
        idle = tools.get_evaluation_status("kb")
        assert idle["status"] == "none"

        outcome = evaluation.run(tools.engine)
        watched = tools.get_evaluation_status("kb")
        assert watched["run_id"] == outcome.run_id
        assert watched["status"] == "completed"
        assert watched["enabled"] is True

        # Starting again is a no-op rather than a second run: the batch is
        # already recorded, and the id is content-addressed.
        assert tools.get_evaluation_status("kb", outcome.run_id)["run_id"] == outcome.run_id
    finally:
        tools.state.close()


def test_the_cli_status_line_survives_a_run_it_did_not_start(
    seeded: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    from pheasant.cli import _print_evaluation_status

    evaluation_store.open_run(
        seeded.state,
        run_id="run-elsewhere",
        kb_id="kb",
        snapshot_id="kb-x",
        started_at="2026-01-01T00:00:00Z",
        mode="current_state",
        config_digest="c",
        owner="other-container:7",
        total_units=36,
    )
    evaluation_store.heartbeat_run(
        seeded.state,
        run_id="run-elsewhere",
        now="2026-01-01T00:00:05Z",
        phase="replay",
        detail="anchor/B2",
        completed_units=9,
    )
    _print_evaluation_status(evaluation.progress(seeded.state, "kb"))
    printed = capsys.readouterr().out
    assert "running" in printed
    assert "25%" in printed
    assert "anchor/B2" in printed
    assert "other-container:7" in printed


def test_the_status_surface_says_nothing_has_run_rather_than_guessing(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        payload = evaluation.progress(engine.state, "kb")
        assert payload["status"] == "none"
        assert "no evaluation batch" in payload["detail"].lower()
    finally:
        engine.close()


def test_an_unknown_run_id_is_reported_as_unknown(seeded: Any) -> None:
    payload = evaluation.progress(seeded.state, "kb", "run-does-not-exist")
    assert payload == {"run_id": "run-does-not-exist", "status": "unknown"}


def test_evidence_recorded_before_a_crash_survives_it(
    seeded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proof is written by traffic, not by runs, so a failed batch must not
    cost the evidence people recorded."""

    before = seeded.state.rows("SELECT COUNT(*) AS c FROM evaluation_proofs")[0]["c"]
    evaluation.record_evidence(
        seeded.state,
        seeded.config,
        query="a question asked during the run",
        target_id=_artifact(seeded, "runbook"),
        event_type="selected",
        interaction_id="mid-run",
    )
    _interrupt_after(monkeypatch, replays=1)
    with pytest.raises(Killed):
        evaluation.run(seeded)
    after = seeded.state.rows("SELECT COUNT(*) AS c FROM evaluation_proofs")[0]["c"]
    assert after == before + 1


# --------------------------------------------------------------------------
# The container topology the CI durability job drives
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_DIR = REPO_ROOT / "deploy" / "compose" / "ci"


def test_every_ci_compose_config_is_valid_for_the_role_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`validate_role` over every CI topology, the way `test_fleet_manifests`
    already does for the Kubernetes manifests.

    That check existed for one set of deployment artifacts and not the other,
    and the gap cost a red build: the evaluation topology ran `--role api`
    without `sync.queue.enabled`, and an api replica refuses to start without a
    queue because it *publishes* index work rather than running it. The failure
    was correct and immediate — the container exited 1 on boot — but it
    surfaced in a container job rather than here, where it is a second to find.

    Written over every compose file and every service rather than the one that
    broke, because the next topology added will have the same shape.
    """

    import yaml

    from pheasant.config.schema import PheasantConfig
    from pheasant.deployment.roles import resolve_role, validate_role

    topologies = sorted(CI_DIR.glob("docker-compose.*.yml"))
    assert topologies, "no CI compose topologies found"

    checked = 0
    for topology in topologies:
        compose = yaml.safe_load(topology.read_text(encoding="utf-8"))
        for name, service in (compose.get("services") or {}).items():
            command = service.get("command") or []
            if not (isinstance(command, list) and "--role" in command):
                # db-init and the runner have their own entrypoints; they take
                # no role and there is nothing to validate.
                continue
            role = command[command.index("--role") + 1]
            mounted = [
                str(volume).split(":", 1)[0]
                for volume in (service.get("volumes") or [])
                if isinstance(volume, str) and ":/config/pheasant.yaml" in str(volume)
            ]
            assert mounted, f"{topology.name}:{name} runs --role {role} with no config mounted"
            config_path = (CI_DIR / mounted[0]).resolve()
            assert config_path.exists(), f"{topology.name}:{name} mounts a missing {config_path}"

            config = PheasantConfig.model_validate(
                yaml.safe_load(config_path.read_text(encoding="utf-8"))
            )
            # The same call the process makes at startup, against the same
            # environment the service is given -- a serving role refuses an
            # unauthenticated non-loopback bind, and validating against an
            # empty environment would miss a topology that forgot the token
            # just as surely as one that forgot the queue.
            with monkeypatch.context() as patched:
                for key, value in (service.get("environment") or {}).items():
                    patched.setenv(str(key), str(value))
                validate_role(resolve_role(config, role), config)
            checked += 1

    assert checked >= 2, f"expected several roled services across the topologies, saw {checked}"


def test_the_ci_evaluation_config_is_valid_for_the_role_that_uses_it() -> None:
    """A malformed CI config fails the durability job for a reason that is not
    the feature, which is the worst kind of red build."""

    import yaml

    from pheasant.config.schema import PheasantConfig

    config = PheasantConfig.model_validate(
        yaml.safe_load((CI_DIR / "pheasant.evaluation.yaml").read_text(encoding="utf-8"))
    )
    assert config.evaluation.enabled is True
    # Driven explicitly: a scheduled run firing underneath the smoke script
    # would make a failure ambiguous.
    assert config.evaluation.on_material_snapshot is False
    assert config.sync.scheduler.enabled is False
    # Short enough that the job does not wait out the production window, and
    # long enough that a healthy replay is not reclaimed mid-flight.
    assert 5 <= config.evaluation.run_stale_seconds <= 60
    # Promotion stays off: the job proves durability, not that a candidate
    # should reach production ranking.
    assert config.evaluation.promotion.enabled is False


def test_the_smoke_script_waits_out_the_window_it_configures() -> None:
    """Reclamation is deliberately not instant. A script that restarted the api
    immediately would poll for a state nothing had produced yet — and would
    fail for a reason that is not a bug.
    """

    import yaml

    from pheasant.config.schema import PheasantConfig

    config = PheasantConfig.model_validate(
        yaml.safe_load((CI_DIR / "pheasant.evaluation.yaml").read_text(encoding="utf-8"))
    )
    script = (CI_DIR / "evaluation-smoke.sh").read_text(encoding="utf-8")
    waits = [int(match) for match in re.findall(r"^sleep (\d+)$", script, re.MULTILINE)]
    assert waits, "the smoke script never waits out the heartbeat window"
    assert max(waits) > config.evaluation.run_stale_seconds, (
        f"the script's longest wait ({max(waits)}s) does not exceed the configured "
        f"run_stale_seconds ({config.evaluation.run_stale_seconds}s), so nothing will "
        "ever be reclaimable when it checks"
    )


def test_the_container_runner_asserts_what_the_smoke_script_parses() -> None:
    """The script reads one JSON line for its resume assertions. If the runner
    stopped printing a field, the job would pass while proving nothing."""

    runner = (CI_DIR / "scripts" / "run_evaluation.py").read_text(encoding="utf-8")
    script = (CI_DIR / "evaluation-smoke.sh").read_text(encoding="utf-8")
    for field in ("run_id", "status", "attempts", "resumed_replays", "gates_passed"):
        assert f'"{field}"' in runner, f"the runner no longer prints {field}"
    for field in ("status", "attempts", "resumed_replays"):
        assert f'outcome["{field}"]' in script, f"the script no longer asserts on {field}"


# --------------------------------------------------------------------------
# The lease a killed process never released
# --------------------------------------------------------------------------
#
# Every in-process test above kills a batch by *raising*, which unwinds
# `EvaluationLease.__exit__` and releases the lease on the way out. A stopped
# container does not unwind anything, so it leaves the lease row behind — and
# the run row and that row then age out on two different clocks:
# `evaluation.run_stale_seconds` against `locks.SOURCE_STALE_SECONDS` (45s).
#
# The window only opens when the first is set *below* the second, because then
# the run is declared dead while its lease still claims a live holder. The
# shipped default (90s) is above it and never sees this; the CI region sets 20s
# so its smoke test does not have to wait out 90 seconds, and hit it on the
# first real container run. So these tests pin `run_stale_seconds` rather than
# taking the default, and time their rows relative to now — an ancient
# timestamp is stale by every window at once and would prove nothing.


#: Lower than `SOURCE_STALE_SECONDS`, as the CI region sets it. This is the
#: configuration in which the two clocks disagree.
RUN_STALE = 20.0


def _seconds_ago(seconds: float) -> str:
    from datetime import timedelta

    from pheasant.evaluation.contracts import UTC, datetime

    return (
        (datetime.now(UTC) - timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _abandoned_lease(state: Any, *, heartbeat_at: str, owner: str = "dead-host:1") -> None:
    """The row a killed container leaves: held, never released, not beating."""

    state.conn.execute(
        "INSERT INTO source_leases(source_id, owner, acquired_at, heartbeat_at) "
        "VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
        "owner=excluded.owner, acquired_at=excluded.acquired_at, "
        "heartbeat_at=excluded.heartbeat_at",
        (EVALUATION_LEASE, owner, heartbeat_at, heartbeat_at),
    )
    state.conn.commit()


def _lease_owner(state: Any) -> str | None:
    rows = state.rows("SELECT owner FROM source_leases WHERE source_id=?", (EVALUATION_LEASE,))
    return str(rows[0]["owner"]) if rows else None


def _kill_a_batch(state: Any, *, ago: float, run_id: str = "run-killed") -> None:
    """A run and its lease, both last beating `ago` seconds back.

    One process wrote both and then stopped, so they stop together — which is
    the whole reason the two staleness windows have to agree about it.
    """

    stopped = _seconds_ago(ago)
    evaluation_store.open_run(
        state,
        run_id=run_id,
        kb_id="kb",
        snapshot_id="kb-x",
        started_at=_seconds_ago(ago + 30),
        mode="current_state",
        config_digest="c",
        owner="dead-host:1",
        total_units=36,
    )
    evaluation_store.heartbeat_run(
        state,
        run_id=run_id,
        now=stopped,
        phase="replay",
        detail="anchor/B3",
        completed_units=7,
    )
    _abandoned_lease(state, heartbeat_at=stopped)


def test_reclaiming_a_run_frees_the_lease_its_killed_process_never_released(
    seeded: Any,
) -> None:
    """The container-stopped case, which the raising kills above cannot reach.

    Reclaiming the run row and freeing the lease are one conclusion — the
    process is gone — and leaving them on separate clocks is what let the
    region advertise a resume it would then decline.
    """

    # 25s dead: past `run_stale_seconds` (20s), still inside the lease's own
    # 45s window. This is the gap, and it is where CI stopped.
    _kill_a_batch(seeded.state, ago=25.0)

    reclaimed = reclaim_interrupted_runs(seeded.state, "kb", stale_after_seconds=RUN_STALE)
    assert reclaimed == ["run-killed"]
    assert evaluation_store.run_status(seeded.state, "run-killed")["status"] == "interrupted"
    assert _lease_owner(seeded.state) is None, (
        "the run was advertised as resumable while its lease still said someone was running it"
    )


def test_the_resume_a_reclaim_advertises_actually_runs(seeded: Any) -> None:
    """The defect end to end: `interrupted` has to mean the next attempt runs.

    Asserted through `evaluation.run` rather than the lease row, because the
    symptom was not a stuck row — it was a batch reporting `skipped` with no
    gates, which reads as success to everything downstream of it.
    """

    _kill_a_batch(seeded.state, ago=25.0)
    reclaim_interrupted_runs(seeded.state, "kb", stale_after_seconds=RUN_STALE)

    outcome = evaluation.run(seeded)
    assert outcome.status == "completed", outcome.skipped_reason
    assert outcome.skipped_reason == ""


def test_a_lease_that_is_still_beating_survives_a_reclaim(seeded: Any) -> None:
    """The other half: a successor that took the lease legitimately keeps it.

    The staleness test lives in the DELETE rather than in a read before it, so
    a live holder survives whatever the reclaiming process believed about the
    holder it knew about.
    """

    from pheasant.evaluation.contracts import utc_now

    _kill_a_batch(seeded.state, ago=25.0)
    # A successor claimed the lease between the kill and this reclaim.
    _abandoned_lease(seeded.state, heartbeat_at=utc_now(), owner="successor:2")

    reclaimed = reclaim_interrupted_runs(seeded.state, "kb", stale_after_seconds=RUN_STALE)
    assert reclaimed == ["run-killed"]
    assert _lease_owner(seeded.state) == "successor:2"


def test_a_held_lease_still_makes_a_second_run_decline(seeded: Any) -> None:
    """The exclusion itself is unchanged: N replicas still produce one run."""

    from pheasant.evaluation.contracts import utc_now

    _abandoned_lease(seeded.state, heartbeat_at=utc_now(), owner="other-replica:9")
    outcome = evaluation.run(seeded)
    assert outcome.status == "skipped"
    assert "lease" in outcome.skipped_reason
    assert _lease_owner(seeded.state) == "other-replica:9"


def test_a_skipped_run_never_reports_that_its_gates_passed(seeded: Any) -> None:
    """`all([])` is True, and a run that evaluated no gates is not a run that
    passed them. The API publishes this field beside `status`, and it is the
    same rule as `insufficient_evidence` rather than `0.0`, one level up."""

    from pheasant.evaluation.contracts import utc_now

    _abandoned_lease(seeded.state, heartbeat_at=utc_now(), owner="other-replica:9")
    held = evaluation.run(seeded)
    assert held.status == "skipped"
    assert held.gates == []
    assert held.gates_passed is False

    seeded.config.evaluation.enabled = False
    disabled = evaluation.run(seeded)
    assert disabled.status == "skipped"
    assert disabled.gates_passed is False


def test_a_completed_run_still_reports_its_gates(seeded: Any) -> None:
    """Guards the fix: requiring a non-empty gate list must not make a real
    run look failed."""

    outcome = evaluation.run(seeded)
    assert outcome.status == "completed"
    assert outcome.gates, "a completed run published no gates at all"
    assert outcome.gates_passed is all(gate.passed for gate in outcome.gates)
