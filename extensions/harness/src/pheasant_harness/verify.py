"""Independent verification of one exact candidate.

The verifier never runs in the editing workspace and never takes the model's
word: each criterion is a grader with an identity and a version, applied to a
candidate *digest*. A check records the digests it read, the environment it
ran in and what it observed, and its outcome is one of ``pass``, ``fail``,
``skipped`` or ``inconclusive`` -- a check that could not run is never a pass.
Command checks run in a fresh ``verifier`` sandbox built from the candidate's
tree by digest, with any held-out fixtures copied in there and only there.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from pheasant_harness.common import HarnessError, digest_bytes, digest_value, new_id, now
from pheasant_harness.render import render_print
from pheasant_harness.tools import EVIDENCE_REF, ToolContext, matches_any

GRADER_VERSION = "harness-verify-1"
STATUS_LABEL = re.compile(
    r"\((inference|proposal|unknown|assumption|generated illustration)\)", re.I
)
MATERIAL = re.compile(r"\d|\b(must|requires?|shall|never|always|only|exception|effective)\b", re.I)
ABSENCE = re.compile(
    r"\b(no (occurrences|matches|callers)|not found|does not (appear|occur)|\d+ occurrences)\b",
    re.I,
)


def _result(outcome: str, observed: dict, evidence: list | None = None) -> dict:
    return {"outcome": outcome, "observed": observed, "evidence": evidence or []}


class Verifier:
    def __init__(self, harness: Any):
        self.harness = harness

    def check(self, run: dict, criterion: dict, candidate: str, cancelled=None) -> dict:
        kind = criterion["type"]
        grader = getattr(self, "_" + kind, None)
        if grader is None:
            outcome = _result("skipped", {"reason": f"no grader for criterion type {kind!r}"})
        else:
            try:
                outcome = grader(run, criterion, candidate, cancelled)
            except HarnessError as exc:
                outcome = _result("inconclusive", {"reason": str(exc)})
        return {
            "id": new_id("chk"),
            "criterion_id": criterion["id"],
            "artifact_digest": candidate,
            "grader": f"{kind}",
            "grader_version": GRADER_VERSION,
            "input_digests": outcome.pop("inputs", [candidate]),
            "environment": outcome.pop("environment", {"where": "coordinator-side grader"}),
            **outcome,
            "created_at": now(),
        }

    # -- document graders ------------------------------------------------------

    def _text(self, candidate: str) -> str:
        if candidate.startswith("tree:"):
            raise HarnessError("this criterion reads a document; the candidate is a tree")
        return self.harness.blobs.get_text(candidate)

    def _contains(self, run, criterion, candidate, cancelled):
        text = self._text(candidate)
        needles = criterion.get("all") or [criterion.get("text")]
        missing = [n for n in needles if n and n.lower() not in text.lower()]
        return _result("fail" if missing else "pass", {"missing": missing})

    def _absent(self, run, criterion, candidate, cancelled):
        text = self._text(candidate)
        found = re.findall(criterion["pattern"], text, re.I)
        return _result("fail" if found else "pass", {"found": found[:10]})

    def _citations_resolve(self, run, criterion, candidate, cancelled):
        """Every cited evidence id exists and its extract is still at its locator."""
        text = self._text(candidate)
        cited = sorted(set(EVIDENCE_REF.findall(text)))
        context = ToolContext(self.harness, run)
        problems, checked, inconclusive = [], 0, []
        for ev_id in cited:
            rows = self.harness.state.rows(
                "SELECT * FROM harness_evidence WHERE run_id=? AND id=?", (run["id"], ev_id)
            )
            if not rows:
                problems.append({"evidence": ev_id, "problem": "no such evidence in this run"})
                continue
            row = rows[0]
            checked += 1
            locator = json.loads(row["locator_json"])
            if row["method"] not in ("read", "grep", "follow:definitions", "follow:callers"):
                continue
            try:
                reg, resolver = context.resolver(row["registration_id"])
                if not self.harness.registry.authorized(reg, run["actor"]):
                    inconclusive.append(
                        {"evidence": ev_id, "problem": "access revoked; cannot re-read"}
                    )
                    continue
                revision = self.harness.registry.revision(reg.id, row["revision"], run["actor"])
                data = resolver.read_bytes(revision, locator["path"]).decode(
                    "utf-8", errors="replace"
                )
            except HarnessError as exc:
                inconclusive.append({"evidence": ev_id, "problem": str(exc)})
                continue
            lines = data.splitlines()
            first, last = locator.get("lines", [1, len(lines)])
            span = "\n".join(lines[first - 1 : last])
            if row["extract"].strip() and row["extract"].strip()[:200] not in span:
                problems.append({"evidence": ev_id, "problem": "extract not found at locator"})
        outcome = "fail" if problems else ("inconclusive" if inconclusive else "pass")
        if not cited:
            outcome = "fail"
            problems.append({"problem": "the document cites no evidence"})
        return _result(
            outcome,
            {
                "cited": len(cited),
                "checked": checked,
                "problems": problems,
                "inconclusive": inconclusive,
            },
            cited,
        )

    def _claims_grounded(self, run, criterion, candidate, cancelled):
        """Material sentences cite evidence or say what they are (inference, proposal, unknown)."""
        text = self._text(candidate)
        section = None
        unsupported, checked, supported = [], 0, 0
        lines = text.splitlines()
        for index, raw in enumerate(lines):
            line = re.sub(r"\{w=\d+\}", "", raw).strip()
            following = lines[index + 1].strip() if index + 1 < len(lines) else ""
            if line.startswith("|") and re.match(r"^\|?\s*:?-{3,}", following):
                continue  # a table's header row names columns; it claims nothing
            if line.startswith("#"):
                section = line.lstrip("#").strip().lower()
                continue
            if line.startswith("**Question"):
                continue  # the caller's own words, restated
            if (
                not line
                or line.startswith(("|---", "![", "```", ">"))
                or section in ("coverage", "provenance", "verification", "sources", "limitations")
            ):
                continue
            for sentence in re.split(r"(?<=[.!?])\s+(?!\[E[0-9a-f]{8}\])", line):
                if not MATERIAL.search(sentence) or len(sentence) < 12:
                    continue
                checked += 1
                if EVIDENCE_REF.search(sentence) or STATUS_LABEL.search(sentence):
                    supported += 1
                else:
                    unsupported.append(sentence[:160])
        ratio = round(supported / checked, 3) if checked else None
        return _result(
            "fail" if unsupported else "pass",
            {
                "checked": checked,
                "supported": supported,
                "ratio": ratio,
                "unsupported": unsupported[:12],
            },
        )

    def _coverage_stated(self, run, criterion, candidate, cancelled):
        """An absence or count claim carries the coverage of a *complete* scan."""
        text = self._text(candidate)
        claims = [line for line in text.splitlines() if ABSENCE.search(line)]
        if not claims:
            return _result("pass", {"claims": 0})
        problems = []
        for line in claims:
            ids = EVIDENCE_REF.findall(line)
            rows = [
                self.harness.state.rows(
                    "SELECT method, status FROM harness_evidence WHERE run_id=? AND id=?",
                    (run["id"], i),
                )
                for i in ids
            ]
            complete = any(
                r and r[0]["method"] == "grep-coverage" and r[0]["status"] == "observed"
                for r in rows
            )
            if not complete:
                problems.append(line[:200])
        return _result(
            "fail" if problems else "pass", {"claims": len(claims), "uncovered": problems}
        )

    def _values_present(self, run, criterion, candidate, cancelled):
        """Machine-readable expectations survive the render a reader will see."""
        printed = render_print(self._text(candidate))
        text = printed["text"]
        missing = []
        for item in criterion.get("values", []):
            value, unit = str(item["value"]), item.get("unit") or ""
            row_ok = any(
                value in line and (not unit or unit in line or f"({unit})" in text)
                for line in text.splitlines()
            )
            if not row_ok:
                missing.append(f"{value} {unit}".strip())
        clipped = [c["text"] for c in printed["clipped"]]
        outcome = "fail" if missing or clipped else "pass"
        render_digest = digest_bytes(text.encode())
        return {
            **_result(outcome, {"missing": missing, "clipped": clipped, "render": render_digest}),
            "inputs": [candidate, render_digest],
            "environment": {"renderer": printed["renderer"], "profile": "print"},
        }

    def _cites_registration(self, run, criterion, candidate, cancelled):
        text = self._text(candidate)
        wanted = criterion["registration_id"]
        ids = set(EVIDENCE_REF.findall(text))
        rows = self.harness.state.rows(
            "SELECT id FROM harness_evidence WHERE run_id=? AND registration_id=?",
            (run["id"], wanted),
        )
        hits = ids & {r["id"] for r in rows}
        return _result("pass" if hits else "fail", {"citations_from_registration": sorted(hits)})

    def _render_ok(self, run, criterion, candidate, cancelled):
        from pheasant_harness import render

        page = render.render_html(self._text(candidate))
        shot = render.render_screenshot(page, tuple(criterion.get("viewport", (1024, 768))))
        if shot.get("status") != "rendered":
            return _result("skipped", {"reason": shot.get("reason")})
        png = self.harness.blobs.put(shot["png"], "image/png")
        return {
            **_result(
                "fail" if shot["overflow"] else "pass",
                {"overflow": shot["overflow"], "screenshot": png},
            ),
            "inputs": [candidate, png],
            "environment": {"renderer": shot["renderer"], "viewport": shot["viewport"]},
        }

    # -- tree graders ----------------------------------------------------------

    def _tree(self, run, candidate):
        if not candidate.startswith("tree:"):
            raise HarnessError("this criterion runs code; the candidate is a document")
        manifest = json.loads(self.harness.blobs.get_text(candidate.removeprefix("tree:")))
        return manifest, ToolContext(self.harness, run).tree_files(manifest)

    def _diff_scope(self, run, criterion, candidate, cancelled):
        manifest, _ = self._tree(run, candidate)
        changed = sorted(manifest["files"])
        allowed = criterion.get("allowed") or ["**"]
        protected = criterion.get("protected") or []
        outside = [p for p in changed if not matches_any(p, allowed)]
        weakened = [p for p in changed if matches_any(p, protected)]
        return _result(
            "fail" if outside or weakened else "pass",
            {"changed": changed, "outside_scope": outside, "protected_modified": weakened},
        )

    def _command(self, run, criterion, candidate, cancelled):
        manifest, files = self._tree(run, candidate)
        sandboxes = self.harness.sandboxes
        sandbox = sandboxes.allocate(run["id"], run["tenant"], "verifier")
        try:
            fixture_files = {}
            for reg_id in criterion.get("fixtures", []):
                if reg_id not in (run["request"].get("fixtures") or []):
                    raise HarnessError(f"{reg_id} is not a fixture of this run")
                reg = self.harness.registry.get(reg_id)
                resolver = self.harness.registry.resolver(reg)
                # Fixtures are read with the operator's authority: held out from the agent.
                revision = resolver.resolve(None)
                for entry in revision.entries:
                    if entry["kind"] == "file":
                        fixture_files[entry["path"]] = resolver.read_bytes(revision, entry["path"])
            materialized = sandboxes.materialize(sandbox, {**files, **fixture_files})
            outcome = sandboxes.execute(
                sandbox,
                list(criterion["argv"]),
                timeout=criterion.get("timeout_seconds"),
                cancelled=cancelled,
            )
        finally:
            sandboxes.destroy(sandbox.generation, "verification finished")
        expected = int(criterion.get("expect_exit", 0))
        passed = outcome["exit_status"] == expected and not outcome["terminated"]
        return {
            **_result(
                "pass"
                if passed
                else ("inconclusive" if outcome["terminated"] == "cancelled" else "fail"),
                {
                    "exit_status": outcome["exit_status"],
                    "terminated": outcome["terminated"],
                    "stdout_tail": outcome["stdout"][-1500:],
                    "stderr_tail": outcome["stderr"][-1500:],
                    "stdout_ref": outcome["stdout_ref"],
                    "fixtures": sorted(fixture_files),
                },
            ),
            "inputs": [candidate, materialized["digest"]],
            "environment": outcome["environment"],
        }

    def _ui_render(self, run, criterion, candidate, cancelled):
        """Render the implemented UI through its real entry point and look at it.

        ``argv`` runs in a fresh verifier sandbox and prints the page; the page
        is opened in a browser at the declared viewport and the expected text
        must be visible without overflow. With no browser the check is skipped.
        """
        from pheasant_harness import render

        produced = self._command(run, {**criterion, "expect_exit": 0}, candidate, cancelled)
        if produced["outcome"] != "pass":
            return produced
        page = produced["observed"]["stdout_tail"]
        missing = [t for t in criterion.get("expect_text", []) if t not in page]
        shot = render.render_screenshot(page, tuple(criterion.get("viewport", (1024, 640))))
        if shot.get("status") != "rendered":
            return {
                **_result(
                    "skipped" if not missing else "fail",
                    {"reason": shot.get("reason"), "missing": missing},
                ),
                "inputs": produced["inputs"],
                "environment": produced["environment"],
            }
        png = self.harness.blobs.put(shot["png"], "image/png")
        ok = not missing and not shot["overflow"]
        return {
            **_result(
                "pass" if ok else "fail",
                {
                    "missing": missing,
                    "overflow": shot["overflow"],
                    "screenshot": png,
                    "viewport": shot["viewport"],
                },
            ),
            "inputs": [*produced["inputs"], png],
            "environment": {**produced["environment"], "renderer": shot["renderer"]},
        }

    def _staging_contract(self, run, criterion, candidate, cancelled):
        url = criterion["url"]
        try:
            with urllib.request.urlopen(
                url, timeout=float(criterion.get("timeout_seconds", 2))
            ) as reply:  # noqa: S310
                status = reply.status
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return _result("inconclusive", {"reason": f"staging service unreachable: {exc}"})
        return _result(
            "pass" if status == int(criterion.get("expect_status", 200)) else "fail",
            {"status": status},
        )


def verdict(checks: list[dict], criteria: list[dict], candidate: str) -> dict:
    """Whether every required criterion has a passing check *on this candidate*."""
    latest: dict[str, dict] = {}
    for check in checks:
        if check["artifact_digest"] == candidate:
            latest[check["criterion_id"]] = check
    required = [c for c in criteria if c.get("required", True)]
    passing = [c["id"] for c in required if latest.get(c["id"], {}).get("outcome") == "pass"]
    failing = [c["id"] for c in required if latest.get(c["id"], {}).get("outcome") == "fail"]
    unverified = [
        c["id"]
        for c in required
        if latest.get(c["id"], {}).get("outcome") in (None, "skipped", "inconclusive")
    ]
    optional_gaps = [
        c["id"]
        for c in criteria
        if not c.get("required", True) and latest.get(c["id"], {}).get("outcome") != "pass"
    ]
    return {
        "candidate": candidate,
        "verified": bool(required) and len(passing) == len(required),
        "passing": passing,
        "failing": failing,
        "unverified": unverified,
        "optional_gaps": optional_gaps,
        "digest": digest_value(sorted(latest.items())),
    }
