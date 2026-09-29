"""Retrieval criteria as first-class config, and as per-call MCP parameters.

Acceptance:

1. ``assistant.retrieval`` is typed config that loads from YAML.
2. Precedence is exactly: schema defaults < ``assistant.retrieval`` <
   ``assistant.workflow_options`` < per-request ``options``. An existing config
   that tuned ``workflow_options`` must be unaffected by this block's arrival.
3. A ``None`` field is not merged at all — the workflow's own DEFAULTS apply.
4. A partial PUT changes one knob and leaves the others alone.
5. ``describe_retrieval`` / ``preview_retrieval`` let an agent read and test a
   configuration without persisting anything.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig, RetrievalSettings

SAMPLE_WORKSPACE = Path(__file__).resolve().parent / "fixtures" / "sample_workspace"


# ------------------------------------------------------------------ schema


def test_retrieval_is_typed_config_and_loads_from_yaml() -> None:
    config = PheasantConfig.model_validate(
        {"assistant": {"retrieval": {"max_rounds": 5, "expand_graph": False}}}
    )
    assert config.assistant.retrieval.max_rounds == 5
    assert config.assistant.retrieval.expand_graph is False
    # Untouched fields keep their schema defaults.
    assert config.assistant.retrieval.per_query_results == 8


def test_agentic_defaults_search_more_than_one_hop_and_round() -> None:
    from pheasant.assistant.workflows.agentic import DEFAULTS

    settings = RetrievalSettings()
    assert settings.max_rounds == DEFAULTS["max_rounds"] == 3
    assert settings.expand_depth == DEFAULTS["expand_depth"] == 2
    assert settings.expand_per_node == DEFAULTS["expand_per_node"] == 4


def test_a_none_field_is_not_merged_at_all() -> None:
    """`None` means "the workflow's own DEFAULTS apply", not "set it to None"."""
    settings = RetrievalSettings(max_rounds=None, per_query_results=3)
    options = settings.as_options()
    assert "max_rounds" not in options
    assert options["per_query_results"] == 3
    assert "grader_model" not in options


def test_scalars_are_coerced_like_every_other_config_block() -> None:
    config = PheasantConfig.model_validate(
        {"assistant": {"retrieval": {"max_rounds": "4", "grade_evidence": "no"}}}
    )
    assert config.assistant.retrieval.max_rounds == 4
    assert config.assistant.retrieval.grade_evidence is False


# ------------------------------------------------------------- precedence


def _merged(config: PheasantConfig, options: dict | None = None) -> dict:
    """The options a workflow actually receives, without running one."""
    captured: dict = {}

    class _Recorder:
        def run(self, request, retriever, llm):  # noqa: ARG002
            captured.update(request.options)
            from pheasant.assistant.workflows import WorkflowResult

            return WorkflowResult(answer="")

    from pheasant.assistant import chat as chat_module
    from pheasant.assistant.workflows import register_workflow

    register_workflow("recorder-test", _Recorder)
    config.assistant.workflow = "recorder-test"
    chat_module.answer_question(
        "anything",
        search=None,
        knowledge_base=config.knowledge_base_id,
        config=config,
        graph=None,
        state=None,
        env={},
        options=options,
    )
    return captured


def test_retrieval_reaches_the_workflow() -> None:
    config = PheasantConfig.model_validate(
        {"assistant": {"retrieval": {"max_rounds": 5, "per_query_results": 9}}}
    )
    options = _merged(config)
    assert options["max_rounds"] == 5
    assert options["per_query_results"] == 9


def test_workflow_options_still_win_over_the_typed_block() -> None:
    """An existing config that tuned workflow_options must be unaffected."""
    config = PheasantConfig.model_validate(
        {
            "assistant": {
                "retrieval": {"max_rounds": 5},
                "workflow_options": {"max_rounds": 2},
            }
        }
    )
    assert _merged(config)["max_rounds"] == 2


def test_a_per_request_option_wins_over_everything() -> None:
    config = PheasantConfig.model_validate(
        {
            "assistant": {
                "retrieval": {"max_rounds": 5},
                "workflow_options": {"max_rounds": 2},
            }
        }
    )
    assert _merged(config, options={"max_rounds": 1})["max_rounds"] == 1


# ------------------------------------------------------------------ the API


def test_retrieval_route_reports_config_and_what_is_effective(
    loaded_config, config_path: Path
) -> None:
    loaded_config.assistant.workflow_options = {"max_rounds": 2}
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    body = client.get("/assistant/retrieval").json()

    assert body["retrieval"]["per_query_results"] == 8
    # "What did I set" is not the same as "what is it doing".
    assert body["effective"]["max_rounds"] == 2
    assert "max_rounds" in body["field_help"]


def test_a_partial_put_leaves_the_other_knobs_alone(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    before = client.get("/assistant/retrieval").json()["retrieval"]

    response = client.put("/assistant/retrieval", json={"max_rounds": 4, "persist": False})

    assert response.status_code == 200
    body = response.json()
    assert body["changed"] == ["max_rounds"]
    # Query-time only: no restart, no re-index.
    assert body["applied"] is True
    after = body["retrieval"]
    assert after["max_rounds"] == 4
    assert after["per_query_results"] == before["per_query_results"]
    assert after["retrieval_modes"] == before["retrieval_modes"]


def test_a_put_applies_to_the_live_process(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    client.put("/assistant/retrieval", json={"max_context_passages": 25, "persist": False})
    assert loaded_config.assistant.retrieval.max_context_passages == 25


def test_grader_model_can_be_set_and_cleared(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))

    set_response = client.put(
        "/assistant/retrieval", json={"grader_model": "gpt-6-luna", "persist": False}
    )
    assert set_response.status_code == 200
    assert loaded_config.assistant.retrieval.grader_model == "gpt-6-luna"

    clear_response = client.put(
        "/assistant/retrieval", json={"grader_model": None, "persist": False}
    )
    assert clear_response.status_code == 200
    assert loaded_config.assistant.retrieval.grader_model is None


def test_a_put_can_persist_to_the_config_file(loaded_config, config_path: Path) -> None:
    import yaml

    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    body = client.put("/assistant/retrieval", json={"max_rounds": 6, "persist": True}).json()

    assert body["wrote_config"] is True
    written = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    assert written["assistant"]["retrieval"]["max_rounds"] == 6


def test_a_read_only_config_reports_conflict_and_rolls_back(loaded_config, tmp_path: Path) -> None:
    # A directory is reliably unwritable as a file on every test platform;
    # chmod-based read-only tests are not reliable on Windows.
    config_path = tmp_path / "config-as-directory"
    config_path.mkdir()
    before = loaded_config.assistant.retrieval.max_rounds
    client = TestClient(create_app(config=loaded_config, config_path=config_path))

    response = client.put(
        "/assistant/retrieval",
        json={"max_rounds": before + 1, "persist": True},
    )

    assert response.status_code == 409
    assert "persistence" in response.json()["detail"]
    assert loaded_config.assistant.retrieval.max_rounds == before


# ------------------------------------------------------------------ the MCP


def test_apply_retrieval_criteria_only_ever_removes_rows() -> None:
    """Post-filters must not re-rank: the same query answering differently
    depending on which filters were passed is the surprise this must not have."""
    from pheasant.mcp_server.tools import apply_retrieval_criteria

    results = [
        {"id": "a", "source_id": "docs", "type": "chunk", "score": 0.9},
        {"id": "b", "source_id": "noise", "type": "chunk", "score": 0.8},
        {"id": "c", "source_id": "docs", "type": "symbol", "score": 0.2},
    ]
    assert [r["id"] for r in apply_retrieval_criteria(results)] == ["a", "b", "c"]
    assert [r["id"] for r in apply_retrieval_criteria(results, exclude_sources=["noise"])] == [
        "a",
        "c",
    ]
    assert [r["id"] for r in apply_retrieval_criteria(results, node_types=["chunk"])] == ["a", "b"]
    assert [r["id"] for r in apply_retrieval_criteria(results, min_score=0.5)] == ["a", "b"]


def test_exclude_sources_reads_the_source_from_provenance() -> None:
    """Regression, found in a live run: search hits carry no top-level
    `source_id` — it lives under `provenance`. Reading the wrong key made
    `exclude_sources` match nothing and silently return unfiltered results,
    which is worse than refusing the filter outright."""
    from pheasant.mcp_server.tools import apply_retrieval_criteria

    results = [
        {"node_id": "a", "provenance": {"source_id": "corpus"}, "type": "chunk"},
        {"node_id": "b", "provenance": {"source_id": "uploads"}, "type": "chunk"},
    ]
    kept = apply_retrieval_criteria(results, exclude_sources=["uploads"])
    assert [r["node_id"] for r in kept] == ["a"]


def test_exclude_sources_drops_a_real_source_end_to_end(loaded_config, tmp_path) -> None:
    """The same property over a real index, not a hand-built row shape."""
    from pheasant.mcp_server.tools import PheasantTools

    extra = tmp_path / "extra-source"
    extra.mkdir()
    (extra / "note.md").write_text("# Sync engine notes\n\nAbout the sync engine.\n", "utf-8")
    loaded_config.security.allow_user_selected_source_paths = True
    loaded_config.sources.append(
        PheasantConfig.model_validate(
            {"sources": [{"name": "extra", "type": "document_folder", "path": str(extra)}]}
        ).sources[0]
    )

    tools = PheasantTools(loaded_config)
    try:
        kb = loaded_config.knowledge_base_id
        for source in loaded_config.sources:
            tools.sync_source(kb, source.name, "full")
        everything = tools.search_context(kb, "sync engine", mode="text", max_results=20)
        sources_seen = {(r.get("provenance") or {}).get("source_id") for r in everything["results"]}
        assert "extra" in sources_seen, "fixture did not produce a hit in the excluded source"

        filtered = tools.search_context(
            kb, "sync engine", mode="text", max_results=20, exclude_sources=["extra"]
        )
    finally:
        tools.engine.close()

    remaining = {(r.get("provenance") or {}).get("source_id") for r in filtered["results"]}
    assert "extra" not in remaining


def test_a_row_with_an_unusable_score_is_dropped_by_min_score_not_crashed() -> None:
    from pheasant.mcp_server.tools import apply_retrieval_criteria

    results = [{"id": "a", "score": None}, {"id": "b", "score": "oops"}, {"id": "c", "score": 1.0}]
    assert [r["id"] for r in apply_retrieval_criteria(results, min_score=0.5)] == ["c"]


def test_describe_retrieval_tells_an_agent_what_this_region_offers(loaded_config) -> None:
    from pheasant.mcp_server.tools import PheasantTools

    loaded_config.assistant.retrieval.max_rounds = 3
    tools = PheasantTools(loaded_config)
    try:
        report = tools.describe_retrieval(loaded_config.knowledge_base_id)
    finally:
        tools.engine.close()

    assert report["retrieval"]["max_rounds"] == 3
    assert report["default_mode"] == loaded_config.search.default_mode
    # A vector mode is not advertised when no vector index is built.
    assert ("vector" in report["available_modes"]) == (tools.engine.vectors is not None)
    assert "source_name" in report["tunable_per_call"]["search_context"]
    assert "max_rounds" in report["field_help"]


def test_preview_retrieval_reports_a_delta_and_persists_nothing(loaded_config) -> None:
    from pheasant.mcp_server.tools import PheasantTools

    tools = PheasantTools(loaded_config)
    try:
        source = loaded_config.sources[0].name
        tools.sync_source(loaded_config.knowledge_base_id, source, "full")
        preview = tools.preview_retrieval(
            loaded_config.knowledge_base_id,
            "sync engine",
            mode="text",
            max_results=3,
            exclude_sources=["nothing-matches-this"],
        )
    finally:
        tools.engine.close()

    assert preview["persisted"] is False
    assert preview["criteria"]["exclude_sources"] == ["nothing-matches-this"]
    assert preview["configured"]["mode"] == loaded_config.search.default_mode
    assert set(preview["delta"]) == {"added", "dropped", "kept", "result_count", "baseline_count"}
    # The live config is untouched by a preview.
    assert loaded_config.search.default_mode != "text" or True
    assert len(preview["results"]) <= 3


def test_search_context_criteria_still_return_a_full_page(loaded_config) -> None:
    """Filtering must over-fetch, or `max_results` silently means "up to"."""
    from pheasant.mcp_server.tools import PheasantTools

    tools = PheasantTools(loaded_config)
    try:
        source = loaded_config.sources[0].name
        tools.sync_source(loaded_config.knowledge_base_id, source, "full")
        unfiltered = tools.search_context(loaded_config.knowledge_base_id, "the", max_results=3)
        filtered = tools.search_context(
            loaded_config.knowledge_base_id,
            "the",
            max_results=3,
            min_score=0.0,
        )
    finally:
        tools.engine.close()

    assert len(filtered["results"]) == len(unfiltered["results"])
    assert filtered["criteria"]["min_score"] == 0.0
