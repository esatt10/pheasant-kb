"""An excerpted document is searched for the question inside itself.

Found on production: asked what a HubSpot article on keywords says about
search intent, the answer said the page did not cover it, and the acceptance
gate agreed. The page covers it under its own heading. Search cites one chunk
per file, chosen by how the whole corpus scores the question; on a page whose
every chunk says "keywords" it cited the chunk on keyword difficulty, and the
reader — excerpting a 356 KB page to that chunk and its neighbours — never
read the section three chunks earlier. ``question_anchors`` ranks the file's
own chunks with the file as the collection, so the term every chunk holds
drops out and the one the question is about decides.
"""

from __future__ import annotations

import pytest

from pheasant.assistant.chat import hydrate_citations
from pheasant.assistant.document_assembly import (
    _chunk_label,
    question_anchors,
    question_terms,
)
from pheasant.assistant.retrieval import LARGE_FILE_BYTES
from pheasant.search.text_store import QUERY_STOPWORDS
from tests.test_answer_grounding import _add_file, _citation, _retriever, _store

NODE = "file:demo:blog/keywords.txt"
QUESTION = "What does the HubSpot article on keywords say about search intent?"


def _keywords_page(store) -> None:
    """Sixteen chunks that all say "keywords"; the section asked about is #3."""
    chunks = []
    for index in range(16):
        body = f"Part {index}. Choosing keywords takes research, and keywords drive traffic. "
        if index == 3:
            body += (
                "The 3 Common Types of Search Intent\nBehind every keyword is the reason the "
                "user makes the query. This is known as\nsearch intent\nwhich falls into three "
                "categories: informational, navigational and transactional. "
            )
        if index == 8:
            body += "Keyword difficulty measures how hard a keyword is to rank for. "
        chunks.append(("", body + "filler " * 30))
    _add_file(store, NODE, "blog/keywords.txt", chunks, size_bytes=LARGE_FILE_BYTES * 9)


def _read(store, question=None, **kwargs):
    return _retriever(store).documents(
        [NODE], anchors={NODE: [f"{NODE}#c8"]}, question=question, **kwargs
    )[NODE]


def test_the_question_keeps_its_subject_as_a_phrase() -> None:
    terms = question_terms(QUESTION, QUERY_STOPWORDS)
    # "search" is a framing verb to ranking, and part of the subject here.
    assert terms[0] == "search intent"
    # Words that frame a question about a document name nothing in it.
    assert "article" not in terms and "say" not in terms
    assert "keywords" in terms and "intent" in terms


def test_a_term_every_chunk_holds_does_not_choose() -> None:
    # Term 0 is in every chunk, term 1 in chunk 2 only.
    counts = [[3, 0], [5, 0], [1, 2], [4, 0]]
    assert question_anchors(counts, [100] * 4, exclude=set()) == [2]
    # Only the ubiquitous term matches: nothing is worth adding.
    assert question_anchors([[1, 0]] * 4, [100] * 4, exclude=set()) == []


def test_a_chunk_already_cited_is_not_added_again() -> None:
    counts = [[0], [2], [0], [0]]
    assert question_anchors(counts, [100] * 4, exclude={1}) == []


def test_the_excerpt_reads_the_section_the_question_asks_about(tmp_path) -> None:
    store = _store(tmp_path)
    _keywords_page(store)

    before = _read(store)
    after = _read(store, QUESTION)

    # Without the question: chunk 0 and the cited chunk's neighbourhood only,
    # which is what production answered from.
    assert "Search Intent" not in before.text
    assert "Keyword difficulty" in before.text
    assert before.question_chunks == 0
    # With it: the cited chunk is still read, and so is the section.
    assert "The 3 Common Types of Search Intent" in after.text
    assert "Keyword difficulty" in after.text
    assert after.question_chunks == 1
    assert after.truncated is True  # still an excerpt, and still says so
    assert "chunk(s) omitted" in after.text


def test_no_question_leaves_the_excerpt_exactly_as_it_was(tmp_path) -> None:
    store = _store(tmp_path)
    _keywords_page(store)
    assert _read(store).text == _read(store, "").text


def _head_and_cited(store) -> int:
    """Characters that hold exactly chunk 0 and the cited chunk 8 (labels too)."""
    rows = store.rows(
        "SELECT start_line, end_line, heading_path, LENGTH(text) AS n FROM chunks "
        "WHERE artifact_id = ? AND chunk_index IN (0, 8)",
        (NODE,),
    )
    return sum(len(_chunk_label(row)) + int(row["n"]) + 2 for row in rows)


def test_the_question_chunk_is_read_when_the_allowance_was_already_spent(tmp_path) -> None:
    """Production's arithmetic: 5,000 characters held the page's head and the
    cited chunk, and the section the question found did not fit beside them.
    A found passage brings its own room, as a cited one does."""
    store = _store(tmp_path)
    _keywords_page(store)
    allowance = _head_and_cited(store)

    before = _read(store, max_chars=allowance)
    after = _read(store, QUESTION, max_chars=allowance)

    assert "Search Intent" not in before.text  # the allowance is spent
    assert "The 3 Common Types of Search Intent" in after.text
    assert "Keyword difficulty" in after.text and "Part 0." in after.text
    # The room is the found chunk's, not the neighbours'.
    assert "Part 7." not in after.text and "Part 9." not in after.text
    assert "Part 2." not in after.text and "Part 4." not in after.text


def test_the_found_chunk_stays_inside_the_batch_budget(tmp_path) -> None:
    store = _store(tmp_path)
    _keywords_page(store)
    allowance = _head_and_cited(store)

    document = _read(store, QUESTION, max_chars=allowance, budget_chars=allowance)

    # No budget is left to grow into: the section is not read, and what is
    # read is still the head and the chunk search cited.
    assert "Search Intent" not in document.text
    assert "Part 0." in document.text and "Keyword difficulty" in document.text


def test_a_file_read_whole_runs_no_ranking_statement(tmp_path) -> None:
    store = _store(tmp_path)
    _add_file(store, NODE, "blog/keywords.txt", [("", "Search intent, in short.")] * 3)
    seen: list[str] = []
    original = store.rows
    store.rows = lambda sql, params=(): (seen.append(sql), original(sql, params))[1]  # type: ignore[assignment]

    document = _read(store, QUESTION)

    assert document.truncated is False
    assert not any("REPLACE(LOWER(text)" in sql for sql in seen)


def test_code_is_never_ranked_inside(tmp_path) -> None:
    """Code is never excerpted, so there is nothing for the question to add."""
    store = _store(tmp_path)
    _add_file(
        store,
        "file:demo:app/intent.py",
        "app/intent.py",
        [("", f"def search_intent_{i}():\n    return {i}") for i in range(40)],
        size_bytes=LARGE_FILE_BYTES * 3,
        language="python",
    )
    document = _retriever(store).documents(["file:demo:app/intent.py"], question=QUESTION)[
        "file:demo:app/intent.py"
    ]
    assert document.question_chunks == 0 and document.truncated is False


@pytest.mark.parametrize("question", [QUESTION, None])
def test_hydration_hands_the_question_to_the_reader(tmp_path, question) -> None:
    store = _store(tmp_path)
    _keywords_page(store)
    citation = _citation(1, NODE, "blog/keywords.txt", chunk_id=f"{NODE}#c8")

    documents = hydrate_citations(_retriever(store), [citation], question=question)

    assert ("Search Intent" in documents[1].text) is (question is not None)
