"""The ranking knobs, and the promise that opening them changed nothing.

Every number in `search.ranking` used to be a module constant baked into an SQL
f-string. Making them addressable is what lets the tuning plane propose them;
the risk it introduces is that the *defaults* drift from the measured values
they replaced, silently re-ranking every region that never opens the block.

So the load-bearing assertion here is a string comparison against the literals
those constants produced. It is deliberately brittle: a change to a default is
supposed to fail this, loudly, and be an explicit decision.
"""

from __future__ import annotations

from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.search import ranking
from pheasant.search.ranking import (
    BOUNDS,
    DEFAULT_RANKING,
    PARAMETER_STAGES,
    RankingParameters,
    RankingResolver,
)
from pheasant.search.text_store import _structural_prior_sql


def test_the_default_bm25_weights_are_the_measured_constants() -> None:
    """8/3/2/1, from the 2026-08-03 retrieval overhaul, character for character."""

    assert DEFAULT_RANKING.bm25_weights == "0.0, 0.0, 0.0, 8.0, 3.0, 2.0, 1.0"


def test_the_default_ts_rank_weights_are_the_measured_constants() -> None:
    """The same four weights on ts_rank_cd's {D,C,B,A} scale."""

    assert DEFAULT_RANKING.ts_rank_weights == "0.125, 0.25, 0.375, 1.0"


def test_the_default_structural_prior_renders_the_constants_it_replaced() -> None:
    sql = _structural_prior_sql(DEFAULT_RANKING)
    assert "+ 0.05 * (length(artifacts.relative_path)" in sql
    assert "THEN 0.6 ELSE 0.0 END" in sql
    assert "THEN 0.3 ELSE 0.0 END" in sql


def test_ts_rank_weights_stay_in_range_when_a_weight_exceeds_the_title() -> None:
    """`ts_rank_cd` weights are 0-1, and the bounds allow path > title.

    Normalizing by the *title* weight specifically — the obvious reading of
    "divide by 8" — sends the array out of range the moment a tuning pass
    proposes a path weight above it, which is a configuration the bounds
    permit. Normalizing by the largest weight is what keeps that safe.
    """

    point = RankingParameters(title_weight=2.0, path_weight=16.0)
    values = [float(part) for part in point.ts_rank_weights.split(", ")]
    assert all(0.0 <= value <= 1.0 for value in values), point.ts_rank_weights
    assert max(values) == 1.0


@pytest.mark.parametrize("name", sorted(PARAMETER_STAGES))
def test_every_parameter_declares_a_stage_a_bound_and_a_config_field(name: str) -> None:
    """Three registries have to agree, and nothing else makes them.

    A parameter present in one and missing from another fails in a different
    way each time: no stage means the strategy never proposes it; no bound
    means an overlay can set it to something that inverts ranking; no config
    field means an operator cannot pin it.
    """

    assert name in BOUNDS
    assert hasattr(PheasantConfig().search.ranking, name)
    assert name in DEFAULT_RANKING.values()


def test_the_config_defaults_equal_the_module_defaults() -> None:
    """`search.ranking` and `RankingParameters` are one set of numbers."""

    from_config = RankingParameters.from_config(PheasantConfig())
    assert from_config.values() == DEFAULT_RANKING.values()


def test_an_overlay_is_clamped_rather_than_trusted() -> None:
    """A stored bundle is data, and data can be wrong.

    `prior_floor` is the sharp one: the prior is a divisor, so zero or negative
    inverts the entire result list rather than degrading it.
    """

    point = DEFAULT_RANKING.with_overlay(
        {"prior_floor": 0.0, "rrf_k": -5.0, "title_weight": 10_000.0},
        provenance="bundle",
    )
    assert point.prior_floor == BOUNDS["prior_floor"][0]
    assert point.rrf_k == BOUNDS["rrf_k"][0]
    assert point.title_weight == BOUNDS["title_weight"][1]


def test_an_overlay_ignores_names_it_does_not_know() -> None:
    """An overlay outlives the code that wrote it.

    A bundle applied under one version and read back under a later one that
    dropped a parameter has to resolve to a working configuration. The
    alternative is a region that will not serve a search until somebody edits
    a row by hand.
    """

    point = DEFAULT_RANKING.with_overlay(
        {"rrf_k": 30.0, "a_parameter_from_the_future": 1.0}, provenance="bundle"
    )
    assert point.rrf_k == 30.0
    assert point.values() == {**DEFAULT_RANKING.values(), "rrf_k": 30.0}


def test_an_overlay_ignores_a_value_that_is_not_a_number() -> None:
    point = DEFAULT_RANKING.with_overlay({"rrf_k": "sixty"}, provenance="bundle")
    assert point.rrf_k == DEFAULT_RANKING.rrf_k


def test_the_resolver_degrades_to_config_when_the_plane_is_unreachable() -> None:
    """Ranking must survive a `/state` that predates the tuning tables.

    Degrading to the configured values is always a valid ranking. Failing the
    search is not — and a region upgrading from an older version reads exactly
    this path on its first query.
    """

    class Broken:
        def rows(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("no such table: tuning_bundles")

    resolver = RankingResolver(base=DEFAULT_RANKING, state=Broken(), kb_id="kb")
    assert resolver.current().values() == DEFAULT_RANKING.values()
    assert resolver.current().provenance == "default"


def test_the_resolver_caches_and_can_be_invalidated() -> None:
    """One indexed read per TTL, not one per search.

    The overlay is on the search path. A read per request would put a database
    round trip in front of every query for a value that changes a few times a
    day at most.
    """

    calls = {"n": 0}

    class Counting:
        def rows(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            calls["n"] += 1
            return []

    resolver = RankingResolver(
        base=DEFAULT_RANKING, state=Counting(), kb_id="kb", ttl_seconds=1000.0
    )
    for _ in range(5):
        resolver.current()
    assert calls["n"] == 1

    resolver.invalidate()
    resolver.current()
    assert calls["n"] == 2


def test_rrf_k_of_zero_is_refused_by_the_bounds() -> None:
    """At k=0, rank one is worth infinitely more than rank two.

    That is score-merging by another name, and score-merging is the thing
    reciprocal rank fusion was adopted to replace.
    """

    assert BOUNDS["rrf_k"][0] >= 1.0
    assert ranking.clamp("rrf_k", 0.0) >= 1.0


# ---------------------------------------------------------------------------
# One over-fetch, not four
#
# `filter_overfetch` is declared tunable: it sits in `PARAMETER_STAGES` mapped
# to the `filters` stage, is bounded (1.0, 10.0), and ships a candidate ladder
# in the tuning space. The glossary tells an operator that a `filters` miss may
# mean "filter_overfetch is too small".
#
# It governed the ACL, section and memory filters only. Retrieval *criteria*
# — exclude_sources, node_types, min_score, source_types — were post-filtered
# by the surfaces at a hardcoded `× 4`, and the vector arm carried a third
# copy. So a tuning bundle could be promoted on the strength of a parameter
# that half-governed the stage it was attributed to, and an operator following
# the glossary's own advice would not see the effect it predicted.
# ---------------------------------------------------------------------------


def test_the_overfetch_is_computed_in_exactly_one_place() -> None:
    """The assertion that would have caught it, in the spirit of the
    DOCUMENT_EXTENSIONS/EXTRACTED_EXTENSIONS set-equality guard.

    Greps the source for a numeric multiplier applied to a result count
    anywhere outside `ranking.py`. A second one is not necessarily wrong — but
    it is necessarily a second answer to "how far past max_results do we
    fetch", which is the thing that diverged.
    """

    import re
    from pathlib import Path

    source_root = Path(ranking.__file__).resolve().parents[1]
    # `max_results * 4`, `fetch * 2`, `limit*3` — a literal factor on a count.
    multiplier = re.compile(r"\b(?:max_results|fetch|limit|fetch_n)\s*\*\s*\d")
    offenders: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        if path.name == "ranking.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if multiplier.search(line) and "noqa: overfetch" not in line:
                offenders.append(f"{path.relative_to(source_root)}:{number}: {line.strip()}")

    assert not offenders, (
        "a result count is multiplied by a literal outside ranking.py:\n  "
        + "\n  ".join(offenders)
        + "\nRoute it through RankingParameters.overfetch, or — if it is genuinely a "
        "different concern — give it its own named parameter and say so in the tuning "
        "glossary, because one name covering two behaviours is what this guard exists for."
    )


def test_overfetch_never_returns_fewer_than_asked() -> None:
    """A factor below 1.0 would turn an over-fetch into a truncation.

    The bounds clamp it, and this pins the behaviour at the boundary rather
    than trusting that they always will.
    """

    for factor in (0.0, 0.5, 1.0, 3.0, 10.0):
        parameters = ranking.RankingParameters(filter_overfetch=factor)
        assert parameters.overfetch(10, filtering=True) >= 10
    assert ranking.RankingParameters().overfetch(10, filtering=False) == 10


def test_the_tunable_parameter_reaches_criteria_filtering() -> None:
    """The defect itself: raising it must change what a criteria-filtered
    search asks the arms for."""

    small = ranking.RankingParameters(filter_overfetch=2.0)
    large = ranking.RankingParameters(filter_overfetch=8.0)
    assert small.overfetch(10, filtering=True) == 20
    assert large.overfetch(10, filtering=True) == 80


def test_the_stage_it_is_attributed_to_is_the_one_it_governs() -> None:
    """`filters` — and now every filter, not a subset of them."""

    assert ranking.PARAMETER_STAGES["filter_overfetch"] == "filters"
    assert ranking.BOUNDS["filter_overfetch"] == (1.0, 10.0)
    assert ranking.DEFAULT_FILTER_OVERFETCH == ranking.RankingParameters().filter_overfetch


def test_both_surfaces_over_fetch_by_the_configured_factor(loaded_config, tmp_path) -> None:
    """The behavioural half of the guard above.

    The grep catches a literal; this catches the thing the literal *did* — a
    surface that computes its own fetch count and therefore does not move when
    the tunable parameter does. Driven through both public surfaces, because
    C1 was a divergence between them and the layer beneath.
    """

    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app
    from pheasant.mcp_server.tools import PheasantTools
    from pheasant.search.hybrid import HybridSearch

    loaded_config.pheasant.workspace_root = tmp_path
    seen: list[int] = []
    real = HybridSearch.search_context

    def recording(self, kb, query, mode, max_results, *args, **kwargs):
        seen.append(max_results)
        return real(self, kb, query, mode, max_results, *args, **kwargs)

    criteria = {"query": "anything", "max_results": 5, "exclude_sources": ["noise"]}

    for factor, expected in ((2.0, 10), (6.0, 30)):
        loaded_config.search.ranking.filter_overfetch = factor

        seen.clear()
        client = TestClient(create_app(config=loaded_config))
        HybridSearch.search_context = recording
        try:
            assert client.post("/search", json=criteria).status_code == 200
            http_fetch = seen[-1]

            seen.clear()
            tools = PheasantTools(loaded_config)
            tools.search_context(
                loaded_config.knowledge_base_id,
                query="anything",
                max_results=5,
                exclude_sources=["noise"],
            )
            mcp_fetch = seen[-1]
        finally:
            HybridSearch.search_context = real

        assert http_fetch == expected, f"HTTP over-fetched {http_fetch}, expected {expected}"
        assert mcp_fetch == expected, f"MCP over-fetched {mcp_fetch}, expected {expected}"
