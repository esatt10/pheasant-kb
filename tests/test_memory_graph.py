"""Step 33.7 — memory in the knowledge graph.

Acceptance is deliberately **per corpus type**, not in aggregate: an
entity-only bridge passes on a Markdown corpus and is a silent no-op on a
corpus of PDFs or of non-Python code, which is exactly the failure this ladder
exists to avoid. Each case asserts *which rung fired* and that the memory
reaches real corpus content, plus the edge cap, the `supersedes` edges, and
that a region without memory produces an unchanged graph.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pheasant.config.loader import load_config
from pheasant.mcp_server.tools import PheasantTools
from pheasant.memory.bridge import (
    DEFAULT_MAX_TARGETS,
    BridgeInputs,
    candidate_identifiers,
    resolve_bridges,
    supersedes_edges,
)
from pheasant.memory.store import MemoryStore

PINNED = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _config(tmp_path: Path, *, corpus: str, include_memory: bool = True) -> Path:
    """A workspace holding one corpus flavour plus (optionally) a memory source."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir(exist_ok=True)

    if corpus == "markdown":
        (corpus_dir / "runbook.md").write_text(
            "# Runbook\n\nThe Kestrel Gateway is operated by the Platform Team.\n",
            encoding="utf-8",
        )
    elif corpus == "python":
        (corpus_dir / "engine.py").write_text(
            "class SyncEngine:\n    def run_incremental(self):\n        return True\n",
            encoding="utf-8",
        )
    elif corpus == "structured":
        (corpus_dir / "agreement.md").write_text(
            "ARTICLE IV\nTerm and Termination\n\n"
            "This article governs termination for convenience.\n",
            encoding="utf-8",
        )
    elif corpus == "opaque":
        # Neither entities nor symbols nor headings: the case an entity-only
        # bridge silently fails on. `.txt` gets no code pass; lowercase prose
        # yields no capitalised entity candidates.
        (corpus_dir / "notes.txt").write_text(
            "the quarterly figures were reconciled last week\n", encoding="utf-8"
        )

    taxonomy = "    taxonomy:\n      enabled: true\n" if corpus == "structured" else ""
    memory_block = (
        "  - name: agent-memory\n    type: memory\n    path: memory\n" if include_memory else ""
    )
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: memory-graph
  state_path: {tmp_path / ".pheasant" / "state"}
  exports_path: {tmp_path / ".pheasant" / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
sources:
  - name: corpus
    type: document_folder
    path: corpus
    include:
      - "**/*.md"
      - "**/*.py"
      - "**/*.txt"
{taxonomy}{memory_block}""",
        encoding="utf-8",
    )
    (tmp_path / "memory").mkdir(exist_ok=True)
    return config_path


def _synced(tmp_path: Path, corpus: str, memories: list[str]) -> PheasantTools:
    config_path = _config(tmp_path, corpus=corpus)
    store = MemoryStore(tmp_path / "memory")
    for index, text in enumerate(memories):
        store.append(
            text,
            scope="org",
            now=datetime(2026, 7, 16, 12, 0, index, tzinfo=UTC),
        )
    tools = PheasantTools(load_config(config_path))
    tools.sync_source("memory-graph", "corpus", "full")
    tools.sync_source("memory-graph", "agent-memory", "full")
    return tools


def _about_edges(tools: PheasantTools) -> list[dict]:
    graph = tools.engine.graph_builder.graph
    out = []
    with graph.reading():
        for (source, target), edge_map in graph.iter_edges():
            for data in edge_map.values():
                if data.get("type") == "about":
                    out.append({"source": source, "target": target, **data})
    return out


# ---------------------------------------------------------------------------
# The ladder, per corpus type
# ---------------------------------------------------------------------------


def test_reference_rung_fires_when_the_record_names_a_file(tmp_path: Path) -> None:
    """Rung 1 — the strongest signal, and it needed no new matching code."""
    tools = _synced(
        tmp_path,
        "markdown",
        ["See [the runbook](runbook.md) before any Kestrel Gateway deploy."],
    )
    try:
        edges = _about_edges(tools)
        assert edges, "no about edge was drawn"
        assert {edge["match_signal"] for edge in edges} == {"reference"}
        assert any("runbook.md" in edge["target"] for edge in edges)
        assert edges[0]["confidence"] == 1.0
    finally:
        tools.engine.close()


def test_symbol_rung_fires_on_a_python_corpus(tmp_path: Path) -> None:
    """Python defines almost no entities — only class names — so `entity`
    alone would have covered a fraction of a code corpus."""
    tools = _synced(tmp_path, "python", ["run_incremental is safe to call twice."])
    try:
        edges = _about_edges(tools)
        assert edges, "no about edge was drawn for a code corpus"
        assert {edge["match_signal"] for edge in edges} == {"symbol"}
        assert any("engine.py" in edge["target"] for edge in edges)
        assert edges[0]["matched"] == "run_incremental"
    finally:
        tools.engine.close()


def test_entity_rung_fires_on_a_markdown_corpus(tmp_path: Path) -> None:
    tools = _synced(tmp_path, "markdown", ["Kestrel Gateway needs the amber gate."])
    try:
        edges = _about_edges(tools)
        assert edges, "no about edge was drawn"
        assert {edge["match_signal"] for edge in edges} == {"entity"}
        assert all(edge["target"].startswith("entity:") for edge in edges)
    finally:
        tools.engine.close()


def test_a_corpus_with_no_entities_symbols_or_headings_reports_unbridged(
    tmp_path: Path,
) -> None:
    """The honest outcome, surfaced rather than silently doing nothing.

    This is the case an entity-only bridge would have failed *quietly*: no
    edges, no error, no way to tell it from a bridge that simply had nothing
    to connect.
    """
    tools = _synced(tmp_path, "opaque", ["nothing here matches anything indexed"])
    try:
        report = tools.engine.graph_builder.add_memory_edges(tools.engine.state)
        assert report["records"] == 1
        assert report["about"] == 0
        assert len(report["unbridged"]) == 1
    finally:
        tools.engine.close()


def test_the_strongest_rung_wins_and_the_weaker_ones_do_not_also_fire(
    tmp_path: Path,
) -> None:
    """Precedence is the point: an explicit pointer is not diluted."""
    tools = _synced(
        tmp_path,
        "markdown",
        ["See [the runbook](runbook.md) — it covers the Kestrel Gateway."],
    )
    try:
        signals = {edge["match_signal"] for edge in _about_edges(tools)}
        assert signals == {"reference"}, (
            "a record with an explicit link should not also match by entity"
        )
    finally:
        tools.engine.close()


# ---------------------------------------------------------------------------
# Bounds, supersession, and the untouched-graph guarantee
# ---------------------------------------------------------------------------


def test_about_edges_are_capped_per_record(tmp_path: Path) -> None:
    """The concept layer had no ceiling; that is how it reached 98.6% of edges."""
    tools = _synced(
        tmp_path,
        "python",
        ["SyncEngine and run_incremental and SyncEngine.run_incremental all appear here."],
    )
    try:
        edges = _about_edges(tools)
        assert 0 < len(edges) <= DEFAULT_MAX_TARGETS
    finally:
        tools.engine.close()


def test_supersedes_edges_match_the_stores_own_chain(tmp_path: Path) -> None:
    """A documented edge type that nothing emitted until now."""
    config_path = _config(tmp_path, corpus="markdown")
    store = MemoryStore(tmp_path / "memory")
    old, _ = store.append("The gate is amber.", scope="org", now=PINNED)
    store.append(
        "The gate is green.",
        scope="org",
        supersedes=old.record_id,
        now=datetime(2026, 8, 1, tzinfo=UTC),
    )
    tools = PheasantTools(load_config(config_path))
    try:
        tools.sync_source("memory-graph", "corpus", "full")
        tools.sync_source("memory-graph", "agent-memory", "full")

        graph = tools.engine.graph_builder.graph
        found = []
        with graph.reading():
            for (source, target), edge_map in graph.iter_edges():
                for data in edge_map.values():
                    if data.get("type") == "supersedes":
                        found.append((source, target))
        assert len(found) == 1
        newer, older = found[0]
        assert old.record_id in older
        assert old.record_id not in newer
    finally:
        tools.engine.close()


def test_memory_artifacts_are_typed_memory_record(tmp_path: Path) -> None:
    """The graph could not previously say which notes were remembered."""
    tools = _synced(tmp_path, "markdown", ["The Kestrel Gateway is amber."])
    try:
        graph = tools.engine.graph_builder.graph
        with graph.reading():
            types = {
                attrs.get("type")
                for _node_id, attrs in graph.iter_nodes()
                if attrs.get("source_id") == "agent-memory"
                and str(attrs.get("relative_path") or "").endswith(".md")
            }
        assert types == {"artifact_record"}

        # And the SQLite row agrees, so a caller reading either sees the same.
        rows = tools.engine.state.rows(
            "SELECT DISTINCT type FROM artifacts WHERE source_id='agent-memory'"
        )
        assert [row["type"] for row in rows] == ["artifact_record"]
    finally:
        tools.engine.close()


def test_a_region_without_memory_gets_an_unchanged_graph(tmp_path: Path) -> None:
    config_path = _config(tmp_path, corpus="markdown", include_memory=False)
    tools = PheasantTools(load_config(config_path))
    try:
        tools.sync_source("memory-graph", "corpus", "full")
        report = tools.engine.graph_builder.add_memory_edges(tools.engine.state)
        assert report == {
            "records": 0,
            "about": 0,
            "supersedes": 0,
            "subsumes": 0,
            "unbridged": [],
            "by_signal": {},
        }
        assert _about_edges(tools) == []
    finally:
        tools.engine.close()


def test_bridging_is_idempotent(tmp_path: Path) -> None:
    """Re-running draws the same relationships and adds nothing.

    Compared on identity, not on the whole dict: `upsert_edge` refreshes
    `updated_at` on every call by design, for every edge type in the graph.
    """

    def identity(edges: list[dict]) -> set[tuple]:
        return {
            (e["source"], e["target"], e["match_signal"], e["matched"], e["confidence"])
            for e in edges
        }

    tools = _synced(tmp_path, "python", ["run_incremental is safe to call twice."])
    try:
        first = identity(_about_edges(tools))
        assert first
        tools.engine.graph_builder.add_memory_edges(tools.engine.state)
        tools.engine.graph_builder.add_memory_edges(tools.engine.state)
        assert identity(_about_edges(tools)) == first
        assert len(_about_edges(tools)) == len(first)
    finally:
        tools.engine.close()


def test_an_entity_label_never_spans_a_line_break() -> None:
    r"""Regression: `\s+` in the Title Case pattern fused a heading with the
    next paragraph into one "entity" whose label contained the line breaks.

    Found by the bridge, which matches entities by label and so could never
    match the real one. Junk labels also reached the graph and the facts panel.
    """
    from pheasant.graph.enrichment import _entity_candidates

    labels = _entity_candidates("# Runbook\r\n\r\nThe Kestrel Gateway is here.")
    assert all("\n" not in label and "\r" not in label for label in labels)
    assert "The Kestrel Gateway" in labels


def test_a_memory_does_not_bridge_to_another_memory(tmp_path: Path) -> None:
    """Otherwise a store of related notes knits itself together and calls that
    grounding."""
    tools = _synced(
        tmp_path,
        "opaque",
        ["The Kestrel Gateway is amber.", "The Kestrel Gateway was checked today."],
    )
    try:
        for edge in _about_edges(tools):
            assert "agent-memory" not in edge["target"], (
                "a memory record was bridged to another memory record"
            )
    finally:
        tools.engine.close()


# ---------------------------------------------------------------------------
# The pure ladder, without a sync
# ---------------------------------------------------------------------------


def _inputs(**kwargs) -> BridgeInputs:
    base = {
        "records": [{"record_id": "mem-1", "artifact_id": "file:mem:a.md:branch=none"}],
        "texts": {},
        "symbols": {},
        "headings": {},
        "entities": {},
        "referenced": {},
    }
    base.update(kwargs)
    return BridgeInputs(**base)


def test_precedence_order_is_reference_symbol_heading_entity() -> None:
    artifact = "file:mem:a.md:branch=none"
    text = "SyncEngine handles 'Term and Termination' for Kestrel Gateway"
    everything = _inputs(
        texts={artifact: text},
        symbols={"syncengine": ["file:code:e.py:branch=none"]},
        headings={"term and termination": ["heading:doc:1"]},
        entities={"kestrel gateway": ["entity:kb:doc:kestrel-gateway"]},
        referenced={artifact: ["file:doc:r.md:branch=none"]},
    )
    edges, unbridged = resolve_bridges(everything)
    assert not unbridged
    assert {edge.signal for edge in edges} == {"reference"}

    # Drop the explicit reference and the next rung takes over, and so on down.
    for dropped, expected in [
        ("referenced", "symbol"),
        ("symbols", "heading"),
        ("headings", "entity"),
    ]:
        kwargs = {
            "texts": {artifact: text},
            "symbols": {"syncengine": ["file:code:e.py:branch=none"]},
            "headings": {"term and termination": ["heading:doc:1"]},
            "entities": {"kestrel gateway": ["entity:kb:doc:kestrel-gateway"]},
            "referenced": {artifact: ["file:doc:r.md:branch=none"]},
        }
        for key in list(kwargs):
            if (
                key == dropped
                or (dropped == "symbols" and key == "referenced")
                or (dropped == "headings" and key in {"referenced", "symbols"})
            ):
                kwargs[key] = {}
        edges, _ = resolve_bridges(_inputs(**kwargs))
        assert {edge.signal for edge in edges} == {expected}, f"after dropping {dropped}"


def test_identifier_candidates_ignore_ordinary_prose() -> None:
    """A bare lowercase word would match half a codebase."""
    words = candidate_identifiers("the deploy runs when run_incremental calls SyncEngine.start")
    assert "run_incremental" in words
    assert "SyncEngine.start" in words
    assert "deploy" not in words
    assert "the" not in words


def test_a_leading_article_does_not_split_one_entity_into_two() -> None:
    """The extractor starts a Title Case run at "The", so the corpus says
    *The Kestrel Gateway* while a memory says *Kestrel Gateway*."""
    from pheasant.memory.bridge import normalize_label

    assert normalize_label("The Kestrel Gateway") == normalize_label("Kestrel Gateway")
    assert normalize_label("An Incident Report") == "incident report"
    # Not over-eager: a word merely starting with "the" is untouched.
    assert normalize_label("Theory Of Change") == "theory of change"


def test_supersedes_skips_a_record_that_names_a_missing_target() -> None:
    """Pointing an edge at nothing is worse than drawing no edge."""
    records = [
        {"record_id": "mem-a", "artifact_id": "file:m:a.md:branch=none", "supersedes": None},
        {
            "record_id": "mem-b",
            "artifact_id": "file:m:b.md:branch=none",
            "supersedes": "mem-does-not-exist",
        },
    ]
    assert supersedes_edges(records) == []


def test_resolution_is_deterministic() -> None:
    artifact = "file:mem:a.md:branch=none"
    inputs = _inputs(
        texts={artifact: "SyncEngine and run_incremental"},
        symbols={
            "syncengine": ["file:z:z.py:branch=none", "file:a:a.py:branch=none"],
            "run_incremental": ["file:m:m.py:branch=none"],
        },
    )
    first, _ = resolve_bridges(inputs)
    second, _ = resolve_bridges(inputs)
    assert first == second


@pytest.mark.parametrize("cap", [1, 2, 5])
def test_the_cap_is_honoured_exactly(cap: int) -> None:
    artifact = "file:mem:a.md:branch=none"
    inputs = _inputs(
        texts={artifact: "SyncEngine"},
        symbols={"syncengine": [f"file:s{i}:x.py:branch=none" for i in range(10)]},
    )
    edges, _ = resolve_bridges(inputs, max_targets=cap)
    assert len(edges) == cap


# ---------------------------------------------------------------------------
# Reachability — the point of the whole step
# ---------------------------------------------------------------------------


def _hops(config_path: Path, source: str, target: str) -> list | None:
    """Walk `GET /graph/path`, the real route, over the persisted graph.

    The engine is closed before the app opens so the graph is read back from
    disk — which also proves the bridge's edges survive persistence rather
    than living only in the process that drew them.
    """
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from pheasant.api.app import create_app

    app = create_app(load_config(config_path), config_path=str(config_path))
    client = fastapi_testclient.TestClient(app)
    try:
        response = client.get(
            "/graph/path", params={"source": source, "target": target, "max_depth": 3}
        )
        assert response.status_code == 200, response.text
        return response.json().get("path")
    finally:
        app.state.engine.close()


def test_a_memory_reaches_the_corpus_file_it_is_about_in_two_hops(tmp_path: Path) -> None:
    """Memory stops being an island.

    Two hops is the bar because the honest shortest path for the weaker rungs
    is record -> symbol/entity -> file; an explicit reference is one hop. If
    this cannot be walked, the graph arm cannot reach corpus content *through*
    a memory, which is the whole justification for the bridge.
    """
    tools = _synced(tmp_path, "python", ["run_incremental is safe to call twice."])
    records = tools.engine.state.rows("SELECT artifact_id FROM memory_records")
    assert records
    memory_artifact = str(records[0]["artifact_id"])
    tools.engine.close()

    path = _hops(tmp_path / "pheasant.yaml", memory_artifact, "file:corpus:engine.py:branch=none")
    assert path, "no path from the memory record to the file it is about"
    assert len(path) - 1 <= 2, path


def test_the_walk_goes_both_ways(tmp_path: Path) -> None:
    """Corpus to memory matters as much as memory to corpus: it is how a
    question about a file surfaces what was remembered about it."""
    tools = _synced(tmp_path, "markdown", ["See [the runbook](runbook.md) for the amber gate."])
    records = tools.engine.state.rows("SELECT artifact_id FROM memory_records")
    memory_artifact = str(records[0]["artifact_id"])
    tools.engine.close()

    path = _hops(tmp_path / "pheasant.yaml", "file:corpus:runbook.md:branch=none", memory_artifact)
    assert path, "the corpus file cannot reach what was remembered about it"


def test_describe_retrieval_reports_bridge_coverage(tmp_path: Path) -> None:
    """ "2 records, 1 bridged" is how an operator tells a working bridge from
    one that silently connects nothing."""
    tools = _synced(
        tmp_path,
        "python",
        ["run_incremental is safe to call twice.", "nothing here matches anything"],
    )
    try:
        coverage = tools.describe_retrieval("memory-graph")["memory"]["graph"]
        assert coverage["records"] == 2
        assert coverage["bridged"] == 1
        assert coverage["unbridged"] == 1
        assert coverage["by_signal"] == {"symbol": 1}
    finally:
        tools.engine.close()
