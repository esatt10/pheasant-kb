"""Executable demonstrations, one per capability the specification asks about.

Every gap in ``docs/stress-test-readiness.md`` is an *evidence* gap: not "we
think pheasant cannot do this" but "nothing here has shown that it does, in
this deployment, against this corpus". A probe is what converts one into
evidence — or, when it fails, into a finding with a reproduction attached.

Three rules the probes hold to, and each of them cost something to learn
elsewhere in this codebase.

**A probe drives the real path.** Every one of these calls the service layer
the transports call — not a helper written for the check, and not a mock.
`tests/test_surface_conformance.py` exists because two implementations of one
operation drift; a probe that exercised a third would be measuring itself.

**A probe that cannot run says so.** ``skipped`` is a first-class result with
a reason, and it is *not* a pass. A check that quietly treats an unrunnable
probe as satisfied is the `all([])` bug wearing different clothes — and the
gate layer refuses to build a `GateSet` out of nothing precisely so it cannot
recur here.

**A probe cleans up after itself, and is honest when it cannot.** Each one
works in a scratch source it owns (``__readiness__``-prefixed), never in a
source the region was configured with. What it cannot undo — a sealed
snapshot, a receipt — is append-only evidence that a check ran, which is the
correct thing for it to leave behind.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pheasant.services import ServiceContext

logger = logging.getLogger(__name__)

#: Source name every probe writes into. Prefixed so it is recognisable in a
#: source list, and fixed so repeated checks reuse one scratch area rather than
#: leaving a source per run.
PROBE_SOURCE = "__readiness__probe"

#: A document whose text is distinctive enough that retrieving it proves
#: retrieval rather than proving that the corpus has one document. Deliberately
#: not lorem ipsum: a probe that searches for a word appearing in real content
#: cannot tell a hit on its own document from a hit on the corpus.
PROBE_MARKER = "zarquon-quilfeather"


@dataclass
class ProbeResult:
    """What one probe established, and how to argue with it."""

    probe_id: str
    passed: bool | None
    summary: str
    #: The numbers the summary is a sentence about. A reader has to be able to
    #: disagree with the conclusion without re-running the check.
    observed: dict[str, Any] = field(default_factory=dict)
    #: Why a probe did not run. Set exactly when ``passed`` is None.
    skipped_reason: str = ""
    duration_ms: float = 0.0

    @property
    def ran(self) -> bool:
        return self.passed is not None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "probe_id": self.probe_id,
            "passed": self.passed,
            "summary": self.summary,
            "observed": self.observed,
            "duration_ms": round(self.duration_ms, 3),
        }
        if self.skipped_reason:
            payload["skipped_reason"] = self.skipped_reason
        return payload


@dataclass
class ProbeContext:
    """What a probe is given, plus the one thing it may do to the region.

    ``sync`` is passed in rather than reached for because a readiness check
    running inside an API process must not import the sync engine to index four
    documents, and one running from the CLI already holds an engine. Handing
    the callable over lets the same probe body serve both, and lets a caller
    that genuinely cannot index — an api replica with no engine — report
    ``skipped`` instead of failing a gate it had no way to satisfy.
    """

    services: ServiceContext
    sync: Callable[[str], Any] | None = None
    run_id: str = ""

    @property
    def config(self) -> Any:
        return self.services.config

    @property
    def state(self) -> Any:
        return self.services.state

    def settings(self) -> Any:
        return getattr(self.config, "readiness", None)


PROBES: dict[str, Callable[[ProbeContext], ProbeResult]] = {}


def probe(probe_id: str) -> Callable[[Callable[..., ProbeResult]], Callable[..., ProbeResult]]:
    """Register a probe, and give it uniform timing and failure handling.

    An exception inside a probe becomes a *failed* probe rather than a failed
    check. That is deliberate and is the opposite of what a test should do: a
    readiness run's whole output is a report of what does and does not work,
    and one probe raising must not deny a reader the other nineteen answers.
    """

    def decorate(fn: Callable[..., ProbeResult]) -> Callable[..., ProbeResult]:
        def wrapper(context: ProbeContext) -> ProbeResult:
            started = time.perf_counter()
            try:
                result = fn(context)
            except NotRunnable as exc:
                result = ProbeResult(probe_id, None, str(exc), skipped_reason=str(exc))
            except Exception as exc:  # noqa: BLE001 - a crash is a finding
                logger.warning("readiness: probe %s raised", probe_id, exc_info=True)
                result = ProbeResult(
                    probe_id,
                    False,
                    f"probe raised {type(exc).__name__}: {exc}",
                    observed={"exception": type(exc).__name__},
                )
            result.duration_ms = (time.perf_counter() - started) * 1000.0
            return result

        PROBES[probe_id] = wrapper
        return wrapper

    return decorate


class NotRunnable(RuntimeError):
    """This probe cannot run here, and that is not a failure.

    Distinct from an ordinary exception so a missing prerequisite — no sync
    callable, no memory configured, no vector index — reports ``skipped`` with
    a reason rather than ``failed``. Reporting it as a failure would make a
    correctly-configured region look broken; reporting it as a pass would be
    the vacuous-truth bug this whole plane is careful about.
    """


#: Probes in the order a run executes them. Order is load-bearing exactly once:
#: `snapshot_drift_refused` ingests, so it runs after everything that wants a
#: stable corpus. The rest are independent and are ordered to read well in a
#: report.
PROBE_ORDER: tuple[str, ...] = (
    "service_identity",
    "mcp_discovery",
    "ingest_roundtrip",
    "idempotent_write",
    "partial_failure",
    "index_barrier",
    "reconciliation",
    "contamination_refused",
    "resolve_result",
    "retrieval_lineage",
    "memory_state_reported",
    "memory_export",
    "memory_as_of",
    "principal_isolation",
    "structured_errors",
    "search_latency",
    "concurrent_writers",
    "snapshot_seal",
    "snapshot_drift_refused",
)


def run_probes(context: ProbeContext, only: tuple[str, ...] | None = None) -> list[ProbeResult]:
    """Every probe, in order, each isolated from the others' failures."""

    selected = only or PROBE_ORDER
    unknown = [name for name in selected if name not in PROBES]
    if unknown:
        raise KeyError(f"unknown probes: {', '.join(unknown)}")
    return [PROBES[name](context) for name in selected]


def probe_source_id(config: Any) -> str:
    """The scratch source's id, so a caller can register or drop it."""

    return f"{getattr(getattr(config, 'pheasant', None), 'name', 'pheasant')}:{PROBE_SOURCE}"


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


# Importing the probe modules is what registers them. Deliberately at the
# bottom and deliberately not lazy: `PROBE_ORDER` above names every probe, and
# `run_probes` refuses an unknown name — so a module that failed to import
# would turn a missing probe into a `KeyError` at check time rather than a
# silently shorter run. The import cycle is avoided the ordinary way: each
# submodule imports the framework names it needs from this module, which is
# fully defined by the time these lines execute.
from pheasant.readiness.probes import identity as _identity  # noqa: E402,F401
from pheasant.readiness.probes import ingestion as _ingestion  # noqa: E402,F401
from pheasant.readiness.probes import integrity as _integrity  # noqa: E402,F401
from pheasant.readiness.probes import retrieval as _retrieval  # noqa: E402,F401
