"""The third plane: whether this region can be *measured by somebody else*.

`pheasant.decision`'s docstring predicted this one — "two planes need gates,
and a third eventually will". Here it is, and it asks a question neither of
the others does.

* The **evaluation** plane asks how well retrieval is doing.
* The **tuning** plane asks which retrieval stage is losing documents.
* This plane asks whether an outside harness may trust either answer: whether
  every submitted write reconciles, whether a result names the exact place it
  came from, whether an isolation boundary holds, whether a failure that moved
  a denominator is visible — and whether the region will say all of that in a
  form a machine can read *before* the experiment starts rather than after it
  has produced numbers nobody can defend.

Three parts, and the order matters.

**The contract** (`contract.py`) declares what this build supports. It is
derived from live introspection rather than written down, because a
hand-maintained capability list is a config flag with no reader: it says what
somebody believed when they last edited it. `unsupported` is a first-class
answer here — a region that cannot do something and says so is usable, and one
that claims a capability it does not have wastes a whole experiment before
anyone finds out.

**The probes** (`probes.py`) demonstrate each capability against a live region
through the real service layer. A declared capability whose probe has not run
is `declared_untested`, which is deliberately not the same word as
`supported`: the specification this implements is a list of *evidence* gaps,
and the whole point is that "pheasant probably does this" is not evidence.

**The gates** (`gates.py`) turn probe results into the four go/no-go decisions
— core, swarm, memory, tuning — through `decision.GateSet`, so a run that
evaluated no gates cannot report that it passed them.

Off by default (`readiness.enabled`), and read-only when on. A check performs
retrieval, seals a snapshot and submits to a scratch source it owns; it never
takes `sync_lock`, never writes memory, and never touches a source the region
was configured with. See ``docs/stress-test-readiness.md``.
"""

from __future__ import annotations

from pheasant.readiness.contract import CAPABILITIES, Capability, build_contract
from pheasant.readiness.gates import GATE_SETS, evaluate_gates
from pheasant.readiness.runner import run_readiness_check

__all__ = [
    "CAPABILITIES",
    "GATE_SETS",
    "Capability",
    "build_contract",
    "evaluate_gates",
    "run_readiness_check",
]
