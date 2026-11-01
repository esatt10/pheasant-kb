"""Several sources at once: the sources rail's multi-select, on every surface.

One name keeps the single-source scope every arm filters inside the index;
two or more are a post-filter with the shared over-fetch, re-applied to graph
neighbours and cached hits, so an answer cannot draw on a source nobody chose.
"""

from __future__ import annotations

import inspect

import pytest

from pheasant.search.criteria import apply_retrieval_criteria, criteria_dict, scoped_sources


def _hit(source: str, n: int) -> dict:
    node_id = f"file:{source}:doc{n}.md"
    return {
        "node_id": node_id,
        "chunk_id": f"{node_id}#c0",
        "type": "chunk",
        "title": f"doc{n}.md",
        "relative_path": f"doc{n}.md",
        "provenance": {"source_id": source},
        "score": 1.0 / (n + 1),
        "chunks": [{"text_preview": f"from {source}"}],
    }


def test_one_name_is_the_single_source_scope_and_two_are_a_list() -> None:
    assert scoped_sources(None, None) == (None, None)
    assert scoped_sources("notes", None) == ("notes", None)
    assert scoped_sources(None, ["notes"]) == ("notes", None)
    assert scoped_sources(None, ["notes", "code", "notes"]) == (None, ["notes", "code"])
    # A single name given beside a list joins it rather than winning or losing.
    assert scoped_sources("wiki", ["notes"]) == (None, ["wiki", "notes"])


def test_the_post_filter_keeps_only_the_selected_sources() -> None:
    hits = [_hit(source, n) for n, source in enumerate(["notes", "code", "wiki", "notes"])]
    kept = apply_retrieval_criteria(hits, source_names=["notes", "wiki"])
    assert [h["provenance"]["source_id"] for h in kept] == ["notes", "wiki", "notes"]
    assert "source_names" not in criteria_dict("notes", None, None, None), "echo unchanged"


class _Search:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search_context(self, kb, query, mode, limit, source, **kwargs):
        self.calls.append({"limit": limit, "source": source})
        hits = [_hit(s, n) for n, s in enumerate(["notes", "code", "wiki", "code", "notes"])]
        return {"results": hits, "counts": {}}


def test_the_retriever_filters_every_search_and_every_evidence_read() -> None:
    from pheasant.assistant.retrieval import PheasantRetriever

    search = _Search()
    retriever = PheasantRetriever(
        search=search, knowledge_base="kb", source_names=["notes", "wiki"]
    )
    found = retriever.search("rotation", limit=3)
    assert {p.source_id for p in found} == {"notes", "wiki"}
    assert search.calls[0]["source"] is None, "no single-source pushdown for a list"
    # A graph neighbour or a cached hit from an unselected source is refused.
    assert not retriever._artifact_allowed("file:code:doc1.md", source_id="code")
    assert retriever._artifact_allowed("file:wiki:doc2.md", source_id="wiki")


def test_an_unknown_intent_is_refused_before_any_work() -> None:
    from pheasant.services import assistant as assistant_service
    from pheasant.services.errors import InvalidRequest

    class Context:
        config = type("C", (), {"assistant": None})()

        def knowledge_base(self, name):
            return "kb"

    request = assistant_service.AnswerRequest(question="q", intent="poem")
    with pytest.raises(InvalidRequest, match="intent must be one of"):
        assistant_service.admit(Context(), request)
    assistant_service.admit(
        Context(), assistant_service.AnswerRequest(question="q", intent="compose")
    )


def test_both_surfaces_take_intent_and_source_names() -> None:
    from pheasant.api.assistant_routes import ChatRequest
    from pheasant.api.search_models import SearchRequest as HttpSearch
    from pheasant.mcp_server.tools import PheasantTools

    ask = inspect.signature(PheasantTools.ask_knowledge_base).parameters
    assert {"intent", "source_names"} <= set(ask)
    assert "source_names" in inspect.signature(PheasantTools.search_context).parameters
    assert {"intent", "source_names"} <= set(ChatRequest.model_fields)
    assert "source_names" in HttpSearch.model_fields

    request = ChatRequest(question="q", intent="compose", source_names=["a", "b"])
    service = request.service_request()
    assert (service.intent, service.source_names) == ("compose", ["a", "b"])
