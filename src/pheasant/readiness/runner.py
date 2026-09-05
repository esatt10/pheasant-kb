"""One readiness check: probe the region, gate the results, report the verdict.

The output is a document somebody makes a decision from, so its shape follows
the decision rather than the code. Three things a reader needs, in this order:

1. **The verdict**, per gate set, with the blocking failures named. Not a
   score — a list of what is in the way.
2. **The gates**, each with the probes that decided it and the numbers those
   probes observed, so the verdict can be argued with rather than believed.
3. **The contract**, so a harness can see what this build supports at all, and
   pin its digest to detect the region changing shape underneath it.

**A check performs real work and says which.** It submits documents to a
scratch source it owns, indexes them, seals snapshots and runs searches. It
never writes to a configured source, never takes ``sync_lock``, and never
writes memory. What it leaves behind — receipts, sealed snapshots — is
append-only evidence that a check ran, which is the right residue.

**Progress is not a process.** The verdict is returned rather than stored: a
readiness check is seconds of work, not the minutes an evaluation batch takes,
so the row-and-heartbeat machinery the evaluation plane needs would be cost
without a case. If that changes — a check that indexes a real corpus rather
than four probe documents — the shape to copy is `evaluation_runs`, and the
reason is written down there.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pheasant.readiness.contract import build_contract
from pheasant.readiness.gates import GATE_SETS, evaluate_gates
from pheasant.readiness.probes import (
    PROBE_ORDER,
    ProbeContext,
    ProbeResult,
    new_run_id,
    run_probes,
)
from pheasant.services import ServiceContext

logger = logging.getLogger(__name__)

#: Gate sets a check evaluates unless told otherwise. All four, because the
#: cost of evaluating a gate set you did not need is a paragraph in a report,
#: and the cost of *not* evaluating one is discovering at experiment time that
#: memory isolation was never checked.
DEFAULT_GATE_SETS = tuple(GATE_SETS)


def run_readiness_check(
    services: ServiceContext,
    *,
    sync: Callable[[str], Any] | None = None,
    gate_sets: tuple[str, ...] = DEFAULT_GATE_SETS,
    probes: tuple[str, ...] = PROBE_ORDER,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run the probes, evaluate the gates, and return the whole report."""

    context = ProbeContext(services=services, sync=sync, run_id=run_id or new_run_id())
    results = run_probes(context, probes)
    by_id: dict[str, ProbeResult] = {result.probe_id: result for result in results}

    verdicts: dict[str, Any] = {}
    for name in gate_sets:
        gates, skipped = evaluate_gates(name, by_id)
        verdicts[name] = {
            # Tri-state, and the third value is the one that matters.
            #
            # `False` when an evaluated gate failed — that verdict stands
            # whatever else was skipped, because a failure is not made
            # provisional by a gap beside it.
            #
            # `None` when every evaluated gate passed and something was
            # skipped. The first version of this returned `True` there, and the
            # demo region caught it immediately: memory reported PASS with
            # three of its four gates never evaluated, because memory and ACL
            # enforcement were both off. That is the specification's own rule —
            # "any unchecked item is a NO-GO for scored results" — and it is
            # the `all([])` shape one level up, where the empty set is not the
            # gate list but the part of it nobody could run.
            #
            # `True` only when every gate in the set was evaluated and passed.
            "passed": _verdict(gates, skipped),
            "evaluated": 0 if gates is None else len(gates),
            "blocking_failures": [] if gates is None else gates.blocking_failures,
            "gates": [] if gates is None else gates.as_dicts(),
            "skipped": skipped,
        }

    proven = {result.probe_id: bool(result.passed) for result in results if result.ran}
    return {
        "run_id": context.run_id,
        "checked_at": datetime.now(tz=UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "knowledge_base": services.knowledge_base(None),
        "verdict": _overall(verdicts),
        "gate_sets": verdicts,
        "probes": [result.as_dict() for result in results],
        "contract": build_contract(services.config, proven=proven),
        "summary": _summarise(verdicts, results),
    }


def _verdict(gates: Any, skipped: list[dict[str, Any]]) -> bool | None:
    """One gate set's answer: passed, failed, or not fully evaluated."""

    if gates is None:
        return None
    if not gates.passed:
        return False
    return None if skipped else True


def _now() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _overall(verdicts: dict[str, Any]) -> str:
    """One word, and it is never optimistic.

    ``core`` is the gate the specification makes the primary decision on, so a
    core failure is ``no-go`` whatever the other three say. A core gate set
    that could not be evaluated is ``incomplete``, not ``go``: the difference
    between "we checked and it is fine" and "we did not check" is the entire
    subject of the document this implements.
    """

    core = verdicts.get("core") or {}
    if core.get("passed") is None:
        return "incomplete"
    if not core.get("passed"):
        return "no-go"
    unevaluated = [name for name, row in verdicts.items() if row.get("passed") is None]
    if any(row.get("passed") is False for row in verdicts.values()):
        return "core-only"
    return "incomplete-beyond-core" if unevaluated else "go"


def _summarise(verdicts: dict[str, Any], results: list[ProbeResult]) -> dict[str, Any]:
    ran = [result for result in results if result.ran]
    return {
        "probes_run": len(ran),
        "probes_passed": sum(1 for result in ran if result.passed),
        "probes_failed": sorted(result.probe_id for result in ran if not result.passed),
        "probes_skipped": {
            result.probe_id: result.skipped_reason for result in results if not result.ran
        },
        "gate_sets_passed": sorted(
            name for name, row in verdicts.items() if row.get("passed") is True
        ),
        "gate_sets_failed": sorted(
            name for name, row in verdicts.items() if row.get("passed") is False
        ),
        "gate_sets_incomplete": sorted(
            name for name, row in verdicts.items() if row.get("passed") is None
        ),
    }


def render_report(report: dict[str, Any]) -> str:
    """The report as Markdown, for a terminal and for a pull request.

    Failures and skips first. A reader opening this has a decision to make, and
    the twelve gates that passed are not what they are here for.
    """

    lines = [
        f"# Readiness — {report['knowledge_base']}",
        "",
        f"**Verdict: {report['verdict'].upper()}** · run `{report['run_id']}` · "
        f"{report['checked_at']} · contract `{report['contract']['digest']}`",
        "",
    ]
    summary = report["summary"]
    lines += [
        f"{summary['probes_passed']}/{summary['probes_run']} probes passed, "
        f"{len(summary['probes_skipped'])} skipped.",
        "",
    ]

    for name, row in report["gate_sets"].items():
        if row["passed"] is True:
            state = "PASS"
        elif row["passed"] is False:
            state = "FAIL"
        elif row["evaluated"]:
            # Not "PASS with caveats". A reader scanning headings has to see
            # that this gate set did not finish, or the caveat is the line
            # they skip.
            state = f"INCOMPLETE ({row['evaluated']} of "
            state += f"{row['evaluated'] + len(row['skipped'])} gates evaluated)"
        else:
            state = "NOT EVALUATED"
        lines.append(f"## {name} — {state}")
        lines.append("")
        for gate in row["gates"]:
            mark = "x" if gate["passed"] else " "
            lines.append(f"- [{mark}] `{gate['gate_id']}` — {gate['summary']}")
        for skip in row["skipped"]:
            lines.append(f"- [~] `{skip['gate_id']}` — not evaluated: {skip['reason']}")
        lines.append("")

    failing = [probe for probe in report["probes"] if probe["passed"] is False]
    if failing:
        lines += ["## Failing probes", ""]
        for probe in failing:
            lines.append(f"### `{probe['probe_id']}`")
            lines.append("")
            lines.append(probe["summary"])
            lines.append("")
            for key, value in sorted(probe["observed"].items()):
                lines.append(f"- {key}: `{value}`")
            lines.append("")

    unsupported = report["contract"]["unsupported"]
    if unsupported:
        lines += ["## Declared unsupported", ""]
        for capability in report["contract"]["capabilities"]:
            if capability["status"] != "unsupported":
                continue
            lines.append(
                f"- **{capability['logical']}** ({capability['gap']}) — "
                f"{capability.get('detail') or capability['summary']}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
