"""What the answering step is actually given to answer from.

Retrieval was already finding the right files; the answers were thin because
of what happened *after* the search. Three things are under test here:

1. **File-level content.** Search scores chunks, and the SQL layer caps a
   chunk preview at 500 characters. Answering "what does this repository do"
   from 500-character windows is how you get an answer that names exactly the
   right files and says nothing about them. Chunks are now joined back up
   into the files they came from, with the line spans, headings and artifact
   metadata the index already recorded.

2. **A budget that knows what a file is.** Code and config are read whole —
   an excerpt of a Python module with its imports cut off is not a smaller
   answer, it is the thing that makes a model invent an import. Large prose
   is excerpted to the matched neighbourhood instead of being allowed to
   swallow the batch budget.

3. **Two answer shapes.** "What does this repository do" and "how do I use
   this tool" need different evidence and different output. The intent is
   classified deterministically, drives retrieval breadth-vs-depth, the
   sufficiency bar, and the answering prompt — and is visible in the trace.

Plus the structural grounding the planner now gets: a planner told only
"2,132 indexed files" writes generic queries; one told the layout, the
languages and the corpus's own vocabulary writes queries that hit.
"""

from __future__ import annotations

import json

import pytest

from pheasant.assistant.chat import (
    CONTENT_DEFAULTS,
    build_prompt,
    classify_intent,
    hydrate_citations,
    system_prompt_for,
)
from pheasant.assistant.llm import LLM
from pheasant.assistant.retrieval import LARGE_FILE_BYTES, PheasantRetriever, _is_code
from pheasant.assistant.workflows import BUILTIN_WORKFLOWS, WorkflowRequest, list_workflows
from pheasant.assistant.workflows.simple import SimpleWorkflow
from pheasant.persistence.state_store import StateStore

# --------------------------------------------------------------- a real index


def _store(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.migrate()
    return store


def _add_file(
    store,
    node_id: str,
    relative_path: str,
    chunks: list[tuple[str, str]],
    *,
    size_bytes: int | None = None,
    language: str | None = None,
    symbols: list[str] = (),
    artifact_type: str = "file",
):
    """Insert one artifact and its chunks the way the sync engine would."""
    store.conn.execute(
        "INSERT INTO artifacts (id, source_id, type, path, relative_path, size_bytes, "
        "git_branch, status) VALUES (?,?,?,?,?,?,?,?)",
        (
            node_id,
            "demo",
            artifact_type,
            f"/w/{relative_path}",
            relative_path,
            size_bytes if size_bytes is not None else sum(len(t) for _h, t in chunks),
            "main",
            "indexed",
        ),
    )
    line = 1
    for index, (heading, text) in enumerate(chunks):
        lines = text.count("\n") + 1
        store.conn.execute(
            "INSERT INTO chunks (id, artifact_id, source_id, chunk_index, heading_path, "
            "start_line, end_line, text, text_hash) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                f"{node_id}#c{index}",
                node_id,
                "demo",
                index,
                heading,
                line,
                line + lines - 1,
                text,
                f"h{index}",
            ),
        )
        # The FTS row too — it is what backs `chunks_vocab`, which is now the
        # source of the corpus's vocabulary.
        store.conn.execute(
            "INSERT INTO chunks_fts (chunk_id, source_id, artifact_id, title, path, "
            "heading_path, text) VALUES (?,?,?,?,?,?,?)",
            (
                f"{node_id}#c{index}",
                "demo",
                node_id,
                relative_path.rsplit("/", 1)[-1],
                relative_path,
                heading,
                text,
            ),
        )
        line += lines
    for position, name in enumerate(symbols):
        store.conn.execute(
            "INSERT INTO symbols (id, artifact_id, source_id, language, symbol_type, name, "
            "start_line) VALUES (?,?,?,?,?,?,?)",
            (f"{node_id}:{name}", node_id, "demo", language, "function", name, position + 1),
        )
    store.conn.commit()


def _retriever(store, **kwargs):
    return PheasantRetriever(
        search=kwargs.pop("search", None) or _FakeSearch(),
        knowledge_base="kb",
        graph=kwargs.pop("graph", None),
        state=store,
        config=kwargs.pop("config", None),
    )


class _FakeSearch:
    vector = None

    def search_context(self, kb, query, mode, max_results, source_name, **kwargs):
        return {"query": query, "mode": mode, "results": [], "counts": {}}


def _citation(index: int, node_id: str, title: str, chunk_id: str | None = None) -> dict:
    return {
        "index": index,
        "node_id": node_id,
        "chunk_id": chunk_id,
        "title": title,
        "relative_path": title,
        "source_id": "demo",
        "type": "chunk",
        "score": 1.0,
        "snippet": "a 500-character preview",
        "used": False,
    }


# ------------------------------------------------- chunks → files, with metadata


def test_a_file_is_reassembled_from_its_chunks_in_order(tmp_path) -> None:
    store = _store(tmp_path)
    _add_file(
        store,
        "file:demo:docs/guide.md",
        "docs/guide.md",
        [
            ("Guide", "First section about setup."),
            ("Guide > Detail", "Second section about running it."),
            ("Guide > More", "Third section about tuning."),
        ],
    )

    documents = _retriever(store).documents(["file:demo:docs/guide.md"])
    document = documents["file:demo:docs/guide.md"]

    # Every chunk, in order, not just the one search matched.
    assert "First section" in document.text
    assert "Second section" in document.text
    assert "Third section" in document.text
    assert (
        document.text.index("First") < document.text.index("Second") < document.text.index("Third")
    )
    assert document.chunk_count == 3
    assert document.included_chunks == 3
    assert document.truncated is False
    # Chunk provenance survives the join-back, so the model can cite lines.
    assert "Guide > Detail" in document.text
    assert "lines" in document.text
    assert document.line_span is not None and document.line_span[0] == 1


def test_reassembly_carries_the_artifact_metadata(tmp_path) -> None:
    """The "aggregated back to file level *with metadata*" half."""
    store = _store(tmp_path)
    _add_file(
        store,
        "file:demo:app/service.py",
        "app/service.py",
        [("", "def start():\n    return 1")],
        language="python",
        symbols=["start", "stop"],
    )

    document = _retriever(store).documents(["file:demo:app/service.py"])["file:demo:app/service.py"]

    described = document.describe()
    assert document.language == "python"
    assert document.symbols == ["start", "stop"]
    assert "source: demo" in described
    assert "branch: main" in described
    assert "defines: start, stop" in described


def test_one_batched_read_regardless_of_how_many_documents(tmp_path) -> None:
    """A per-node loop over content() was two queries each."""
    store = _store(tmp_path)
    for index in range(6):
        _add_file(store, f"file:demo:d{index}.md", f"d{index}.md", [("", f"body {index}")])

    seen: list[str] = []
    original = store.rows
    store.rows = lambda sql, params=(): (seen.append(sql), original(sql, params))[1]  # type: ignore[assignment]

    documents = _retriever(store).documents([f"file:demo:d{i}.md" for i in range(6)])

    assert len(documents) == 6
    assert len(seen) == 5, "expected batched auth, metadata, descriptors, symbols, and text reads"
    assert sum("SUBSTR(text" in sql for sql in seen) == 1


def test_large_document_hydration_transfers_only_selected_chunk_bodies(tmp_path) -> None:
    store = _store(tmp_path)
    node_id = "file:demo:docs/large.md"
    chunks = [(f"Section {i}", f"Unique body for section {i}. " + ("x" * 80)) for i in range(8)]
    _add_file(store, node_id, "docs/large.md", chunks, size_bytes=50_000)

    seen: list[tuple[str, tuple]] = []
    original = store.rows
    store.rows = lambda sql, params=(): (seen.append((sql, params)), original(sql, params))[1]  # type: ignore[assignment]

    document = _retriever(store).documents(
        [node_id],
        anchors={node_id: [f"{node_id}#c6"]},
        max_chars=1000,
        large_file_bytes=1000,
    )[node_id]

    text_reads = [(sql, params) for sql, params in seen if "SUBSTR(text" in sql]
    assert len(text_reads) == 1
    sql, params = text_reads[0]
    body_ids = params[len(params) * 2 // 3 :]
    assert 0 < len(body_ids) < len(chunks)
    assert "chunk(s) omitted" in document.text
    assert document.chunk_count == len(chunks)


# ------------------------------------------------------ what counts as "large"


def test_code_files_are_never_treated_as_large(tmp_path) -> None:
    """A module read halfway is not a smaller answer — it is a wrong one."""
    store = _store(tmp_path)
    body = "\n".join(f"def function_{i}():\n    return {i}" for i in range(400))
    chunks = [("", body[i : i + 1500]) for i in range(0, len(body), 1500)]
    _add_file(
        store,
        "file:demo:app/big.py",
        "app/big.py",
        chunks,
        size_bytes=LARGE_FILE_BYTES * 3,  # far past the prose threshold
        language="python",
    )

    document = _retriever(store).documents(["file:demo:app/big.py"])["file:demo:app/big.py"]

    assert document.included_chunks == document.chunk_count
    assert document.truncated is False
    assert "function_0" in document.text and "function_399" in document.text


def test_a_large_prose_file_is_cut_to_the_matched_neighbourhood(tmp_path) -> None:
    store = _store(tmp_path)
    chunks = [(f"Section {i}", f"Paragraph {i}. " + "filler " * 40) for i in range(20)]
    node_id = "file:demo:docs/handbook.md"
    _add_file(
        store,
        node_id,
        "docs/handbook.md",
        chunks,
        size_bytes=LARGE_FILE_BYTES + 1,
        artifact_type="markdown_note",
    )

    document = _retriever(store).documents([node_id], anchors={node_id: [f"{node_id}#c10"]})[
        node_id
    ]

    assert document.truncated is True
    # The match and its immediate neighbours, plus chunk 0 for orientation.
    assert "Paragraph 10." in document.text
    assert "Paragraph 9." in document.text and "Paragraph 11." in document.text
    assert "Paragraph 0." in document.text
    # ...and nothing from the far side of the document.
    assert "Paragraph 5." not in document.text
    assert "Paragraph 18." not in document.text
    # The excerpting is stated, so the model does not claim it saw the file.
    assert "chunk(s) omitted" in document.text


def test_a_small_prose_file_is_still_read_whole(tmp_path) -> None:
    """The large-file policy keys off the original size, not the text length."""
    store = _store(tmp_path)
    chunks = [(f"S{i}", f"Paragraph {i}.") for i in range(8)]
    node_id = "file:demo:docs/small.md"
    _add_file(store, node_id, "docs/small.md", chunks, size_bytes=2_000)

    document = _retriever(store).documents([node_id], anchors={node_id: [f"{node_id}#c4"]})[node_id]

    assert document.included_chunks == 8
    assert document.truncated is False


@pytest.mark.parametrize(
    ("path", "language", "expected"),
    [
        ("app/main.py", None, True),
        ("infra/values.yaml", None, True),
        ("package.json", None, True),
        ("Dockerfile", None, True),
        ("docs/handbook.md", None, False),
        ("notes/meeting.txt", None, False),
        # Symbols extracted at index time settle it regardless of extension.
        ("scripts/tool", "python", True),
    ],
)
def test_code_detection(path, language, expected) -> None:
    assert _is_code(path, language) is expected


def test_the_batch_budget_funds_documents_in_citation_order(tmp_path) -> None:
    store = _store(tmp_path)
    for index in range(4):
        _add_file(
            store,
            f"file:demo:doc{index}.md",
            f"doc{index}.md",
            [("", "x" * 900)],
            size_bytes=900,
        )

    documents = _retriever(store).documents(
        [f"file:demo:doc{i}.md" for i in range(4)], budget_chars=2000
    )

    # The best-scoring hit is never the one that gets starved.
    assert "file:demo:doc0.md" in documents
    assert len(documents) < 4


# ------------------------------------------------------ hydration → the prompt


def test_hydration_replaces_the_preview_with_the_file(tmp_path) -> None:
    store = _store(tmp_path)
    _add_file(
        store,
        "file:demo:docs/guide.md",
        "docs/guide.md",
        [("Guide", "The whole point of the guide, in full.")],
    )
    citations = [_citation(1, "file:demo:docs/guide.md", "docs/guide.md")]

    documents = hydrate_citations(_retriever(store), citations, CONTENT_DEFAULTS)
    prompt = build_prompt("what is this", citations, [], documents)

    assert 1 in documents
    assert "The whole point of the guide, in full." in prompt
    # The citation record itself is untouched: `snippet` is the UI's preview
    # card, and blowing it up to a whole file would change the API contract.
    assert citations[0]["snippet"] == "a 500-character preview"


def test_two_chunks_of_one_file_hydrate_once(tmp_path) -> None:
    """Both matched chunks anchor the same reassembly; repeating it is waste."""
    store = _store(tmp_path)
    node_id = "file:demo:docs/guide.md"
    _add_file(store, node_id, "docs/guide.md", [("A", "first part"), ("B", "second part")])
    citations = [
        _citation(1, node_id, "docs/guide.md", chunk_id=f"{node_id}#c0"),
        _citation(2, node_id, "docs/guide.md", chunk_id=f"{node_id}#c1"),
    ]

    documents = hydrate_citations(_retriever(store), citations, CONTENT_DEFAULTS)

    assert list(documents) == [1]
    prompt = build_prompt("q", citations, [], documents)
    assert prompt.count("first part") == 1


def test_hydration_degrades_to_snippets_rather_than_failing(tmp_path) -> None:
    """No state store is a normal deployment, not an error branch."""
    citations = [_citation(1, "file:demo:x.md", "x.md")]
    retriever = PheasantRetriever(search=_FakeSearch(), knowledge_base="kb", state=None)

    assert hydrate_citations(retriever, citations, CONTENT_DEFAULTS) == {}
    assert "a 500-character preview" in build_prompt("q", citations, [], {})


def test_content_can_be_switched_off(tmp_path) -> None:
    store = _store(tmp_path)
    _add_file(store, "file:demo:a.md", "a.md", [("", "body")])
    citations = [_citation(1, "file:demo:a.md", "a.md")]

    assert hydrate_citations(_retriever(store), citations, {"include_full_content": False}) == {}


# ------------------------------------------------------------------- intent


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What does this repository do?", "knowledge"),
        ("what is the sync engine", "knowledge"),
        ("Explain the architecture", "knowledge"),
        ("Summarize the docs folder", "knowledge"),
        ("How does the watcher work?", "knowledge"),
        ("How do I leverage this tool?", "procedural"),
        ("how to configure a connector", "procedural"),
        ("show me an example of a workflow", "procedural"),
        ("walk me through setting up a source", "procedural"),
        ("How can I install the agent extra?", "procedural"),
        ("", "knowledge"),
        ("checkpoints", "knowledge"),
    ],
)
def test_intent_classification(question, expected) -> None:
    intent, why = classify_intent(question)
    assert intent == expected, f"{question!r} → {intent} ({why})"
    assert why


def test_classification_is_deterministic() -> None:
    assert classify_intent("How do I use it?") == classify_intent("How do I use it?")


def test_each_intent_has_its_own_answering_prompt() -> None:
    knowledge = system_prompt_for("knowledge")
    procedural = system_prompt_for("procedural")

    assert knowledge != procedural
    assert "KNOWLEDGE SUMMARY" in knowledge
    assert "PROCEDURAL" in procedural
    # The rule that keeps a procedural answer honest.
    assert "never invent an API" in procedural
    # Both keep the shared grounding rules.
    for prompt in (knowledge, procedural):
        assert "Use ONLY the numbered passages" in prompt


# ------------------------------------------------------------------ workflows


def test_the_three_workflow_options(tmp_path) -> None:
    pytest.importorskip("langgraph")
    entries = list_workflows()
    names = [entry["name"] for entry in entries]

    assert names[: len(BUILTIN_WORKFLOWS)] == list(BUILTIN_WORKFLOWS)
    assert names[:3] == ["knowledge-summary", "agentic", "simple"]
    labels = {entry["name"]: entry["label"] for entry in entries}
    assert labels["knowledge-summary"] == "Knowledge summary"
    assert labels["simple"] == "Single pass"
    assert all(entry["builtin"] for entry in entries[:3])


class _RecordingLLM(LLM):
    """Captures which system prompt each call arrived with."""

    def __init__(self, script):
        super().__init__(provider="openai", api_key="k", model="test-model")
        self.script = script
        self.systems: list[str] = []

    def complete(self, system, prompt, **kwargs):  # type: ignore[override]
        self.systems.append(system)
        self.prompt = prompt
        for marker, reply in self.script:
            if marker in system:
                return reply
        raise AssertionError(f"unscripted system prompt: {system[:60]}")


def _agentic_llm():
    return _RecordingLLM(
        [
            ("plan retrieval", json.dumps({"queries": ["q"], "modes": ["text"]})),
            ("judge whether", json.dumps({"sufficient": True})),
            ("research assistant", "Answer [1]."),
        ]
    )


def _agentic_retriever(store):
    from pheasant.graph.simple import SimpleMultiDiGraph

    class Search:
        vector = None

        def search_context(self, kb, query, mode, max_results, source_name, **kwargs):
            return {
                "query": query,
                "mode": mode,
                "results": [
                    {
                        "node_id": "file:demo:docs/guide.md",
                        "chunk_id": "file:demo:docs/guide.md#c0",
                        "type": "chunk",
                        "title": "docs/guide.md",
                        "relative_path": "docs/guide.md",
                        "score": 2.0,
                        "chunks": [{"text_preview": "short preview"}],
                    }
                ],
                "counts": {},
            }

    return PheasantRetriever(
        search=Search(), knowledge_base="kb", graph=SimpleMultiDiGraph(), state=store
    )


def test_a_procedural_question_is_classified_planned_and_answered_as_one(tmp_path) -> None:
    pytest.importorskip("langgraph")
    from pheasant.assistant.workflows.agentic import AgenticWorkflow

    store = _store(tmp_path)
    _add_file(
        store,
        "file:demo:docs/guide.md",
        "docs/guide.md",
        [("Guide", "from pheasant import Client\nclient = Client()")],
    )
    llm = _agentic_llm()

    result = AgenticWorkflow().run(
        WorkflowRequest(question="How do I leverage the client?"),
        _agentic_retriever(store),
        llm,
    )

    assert result.counts["intent"] == "procedural"
    names = [step.name for step in result.steps]
    assert names[0] == "classify", names
    # The reader can see how the question was read, before the answer lands.
    classify = next(step for step in result.steps if step.name == "classify")
    assert "procedural" in classify.detail
    # The answering call used the procedural prompt, not the generic one.
    assert any("PROCEDURAL" in system for system in llm.systems)
    # ...over the whole file, not the 500-character preview.
    assert "from pheasant import Client" in llm.prompt
    assert "read" in names


def test_a_knowledge_question_gets_the_summary_prompt(tmp_path) -> None:
    pytest.importorskip("langgraph")
    from pheasant.assistant.workflows.agentic import AgenticWorkflow

    store = _store(tmp_path)
    _add_file(store, "file:demo:docs/guide.md", "docs/guide.md", [("Guide", "body")])
    llm = _agentic_llm()

    result = AgenticWorkflow().run(
        WorkflowRequest(question="What does this repository do?"),
        _agentic_retriever(store),
        llm,
    )

    assert result.counts["intent"] == "knowledge"
    assert any("KNOWLEDGE SUMMARY" in system for system in llm.systems)


def test_the_knowledge_summary_workflow_pins_the_intent(tmp_path) -> None:
    """Picking it *is* choosing the reading, even for a "how do I" question."""
    pytest.importorskip("langgraph")
    from pheasant.assistant.workflows.agentic import KnowledgeSummaryWorkflow

    store = _store(tmp_path)
    _add_file(store, "file:demo:docs/guide.md", "docs/guide.md", [("Guide", "body")])
    llm = _agentic_llm()

    result = KnowledgeSummaryWorkflow().run(
        WorkflowRequest(question="How do I leverage the client?"),
        _agentic_retriever(store),
        llm,
    )

    assert result.workflow == "knowledge-summary"
    assert result.counts["intent"] == "knowledge"
    assert any("KNOWLEDGE SUMMARY" in system for system in llm.systems)


def test_intent_profiles_yield_to_an_explicit_option(tmp_path) -> None:
    pytest.importorskip("langgraph")
    from pheasant.assistant.workflows.agentic import INTENT_PROFILES, resolve_options

    base = {"intent": "auto", "max_context_passages": 10, "passage_chars": 6000}

    tuned = resolve_options({"options": dict(base), "intent": "procedural"})
    assert tuned["passage_chars"] == INTENT_PROFILES["procedural"]["passage_chars"]
    assert tuned["max_context_passages"] == INTENT_PROFILES["procedural"]["max_context_passages"]

    # A caller who named the key keeps it.
    pinned = resolve_options(
        {
            "options": {**base, "passage_chars": 123},
            "intent": "procedural",
            "explicit_options": ["passage_chars"],
        }
    )
    assert pinned["passage_chars"] == 123

    # ...and a requested result count is never reduced by a profile.
    floored = resolve_options(
        {"options": {**base, "min_context_passages": 20}, "intent": "procedural"}
    )
    assert floored["max_context_passages"] == 20


def test_the_two_intents_actually_retrieve_differently() -> None:
    pytest.importorskip("langgraph")
    from pheasant.assistant.workflows.agentic import INTENT_PROFILES

    knowledge = INTENT_PROFILES["knowledge"]
    procedural = INTENT_PROFILES["procedural"]

    # Breadth vs depth: a summary reads more files, a how-to reads further.
    assert knowledge["max_context_passages"] > procedural["max_context_passages"]
    assert procedural["passage_chars"] > knowledge["passage_chars"]


def test_the_single_pass_workflow_reads_files_too(tmp_path) -> None:
    """Fewer model calls is the trade; a starved prompt is not."""
    store = _store(tmp_path)
    _add_file(
        store,
        "file:demo:docs/guide.md",
        "docs/guide.md",
        [("Guide", "the full body of the guide")],
    )
    llm = _RecordingLLM([("research assistant", "Answer [1].")])

    result = SimpleWorkflow().run(
        WorkflowRequest(question="How do I use the guide?"), _agentic_retriever(store), llm
    )

    assert result.counts["intent"] == "procedural"
    assert "the full body of the guide" in llm.prompt
    assert any("PROCEDURAL" in system for system in llm.systems)
    assert "read" in [step.name for step in result.steps]


# ------------------------------------------------ metadata for the retrieval loop


def test_metadata_describes_files_without_reading_them(tmp_path) -> None:
    store = _store(tmp_path)
    _add_file(
        store,
        "file:demo:app/service.py",
        "app/service.py",
        [("", "def start(): ...\ndef stop(): ..."), ("", "more code")],
        language="python",
        symbols=["start", "stop"],
    )

    seen: list[str] = []
    original = store.rows
    store.rows = lambda sql, params=(): (seen.append(sql), original(sql, params))[1]  # type: ignore[assignment]

    meta = _retriever(store).metadata(["file:demo:app/service.py"])["file:demo:app/service.py"]

    assert meta["type"] == "file"
    assert meta["language"] == "python"
    assert meta["symbols"] == ["start", "stop"]
    assert meta["chunk_count"] == 2
    assert meta["lines"]
    assert len(seen) == 4, "metadata includes a memory-policy read and three indexed queries"
    assert all("SELECT text" not in sql for sql in seen)
    # The point of it being the cheap half: no chunk text is fetched.
    assert not any("text" in sql.split("FROM")[0].lower() for sql in seen)


def test_the_grader_is_shown_what_kind_of_files_came_back(tmp_path) -> None:
    """ "All docs, no source" is a miss no snippet reveals."""
    pytest.importorskip("langgraph")
    from pheasant.assistant.workflows.agentic import AgenticWorkflow

    store = _store(tmp_path)
    _add_file(
        store,
        "file:demo:docs/guide.md",
        "docs/guide.md",
        [("Guide", "prose about the client")],
        artifact_type="markdown_note",
    )

    graded: dict[str, str] = {}

    class Grading(_RecordingLLM):
        def complete(self, system, prompt, **kwargs):
            if "judge whether" in system:
                graded["system"], graded["prompt"] = system, prompt
                return json.dumps({"sufficient": True})
            return super().complete(system, prompt, **kwargs)

    llm = Grading(
        [
            ("plan retrieval", json.dumps({"queries": ["q"], "modes": ["text"]})),
            ("research assistant", "Answer [1]."),
        ]
    )

    AgenticWorkflow().run(
        WorkflowRequest(question="How do I call the client?"), _agentic_retriever(store), llm
    )

    assert "markdown_note" in graded["prompt"], graded["prompt"]
    assert "chunk(s)" in graded["prompt"]
    assert "Result set:" in graded["prompt"]
    # ...and it was told to use it, under the procedural bar.
    assert "shape" in graded["system"]
    assert "NOT sufficient" in graded["system"], "procedural grading criteria missing"


# ---------------------------------------------------------- structural context


def test_the_planner_is_told_what_the_corpus_actually_is(tmp_path) -> None:
    """The planner's context is structure, not just a row count."""
    store = _store(tmp_path)
    _add_file(
        store,
        "file:demo:python/core/runner.py",
        "python/core/runner.py",
        [("", "def run(): ...")],
        language="python",
        symbols=["run", "Runner"],
    )
    _add_file(
        store,
        "file:demo:docs/overview.md",
        "docs/overview.md",
        # The corpus's vocabulary now comes from the FTS index itself
        # (chunks_vocab), not from a separate concept layer — so the words a
        # planner is shown are literally the words in the documents.
        [("", "checkpointing checkpointing durable resumable workflows")],
        artifact_type="markdown_note",
    )
    store.conn.commit()

    context = _retriever(store).capabilities().as_prompt_context()

    assert "python/ (1)" in context and "docs/ (1)" in context  # layout
    assert "markdown_note" in context  # file types
    assert "Code languages: python" in context
    assert "checkpointing" in context  # the corpus's own vocabulary
    assert "Runner" in context or "run" in context  # symbols it defines
    assert "Indexed files: 2" in context


def test_structure_is_cached_against_the_index_signature(tmp_path) -> None:
    store = _store(tmp_path)
    _add_file(store, "file:demo:a.md", "a.md", [("", "body")])
    retriever = _retriever(store)

    first = retriever.structure(1, 1)
    second = retriever.structure(1, 1)
    assert first is second, "re-derived a fact that only a sync can change"

    # A sync moves the signature, and the shape is derived again.
    _add_file(store, "file:demo:b.md", "b.md", [("", "body")])
    assert retriever.structure(2, 2) is not first


def test_structure_survives_a_region_with_no_state() -> None:
    retriever = PheasantRetriever(search=_FakeSearch(), knowledge_base="kb", state=None)
    context = retriever.capabilities().as_prompt_context()

    assert "Sources:" in context
    assert "Search modes available" in context


def test_directory_rollup_uses_postgres_string_functions() -> None:
    from pheasant.assistant.retrieval import _top_directories_sql

    postgres_state = type(
        "State",
        (),
        {"dialect": type("Dialect", (), {"is_postgres": True})()},
    )()
    sqlite_state = type(
        "State",
        (),
        {"dialect": type("Dialect", (), {"is_postgres": False})()},
    )()

    assert "strpos(" in _top_directories_sql(postgres_state)
    assert "instr(" not in _top_directories_sql(postgres_state)
    assert "instr(" in _top_directories_sql(sqlite_state)
