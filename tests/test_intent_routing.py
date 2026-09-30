"""The intent router: depth and visual beside intent, and what each changes.

Three claims, each with a way to fail:

1. **The rules read questions the way a person would**, on a labelled set —
   the router's evaluation set. A change that moves any row fails here, which
   is the point: routing is a product decision, and it should not drift as a
   side effect of an edit to a regex.
2. **``short`` is exactly the old behaviour.** No classify step on the simple
   workflow, the same search size, the same prompt. The default route must not
   cost the answers pheasant already gives anything.
3. **``medium`` and ``long`` do what they say**, including the long answer's
   outline → parallel sections → stitch, a section that fails or overruns
   degrading to its passages, and every marker keeping the global numbering.
"""

from __future__ import annotations

import threading
import time

import pytest

from pheasant.assistant import longform, routing
from pheasant.assistant.llm import LLM
from pheasant.assistant.retrieval import PheasantRetriever
from pheasant.assistant.workflows import WorkflowRequest
from pheasant.assistant.workflows.simple import SimpleWorkflow

# (question, depth, visual) — the router's labelled evaluation set.
LABELLED = [
    ("what does the sync engine do?", "short", "none"),
    ("briefly, what is a region?", "short", "none"),
    ("tl;dr of the memory plane", "short", "none"),
    ("give me an overview of the retrieval stack", "medium", "none"),
    ("compare text search and vector search", "medium", "none"),
    ("what are the trade-offs of the rows graph backend?", "medium", "none"),
    ("walk me through how a sync commits", "medium", "none"),
    ("explain the evaluation plane in detail", "long", "none"),
    ("write a comprehensive report on the tuning plane", "long", "none"),
    ("deep dive into how the fleet scales", "long", "none"),
    ("draw a diagram of the release process", "short", "diagram"),
    ("visualize how retrieval fuses its arms", "short", "diagram"),
    ("create a flowchart of the ingestion pipeline", "short", "diagram"),
    ("give me a detailed sequence diagram of a sync", "long", "diagram"),
    ("show me the deploy pipeline diagram", "short", "image"),
    ("find the architecture image in the design doc", "short", "image"),
    ("where is the screenshot of the settings page?", "short", "image"),
    # The false positives the patterns were narrowed against.
    ("how do I configure visual studio code for this repo?", "short", "none"),
    ("help me figure out why sync is slow", "short", "none"),
    ("show me how to figure out the region's generation", "short", "none"),
]


@pytest.mark.parametrize(("question", "depth", "visual"), LABELLED)
def test_the_router_reads_the_labelled_set(question: str, depth: str, visual: str) -> None:
    assert routing.classify_depth(question)[0] == depth
    assert routing.classify_visual(question)[0] == visual


def test_a_pinned_axis_wins_over_the_question() -> None:
    assert routing.classify_depth("briefly, what is it?", "long") == (
        "long",
        "pinned by the caller",
        "pinned",
    )
    assert routing.classify_visual("draw the pipeline", "none")[0] == "none"
    # "auto" and junk are both "read the question".
    assert routing.classify_depth("explain in detail", "auto")[0] == "long"
    assert routing.classify_depth("explain in detail", "enormous")[0] == "long"


def test_a_depth_profile_never_overrides_what_the_caller_set() -> None:
    merged = routing.depth_options("long", {"max_context_passages": 5}, {"max_context_passages"})
    assert merged["max_context_passages"] == 5
    assert merged["max_sections"] == routing.DEPTH_PROFILES["long"]["max_sections"]
    assert routing.depth_options("short", {"a": 1}, set()) == {"a": 1}


# ---------------------------------------------------------------------------
# The simple workflow under each depth
# ---------------------------------------------------------------------------


class _Search:
    def __init__(self) -> None:
        self.limits: list[int] = []

    def search_context(self, kb, query, mode, max_results, source_name, **kwargs):
        self.limits.append(max_results)
        hits = [
            {
                "node_id": f"file:kb:docs/{i}.md",
                "chunk_id": f"chunk:kb:docs/{i}.md:0",
                "type": "chunk",
                "title": f"docs/{i}.md",
                "relative_path": f"docs/{i}.md",
                "score": 10.0 - i,
                "chunks": [{"text_preview": f"Passage {i} about the pipeline."}],
            }
            for i in range(30)
        ]
        return {"results": hits[:max_results]}


class _Recorder(LLM):
    def __init__(self, reply="An answer [1].") -> None:
        super().__init__(provider="openai", api_key="k", model="m")
        self.calls: list[tuple[str, str, int | None]] = []
        self.reply = reply
        self.lock = threading.Lock()

    def complete(self, system, prompt, *, max_output_tokens=None):  # type: ignore[override]
        with self.lock:
            self.calls.append((system, prompt, max_output_tokens))
        return self.reply(system, prompt) if callable(self.reply) else self.reply


def _retriever(search: _Search) -> PheasantRetriever:
    return PheasantRetriever(search=search, knowledge_base="kb", graph=None, state=None)


def test_a_short_answer_is_the_answer_pheasant_always_gave() -> None:
    search, llm = _Search(), _Recorder()
    result = SimpleWorkflow().run(
        WorkflowRequest(question="what does the pipeline do?", max_results=8),
        _retriever(search),
        llm,
    )

    assert [step.name for step in result.steps] == ["retrieve", "answer"]
    assert search.limits == [8]
    system, _prompt, cap = llm.calls[0]
    assert "LENGTH" not in system and cap is None
    assert result.route["depth"] == "short"


def test_a_medium_answer_reads_more_and_asks_for_sections() -> None:
    search, llm = _Search(), _Recorder()
    result = SimpleWorkflow().run(
        WorkflowRequest(question="give me an overview of the pipeline", max_results=8),
        _retriever(search),
        llm,
    )

    assert result.steps[0].name == "classify" and "medium" in result.steps[0].detail
    assert search.limits == [routing.DEPTH_PROFILES["medium"]["max_context_passages"]]
    system, _prompt, cap = llm.calls[0]
    assert "LENGTH: a medium-length answer" in system
    assert cap == routing.DEPTH_PROFILES["medium"]["max_output_tokens"]
    assert result.counts["depth"] == "medium"


def test_the_single_pass_workflow_says_when_it_answers_long_as_medium() -> None:
    llm = _Recorder()
    result = SimpleWorkflow().run(
        WorkflowRequest(question="explain the pipeline in detail", max_results=8),
        _retriever(_Search()),
        llm,
    )

    assert "answered as medium in one call" in result.steps[0].detail
    assert len(llm.calls) == 1


# ---------------------------------------------------------------------------
# Long form: outline → sections → stitch
# ---------------------------------------------------------------------------


def _citations(n: int) -> list[dict]:
    return [
        {
            "index": i,
            "node_id": f"n{i}",
            "relative_path": f"area{i % 2}/f{i}.md",
            "title": f"f{i}",
            "snippet": f"text {i}",
        }
        for i in range(1, n + 1)
    ]


def test_an_outline_keeps_only_real_passage_numbers() -> None:
    raw = (
        '{"overview": "It works [1].", "sections": ['
        '{"heading": "Parts", "passages": [1, 2, 99]}, {"heading": "", "passages": [3]},'
        '{"heading": "Flow", "passages": [3, "4", "x"]}]}'
    )
    outline = longform.plan_sections(raw, _citations(4), max_sections=5)

    assert outline["planned_by"] == "model"
    assert outline["sections"] == [
        {"heading": "Parts", "passages": [1, 2]},
        {"heading": "Flow", "passages": [3, 4]},
    ]


def test_an_unusable_outline_groups_passages_by_where_they_live() -> None:
    outline = longform.plan_sections("not json", _citations(4), max_sections=5)

    assert outline["planned_by"] == "location"
    assert sorted(n for s in outline["sections"] for n in s["passages"]) == [1, 2, 3, 4]


def test_sections_run_in_parallel_and_a_failing_one_degrades_to_its_passages() -> None:
    citations = _citations(3)
    outline = {
        "sections": [
            {"heading": h, "passages": [i]} for i, h in ((1, "One"), (2, "Two"), (3, "Three"))
        ]
    }
    running, peak = [0], [0]
    lock = threading.Lock()

    def write(section, own):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        if section["heading"] == "Two":
            raise RuntimeError("provider exploded")
        return f"{section['heading']} body [{own[0]['index']}]"

    sections, degraded = longform.write_sections(
        outline=outline,
        citations=citations,
        write=write,
        fallback=longform.extractive_section,
        concurrency=3,
        deadline_seconds=5,
    )

    assert peak[0] > 1, "sections must be written concurrently"
    assert [s["heading"] for s in sections] == ["One", "Two", "Three"]
    assert sections[0]["text"] == "One body [1]"
    assert "[2]" in sections[1]["text"], "the fallback keeps the passage's own number"
    assert degraded == ["Two (provider exploded)"]


def test_a_section_past_the_deadline_does_not_hold_the_answer() -> None:
    outline = {"sections": [{"heading": "Slow", "passages": [1]}]}
    started = time.monotonic()
    sections, degraded = longform.write_sections(
        outline=outline,
        citations=_citations(1),
        write=lambda s, o: time.sleep(2) or "late",
        fallback=longform.extractive_section,
        concurrency=1,
        deadline_seconds=0.2,
    )

    assert time.monotonic() - started < 1.5
    assert degraded == ["Slow (time budget reached)"]


def test_a_long_agentic_answer_is_outlined_written_and_stitched() -> None:
    pytest.importorskip("langgraph")
    from pheasant.assistant.workflows.agentic import AgenticWorkflow

    def reply(system, prompt):
        if "plan retrieval" in system:
            return '{"queries": ["pipeline"], "modes": ["text"], "intent": "knowledge"}'
        if "judge whether" in system:
            return '{"sufficient": true}'
        if "plan a long, sectioned answer" in system:
            return (
                '{"overview": "The pipeline has two areas [1].", "sections": ['
                '{"heading": "Area zero", "passages": [2, 4]},'
                '{"heading": "Area one", "passages": [1, 3]}]}'
            )
        heading = prompt.split("Section to write: ", 1)[1].splitlines()[0]
        return f"Body for {heading} [2]."

    llm = _Recorder(reply)
    result = AgenticWorkflow().run(
        WorkflowRequest(question="write a comprehensive report on the pipeline", max_results=8),
        _retriever(_Search()),
        llm,
    )

    names = [step.name for step in result.steps]
    assert "outline" in names and "sections" in names
    assert result.answer.startswith("The pipeline has two areas [1].")
    assert "### Area zero\n\nBody for Area zero [2]." in result.answer
    assert result.route["depth"] == "long" and result.counts["depth"] == "long"
    section_prompts = [p for s, p, _ in llm.calls if "ONE section" in s]
    # Each section saw only its own passages, under their original numbers.
    zero = next(p for p in section_prompts if "Area zero" in p)
    assert "[2] docs/1.md" in zero and "[1] docs/0.md" not in zero


def test_the_planner_may_overrule_a_rule_but_never_a_pin() -> None:
    pytest.importorskip("langgraph")
    from pheasant.assistant.workflows.agentic import AgenticWorkflow

    def reply(system, prompt):
        if "plan retrieval" in system:
            return '{"queries": ["pipeline"], "modes": ["text"], "depth": "medium"}'
        if "judge whether" in system:
            return '{"sufficient": true}'
        return "Answer [1]."

    ruled = AgenticWorkflow().run(
        WorkflowRequest(question="what is the pipeline?"), _retriever(_Search()), _Recorder(reply)
    )
    pinned = AgenticWorkflow().run(
        WorkflowRequest(question="what is the pipeline?", options={"depth": "short"}),
        _retriever(_Search()),
        _Recorder(reply),
    )

    assert ruled.route["depth"] == "medium" and ruled.route["decided_by"]["depth"] == "planner"
    assert pinned.route["depth"] == "short" and pinned.route["decided_by"]["depth"] == "pinned"
