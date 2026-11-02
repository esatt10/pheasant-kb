"""Marker clean-up changes prose, never code (``identity[0]`` is an index)."""

from __future__ import annotations

from pheasant.assistant.markup import outside_code

ANSWER = (
    "Builds it [1] and more [9] .\n\n"
    "```hcl\n# [1]\nprincipal_id = azurerm_api_management.gw.identity[0].principal_id\n"
    "subnet_id = azurerm_virtual_network.v.subnet[12].id\n```\n\n"
    "Fill in the rest [7] ."
)


def test_outside_code_leaves_fenced_blocks_alone() -> None:
    upper = outside_code(ANSWER, str.upper)
    assert "BUILDS IT" in upper and "FILL IN THE REST" in upper
    assert "identity[0].principal_id" in upper, "the code is untouched"
    unclosed = outside_code("prose [1]\n```hcl\nx = y[0]\n", lambda s: s.replace("[1]", ""))
    assert unclosed == "prose \n```hcl\nx = y[0]\n", "an unclosed fence runs to the end"


def test_verify_keeps_indexes_in_code_and_drops_dangling_markers_in_prose(monkeypatch) -> None:
    from tests.test_compose_answers import PLAN, _search_everything
    from tests.test_decisions_api import _LLM, _decider, _run, _Wire

    llm = _LLM([PLAN, ANSWER])
    wire = _Wire({"sufficient": 0.9}, {"grounded": 0.9, "responsive": 0.9})
    result = _run(
        _search_everything(),
        llm,
        _decider(),
        monkeypatch,
        wire,
        intent="compose",
        compose_review=False,
    )
    assert "identity[0].principal_id" in result.answer, "measured: [0] was removed as a marker"
    assert "subnet[12].id" in result.answer
    assert "[9]" not in result.answer and "[7]" not in result.answer, "dangling prose markers go"
    assert "Builds it [1] and more." in result.answer


def test_history_keeps_an_earlier_artifacts_indexes() -> None:
    from pheasant.assistant.conversation import normalize_history

    (turn,) = normalize_history([{"question": "q", "answer": ANSWER}])
    assert "identity[0].principal_id" in turn.answer
    assert "Builds it and more" in turn.answer, "prose markers are still stripped"
