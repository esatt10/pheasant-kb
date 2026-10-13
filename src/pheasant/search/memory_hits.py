"""How agent-memory hits are marked and given room in a fused result list.

Split out of ``search.hybrid`` by bounded context: both functions read the
memory index and nothing else of the search, and ``hybrid`` calls them once
each around the merge. Behaviour is unchanged by the move.
"""

from __future__ import annotations

from typing import Any

from pheasant.memory.policy import PREFER_SHARE, describe, resolve


def annotate_memory(
    results: list[dict[str, Any]],
    memory_index: dict[str, dict[str, Any]],
    policy: Any,
) -> None:
    """Mark which hits are remembered assertions rather than corpus content.

    Added only to hits that *are* memory records, so a corpus result keeps the
    exact shape it had before Step 33.6 — the same rule `heading_path` follows.

    This is not decoration. A caller that cannot tell a remembered assertion
    from a document has no way to weigh it differently, and the answering
    prompt and the UI both need the distinction to be explicit rather than
    inferred from a path that happens to start with a scope directory.
    """
    for item in results:
        record = resolve(item, memory_index)
        if record is None:
            continue
        item["memory"] = describe(record)
        provenance = item.get("provenance")
        if isinstance(provenance, dict):
            provenance["memory"] = True
    _ = policy


def prefer_memory(
    text_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    graph_results: list[dict[str, Any]],
    memory_index: dict[str, dict[str, Any]],
    max_results: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Guarantee memory a share of the slots under `mode="prefer"`.

    Fusion ranks memory against a whole corpus, so on a large index a genuinely
    relevant memory can lose every slot to documents that merely match more
    words. `prefer` reserves up to half the results for memory hits by moving
    them to the front of their own arm, which is enough for RRF to keep them:
    their rank within the arm is what the fusion actually reads.

    Deliberately a *reordering*, not a score change. Inventing a boost would
    make the same query answer differently depending on a flag, in a way no
    caller could predict; promoting within an arm is bounded, explainable and
    leaves every other hit's relative order intact.
    """
    reserved = max(1, int(max_results * PREFER_SHARE))

    def promote(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        remembered = [r for r in results if resolve(r, memory_index) is not None]
        if not remembered:
            return results
        rest = [r for r in results if resolve(r, memory_index) is None]
        return remembered[:reserved] + rest + remembered[reserved:]

    return promote(text_results), promote(vector_results), promote(graph_results)
