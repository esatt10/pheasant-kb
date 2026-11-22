"""Ranking behaviour, pinned against the failures that motivated it.

Every case here is a reduction of something measured on a real 2,132-file
corpus, where asking "locate readme" returned Python source and the
repository's own README.md sat at rank 125:

* a rare framing verb ("locate" — 15 chunks) outscored the subject noun
  ("readme" — 724 chunks), because BM25 weights by rarity and the OR-expansion
  lets every token vote;
* ``chunks_fts.title`` held the same full path already in ``path``, so a
  filename match was worth one body word and BM25 ranked by body brevity;
* 412 files were named README.md, so no lexical signal could separate them —
  the discriminator is structural (root vs. nested);
* "where is X implemented" returned X's tests;
* the three search arms scored on incomparable scales (text 0.86-0.92,
  vector 0.667-0.674, graph a flat 0.60), so hybrid silently degraded to
  text-only while paying for all three.
"""

from __future__ import annotations

import pytest

from pheasant.persistence.state_store import StateStore, _basename
from pheasant.search.hybrid import RRF_K, _merge_rrf
from pheasant.search.text_store import (
    SearchStore,
    _postgres_rank_expression,
    _query_tokens,
)
from tests.conftest import seed_source


def _index(tmp_path, files: dict[str, str]) -> SearchStore:
    """Build a real index from ``{relative_path: body}``."""
    store = StateStore()
    store.migrate()
    seed_source(store)
    for i, (path, body) in enumerate(files.items()):
        artifact_id = f"file:demo:{path}"
        store.conn.execute(
            "INSERT INTO artifacts (id, source_id, type, path, relative_path, size_bytes, status)"
            " VALUES (?,?,?,?,?,?,?)",
            (artifact_id, "demo", "file", f"/w/{path}", path, len(body), "indexed"),
        )
        store.conn.execute(
            "INSERT INTO chunks (id, artifact_id, source_id, chunk_index, start_line, end_line,"
            " text, text_hash) VALUES (?,?,?,?,?,?,?,?)",
            (f"{artifact_id}#0", artifact_id, "demo", 0, 1, 5, body, f"h{i}"),
        )
        store.conn.execute(
            "INSERT INTO chunks_fts (chunk_id, source_id, artifact_id, title, path,"
            " heading_path, text) VALUES (?,?,?,?,?,?,?)",
            (f"{artifact_id}#0", "demo", artifact_id, _basename(path), path, "", body),
        )
    store.conn.commit()
    return SearchStore(store)


def _paths(hits: list[dict]) -> list[str]:
    out, seen = [], set()
    for hit in hits:
        path = hit.get("relative_path")
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


# ----------------------------------------------------------------- tokenizing


@pytest.mark.parametrize(
    ("query", "expected", "absent"),
    [
        ("locate readme", "readme", "locate"),
        ("where is the sync engine", "sync", "where"),
        ("how do I find the changelog", "changelog", "find"),
        ("show me the tests", "tests", "show"),
    ],
)
def test_framing_words_are_dropped_from_ranking(query, expected, absent) -> None:
    tokens = _query_tokens(query)
    assert expected in tokens
    assert absent not in tokens, f"{absent!r} carries no signal but skews BM25 by rarity"


def test_an_all_stopword_query_still_searches_for_something() -> None:
    """Filtering must yield rather than produce a query that matches nothing."""
    tokens = _query_tokens("what is it about")
    assert tokens, "an all-stopword question must not tokenize to nothing"


def test_identifiers_survive_intact() -> None:
    tokens = _query_tokens("SyncEngine._run_sync")
    assert "syncengine" in tokens and "sync" in tokens and "run" in tokens


def test_postgres_uses_the_rarest_terms_to_bound_the_candidate_set(monkeypatch) -> None:
    """Planner prose must not make Postgres rank most of the corpus per term."""
    frequencies = {
        "architecture": 139,
        "components": 287,
        "openwiki": 834,
        "main": 1624,
        "langgraph": 2517,
        "deepagents": 6206,
        "langchain": 6769,
    }
    monkeypatch.setattr(
        "pheasant.search.text_store._postgres_document_frequencies",
        lambda _state, _tokens, _source=None: (19_865, frequencies),
    )

    expression, params, match_tokens = _postgres_rank_expression(object(), list(frequencies))

    assert match_tokens == ["architecture", "components", "openwiki", "main"]
    assert params[:4] == match_tokens
    assert expression.count("ts_rank_cd") == len(frequencies)


def test_postgres_rank_statistics_are_routed_to_the_requested_source(monkeypatch) -> None:
    seen: list[str | None] = []

    def frequencies(_state, _tokens, source_name=None):
        seen.append(source_name)
        return 10, {"needle": 1}

    monkeypatch.setattr("pheasant.search.text_store._postgres_document_frequencies", frequencies)

    _postgres_rank_expression(object(), ["needle"], "docs")

    assert seen == ["docs"]


def test_postgres_source_filter_is_qualified_across_joined_tables() -> None:
    class Dialect:
        is_postgres = True

    class State:
        dialect = Dialect()

        def __init__(self) -> None:
            self.queries: list[str] = []

        def rows(self, sql, params=()):
            self.queries.append(sql)
            if "FROM unnest" in sql:
                return [{"term": term, "df": 1} for term in params[0]]
            if "count(*) AS n" in sql:
                return [{"n": 1}]
            return []

    state = State()

    SearchStore(state).search("needle", source_name="docs")

    main_query = next(sql for sql in state.queries if "ORDER BY rank_score" in sql)
    assert "AND chunks_fts.source_id = ?" in main_query


# -------------------------------------------------------------- name matching


def test_the_file_named_readme_beats_the_file_that_says_readme(tmp_path) -> None:
    """The original bug: a filename match was worth exactly one body word."""
    search = _index(
        tmp_path,
        {
            "README.md": "This project does a thing. Installation and usage follow.",
            "notes/chatter.md": "readme readme readme readme readme readme readme",
        },
    )
    assert _paths(search.search("readme", max_results=10))[0] == "README.md"


def test_the_root_readme_wins_when_many_files_share_the_name(tmp_path) -> None:
    """412 identical basenames: the discriminator is structural, not lexical."""
    files = {"README.md": "top level readme for the whole project"}
    for pkg in ("alpha", "beta", "gamma", "delta"):
        files[f"python/packages/{pkg}/README.md"] = f"readme for the {pkg} package"
        files[f"python/packages/{pkg}/deep/nested/README.md"] = "readme buried deeper"
    search = _index(tmp_path, files)

    assert _paths(search.search("readme", max_results=10))[0] == "README.md"


def test_naming_the_location_reaches_a_nested_file(tmp_path) -> None:
    """The counterweight to the depth prior, and why the planner is told to
    put a directory in the query: a nested file is reachable when its
    location is said out loud."""
    files = {"README.md": "top level readme"}
    for pkg in ("devui", "core", "azureai"):
        files[f"python/packages/{pkg}/README.md"] = f"readme for {pkg}"
    search = _index(tmp_path, files)

    top = _paths(search.search("devui readme", max_results=10))[0]
    assert top == "python/packages/devui/README.md"


def test_implementation_outranks_its_tests(tmp_path) -> None:
    search = _index(
        tmp_path,
        {
            "src/checkpoint.py": "def save_checkpoint(): store the workflow checkpoint",
            "tests/test_checkpoint.py": "checkpoint checkpoint checkpoint test the checkpoint",
        },
    )
    assert _paths(search.search("checkpoint", max_results=10))[0] == "src/checkpoint.py"


def test_a_strong_deep_match_still_beats_a_weak_shallow_one(tmp_path) -> None:
    """The depth prior scales a match; it must not displace one.

    This is the imbalance guard: an additive penalty was tried first and
    demoted legitimately-deep code, so the prior is a divisor instead.
    """
    search = _index(
        tmp_path,
        {
            "overview.md": "this project mentions telemetry once",
            "a/b/c/d/telemetry.py": "telemetry telemetry telemetry exporter spans metrics",
        },
    )
    assert _paths(search.search("telemetry exporter spans", max_results=10))[0] == (
        "a/b/c/d/telemetry.py"
    )


# ------------------------------------------------------------------ migration


def test_basename_is_posix_only() -> None:
    assert _basename("docs/deep/guide.md") == "guide.md"
    assert _basename("README.md") == "README.md"
    # A backslash is a legal filename character on POSIX; splitting on it would
    # make the same corpus index differently on Windows.
    assert _basename("weird\\name.md") == "weird\\name.md"
    assert _basename(None) == ""


# ----------------------------------------------------------------------- RRF


def _hit(node_id: str, score: float) -> dict:
    return {"node_id": node_id, "chunk_id": node_id, "score": score, "title": node_id}


def test_rrf_ignores_incomparable_score_scales() -> None:
    """Text 0.9, vector 0.67, graph 0.60 — merging on raw score meant text
    won every slot and the other two arms were decorative."""
    text = [_hit("t1", 0.92), _hit("t2", 0.91)]
    vector = [_hit("v1", 0.67), _hit("t2", 0.66)]
    graph = [_hit("g1", 0.60)]

    merged = _merge_rrf(text, vector, graph, 10)
    ids = [item["node_id"] for item in merged]

    # t2 is the only item two arms agree on, so it leads despite a lower score
    # than t1 in its own arm.
    assert ids[0] == "t2"
    # ...and the low-scoring arms are represented at all, which they were not.
    assert "v1" in ids and "g1" in ids


def test_rrf_scores_reflect_agreement_not_magnitude() -> None:
    both = _merge_rrf([_hit("x", 0.1)], [_hit("x", 0.1)], [], 10)
    one = _merge_rrf([_hit("y", 0.99)], [], [], 10)

    # Scores are rounded to 6dp on the way out, so compare at that resolution.
    assert both[0]["score"] == pytest.approx(2.0 / (RRF_K + 1), abs=1e-6)
    assert one[0]["score"] == pytest.approx(1.0 / (RRF_K + 1), abs=1e-6)
    assert both[0]["score"] > one[0]["score"]
    assert both[0]["retrieved_by"] == "text+vector"


def test_rrf_is_deterministic_and_dedupes_nodes() -> None:
    text = [_hit("a", 0.9), _hit("b", 0.8)]
    vector = [_hit("b", 0.7), _hit("a", 0.6)]

    first = [i["node_id"] for i in _merge_rrf(text, vector, [], 10)]
    second = [i["node_id"] for i in _merge_rrf(text, vector, [], 10)]

    assert first == second
    assert len(first) == len(set(first))


def test_rrf_prefers_the_record_carrying_a_preview() -> None:
    """A graph hit and a chunk hit for one node: keep the one with the text."""
    graph = [{"node_id": "n", "kind": "node", "title": "n", "score": 0.6}]
    text = [{"node_id": "n", "chunk_id": "n", "kind": "chunk", "title": "n", "score": 0.9}]

    merged = _merge_rrf(text, [], graph, 5)
    assert len(merged) == 1
    assert merged[0].get("kind") == "chunk"


# ----------------------------------------------------------- graph facts


def _graph_with_facts():
    from pheasant.graph.simple import SimpleMultiDiGraph

    graph = SimpleMultiDiGraph()
    graph.add_node("f:a", type="markdown_note", label="CONTRIBUTING.md")
    graph.add_node("f:b", type="markdown_note", label="dotnet/AGENTS.md")
    graph.add_node("ext:x", type="external_reference", label="./dotnet/AGENTS.md")
    graph.add_node("c:noise", type="concept", label="limit")
    # The same link produces both an unresolved name and, after internal
    # resolution, the document it points at.
    graph.add_edge("f:a", "ext:x", type="references")
    graph.add_edge("f:a", "f:b", type="references", enrichment_pass="internal_resolution")
    graph.add_edge("f:a", "c:noise", type="mentions")
    return graph


def test_a_resolved_document_outranks_the_name_that_pointed_at_it() -> None:
    from pheasant.assistant.chat import collect_facts

    facts = collect_facts(_graph_with_facts(), ["f:a"], 12)

    assert facts, "file->file edges must reach the panel at all"
    assert facts[0]["object_id"] == "f:b"
    assert facts[0]["object_type"] == "markdown_note"
    # Concept mentions are the noise floor and must not lead.
    assert facts[0]["edge_type"] != "mentions"


def test_facts_do_not_come_all_from_one_subject() -> None:
    """A well-linked README has more one-hop edges than the whole budget."""
    from pheasant.assistant.chat import MAX_FACTS_PER_SUBJECT, collect_facts
    from pheasant.graph.simple import SimpleMultiDiGraph

    graph = SimpleMultiDiGraph()
    for subject in ("s1", "s2", "s3"):
        graph.add_node(subject, type="markdown_note", label=subject)
        for i in range(8):
            target = f"{subject}-t{i}"
            graph.add_node(target, type="markdown_note", label=target)
            graph.add_edge(subject, target, type="references")

    facts = collect_facts(graph, ["s1", "s2", "s3"], 12)

    per_subject: dict[str, int] = {}
    for fact in facts:
        per_subject[fact["subject_id"]] = per_subject.get(fact["subject_id"], 0) + 1
    assert len(per_subject) == 3, per_subject
    assert max(per_subject.values()) <= MAX_FACTS_PER_SUBJECT
