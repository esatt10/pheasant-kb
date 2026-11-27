"""The external ledger: what the run knows, rebuilt from committed records.

The agent's working memory is not its context window. Every model call is
assembled from this ledger -- the request, accepted revisions, decisions,
open questions, failed avenues, evidence ids and remaining budget -- and a
ledger that outgrows its share of the context is *compacted*: long extracts
become one-line summaries that keep their evidence ids, and the original is
reopened (read again at its locator) before anything is quoted from it.
Because it is rebuilt from rows, a replacement worker sees exactly what the
lost one would have, without any private reasoning to restore.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pheasant_harness import budget
from pheasant_harness.common import loads


@dataclass
class Ledger:
    run: dict
    steps: list[dict]
    evidence: list[dict]
    artifacts: list[dict]
    checks: list[dict]
    usage: dict
    revisions: dict[str, dict] = field(default_factory=dict)
    resumes: list[dict] = field(default_factory=list)
    compacted: bool = False

    @property
    def request(self) -> dict:
        return self.run["request"]

    def done(self, action: str, **match: Any) -> list[dict]:
        return [
            s
            for s in self.steps
            if s["action"] == action
            and s["status"] == "succeeded"
            and all(s["args"].get(k) == v for k, v in match.items())
        ]

    def attempted(self, action: str, **match: Any) -> list[dict]:
        return [
            s
            for s in self.steps
            if s["action"] == action and all(s["args"].get(k) == v for k, v in match.items())
        ]

    def pending(self) -> list[dict]:
        return [s for s in self.steps if s["status"] == "dispatched"]

    def observation(self, step: dict) -> dict:
        return (step.get("result") or {}).get("observation", {})

    def latest(self, kind: str) -> dict | None:
        found = [a for a in self.artifacts if a["kind"] == kind]
        return found[-1] if found else None

    def documents(self, name: str | None = None) -> list[dict]:
        return [
            a
            for a in self.artifacts
            if a["kind"] == "document" and (name is None or a["name"] == name)
        ]

    def checks_for(self, digest: str) -> list[dict]:
        return [c for c in self.checks if c["artifact_digest"] == digest]

    def failed_avenues(self) -> list[dict]:
        return [
            {
                "step": s["step_id"],
                "action": s["action"],
                "outcome": (s.get("result") or {}).get("outcome"),
            }
            for s in self.steps
            if s["status"] in ("failed", "denied")
            or (s.get("result") or {}).get("outcome") not in (None, "success", "partial")
        ]

    def remaining(self) -> dict:
        return budget.remaining(self.run["limits"], self.usage)

    def evidence_by_id(self) -> dict[str, dict]:
        return {e["id"]: e for e in self.evidence}

    def context(self, chars: int | None = None) -> tuple[str, bool]:
        """The ledger as a model reads it, compacted to ``chars`` if it must be."""
        limit = int(chars or self.request.get("context_chars", 24000))
        head = {
            "request": {
                k: self.request.get(k) for k in ("workload", "question", "audience", "purpose")
            },
            "accepted_revisions": self.revisions,
            "failed_avenues": self.failed_avenues(),
            "budget_remaining": self.remaining(),
        }
        text = json.dumps(head, sort_keys=True)
        full = [
            f"[{e['id']}] {e['method']} {json.dumps(e['locator'])}: {e['extract']}"
            for e in self.evidence
        ]
        body = "\n".join(full)
        if len(text) + len(body) <= limit:
            return text + "\n" + body, False
        compact = [
            f"[{e['id']}] {e['method']} {e['locator'].get('path', '')} "
            f"{e['locator'].get('lines', '')}: {e['extract'][:60].replace(chr(10), ' ')}…"
            for e in self.evidence
        ]
        budget_left = max(0, limit - len(text))
        kept, used = [], 0
        for line in compact:
            if used + len(line) + 1 > budget_left:
                break
            kept.append(line)
            used += len(line) + 1
        return text + "\n" + "\n".join(kept), True


def load(harness: Any, run: dict) -> Ledger:
    state = harness.state
    steps = []
    for row in state.rows(
        "SELECT * FROM harness_steps WHERE run_id=? ORDER BY created_at, step_id", (run["id"],)
    ):
        steps.append(
            {
                "step_id": row["step_id"],
                "action": row["action"],
                "identity": row["identity"],
                "args": loads(row["input_json"], {}),
                "status": row["status"],
                "required": bool(row["required"]),
                "candidate": row["candidate"],
                "effect_class": row["effect_class"],
                "current_attempt": row["current_attempt"],
                "result": loads(row["result_json"], None),
                "policy": loads(row["policy_json"], {}),
                "created_at": row["created_at"],
            }
        )
    evidence = [
        {
            "id": r["id"],
            "registration_id": r["registration_id"],
            "revision": r["revision"],
            "locator": loads(r["locator_json"], {}),
            "method": r["method"],
            "status": r["status"],
            "extract": r["extract"],
            "flags": loads(r["flags_json"], {}),
            "step_id": r["step_id"],
            "observed_at": r["observed_at"],
        }
        for r in state.rows(
            "SELECT * FROM harness_evidence WHERE run_id=? ORDER BY observed_at, id", (run["id"],)
        )
    ]
    artifacts = [
        {
            "digest": r["digest"],
            "kind": r["kind"],
            "name": r["name"],
            "media_type": r["media_type"],
            "parent_digest": r["parent_digest"],
            "step_id": r["step_id"],
            "provenance": loads(r["provenance_json"], {}),
            "review_status": r["review_status"],
            "created_at": r["created_at"],
        }
        for r in state.rows(
            "SELECT * FROM harness_artifacts WHERE run_id=? ORDER BY created_at", (run["id"],)
        )
    ]
    checks = [
        {
            "id": r["id"],
            "criterion_id": r["criterion_id"],
            "artifact_digest": r["artifact_digest"],
            "grader": r["grader"],
            "grader_version": r["grader_version"],
            "outcome": r["outcome"],
            "observed": loads(r["observed_json"], {}),
            "input_digests": loads(r["input_digests_json"], []),
            "environment": loads(r["environment_json"], {}),
            "evidence": loads(r["evidence_json"], []),
            "created_at": r["created_at"],
        }
        for r in state.rows(
            "SELECT * FROM harness_verifications WHERE run_id=? ORDER BY created_at", (run["id"],)
        )
    ]
    revisions = {}
    for step in steps:
        if step["action"] == "resolve" and step["status"] == "succeeded":
            handle = (step["result"] or {}).get("observation", {}).get("handle")
            if handle:
                revisions[handle["registration_id"]] = handle
    resumes = [
        json.loads(r["data_json"])
        for r in state.rows(
            "SELECT data_json FROM harness_events WHERE run_id=? AND type='run.resumed' ORDER BY "
            "seq",
            (run["id"],),
        )
    ]
    compacted = bool(
        state.rows(
            "SELECT 1 FROM harness_events WHERE run_id=? AND type='ledger.compacted' LIMIT 1",
            (run["id"],),
        )
    )
    return Ledger(
        run,
        steps,
        evidence,
        artifacts,
        checks,
        budget.usage(state, run["id"]),
        revisions,
        resumes,
        compacted,
    )
