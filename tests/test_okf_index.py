"""OKF bundles wherever they arrive, and what each document's nearest index says.

Acceptance:

1. A repository that *is* a bundle (root ``index.md``, developer docs and
   ``.github/`` beside the concepts) is one bundle at its root, not fragments.
2. Every Markdown document in a bundle has one ``indexed_by`` edge to its
   nearest ``index.md``, saying whether that index lists it (by file or by a
   folder) and what the entry states; boilerplate gets none.
3. An entry's words are read deterministically (superseded, deprecated,
   stale, draft, current, a stated date), and the verdict at retrieval puts
   the index first, says when it and the document disagree, and reads
   ``stale_after`` against the clock without storing it.
4. A search hit or an answer passage inside a bundle carries that block, one
   hop, and the answer's prompt says it; on the graph service it is one call.
5. A folder uploaded directly keeps its tree, so a bundle uploaded as files is
   detected like a folder source or a ZIP, and no path leaves the upload root.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.assistant.chat import build_prompt
from pheasant.graph import okf_index
from pheasant.graph.okf import SourceArtifact, detect_bundles
from pheasant.ingestion.landing import safe_relative_path, store_upload
from pheasant.ingestion.okf import parse_okf
from pheasant.sync.engine import SyncEngine
from tests.test_okf_bundles import _art, _config, write_bundle

# --------------------------------------------------------------- reading words


@pytest.mark.parametrize(
    ("title", "description", "section", "statement"),
    [
        ("Margin (old)", "Superseded by the 2026 definition.", None, "superseded"),
        ("Margin (old)", "Use margin.md instead.", None, "superseded"),
        ("Legacy margin", None, "Deprecated", "deprecated"),
        ("Orders", "No longer maintained", None, "deprecated"),
        ("Revenue", "Needs review after the Q3 restatement", None, "stale"),
        ("Revenue", "Out of date", None, "stale"),
        ("Churn", "Draft", None, "draft"),
        ("Revenue", "The canonical definition", None, "current"),
        ("Revenue", "Recognized revenue.", None, None),
    ],
)
def test_an_entry_is_read_without_a_model(title, description, section, statement):
    assert okf_index.classify(title, description, section)["statement"] == statement


def test_the_strongest_statement_wins_and_a_date_is_kept():
    words = okf_index.classify("Margin", "Current until replaced by v2; updated 2026-07-01", None)
    assert words["statement"] == "superseded" and words["stated_date"] == "2026-07-01"


# ------------------------------------------------------------- the verdict

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _edge(**changes: Any) -> dict[str, Any]:
    edge = okf_index.edge_attrs("metrics/index.md", 0, {"title": "Margin"}, "file")
    edge.update(changes)
    return edge


@pytest.mark.parametrize(
    ("edge", "okf", "verdict", "conflict"),
    [
        (_edge(), {"status": "stable"}, "current", False),
        (_edge(statement="superseded"), {"status": "stable"}, "superseded", True),
        (_edge(statement="deprecated"), {"status": "deprecated"}, "deprecated", False),
        (_edge(statement="current"), {"status": "deprecated"}, "deprecated", True),
        (_edge(), {"status": "stable", "stale_after": "2026-06-15T00:00:00Z"}, "stale", False),
        (_edge(statement="stale"), {"status": "stable"}, "stale", True),
        (_edge(statement="draft"), {"status": "stable"}, "draft", False),
        (_edge(listed=False, listed_via=None), {"status": "stable"}, "unlisted", False),
        (
            _edge(stated_date="2026-08-01"),
            {"status": "stable", "verified": [{"by": "h", "at": "2026-06-25T09:00:00Z"}]},
            "stale",
            False,
        ),
    ],
)
def test_the_index_comes_first_and_disagreement_is_said(edge, okf, verdict, conflict):
    block = okf_index.freshness(edge, okf, NOW)
    assert block["verdict"] == verdict and block["conflict"] is conflict
    assert block["index"] == "metrics/index.md" and block["note"]
    if conflict:
        assert "the index is newer evidence" in block["note"]


# -------------------------------------------------------- detection in a repo


def _artifacts(root: Path) -> list[SourceArtifact]:
    found = []
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root).as_posix()
        found.append(
            SourceArtifact(f"file:x:{relative}", relative, parse_okf(relative, path.read_text()))
        )
    return found


def test_a_repository_that_is_a_bundle_is_one_bundle(tmp_path: Path):
    write_bundle(tmp_path)
    for relative, text in {
        "docs/development.md": "# Development\n\nRun the tests.\n",
        "docs/release.md": "# Releasing\n\nTag and push.\n",
        "docs/architecture.md": "# Architecture\n\nThree tiers.\n",
        ".github/pull_request_template.md": "## What\n\n## Why\n",
        ".github/ISSUE_TEMPLATE/bug.md": "---\nname: Bug\nabout: report\n---\n",
    }.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    bundles = detect_bundles(_artifacts(tmp_path))
    assert [(b.root, b.detection) for b in bundles] == [("", "index")], (
        "the root index's listing is the bundle's scope; the developer docs and "
        ".github/ beside it do not split it into fragments"
    )


def test_a_folder_without_a_bundle_still_has_none(tmp_path: Path):
    for relative in ("index.md", "a.md", "b.md", "c.md"):
        (tmp_path / relative).write_text("# Plain\n\n* [a](a.md) - A note.\n")
    assert detect_bundles(_artifacts(tmp_path)) == []


# ---------------------------------------------------------- the nearest index


@pytest.fixture
def synced(tmp_path: Path):
    corpus = write_bundle(tmp_path / "acme")
    (corpus / "metrics" / "index.md").write_text(
        "# Metric\n\n"
        "* [Income statement](income-statement.md) - Headline figures.\n"
        "* [Gross Margin](margin.md) - The canonical definition.\n\n"
        "## Deprecated\n\n"
        "* [Gross Margin (legacy)](margin-legacy.md) - Superseded by margin.md.\n"
    )
    engine = SyncEngine(_config(tmp_path, corpus))
    engine.sync_source("kb", "full")
    try:
        yield engine
    finally:
        engine.close()


def _indexed_by(engine: SyncEngine) -> dict[str, dict[str, Any]]:
    found = {}
    for (source, _target), edge_map in engine.graph_builder.graph.iter_edges():
        for data in edge_map.values():
            if data.get("type") == okf_index.EDGE_TYPE:
                found[source] = dict(data)
    return found


def test_each_document_points_at_its_nearest_index(synced):
    edges = _indexed_by(synced)
    legacy = edges[_art("metrics/margin-legacy.md")]
    assert legacy["index_path"] == "metrics/index.md" and legacy["levels"] == 0
    assert legacy["listed"] is True and legacy["listed_via"] == "file"
    assert legacy["statement"] == "superseded" and legacy["section"] == "Deprecated"
    assert edges[_art("metrics/margin.md")]["statement"] == "current"
    skill = edges[_art("references/skills/run-on-bq.md")]
    assert skill["index_path"] == "index.md" and skill["levels"] == 2
    assert skill["listed"] is False, "the root index does not name references/"
    orders = edges[_art("tables/orders.md")]
    assert orders["index_path"] == "tables/index.md" and orders["listed"] is True
    assert _art("README.md") not in edges, "boilerplate is not the bundle's to speak for"
    assert not any(
        source.endswith(("index.md:branch=none", "log.md:branch=none")) for source in edges
    )


def test_the_block_is_one_hop_and_an_unchanged_resync_moves_nothing(synced):
    graph = synced.graph_builder.graph
    hits = [
        {"node_id": _art("metrics/margin-legacy.md"), "relative_path": "metrics/margin-legacy.md"},
        {
            "chunk_id": "c1",
            "artifact_id": _art("metrics/margin.md"),
            "relative_path": "metrics/margin.md",
        },
        {"node_id": "file:kb:notes.txt:branch=none", "relative_path": "notes.txt"},
    ]
    okf_index.annotate(hits, graph)
    # Listed as superseded, and the document itself says deprecated: they agree.
    assert hits[0]["okf"]["verdict"] == "superseded" and hits[0]["okf"]["conflict"] is False
    assert hits[1]["okf"]["verdict"] == "current"
    assert "okf" not in hits[2], "only Markdown is looked up"
    before = synced.graph_builder.graph.generation_id() if hasattr(graph, "generation_id") else None
    synced.sync_source("kb", "incremental")
    after = synced.graph_builder.graph.generation_id() if hasattr(graph, "generation_id") else None
    assert before == after


def test_the_graph_service_answers_in_one_call():
    asked: list[list[str]] = []

    class RemoteGraph:
        def remote_okf_index(self, artifact_ids):
            asked.append(artifact_ids)
            return {artifact_ids[0]: {"verdict": "stale", "note": "x"}}

    hits = [
        {"node_id": "file:kb:a.md:branch=none", "relative_path": "a.md"},
        {"node_id": "file:kb:b.md:branch=none", "relative_path": "b.md"},
    ]
    okf_index.annotate(hits, RemoteGraph())
    assert len(asked) == 1 and sorted(asked[0]) == sorted(h["node_id"] for h in hits)
    assert hits[0]["okf"]["verdict"] == "stale" and "okf" not in hits[1]


def test_a_broken_graph_never_breaks_retrieval():
    class Broken:
        def out_edges_batch(self, ids):
            raise RuntimeError("graph down")

    hits = [{"node_id": "file:kb:a.md:branch=none", "relative_path": "a.md"}]
    assert okf_index.annotate(hits, Broken()) is hits and "okf" not in hits[0]


def test_the_prompt_carries_what_the_index_says():
    block = okf_index.freshness(
        _edge(statement="superseded", section="Deprecated"), {"status": "stable"}, NOW
    )
    citation = {
        "index": 1,
        "title": "Gross Margin (legacy)",
        "relative_path": "metrics/margin-legacy.md",
        "snippet": "Retired.",
        "okf": block,
    }
    prompt = build_prompt("What is gross margin?", [citation], [])
    assert (
        "[1] Gross Margin (legacy) (metrics/margin-legacy.md)\nOKF index check: superseded."
        in prompt
    )
    plain = build_prompt("What is gross margin?", [{**citation, "okf": None}], [])
    assert "OKF index check" not in plain, "no block, no extra tokens"


# --------------------------------------------------------- arriving as files


def test_a_folder_upload_keeps_its_tree_and_cannot_leave_the_root(tmp_path: Path):
    stored = store_upload(tmp_path, "acme/metrics/index.md", b"* [a](a.md) - A.\n")
    assert stored.filename == "acme/metrics/index.md"
    assert (tmp_path / "acme" / "metrics" / "index.md").is_file()
    again = store_upload(tmp_path, "acme/metrics/index.md", b"* [a](a.md) - A.\n")
    assert again.unchanged and again.filename == "acme/metrics/index.md"
    other = store_upload(tmp_path, "acme/tables/index.md", b"* [o](o.md) - O.\n")
    assert other.filename == "acme/tables/index.md", "two index.md in two folders, not index-2.md"
    escaped = store_upload(tmp_path, "../../outside/x.md", b"x")
    assert Path(escaped.path).resolve().is_relative_to(tmp_path.resolve())
    assert safe_relative_path(".git/config") == "git/config"
    assert safe_relative_path("") == "upload"


def test_a_bundle_uploaded_as_a_folder_is_detected(
    loaded_config, config_path: Path, tmp_path: Path
):
    corpus = write_bundle(tmp_path / "acme")
    files = [
        (
            "files",
            (path.relative_to(tmp_path).as_posix(), io.BytesIO(path.read_bytes()), "text/markdown"),
        )
        for path in sorted(corpus.rglob("*"))
        if path.is_file()
    ]
    app = create_app_for(loaded_config, config_path)
    with TestClient(app) as client:
        response = client.post(
            "/sources/upload",
            files=files,
            data={"source_name": "okf-uploads", "sync_now": "true", "wait": "true"},
        )
        assert response.status_code == 200, response.text
        names = {item["filename"] for item in response.json()["stored"]}
        assert {"acme/index.md", "acme/metrics/index.md", "acme/tables/index.md"} <= names
        graph = app.state.engine.graph_builder.graph
        with graph.reading():
            bundles = [
                attrs.get("okf_root")
                for _node, attrs in graph.nodes(data=True)
                if attrs.get("type") == "okf_bundle"
            ]
        assert bundles == ["acme"]
        hits = client.post("/search", json={"query": "Gross Margin legacy", "mode": "text"}).json()
        legacy = next(
            h for h in hits["results"] if h.get("relative_path") == "acme/metrics/margin-legacy.md"
        )
        assert legacy["okf"]["index"] == "acme/metrics/index.md"
        assert legacy["okf"]["verdict"] == "deprecated", "its own status: deprecated"
    app.state.engine.close()


def create_app_for(config: Any, config_path: Path) -> Any:
    from pheasant.api.app import create_app

    return create_app(config=config, config_path=config_path)
