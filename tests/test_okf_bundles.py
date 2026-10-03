"""Open Knowledge Format bundles: per-file parsing, bundle detection, graph.

OKF (Google's knowledge-catalog spec, v0.2) is "a directory of Markdown files
with YAML frontmatter". Nothing here registers a bundle: a bundle is
*detected* inside whatever document collection holds it, so most of what is
tested is the two directions detection can go wrong — a bundle that is not
seen, and a folder of ordinary Markdown (a Hugo site, a docs tree) that is
mistaken for one. The fixture bundle follows the spec's own Appendix A.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.graph.okf import SourceArtifact, apply_source, detect_bundles
from pheasant.ingestion.okf import parse_okf, trust_tier
from pheasant.ingestion.pipeline import ParsedArtifact
from pheasant.sync.engine import SyncEngine
from pheasant.sync.remote_worker import IncompatibleResult, parsed_from_wire, parsed_to_wire

INCOME_STATEMENT = """---
type: Metric
title: Income statement (fiscal year)
description: Headline income-statement figures for a fiscal year.
tags: [finance, income-statement]
status: stable
generated: { by: reference_agent/gemini-2.5-pro, at: 2026-06-20T22:53:05Z }
verified: { by: human:ahormati, at: 2026-06-25T09:00:00Z }
stale_after: 2026-12-31T00:00:00Z
sources:
  - id: fpa-handbook
    resource: https://wiki.acme/finance/fpa-handbook
    title: FP&A reporting handbook
---

# Definition
The income statement reports [revenue](../computations/revenue.md) and
[gross profit](../computations/profit.md) for a fiscal year, per the FP&A
reporting handbook.[^fpa-handbook]

[^fpa-handbook]: FP&A reporting handbook
"""

REVENUE = """---
type: Attested Computation
title: Revenue for fiscal year
description: Recognized revenue for a fiscal year, per Finance's definition.
tags: [finance, revenue]
runtime: bigquery
parameters:
  - { name: year, type: integer, required: true }
executor:
  resource: references/skills/run-on-bq.md
  receipt: [job_id, executed_sql, result]
attester:
  resource: references/attesters/sql-equality.py
generated: { by: reference_agent/gemini-2.5-pro, at: 2026-06-28T14:00:00Z }
verified: { by: human:ahormati, at: 2026-06-25T09:00:00Z }
sources:
  - id: rev-policy
    resource: https://wiki.acme/finance/revenue-recognition
    title: Revenue recognition policy
    author: team:finance-fpa
  - id: exec-rev-dash
    resource: dashboards/exec-revenue
    usage_count: 5000
  - id: orders
    resource: tables/orders.md
usage_window: { from: 2026-06-01T00:00:00Z, to: 2026-06-30T00:00:00Z }
---

# Computation

```sql
SELECT SUM(amount) AS revenue  -- [not a link](tables/orders.md)
FROM finance.recognized_revenue
WHERE fiscal_year = @year
```

Booked against [orders](/tables/orders.md).[^rev-policy] Corroborated by the
dashboard.[^exec-rev-dash] Per policy again.[^rev-policy]

[^rev-policy]: Revenue recognition policy
[^exec-rev-dash]: Executive revenue dashboard
"""

PROFIT = """---
type: Attested Computation
title: Gross profit for fiscal year
runtime: dbt
computation: references/computations/profit.sql
parameters:
  - { name: year, type: integer, required: true }
executor: { resource: references/skills/run-on-bq.md, receipt: [run_id] }
verified:
  - { by: process:finance-nightly, at: 2026-06-12T08:00:00Z }
stale_after: 2026-06-15T00:00:00Z
---

Gross profit by segment.
"""

ORDERS = """---
type: BigQuery Table
title: Customer Orders
resource: https://console.cloud.google.com/bigquery?p=acme&d=sales&t=orders
tags: [sales, finance]
sources:
  - resource: all queries in BigQuery project acme
---

# Schema

| Column | Type |
|---|---|
| `order_id` | STRING |
"""

MARGIN = """---
type: Metric
title: Gross Margin
tags: [finance]
---

Replaces the [legacy definition](margin-legacy.md).
"""

MARGIN_LEGACY = """---
type: Metric
title: Gross Margin (legacy)
status: deprecated
---

Retired. The current definition is [Gross Margin](./margin.md).
"""

SKILL = """---
type: Skill
title: Run on BigQuery
---

Bind parameters, submit the job, return a receipt.
"""

ROOT_INDEX = """# Subdirectories

* [metrics](metrics/) - Business definitions.
* [computations](computations/index.md) - Sanctioned SQL.
* [tables](tables/index.md) - Tables the bundle grounds against.
"""

METRICS_INDEX = """# Metric

* [Income statement](income-statement.md) - Headline figures.
* [Gross Margin](margin.md) - Current margin.
* [Gross Margin (legacy)](margin-legacy.md) - Deprecated.
"""

LOG = """# Bundle history

## 2026-06-30
* **Update**: Re-generated [revenue](/computations/revenue.md).

## 2026-04-15
* **Deprecation**: Retired `metrics/margin-legacy.md` in favour of
  [margin](/metrics/margin.md).
* **Update**: Touched [revenue](/computations/revenue.md) again.
"""


def write_bundle(root: Path) -> Path:
    files = {
        "index.md": ROOT_INDEX,
        "log.md": LOG,
        "README.md": "# Acme finance knowledge\n\nThis repository is an OKF bundle.\n",
        "metrics/index.md": METRICS_INDEX,
        "metrics/income-statement.md": INCOME_STATEMENT,
        "metrics/margin.md": MARGIN,
        "metrics/margin-legacy.md": MARGIN_LEGACY,
        "computations/index.md": "* [Revenue](revenue.md) - Revenue.\n",
        "computations/revenue.md": REVENUE,
        "computations/profit.md": PROFIT,
        "tables/index.md": "* [orders](orders.md) - Orders.\n",
        "tables/orders.md": ORDERS,
        "references/skills/run-on-bq.md": SKILL,
        "references/attesters/sql-equality.py": "def attest(receipt):\n    return True\n",
    }
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _config(tmp_path: Path, corpus: Path, **graph: Any) -> PheasantConfig:
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "okf",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "graph": graph,
            "sources": [{"name": "kb", "type": "document_folder", "path": str(corpus)}],
        }
    )


def _okf_edges(engine: SyncEngine) -> set[tuple[str, str, str]]:
    edges = set()
    for (source, target), edge_map in engine.graph_builder.graph.iter_edges():
        for data in edge_map.values():
            if data.get("enrichment_pass") == "okf":
                edges.add((source, target, data["type"]))
    return edges


def _art(relative: str) -> str:
    return f"file:kb:{relative}:branch=none"


# -- per-file parsing --------------------------------------------------------


def test_concept_frontmatter_families_are_read_as_written() -> None:
    okf = parse_okf("computations/revenue.md", REVENUE)
    assert okf is not None
    assert okf["role"] == "concept"
    assert okf["type"] == "Attested Computation"
    # Timestamps keep the author's spelling. PyYAML's safe loader would make
    # this a datetime that re-renders as "+00:00" -- a different string in a
    # graph whose generation id digests what it stores.
    assert okf["generated"] == {
        "by": "reference_agent/gemini-2.5-pro",
        "at": "2026-06-28T14:00:00Z",
    }
    # A bare `verified` mapping is a one-element list (OKF §5.2).
    assert okf["verified"] == [{"by": "human:ahormati", "at": "2026-06-25T09:00:00Z"}]
    assert okf["trust_tier"] == "human-reviewed"
    assert okf["status"] == "stable" and okf["status_declared"] is False
    sources = {source["id"]: source for source in okf["sources"]}
    assert sources["exec-rev-dash"]["usage_count"] == 5000
    # The sibling `usage_window` frames every entry that has none of its own.
    assert sources["exec-rev-dash"]["usage_window"] == {
        "from": "2026-06-01T00:00:00Z",
        "to": "2026-06-30T00:00:00Z",
    }
    # Footnote references are counted by label; definitions are not.
    assert okf["citations"] == {"exec-rev-dash": 1, "rev-policy": 2}
    # A link inside a fenced block is code, not a relationship.
    assert [link["target"] for link in okf["links"]] == ["/tables/orders.md"]
    computation = okf["computation"]
    assert computation["runtime"] == "bigquery"
    assert computation["parameters"] == [{"name": "year", "type": "integer", "required": True}]
    assert computation["executor"]["receipt"] == ["job_id", "executed_sql", "result"]
    assert computation["inline"] is True


def test_trust_tiers_and_legacy_timestamp() -> None:
    assert trust_tier([]) == "unverified"
    assert trust_tier([{"by": "process:nightly", "at": None}]) == "machine-confirmed"
    assert trust_tier([{"by": "process:x", "at": None}, {"by": "human:a", "at": None}]) == (
        "human-reviewed"
    )
    okf = parse_okf("m.md", "---\ntype: Metric\ntimestamp: '2026-05-28T22:53:05+00:00'\n---\n")
    assert okf is not None
    # OKF §13.1: a v0.1 `timestamp` stands in for `generated.at`.
    assert okf["generated"] == {"by": None, "at": "2026-05-28T22:53:05+00:00"}
    assert okf["trust_tier"] == "unverified"


@pytest.mark.parametrize(
    "relative,text",
    [
        ("notes/plain.md", "# Just a note\n\nNo frontmatter here.\n"),
        ("notes/hugo.md", "---\ntitle: Hello\ndate: 2026-01-01\n---\nBody\n"),
        ("notes/empty-type.md", "---\ntype: ''\n---\nBody\n"),
        ("notes/list.md", "---\n- not\n- a mapping\n---\nBody\n"),
        ("notes/broken.md", "---\ntype: [unclosed\n---\nBody\n"),
        # Hugo leaf bundles are `index.md` *with* frontmatter; an OKF index
        # has none unless it declares `okf_version` (§8, §12).
        ("posts/hello/index.md", "---\ntitle: Hello\n---\n* [x](y.md) - z\n"),
        ("log.md", "# Changes\n\n* nothing dated here\n"),
        ("notes/code.py", "type = 'Metric'\n"),
    ],
)
def test_files_without_okf_structure_parse_to_nothing(relative: str, text: str) -> None:
    assert parse_okf(relative, text) is None


def test_index_and_log_roles() -> None:
    index = parse_okf("index.md", ROOT_INDEX)
    assert index is not None and index["role"] == "index"
    assert index["entries"][0] == {
        "title": "metrics",
        "target": "metrics/",
        "description": "Business definitions.",
        "section": "Subdirectories",
    }
    declared = parse_okf("index.md", '---\nokf_version: "0.2"\n---\n# Empty\n')
    assert declared is not None and declared["okf_version"] == "0.2"

    log = parse_okf("log.md", LOG)
    assert log is not None and log["role"] == "log"
    assert log["entries"][1] == {
        "date": "2026-04-15",
        "action": "Deprecation",
        "targets": ["metrics/margin-legacy.md"],
    }


def test_frontmatter_is_bounded() -> None:
    sources = "".join(f"  - resource: https://example.com/{i}\n" for i in range(1_000))
    okf = parse_okf("big.md", f"---\ntype: Metric\nsources:\n{sources}---\n")
    assert okf is not None
    assert len(okf["sources"]) == 200


# -- bundle detection --------------------------------------------------------


def _artifacts(files: dict[str, str]) -> list[SourceArtifact]:
    return [
        SourceArtifact(_art(relative), relative, parse_okf(relative, text))
        for relative, text in sorted(files.items())
    ]


CONCEPT = "---\ntype: Metric\n---\nBody\n"
SIGNALLED = "---\ntype: Metric\ngenerated: { by: human:a }\n---\nBody\n"


def test_shallowest_index_is_the_bundle_and_sub_indexes_are_listings() -> None:
    files = {
        "README.md": "# repo\n",
        "SPEC.md": "# The spec, no frontmatter\n",
        "src/prompts/one.md": "# prompt\n",
        "src/prompts/two.md": "# prompt\n",
        "bundles/a/index.md": "* [m](metrics/index.md) - metrics\n",
        "bundles/a/metrics/index.md": "* [x](x.md) - x\n",
        "bundles/a/metrics/x.md": CONCEPT,
        "bundles/a/metrics/y.md": CONCEPT,
        "bundles/b/index.md": "* [t](t.md) - t\n",
        "bundles/b/t.md": CONCEPT,
        "bundles/b/u.md": CONCEPT,
    }
    bundles = detect_bundles(_artifacts(files))
    # Two sibling bundles, not one `bundles/` bundle holding both, and not
    # the repository root, whose spec and prompts are ordinary Markdown.
    assert [(b.root, b.detection) for b in bundles] == [
        ("bundles/a", "index"),
        ("bundles/b", "index"),
    ]


def test_type_frontmatter_alone_is_not_a_bundle() -> None:
    # A Hugo content tree routes pages with `type:` too. With no OKF listing,
    # log, or producer key, it stays ordinary Markdown.
    hugo = {f"content/post-{i}.md": CONCEPT for i in range(5)}
    assert detect_bundles(_artifacts(hugo)) == []
    corroborated = {**hugo, "content/post-0.md": SIGNALLED}
    assert [(b.root, b.detection) for b in detect_bundles(_artifacts(corroborated))] == [
        ("", "conformant_tree")
    ]


def test_explicit_version_and_conformance_share() -> None:
    explicit = {"kb/index.md": '---\nokf_version: "0.2"\n---\n', "kb/only.md": CONCEPT}
    assert [(b.root, b.detection, b.okf_version) for b in detect_bundles(_artifacts(explicit))] == [
        ("kb", "explicit", "0.2")
    ]
    # Mostly prose with two typed files is a docs site, not a bundle.
    mixed = {
        "docs/index.md": "* [a](a.md) - a\n",
        "docs/a.md": CONCEPT,
        "docs/b.md": CONCEPT,
        **{f"docs/guide-{i}.md": "# guide\n" for i in range(4)},
    }
    assert detect_bundles(_artifacts(mixed)) == []


def test_supersedes_is_inferred_only_from_a_single_successor() -> None:
    from pheasant.graph.okf import plan_source

    legacy = "---\ntype: Metric\nstatus: deprecated\n---\nUse [a](a.md).\n"
    ambiguous = "---\ntype: Metric\nstatus: deprecated\n---\nSee [a](a.md), [b](b.md).\n"
    other_type = "---\ntype: Policy\n---\nBody\n"

    def supersedes(files: dict[str, str]) -> set[tuple[str, str]]:
        plan = plan_source("kb-id", "kb", _artifacts({"index.md": "* [a](a.md) - a\n", **files}))
        return {(e.source, e.target) for e in plan.edges if e.type == "supersedes"}

    assert supersedes({"a.md": CONCEPT, "b.md": CONCEPT, "old.md": legacy}) == {
        (_art("a.md"), _art("old.md"))
    }
    # Two current metrics named: related work, not a successor.
    assert supersedes({"a.md": CONCEPT, "b.md": CONCEPT, "old.md": ambiguous}) == set()
    # A successor has to be the same kind of thing.
    assert supersedes({"a.md": other_type, "b.md": CONCEPT, "old.md": legacy}) == set()


# -- the graph ---------------------------------------------------------------


@pytest.fixture
def synced(tmp_path: Path):
    corpus = write_bundle(tmp_path / "acme")
    engine = SyncEngine(_config(tmp_path, corpus))
    engine.sync_source("kb", "full")
    try:
        yield engine, corpus
    finally:
        engine.close()


def test_bundle_graph_carries_every_relationship_the_spec_names(synced) -> None:
    engine, _corpus = synced
    graph = engine.graph_builder.graph
    bundle = graph.nodes["okf_bundle:kb:."]
    assert bundle["type"] == "okf_bundle"
    assert bundle["detection"] == "index"
    assert bundle["concept_count"] == 7
    assert bundle["type_counts"] == {
        "Attested Computation": 2,
        "BigQuery Table": 1,
        "Metric": 3,
        "Skill": 1,
    }
    assert bundle["trust_counts"] == {
        "human-reviewed": 2,
        "machine-confirmed": 1,
        "unverified": 4,
    }
    assert bundle["status_counts"] == {"deprecated": 1, "stable": 6}
    edges = _okf_edges(engine)
    metric_hub = "okf_type:kb:.:metric"
    revenue = _art("computations/revenue.md")
    profit = _art("computations/profit.md")
    income = _art("metrics/income-statement.md")
    expected = {
        ("source:okf:kb", "okf_bundle:kb:.", "contains"),
        ("okf_bundle:kb:.", metric_hub, "contains"),
        (metric_hub, income, "contains"),
        ("okf_bundle:kb:.", _art("index.md"), "contains"),
        ("okf_bundle:kb:.", _art("log.md"), "contains"),
        # Body links, relative and bundle-absolute (§6.1).
        (income, revenue, "links_to"),
        (income, profit, "links_to"),
        (revenue, _art("tables/orders.md"), "links_to"),
        # Provenance into the bundle, written bundle-root-relative from inside
        # `computations/` exactly as the spec's own examples do.
        (revenue, _art("tables/orders.md"), "derived_from"),
        # Attested computation contract.
        (revenue, _art("references/skills/run-on-bq.md"), "executed_by"),
        (revenue, _art("references/attesters/sql-equality.py"), "attested_by"),
        (profit, _art("references/skills/run-on-bq.md"), "executed_by"),
        # Listings: a directory target resolves to its index.md.
        (_art("index.md"), _art("metrics/index.md"), "links_to"),
        (_art("metrics/index.md"), _art("metrics/margin-legacy.md"), "links_to"),
        (_art("log.md"), revenue, "links_to"),
        (_art("log.md"), _art("metrics/margin-legacy.md"), "links_to"),
        (income, "tag:kb:.:finance", "tagged_with"),
        (_art("metrics/margin.md"), _art("metrics/margin-legacy.md"), "supersedes"),
    }
    assert expected <= edges
    # The README is the repository's, not a concept, and joins nothing.
    assert not any(_art("README.md") in (s, t) for s, t, _ in edges)


def test_edge_attributes_carry_the_spec_metadata(synced) -> None:
    engine, _corpus = synced
    graph = engine.graph_builder.graph
    revenue = _art("computations/revenue.md")

    def edge(source: str, target: str, edge_type: str) -> dict[str, Any]:
        for data in graph.get_edge_data(source, target, default={}).values():
            if data.get("type") == edge_type:
                return data
        raise AssertionError((source, target, edge_type))

    concept = edge("okf_type:kb:.:attested-computation", revenue, "contains")
    assert concept["concept_id"] == "computations/revenue"
    assert concept["trust_tier"] == "human-reviewed"
    dashboard = [
        target
        for source, target, kind in _okf_edges(engine)
        if source == revenue and kind == "derived_from" and "exec-revenue" in target
    ]
    assert len(dashboard) == 1
    provenance = edge(revenue, dashboard[0], "derived_from")
    assert provenance["usage_count"] == 5000 and provenance["citations"] == 1
    stub = graph.nodes[dashboard[0]]
    assert stub["type"] == "external_reference"
    assert stub["resource_kind"] == "unresolved_path"
    executor = edge(revenue, _art("references/skills/run-on-bq.md"), "executed_by")
    assert executor["receipt"] == ["job_id", "executed_sql", "result"]
    log = edge(_art("log.md"), revenue, "links_to")
    assert log["relation"] == "log_entry"
    assert [entry["date"] for entry in log["entries"]] == ["2026-06-30", "2026-04-15"]
    assert log["last_date"] == "2026-06-30"
    # A scope descriptor is provenance nothing can follow -- still a node.
    scope = [
        graph.nodes[target]
        for source, target, kind in _okf_edges(engine)
        if source == _art("tables/orders.md") and kind == "derived_from"
    ]
    assert [node["resource_kind"] for node in scope] == ["scope"]
    # `computation:` names a .sql file this source does not index.
    computed = [t for s, t, k in _okf_edges(engine) if s == _art("computations/profit.md")]
    assert any("profit.sql" in target for target in computed)


def test_unchanged_resync_moves_nothing_and_edits_retract_precisely(synced) -> None:
    engine, corpus = synced
    before = (_okf_edges(engine), engine.loaded_graph_generation)
    engine.sync_source("kb", "incremental")
    assert (_okf_edges(engine), engine.loaded_graph_generation) == before

    income = _art("metrics/income-statement.md")
    profit = _art("computations/profit.md")
    assert (income, profit, "links_to") in before[0]
    path = corpus / "metrics/income-statement.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[gross profit](../computations/profit.md)", "gross profit"
        ),
        encoding="utf-8",
    )
    engine.sync_source("kb", "incremental")
    after = _okf_edges(engine)
    assert (income, profit, "links_to") not in after
    assert before[0] - after == {(income, profit, "links_to")}


def test_a_file_that_stops_being_okf_leaves_its_bundle(synced) -> None:
    engine, corpus = synced
    skill = _art("references/skills/run-on-bq.md")
    assert engine.graph_builder.graph.nodes[skill]["okf"]["type"] == "Skill"
    (corpus / "references/skills/run-on-bq.md").write_text("# Run on BigQuery\n", encoding="utf-8")
    engine.sync_source("kb", "incremental")
    graph = engine.graph_builder.graph
    assert graph.nodes[skill].get("okf") is None
    assert "okf_type:kb:.:skill" not in graph
    # Still a link target of the computations, just no longer a concept.
    assert (_art("computations/revenue.md"), skill, "executed_by") in _okf_edges(engine)


def test_turning_detection_off_retracts_only_okf_edges(tmp_path: Path) -> None:
    corpus = write_bundle(tmp_path / "acme")
    engine = SyncEngine(_config(tmp_path, corpus))
    try:
        engine.sync_source("kb", "full")
        income = _art("metrics/income-statement.md")
        revenue = _art("computations/revenue.md")
        kinds = {
            data["type"]
            for data in engine.graph_builder.graph.get_edge_data(income, revenue).values()
        }
        # The reference resolver and the OKF pass both draw this pair.
        assert {"references", "links_to"} <= kinds
        engine.config.graph.okf_bundles = False
        report = apply_source(engine.graph_builder, "kb")
        assert report["bundles"] == [] and report["removed_edges"] > 0
        graph = engine.graph_builder.graph
        assert _okf_edges(engine) == set()
        assert not any(a.get("enrichment_pass") == "okf" for _, a in graph.iter_nodes())
        assert {d["type"] for d in graph.get_edge_data(income, revenue).values()} == {"references"}
    finally:
        engine.close()


def test_turning_detection_off_survives_a_restart(tmp_path: Path) -> None:
    corpus = write_bundle(tmp_path / "acme")
    engine = SyncEngine(_config(tmp_path, corpus))
    try:
        engine.sync_source("kb", "full")
        assert _okf_edges(engine)
    finally:
        engine.close()
    engine = SyncEngine(_config(tmp_path, corpus, okf_bundles=False))
    try:
        engine.sync_source("kb", "full")
        assert _okf_edges(engine) == set()
    finally:
        engine.close()
    # The rows, not just the working set: a restart reads what was committed.
    engine = SyncEngine(_config(tmp_path, corpus, okf_bundles=False))
    try:
        assert _okf_edges(engine) == set()
    finally:
        engine.close()


def test_a_folder_without_a_bundle_is_untouched(tmp_path: Path) -> None:
    corpus = tmp_path / "docs"
    corpus.mkdir()
    (corpus / "index.md").write_text("# Docs\n\n* [Guide](guide.md) - how to\n", encoding="utf-8")
    (corpus / "guide.md").write_text("# Guide\n\nSee [index](index.md).\n", encoding="utf-8")
    (corpus / "page.md").write_text("---\ntitle: Page\n---\nBody\n", encoding="utf-8")

    def graph_of(enabled: bool, where: Path) -> tuple[set, set]:
        engine = SyncEngine(_config(where, corpus, okf_bundles=enabled))
        try:
            engine.sync_source("kb", "full")
            graph = engine.graph_builder.graph
            nodes = {node_id for node_id, _ in graph.iter_nodes()}
            edges = {(s, t, d["type"]) for (s, t), em in graph.iter_edges() for d in em.values()}
            return nodes, edges
        finally:
            engine.close()

    on = graph_of(True, tmp_path / "on")
    off = graph_of(False, tmp_path / "off")
    assert on == off
    assert not any(node.startswith(("okf_", "tag:")) for node in on[0])


def test_the_pass_walks_the_source_not_the_graph(synced, monkeypatch) -> None:
    engine, _corpus = synced
    graph = engine.graph_builder.graph

    def refuse(*_args: Any, **_kwargs: Any):
        raise AssertionError("the OKF pass walked the whole graph")

    monkeypatch.setattr(graph, "iter_nodes", refuse)
    monkeypatch.setattr(graph, "iter_edges", refuse)
    monkeypatch.setattr(graph, "edges", refuse)
    before = _okf_edges_unpatched(graph)
    report = apply_source(engine.graph_builder, "kb")
    assert report["removed_edges"] == 0 and report["removed_nodes"] == 0
    assert _okf_edges_unpatched(graph) == before


def _okf_edges_unpatched(graph: Any) -> set[tuple[str, str, str]]:
    with graph.reading():
        return {
            (source, target, data["type"])
            for (source, target), edge_map in graph._edges.items()
            for data in edge_map.values()
            if data.get("enrichment_pass") == "okf"
        }


# -- the worker boundary -----------------------------------------------------


def _parsed(relative: str, okf: dict[str, Any] | None) -> ParsedArtifact:
    return ParsedArtifact(
        id=_art(relative),
        source_id="kb",
        path=relative,
        relative_path=relative,
        type="markdown_note",
        mime_type=None,
        size_bytes=1,
        sha256="0" * 64,
        mtime="2026-01-01T00:00:00Z",
        git_branch=None,
        git_commit=None,
        chunks=[],
        okf=okf,
    )


def test_okf_reading_crosses_the_worker_wire() -> None:
    okf = parse_okf("metrics/margin.md", MARGIN)
    assert parsed_from_wire(parsed_to_wire(_parsed("metrics/margin.md", okf))).okf == okf
    # A worker from before OKF parsing omits the key; for Markdown that is an
    # answer the indexer must not commit, since it would decide bundle
    # membership by which replica happened to prepare the file.
    old = parsed_to_wire(_parsed("metrics/margin.md", okf))
    old.pop("okf")
    with pytest.raises(IncompatibleResult):
        parsed_from_wire(old)
    other = parsed_to_wire(_parsed("src/app.py", None))
    other.pop("okf")
    assert parsed_from_wire(other).okf is None


def test_memory_records_are_not_read_as_okf() -> None:
    from types import SimpleNamespace

    from pheasant.config.schema import SourceType
    from pheasant.ingestion.pipeline import okf_for_source

    record = "---\ntype: fact\nscope: org\n---\nA remembered fact.\n"
    memory = SimpleNamespace(type=SourceType.memory)
    notes = SimpleNamespace(type=SourceType.markdown_folder)
    assert okf_for_source(memory, "r.md", record) is None
    assert okf_for_source(notes, "r.md", record) is not None
