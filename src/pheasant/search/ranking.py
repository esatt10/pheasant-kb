"""The retrieval parameters that ranking actually reads.

Every number in here used to be a module constant inside
:mod:`pheasant.search.text_store` or :mod:`pheasant.search.hybrid`, baked
into an SQL f-string or a fusion loop. That was fine while they were the
output of a one-off tuning pass; it stopped being fine the moment the region
grew a plane whose whole job is to *propose better values* for them. A
parameter nothing can address is a parameter nothing can tune.

Three properties this module owes its callers.

**The defaults are the old constants, exactly.** ``DEFAULT_RANKING`` reproduces
the 2026-08-03 retrieval overhaul's measured values -- 8/3/2/1 column weights,
a 0.05 depth prior, RRF at 60 -- so a region that configures nothing ranks
character-for-character as it did before this module existed.
``tests/test_ranking_parameters.py`` pins that by comparing the generated SQL
against the literal strings the constants produced.

**Resolution is fleet-scoped, and deliberately.** Parameters come from
``search.ranking`` in the config file, overlaid by the **active tuning
bundle** -- one row per knowledge base in ``/state``. There is no per-request
and no per-principal override, and there is no API to add one: a ranking
parameter that varied by caller would make two agents disagree about what the
region contains, and would make every measurement in the evaluation plane a
measurement of whoever happened to ask. A bundle applies to the region, so
every replica reading that ``/state`` converges on it.

**The overlay is polled, not pushed.** ``RankingResolver`` re-reads the active
bundle on a TTL (one indexed single-row ``SELECT``, default every 30s), which
is what lets a fleet of API replicas pick up an applied bundle without a
rolling restart -- the same problem, and the same shape of answer, as
``RolePolicy.refreshes_graph`` reloading a graph another process wrote.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any

logger = logging.getLogger(__name__)

#: How long a resolver serves a cached overlay before re-reading ``/state``.
#: Long enough that the read is invisible next to the search it precedes,
#: short enough that applying a bundle converges a fleet within one health
#: check rather than one deployment.
OVERLAY_TTL_SECONDS = 30.0

#: Names a bundle overlay may set, with the stage each one acts on. The stage
#: is not decoration: the tuning plane proposes a parameter only when the
#: diagnosis blames that parameter's stage, and this mapping is where that
#: association is declared once rather than restated per module.
PARAMETER_STAGES: dict[str, str] = {
    "title_weight": "lexical_candidates",
    "path_weight": "lexical_candidates",
    "heading_weight": "lexical_candidates",
    "text_weight": "lexical_candidates",
    "depth_prior": "structural_prior",
    "test_prior": "structural_prior",
    "sample_prior": "structural_prior",
    "prefer_bonus": "structural_prior",
    "prior_floor": "structural_prior",
    "rrf_k": "fusion",
    "text_arm_weight": "fusion",
    "vector_arm_weight": "fusion",
    "graph_arm_weight": "fusion",
    "filter_overfetch": "filters",
}

#: Bounds every parameter is clamped to, whatever a config file or a proposed
#: bundle says. These are not taste: each one is a value past which ranking
#: stops being ranking. A non-positive ``prior_floor`` inverts the divisor and
#: reverses the whole result list; ``rrf_k`` at zero makes rank one worth
#: infinitely more than rank two, which is score-merging by another name.
BOUNDS: dict[str, tuple[float, float]] = {
    "title_weight": (0.0, 64.0),
    "path_weight": (0.0, 64.0),
    "heading_weight": (0.0, 64.0),
    "text_weight": (0.0, 64.0),
    "depth_prior": (0.0, 2.0),
    "test_prior": (0.0, 8.0),
    "sample_prior": (0.0, 8.0),
    "prefer_bonus": (0.0, 4.0),
    "prior_floor": (0.01, 4.0),
    "rrf_k": (1.0, 1000.0),
    "text_arm_weight": (0.0, 8.0),
    "vector_arm_weight": (0.0, 8.0),
    "graph_arm_weight": (0.0, 8.0),
    "filter_overfetch": (1.0, 10.0),
}


#: The default over-fetch factor, exported so a module that must take it as a
#: parameter (the vector arm, which is called with it rather than resolving it)
#: has a default without writing the number down a second time.
DEFAULT_FILTER_OVERFETCH = 3.0


def clamp(name: str, value: float) -> float:
    low, high = BOUNDS.get(name, (float("-inf"), float("inf")))
    return min(max(float(value), low), high)


@dataclass(frozen=True)
class RankingParameters:
    """One complete, self-consistent ranking configuration.

    Frozen because it is read on the search path from several threads at once
    and because a trial's identity is its parameter values: a mutable point
    would let a result be attributed to numbers that are no longer the ones it
    ran under.
    """

    #: BM25 column weights. ``title`` holds the file's basename, which is why
    #: it outweighs ``text`` eightfold -- see ``SearchStore._fts_title``.
    title_weight: float = 8.0
    path_weight: float = 3.0
    heading_weight: float = 2.0
    text_weight: float = 1.0
    #: Structural priors, applied as a divisor on the (negative) BM25 cost.
    depth_prior: float = 0.05
    test_prior: float = 0.60
    sample_prior: float = 0.30
    prefer_bonus: float = 0.35
    prior_floor: float = 0.25
    #: Reciprocal-rank-fusion constant, from the original RRF paper.
    rrf_k: float = 60.0
    #: Per-arm fusion weights. All 1.0 is unweighted RRF, which is what the
    #: fusion did before these existed. They are here because the *diagnosis*
    #: can distinguish "the vector arm never had it" from "the vector arm had
    #: it and fusion buried it", and only the second is a fusion problem.
    text_arm_weight: float = 1.0
    vector_arm_weight: float = 1.0
    graph_arm_weight: float = 1.0
    #: How far past ``max_results`` the arms fetch when a filter will remove
    #: candidates afterwards — ACL, memory policy, section, *and* the
    #: retrieval criteria the surfaces apply (`exclude_sources`, `node_types`,
    #: `min_score`, `source_types`). One parameter, because it is one
    #: behaviour: a post-filter is going to drop rows, so ask for more.
    #:
    #: It did not used to be. The surfaces each carried their own hardcoded
    #: `max_results * 4`, so raising this moved the ACL/section/memory
    #: over-fetch and did nothing for criteria — while the tuning glossary told
    #: an operator that a `filters` miss might mean "filter_overfetch is too
    #: small". A bundle could be promoted on a parameter that half-governed the
    #: stage it was attributed to. Every over-fetch goes through
    #: :meth:`overfetch` now, and `tests/test_ranking_parameters.py` fails if a
    #: second multiplier appears anywhere else.
    filter_overfetch: float = DEFAULT_FILTER_OVERFETCH
    #: Where these values came from, for the audit trail. Never part of
    #: ranking; carried so a served result can be traced to a decision.
    provenance: str = "default"
    bundle_id: str = ""

    def __post_init__(self) -> None:
        for name in PARAMETER_STAGES:
            object.__setattr__(self, name, clamp(name, getattr(self, name)))

    def overfetch(self, max_results: int, *, filtering: bool) -> int:
        """How many candidates to ask the arms for.

        The single home for the arithmetic, so "how far past `max_results` do
        we fetch" has one answer per configuration rather than one per surface.
        `filtering=False` returns `max_results` unchanged: over-fetching when
        nothing will be dropped is pure cost.

        Never returns fewer than `max_results` — the clamp keeps a bundle that
        proposed a factor below 1.0 from turning an over-fetch into a
        truncation, which would silently shorten every filtered answer.
        """

        if not filtering:
            return max(0, int(max_results))
        return max(int(max_results), int(int(max_results) * self.filter_overfetch))

    @property
    def bm25_weights(self) -> str:
        """The SQLite ``bm25()`` weight list, positionally matching chunks_fts.

        The first three columns (chunk_id, source_id, artifact_id) are
        UNINDEXED and weigh nothing; the remaining four are title, path,
        heading_path, text.
        """

        return (
            f"0.0, 0.0, 0.0, {float(self.title_weight)}, "
            f"{float(self.path_weight)}, {float(self.heading_weight)}, {float(self.text_weight)}"
        )

    @property
    def ts_rank_weights(self) -> str:
        """The same four weights for Postgres ``ts_rank_cd``.

        Its array is ordered ``{D, C, B, A}`` -- the reverse of how the columns
        read above -- and is normalized onto its 0-1 scale by dividing through
        by the largest weight. Normalizing by the *title* weight specifically
        (rather than by ``max``) would send the array out of range the moment
        a tuning pass proposed a path weight above the title weight, which is
        a configuration the bounds allow.
        """

        weights = [self.text_weight, self.heading_weight, self.path_weight, self.title_weight]
        scale = max(weights) or 1.0
        return ", ".join(str(round(value / scale, 6)) for value in weights)

    def arm_weight(self, arm: str) -> float:
        return {
            "text": self.text_arm_weight,
            "vector": self.vector_arm_weight,
            "graph": self.graph_arm_weight,
        }.get(arm, 1.0)

    def values(self) -> dict[str, float]:
        """Just the tunable numbers, in a stable order. The trial's identity."""

        return {name: float(getattr(self, name)) for name in sorted(PARAMETER_STAGES)}

    def with_overlay(self, overlay: dict[str, Any], *, provenance: str, bundle_id: str = ""):
        """This point with named parameters replaced, out-of-range clamped.

        Unknown keys are **ignored rather than rejected**. An overlay is
        persisted data that outlives the code that wrote it: a bundle applied
        under one version and read back under a later one that dropped a
        parameter must still resolve to a working configuration, because the
        alternative is a region that will not serve a search until somebody
        edits a row by hand.
        """

        changes: dict[str, Any] = {}
        for name, value in (overlay or {}).items():
            if name not in PARAMETER_STAGES:
                logger.debug("ranking: ignoring unknown overlay parameter %r", name)
                continue
            try:
                changes[name] = clamp(name, float(value))
            except (TypeError, ValueError):
                logger.warning("ranking: overlay parameter %r is not a number", name)
        return replace(self, **changes, provenance=provenance, bundle_id=bundle_id)

    @classmethod
    def from_config(cls, config: Any) -> RankingParameters:
        """Read ``search.ranking``, falling back to the defaults field by field."""

        settings = getattr(getattr(config, "search", None), "ranking", None)
        if settings is None:
            return cls()
        values = {
            name: getattr(settings, name)
            for name in PARAMETER_STAGES
            if getattr(settings, name, None) is not None
        }
        return cls(**values, provenance="config")

    def describe(self) -> dict[str, Any]:
        """What a caller is told about the parameters a result ranked under."""

        return {
            "provenance": self.provenance,
            "bundle_id": self.bundle_id,
            "values": self.values(),
        }


#: The shipped point. Equal, value for value, to the constants this module
#: replaced.
DEFAULT_RANKING = RankingParameters()


def resolver_for(config: Any, state: Any) -> RankingResolver:
    """The resolver every long-lived searcher should be built with.

    One call site per process type (API, MCP, engine, evaluation replay) so
    they cannot drift onto different parameters -- which would make the
    numbers the evaluation plane publishes describe a configuration the HTTP
    surface does not serve.
    """

    return RankingResolver(
        base=RankingParameters.from_config(config),
        state=state,
        kb_id=str(getattr(config, "knowledge_base_id", "") or ""),
    )


@dataclass
class RankingResolver:
    """Config parameters overlaid by whatever bundle the region has applied.

    Held by a searcher rather than resolved per call, and refreshed on a TTL
    rather than per call: the overlay is a single row and the read is cheap,
    but it is still a database read and the search path is not the place to do
    one per request for a value that changes at most a few times a day.
    """

    base: RankingParameters = field(default_factory=RankingParameters)
    state: Any = None
    kb_id: str = ""
    ttl_seconds: float = OVERLAY_TTL_SECONDS
    _current: RankingParameters | None = field(default=None, repr=False)
    _checked_at: float = field(default=0.0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def current(self) -> RankingParameters:
        """The parameters to rank with now."""

        with self._lock:
            now = time.monotonic()
            if self._current is not None and (now - self._checked_at) < self.ttl_seconds:
                return self._current
            self._checked_at = now
            self._current = self._resolve()
            return self._current

    def invalidate(self) -> None:
        """Drop the cache so the next search re-reads the overlay.

        Called when *this* process applies a bundle. Other replicas find out on
        their own TTL, which is the whole reason there is one.
        """

        with self._lock:
            self._current = None
            self._checked_at = 0.0

    def _resolve(self) -> RankingParameters:
        if self.state is None or not self.kb_id:
            return self.base
        try:
            from pheasant.tuning.store import active_overlay

            overlay = active_overlay(self.state, self.kb_id)
        except Exception:  # noqa: BLE001 - ranking must survive a missing plane
            # A region whose /state predates the tuning tables, or whose
            # tuning plane raised for any reason, ranks on its configured
            # parameters. Degrading to the config is always a valid ranking;
            # failing the search is not.
            logger.debug("ranking: could not read the active overlay", exc_info=True)
            return self.base
        if not overlay:
            return self.base
        return self.base.with_overlay(
            overlay.get("parameters") or {},
            provenance="bundle",
            bundle_id=str(overlay.get("bundle_id") or ""),
        )
