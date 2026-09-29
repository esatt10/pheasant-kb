"""The merge rule behind `search_batch`, on inputs that can tell orderings apart.

The surface tests in `test_surface_conformance.py` run real batches, but a
three-document corpus puts every hit at rank one, so it cannot distinguish
rank-major order from query-major order — a mutant swapping them survived
there. These cases are built so that every plausible wrong ordering gives a
different answer.
"""

from __future__ import annotations

from pheasant.services.retrieval import _merge_batch


def _hits(*ids: str | None) -> dict:
    return {
        "results": [{"chunk_id": identity} if identity else {"text": "no id"} for identity in ids]
    }


def _order(merged: list[dict]) -> list[str | None]:
    return [hit.get("chunk_id") for hit in merged]


def test_every_querys_best_hit_leads_before_any_second_hit() -> None:
    """Rank-major: truncating the merged list keeps coverage of every query.

    Query-major would give A B C D E, letting the first query's tail crowd
    out the other two queries' best answers.
    """

    merged = _merge_batch([_hits("A", "B", "C"), _hits("D", "A"), _hits("E")])

    assert _order(merged) == ["A", "D", "E", "B", "C"]


def test_a_hit_takes_its_best_rank_across_queries() -> None:
    merged = _merge_batch([_hits("X", "Y", "A"), _hits("A")])
    by_id = {hit["chunk_id"]: hit["batch"] for hit in merged}

    assert by_id["A"] == {"best_rank": 1, "matched_queries": [0, 1]}
    # A reached rank one through the second query, so it ties X on rank and
    # wins the tie on agreement; Y, a rank-two hit, follows both.
    assert _order(merged) == ["A", "X", "Y"]


def test_ties_in_rank_go_to_the_hit_more_queries_agreed_on() -> None:
    merged = _merge_batch([_hits("P"), _hits("Q"), _hits("Q")])

    assert _order(merged) == ["Q", "P"]


def test_remaining_ties_keep_query_order() -> None:
    merged = _merge_batch([_hits("P"), _hits("Q"), _hits("R")])

    assert _order(merged) == ["P", "Q", "R"]


def test_a_hit_without_an_id_is_never_collapsed_into_another() -> None:
    merged = _merge_batch([_hits(None, "A"), _hits(None)])

    assert _order(merged).count(None) == 2
    assert len(merged) == 3


def test_the_payload_kept_is_the_one_from_the_best_rank() -> None:
    first = {"results": [{"chunk_id": "Z", "score": 0.1}, {"chunk_id": "A", "score": 0.2}]}
    second = {"results": [{"chunk_id": "A", "score": 0.9}]}

    merged = {hit["chunk_id"]: hit for hit in _merge_batch([first, second])}

    assert merged["A"]["score"] == 0.9
