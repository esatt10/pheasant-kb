"""A diagram does not depend on which model is drawing it.

Switching ``assistant.model`` from one vendor's model to another turned
"draw a diagram of X" into "No visual" for most requests, with nothing in the
payload saying why. The causes were several and all looked alike from outside.
Each has a test here, and most drive the real provider wire through the one
``_http_json`` seam, because the first cause lives there:

* **a reasoning model spent the output cap on thinking.** The drawing call
  asked for 1,600 tokens; GPT-6 (and Gemini 2.5, and any model that thinks
  first) spends hidden tokens out of that same cap, returned an empty 200 with
  ``finish_reason: length``, and the visual reported "the model did not
  return a diagram". The budget now fits a thinking model, and a reply cut
  off before any text is asked again with twice the room;
* **the JSON was wrapped, fenced, preceded by reasoning, or spelled in
  another dialect** (``source``/``target``, ``citations``, nested
  ``children``, ids with spaces, a table of rows holding their cells). Every
  one of those states the same diagram, and every one now draws it;
* **what could not be read got no second chance** — one repair turn now says
  what was wrong. A diagram declined as *ungrounded* gets no repair: asking a
  model for citations until the check passes is asking it to pass the check;
* **and when the model cannot draw at all, the reader got nothing.** The
  index's own edges between the cited sources — the model-free path — are
  drawn instead, and the payload says so.

The strictness is asserted alongside: normalization never invents a citation,
``"n1"`` is not passage 1, and a mostly-inferred diagram in any dialect is
still declined.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from pheasant.assistant import answering, providers, visual_dialect, visual_prompt, visuals
from pheasant.assistant.llm import LLM
from pheasant.assistant.providers import OutputBudgetExhausted, ProviderError
from pheasant.assistant.visual_specs import KINDS
from pheasant.telemetry import metrics

CITATIONS = [
    {"index": i, "node_id": f"file:n{i}", "title": f"n{i}", "snippet": f"passage {i}"}
    for i in (1, 2, 3)
]

FACTS = [
    {
        "subject": "release.md",
        "subject_id": "file:n1",
        "predicate": "references",
        "object": "checklist.md",
        "object_id": "file:n2",
    }
]

CANONICAL = {
    "kind": "flow",
    "title": "Release",
    "nodes": [
        {"id": "build", "label": "Build", "cites": [1]},
        {"id": "test", "label": "Test", "cites": [2]},
        {"id": "ship", "label": "Ship", "cites": [3]},
    ],
    "edges": [
        {"from": "build", "to": "test", "cites": [1]},
        {"from": "test", "to": "ship", "cites": [2]},
    ],
}


class _Scripted(LLM):
    """Replies with each of ``replies`` in turn; an exception is raised instead."""

    def __init__(self, *replies: Any, provider: str = "openai") -> None:
        super().__init__(provider=provider, api_key="k")
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def complete(self, system, prompt, **kwargs):  # type: ignore[override]
        self.calls.append({"system": system, "prompt": prompt, **kwargs})
        reply = self.replies.pop(0) if self.replies else "still not a diagram"
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _shape(result: dict) -> tuple[list[tuple[str, tuple[int, ...]]], list[tuple[str, str]]]:
    """A drawn diagram reduced to what it claims: labels with citations, and links."""

    diagram = result["diagram"]
    labels = {node["id"]: node["label"] for node in diagram["nodes"]}
    return (
        [(node["label"], tuple(node["cites"])) for node in diagram["nodes"]],
        [(labels[edge["from"]], labels[edge["to"]]) for edge in diagram["edges"]],
    )


def _outcome(outcome: str, provider: str = "openai") -> float:
    metrics.register_default_metrics("test")
    value = metrics.REGISTRY.value(
        "pheasant_assistant_visual_model_total", provider=provider, outcome=outcome
    )
    return value or 0.0


# ---------------------------------------------------------------------------
# the budget: a reasoning model thinks out of the same cap it answers from
# ---------------------------------------------------------------------------


def _reasoning_openai(thinking: int, seen: list[dict]) -> Any:
    """An OpenAI endpoint whose model spends ``thinking`` tokens before writing."""

    def fake_http(url, payload, headers, timeout):
        seen.append(payload)
        cap = payload.get("max_completion_tokens") or payload.get("max_tokens")
        if cap <= thinking:
            return {
                "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": cap},
            }
        return {
            "choices": [{"message": {"content": json.dumps(CANONICAL)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": thinking + 300},
        }

    return fake_http


def test_a_reasoning_model_gets_room_to_think_and_still_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(providers, "_http_json", _reasoning_openai(3000, seen))
    llm = LLM(provider="openai", api_key="k", model="gpt-6-luna")

    result = visuals.build_diagram("the release", CITATIONS, llm, prompt="[1] a [2] b [3] c")

    assert result["status"] == "ok", result
    assert len(seen) == 1, "the first call's budget is enough for a model that thinks first"
    assert seen[0]["max_completion_tokens"] >= visuals.DIAGRAM_OUTPUT_TOKENS
    assert seen[0]["response_format"] == {"type": "json_object"}


def test_the_old_cap_is_the_failure_this_fixes(monkeypatch: pytest.MonkeyPatch) -> None:
    """At 1,600 tokens the same model returned nothing at all — the reported bug."""

    seen: list[dict] = []
    monkeypatch.setattr(providers, "_http_json", _reasoning_openai(3000, seen))
    with pytest.raises(OutputBudgetExhausted, match="before writing any text"):
        providers.complete(
            "openai",
            api_key="k",
            system="s",
            prompt="p",
            model="gpt-6-luna",
            max_output_tokens=1600,
        )


def test_a_reply_cut_off_before_any_text_is_asked_again_with_more_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(
        providers, "_http_json", _reasoning_openai(visuals.DIAGRAM_OUTPUT_TOKENS + 100, seen)
    )
    llm = LLM(provider="openai", api_key="k", model="gpt-6-luna")

    result = visuals.build_diagram("the release", CITATIONS, llm, prompt="passages")

    assert result["status"] == "ok"
    caps = [payload["max_completion_tokens"] for payload in seen]
    assert caps == [
        visuals.DIAGRAM_OUTPUT_TOKENS,
        visuals.DIAGRAM_OUTPUT_TOKENS * visuals.BUDGET_RETRY_FACTOR,
    ]


def test_a_configured_budget_above_the_floor_is_respected() -> None:
    llm = _Scripted(json.dumps(CANONICAL))
    llm.max_output_tokens = 20000
    visuals.build_diagram("the release", CITATIONS, llm, prompt="passages")
    assert llm.calls[0]["max_output_tokens"] == 20000


def test_an_endpoint_that_rejects_json_mode_is_asked_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []

    def fake_http(url, payload, headers, timeout):
        seen.append(dict(payload))
        if "response_format" in payload:
            raise ProviderError("400 from provider: unknown field response_format")
        return {"choices": [{"message": {"content": json.dumps(CANONICAL)}}]}

    monkeypatch.setattr(providers, "_http_json", fake_http)
    llm = LLM(provider="openai", api_key="k", model="local-model", base_url="http://llm:8000/v1")

    result = visuals.build_diagram("the release", CITATIONS, llm, prompt="passages")

    assert result["status"] == "ok"
    assert ["response_format" in payload for payload in seen] == [True, False]


def test_both_renames_are_absorbed_in_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []

    def fake_http(url, payload, headers, timeout):
        seen.append(dict(payload))
        if "max_tokens" in payload:
            raise ProviderError("400: use max_completion_tokens instead of max_tokens")
        if "response_format" in payload:
            raise ProviderError("400: response_format is not supported")
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(providers, "_http_json", fake_http)
    text = providers.complete(
        "openai", api_key="k", system="s", prompt="p", model="o-series", json_mode=True
    )
    assert text == "ok" and len(seen) == 3


def test_json_mode_is_not_sent_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []

    def fake_http(url, payload, headers, timeout):
        seen.append(payload)
        return {"choices": [{"message": {"content": "an answer"}}]}

    monkeypatch.setattr(providers, "_http_json", fake_http)
    LLM(provider="openai", api_key="k").complete("s", "p")
    assert "response_format" not in seen[0], "answers are prose; only drawing asks for JSON"


def test_gemini_is_asked_for_json_and_its_thoughts_are_not_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []

    def fake_http(url, payload, headers, timeout):
        seen.append(payload)
        parts = [{"text": "Let me think about the steps…", "thought": True}]
        return {"candidates": [{"content": {"parts": [*parts, {"text": json.dumps(CANONICAL)}]}}]}

    monkeypatch.setattr(providers, "_http_json", fake_http)
    llm = LLM(provider="gemini", api_key="k")

    result = visuals.build_diagram("the release", CITATIONS, llm, prompt="passages")

    assert result["status"] == "ok"
    assert seen[0]["generationConfig"]["responseMimeType"] == "application/json"
    assert seen[0]["generationConfig"]["maxOutputTokens"] >= visuals.DIAGRAM_OUTPUT_TOKENS


@pytest.mark.parametrize(
    ("provider", "response"),
    [
        ("gemini", {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}),
        ("anthropic", {"content": [], "stop_reason": "max_tokens"}),
    ],
)
def test_every_provider_says_when_the_budget_ran_out(
    monkeypatch: pytest.MonkeyPatch, provider: str, response: dict
) -> None:
    monkeypatch.setattr(providers, "_http_json", lambda *a, **k: response)
    with pytest.raises(OutputBudgetExhausted):
        providers.complete(provider, api_key="k", system="s", prompt="p")


# ---------------------------------------------------------------------------
# dialects: the same diagram, spelled the ways models spell it
# ---------------------------------------------------------------------------

_SAME = json.dumps(CANONICAL)

DIALECTS: dict[str, str] = {
    "canonical": _SAME,
    "prose then a fence": f"Here is the diagram:\n\n```json\n{_SAME}\n```\nHope it helps!",
    "a think block first": f"<think>Build, then test… {{not json}}</think>\n{_SAME}",
    "wrapped": json.dumps({"diagram": CANONICAL}),
    "trailing commas": _SAME.replace("]}", "],}").replace("}]", "},]"),
    "a python literal": repr(CANONICAL),
    "a quoted example before the answer": 'Format: {"a": 1}. Answer: ' + _SAME,
    "source/target, citations, name": json.dumps(
        {
            "type": "flow",
            "title": "Release",
            "nodes": [
                {"id": "build", "name": "Build", "citations": ["[1]"]},
                {"id": "test", "name": "Test", "citations": "passage 2"},
                {"id": "ship", "name": "Ship", "sources": [{"index": 3}]},
            ],
            "links": [
                {"source": "build", "target": "test", "refs": [1]},
                {"source": "test", "target": "ship", "citations": "[2]"},
            ],
        }
    ),
    "ids with spaces, edges by label": json.dumps(
        {
            "kind": "flow",
            "nodes": [
                {"id": "build step", "label": "Build", "cites": [1]},
                {"id": "test step", "label": "Test", "cites": [2]},
                {"label": "Ship", "cites": [3]},
            ],
            "edges": [
                {"from": "build step", "to": "Test", "cites": [1]},
                {"from": "test", "to": "ship", "cites": [2]},
            ],
        }
    ),
    "steps as strings with arrow edges": json.dumps(
        {
            "kind": "flow",
            "steps": [
                {"label": "Build", "cites": "1"},
                {"label": "Test", "cites": "[2]"},
                {"label": "Ship", "cites": "#3"},
            ],
            "edges": [["Build", "Test", ""], "Test -> Ship"],
        }
    ),
}


@pytest.mark.parametrize("dialect", sorted(DIALECTS))
def test_every_dialect_draws_the_same_diagram(dialect: str) -> None:
    llm = _Scripted(DIALECTS[dialect])
    result = visuals.build_diagram("the release", CITATIONS, llm, prompt="passages")

    assert result["status"] == "ok", result
    nodes, edges = _shape(result)
    assert nodes == [("Build", (1,)), ("Test", (2,)), ("Ship", (3,))]
    # "Test -> Ship" and the canonical edges carry citations; the arrow-string
    # edge has none, so it is drawn as inferred rather than dropped.
    assert edges == [("Build", "Test"), ("Test", "Ship")]
    assert len(llm.calls) == 1, "a readable dialect needs no repair turn"


def test_a_nested_tree_is_a_hierarchy_with_edges_the_children_cite() -> None:
    reply = {
        "kind": "hierarchy",
        "nodes": [
            {
                "label": "Platform",
                "cites": [1],
                "children": [
                    {"label": "Search", "cites": [2]},
                    {"label": "Sync", "cites": [3], "children": [{"label": "Queue", "cites": [3]}]},
                ],
            }
        ],
    }
    result = visuals.build_diagram(
        "the platform", CITATIONS, _Scripted(json.dumps(reply)), prompt="p"
    )

    assert result["status"] == "ok"
    nodes, edges = _shape(result)
    assert [label for label, _ in nodes] == ["Platform", "Search", "Sync", "Queue"]
    assert edges == [("Platform", "Search"), ("Platform", "Sync"), ("Sync", "Queue")]
    cites = {(e["from"], e["to"]): e["cites"] for e in result["diagram"]["edges"]}
    assert list(cites.values()) == [[2], [3], [3]], "a nesting edge cites what cites the child"


def test_lanes_holding_their_nodes_are_a_swimlane() -> None:
    reply = {
        "kind": "swimlane",
        "lanes": [
            {
                "label": "Developers",
                "cites": [1],
                "nodes": [{"id": "m", "label": "Merge", "cites": [1]}],
            },
            {"label": "Ops", "cites": [2], "nodes": [{"id": "d", "label": "Deploy", "cites": [2]}]},
        ],
        "edges": [{"from": "m", "to": "d", "cites": [2]}],
    }
    result = visuals.build_diagram(
        "who does what", CITATIONS, _Scripted(json.dumps(reply)), prompt="p"
    )

    assert result["status"] == "ok"
    diagram = result["diagram"]
    assert [g["label"] for g in diagram["groups"]] == ["Developers", "Ops"]
    assert [n["group"] for n in diagram["nodes"]] == ["Developers", "Ops"]


def test_a_table_of_rows_holding_their_cells_is_a_table() -> None:
    reply = {
        "kind": "table",
        "columns": ["Scales to", "Needs"],
        "rows": [
            {
                "label": "SQLite",
                "cites": [1],
                "cells": {"Scales to": "one host", "Needs": "nothing"},
            },
            {
                "label": "Postgres",
                "cites": [2],
                "values": {"Scales to": {"text": "a fleet", "cites": [3]}},
            },
        ],
    }
    result = visuals.build_diagram("compare", CITATIONS, _Scripted(json.dumps(reply)), prompt="p")

    assert result["status"] == "ok", result
    cells = {(c["row"], c["column"]): (c["text"], c["cites"]) for c in result["diagram"]["cells"]}
    rows = {n["label"]: n["id"] for n in result["diagram"]["nodes"]}
    assert cells[(rows["SQLite"], "Scales to")] == ("one host", [1]), (
        "a cell inherits its row's cite"
    )
    assert cells[(rows["Postgres"], "Scales to")] == ("a fleet", [3]), "a cell's own cite wins"


def test_a_timeline_of_dated_events_under_its_own_names() -> None:
    reply = {
        "type": "timeline",
        "events": [
            {"title": "First release", "date": "2024", "citations": [1]},
            {"title": "Rollbacks", "date": "2025", "citations": [2]},
        ],
    }
    result = visuals.build_diagram("history", CITATIONS, _Scripted(json.dumps(reply)), prompt="p")

    assert result["status"] == "ok" and result["diagram"]["kind"] == "timeline"
    assert [n["when"] for n in result["diagram"]["nodes"]] == ["2024", "2025"]


# ---------------------------------------------------------------------------
# what normalization must never do
# ---------------------------------------------------------------------------


def test_normalizing_never_invents_a_citation() -> None:
    reply = {
        "nodes": [
            {"id": "a", "label": "Build", "cites": ["n1"]},
            {"id": "b", "label": "Test", "note": "see passage 2"},
            {"id": "c", "label": "Ship", "cites": [3]},
        ],
        "edges": [{"source": "a", "target": "b"}, {"from": "b", "to": "c", "cites": "v1.2"}],
    }
    result = visuals.validate_spec(visual_dialect.normalize(reply), CITATIONS)
    nodes = {n["label"]: n for n in result.get("diagram", {}).get("nodes", [])}

    assert result["status"] == "declined", "four of five elements have no citation"
    assert result["grounding"]["cited"] == 1
    assert nodes == {}, "a declined diagram draws nothing"
    assert visual_specs_cites(["n1", "v1.2", "p2x"]) == [], "not one of these is a passage number"


def visual_specs_cites(value: Any) -> list[int]:
    from pheasant.assistant.visual_specs import cites

    return cites(value, {1, 2, 3})


@pytest.mark.parametrize(
    ("given", "read"),
    [
        (2, [2]),
        ("[2]", [2]),
        ("[1][3]", [1, 3]),
        ("1, 3", [1, 3]),
        ("passage 2", [2]),
        ("#3", [3]),
        ([1.0, "2"], [1, 2]),
        ([True, 9, "n1", 1.5], []),
    ],
)
def test_citations_are_read_as_models_write_them(given: Any, read: list[int]) -> None:
    assert visual_specs_cites(given) == read


@pytest.mark.parametrize("kind", KINDS)
def test_a_spec_already_in_the_grammar_is_unchanged_by_normalizing(kind: str) -> None:
    spec = visual_prompt.example(kind)
    assert visuals.validate_spec(visual_dialect.normalize(spec, kind=kind), CITATIONS) == (
        visuals.validate_spec(spec, CITATIONS)
    )


def test_an_ungrounded_diagram_in_any_dialect_is_declined_without_a_repair() -> None:
    reply = {
        "nodes": [{"name": f"Step {i}"} for i in range(4)],
        "links": [{"source": "Step 0", "target": "Step 1", "citations": [1]}],
    }
    llm = _Scripted(json.dumps(reply), json.dumps(CANONICAL))
    result = visuals.build_diagram("the release", CITATIONS, llm, prompt="p")

    assert result["status"] == "declined" and "1 of 5 elements" in result["reason"]
    assert len(llm.calls) == 1, "a model is never asked to add citations until the check passes"


# ---------------------------------------------------------------------------
# the prompt: any model is told the same, exactly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_has_a_worked_example_the_check_accepts(kind: str) -> None:
    result = visuals.validate_spec(visual_prompt.example(kind), CITATIONS)
    assert result["status"] == "ok", result
    assert result["diagram"]["kind"] == kind
    assert result["grounding"]["inferred"] == 0, "an example teaches citing everything"


def test_the_call_names_the_citable_passages_and_ends_on_the_contract() -> None:
    llm = _Scripted(_SAME)
    visuals.build_diagram("the release", CITATIONS, llm, prompt="Passages: …", kind="state machine")
    system, prompt = llm.calls[0]["system"], llm.calls[0]["prompt"]

    assert "Passage numbers you may cite: 1, 2, 3." in prompt
    assert prompt.rstrip().endswith(visual_prompt.CONTRACT)
    assert visual_prompt.CONTRACT in system
    assert "Use kind: state." in system
    assert json.dumps(visual_prompt.example("state"), separators=(",", ":")) in system
    assert llm.calls[0]["json_mode"] is True


# ---------------------------------------------------------------------------
# repair, and what a reader sees when the model cannot draw at all
# ---------------------------------------------------------------------------


def test_an_unreadable_reply_gets_one_repair_that_says_what_was_wrong() -> None:
    before = _outcome("repaired")
    llm = _Scripted("I would draw Build, then Test, then Ship.", _SAME)
    result = visuals.build_diagram("the release", CITATIONS, llm, prompt="p")

    assert result["status"] == "ok"
    assert len(llm.calls) == 2
    repair = llm.calls[1]["prompt"]
    assert "could not be used: the model did not return a diagram" in repair
    assert "I would draw Build, then Test, then Ship." in repair
    assert _outcome("repaired") == before + 1


def test_a_reply_with_nothing_drawable_is_repaired_too() -> None:
    llm = _Scripted(json.dumps({"kind": "flow", "nodes": [], "edges": []}), _SAME)
    result = visuals.build_diagram("the release", CITATIONS, llm, prompt="p")
    assert result["status"] == "ok" and len(llm.calls) == 2
    assert "fewer than 2 nodes" in llm.calls[1]["prompt"]


@pytest.mark.parametrize(
    "failure",
    [ProviderError("503 from provider: overloaded"), TimeoutError("read timed out")],
)
def test_a_failed_call_never_raises_and_says_why(failure: Exception) -> None:
    llm = _Scripted(failure)
    result = visuals.build_diagram("the release", CITATIONS, llm, prompt="p")

    assert result["status"] == "declined"
    assert str(failure) in result["reason"]
    assert len(llm.calls) == 1, "a repair cannot help a transport failure"


def test_when_the_model_cannot_draw_the_indexs_own_links_are_shown() -> None:
    before = _outcome("fallback")
    llm = _Scripted("no", "still no")
    visual = answering.visual_for(
        "draw the release", "diagram", citations=CITATIONS, facts=FACTS, figures=[], llm=llm
    )

    assert visual is not None and visual["status"] == "ok"
    assert visual["source"] == "graph" and visual["fallback_from"] == "model"
    assert "could not be used (the model did not return a diagram)" in visual["note"]
    assert visual["grounding"]["inferred"] == 0
    assert _outcome("fallback") == before + 1


def test_with_nothing_to_fall_back_on_the_models_reason_is_kept() -> None:
    llm = _Scripted(ProviderError("401 from provider: bad key"))
    visual = answering.visual_for(
        "draw the release", "diagram", citations=CITATIONS, facts=[], figures=[], llm=llm
    )
    assert visual is not None and visual["status"] == "declined"
    assert "401 from provider: bad key" in visual["reason"]


def test_an_image_request_that_falls_all_the_way_back_says_both_things() -> None:
    visual = answering.visual_for(
        "show me the release diagram",
        "image",
        citations=CITATIONS,
        facts=FACTS,
        figures=[],
        llm=_Scripted("no", "no"),
    )
    assert visual is not None and visual["status"] == "ok"
    assert visual["fallback_from"] == "image"
    assert visual["note"].startswith("none of the cited sources shows an image")
    assert "the model's drawing could not be used" in visual["note"]


def test_a_model_that_draws_is_never_replaced_by_the_fallback() -> None:
    visual = answering.visual_for(
        "draw the release",
        "diagram",
        citations=CITATIONS,
        facts=FACTS,
        figures=[],
        llm=_Scripted(_SAME),
    )
    assert visual is not None and visual["source"] == "model" and "fallback_from" not in visual


def test_extract_prefers_the_spec_over_the_first_object() -> None:
    raw = '{"note": "example"} and then ' + _SAME
    assert visual_dialect.extract(raw) == CANONICAL
    assert visual_dialect.extract("no braces at all") is None
    assert visual_dialect.extract(None) is None
