"""Mathematics in answers: the prompt asks for TeX when it applies, and only then.

The UI typesets TeX (`ui/src/util/math.ts`), so an answer about an equation
should write one in the delimiters it reads. Three things are held here:

1. ``needs_math`` reads a question or a passage as mathematical by narrow
   rules — a TeX delimiter or command, or a question asking for an equation —
   and prose with dollar amounts is not mathematics.
2. The rule reaches the answering prompt when it applies and is absent
   otherwise, so every non-mathematical prompt stays byte-identical; a
   diagram's prompt never carries it, because its labels are not typeset.
3. TeX that crossed a JSON reply with single backslashes (``"\\alpha"``, not
   valid JSON) is read as the TeX the model meant rather than discarding the
   reply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from pheasant.assistant.chat import MATH_RULE, build_prompt, needs_math, system_prompt_for
from pheasant.assistant.llm import LLM
from pheasant.assistant.replies import json_object
from pheasant.config.schema import PheasantConfig
from pheasant.mcp_server.tools import PheasantTools

# ---------------------------------------------------------------------------
# 1. Reading a question or a passage as mathematical
# ---------------------------------------------------------------------------


def _passage(text: str) -> list[dict[str, Any]]:
    return [{"index": 1, "snippet": text}]


@pytest.mark.parametrize(
    "text",
    [
        r"The loss is $L = \sum_i (y_i - \hat y_i)^2$ over a batch.",
        "Displayed:\n$$\nE = mc^2\n$$\n",
        r"Inline \(a^2 + b^2 = c^2\) in a sentence.",
        r"Displayed \[ \int_0^1 x\,dx \] in a sentence.",
        "\\begin{align}\na &= b\n\\end{align}",
    ],
)
def test_a_passage_with_tex_needs_math(text: str) -> None:
    assert needs_math("what does it say", _passage(text))


@pytest.mark.parametrize(
    "text",
    [
        "The plan costs $5 a month, or $50 a year.",
        "Set PRICE=$PRICE and run it.",
        "No mathematics here at all.",
        r"A Windows path C:\temp\new is not TeX.",
    ],
)
def test_prose_is_not_math(text: str) -> None:
    assert not needs_math("what does it say", _passage(text))


def test_a_hydrated_document_is_read_before_its_snippet() -> None:
    @dataclass
    class Document:
        text: str

    citations = _passage("a preview with no maths")
    documents = {1: Document(r"Later in the file: $\frac{a}{b}$.")}
    assert needs_math("what does it say", citations, documents)
    assert not needs_math("what does it say", citations)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("what is the equation for the loss?", True),
        ("derive the update rule", True),
        ("prove the lemma", True),
        ("how does the gateway rotate credentials?", False),
    ],
)
def test_a_question_asking_for_an_equation_needs_math(question: str, expected: bool) -> None:
    assert needs_math(question, []) is expected


# ---------------------------------------------------------------------------
# 2. The prompt
# ---------------------------------------------------------------------------


def test_the_rule_precedes_the_question_only_when_it_applies() -> None:
    tex = _passage(r"The loss is $\frac{1}{n} \sum_i x_i$.")
    for citations in (tex, _passage("No mathematics here.")):
        citations[0]["title"] = "notes.md"
    with_math = build_prompt("what is the loss?", tex, [])
    assert with_math.endswith(f"{MATH_RULE}\n\nQuestion: what is the loss?")
    assert "$...$" in MATH_RULE and "$$...$$" in MATH_RULE

    plain = _passage("No mathematics here.")
    plain[0]["title"] = "notes.md"
    assert MATH_RULE not in build_prompt("what is the loss?", plain, [])
    # A diagram's labels are not typeset, so its prompt never asks for TeX.
    assert MATH_RULE not in build_prompt("what is the loss?", tex, [], math_notation=False)


CORPUS = {
    "notes/loss.md": (
        "# Loss\n\nTraining minimises the squared error "
        "$L(\\theta) = \\sum_i (y_i - \\hat y_i)^2$ over every batch.\n"
    ),
    "notes/deploy.md": "# Deploy\n\nThe gateway rotates credentials nightly at 02:00.\n",
}


@dataclass
class _Model(LLM):
    """A connected model that records what it is sent."""

    sent: list[tuple[str, str]] = field(default_factory=list)

    def complete(self, system, prompt, **kwargs):  # type: ignore[override]
        self.sent.append((system, prompt))
        return "It is $L(\\theta) = \\sum_i (y_i - \\hat y_i)^2$ [1]."


@pytest.fixture(scope="module")
def tools(tmp_path_factory: pytest.TempPathFactory) -> PheasantTools:
    root = tmp_path_factory.mktemp("math")
    workspace = root / "workspace"
    for relative, text in CORPUS.items():
        path = Path(workspace / relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "math",
                "state_path": str(root / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(root / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "storage": {"graph_snapshots": False},
            "assistant": {"provider": "none"},
            "sources": [
                {"name": "notes", "type": "markdown_folder", "path": str(workspace / "notes")}
            ],
        }
    )
    built = PheasantTools(config)
    built.engine.sync_source("notes", "full")
    built.engine.reload_graph()
    return built


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> _Model:
    connected = _Model(provider="openai", api_key="k", model="m")
    monkeypatch.setattr("pheasant.assistant.answering.resolve_llm", lambda *a, **k: connected)
    return connected


def test_a_mathematical_passage_puts_the_rule_in_the_prompt(
    tools: PheasantTools, model: _Model
) -> None:
    tools.ask_knowledge_base(
        tools.config.knowledge_base_id, "what does training minimise?", workflow="simple"
    )
    system, prompt = model.sent[-1]
    assert "loss.md" in prompt
    assert f"{MATH_RULE}\n\nQuestion: what does training minimise?" in prompt
    assert system == system_prompt_for("knowledge", "short", figures=False)


def test_a_question_without_math_sends_the_prompt_it_always_sent(
    tools: PheasantTools, model: _Model
) -> None:
    tools.ask_knowledge_base(
        tools.config.knowledge_base_id,
        "when does the gateway rotate credentials?",
        workflow="simple",
    )
    system, prompt = model.sent[-1]
    assert "loss.md" not in prompt, "the fixture must keep the TeX passage out of this answer"
    assert MATH_RULE not in prompt
    assert system == system_prompt_for("knowledge", "short", figures=False)


# ---------------------------------------------------------------------------
# 3. TeX through a JSON reply
# ---------------------------------------------------------------------------


def test_single_backslash_tex_in_a_json_reply_is_read_as_tex() -> None:
    raw = '{"sufficient": true, "answer": "It is $\\alpha + \\sum_i x_i$ [1].", "missing": ""}'
    parsed = json_object(raw)
    assert parsed is not None
    assert parsed["answer"] == r"It is $\alpha + \sum_i x_i$ [1]."


def test_valid_json_escapes_are_left_alone() -> None:
    raw = '{"answer": "line one\\nline two \\"quoted\\" \\\\alpha"}'
    assert json_object(raw) == {"answer": 'line one\nline two "quoted" \\alpha'}
