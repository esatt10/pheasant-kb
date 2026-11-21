"""Acceptance tests for context search with provenance."""

from __future__ import annotations

from tests.conftest import item_text, result_items, run_sync, search_context


def test_search_context_returns_sync_engine_file_with_provenance(
    loaded_config: object, sync_engine: object
) -> None:
    """A sync-engine query should return relevant chunks/files and source provenance."""

    run_sync(sync_engine, source_name="pheasant-repo", mode="full")

    search_result = search_context("sync engine", loaded_config=loaded_config, engine=sync_engine)
    items = result_items(search_result)

    assert items, "Expected at least one search result for 'sync engine'."
    flattened = "\n".join(item_text(item).lower() for item in items)
    assert "sync" in flattened and "engine" in flattened
    assert (
        "sync_engine.py" in flattened or "sync-engine.md" in flattened or "readme.md" in flattened
    )
    assert "provenance" in flattened or "source" in flattened or "path" in flattened


# ---------------------------------------------------------------------------
# The stage block is a contract, and nothing was checking it
#
# `hybrid.search_context(explain=True)` produces it; `tuning.stages.attribute`
# consumes it; the whole retrieval diagnosis rests on the two agreeing. Every
# read on the consumer side is a `.get(...) or {}`, because a stage that did
# not happen is legitimately absent — which also means a *renamed* key reads as
# an absence. The plane then blames a different stage, confidently, and the
# number it publishes looks like a measurement.
#
# There is no type checker in CI, so the TypedDict in `search.explain` is
# documentation. These are the enforcement.
# ---------------------------------------------------------------------------


def _explained_stages(loaded_config: object, sync_engine: object) -> dict:
    run_sync(sync_engine, source_name="pheasant-repo", mode="full")
    from pheasant.search.hybrid import HybridSearch
    from pheasant.search.text_store import SearchStore

    searcher = HybridSearch(SearchStore(sync_engine.state))
    payload = searcher.search_context(
        loaded_config.knowledge_base_id, "sync engine", "hybrid", 5, explain=True
    )
    assert "stages" in payload, "explain=True produced no stage block at all"
    return payload["stages"]


def test_the_producer_writes_every_key_the_contract_declares(
    loaded_config: object, sync_engine: object
) -> None:
    """A stage the producer stopped emitting is a stage the diagnosis can never
    blame — and it would look like that stage simply never loses documents."""

    from pheasant.search.explain import REQUIRED_KEYS, StageBlock

    stages = _explained_stages(loaded_config, sync_engine)

    missing = sorted(REQUIRED_KEYS - set(stages))
    assert not missing, f"the explain block is missing declared keys: {missing}"

    undeclared = sorted(set(stages) - set(StageBlock.__annotations__))
    assert not undeclared, (
        f"the explain block carries keys the contract does not declare: {undeclared}. "
        "Add them to search/explain.py so the consumer's author can see them."
    )


def test_every_key_the_diagnosis_reads_is_one_the_producer_can_write() -> None:
    """The direction that fails silently.

    A consumer reading `stages["candidate"]` instead of `"candidates"` gets an
    empty dict, attributes nothing to that stage, and reports a clean
    histogram. Nothing raises, and the diagnosis is wrong in the direction
    that looks like good news.
    """

    import re
    from pathlib import Path

    from pheasant.search.explain import StageBlock
    from pheasant.tuning import stages as stage_module

    source = Path(stage_module.__file__).read_text(encoding="utf-8")
    read = set(re.findall(r"stages\.get\(\s*[\"']([a-z_]+)[\"']", source))
    read |= set(re.findall(r"stages\[\s*[\"']([a-z_]+)[\"']\s*\]", source))
    assert read, "found no stage-block reads at all — has the consumer moved?"

    declared = set(StageBlock.__annotations__)
    undeclared = sorted(read - declared)
    assert not undeclared, (
        f"tuning/stages.py reads keys the contract does not declare: {undeclared}. "
        "Either the producer stopped writing them (a permanently blind stage) or "
        "they are typos, which read identically."
    )


def test_the_declared_consumed_keys_match_what_the_consumer_actually_reads() -> None:
    """Keeps `CONSUMED_KEYS` honest rather than aspirational."""

    import re
    from pathlib import Path

    from pheasant.search.explain import CONSUMED_KEYS
    from pheasant.tuning import stages as stage_module

    source = Path(stage_module.__file__).read_text(encoding="utf-8")
    read = set(re.findall(r"stages\.get\(\s*[\"']([a-z_]+)[\"']", source))
    read |= set(re.findall(r"stages\[\s*[\"']([a-z_]+)[\"']\s*\]", source))

    unlisted = sorted(read - CONSUMED_KEYS)
    assert not unlisted, f"CONSUMED_KEYS omits keys the consumer reads: {unlisted}"
