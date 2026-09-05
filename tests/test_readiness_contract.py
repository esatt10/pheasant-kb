"""The contract cannot rot, and the gates cannot be vacuously true.

Two properties, and both are structural rather than remembered.

The capability table names live symbols, MCP tools and gap ids. Every one of
those is a thing that can be renamed by a change that has nothing to do with
this plane, and a contract that names a symbol nothing provides is worse than
no contract: a harness reads it, believes it, and builds an experiment on an
operation that does not exist. Three such names shipped in the plane's first
version and were caught by running a check rather than by review, which is
what these assertions make cheap.

The gate half exists because `all([])` is `True` and this codebase has paid
for that twice — once in the evaluation plane, once in the tuning plane's own
copy of the guard. Here it is checked at both levels: a gate set built from
nothing is `None`, and a gate set whose gates were *skipped* does not report
that it passed them.
"""

from __future__ import annotations

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.decision import EmptyGateSet, GateSet
from pheasant.readiness.contract import CAPABILITIES, build_contract, resolve
from pheasant.readiness.gates import GATE_SETS, evaluate_gates
from pheasant.readiness.probes import PROBE_ORDER, PROBES, ProbeResult


@pytest.mark.parametrize("capability", CAPABILITIES, ids=lambda cap: cap.logical)
def test_every_declared_implementation_exists(capability) -> None:
    """A supported capability names something this build actually has.

    The one assertion that would have caught `MCPServer`, `Bundle` and
    `SourceType.DOCUMENT_FOLDER` before a run did. An `unsupported` row is
    exempt by construction: it names no implementation, which is what makes
    "we cannot do this" representable rather than a gap in the table.
    """

    if capability.unsupported_reason:
        assert not capability.implementation, (
            f"{capability.logical} is declared unsupported and also names an "
            "implementation. Pick one: a capability that exists is not unsupported."
        )
        return
    missing = [path for path in capability.implementation if resolve(path) is None]
    assert not missing, f"{capability.logical} names symbols nothing provides: {missing}"


@pytest.mark.parametrize("capability", CAPABILITIES, ids=lambda cap: cap.logical)
def test_every_declared_mcp_tool_is_on_the_facade(capability) -> None:
    """A contracted tool name is a promise a harness hard-codes against.

    Rule 8 makes the tool surface public API, so this is the freshness half of
    that rule for the readiness contract specifically: a renamed tool has to
    break here rather than in somebody's preflight.
    """

    from pheasant.mcp_server.tools import PheasantTools

    missing = [name for name in capability.mcp_tools if not hasattr(PheasantTools, name)]
    assert not missing, f"{capability.logical} names tools the facade lacks: {missing}"


@pytest.mark.parametrize("capability", CAPABILITIES, ids=lambda cap: cap.logical)
def test_every_declared_probe_is_registered(capability) -> None:
    missing = [name for name in capability.probes if name not in PROBES]
    assert not missing, f"{capability.logical} names probes that do not exist: {missing}"


def test_every_registered_probe_runs_in_a_check() -> None:
    """A probe nobody runs is a probe that rots.

    Same argument as an index with no reader: it looks like the mechanism, and
    the next person to ask "how is this demonstrated" finds the wrong answer.
    """

    assert set(PROBES) == set(PROBE_ORDER)


def test_every_gate_names_probes_that_exist() -> None:
    for gate_set, specs in GATE_SETS.items():
        for spec in specs:
            missing = [name for name in spec.probes if name not in PROBES]
            assert not missing, f"{gate_set}.{spec.gate_id} names unknown probes: {missing}"


def test_the_contract_digest_ignores_probe_results() -> None:
    """A capability snapshot detects the region changing *shape*.

    A digest that moved because a latency probe was a millisecond slower would
    make that detection useless — which is the whole reason a harness pins one.
    """

    config = PheasantConfig.model_validate({"pheasant": {"name": "digest"}})
    bare = build_contract(config)
    with_probes = build_contract(config, proven={"search_latency": True})
    assert bare["digest"] == with_probes["digest"]


def test_a_capability_with_no_probe_is_never_proven() -> None:
    """`supported` and `proven` are different claims, and stay different.

    The specification's gaps are *evidence* gaps. "The code is present" has
    never been evidence that it works in the deployment about to be measured,
    and a table that conflated them would report exactly the confidence the
    document was written to withhold.
    """

    config = PheasantConfig.model_validate({"pheasant": {"name": "status"}})
    rows = {row["logical"]: row for row in build_contract(config)["capabilities"]}
    for capability in CAPABILITIES:
        if capability.probes or capability.unsupported_reason:
            continue
        assert rows[capability.logical]["status"] == "supported"


def test_an_unrun_capability_reports_declared_untested() -> None:
    config = PheasantConfig.model_validate({"pheasant": {"name": "untested"}})
    rows = {row["logical"]: row for row in build_contract(config)["capabilities"]}
    assert rows["search"]["status"] == "declared_untested"

    proven = build_contract(config, proven={"retrieval_lineage": True, "search_latency": True})
    rows = {row["logical"]: row for row in proven["capabilities"]}
    assert rows["search"]["status"] == "proven"


def test_the_refusal_code_table_is_derived_from_the_exception_classes() -> None:
    """Published because MCP cannot carry a code, so it must not be typed twice.

    An agent maps a refusal's text onto this table. A hand-written copy would
    be a second spelling of the thing the table exists to make canonical.
    """

    config = PheasantConfig.model_validate({"pheasant": {"name": "codes"}})
    codes = {row["code"] for row in build_contract(config)["refusal_codes"]}
    assert {"UNKNOWN_SOURCE", "SNAPSHOT_DRIFTED", "CORPUS_DENYLISTED", "REGION_BUSY"} <= codes


# ---------------------------------------------------------------------------
# Vacuous truth, at both levels
# ---------------------------------------------------------------------------


def test_a_gate_set_cannot_be_built_from_nothing() -> None:
    with pytest.raises(EmptyGateSet):
        GateSet([])


def test_a_gate_set_whose_probes_all_skipped_is_none_not_passing() -> None:
    """ "No gates were evaluated" has no `passed` to misread.

    The evaluation plane's interrupted-run incident in one line: a skipped run
    carried no gates, `all([])` is True, and it reported that it passed them —
    straight into an exit status.
    """

    skipped = ProbeResult("memory_export", None, "skipped", skipped_reason="memory is off")
    gates, not_evaluated = evaluate_gates("memory", {"memory_export": skipped})
    assert gates is None
    assert any(row["gate_id"] == "memory_auditable" for row in not_evaluated)
    assert "memory is off" in str(not_evaluated)


def test_a_partly_skipped_gate_set_does_not_report_passing() -> None:
    """The same bug one level up, and the one the demo region caught.

    Memory reported PASS with three of four gates never evaluated, because
    memory and ACL enforcement were both off. The specification's own rule is
    "any unchecked item is a NO-GO for scored results", so a verdict is
    tri-state: `True` only when every gate in the set was evaluated and passed.
    """

    from pheasant.readiness.runner import _verdict

    passing = ProbeResult("memory_state_reported", True, "ok")
    gates, skipped = evaluate_gates("memory", {"memory_state_reported": passing})
    assert gates is not None
    assert gates.passed is True
    assert skipped
    assert _verdict(gates, skipped) is None


def test_a_failure_stands_even_when_other_gates_were_skipped() -> None:
    """A failure is not made provisional by a gap beside it."""

    from pheasant.readiness.runner import _verdict

    failing = ProbeResult("memory_state_reported", False, "no memory state reported")
    gates, skipped = evaluate_gates("memory", {"memory_state_reported": failing})
    assert _verdict(gates, skipped) is False


# ---------------------------------------------------------------------------
# The contamination probe's derived path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "patterns",
    [
        ("benchmark/*",),
        ("**/*.json",),
        ("*.answers.json",),
        ("eval/**",),
        ("benchmark/*", "*.answers.json"),
    ],
)
def test_the_contamination_probe_derives_a_path_that_is_actually_refused(
    patterns: tuple[str, ...],
) -> None:
    """A false failure is the worst thing a gate can produce.

    The probe submits a path derived from the operator's own patterns, because
    a hardcoded `benchmark/answers.md` proves nothing against a denylist of
    `eval-*.json`. Deriving a filename from a glob is a *guess*, though —
    `**/*.json` un-globs to `readiness.json`, which `fnmatch` then does not
    match, because `**` is not path-aware there. So the derived candidate is
    verified against the same predicate the write path uses, and this asserts
    that it comes back refused rather than merely plausible.
    """

    from pheasant.readiness.probes.ingestion import _first_matching_name
    from pheasant.services.errors import ContaminationRefused
    from pheasant.services.ingestion import check_denylist

    candidate = _first_matching_name(patterns)
    with pytest.raises(ContaminationRefused):
        check_denylist(candidate, patterns)


def test_an_underivable_denylist_skips_rather_than_failing() -> None:
    """ "This check could not exercise the boundary" is not "the boundary is broken".

    An exotic pattern the probe cannot construct a path for is a gap in the
    check, and reporting it as a failure would send an operator to fix a region
    that is behaving correctly.
    """

    from pheasant.readiness.probes import NotRunnable
    from pheasant.readiness.probes.ingestion import _first_matching_name

    with pytest.raises(NotRunnable):
        _first_matching_name(("[!a-z]",))
