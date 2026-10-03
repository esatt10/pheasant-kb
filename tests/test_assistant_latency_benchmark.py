from __future__ import annotations

import io
import json

import pytest
from scripts.benchmark_assistant_latency import (
    _clustered_ci,
    _load_cases,
    _request_once,
    _summarize,
)


def test_sse_benchmark_times_answer_text_separately_from_progress(monkeypatch) -> None:
    from scripts import benchmark_assistant_latency as benchmark

    class Response(io.BytesIO):
        status = 200

    events = [
        {"type": "step", "name": "retrieve"},
        {"type": "draft", "delta": " "},
        {"type": "draft", "delta": "Grounded"},
        {"type": "answer", "answer": {"answer": "Grounded [1]", "citations": []}},
    ]
    payload = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
    monkeypatch.setattr(benchmark, "_post_json", lambda *_args, **_kwargs: Response(payload))

    row = _request_once(
        base_url="http://example.test",
        token="test-token",
        timeout=5,
        transport="sse",
        case={"id": "one", "question": "Question?", "complexity": "moderate"},
    )

    assert row["first_progress_ms"] is not None
    assert row["first_answer_text_ms"] is not None
    assert row["first_answer_text_ms"] >= row["first_progress_ms"]
    assert row["client_completed_answer_ms"] >= row["first_answer_text_ms"]
    summary = _summarize([row], wall_seconds=1.0)
    assert summary["first_answer_text_rate"] == 1.0
    assert summary["first_answer_text_p95_ms"] == row["first_answer_text_ms"]


def test_case_manifest_requires_independent_quality_labels(tmp_path) -> None:
    manifest = tmp_path / "cases.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "id": "moderate-1",
                    "complexity": "moderate",
                    "answer_length": "short",
                    "question": "What is in the corpus?",
                    "expected_facts": ["indexed documents"],
                    "acceptable_passage_ids": ["chunk:docs/overview#c0"],
                }
            ]
        ),
        encoding="utf-8",
    )

    assert _load_cases(manifest)[0]["id"] == "moderate-1"

    manifest.write_text(
        json.dumps(
            [
                {
                    "id": "missing-labels",
                    "complexity": "moderate",
                    "answer_length": "short",
                    "question": "What is in the corpus?",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="expected_facts"):
        _load_cases(manifest)


def test_fast_extractives_and_incomplete_answers_do_not_enter_success_latency() -> None:
    rows = [
        {
            "case_id": "a",
            "complexity": "moderate",
            "case_labels": {"answerable": True},
            "client_completed_answer_ms": 10.0,
            "client_request_end_ms": 10.0,
            "citation_count": 1,
            "degraded": True,
            "answer_mode": "extractive",
            "termination_reason": "extractive_fallback",
        },
        {
            "case_id": "b",
            "complexity": "moderate",
            "case_labels": {"answerable": True},
            "client_completed_answer_ms": 700.0,
            "client_request_end_ms": 700.0,
            "citation_count": 1,
            "degraded": False,
            "answer_mode": "llm",
            "termination_reason": "completed",
            "query_embedding": {"fresh_misses": 1, "provider_requests": 1},
        },
        {
            "case_id": "c",
            "complexity": "complex",
            "case_labels": {"answerable": True},
            "client_completed_answer_ms": None,
            "client_request_end_ms": 3000.0,
            "error": "timeout",
            "timeout": True,
        },
    ]

    result = _summarize(rows, wall_seconds=3.0)

    assert result["completion_rate"] == pytest.approx(2 / 3)
    assert result["completed_answer_p95_ms"] == 700.0
    assert result["eligible_quality_ungraded_p95_ms"] == 700.0
    assert result["complexity"]["moderate"]["distinct_questions"] == 2
    assert result["query_embedding_cache"]["fresh_miss"]["requests"] == 1
    assert result["timeouts"] == 1


def test_confidence_interval_resamples_distinct_questions_as_clusters() -> None:
    assert (
        _clustered_ci(
            [{"case_id": "one", "latency": 1.0}, {"case_id": "one", "latency": 2.0}],
            "latency",
        )
        is None
    )

    interval = _clustered_ci(
        [
            {"case_id": "one", "latency": 1.0},
            {"case_id": "one", "latency": 2.0},
            {"case_id": "two", "latency": 3.0},
            {"case_id": "two", "latency": 4.0},
        ],
        "latency",
    )
    assert interval is not None and len(interval) == 2
