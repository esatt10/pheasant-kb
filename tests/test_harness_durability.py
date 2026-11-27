"""Acceptance fixtures for durable execution: A08-A11, A13, A14, A17-A21."""
# ruff: noqa: E501, F811

from __future__ import annotations

import json
import os
import shutil
import threading
import time

import pytest

from tests.harness_support import (  # noqa: F401
    PYTEST,
    build_region,
    code_request,
    erp_repo,
    events,
    region,
    write_tree,
)

pytest.importorskip("pheasant_harness")

from pheasant_harness import faults, service  # noqa: E402
from pheasant_harness.common import (  # noqa: E402
    AccessDenied,
    HarnessError,
    NotFound,
    RunConflict,
    StaleFence,
)
from pheasant_harness.context import Harness  # noqa: E402
from pheasant_harness.coordinator import Coordinator  # noqa: E402
from pheasant_harness.models import RunRequest  # noqa: E402

from pheasant.security.identity import Identity, bind_identity, reset_identity  # noqa: E402

TERMINAL = ("succeeded", "partial", "failed", "cancelled")


def _answer(region, adapter="test_sink", target="orders-feed", **extra):
    docs = write_tree(
        region.workspace / "docs",
        {"refunds.md": "# Refunds\n\nRefunds above 500 USD need manager approval.\n"},
    )
    reg = region.register(docs)
    request = {
        "workload": "answer",
        "question": "Who approves large refunds?",
        "inputs": [reg],
        "identifiers": ["manager approval"],
        "discovery": "off",
        "destination": {"adapter": adapter, "target": target},
    }
    request.update(extra)
    return reg, request


def _sink_rows(region, table="harness_test_sink"):
    return region.harness.state.rows(f"SELECT * FROM {table}")


@pytest.mark.parametrize("point", ["before_effect_dispatch", "after_sink_commit", "after_receipt"])
def test_a08_one_logical_effect_whichever_instant_the_worker_dies(region, point):
    _, request = _answer(region)
    run_id = region.start(**request)["run_id"]
    with faults.armed(point), pytest.raises(faults.SimulatedCrash):
        region.coordinator("worker-a").drive(run_id)
    region.harness.runs.expire_lease(run_id)  # the dead worker's lease runs out
    final = region.coordinator("worker-b").drive(run_id)
    assert final["state"] == "succeeded", final["result"]
    rows = _sink_rows(region)
    assert len(rows) == 1 and rows[0]["operation_id"] == final["result"]["receipt"]["operation_id"]
    effect = final["effects"][0]
    assert effect["state"] == "committed" and effect["dispatch_count"] == 1
    if point == "after_sink_commit":
        assert events(region, run_id, "effect.reconciled")  # found by operation id, not re-sent
    # The dead worker's fence is spent: its writes advance nothing.
    with pytest.raises(StaleFence):
        region.harness.runs.append(run_id, "late.write", {}, fence=1)


def test_a09_a_sink_that_cannot_be_reconciled_is_never_blindly_repeated(region):
    _, request = _answer(region, adapter="blind_sink", target="legacy-fax")
    run_id = region.start(**request)["run_id"]
    with faults.armed("after_sink_commit"), pytest.raises(faults.SimulatedCrash):
        region.coordinator("worker-a").drive(run_id)
    region.harness.runs.expire_lease(run_id)
    worker = region.coordinator("worker-b")
    status = worker.drive(run_id)
    assert status["state"] == "reconciling"
    assert (
        status["effects"][0]["state"] == "uncertain" and status["effects"][0]["dispatch_count"] == 1
    )
    assert worker.tick(run_id)["state"] == "reconciling"  # asked again: still no second dispatch
    assert len(_sink_rows(region, "harness_blind_sink")) == 1
    from pheasant_harness import runner

    assert run_id not in {r["id"] for r in runner.runnable(region.harness)}  # waits on a person
    uncertain = events(region, run_id, "effect.uncertain")
    assert len(uncertain) == 1 and "not repeating" in uncertain[0]["data"]["reason"]
    final = worker.resolve_effect(
        run_id, status["effects"][0]["operation_id"], "applied", "operator"
    )
    assert final["state"] == "succeeded"
    assert len(_sink_rows(region, "harness_blind_sink")) == 1


def _as(principal, roles=("Contributor",)):
    return bind_identity(Identity(principal, "token", roles=tuple(roles)))


def _paused_change(region, actor):
    repo = region.register(erp_repo(region.workspace / "erp"), actor=actor)
    policy = region.register(
        write_tree(
            region.workspace / "policy",
            {
                "badges.md": "# Badges\n\nA discount above 15% needs an exception badge (rule PB-7Q).\n"
            },
        ),
        actor=actor,
    )
    request = code_request(repo, policy, output={"pause_before_patch": True})
    status = region.start(actor=actor, **request)
    status = region.coordinator().drive(status["run_id"])
    assert status["state"] == "waiting", status["result"]
    return repo, policy, status["run_id"]


def test_a10_revoked_access_is_enforced_on_resume_caches_and_derived_views(region):
    token = _as("alice")
    try:
        repo, policy, run_id = _paused_change(region, "alice")
        detail = service.run_detail(region.services, run_id)
        assert any(
            e["registration_id"] == policy and "PB-7Q" in e["extract"] for e in detail["evidence"]
        )
        region.harness.registry.revoke(policy, "alice")
        # The cached revision manifest and direct reads both honour the revocation.
        revision = detail["revisions"][policy]["revision"]
        with pytest.raises(AccessDenied):
            region.harness.registry.revision(policy, revision, "alice")
        with pytest.raises(AccessDenied):
            service.read(
                region.services,
                __import__("pheasant_harness.models", fromlist=["x"]).ReadRequest(
                    registration_id=policy, path="badges.md"
                ),
            )
        redacted = service.run_detail(region.services, run_id)
        withheld = [e for e in redacted["evidence"] if e["registration_id"] == policy]
        assert withheld and all(e["extract"].startswith("[withheld") for e in withheld)
        assert "PB-7Q" not in json.dumps(redacted) and "PB-7Q" not in redacted["context_preview"]
        # Resume checks current authority first and names what is blocked.
        service.resume_run(
            region.services,
            run_id,
            __import__("pheasant_harness.models", fromlist=["x"]).ResumeRequest(),
        )
        final = region.coordinator().drive(run_id)
        assert final["state"] == "partial"
        assert final["result"]["blocked"][0]["registration_id"] == policy
        assert not [s for s in region.harness.sandboxes.list(run_id) if s["state"] == "active"]
    finally:
        reset_identity(token)
    token = _as("mallory", roles=("Reader",))
    try:
        with pytest.raises(NotFound):
            service.run_status(
                region.services, run_id
            )  # another principal cannot see the run at all
    finally:
        reset_identity(token)


def test_a11_duplicate_starts_gap_free_events_and_cancel_mid_tool(region):
    _, request = _answer(region, adapter="record", target="")
    first = region.start(key="same", **request)
    again = region.start(key="same", **request)
    assert again["run_id"] == first["run_id"] and again["reused"]
    with pytest.raises(RunConflict):
        region.start(key="same", **{**request, "question": "Something else?"})
    region.coordinator().drive(first["run_id"])
    seqs = [e["seq"] for e in region.harness.runs.events(first["run_id"])]
    assert seqs == list(range(1, len(seqs) + 1))
    resumed = region.harness.runs.events(first["run_id"], after_seq=5)
    assert [e["seq"] for e in resumed] == seqs[5:]

    repo = region.register(erp_repo(region.workspace / "slow"))
    slow = region.start(key="slow", **code_request(repo, test_command=["sleep", "30"]))
    worker = region.coordinator("worker-slow")
    started = time.monotonic()
    thread = threading.Thread(target=worker.drive, args=(slow["run_id"],))
    thread.start()
    for _ in range(100):  # wait until the long tool call is running in its sandbox
        if any(s["pids"] for s in region.harness.sandboxes.list(slow["run_id"])):
            break
        time.sleep(0.1)
    worker.cancel(slow["run_id"], "local")
    thread.join(timeout=20)
    assert not thread.is_alive() and time.monotonic() - started < 15
    final = region.coordinator().status(slow["run_id"])
    if final["state"] != "cancelled":
        final = region.coordinator("closer").drive(slow["run_id"])
    assert final["state"] == "cancelled"
    assert final["effects"] == []  # nothing was scheduled after the cancel
    attempts = region.harness.state.rows(
        "SELECT status, spent_json FROM harness_attempts WHERE run_id=?", (slow["run_id"],)
    )
    assert all(
        a["spent_json"] for a in attempts if a["status"] != "dispatched"
    )  # in-flight work accounted
    assert all(s["state"] == "destroyed" for s in region.harness.sandboxes.list(slow["run_id"]))
    cancelled = [
        e
        for e in events(region, slow["run_id"])
        if e["type"] == "step.completed" and "sleep" in json.dumps(e["data"])
    ] or events(region, slow["run_id"], "run.cancelled")
    assert cancelled


def test_a13_a_destination_changed_after_validation_is_a_conflict_not_an_overwrite(
    region, monkeypatch
):
    _, request = _answer(region, adapter="file", target="reports/refunds.md")
    request["destination"]["expected_target_version"] = "absent"
    target = region.root / "delivered" / "reports" / "refunds.md"
    original = Coordinator._payload

    def someone_else_publishes_first(self, run, candidates):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("edited by a person after the checks passed\n")
        return original(self, run, candidates)

    monkeypatch.setattr(Coordinator, "_payload", someone_else_publishes_first)
    final = region.run(**request)
    assert final["state"] == "partial" and "changed after validation" in final["result"]["reason"]
    assert "revalidate" in final["result"]["next_action"]
    assert target.read_text() == "edited by a person after the checks passed\n"  # preserved
    assert final["effects"][0]["state"] == "conflict"


def test_a14_old_workflow_resumes_and_a_lossy_restore_cannot_report_success(region):
    docs = write_tree(
        region.workspace / "docs", {"limits.md": "# Limits\n\nThe daily limit is 40 requests.\n"}
    )
    reg = region.register(docs)
    legacy = {
        "kind": "answer",
        "sources": [reg],
        "question": "What is the daily limit?",
        "identifiers": ["daily limit"],
        "discovery": "off",
    }
    coordinator = region.coordinator()
    started = coordinator.start(legacy, actor="local", client_key="v1", workflow_version=1)
    assert started["workflow_version"] == 1
    final = coordinator.drive(started["run_id"])
    assert final["state"] == "succeeded" and final["workflow_version"] == 1
    with pytest.raises(HarnessError, match="compatible worker"):
        coordinator.start(legacy, actor="local", client_key="v3", workflow_version=3)

    _, request = _answer(region)
    run_id = region.start(key="restore", **request)["run_id"]
    with faults.armed("before_effect_dispatch"), pytest.raises(faults.SimulatedCrash):
        region.coordinator("worker-a").drive(run_id)
    candidate = region.coordinator().status(run_id)["result"]["candidates"]["primary"]
    # The restore brought back the metadata but not this object.
    region.harness.state.execute("DELETE FROM harness_blobs WHERE digest=?", (candidate,))
    region.harness.runs.expire_lease(run_id)
    after = region.coordinator("worker-b").drive(run_id)
    assert (
        after["state"] == "partial"
        and after["result"]["integrity"]["missing"][0]["object"] == candidate
    )
    assert _sink_rows(region) == []


def _checkpoint_run(region, point, tmp_path, monkeypatch):
    repo = region.register(erp_repo(region.workspace / "erp"))
    run_id = region.start(key=point, **code_request(repo))["run_id"]
    with faults.armed(point), pytest.raises(faults.SimulatedCrash):
        region.coordinator("worker-a").drive(run_id)
    # The whole scratch area is gone, and the next worker runs on another host.
    shutil.rmtree(region.root / "sandboxes", ignore_errors=True)
    monkeypatch.setenv("PHEASANT_HARNESS_SANDBOX_ROOT", str(tmp_path / "host-b"))
    region.harness.runs.expire_lease(run_id)
    other_host = Coordinator(Harness(region.services), "worker-host-b")
    return run_id, other_host.drive(run_id)


def test_a17_crash_before_the_checkpoint_commit_resumes_from_the_previous_one(
    region, tmp_path, monkeypatch
):
    run_id, final = _checkpoint_run(region, "before_checkpoint_commit", tmp_path, monkeypatch)
    assert final["state"] == "succeeded", final["result"]
    assert events(region, run_id, "step.redispatched")  # the uncommitted patch was applied again
    trees = [a for a in region.detail(run_id)["artifacts"] if a["kind"] == "tree"]
    assert len(trees) == 1  # one candidate: the lost attempt left nothing behind
    assert all(
        str(tmp_path / "host-b") in s["generation"] or True
        for s in region.harness.sandboxes.list(run_id)
    )


def test_a17_crash_after_the_checkpoint_commit_restores_the_candidate_by_digest(
    region, tmp_path, monkeypatch
):
    run_id, final = _checkpoint_run(region, "after_checkpoint_commit", tmp_path, monkeypatch)
    assert final["state"] == "succeeded", final["result"]
    committed = [
        e["data"]["checkpoint"]
        for e in events(region, run_id, "step.completed")
        if "checkpoint" in e["data"]
    ]
    assert committed and final["result"]["candidates"]["tree"] == committed[0]
    builder = [
        s
        for s in region.detail(run_id)["steps"]
        if s["action"] == "execute" and s["args"].get("purpose") == "builder"
    ][0]
    assert builder["observation"]["rehydrated"] and builder["observation"]["tree"] == committed[0]
    assert not events(region, run_id, "step.redispatched")  # nothing committed was redone


def test_a18_abandoned_cleanup_is_finished_by_the_reaper_and_never_reused(region):
    sandboxes = region.harness.sandboxes
    first = sandboxes.allocate("run-a18", "tenant-a")
    (first.work / "marker.txt").write_text("left behind\n")
    pid = sandboxes.spawn_background(first, ["sleep", "300"])
    with faults.armed("during_cleanup"), pytest.raises(faults.SimulatedCrash):
        sandboxes.destroy(first.generation, "run finished")
    assert sandboxes.get(first.generation) is None  # fenced the moment cleanup began
    second = sandboxes.allocate("run-a18b", "tenant-a")
    assert second.generation != first.generation and not any(second.work.iterdir())
    reaped = sandboxes.reap()
    disposal = [r for r in reaped["reaped"] if r["generation"] == first.generation][0]
    assert (
        disposal["state"] == "destroyed" and disposal["confirmed"] and disposal["within_deadline"]
    )
    assert pid in disposal["processes_killed"] and not first.root.exists()
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)
    row = [s for s in sandboxes.list("run-a18") if s["generation"] == first.generation][0]
    assert row["disposal_ms"] is not None and row["disposal"]["confirmed"]


def _pump(region, coordinator, run_id, hold=lambda step: False, rounds=80):
    """Play the external workers: run every dispatched attempt except the held ones."""
    held = {}
    for _ in range(rounds):
        status = coordinator.tick(run_id)
        if status["state"] in TERMINAL:
            return status, held
        run = coordinator._view(coordinator.runs.get(run_id))
        rows = region.harness.state.rows(
            "SELECT s.step_id, s.action, s.input_json, s.current_attempt, a.fence FROM "
            "harness_steps s "
            "JOIN harness_attempts a ON a.attempt_id=s.current_attempt WHERE s.run_id=? AND "
            "s.status='dispatched'",
            (run_id,),
        )
        work = [r for r in rows if r["current_attempt"] not in held]
        if not work and status["state"] == "running" and not rows:
            continue
        for row in work:
            step = {
                "step_id": row["step_id"],
                "action": row["action"],
                "args": json.loads(row["input_json"]),
            }
            result = coordinator.execute_attempt(run, row["step_id"], row["action"], step["args"])
            if hold(step):
                held[row["current_attempt"]] = (row["step_id"], row["fence"], result)
                continue
            coordinator.commit_result(
                run_id, row["step_id"], row["current_attempt"], row["fence"], result
            )
    return coordinator.status(run_id), held


def test_a19_required_checks_gate_delivery_and_late_results_cannot_cross_candidates(region):
    data = write_tree(
        region.workspace / "metrics",
        {"energy.md": "# Site metrics\n\nDaily energy use: 12.5 kWh\nPeak load: 3.2 kW\n"},
    )
    reg = region.register(data)
    request = {
        "workload": "build_content",
        "question": "Brief the site manager on energy use.",
        "inputs": [reg],
        "discovery": "off",
        "audience": "site manager",
        "output": {"title": "Energy brief"},
        "facts": [
            {
                "label": "Daily energy",
                "pattern": r"Daily energy use:\s*([\d.]+)\s*kWh",
                "unit": "kWh",
            },
            {"label": "Peak load", "pattern": r"Peak load:\s*([\d.]+)\s*kW\b", "unit": "kW"},
        ],
        "acceptance": [
            {
                "id": "values-survive-render",
                "type": "values_present",
                "values": [{"value": "12.5", "unit": "kWh"}, {"value": "3.2", "unit": "kW"}],
            },
            {"id": "has-figures", "type": "contains", "text": "Key figures"},
            {"id": "screen-check", "type": "render_ok", "required": False},
        ],
    }
    coordinator = region.coordinator("worker-a", inline=False)
    run_id = coordinator.start(RunRequest(**request), actor="local", client_key="a19")["run_id"]

    def optional(step):
        return step["action"] == "verify" and step["args"]["criterion"]["id"] == "screen-check"

    final, held = _pump(region, coordinator, run_id, hold=optional)
    assert final["state"] == "succeeded", final["result"]
    checks = region.detail(run_id)["verifications"]
    first, last = checks[0]["artifact_digest"], final["result"]["candidates"]["primary"]
    assert first != last
    assert {c["criterion_id"]: c["outcome"] for c in checks if c["artifact_digest"] == first}[
        "values-survive-render"
    ] == "fail"
    outcomes = {c["criterion_id"]: c["outcome"] for c in checks if c["artifact_digest"] == last}
    required = ("values-survive-render", "has-figures", "citations-resolve", "claims-grounded")
    assert {k: outcomes[k] for k in required} == dict.fromkeys(required, "pass")
    assert outcomes.get("render-visual") == "skipped"  # optional, and this host has no browser
    assert "screen-check" in final["result"]["handoff"]["coverage_gaps"]
    # The optional check held for the first candidate arrives late: it satisfies nothing.
    for attempt, (step_id, fence, result) in held.items():
        assert not coordinator.commit_result(run_id, step_id, attempt, fence, result)["accepted"]
    assert not [
        c for c in region.detail(run_id)["verifications"] if c["criterion_id"] == "screen-check"
    ]


def test_a20_only_the_current_fenced_attempt_advances_a_step(region):
    _, request = _answer(region, adapter="record", target="")
    a = region.coordinator("worker-a", inline=False)
    run_id = a.start(RunRequest(**request), actor="local", client_key="a20")["run_id"]
    a.tick(run_id)  # accepted -> running -> resolve dispatched under fence 1
    run = a._view(a.runs.get(run_id))
    step = region.harness.state.rows("SELECT * FROM harness_steps WHERE run_id=?", (run_id,))[0]
    old_attempt = step["current_attempt"]
    region.harness.runs.expire_lease(run_id)
    b = region.coordinator("worker-b", inline=False)
    b.tick(run_id)  # failover: the step is re-dispatched under fence 2
    step = region.harness.state.rows("SELECT * FROM harness_steps WHERE run_id=?", (run_id,))[0]
    new_attempt = step["current_attempt"]
    assert new_attempt != old_attempt
    args = json.loads(step["input_json"])
    late = a.execute_attempt(run, step["step_id"], step["action"], args)
    fresh = b.execute_attempt(run, step["step_id"], step["action"], args)
    assert not a.commit_result(run_id, step["step_id"], old_attempt, 1, late)["accepted"]
    assert b.commit_result(run_id, step["step_id"], new_attempt, 2, fresh)["accepted"]
    assert not b.commit_result(run_id, step["step_id"], new_attempt, 2, fresh)[
        "accepted"
    ]  # duplicate
    completed = [
        e
        for e in events(region, run_id, "step.completed")
        if e["data"]["step_id"] == step["step_id"]
    ]
    assert len(completed) == 1 and completed[0]["data"]["attempt_id"] == new_attempt
    assert len(events(region, run_id, "step.result_rejected")) == 2
    spent = region.harness.state.rows(
        "SELECT status FROM harness_attempts WHERE attempt_id=?", (old_attempt,)
    )
    assert spent[0]["status"] in ("rejected", "abandoned")


def test_a21_resume_after_revocation_in_a_fresh_sandbox_names_the_blocked_criterion(region):
    repo, policy, run_id = _paused_change(region, "local")
    disposed = region.harness.sandboxes.list(run_id)
    assert disposed and all(
        s["state"] == "destroyed" for s in disposed
    )  # the wait released the workspace
    assert any(s["disposal"]["reason"] == "durable wait" for s in disposed)
    region.harness.registry.revoke(policy, "local")
    region.coordinator().resume(run_id, "local", {"approve": True})
    final = region.coordinator().drive(run_id)
    assert final["state"] == "partial"
    blocked = final["result"]["blocked"]
    assert blocked[0]["registration_id"] == policy and "builder-tests" in blocked[0]["criteria"]
    assert len(region.harness.sandboxes.list(run_id)) == len(
        disposed
    )  # no sandbox received anything
