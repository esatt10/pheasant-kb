"""Acceptance tests for structural taxonomy extraction.

Fully offline and rule-based — no model, no network.

What this feature connects, and why the assertions look the way they do: three
pieces already existed and none were wired up. ``TextChunk.heading_path`` was a
field, a ``chunks`` column *and* an FTS5 column at BM25 weight 2.0 that was
``NULL`` for every chunk ever indexed; ``docs/graph_model.md`` documents a
``heading`` node type and a ``has_heading`` edge type that were never emitted;
and ``enrichment.py``'s Markdown heading regex fed ``_add_concept``, a no-op
since the 2026-08-03 concept retirement. Several tests below pin each of those
so a regression is visible as "the dormant thing went dormant again".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig, TaxonomySettings
from pheasant.ingestion.taxonomy import (
    PATH_SEPARATOR,
    RULE_NAMES,
    SectionHeading,
    detect_headings,
    heading_path_for_line,
    taxonomy_tree,
)
from pheasant.search.hybrid import HybridSearch
from pheasant.search.sqlite_store import SearchStore
from pheasant.sync.engine import SyncEngine

# A contract shaped the way real ones are: an ALL-CAPS title, a bare ARTICLE
# citation whose caption sits on the next line, decimal subsections, lettered
# sub-subsections, a § code, and a top-level SCHEDULE that must pop the stack.
LEGAL = """MASTER SERVICES AGREEMENT

ARTICLE IV
Term and Termination

4.1 Initial Term
This Agreement commences on the Effective Date and continues for three years.

4.2 Termination for Cause
Either party may terminate upon a material breach by the other party.

(a) Material breach
A breach not cured within thirty days of written notice.

(b) Insolvency
Upon any bankruptcy filing by either party.

§ 12.3 Governing Law
This Agreement is governed by the laws of Delaware, excluding its conflict rules.

SCHEDULE 1
Fee Table
Fees are billed monthly in arrears.
"""

PROCEDURE = """# Incident Response Procedure

## 1. Detection
Monitor the alerting channel continuously.

## 2. Triage
### 2.1 Severity assessment
Assign a severity within fifteen minutes.
### 2.2 Escalation
Page the on-call lead immediately.

STEP 3 Containment
Isolate the affected host from the network.
"""

# A shopping list: numbered lines that are list items, not sections. Kept as a
# test subject because the ambiguity is real and unresolvable by heuristics —
# which is *why* the feature is per-source opt-in rather than global.
PROSE = """Notes from the week.

1. Buy milk
2. Call the plumber about the leak in the upstairs bathroom sink before Friday

An ordinary paragraph that runs on for a while and is not a heading at all.
"""


def _engine(tmp_path: Path, text: str, *, taxonomy: dict | None = None, name: str = "docs"):
    workspace = tmp_path / "workspace" / "docs"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "doc.md").write_text(text, encoding="utf-8")
    source: dict = {
        "name": name,
        "type": "document_folder",
        "path": str(workspace),
        "include": ["**/*.md"],
    }
    if taxonomy is not None:
        source["taxonomy"] = taxonomy
    return SyncEngine(
        PheasantConfig.model_validate(
            {
                "pheasant": {
                    "name": f"taxonomy-{name}",
                    "state_path": str(tmp_path / "state"),
                    "workspace_root": str(tmp_path / "workspace"),
                    "exports_path": str(tmp_path / "exports"),
                },
                "sources": [source],
            }
        )
    )


def _chunks(engine: SyncEngine) -> list[dict]:
    return [
        dict(row)
        for row in engine.state.rows(
            "SELECT chunk_index, start_line, end_line, heading_path, text "
            "FROM chunks ORDER BY chunk_index"
        )
    ]


def _node_types(engine: SyncEngine) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _node_id, data in engine.graph_builder.graph.node_map().items():
        counts[str(data.get("type"))] = counts.get(str(data.get("type")), 0) + 1
    return counts


def _edge_types(engine: SyncEngine) -> dict[str, int]:
    counts: dict[str, int] = {}
    for (_s, _t), edge_map in engine.graph_builder.graph.iter_edges():
        for _key, data in edge_map.items():
            counts[str(data.get("type"))] = counts.get(str(data.get("type")), 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_legal_document_nests_mixed_conventions() -> None:
    """One document may mix ARTICLE / 4.1 / (a) / § and still nest correctly."""
    paths = [h.path for h in detect_headings(LEGAL)]
    assert "MASTER SERVICES AGREEMENT" in paths
    assert "MASTER SERVICES AGREEMENT > Article IV Term and Termination" in paths
    assert "MASTER SERVICES AGREEMENT > Article IV Term and Termination > 4.1 Initial Term" in paths
    assert (
        "MASTER SERVICES AGREEMENT > Article IV Term and Termination > "
        "4.2 Termination for Cause > (a) Material breach" in paths
    )


def test_bare_citation_takes_its_caption_from_the_next_line() -> None:
    """``ARTICLE IV`` / ``Term and Termination`` on two lines is standard legal
    drafting. Without the continuation lookup the Article's path reads
    "Article IV" and the words a searcher would actually use are dropped."""
    headings = detect_headings(LEGAL)
    article = next(h for h in headings if h.number == "Article IV")
    assert article.title == "Term and Termination"
    assert article.label == "Article IV Term and Termination"


def test_top_level_heading_pops_the_stack() -> None:
    """``SCHEDULE 1`` is level 1, so it must not end up nested under Article IV."""
    schedule = next(h for h in detect_headings(LEGAL) if h.number == "Schedule 1")
    assert schedule.path == "Schedule 1 Fee Table"
    assert "Article" not in schedule.path


def test_procedure_markdown_and_keyword_headings() -> None:
    paths = [h.path for h in detect_headings(PROCEDURE)]
    assert "Incident Response Procedure" in paths
    assert "Incident Response Procedure > 2. Triage" in paths
    assert "Incident Response Procedure > 2. Triage > 2.1 Severity assessment" in paths
    assert any(p.endswith("Step 3 Containment") for p in paths)


def test_number_is_kept_separate_from_title() -> None:
    """ "What does § 12.3 say" is answerable from ``number`` alone."""
    code = next(h for h in detect_headings(LEGAL) if h.kind == "code")
    assert code.number == "§ 12.3"
    assert code.title == "Governing Law"


def test_long_numbered_lines_are_not_treated_as_headings() -> None:
    """The prose filter: a numbered line that reads like a sentence is skipped.

    The short ones are still picked up, and that ambiguity is genuine — "1.
    Scope" in a standards document and "1. Buy milk" in a note are
    indistinguishable. Documented, and the reason the feature is enabled per
    source rather than globally.
    """
    titles = [h.title for h in detect_headings(PROSE)]
    assert not any("plumber" in t for t in titles), titles
    assert not any("ordinary paragraph" in t.lower() for t in titles)


def test_detection_is_deterministic() -> None:
    first = [h.as_dict() for h in detect_headings(LEGAL)]
    second = [h.as_dict() for h in detect_headings(LEGAL)]
    assert first == second


def test_rules_can_be_narrowed() -> None:
    """``detect`` drops the noisiest rules on a corpus that does not need them."""
    only_markdown = detect_headings(PROCEDURE, rules=("markdown",))
    assert {h.kind for h in only_markdown} == {"markdown"}
    assert detect_headings(LEGAL, rules=("markdown",)) == []
    assert all(name in RULE_NAMES for name in ("markdown", "keyword", "code", "caps"))


def test_max_depth_excludes_deeper_headings() -> None:
    shallow = detect_headings(LEGAL, max_depth=3)
    assert shallow, "expected some headings at depth 3"
    assert max(h.level for h in shallow) <= 3
    assert not any(h.kind == "lettered" for h in shallow)  # lettered is level 6


def test_heading_cap_is_enforced() -> None:
    text = "\n".join(f"## Section {n}" for n in range(500))
    assert len(detect_headings(text, max_headings=10)) == 10


def test_heading_path_for_line_finds_the_enclosing_section() -> None:
    headings = detect_headings(LEGAL)
    assert heading_path_for_line(headings, 1) == "MASTER SERVICES AGREEMENT"
    deep = heading_path_for_line(headings, 13)
    assert deep is not None and deep.endswith("(a) Material breach")
    # A line before any heading has no section.
    assert heading_path_for_line(detect_headings(PROCEDURE), 0) is None


def test_taxonomy_tree_nests_by_level() -> None:
    tree = taxonomy_tree(detect_headings(PROCEDURE))
    assert len(tree) == 1
    root = tree[0]
    assert root["title"] == "Incident Response Procedure"
    triage = next(c for c in root["children"] if c["title"] == "2. Triage")
    assert [c["title"] for c in triage["children"]] == [
        "2.1 Severity assessment",
        "2.2 Escalation",
    ]


def test_empty_and_structureless_text() -> None:
    assert detect_headings("") == []
    assert detect_headings("just one line of prose") == []
    assert detect_headings(LEGAL, rules=()) == []


# ---------------------------------------------------------------------------
# The dormant pieces, now connected
# ---------------------------------------------------------------------------


def test_heading_path_was_and_stays_null_without_the_toggle(tmp_path: Path) -> None:
    """Pins the pre-feature behavior, which is also the opt-out guarantee."""
    engine = _engine(tmp_path, LEGAL)
    engine.sync_source("docs", mode="full")
    assert [c["heading_path"] for c in _chunks(engine)] == [None]
    assert "heading" not in _node_types(engine)
    assert "has_heading" not in _edge_types(engine)


def test_toggle_populates_heading_path(tmp_path: Path) -> None:
    engine = _engine(tmp_path, LEGAL, taxonomy={"enabled": True})
    engine.sync_source("docs", mode="full")
    paths = [c["heading_path"] for c in _chunks(engine)]
    assert all(paths), f"every chunk should carry a section: {paths}"
    assert any("§ 12.3 Governing Law" in p for p in paths)


def test_toggle_emits_the_documented_heading_nodes(tmp_path: Path) -> None:
    """`heading` / `has_heading` are in graph_model.md and were never emitted."""
    engine = _engine(tmp_path, LEGAL, taxonomy={"enabled": True})
    engine.sync_source("docs", mode="full")
    assert _node_types(engine).get("heading", 0) >= 6
    assert _edge_types(engine).get("has_heading", 0) >= 6


def test_heading_hierarchy_uses_contains_edges(tmp_path: Path) -> None:
    """A subsection is parented to its section, not flattened onto the file."""
    engine = _engine(tmp_path, LEGAL, taxonomy={"enabled": True})
    engine.sync_source("docs", mode="full")
    graph = engine.graph_builder.graph
    nodes = graph.node_map()
    article = next(
        node_id
        for node_id, data in nodes.items()
        if data.get("type") == "heading" and data.get("number") == "Article IV"
    )
    children = [
        nodes[target].get("number")
        for (src, target), edge_map in graph.iter_edges()
        if src == article
        for _key, data in edge_map.items()
        if data.get("type") == "contains"
    ]
    assert "4.1" in children and "4.2" in children


def test_graph_nodes_can_be_disabled_independently(tmp_path: Path) -> None:
    """Search labelling without graph growth is a valid configuration."""
    engine = _engine(tmp_path, LEGAL, taxonomy={"enabled": True, "graph_nodes": False})
    engine.sync_source("docs", mode="full")
    assert all(c["heading_path"] for c in _chunks(engine))
    assert "heading" not in _node_types(engine)


def test_heading_node_ids_survive_a_line_shift(tmp_path: Path) -> None:
    """IDs key on the section breadcrumb, not its line number.

    A line-numbered ID would churn the entire graph on any edit: inserting one
    paragraph shifts every heading below it, dropping and re-creating sections
    that did not change.
    """
    engine = _engine(tmp_path, PROCEDURE, taxonomy={"enabled": True})
    engine.sync_source("docs", mode="full")
    before = {
        node_id
        for node_id, data in engine.graph_builder.graph.node_map().items()
        if data.get("type") == "heading"
    }

    doc = tmp_path / "workspace" / "docs" / "doc.md"
    doc.write_text("A new preamble line.\n\n" + PROCEDURE, encoding="utf-8")
    engine.sync_source("docs", mode="full")
    after = {
        node_id
        for node_id, data in engine.graph_builder.graph.node_map().items()
        if data.get("type") == "heading"
    }
    assert before == after


# ---------------------------------------------------------------------------
# Section-aligned chunking
# ---------------------------------------------------------------------------


def test_chunks_are_cut_at_section_boundaries(tmp_path: Path) -> None:
    """Without this the whole contract is one 4000-char chunk carrying one
    heading_path, and the taxonomy is decorative."""
    engine = _engine(tmp_path, LEGAL, taxonomy={"enabled": True})
    engine.sync_source("docs", mode="full")
    chunks = _chunks(engine)
    assert len(chunks) >= 6, f"expected one chunk per section, got {len(chunks)}"
    # Each chunk covers exactly one section.
    assert len({c["heading_path"] for c in chunks}) == len(chunks)
    # Indices are a single ascending run, so chunk IDs stay stable.
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
    # Line numbers are absolute, not relative to the section.
    assert all(c["start_line"] >= 1 for c in chunks)
    assert chunks[-1]["start_line"] > chunks[0]["start_line"]


def test_section_chunk_text_matches_its_section(tmp_path: Path) -> None:
    engine = _engine(tmp_path, LEGAL, taxonomy={"enabled": True})
    engine.sync_source("docs", mode="full")
    governing = next(c for c in _chunks(engine) if "§ 12.3" in (c["heading_path"] or ""))
    assert "Delaware" in governing["text"]
    assert "bankruptcy" not in governing["text"], "section chunk leaked its neighbour"


def test_oversized_section_is_still_split_by_max_chars(tmp_path: Path) -> None:
    """One enormous section must not become one enormous chunk."""
    big = "## Big Section\n" + ("filler sentence for the body. " * 400) + "\n## Small\ntail\n"
    engine = _engine(
        tmp_path,
        big,
        taxonomy={"enabled": True},
    )
    engine.config.sources[0].chunking.max_chars = 500
    engine.config.sources[0].chunking.overlap_chars = 50
    engine.sync_source("docs", mode="full")
    chunks = _chunks(engine)
    big_chunks = [c for c in chunks if (c["heading_path"] or "") == "Big Section"]
    assert len(big_chunks) > 1, "oversized section was not subdivided"
    # All the pieces still carry the same section path.
    assert {c["heading_path"] for c in big_chunks} == {"Big Section"}


def test_split_on_sections_can_be_turned_off(tmp_path: Path) -> None:
    """Labelling without re-cutting: the original boundaries are preserved."""
    labelled = _engine(
        tmp_path / "a", LEGAL, taxonomy={"enabled": True, "split_on_sections": False}
    )
    labelled.sync_source("docs", mode="full")
    plain = _engine(tmp_path / "b", LEGAL)
    plain.sync_source("docs", mode="full")

    labelled_chunks = _chunks(labelled)
    plain_chunks = _chunks(plain)
    assert len(labelled_chunks) == len(plain_chunks)
    assert [c["text"] for c in labelled_chunks] == [c["text"] for c in plain_chunks]
    # Same text, but now labelled.
    assert labelled_chunks[0]["heading_path"] == "MASTER SERVICES AGREEMENT"
    assert plain_chunks[0]["heading_path"] is None


# ---------------------------------------------------------------------------
# Search + idempotency
# ---------------------------------------------------------------------------


def test_search_returns_the_section_that_matched(tmp_path: Path) -> None:
    """The payoff: a hit says *which section*, not just which file.

    ``heading_path`` was already selected by the search SQL and dropped when
    the result dict was built.
    """
    engine = _engine(tmp_path, LEGAL, taxonomy={"enabled": True})
    engine.sync_source("docs", mode="full")
    searcher = HybridSearch(SearchStore(engine.state), vector=None)

    for query, expected in [
        ("governing law Delaware", "§ 12.3 Governing Law"),
        ("material breach thirty days", "(a) Material breach"),
        ("initial term three years", "4.1 Initial Term"),
    ]:
        payload = searcher.search_context(
            engine.config.knowledge_base_id, query, mode="text", max_results=1
        )
        top = payload["results"][0]
        assert expected in (top.get("heading_path") or ""), (query, top.get("heading_path"))
        assert top["chunks"][0]["heading_path"] == top["heading_path"]


def test_search_payload_unchanged_without_taxonomy(tmp_path: Path) -> None:
    """No `heading_path` key at all when there is nothing to say."""
    engine = _engine(tmp_path, LEGAL)
    engine.sync_source("docs", mode="full")
    searcher = HybridSearch(SearchStore(engine.state), vector=None)
    payload = searcher.search_context(
        engine.config.knowledge_base_id, "termination", mode="text", max_results=1
    )
    top = payload["results"][0]
    assert "heading_path" not in top
    assert "heading_path" not in top["chunks"][0]
    assert "heading_path" not in top["provenance"]


def test_resync_is_idempotent(tmp_path: Path) -> None:
    engine = _engine(tmp_path, LEGAL, taxonomy={"enabled": True})
    engine.sync_source("docs", mode="full")
    first_chunks = _chunks(engine)
    first_headings = sorted(
        node_id
        for node_id, data in engine.graph_builder.graph.node_map().items()
        if data.get("type") == "heading"
    )
    engine.sync_source("docs", mode="full")
    assert _chunks(engine) == first_chunks
    assert (
        sorted(
            node_id
            for node_id, data in engine.graph_builder.graph.node_map().items()
            if data.get("type") == "heading"
        )
        == first_headings
    )


def test_incremental_resync_does_not_rebuild(tmp_path: Path) -> None:
    engine = _engine(tmp_path, LEGAL, taxonomy={"enabled": True})
    engine.sync_source("docs", mode="full")
    before = _chunks(engine)
    engine.sync_source("docs", mode="incremental")
    assert _chunks(engine) == before


# ---------------------------------------------------------------------------
# Config + registration surfaces
# ---------------------------------------------------------------------------


def test_defaults_are_off() -> None:
    settings = TaxonomySettings()
    assert settings.enabled is False
    # Once enabled, the useful behaviours are on by default.
    assert settings.graph_nodes is True
    assert settings.split_on_sections is True


def test_taxonomy_block_is_actually_wired(tmp_path: Path) -> None:
    """Guard for the class of bug that silently discarded the ``graph:`` block."""
    config = PheasantConfig.model_validate(
        {
            "pheasant": {"name": "cfg", "state_path": str(tmp_path / "state")},
            "sources": [
                {
                    "name": "s",
                    "type": "document_folder",
                    "path": str(tmp_path),
                    "taxonomy": {
                        "enabled": True,
                        "max_depth": 3,
                        "detect": ["markdown"],
                        "graph_nodes": False,
                        "split_on_sections": False,
                    },
                }
            ],
        }
    )
    taxonomy = config.sources[0].taxonomy
    assert taxonomy.enabled is True
    assert taxonomy.max_depth == 3
    assert taxonomy.detect == ["markdown"]
    assert taxonomy.graph_nodes is False
    assert taxonomy.split_on_sections is False


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    workspace = tmp_path / "workspace" / "legal"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "msa.md").write_text(LEGAL, encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "taxonomy-api",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path / "workspace"),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [],
        }
    )
    return TestClient(create_app(config)), workspace


def test_register_source_with_the_toggle(tmp_path: Path) -> None:
    """The headline surface: switch it on when the source is registered."""
    client, workspace = _client(tmp_path)
    response = client.post(
        "/sources",
        json={
            "name": "legal",
            "type": "document_folder",
            "path": str(workspace),
            "include": ["**/*.md"],
            "taxonomy": {"enabled": True},
            "sync_now": True,
            "wait": True,
        },
    )
    assert response.status_code < 400, response.text

    listed = client.get("/sources").json()
    entries = listed if isinstance(listed, list) else listed.get("sources", [])
    entry = next(e for e in entries if e.get("name") == "legal")
    stored = json.loads(entry["config_json"])["taxonomy"]
    assert stored["enabled"] is True

    taxonomy = client.get("/taxonomy").json()
    assert taxonomy["heading_count"] >= 6


def test_taxonomy_endpoint_returns_a_nested_tree(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    client.post(
        "/sources",
        json={
            "name": "legal",
            "type": "document_folder",
            "path": str(workspace),
            "include": ["**/*.md"],
            "taxonomy": {"enabled": True},
            "sync_now": True,
            "wait": True,
        },
    )
    payload = client.get("/taxonomy").json()
    document = payload["documents"][0]
    assert document["relative_path"] == "msa.md"

    def find(nodes: list[dict], title: str) -> dict | None:
        for node in nodes:
            if node["title"] == title:
                return node
            hit = find(node["children"], title)
            if hit:
                return hit
        return None

    article = find(document["tree"], "Term and Termination")
    assert article is not None and article["number"] == "Article IV"
    assert any(child["number"] == "4.1" for child in article["children"])

    # Filters
    assert client.get("/taxonomy", params={"path": "msa.md"}).json()["heading_count"] >= 6
    assert client.get("/taxonomy", params={"path": "nope.md"}).json()["documents"] == []
    assert client.get("/taxonomy", params={"source": "other"}).json()["documents"] == []


def test_taxonomy_endpoint_is_empty_without_the_toggle(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    client.post(
        "/sources",
        json={
            "name": "legal",
            "type": "document_folder",
            "path": str(workspace),
            "include": ["**/*.md"],
            "sync_now": True,
            "wait": True,
        },
    )
    payload = client.get("/taxonomy").json()
    assert payload["documents"] == []
    assert payload["heading_count"] == 0


def test_quick_add_accepts_the_boolean_form(tmp_path: Path) -> None:
    client, workspace = _client(tmp_path)
    response = client.post(
        "/sources/quick-add",
        json={"target": str(workspace), "taxonomy": True, "sync_now": True, "wait": True},
    )
    assert response.status_code < 400, response.text
    assert client.get("/taxonomy").json()["heading_count"] >= 6


def test_mcp_register_source_accepts_the_toggle(tmp_path: Path) -> None:
    """MCP tool surface is public API — an additive optional parameter."""
    from pheasant.mcp_server.tools import PheasantTools

    workspace = tmp_path / "workspace" / "legal"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "msa.md").write_text(LEGAL, encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "taxonomy-mcp",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path / "workspace"),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [],
        }
    )
    tools = PheasantTools(config)

    tools.register_source(
        knowledge_base=config.knowledge_base_id,
        name="legal",
        source_type="document_folder",
        path=str(workspace),
        include=["**/*.md"],
        taxonomy=True,
    )
    source = next(s for s in config.sources if s.name == "legal")
    assert source.taxonomy.enabled is True

    # And omitting it registers exactly what it always did.
    tools.register_source(
        knowledge_base=config.knowledge_base_id,
        name="plain",
        source_type="document_folder",
        path=str(workspace),
        include=["**/*.md"],
    )
    assert next(s for s in config.sources if s.name == "plain").taxonomy.enabled is False


@pytest.mark.parametrize("text", [LEGAL, PROCEDURE, PROSE, "", "# only a title\n"])
def test_detection_never_raises(text: str) -> None:
    assert isinstance(detect_headings(text), list)
    assert isinstance(taxonomy_tree(detect_headings(text)), list)


def test_section_heading_as_dict_round_trip() -> None:
    heading = SectionHeading(
        line=3, level=2, number="4.1", title="T", kind="numbered", path="A > B"
    )
    assert heading.as_dict() == {
        "line": 3,
        "level": 2,
        "number": "4.1",
        "title": "T",
        "kind": "numbered",
        "path": "A > B",
    }
    assert heading.label == "4.1 T"


# ---------------------------------------------------------------------------
# Ordinal detection + reconciliation
# ---------------------------------------------------------------------------

# A contract carrying two *independent* numbering series (roman Articles with
# decimal subsections, plus a separate § code series) and deliberate sequence
# defects: a gap 12.3 -> 12.5 and a duplicated (b).
ORDINAL_LEGAL = """MASTER SERVICES AGREEMENT

ARTICLE IV
Term and Termination

4.1 Initial Term
Commences on the Effective Date.

4.2 Termination for Cause
Either party may terminate.

(a) Material breach
Not cured within thirty days.

(b) Insolvency
Upon any bankruptcy filing.

(b) Change of Control
On any change of control.

§ 12.3 Governing Law
Laws of Delaware.

§ 12.5 Notices
All notices in writing.
"""


def _by_number(headings: list[SectionHeading], number: str) -> SectionHeading:
    return next(h for h in headings if h.number == number)


def test_parse_ordinal_decimal_roman_and_letters() -> None:
    from pheasant.ingestion.taxonomy import parse_ordinal

    assert parse_ordinal("4.2", "numbered").parts == (4, 2)
    assert parse_ordinal("1.2.3.4", "numbered").parts == (1, 2, 3, 4)
    # Roman is near-universal for Articles and Parts.
    assert parse_ordinal("IV", "keyword", "Article").parts == (4,)
    assert parse_ordinal("XII", "keyword", "Chapter").parts == (12,)
    # A lettered sub-item has a position but no absolute path.
    lettered = parse_ordinal("(c)", "lettered")
    assert lettered.parts == (3,) and lettered.relative is True
    assert parse_ordinal("(iv)", "lettered").parts == (4,)
    # Seven letters are also roman numerals. A lone c/d/l/m is the letter (no
    # roman sub-list starts at 100); a lone i/v/x is the numeral.
    assert parse_ordinal("(d)", "lettered").parts == (4,)
    assert parse_ordinal("(m)", "lettered").parts == (13,)
    assert parse_ordinal("(i)", "lettered").parts == (1,)
    assert parse_ordinal("(x)", "lettered").parts == (10,)
    assert parse_ordinal("(iii)", "lettered").parts == (3,)
    # An inserted section keeps its root and carries the suffix.
    inserted = parse_ordinal("12A", "code")
    assert inserted.parts == (12,) and inserted.suffix == "A"
    assert parse_ordinal(None, "numbered") is None
    assert parse_ordinal("not-an-ordinal", "numbered") is None


def test_ordinal_series_comes_from_the_keyword() -> None:
    from pheasant.ingestion.taxonomy import parse_ordinal

    assert parse_ordinal("4", "keyword", "Chapter").series == "chapter"
    assert parse_ordinal("4", "keyword", "Article").series == "article"
    # Aliases collapse onto one series.
    assert (
        parse_ordinal("1", "keyword", "Part").series
        == parse_ordinal("1", "keyword", "Title").series
    )


def test_independent_series_no_longer_mis_parents() -> None:
    """The regression this change exists for.

    Level-only nesting put `§ 12.3` *under* `ARTICLE IV`, because it is deeper
    by pattern depth. `(4,)` is not a prefix of `(12, 3)`, so the ordinal says
    they are different series and the § attaches above the Article instead.
    """
    headings = detect_headings(ORDINAL_LEGAL)
    code = _by_number(headings, "§ 12.3")
    assert code.path == "MASTER SERVICES AGREEMENT > § 12.3 Governing Law"
    assert "Article" not in code.path


def test_decimal_subsections_attach_to_their_roman_article() -> None:
    """Reconciliation across conventions: `IV` parses to `(4,)`, so `4.1` — whose
    prefix is `(4,)` — belongs to it. The document's two ways of writing "four"
    are recognised as the same number."""
    headings = detect_headings(ORDINAL_LEGAL)
    assert (
        _by_number(headings, "4.1").path
        == "MASTER SERVICES AGREEMENT > Article IV Term and Termination > 4.1 Initial Term"
    )


def test_prefix_chaining_within_one_series() -> None:
    headings = detect_headings("§ 12 General\n§ 12.3 Governing Law\n§ 12.4 Notices\n")
    assert _by_number(headings, "§ 12.3").path == "§ 12 General > § 12.3 Governing Law"
    assert _by_number(headings, "§ 12.4").path == "§ 12 General > § 12.4 Notices"


def test_same_number_in_different_series_does_not_parent() -> None:
    """`CHAPTER 4` and `ARTICLE 4` share a number but not a series."""
    headings = detect_headings("CHAPTER 4\nScope of Work\n\nARTICLE 4\nPayment Terms\n")
    article = _by_number(headings, "Article 4")
    assert "Chapter" not in article.path
    assert article.level == 1


def test_inserted_section_is_a_sibling_not_a_child() -> None:
    """`§ 12A` is how a numbering absorbs a new section without renumbering, so
    it sits beside `§ 12` rather than inside it."""
    headings = detect_headings("§ 12 General\n§ 12A Inserted Provision\n§ 13 Other\n")
    levels = {h.number: h.level for h in headings}
    assert levels["§ 12A"] == levels["§ 12"] == levels["§ 13"]
    assert "§ 12 General >" not in _by_number(headings, "§ 12A").path


def test_deep_decimal_chain_reparents_correctly() -> None:
    headings = detect_headings(
        "1. Scope\n1.1 Purpose\n1.1.1 Detail\n1.1.2 More\n1.2 Limits\n2. Terms\n"
    )
    assert _by_number(headings, "1.1.1").path == "1 Scope > 1.1 Purpose > 1.1.1 Detail"
    # 1.2 must climb back out of 1.1, not chain off 1.1.2.
    assert _by_number(headings, "1.2").path == "1 Scope > 1.2 Limits"
    assert _by_number(headings, "2").level == 1


def test_lettered_items_are_siblings_of_each_other() -> None:
    """Regression for a bug found while building this.

    The sibling test compared an ancestor's *reconciled* level against the
    candidate's *pattern* level — different scales — so `(b)` looked deeper than
    its own sibling `(a)` and nested inside it.
    """
    headings = detect_headings("4.2 Termination\n(a) First\n(b) Second\n(c) Third\n")
    lettered = [h for h in headings if h.kind == "lettered"]
    assert len({h.level for h in lettered}) == 1, [h.path for h in lettered]
    for heading in lettered:
        assert heading.path.startswith("4.2 Termination > ")
        assert heading.path.count(PATH_SEPARATOR) == 1


def test_markdown_siblings_stay_siblings() -> None:
    headings = detect_headings("# H\n\n## 2. Triage\n### 2.1 Sev\n### 2.2 Esc\n## 3. Cont\n")
    deep = [h for h in headings if h.title.startswith(("2.1", "2.2"))]
    assert len(deep) == 2
    assert len({h.level for h in deep}) == 1


def test_pattern_level_is_kept_alongside_the_reconciled_level() -> None:
    headings = detect_headings(ORDINAL_LEGAL)
    lettered = _by_number(headings, "(a)")
    assert lettered.pattern_level == 6  # the rule's natural depth
    assert lettered.level < lettered.pattern_level  # reconciled depth is compressed


def test_max_depth_filters_on_the_pattern_level() -> None:
    """`max_depth` stays a statement about the pattern, not about where the
    document happened to place it."""
    shallow = detect_headings(ORDINAL_LEGAL, max_depth=3)
    assert shallow
    assert max(h.pattern_level for h in shallow) <= 3
    assert not any(h.kind == "lettered" for h in shallow)


def test_reconciliation_is_deterministic() -> None:
    first = [h.as_dict() for h in detect_headings(ORDINAL_LEGAL)]
    second = [h.as_dict() for h in detect_headings(ORDINAL_LEGAL)]
    assert first == second


def test_as_dict_carries_the_ordinal() -> None:
    payload = _by_number(detect_headings(ORDINAL_LEGAL), "§ 12.3").as_dict()
    assert payload["ordinal"]["parts"] == [12, 3]
    assert payload["ordinal"]["series"] == "code"
    # A heading with no number carries no ordinal key.
    caps = next(h for h in detect_headings(ORDINAL_LEGAL) if h.kind == "caps")
    assert "ordinal" not in caps.as_dict()


# --- sequence reconciliation -----------------------------------------------


def test_gap_in_a_series_is_reported() -> None:
    from pheasant.ingestion.taxonomy import reconcile_issues

    issues = reconcile_issues(detect_headings("§ 12.3 Governing Law\n§ 12.5 Notices\n"))
    gap = next(i for i in issues if i["kind"] == "gap")
    assert gap["missing"] == [4]
    assert gap["after"] == "§ 12.3" and gap["at"] == "§ 12.5"


def test_duplicate_and_out_of_order_are_reported() -> None:
    from pheasant.ingestion.taxonomy import reconcile_issues

    duplicates = reconcile_issues(detect_headings("4.2 T\n(a) One\n(b) Two\n(b) Two again\n"))
    assert any(i["kind"] == "duplicate" for i in duplicates)

    unordered = reconcile_issues(detect_headings("1. One\n3. Three\n2. Two\n"))
    assert any(i["kind"] == "out_of_order" for i in unordered)


def test_clean_sequences_report_nothing() -> None:
    from pheasant.ingestion.taxonomy import reconcile_issues

    assert reconcile_issues(detect_headings("1. One\n2. Two\n3. Three\n")) == []
    assert reconcile_issues(detect_headings(PROCEDURE)) == []


def test_a_series_starting_mid_document_is_not_a_gap() -> None:
    """An excerpt legitimately begins at 3; guessing otherwise cries wolf on
    every extract."""
    from pheasant.ingestion.taxonomy import reconcile_issues

    assert reconcile_issues(detect_headings("3. Third\n4. Fourth\n")) == []


def test_inserted_section_does_not_create_a_gap() -> None:
    from pheasant.ingestion.taxonomy import reconcile_issues

    assert reconcile_issues(detect_headings("§ 12 A\n§ 12A Inserted\n§ 13 B\n")) == []


# --- surfaces ---------------------------------------------------------------


def test_heading_nodes_persist_the_ordinal(tmp_path: Path) -> None:
    engine = _engine(tmp_path, ORDINAL_LEGAL, taxonomy={"enabled": True})
    engine.sync_source("docs", mode="full")
    nodes = [
        data
        for _node_id, data in engine.graph_builder.graph.node_map().items()
        if data.get("type") == "heading"
    ]
    code = next(n for n in nodes if n.get("number") == "§ 12.3")
    assert code["ordinal_parts"] == [12, 3]
    assert code["ordinal_series"] == "code"
    assert code["pattern_level"] == 4
    # A heading without a number stores an empty ordinal rather than nothing.
    caps = next(n for n in nodes if n.get("kind") == "caps")
    assert caps["ordinal_parts"] == []


def test_taxonomy_endpoint_reports_issues_and_ordinals(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace" / "legal"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "msa.md").write_text(ORDINAL_LEGAL, encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "taxonomy-ordinals",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path / "workspace"),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [],
        }
    )
    client = TestClient(create_app(config))
    client.post(
        "/sources",
        json={
            "name": "legal",
            "type": "document_folder",
            "path": str(workspace),
            "include": ["**/*.md"],
            "taxonomy": {"enabled": True},
            "sync_now": True,
            "wait": True,
        },
    )
    payload = client.get("/taxonomy").json()
    assert payload["issue_count"] >= 2
    document = payload["documents"][0]
    kinds = {issue["kind"] for issue in document["issues"]}
    assert "gap" in kinds and "duplicate" in kinds

    # The § series hangs off the document title, not off the Article.
    def find(nodes: list[dict], number: str) -> dict | None:
        for node in nodes:
            if node.get("number") == number:
                return node
            hit = find(node["children"], number)
            if hit:
                return hit
        return None

    title = document["tree"][0]
    assert any(child.get("number") == "§ 12.3" for child in title["children"])
    code = find(document["tree"], "§ 12.3")
    assert code is not None and code["ordinal"]["parts"] == [12, 3]


def test_reconciled_hierarchy_reaches_the_graph_edges(tmp_path: Path) -> None:
    """`contains` edges must agree with the ordinals, not with pattern depth."""
    engine = _engine(tmp_path, ORDINAL_LEGAL, taxonomy={"enabled": True})
    engine.sync_source("docs", mode="full")
    graph = engine.graph_builder.graph
    nodes = graph.node_map()
    article = next(
        node_id
        for node_id, data in nodes.items()
        if data.get("type") == "heading" and data.get("number") == "Article IV"
    )
    descendants = {
        nodes[target].get("number")
        for (src, target), edge_map in graph.iter_edges()
        if src == article
        for _key, data in edge_map.items()
        if data.get("type") == "contains"
    }
    assert "4.1" in descendants and "4.2" in descendants
    # The independent § series is NOT a child of the Article.
    assert "§ 12.3" not in descendants


def test_numbering_resumes_across_an_interruption() -> None:
    """What the prefix match is actually load-bearing for.

    When a shallower heading closes the open chain, the ordinal is the only
    thing that can put a resumed series back where it belongs. Here ``SCHEDULE
    9`` closes everything, and ``1.2`` must still rejoin ``1. Alpha`` — the
    ancestor walk alone cannot, because ``1. Alpha`` is no longer open.
    """
    headings = detect_headings(
        "1. Alpha\nbody\n\n1.1 First\nbody\n\nSCHEDULE 9\nInterruption\n\n1.2 Second\nbody\n"
    )
    resumed = _by_number(headings, "1.2")
    assert resumed.path == "1 Alpha > 1.2 Second"
    assert "Schedule" not in resumed.path


def test_section_keyword_nests_under_a_chapter_by_ordinal() -> None:
    """`SECTION 4.1` belongs inside `CHAPTER 4`: the prefix says so, and the
    differing keywords are not a reason to refuse it."""
    headings = detect_headings("CHAPTER 4\nScope\n\nSECTION 4.1\nDetail\n")
    assert _by_number(headings, "Section 4.1").path == "Chapter 4 Scope > Section 4.1 Detail"


# ---------------------------------------------------------------------------
# Ordinal disambiguation from the surrounding run, and series identity across
# parents — the two items the first ordinal pass recorded as still open.


def _lettered_values(text: str) -> list[int]:
    headings = detect_headings(text, rules=("lettered",))
    return [h.ordinal.parts[0] for h in headings if h.ordinal]


def test_a_letter_list_that_reaches_i_keeps_counting() -> None:
    """The limitation the character-only reading left behind.

    `i` is both the ninth letter and roman 1. Read alone it is the numeral, so
    a lettered list counting up to its ninth item used to restart at 1 in the
    middle. The run says otherwise: 9 follows 8, 1 does not.
    """
    assert _lettered_values("\n".join(f"({c}) item" for c in "fghijk")) == [6, 7, 8, 9, 10, 11]


def test_a_letter_list_that_reaches_v_keeps_counting() -> None:
    """Same defect, different character — `(v)` after `(u)` is 22, not 5."""
    assert _lettered_values("\n".join(f"({c}) item" for c in "tuvw")) == [20, 21, 22, 23]


def test_a_roman_sub_list_is_still_roman() -> None:
    """The reading that must not regress: a run *opening* at `(i)` is roman."""
    assert _lettered_values("\n".join(f"({r}) item" for r in ["i", "ii", "iii", "iv", "v"])) == [
        1,
        2,
        3,
        4,
        5,
    ]


def test_a_nested_list_opening_at_i_is_roman_not_the_ninth_letter() -> None:
    """`(b)` then `(i)` continues neither reading (9 and 1 both follow 2 badly),
    so the convention stands and the nested list opens at roman 1."""
    assert _lettered_values("(a) x\n(b) y\n(i) sub\n(ii) sub\n(c) z") == [1, 2, 1, 2, 3]


def test_a_non_lettered_heading_breaks_the_run() -> None:
    """A run is consecutive lettered items; a section between them starts a new
    one, so the `(i)` below opens a list rather than continuing `(h)`."""
    headings = detect_headings("(h) eighth\n§ 5 Interruption\n(i) opens a new list\n")
    values = [h.ordinal.parts[0] for h in headings if h.ordinal and h.kind == "lettered"]
    assert values == [8, 1]


def test_parse_ordinal_alone_still_reads_i_as_roman() -> None:
    """The single-heading reading is unchanged — disambiguation needs a run, and
    one item in isolation has none."""
    from pheasant.ingestion.taxonomy import parse_ordinal

    assert parse_ordinal("(i)", "lettered").parts == (1,)


def test_a_gap_is_found_when_an_interruption_reparents_the_tail() -> None:
    """Grouping sequence checks by resolved parent hid this.

    An unnumbered heading between `§ 12.2` and `§ 12.4` re-parents the tail, so
    the two ends of one series sit under different parents. The series is
    identified by its numeric prefix, not by where its members landed, so the
    gap is still reported.
    """
    from pheasant.ingestion.taxonomy import reconcile_issues

    headings = detect_headings("§ 12.1 Alpha\n§ 12.2 Beta\nGENERAL PROVISIONS\n§ 12.4 Delta\n")
    tail = _by_number(headings, "§ 12.4")
    assert "GENERAL PROVISIONS" in tail.path, "precondition: the tail is re-parented"
    gaps = [i for i in reconcile_issues(headings) if i["kind"] == "gap"]
    assert len(gaps) == 1
    assert gaps[0]["after"] == "§ 12.2" and gaps[0]["at"] == "§ 12.4"
    assert gaps[0]["missing"] == [3]


def test_a_per_part_restart_is_not_a_defect() -> None:
    """The other half of the same rule, and why top-level series stay grouped by
    parent: two Parts each numbering their sections from 1 is two series, not
    one that jumped backwards."""
    from pheasant.ingestion.taxonomy import reconcile_issues

    headings = detect_headings("PART I\n1. Scope\n2. Terms\nPART II\n1. Duties\n2. Payment\n")
    assert reconcile_issues(headings) == []


def test_a_container_parents_the_numbering_inside_it() -> None:
    """`PART I` and `1. Scope` are both "one", so the conflicting-series rule
    used to refuse the attachment and flatten the section out of its Part. A
    container exists to hold a numbering it does not participate in."""
    headings = detect_headings("PART I\n1. Scope\n2. Terms\n")
    assert _by_number(headings, "1").path == "Part I > 1 Scope"
    assert _by_number(headings, "2").path == "Part I > 2 Terms"


def test_a_container_does_not_parent_another_container() -> None:
    """`PART II` is beside `PART I`, never inside it — the permission is for a
    *different* series only."""
    headings = detect_headings("PART I\nFirst\nPART II\nSecond\n")
    assert _by_number(headings, "Part II").level == _by_number(headings, "Part I").level
    # Substring-safe: "Part I" is a prefix of "Part II", so test the breadcrumb
    # separator rather than mere containment.
    assert PATH_SEPARATOR not in _by_number(headings, "Part II").path


def test_a_container_parents_a_chapter_with_an_unrelated_number() -> None:
    headings = detect_headings("PART I\nOpening\nCHAPTER 2\nDetail\n")
    assert _by_number(headings, "Chapter 2").path.startswith("Part I")


# ---------------------------------------------------------------------------
# Section-filtered retrieval. The outline is only half the point: "what does
# § 12.3 say about X" needs to be *askable*, not merely labelled.


def _sectioned_client(tmp_path: Path) -> TestClient:
    """A synced knowledge base holding LEGAL with taxonomy on."""
    client, workspace = _client(tmp_path)
    client.post(
        "/sources",
        json={
            "name": "legal",
            "type": "document_folder",
            "path": str(workspace),
            "include": ["**/*.md"],
            "taxonomy": {"enabled": True},
            "sync_now": True,
            "wait": True,
        },
    )
    return client


def _search(client: TestClient, query: str, **extra: object) -> list[dict]:
    body: dict[str, object] = {"query": query, "mode": "text", "max_results": 20}
    body.update(extra)
    return client.post("/search", json=body).json()["results"]


def test_search_can_be_restricted_to_one_section(tmp_path: Path) -> None:
    """The question the feature exists to answer, asked directly."""
    client = _sectioned_client(tmp_path)
    hits = _search(client, "governed Delaware", section="§ 12.3")
    assert hits, "expected the Governing Law section to answer"
    assert all("12.3" in hit["heading_path"] for hit in hits)


def test_a_section_filter_excludes_matches_from_other_sections(tmp_path: Path) -> None:
    """The half that makes it a filter rather than a ranking nudge: a term that
    genuinely appears elsewhere in the document must not come back."""
    client = _sectioned_client(tmp_path)
    unfiltered = _search(client, "terminate breach")
    assert any("4.2" in hit.get("heading_path", "") for hit in unfiltered), "precondition"
    assert _search(client, "terminate breach", section="§ 12.3") == []


def test_naming_a_parent_section_returns_what_is_nested_under_it(tmp_path: Path) -> None:
    """Matching the whole breadcrumb, not just its last crumb, is what makes
    asking for an Article reach its subsections."""
    client = _sectioned_client(tmp_path)
    hits = _search(client, "breach insolvency terminate", section="Article IV")
    assert hits
    assert all("Article IV" in hit["heading_path"] for hit in hits)
    # ...and it reached deeper than the Article's own text.
    assert any(hit["heading_path"].count(">") >= 2 for hit in hits)


def test_a_section_that_matches_nothing_returns_no_results_not_an_error(tmp_path: Path) -> None:
    client = _sectioned_client(tmp_path)
    response = client.post(
        "/search", json={"query": "breach", "mode": "text", "section": "§ 99.9 Nonexistent"}
    )
    assert response.status_code == 200
    assert response.json()["results"] == []


def _deterministic(payload: dict) -> dict:
    """A search response minus the one field that is a measurement.

    `lineage.timing.retrieval_ms` is how long the arms took, so two identical
    requests differ in it by construction. Everything else in a search
    response is a function of the request and the region — which is a property
    worth keeping, and `tests/test_retrieval_lineage.py` asserts that this
    stays the *only* exception rather than the first of several.
    """

    stripped = json.loads(json.dumps(payload))
    timing = (stripped.get("lineage") or {}).get("timing")
    if isinstance(timing, dict):
        timing.pop("retrieval_ms", None)
    return stripped


def test_omitting_the_section_is_unchanged(tmp_path: Path) -> None:
    """Parity: the filter is additive, so a request without it must return
    exactly what it returned before the filter existed."""
    client = _sectioned_client(tmp_path)
    without = client.post("/search", json={"query": "breach", "mode": "hybrid"}).json()
    explicit_none = client.post(
        "/search", json={"query": "breach", "mode": "hybrid", "section": None}
    ).json()
    assert _deterministic(without) == _deterministic(explicit_none)
    blank = client.post(
        "/search", json={"query": "breach", "mode": "hybrid", "section": "  "}
    ).json()
    assert _deterministic(without) == _deterministic(blank)


def test_section_matching_is_substring_and_case_insensitive() -> None:
    from pheasant.search.sqlite_store import section_matches, section_needle

    path = "MASTER SERVICES AGREEMENT > Article IV Term and Termination > § 12.3 Governing Law"
    assert section_matches(path, "§ 12.3")
    assert section_matches(path, "article iv")
    assert section_matches(path, "Governing Law")
    assert not section_matches(path, "Schedule 1")
    # No filter requested: everything matches, including a chunk with no
    # breadcrumb at all, so a corpus without taxonomy is unaffected.
    assert section_matches(None, None) and section_matches("", "")
    assert not section_matches(None, "§ 12.3")
    assert section_needle("  § 12.3 ") == "§ 12.3"


def test_mcp_search_context_accepts_a_section(tmp_path: Path) -> None:
    """Rule 8: additive optional parameter on the public tool surface."""
    from pheasant.mcp_server.tools import PheasantTools

    engine = _engine(tmp_path, LEGAL, taxonomy={"enabled": True})
    engine.sync_source("docs")
    tools = PheasantTools(engine.config)
    payload = tools.search_context(
        engine.config.knowledge_base_id, "governed Delaware", mode="text", section="§ 12.3"
    )
    assert payload["results"]
    assert all("12.3" in hit["heading_path"] for hit in payload["results"])
    # Omitting it is the pre-existing behaviour.
    plain = tools.search_context(engine.config.knowledge_base_id, "governed Delaware", mode="text")
    assert len(plain["results"]) >= len(payload["results"])


def test_a_section_filter_keeps_graph_hits_out_of_hybrid_results(tmp_path: Path) -> None:
    """The arms that cannot be narrowed in SQL still have to be narrowed.

    Graph search matches entity / relationship / heading nodes, none of which
    carry a breadcrumb, so under a section filter they are dropped: a symbol is
    not *inside* § 12.3, and admitting it would answer a question about one
    section with content from anywhere. Browsing the outline itself is what
    ``GET /taxonomy`` is for.
    """
    client = _sectioned_client(tmp_path)
    wide = client.post("/search", json={"query": "governing", "mode": "hybrid"}).json()
    assert wide["counts"]["graph"] > 0, "precondition: the graph arm answers this query"

    narrowed = client.post(
        "/search", json={"query": "governing", "mode": "hybrid", "section": "§ 12.3"}
    ).json()
    assert narrowed["counts"]["graph"] == 0
    assert narrowed["results"], "the section's own chunk should still answer"
    for hit in narrowed["results"]:
        assert "12.3" in (hit.get("heading_path") or ""), hit


# ---------------------------------------------------------------------------
# What the UI reads. The React surfaces are thin over these payloads, so the
# payloads are what gets tested here.


def test_stored_source_config_carries_the_taxonomy_block(tmp_path: Path) -> None:
    """The edit form and the "outline" button both read `config_json`.

    If the block were not persisted there, opening a taxonomy-enabled source in
    the Advanced form would prefill the checkbox as *off* and saving would
    silently switch the feature off — a data-losing round trip, not a cosmetic
    gap.
    """
    client = _sectioned_client(tmp_path)
    record = next(s for s in client.get("/sources").json() if s["name"] == "legal")
    stored = json.loads(record["config_json"])
    assert stored["taxonomy"]["enabled"] is True
    # The two sub-toggles the form also offers must survive the round trip.
    assert stored["taxonomy"]["split_on_sections"] is True
    assert stored["taxonomy"]["graph_nodes"] is True


def test_editing_a_source_can_turn_the_toggle_off_and_on(tmp_path: Path) -> None:
    client = _sectioned_client(tmp_path)
    assert client.put("/sources/legal", json={"taxonomy": {"enabled": False}}).status_code == 200
    off = json.loads(
        next(s for s in client.get("/sources").json() if s["name"] == "legal")["config_json"]
    )
    assert off["taxonomy"]["enabled"] is False
    assert client.put("/sources/legal", json={"taxonomy": {"enabled": True}}).status_code == 200
    on = json.loads(
        next(s for s in client.get("/sources").json() if s["name"] == "legal")["config_json"]
    )
    assert on["taxonomy"]["enabled"] is True


def test_citations_name_the_section_they_came_from(tmp_path: Path) -> None:
    """What the answer's source chips show.

    On a long structured document the file name is nearly no information — every
    citation carries the same one — so the section is the part that tells a
    reader where the answer came from.
    """
    from pheasant.assistant.chat import build_citations

    engine = _engine(tmp_path, LEGAL, taxonomy={"enabled": True})
    engine.sync_source("docs")
    searcher = HybridSearch(SearchStore(engine.state))
    payload = searcher.search_context(
        engine.config.knowledge_base_id, "governed Delaware", mode="text"
    )
    citations = build_citations(payload["results"], limit=5)
    assert citations
    assert any("Governing Law" in (c.get("heading_path") or "") for c in citations), citations


def test_citations_omit_the_section_without_taxonomy(tmp_path: Path) -> None:
    """Parity: a corpus with no taxonomy produces the citation payload it
    always did, with no empty `heading_path` key added."""
    from pheasant.assistant.chat import build_citations

    engine = _engine(tmp_path, LEGAL)  # no taxonomy block at all
    engine.sync_source("docs")
    searcher = HybridSearch(SearchStore(engine.state))
    payload = searcher.search_context(
        engine.config.knowledge_base_id, "governed Delaware", mode="text"
    )
    citations = build_citations(payload["results"], limit=5)
    assert citations
    assert all("heading_path" not in c for c in citations)


def test_passage_citations_carry_the_section_too(tmp_path: Path) -> None:
    """The workflow-facing citation builder must not drop what the other keeps —
    the two produce the same shape on purpose."""
    from pheasant.assistant.chat import passages_to_citations
    from pheasant.assistant.retrieval import Passage

    passage = Passage(
        node_id="n1",
        chunk_id="c1",
        title="msa.md",
        relative_path="msa.md",
        source_id="legal",
        type="chunk",
        score=1.0,
        snippet="…",
        mode="text",
        heading_path="MASTER SERVICES AGREEMENT > § 12.3 Governing Law",
    )
    assert passages_to_citations([passage], 5)[0]["heading_path"].endswith("Governing Law")
    plain = Passage(
        node_id="n2",
        chunk_id="c2",
        title="notes.md",
        relative_path="notes.md",
        source_id="docs",
        type="chunk",
        score=1.0,
        snippet="…",
        mode="text",
    )
    assert "heading_path" not in passages_to_citations([plain], 5)[0]


# ---------------------------------------------------------------------------
# A real, producer-generated PDF. Everything above works on text authored
# directly in this file, which cannot reproduce what a PDF extractor actually
# emits — and the difference found a bug. `agreement.pdf` was written as HTML
# (kept beside it as `agreement.source.html`) and converted by LibreOffice, so
# it has real PDF line layout and real kerning.

STRUCTURED = Path(__file__).parent / "fixtures" / "structured"
AGREEMENT = STRUCTURED / "agreement.pdf"


def _agreement_client(tmp_path: Path) -> TestClient:
    import shutil

    workspace = tmp_path / "workspace" / "contracts"
    workspace.mkdir(parents=True, exist_ok=True)
    shutil.copy(AGREEMENT, workspace / "agreement.pdf")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "agreement",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path / "workspace"),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [],
        }
    )
    client = TestClient(create_app(config))
    assert (
        client.post(
            "/sources",
            json={
                "name": "contracts",
                "type": "document_folder",
                "path": str(workspace),
                "include": ["**/*.pdf"],
                "taxonomy": {"enabled": True},
                "sync_now": True,
                "wait": True,
            },
        ).status_code
        == 200
    )
    return client


def test_a_keyword_line_needs_a_real_ordinal_not_a_stray_word() -> None:
    """The bug the real PDF found, isolated.

    PDF text arrives already broken into layout lines, so a paragraph's
    continuation can *begin* with "Section" or "Part" — and this extractor
    renders "of" as "o f", which is ordinary kerning. The keyword pattern is
    case-insensitive, so its single-letter alternative read that ``o`` as
    ordinal 15 and invented a heading in the middle of a sentence. Worse, the
    invented heading became a **parent**, re-parenting genuine sections under
    it.
    """
    assert detect_headings("Section o f this Agreement unless stated otherwise.") == []
    assert detect_headings("part o f its assets.") == []
    # A real letter citation is written in capitals, and still works.
    assert [h.number for h in detect_headings("PART A\nGeneral Provisions\n")] == ["Part A"]
    assert [h.number for h in detect_headings("ANNEX B\nFee Table\n")] == ["Annex B"]

    # A *numeric* citation opening a prose line is the case the ordinal check
    # cannot catch — "1" is a perfectly good ordinal — so the keyword branch
    # also runs the same prose filter every other rule already used. It had
    # none before: only a line-length check, and this line is short enough.
    assert (
        detect_headings(
            "Part 1 of the agreement shall be construed as follows, and the parties agree."
        )
        == []
    )


def test_the_real_agreement_outline_is_correct(tmp_path: Path) -> None:
    """End to end on producer-generated bytes: extract, detect, reconcile, index."""
    client = _agreement_client(tmp_path)
    document = client.get("/taxonomy", params={"path": "agreement.pdf"}).json()["documents"]
    assert document, "no outline extracted from a real PDF"

    labels: list[str] = []

    def walk(nodes: list[dict], depth: int = 0) -> None:
        for node in nodes:
            labels.append(f"{depth}:{(node['number'] or '').strip()} {node['title']}".strip())
            walk(node["children"], depth + 1)

    walk(document[0]["tree"])
    flat = " | ".join(labels)

    # Containers parent the numbering inside them (depth 1 under the Part), and
    # each Part's sections restart from 1 without that reading as a defect.
    assert "0:Part I General Terms" in labels
    assert "1:Article IV Term and Termination" in labels
    assert "2:4.1 Initial Term" in labels
    assert "0:Part II Commercial Terms" in labels
    assert "1:1 Charges" in labels and "1:2 Expenses" in labels
    # No invented heading from a mid-sentence "Section o f" / "part o f" line.
    assert "Part o" not in flat and "Section o" not in flat
    # Both real gaps are reported, and only those two: the § series skips 12.3,
    # and the Articles jump I -> IV. Each is a genuine defect in the drafting
    # this document deliberately imitates, and each is found in a *different*
    # numbering series, which is what makes them independent evidence.
    gaps = {
        (g["series"], g["after"], g["at"], tuple(g["missing"]))
        for g in document[0]["issues"]
        if g["kind"] == "gap"
    }
    assert gaps == {
        ("code", "§ 12.2", "§ 12.4", (3,)),
        ("article", "Article I", "Article IV", (2, 3)),
    }


def test_the_real_agreement_is_searchable_by_section(tmp_path: Path) -> None:
    """The question the whole feature exists to answer, on real bytes."""
    client = _agreement_client(tmp_path)

    def sections(query: str, section: str) -> list[str]:
        body = {"query": query, "mode": "text", "max_results": 20, "section": section}
        return [
            h["heading_path"].split(" > ")[-1]
            for h in client.post("/search", json=body).json()["results"]
        ]

    assert sections("governing law jurisdiction", "§ 12.4") == ["§ 12.4 Governing Law"]
    # Naming the Article reaches a lettered sub-item nested two levels under it.
    assert sections("insolvency receiver", "Article IV") == ["(b) Insolvency"]
    # And a term from another section does not leak into this one.
    assert sections("insolvency receiver", "§ 12.4") == []
