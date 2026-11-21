"""The cheap path must be the same path.

Offline re-fusion is what makes a parameter search affordable: the fusion
family is evaluated by re-running the merge over cached arm lists instead of
re-running retrieval. That is only sound if the re-implementation *is* the
implementation, and this repository has already paid for the alternative --
a hand-rolled ``yaml.py`` shadowed the real parser and concealed four bugs
before it was deleted.

So the load-bearing assertion here is not "re-fusion produces plausible
results". It is: **re-fuse the captured inputs at the parameters that actually
ran, and get back exactly what the region served** -- id for id, order
included, against a real indexed corpus through the real
``HybridSearch.search_context``.
"""

from __future__ import annotations

from typing import Any

import pytest

from pheasant.search.hybrid import HybridSearch
from pheasant.search.ranking import RankingParameters
from pheasant.search.text_store import SearchStore
from pheasant.tuning import refusion
from tests.conftest import run_sync

QUERIES = (
    "sync engine",
    "how does indexing work",
    "readme",
    "search context provenance",
    "configuration",
)


def _searcher(engine: Any, ranking: RankingParameters | None = None) -> HybridSearch:
    return HybridSearch(
        SearchStore(engine.state, ranking=ranking),
        vector=engine.vector_searcher(),
        node_index=getattr(engine, "node_index", None),
    )


@pytest.fixture()
def indexed(loaded_config: Any, sync_engine: Any) -> Any:
    run_sync(sync_engine, source_name="pheasant-repo", mode="full")
    return sync_engine


def test_explain_is_absent_unless_asked_for(indexed: Any, loaded_config: Any) -> None:
    """The ordinary payload must not change because a diagnostic exists.

    Same rule ``heading_path`` and ``memory_policy`` follow: a key appears
    only when it says something. A production caller that never asks for
    stages must get the response it got before this plane was written.
    """

    searcher = _searcher(indexed)
    plain = searcher.search_context(loaded_config.knowledge_base_id, "sync engine")
    assert "stages" not in plain

    explained = searcher.search_context(
        loaded_config.knowledge_base_id, "sync engine", explain=True
    )
    assert "stages" in explained
    # ...and asking for it must not change the answer.
    assert [item["node_id"] for item in explained["results"]] == [
        item["node_id"] for item in plain["results"]
    ]


@pytest.mark.parametrize("query", QUERIES)
def test_refusion_reproduces_the_served_order_exactly(
    indexed: Any, loaded_config: Any, query: str
) -> None:
    """The equivalence the whole cheap path rests on."""

    searcher = _searcher(indexed)
    payload = searcher.search_context(
        loaded_config.knowledge_base_id, query, max_results=10, explain=True
    )
    stages = payload["stages"]
    if not payload["results"]:
        pytest.skip(f"corpus returned nothing for {query!r}")

    ok, reason = refusion.verify_equivalence(stages, searcher.ranking_parameters())
    assert ok, reason

    served = [item["node_id"] for item in payload["results"]]
    computed = refusion.refuse(
        stages["fusion_input"], stages["max_results"], searcher.ranking_parameters()
    )
    assert computed == served


def test_refusion_matches_a_real_search_under_changed_parameters(
    indexed: Any, loaded_config: Any
) -> None:
    """The point of the whole thing: a *predicted* ranking must be the real one.

    Re-fusion is only useful if a parameter point it scores well is a point the
    region would really serve that way. So this runs the expensive path -- a
    genuine search with the trial's parameters pinned into the store -- and
    requires the cheap path to have predicted it.
    """

    baseline = _searcher(indexed)
    payload = baseline.search_context(
        loaded_config.knowledge_base_id, "sync engine", max_results=10, explain=True
    )
    if len(payload["results"]) < 2:
        pytest.skip("need at least two results to tell orderings apart")

    for point in (
        RankingParameters(rrf_k=5.0),
        RankingParameters(rrf_k=200.0),
        RankingParameters(text_arm_weight=3.0, graph_arm_weight=0.0),
        RankingParameters(vector_arm_weight=0.0),
    ):
        predicted = refusion.refuse(
            payload["stages"]["fusion_input"], payload["stages"]["max_results"], point
        )
        actual = _searcher(indexed, ranking=point).search_context(
            loaded_config.knowledge_base_id, "sync engine", max_results=10
        )
        assert predicted == [item["node_id"] for item in actual["results"]], (
            f"re-fusion mispredicted the ranking under {point.values()}"
        )


def test_refusion_refuses_a_capture_it_cannot_reproduce() -> None:
    """A degraded capture must fall back, never approximate.

    Each of these returns a number if the guard is missing, and the number
    looks exactly like a measurement.
    """

    assert not refusion.refusable({})
    assert not refusion.refusable({"fusion_input": {}, "max_results": 10})
    assert not refusion.refusable({"fusion_input": {"text": [["a", "a", "chunk"]]}})
    assert not refusion.refusable(
        {
            "fusion_input": {"text": [["a", "a", "chunk"]]},
            "max_results": 10,
            "fusion_input_truncated": True,
        }
    )
    ok, reason = refusion.verify_equivalence({}, RankingParameters())
    assert not ok and reason


def test_arm_weight_of_zero_removes_an_arm_from_the_merge() -> None:
    """Zero is a real value on the ladder, and it has to mean 'ignore this arm'.

    Proving a merge is better *without* a stale vector index is a legitimate
    outcome of a tuning pass, so the parameter has to be able to express it.
    """

    fusion_input = {
        "text": [["c1", "n1", "chunk"]],
        "vector": [["c2", "n2", "chunk"]],
        "graph": [],
    }
    both = refusion.refuse(fusion_input, 10, RankingParameters())
    assert set(both) == {"n1", "n2"}

    text_only = refusion.refuse(fusion_input, 10, RankingParameters(vector_arm_weight=0.0))
    # The arm still contributes its candidate — a zero weight is a zero score,
    # not a filter — but it can no longer outrank a weighted arm.
    assert text_only[0] == "n1"


# ---------------------------------------------------------------------------
# One loop, two entry points
#
# The re-fusion used to be "a line-for-line mirror" of `hybrid._merge_rrf`,
# with `verify_equivalence` catching drift after the fact. Both now call
# `search.fusion.fuse`, so the correspondence is structural rather than
# maintained — and these are the tests that keep it that way.
# ---------------------------------------------------------------------------


def test_neither_side_still_carries_its_own_merge_loop() -> None:
    """The duplication this removed, asserted gone.

    Greps for the accumulator the two loops shared. A second one is a second
    answer to "how does this region rank", which is the thing that could
    diverge silently until a guard happened to cover the case.
    """

    import re
    from pathlib import Path

    from pheasant.search import fusion as fusion_module

    root = Path(fusion_module.__file__).resolve().parents[1]
    # `scores[key] = scores.get(key, 0.0) + ... / (rrf_k + rank)` — the RRF
    # accumulation itself, wherever it is spelled.
    accumulation = re.compile(r"/\s*\(\s*(?:ranking\.)?rrf_k\s*\+")
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path == Path(fusion_module.__file__).resolve():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if accumulation.search(line):
                offenders.append(f"{path.relative_to(root)}:{number}: {line.strip()}")

    assert not offenders, (
        "reciprocal rank fusion is computed outside search/fusion.py:\n  "
        + "\n  ".join(offenders)
        + "\nBoth the query path and the re-fusion are entry points onto one loop; "
        "a third implementation is a third ranking this region can produce."
    )


def test_both_entry_points_agree_on_a_generated_merge() -> None:
    """Differential over the two entry points, rather than trust in the grep.

    The query path fuses rich records and the re-fusion fuses captured
    triples. Feeding both the same candidates must produce the same order —
    which is what `verify_equivalence` used to have to prove and now cannot
    fail to be true.
    """

    import random

    from pheasant.search.hybrid import _merge_rrf
    from pheasant.search.ranking import RankingParameters
    from pheasant.tuning.refusion import refuse

    rng = random.Random(20260904)
    for _ in range(25):
        arms: dict[str, list[dict]] = {}
        for arm in ("text", "vector", "graph"):
            arms[arm] = [
                {
                    "chunk_id": f"c{rng.randrange(12)}",
                    "node_id": f"n{rng.randrange(8)}",
                    "kind": rng.choice(["chunk", "node", "relationship"]),
                }
                for _ in range(rng.randrange(0, 7))
            ]
        ranking = RankingParameters(
            rrf_k=rng.choice([10.0, 60.0, 200.0]),
            text_arm_weight=rng.choice([0.0, 1.0, 2.0]),
            vector_arm_weight=rng.choice([0.0, 1.0, 2.0]),
            graph_arm_weight=rng.choice([0.0, 1.0, 2.0]),
        )
        limit = rng.randrange(1, 8)

        collected: dict = {}
        served = _merge_rrf(
            [dict(item) for item in arms["text"]],
            [dict(item) for item in arms["vector"]],
            [dict(item) for item in arms["graph"]],
            limit,
            ranking,
            collect=collected,
        )
        recomputed = refuse(collected["fusion_input"], limit, ranking)
        assert [str(item.get("node_id") or "") for item in served] == recomputed
