"""The run coordinator: the only component that commits run state.

A run moves through ``accepted -> running <-> verifying -> delivering ->
succeeded``, with ``waiting``, ``reconciling`` and the terminal ``partial``,
``failed`` and ``cancelled`` beside it. Every transition is a fenced journal
append (``store.RunStore.append``), so the coordinator holding the lease is the
only one whose transitions land, and a coordinator that lost its lease finds
out on its next write.

The agent proposes; the coordinator disposes. Each proposal is admitted by
the broker (authority, capability, destination, budget), persisted as a step
with an attempt carrying the current fence *before* it runs, executed, and
committed only if that attempt is still the step's current one under the
current fence. A late, duplicate or stale result is recorded with what it
spent and advances nothing.

Recovery is the same code path as progress: a coordinator that claims a run
rebuilds the ledger from committed rows, re-dispatches read and workspace
steps whose attempt belonged to an earlier fence, and sends any delivery
whose outcome it cannot see through reconciliation before anything is sent
again.
"""

from __future__ import annotations

import contextvars
import json
import platform
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pheasant_harness import budget, delivery, faults, tools
from pheasant_harness import ledger as ledger_mod
from pheasant_harness.broker import TOOLS, WORKLOAD_CAPABILITIES
from pheasant_harness.common import (
    POLICY_VERSION,
    SCHEMA_VERSION,
    WORKFLOW_VERSION,
    AccessDenied,
    HarnessError,
    NotFound,
    QueueFull,
    RunConflict,
    StaleFence,
    canonical,
    digest_bytes,
    digest_value,
    new_id,
    now,
)
from pheasant_harness.planner import ModelPlanner, Plan, Proposal, RulePlanner, default_criteria
from pheasant_harness.sandbox import image_digest, isolation_manifest
from pheasant_harness.store import ACTIVE_STATES, TERMINAL_STATES
from pheasant_harness.verify import GRADER_VERSION, Verifier, verdict

LEASE_SECONDS = 30.0
MAX_ACTIVE_PER_TENANT = 8
CLASS_OF = {
    "answer": "interactive",
    "summarize": "interactive",
    "build_content": "build",
    "code_change": "build",
}


def upgrade_request(version: int, request: dict) -> dict:
    """Read a run's task contract as the workflow version it was pinned to wrote it."""
    if version == 1:
        upgraded = dict(request)
        if "sources" in upgraded:
            upgraded["inputs"] = upgraded.pop("sources")
        if "kind" in upgraded:
            upgraded["workload"] = upgraded.pop("kind")
        upgraded.setdefault("discovery", "auto")
        upgraded.setdefault("max_repairs", 2)
        upgraded.setdefault("destination", {"adapter": "record", "target": ""})
        return upgraded
    if version == WORKFLOW_VERSION:
        return request
    raise HarnessError(f"workflow version {version} needs a compatible worker")


def _status_for(outcome: str) -> str:
    if outcome in ("success", "partial"):
        return "succeeded"
    if outcome == "denied":
        return "denied"
    return "failed"


class Coordinator:
    def __init__(
        self,
        harness: Any,
        worker_id: str | None = None,
        *,
        lease_seconds: float = LEASE_SECONDS,
        inline: bool = True,
        max_parallel: int = 4,
        llm: Any = None,
    ):
        self.harness = harness
        self.runs = harness.runs
        self.worker = worker_id or f"{platform.node()}:{new_id('w')}"
        self.lease_seconds = lease_seconds
        self.inline = inline
        self.max_parallel = max_parallel
        self.llm = llm
        self.verifier = Verifier(harness)
        self._local = threading.local()

    # ------------------------------------------------------------------ start

    def start(
        self,
        request: Any,
        *,
        actor: str,
        client_key: str,
        tenant: str | None = None,
        workflow_version: int = WORKFLOW_VERSION,
        parent_run: str | None = None,
    ) -> dict:
        data = request.model_dump(mode="json") if hasattr(request, "model_dump") else dict(request)
        fingerprint = digest_value(data)
        existing = self.runs.find_by_key(actor, client_key)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise RunConflict(
                    f"client key {client_key!r} already names a different request (run "
                    f"{existing['id']})"
                )
            return {**self.status(existing["id"]), "reused": True}
        view = upgrade_request(workflow_version, data)
        for reg_id in [*view.get("inputs", []), *view.get("fixtures", [])]:
            reg = self.harness.registry.get(reg_id)
            if reg_id in view.get("inputs", []) and not self.harness.registry.authorized(
                reg, actor
            ):
                raise AccessDenied(f"{actor} is not authorized to read {reg_id}")
        delivery.adapter(self.harness, (view.get("destination") or {}).get("adapter", "record"))
        tenant = tenant or view.get("tenant") or actor
        active = self.harness.state.rows(
            "SELECT COUNT(*) AS n FROM harness_runs WHERE kb_id=? AND tenant=? AND state IN "
            "('accepted','running','verifying','delivering')",
            (self.harness.kb_id, tenant),
        )[0]["n"]
        if active >= MAX_ACTIVE_PER_TENANT:
            raise QueueFull(f"tenant {tenant} already has {active} active runs", retry_after=15)
        limits = budget.limits(view.get("budget"))
        run_id = new_id("run")
        versions = {
            "workflow": workflow_version,
            "schema": SCHEMA_VERSION,
            "policy": POLICY_VERSION,
            "planner": self._planner_name(view),
            "verifier": GRADER_VERSION,
            "sandbox_image": image_digest(),
            "tools": digest_value(sorted(TOOLS)),
        }
        criteria = default_criteria(view)
        row = {
            "id": run_id,
            "tenant": tenant,
            "actor": actor,
            "client_key": client_key,
            "fingerprint": fingerprint,
            "request_json": canonical(data),
            "workload": view["workload"],
            "state": "accepted",
            "workflow_version": workflow_version,
            "deadline_at": now() + float(limits["wall_seconds"]),
            "budget_json": canonical(limits),
            "usage_json": canonical({}),
            "versions_json": canonical(versions),
            "parent_run": parent_run,
        }
        envelope = {
            "actor": actor,
            "tenant": tenant,
            "inputs": view.get("inputs", []),
            "fixtures": view.get("fixtures", []),
            "destination": view.get("destination"),
            "policy_version": POLICY_VERSION,
            "capabilities": sorted(
                view.get("capabilities") or WORKLOAD_CAPABILITIES[view["workload"]]
            ),
        }
        created = self.runs.create(
            row,
            (
                "run.accepted",
                {
                    "task": view,
                    "authority": envelope,
                    "criteria": criteria,
                    "budget": limits,
                    "versions": versions,
                    "class": CLASS_OF[view["workload"]],
                },
            ),
        )
        if created["fingerprint"] != fingerprint:  # pragma: no cover - lost a create race
            raise RunConflict(f"client key {client_key!r} already names a different request")
        return {**self.status(created["id"]), "reused": created["id"] != run_id}

    def _planner_name(self, request: dict) -> str:
        if request.get("planner") == "rules" or self.llm is None:
            return RulePlanner.name
        return ModelPlanner.name

    def planner(self, request: dict):
        rules = RulePlanner(self.harness)
        if request.get("planner") == "rules" or self.llm is None:
            return rules
        return ModelPlanner(self.harness, self.llm, rules)

    # ------------------------------------------------------------------ views

    def _view(self, run: dict) -> dict:
        run = dict(run)
        run["request"] = upgrade_request(run["workflow_version"], run["request"])
        return run

    # ------------------------------------------------------------------- tick

    def tick(self, run_id: str, max_rounds: int = 200) -> dict:
        """Claim the run, advance it until it waits or ends, and release it."""
        fence = self.runs.claim(run_id, self.worker, self.lease_seconds)
        if fence is None:
            return {**self.status(run_id), "busy": True}
        self._local.fence = fence
        try:
            for _ in range(max_rounds):
                run = self._view(self.runs.get(run_id))
                if run["fence"] != fence:
                    break
                if not self._advance(run, fence):
                    break
                self.runs.renew(run_id, fence, self.lease_seconds)
        except StaleFence:
            return {**self.status(run_id), "stale": True}
        except HarnessError as exc:
            self.runs.append(
                run_id,
                "run.error",
                {"error": str(exc), "code": exc.code},
                fence=fence,
                visibility="operator",
            )
        except Exception as exc:
            # A defect, not a crash: say so, free the run for another worker, and surface it.
            self.runs.append(
                run_id,
                "run.error",
                {"error": f"{type(exc).__name__}: {exc}"[:500]},
                fence=fence,
                visibility="operator",
            )
            self.runs.release(run_id, fence)
            raise
        self.runs.release(run_id, fence)
        return self.status(run_id)

    def drive(self, run_id: str, rounds: int = 20) -> dict:
        """Tick until the run waits, ends, or stops making progress (tests, CLI)."""
        status = self.status(run_id)
        for _ in range(rounds):
            before = status["last_seq"]
            status = self.tick(run_id)
            if status["state"] in TERMINAL_STATES or status["state"] == "waiting":
                break
            if status["last_seq"] == before:
                break
        return status

    def _advance(self, run: dict, fence: int) -> bool:
        state = run["state"]
        if state in TERMINAL_STATES or state == "waiting":
            return False
        if run["cancel_requested"]:
            self._cancel(run, fence)
            return False
        if now() > run["deadline_at"]:
            self._finish(run, fence, {"status": "partial", "reason": "deadline reached"})
            return False
        if state == "accepted":
            blocked = self._blocked(run)
            if blocked:
                self._finish(
                    run,
                    fence,
                    {
                        "status": "failed",
                        "reason": "inputs unreadable at start",
                        "blocked": blocked,
                    },
                )
                return False
            self.runs.append(
                run["id"],
                "run.running",
                {"worker": self.worker, "fence": fence},
                fence=fence,
                updates={"state": "running"},
            )
            return True
        if self._recover(run, fence):
            return True
        if state == "reconciling":
            return self._reconcile(run, fence)
        if state == "delivering":
            return self._deliver(run, fence)
        current = ledger_mod.load(self.harness, run)
        if any(s["required"] for s in current.pending()):
            # Required results are owed by external workers; optional ones are not waited on.
            return False
        if current.resumes and current.resumes[-1].get("blocked"):
            self._finish(
                run,
                fence,
                {
                    "status": "partial",
                    "reason": "required input access revoked",
                    "blocked": current.resumes[-1]["blocked"],
                },
            )
            return False
        plan = self.planner(run["request"]).next(current)
        self._record_plan(run, fence, current, plan)
        if plan.wait:
            self.harness.sandboxes.destroy_for_run(run["id"], "durable wait")
            self.runs.append(
                run["id"],
                "run.waiting",
                plan.wait,
                fence=fence,
                updates={"state": "waiting", "wait_json": plan.wait},
            )
            return False
        if plan.finish:
            self._finish(run, fence, plan.finish)
            return False
        if plan.ready:
            return self._candidate_ready(run, fence, current, plan.ready)
        if plan.proposals:
            return self._dispatch(run, fence, current, plan.proposals)
        self._finish(
            run, fence, {"status": "partial", "reason": "the planner had nothing further to do"}
        )
        return False

    def _record_plan(self, run, fence, current, plan: Plan) -> None:
        if plan.spent:
            attempt = new_id("att")
            self.harness.state.execute(
                "INSERT INTO "
                "harness_attempts(attempt_id,run_id,step_id,fence,worker,status,reserved_json,"
                "spent_json,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt,
                    run["id"],
                    "plan",
                    fence,
                    self.worker,
                    "completed",
                    "{}",
                    json.dumps(plan.spent),
                    now(),
                    now(),
                ),
            )
        if plan.compacted and not current.compacted:
            self.runs.append(
                run["id"],
                "ledger.compacted",
                {
                    "context_chars": run["request"].get("context_chars"),
                    "evidence": len(current.evidence),
                },
                fence=fence,
            )
        for note in plan.notes:
            self.runs.append(
                run["id"], "planner.note", {"note": note, "planner": plan.planner}, fence=fence
            )

    def _blocked(self, run: dict) -> list[dict]:
        out = []
        for reg_id in run["request"].get("inputs", []):
            try:
                reg = self.harness.registry.get(reg_id)
            except NotFound:
                out.append({"registration_id": reg_id, "reason": "registration missing"})
                continue
            if not self.harness.registry.authorized(reg, run["actor"]):
                out.append(
                    {
                        "registration_id": reg_id,
                        "reason": "access revoked",
                        "criteria": [
                            c["id"] for c in default_criteria(run["request"]) if c["required"]
                        ],
                    }
                )
        return out

    # ---------------------------------------------------------------- dispatch

    def _dispatch(self, run: dict, fence: int, current, proposals: list[Proposal]) -> bool:
        usage = budget.usage(self.harness.state, run["id"])
        count = len(current.steps)
        admitted = []
        progressed = False
        for proposal in proposals:
            identity = digest_value([proposal.action, proposal.args])
            if any(s["identity"] == identity and s["status"] == "succeeded" for s in current.steps):
                continue  # an equivalent call already answered; never repeat it for nothing
            decision = self.harness.broker.authorize(run, proposal.action, proposal.args, usage)
            count += 1
            step_id = f"s{count:04d}"
            spec = self._step_row(run, step_id, proposal, identity, decision.as_dict())
            if not decision.allowed:
                spec["status"] = "denied"
                spec["result_json"] = canonical(
                    {"outcome": "denied", "observation": {"reason": decision.reason}}
                )
                self.runs.append(
                    run["id"],
                    "step.denied",
                    {
                        "step_id": step_id,
                        "action": proposal.action,
                        "decision": decision.as_dict(),
                        "why": proposal.why,
                    },
                    fence=fence,
                    statements=(self._insert_step(spec),),
                )
                progressed = True
                if decision.reason and decision.reason.startswith("budget:"):
                    self._finish(
                        run,
                        fence,
                        {"status": "partial", "reason": f"budget limit ({decision.reason[7:]})"},
                    )
                    return False
                continue
            cost = budget.COSTS.get(proposal.action, {"tool_calls": 1})
            for key, value in cost.items():
                usage["reserved"][key] = usage["reserved"].get(key, 0) + value
            attempt = new_id("att")
            spec["current_attempt"] = attempt
            self.runs.append(
                run["id"],
                "step.dispatched",
                {
                    "step_id": step_id,
                    "attempt_id": attempt,
                    "action": proposal.action,
                    "fence": fence,
                    "required": proposal.required,
                    "why": proposal.why,
                    "args": _public_args(proposal.args),
                },
                fence=fence,
                statements=(
                    self._insert_step(spec),
                    (
                        "INSERT INTO "
                        "harness_attempts(attempt_id,run_id,step_id,fence,worker,status,reserved_json,"
                        "started_at) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            attempt,
                            run["id"],
                            step_id,
                            fence,
                            self.worker,
                            "dispatched",
                            json.dumps(cost),
                            now(),
                        ),
                    ),
                    (
                        "INSERT INTO harness_outbox(id,run_id,kind,payload_json,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (
                            attempt,
                            run["id"],
                            "step",
                            canonical({"step_id": step_id, "attempt_id": attempt}),
                            now(),
                        ),
                    ),
                ),
            )
            admitted.append((step_id, attempt, proposal))
            progressed = True
        if not admitted:
            if not progressed:
                self._finish(
                    run,
                    fence,
                    {
                        "status": "partial",
                        "reason": "no new evidence: every proposed call was already answered",
                    },
                )
                return False
            return True
        faults.hit("before_dispatch")
        if self.inline:
            self._execute_all(run, fence, admitted)
        return True

    def _step_row(self, run, step_id, proposal, identity, decision) -> dict:
        spec = TOOLS.get(proposal.action, {"effect": "unknown", "capability": "none"})
        return {
            "run_id": run["id"],
            "step_id": step_id,
            "action": proposal.action,
            "identity": identity,
            "input_json": canonical(proposal.args),
            "depends_json": "[]",
            "capability": spec["capability"],
            "effect_class": spec["effect"],
            "required": int(proposal.required),
            "candidate": proposal.args.get("digest") if proposal.action == "verify" else None,
            "status": "dispatched",
            "current_attempt": None,
            "result_json": None,
            "policy_json": canonical(decision),
            "created_at": now(),
            "updated_at": now(),
        }

    @staticmethod
    def _insert_step(spec: dict) -> tuple:
        names = ",".join(spec)
        return (
            f"INSERT INTO harness_steps({names}) VALUES({','.join('?' for _ in spec)})",
            tuple(spec.values()),
        )

    def _cancel_check(self, run_id: str):
        state = {"checked": 0.0, "value": False}

        def cancelled() -> bool:
            if now() - state["checked"] > 0.5:
                rows = self.harness.state.rows(
                    "SELECT cancel_requested FROM harness_runs WHERE kb_id=? AND id=?",
                    (self.harness.kb_id, run_id),
                )
                state["value"] = bool(rows and rows[0]["cancel_requested"])
                state["checked"] = now()
            return state["value"]

        return cancelled

    def _execute_all(self, run: dict, fence: int, admitted: list) -> None:
        cancelled = self._cancel_check(run["id"])

        def work(item):
            step_id, attempt, proposal = item
            result = self.execute_attempt(run, step_id, proposal.action, proposal.args, cancelled)
            faults.hit("between_dispatch_and_commit")
            return self.commit_result(run["id"], step_id, attempt, fence, result)

        if len(admitted) == 1:
            for item in admitted:
                work(item)
            return
        workspace = [
            i for i in admitted if i[2].action in ("materialize", "apply_patch", "execute")
        ]
        parallel = [i for i in admitted if i not in workspace]
        with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
            futures = [pool.submit(contextvars.copy_context().run, work, item) for item in parallel]
            for future in futures:
                future.result()
        for item in workspace:  # dependent writes are serialized
            work(item)

    def execute_attempt(
        self, run: dict, step_id: str, action: str, args: dict, cancelled=None
    ) -> dict:
        """Run one attempt. Pure with respect to run state: nothing is committed here."""
        if action == "verify":
            check = self.verifier.check(run, args["criterion"], args["digest"], cancelled)
            return {
                "outcome": "success",
                "observation": {"outcome": check["outcome"], "criterion_id": check["criterion_id"]},
                "verification": check,
                "spent": {"tool_calls": 1},
            }
        return tools.execute(
            self.harness, run, {"step_id": step_id, "action": action, "args": args}, cancelled
        )

    # ------------------------------------------------------------------ commit

    def commit_result(
        self, run_id: str, step_id: str, attempt_id: str, fence: int, result: dict
    ) -> dict:
        """Accept a completion only for the step's current attempt under the current fence."""
        spent = result.get("spent", {})
        statements = []
        for record in result.get("evidence", []):
            statements.append(
                (
                    "INSERT INTO "
                    "harness_evidence(run_id,id,registration_id,revision,locator_json,method,status,"
                    "extract,extract_digest,classification,flags_json,step_id,observed_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (run_id,id) DO NOTHING",
                    (
                        run_id,
                        record["id"],
                        record["registration_id"],
                        record["revision"],
                        canonical(record["locator"]),
                        record["method"],
                        record["status"],
                        record["extract"],
                        record["extract_digest"],
                        record["classification"],
                        canonical(record["flags"]),
                        step_id,
                        now(),
                    ),
                )
            )
        for artifact in result.get("artifacts", []):
            statements.append(
                (
                    "INSERT INTO "
                    "harness_artifacts(run_id,digest,kind,name,media_type,parent_digest,step_id,"
                    "provenance_json,review_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT (run_id,digest) DO NOTHING",
                    (
                        run_id,
                        artifact["digest"],
                        artifact["kind"],
                        artifact["name"],
                        artifact["media_type"],
                        artifact.get("parent_digest"),
                        step_id,
                        canonical(artifact.get("provenance", {})),
                        "unreviewed",
                        now(),
                    ),
                )
            )
        check = result.get("verification")
        if check and check["observed"].get("screenshot"):
            # What the verifier saw is an artifact of its own, a derivative of the candidate.
            statements.append(
                (
                    "INSERT INTO "
                    "harness_artifacts(run_id,digest,kind,name,media_type,parent_digest,"
                    "step_id,provenance_json,review_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT (run_id,digest) DO NOTHING",
                    (
                        run_id,
                        check["observed"]["screenshot"],
                        "render",
                        f"{check['criterion_id']} screenshot",
                        "image/png",
                        check["artifact_digest"],
                        step_id,
                        canonical(
                            {
                                "verifier": check["grader"],
                                "environment": check["environment"],
                                "viewport": check["observed"].get("viewport"),
                            }
                        ),
                        "verifier-observed",
                        now(),
                    ),
                )
            )
        if check:
            statements.append(
                (
                    "INSERT INTO "
                    "harness_verifications(run_id,id,criterion_id,artifact_digest,input_digests_json,"
                    "environment_json,grader,grader_version,outcome,observed_json,evidence_json,"
                    "created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        check["id"],
                        check["criterion_id"],
                        check["artifact_digest"],
                        canonical(check["input_digests"]),
                        canonical(check["environment"]),
                        check["grader"],
                        check["grader_version"],
                        check["outcome"],
                        canonical(check["observed"]),
                        canonical(check["evidence"]),
                        now(),
                    ),
                )
            )
        stored = {
            k: v for k, v in result.items() if k not in ("evidence", "artifacts", "verification")
        }
        stored["evidence_ids"] = [e["id"] for e in result.get("evidence", [])]
        stored["artifact_digests"] = [a["digest"] for a in result.get("artifacts", [])]
        if check:
            stored["verification_id"] = check["id"]
        status = _status_for(result.get("outcome", "permanent_error"))
        statements += [
            (
                "UPDATE harness_steps SET status=?, result_json=?, updated_at=? WHERE run_id=? "
                "AND step_id=? "
                "AND current_attempt=? AND status='dispatched'",
                (status, canonical(stored), now(), run_id, step_id, attempt_id),
                True,
            ),
            (
                "UPDATE harness_attempts SET status='completed', spent_json=?, finished_at=? "
                "WHERE attempt_id=?",
                (json.dumps(spent), now(), attempt_id),
            ),
            ("UPDATE harness_outbox SET done_at=? WHERE id=?", (now(), attempt_id)),
        ]
        summary = {
            "step_id": step_id,
            "attempt_id": attempt_id,
            "outcome": result.get("outcome"),
            "evidence": stored["evidence_ids"][:20],
            "artifacts": stored["artifact_digests"],
            "spent": spent,
        }
        if check:
            summary["verification"] = {
                "criterion": check["criterion_id"],
                "outcome": check["outcome"],
                "candidate": check["artifact_digest"],
            }
        if checkpoint := [a for a in result.get("artifacts", []) if a["kind"] == "tree"]:
            summary["checkpoint"] = checkpoint[0]["digest"]
            faults.hit("before_checkpoint_commit")
        try:
            self.runs.append(
                run_id,
                "step.completed",
                summary,
                fence=fence,
                statements=tuple(statements),
                require_state=ACTIVE_STATES,
            )
        except StaleFence as exc:
            # The attempt spent what it spent; its result advances nothing.
            self.harness.state.execute(
                "UPDATE harness_attempts SET status='rejected', spent_json=COALESCE(spent_json, "
                "?), "
                "finished_at=? WHERE attempt_id=?",
                (json.dumps(spent), now(), attempt_id),
            )
            self.runs.append(
                run_id,
                "step.result_rejected",
                {"step_id": step_id, "attempt_id": attempt_id, "fence": fence, "reason": str(exc)},
                visibility="operator",
            )
            return {"accepted": False, "reason": str(exc)}
        if "checkpoint" in summary:
            faults.hit("after_checkpoint_commit")
        return {"accepted": True, "status": status}

    # ---------------------------------------------------------------- recovery

    def _recover(self, run: dict, fence: int) -> bool:
        rows = self.harness.state.rows(
            "SELECT s.step_id, s.action, s.input_json, s.current_attempt, a.fence FROM "
            "harness_steps s "
            "JOIN harness_attempts a ON a.attempt_id=s.current_attempt "
            "WHERE s.run_id=? AND s.status='dispatched' AND a.fence<?",
            (run["id"], fence),
        )
        if not rows:
            return False
        redo = []
        for row in rows:
            attempt = new_id("att")
            args = json.loads(row["input_json"])
            cost = budget.COSTS.get(row["action"], {"tool_calls": 1})
            if row["action"] == "verify":
                superseded = self._superseded(run, args)
                if superseded:
                    self.runs.append(
                        run["id"],
                        "step.superseded",
                        {"step_id": row["step_id"], "candidate": args["digest"]},
                        fence=fence,
                        statements=(
                            (
                                "UPDATE harness_steps SET status='superseded' WHERE run_id=? "
                                "AND step_id=?",
                                (run["id"], row["step_id"]),
                            ),
                        ),
                    )
                    continue
            self.runs.append(
                run["id"],
                "step.redispatched",
                {
                    "step_id": row["step_id"],
                    "abandoned_attempt": row["current_attempt"],
                    "attempt_id": attempt,
                    "fence": fence,
                },
                fence=fence,
                statements=(
                    (
                        "UPDATE harness_attempts SET status='abandoned', "
                        "spent_json=reserved_json, finished_at=? "
                        "WHERE attempt_id=?",
                        (now(), row["current_attempt"]),
                    ),
                    (
                        "UPDATE harness_steps SET current_attempt=?, updated_at=? WHERE run_id=? "
                        "AND step_id=?",
                        (attempt, now(), run["id"], row["step_id"]),
                    ),
                    (
                        "INSERT INTO "
                        "harness_attempts(attempt_id,run_id,step_id,fence,worker,status,reserved_json,"
                        "started_at) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            attempt,
                            run["id"],
                            row["step_id"],
                            fence,
                            self.worker,
                            "dispatched",
                            json.dumps(cost),
                            now(),
                        ),
                    ),
                ),
            )
            redo.append((row["step_id"], attempt, Proposal(row["action"], args)))
        if self.inline and redo:
            self._execute_all(run, fence, redo)
        return True

    def _superseded(self, run: dict, args: dict) -> bool:
        target = args.get("target", "primary")
        current = ledger_mod.load(self.harness, run)
        kinds = {"tree": "tree"}.get(target, "document")
        latest = [a for a in current.artifacts if a["kind"] == kinds]
        return bool(latest) and latest[-1]["digest"] != args["digest"]

    # ------------------------------------------------------------ verification

    def _candidate_ready(self, run: dict, fence: int, current, ready: dict[str, str]) -> bool:
        criteria = default_criteria(run["request"])
        # Checks on any other candidate no longer count; outstanding ones are superseded.
        stale = [
            s
            for s in current.steps
            if s["action"] == "verify"
            and s["status"] == "dispatched"
            and s["candidate"] not in ready.values()
        ]
        for step in stale:
            self.runs.append(
                run["id"],
                "step.superseded",
                {"step_id": step["step_id"], "candidate": step["candidate"]},
                fence=fence,
                statements=(
                    (
                        "UPDATE harness_steps SET status='superseded' WHERE run_id=? AND step_id=?",
                        (run["id"], step["step_id"]),
                    ),
                ),
            )
        needed = []
        for criterion in criteria:
            digest = ready.get(criterion["target"])
            if digest is None:
                continue
            done = [
                c
                for c in current.checks
                if c["criterion_id"] == criterion["id"] and c["artifact_digest"] == digest
            ]
            asked = [
                s
                for s in current.steps
                if s["action"] == "verify"
                and s["candidate"] == digest
                and s["args"]["criterion"]["id"] == criterion["id"]
                and s["status"] != "superseded"
            ]
            if not done and not asked:
                needed.append(
                    Proposal(
                        "verify",
                        {"criterion": criterion, "digest": digest, "target": criterion["target"]},
                        required=criterion["required"],
                        why=f"check {criterion['id']}",
                    )
                )
        if needed:
            if run["state"] != "verifying":
                self.runs.append(
                    run["id"],
                    "run.verifying",
                    {"candidates": ready},
                    fence=fence,
                    updates={"state": "verifying"},
                )
                run = self._view(self.runs.get(run["id"]))
            return self._dispatch(run, fence, current, needed)
        checks = current.checks
        verdicts = {}
        for target, digest in ready.items():
            relevant = [c for c in criteria if c["target"] == target]
            verdicts[target] = verdict(checks, relevant, digest)
        failing = {t: v["failing"] for t, v in verdicts.items() if v["failing"]}
        if failing:
            offered = self.harness.state.rows(
                "SELECT data_json FROM harness_events WHERE run_id=? AND "
                "type='verification.failed'",
                (run["id"],),
            )
            if any(json.loads(r["data_json"]).get("candidates") == ready for r in offered):
                # The planner offered, unchanged, a candidate that already failed: stop rather
                # than ask the same question again (§4.5).
                self._finish(
                    run,
                    fence,
                    {
                        "status": "partial",
                        "reason": "no repair: the failing candidate was offered again unchanged",
                        "candidate": ready,
                        "verdicts": verdicts,
                        "failing": failing,
                    },
                )
                return False
            self.runs.append(
                run["id"],
                "verification.failed",
                {
                    "candidates": ready,
                    "verdicts": verdicts,
                    "findings": self._findings(checks, ready),
                },
                fence=fence,
                updates={"state": "running"},
            )
            return True
        unverified = {t: v["unverified"] for t, v in verdicts.items() if v["unverified"]}
        if unverified:
            self._finish(
                run,
                fence,
                {
                    "status": "partial",
                    "reason": "required checks could not run",
                    "unverified": unverified,
                    "candidate": ready,
                    "verdicts": verdicts,
                },
            )
            return False
        integrity = self.integrity(run["id"])
        if not integrity["ok"]:
            self._finish(
                run,
                fence,
                {
                    "status": "partial",
                    "reason": "artifact integrity check failed",
                    "integrity": integrity,
                    "candidate": ready,
                },
            )
            return False
        self.runs.append(
            run["id"],
            "verification.passed",
            {"verdicts": verdicts},
            fence=fence,
            updates={
                "state": "delivering",
                "result_json": {"candidates": ready, "verdicts": verdicts},
            },
        )
        return True

    @staticmethod
    def _findings(checks, ready) -> list[dict]:
        return [
            {
                "criterion": c["criterion_id"],
                "candidate": c["artifact_digest"],
                "observed": c["observed"],
            }
            for c in checks
            if c["artifact_digest"] in ready.values() and c["outcome"] == "fail"
        ]

    # ---------------------------------------------------------------- delivery

    def _payload(self, run: dict, candidates: dict[str, str]) -> bytes:
        blobs = self.harness.blobs
        if set(candidates) == {"primary"}:
            return blobs.get(candidates["primary"])
        bundle = {}
        for target, digest in sorted(candidates.items()):
            if digest.startswith("tree:"):
                manifest = json.loads(blobs.get_text(digest.removeprefix("tree:")))
                bundle[target] = {
                    "digest": digest,
                    "manifest": manifest,
                    "files": {
                        p: (blobs.get_text(d) if d else None) for p, d in manifest["files"].items()
                    },
                }
            else:
                bundle[target] = {"digest": digest, "content": blobs.get_text(digest)}
        return canonical(bundle).encode()

    def operation_id(self, run: dict, adapter: str, target: str, payload_digest: str) -> str:
        lineage = run.get("parent_run") or run["id"]
        return "op_" + digest_value([lineage, "deliver", adapter, target, payload_digest])[7:31]

    def _effect(self, operation_id: str) -> dict | None:
        rows = self.harness.state.rows(
            "SELECT * FROM harness_effects WHERE operation_id=?", (operation_id,)
        )
        return dict(rows[0]) if rows else None

    def _deliver(self, run: dict, fence: int) -> bool:
        destination = run["request"].get("destination") or {"adapter": "record", "target": ""}
        candidates = (run["result"] or {}).get("candidates") or {}
        sink = delivery.adapter(self.harness, destination.get("adapter", "record"))
        decision = self.harness.broker.authorize(
            run,
            "deliver",
            {
                "digest": next(iter(candidates.values()), ""),
                "adapter": destination.get("adapter"),
                "target": destination.get("target"),
            },
            budget.usage(self.harness.state, run["id"]),
        )
        if not decision.allowed:
            self._finish(
                run,
                fence,
                {
                    "status": "partial",
                    "reason": f"delivery refused: {decision.reason}",
                    "candidate": candidates,
                },
            )
            return False
        integrity = self.integrity(run["id"])
        if not integrity["ok"]:
            # A restore that lost an object must not deliver, or report, what it cannot reproduce.
            self._finish(
                run,
                fence,
                {
                    "status": "partial",
                    "reason": "artifact integrity check failed",
                    "integrity": integrity,
                    "candidate": candidates,
                },
            )
            return False
        payload = self._payload(run, candidates)
        payload_digest = digest_bytes(payload)
        op = self.operation_id(run, sink.name, destination.get("target", ""), payload_digest)
        effect = self._effect(op)
        if effect is not None and effect["payload_hash"] != payload_digest:
            raise RunConflict(f"operation {op} was already used with a different payload")
        if effect is None:
            self.harness.blobs.put(payload, "application/octet-stream")
            self.runs.append(
                run["id"],
                "effect.intended",
                {
                    "operation_id": op,
                    "adapter": sink.name,
                    "target": destination.get("target"),
                    "payload_digest": payload_digest,
                    "adapter_contract": sink.describe(),
                },
                fence=fence,
                statements=(
                    (
                        "INSERT INTO harness_effects(operation_id,run_id,step_id,adapter,target,"
                        "expected_revision,payload_digest,payload_hash,state,created_at,"
                        "updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            op,
                            run["id"],
                            "deliver",
                            sink.name,
                            destination.get("target", ""),
                            destination.get("expected_target_version"),
                            payload_digest,
                            payload_digest,
                            "intended",
                            now(),
                            now(),
                        ),
                    ),
                ),
            )
            effect = self._effect(op)
        if effect["state"] == "committed":
            return self._succeed(run, fence, effect)
        if effect["state"] in ("dispatched", "uncertain"):
            self.runs.append(
                run["id"],
                "run.reconciling",
                {"operation_id": op},
                fence=fence,
                updates={"state": "reconciling"},
            )
            return True
        faults.hit("before_effect_dispatch")
        self.runs.append(
            run["id"],
            "effect.dispatching",
            {"operation_id": op, "attempt": effect["dispatch_count"] + 1},
            fence=fence,
            statements=(
                (
                    "UPDATE harness_effects SET state='dispatched', "
                    "dispatch_count=dispatch_count+1, "
                    "updated_at=? WHERE operation_id=? AND state='intended'",
                    (now(), op),
                    True,
                ),
            ),
        )
        try:
            receipt = sink.dispatch(
                op,
                destination.get("target", ""),
                payload,
                destination.get("expected_target_version"),
            )
        except delivery.DeliveryConflict as exc:
            self.runs.append(
                run["id"],
                "effect.conflict",
                {"operation_id": op, "detail": str(exc)},
                fence=fence,
                statements=(
                    (
                        "UPDATE harness_effects SET state='conflict', disposition=?, updated_at=? "
                        "WHERE operation_id=?",
                        (str(exc), now(), op),
                    ),
                ),
            )
            self._finish(
                run,
                fence,
                {
                    "status": "partial",
                    "reason": "delivery target changed after validation",
                    "conflict": str(exc),
                    "candidate": candidates,
                    "next_action": "revalidate a new candidate against the current target",
                },
            )
            return False
        except HarnessError:
            raise
        except Exception as exc:  # noqa: BLE001 - an ambiguous outcome is reconciled, not retried
            self.runs.append(
                run["id"],
                "effect.ambiguous",
                {"operation_id": op, "error": type(exc).__name__},
                fence=fence,
                updates={"state": "reconciling"},
            )
            return True
        self.runs.append(
            run["id"],
            "effect.committed",
            {"operation_id": op, **receipt},
            fence=fence,
            statements=(
                (
                    "UPDATE harness_effects SET state='committed', downstream_id=?, "
                    "downstream_revision=?, disposition='applied', updated_at=? WHERE "
                    "operation_id=?",
                    (receipt.get("downstream_id"), receipt.get("downstream_revision"), now(), op),
                ),
            ),
        )
        faults.hit("after_receipt")
        return self._succeed(run, fence, self._effect(op))

    def _reconcile(self, run: dict, fence: int) -> bool:
        effects = self.harness.state.rows(
            "SELECT * FROM harness_effects WHERE run_id=? AND state IN ('dispatched','uncertain')",
            (run["id"],),
        )
        if not effects:
            committed = self.harness.state.rows(
                "SELECT * FROM harness_effects WHERE run_id=? AND state='committed'", (run["id"],)
            )
            if committed:
                return self._succeed(run, fence, dict(committed[0]))
            self.runs.append(
                run["id"], "run.delivering", {}, fence=fence, updates={"state": "delivering"}
            )
            return True
        effect = dict(effects[0])
        sink = delivery.adapter(self.harness, effect["adapter"])
        found = sink.reconcile(effect["operation_id"], effect["target"], effect["payload_hash"])
        op = effect["operation_id"]
        if found["status"] == "committed":
            self.runs.append(
                run["id"],
                "effect.reconciled",
                {"operation_id": op, **found},
                fence=fence,
                statements=(
                    (
                        "UPDATE harness_effects SET state='committed', downstream_id=?, "
                        "downstream_revision=?, disposition='reconciled: found by operation id', "
                        "updated_at=? WHERE operation_id=?",
                        (found.get("downstream_id"), found.get("downstream_revision"), now(), op),
                    ),
                ),
            )
            return self._succeed(run, fence, self._effect(op))
        if found["status"] == "absent" and (sink.idempotent or sink.observable):
            self.runs.append(
                run["id"],
                "effect.reconciled",
                {
                    "operation_id": op,
                    "status": "absent",
                    "next": "dispatch again under the same operation id",
                },
                fence=fence,
                updates={"state": "delivering"},
                statements=(
                    (
                        "UPDATE harness_effects SET state='intended', updated_at=? "
                        "WHERE operation_id=?",
                        (now(), op),
                    ),
                ),
            )
            return True
        if found["status"] == "conflict":
            self._finish(
                run,
                fence,
                {"status": "partial", "reason": "target holds a different payload", "effect": op},
            )
            return False
        if effect["state"] != "uncertain":
            self.runs.append(
                run["id"],
                "effect.uncertain",
                {
                    "operation_id": op,
                    "dispatch_count": effect["dispatch_count"],
                    "reason": "the target can neither deduplicate nor be observed; not repeating",
                    "next_action": "an operator inspects the target and resolves this effect",
                },
                fence=fence,
                statements=(
                    (
                        "UPDATE harness_effects SET state='uncertain', reconciliation_owner=?, "
                        "updated_at=? WHERE operation_id=?",
                        ("operator", now(), op),
                    ),
                ),
            )
        return False

    def resolve_effect(self, run_id: str, operation_id: str, disposition: str, actor: str) -> dict:
        """An operator's finding about an uncertain effect: applied, or not applied."""
        effect = self._effect(operation_id)
        if effect is None or effect["run_id"] != run_id:
            raise NotFound(f"Unknown effect: {operation_id}")
        if effect["state"] != "uncertain":
            raise HarnessError(f"effect {operation_id} is {effect['state']}, not uncertain")
        if disposition not in ("applied", "not_applied"):
            raise HarnessError("disposition is 'applied' or 'not_applied'")
        new_state = "committed" if disposition == "applied" else "intended"
        self.runs.append(
            run_id,
            "effect.resolved",
            {"operation_id": operation_id, "disposition": disposition, "by": actor},
            statements=(
                (
                    "UPDATE harness_effects SET state=?, disposition=?, updated_at=? "
                    "WHERE operation_id=?",
                    (new_state, f"operator: {disposition}", now(), operation_id),
                ),
            ),
        )
        return self.tick(run_id)

    def _succeed(self, run: dict, fence: int, effect: dict) -> bool:
        self._finish(
            run,
            fence,
            {
                "status": "succeeded",
                "receipt": {
                    "operation_id": effect["operation_id"],
                    "adapter": effect["adapter"],
                    "target": effect["target"],
                    "payload_digest": effect["payload_digest"],
                    "downstream_id": effect["downstream_id"],
                    "downstream_revision": effect["downstream_revision"],
                    "disposition": effect["disposition"],
                },
            },
        )
        return False

    # --------------------------------------------------------------- endings

    def _finish(self, run: dict, fence: int | None, outcome: dict) -> None:
        status = outcome["status"]
        current = ledger_mod.load(self.harness, run)
        prior = run.get("result") or {}
        candidates = outcome.get("candidate") or prior.get("candidates")
        if isinstance(candidates, str):
            candidates = {"primary": candidates}
        result = {
            **{k: v for k, v in outcome.items() if k not in ("candidate",)},
            "candidates": candidates,
            "verdicts": outcome.get("verdicts") or prior.get("verdicts"),
            "handoff": self._handoff(run, current, candidates),
        }
        self.harness.sandboxes.destroy_for_run(run["id"], f"run {status}")
        # Anything still out (an optional branch nobody waited for) is closed: its result,
        # if it ever arrives, is accounted for and changes nothing.
        closing = (
            (
                "UPDATE harness_steps SET status='closed', updated_at=? WHERE run_id=? "
                "AND status='dispatched'",
                (now(), run["id"]),
            ),
        )
        self.runs.append(
            run["id"],
            f"run.{status}",
            {k: v for k, v in result.items() if k != "handoff"},
            fence=fence,
            updates={"state": status, "result_json": result},
            statements=closing,
        )

    def _handoff(self, run: dict, current, candidates: dict | None) -> dict:
        criteria = default_criteria(run["request"])
        verified, unverified = [], []
        for target, digest in (candidates or {}).items():
            v = verdict(current.checks, [c for c in criteria if c["target"] == target], digest)
            verified += v["passing"]
            unverified += v["unverified"] + v["failing"]
        executed = [
            {
                "argv": s["args"]["argv"],
                "purpose": s["args"].get("purpose"),
                "exit_status": (s["result"] or {}).get("observation", {}).get("exit_status"),
                "environment": (s["result"] or {}).get("observation", {}).get("environment"),
            }
            for s in current.steps
            if s["action"] == "execute" and s["result"]
        ]
        return {
            "artifacts": candidates,
            "verified_criteria": sorted(set(verified)),
            "unverified_criteria": sorted(set(unverified)),
            "assumptions": [
                s["args"].get("purpose") for s in current.steps if s["action"] == "plan"
            ],
            "unresolved": current.failed_avenues(),
            "evidence_count": len(current.evidence),
            "revisions": current.revisions,
            "executed": executed,
            "coverage_gaps": [
                c["id"]
                for c in criteria
                if not c["required"]
                and not any(
                    k["criterion_id"] == c["id"] and k["outcome"] == "pass" for k in current.checks
                )
            ],
        }

    def _cancel(self, run: dict, fence: int | None) -> None:
        in_flight = self.harness.state.rows(
            "SELECT step_id, current_attempt FROM harness_steps WHERE run_id=? AND "
            "status='dispatched'",
            (run["id"],),
        )
        effects = [
            dict(r)
            for r in self.harness.state.rows(
                "SELECT operation_id, state FROM harness_effects WHERE run_id=?", (run["id"],)
            )
        ]
        statements = [
            (
                "UPDATE harness_steps SET status='cancelled', updated_at=? WHERE run_id=? AND "
                "status='dispatched'",
                (now(), run["id"]),
            ),
            (
                "UPDATE harness_attempts SET status='cancelled', spent_json=COALESCE(spent_json, "
                "reserved_json), "
                "finished_at=? WHERE run_id=? AND status='dispatched'",
                (now(), run["id"]),
            ),
        ]
        disposed = self.harness.sandboxes.destroy_for_run(run["id"], "cancelled")
        current = ledger_mod.load(self.harness, run)
        latest = {a["kind"]: a["digest"] for a in current.artifacts}
        result = {
            "status": "cancelled",
            "in_flight": [dict(r) for r in in_flight],
            "effects": effects,
            "partial_artifacts": latest,
            "sandboxes": [d.get("generation") for d in disposed],
            "uncertain_effects": [e for e in effects if e["state"] in ("dispatched", "uncertain")],
        }
        self.runs.append(
            run["id"],
            "run.cancelled",
            result,
            fence=fence,
            updates={"state": "cancelled", "result_json": result},
            statements=tuple(statements),
        )

    # ------------------------------------------------------- caller actions

    def cancel(self, run_id: str, actor: str) -> dict:
        run = self.runs.get(run_id)
        if run["state"] in TERMINAL_STATES:
            return self.status(run_id)
        self.runs.append(
            run_id, "run.cancel_requested", {"by": actor}, updates={"cancel_requested": 1}
        )
        if run["state"] == "waiting":
            fence = self.runs.claim(run_id, self.worker, self.lease_seconds)
            if fence is not None:
                self._cancel(self._view(self.runs.get(run_id)), fence)
                self.runs.release(run_id, fence)
        return self.status(run_id)

    def resume(self, run_id: str, actor: str, user_input: dict | None = None) -> dict:
        run = self._view(self.runs.get(run_id))
        if run["state"] != "waiting":
            raise HarnessError(f"run {run_id} is {run['state']}, not waiting")
        blocked = self._blocked(run)
        self.runs.append(
            run_id,
            "run.resumed",
            {"input": user_input or {}, "by": actor, "blocked": blocked},
            updates={"state": "running", "wait_json": None},
            require_state=("waiting",),
        )
        return self.status(run_id)

    # --------------------------------------------------------------- integrity

    def integrity(self, run_id: str) -> dict:
        """Every object the run's records name is present and matches its digest."""
        blobs = self.harness.blobs
        missing = []
        for row in self.harness.state.rows(
            "SELECT digest, kind FROM harness_artifacts WHERE run_id=?", (run_id,)
        ):
            digest = row["digest"].removeprefix("tree:")
            if not blobs.exists(digest):
                missing.append({"object": row["digest"], "kind": row["kind"]})
                continue
            if row["kind"] == "tree":
                manifest = json.loads(blobs.get_text(digest))
                for path, file_digest in manifest.get("files", {}).items():
                    if file_digest and not blobs.exists(file_digest):
                        missing.append({"object": file_digest, "kind": "tree file", "path": path})
        for row in self.harness.state.rows(
            "SELECT DISTINCT r.manifest_digest FROM harness_revisions r JOIN harness_evidence e "
            "ON e.registration_id=r.registration_id AND e.revision=r.revision WHERE e.run_id=?",
            (run_id,),
        ):
            if not blobs.exists(row["manifest_digest"]):
                missing.append({"object": row["manifest_digest"], "kind": "revision manifest"})
        return {"ok": not missing, "missing": missing, "checked_at": now()}

    # ------------------------------------------------------------------ status

    def status(self, run_id: str) -> dict:
        run = self._view(self.runs.get(run_id))
        usage = budget.usage(self.harness.state, run_id)
        steps = self.harness.state.rows(
            "SELECT status, COUNT(*) AS n FROM harness_steps WHERE run_id=? GROUP BY status",
            (run_id,),
        )
        effects = [
            dict(r)
            for r in self.harness.state.rows(
                "SELECT operation_id, adapter, target, state, dispatch_count, downstream_id, "
                "disposition "
                "FROM harness_effects WHERE run_id=?",
                (run_id,),
            )
        ]
        return {
            "run_id": run_id,
            "state": run["state"],
            "workload": run["workload"],
            "class": CLASS_OF.get(run["workload"]),
            "tenant": run["tenant"],
            "actor": run["actor"],
            "question": run["request"].get("question"),
            "last_seq": run["last_seq"],
            "revision": run["revision"],
            "fence": run["fence"],
            "lease": {
                "owner": run["lease_owner"],
                "until": run["lease_until"],
                "held": bool(run["lease_until"] and run["lease_until"] > now()),
            },
            "cancel_requested": bool(run["cancel_requested"]),
            "workflow_version": run["workflow_version"],
            "versions": run["versions"],
            "wait": run["wait"],
            "result": run["result"],
            "steps": {r["status"]: r["n"] for r in steps},
            "budget": {
                "limits": run["limits"],
                "spent": usage["spent"],
                "reserved": usage["reserved"],
                "remaining": budget.remaining(run["limits"], usage),
            },
            "effects": effects,
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "deadline_at": run["deadline_at"],
        }

    def detail(self, run_id: str) -> dict:
        run = self._view(self.runs.get(run_id))
        current = ledger_mod.load(self.harness, run)
        attempts = [
            dict(r)
            for r in self.harness.state.rows(
                "SELECT attempt_id, step_id, fence, worker, status, reserved_json, spent_json, "
                "started_at, finished_at "
                "FROM harness_attempts WHERE run_id=? ORDER BY started_at",
                (run_id,),
            )
        ]
        return {
            **self.status(run_id),
            "request": run["request"],
            "criteria": default_criteria(run["request"]),
            "steps": [
                {k: v for k, v in s.items() if k != "result"}
                | {
                    "outcome": (s["result"] or {}).get("outcome"),
                    "observation": _compact((s["result"] or {}).get("observation", {})),
                }
                for s in current.steps
            ],
            "attempts": attempts,
            "evidence": current.evidence,
            "artifacts": current.artifacts,
            "verifications": current.checks,
            "revisions": current.revisions,
            "sandboxes": self.harness.sandboxes.list(run_id),
            "isolation": isolation_manifest(),
            "integrity": self.integrity(run_id),
            "context_preview": current.context()[0][:4000],
        }


def _public_args(args: dict) -> dict:
    out = {}
    for key, value in args.items():
        if key == "content":
            out[key] = f"<{len(value)} chars>"
        elif key == "edits":
            out[key] = [{"path": e.get("path")} for e in value]
        elif key == "criterion":
            out[key] = {"id": value.get("id"), "type": value.get("type")}
        else:
            out[key] = value
    return out


def _compact(observation: dict) -> dict:
    out = {}
    for key, value in observation.items():
        if key in ("paths", "entries") and isinstance(value, list):
            out[key] = value[:50]
            out[f"{key}_total"] = len(value)
        elif key in ("matches", "candidates", "definitions", "callers") and isinstance(value, list):
            out[key] = value[:20]
        elif key == "content" and isinstance(value, str):
            out[key] = value[:4000]
        else:
            out[key] = value
    return out
