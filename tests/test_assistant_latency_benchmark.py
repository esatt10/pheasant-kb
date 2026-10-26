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


def test_arms_run_every_case_with_the_arm_options_in_a_shuffled_order(tmp_path) -> None:
    from scripts.benchmark_assistant_latency import _arm_jobs, _load_arms, _with_arm

    arms_file = tmp_path / "arms.json"
    arms_file.write_text(
        json.dumps(
            {
                "baseline": "base",
                "arms": [
                    {"name": "base", "options": {}},
                    {"name": "cheap", "options": {"repair_efforts": ["low"]}},
                ],
            }
        )
    )
    baseline, arms = _load_arms(arms_file)
    cases = [{"id": f"q{n}", "options": {"decisions": "beta"}} for n in range(8)]
    jobs = _arm_jobs(cases, arms, repeats=2, seed=1)

    assert baseline == "base" and len(jobs) == 8 * 2 * 2
    per_case = [tuple(arm["name"] for _, _, arm in jobs[i : i + 2]) for i in range(0, 32, 2)]
    assert all(sorted(pair) == ["base", "cheap"] for pair in per_case)
    assert len(set(per_case)) == 2, "the order is shuffled, not fixed"
    assert jobs == _arm_jobs(cases, arms, repeats=2, seed=1), "and seeded"
    merged = _with_arm(cases[0], arms[1])
    assert merged["options"] == {"decisions": "beta", "repair_efforts": ["low"]}
    assert cases[0]["options"] == {"decisions": "beta"}, "the case is not mutated"

    arms_file.write_text(json.dumps({"baseline": "nope", "arms": [{"name": "base"}]}))
    with pytest.raises(ValueError):
        _load_arms(arms_file)


def test_a_row_reports_what_the_acceptance_gate_did_and_the_time_spent_writing() -> None:
    from scripts.benchmark_assistant_latency import _answer_writing

    answer = {
        "decisions": {
            "accepted": True,
            "repairs": 1,
            "draft_effort_used": "low",
            "repair_efforts": ["low"],
            "max_repairs": 2,
        },
        "steps": [
            {"name": "history_rewrite", "duration_seconds": 1.5},
            {"name": "synthesize", "duration_seconds": 5.0, "passages": 8},
            {"name": "accept", "duration_seconds": 0.3},
            {"name": "synthesize", "duration_seconds": 4.0, "passages": 8},
        ],
    }
    row = _answer_writing(answer)
    assert (row["draft_seconds"], row["rewrite_seconds"]) == (5.0, 4.0)
    assert row["history_rewrite_seconds"] == 1.5 and row["synthesis_passages"] == 8
    assert (row["repair_efforts_ran"], row["max_repairs_ran"]) == (["low"], 2)
    assert _answer_writing({})["rewrite_seconds"] is None


def test_arms_are_compared_with_the_baseline_case_by_case() -> None:
    from scripts.benchmark_assistant_latency import _arms_summary

    def row(case, arm, ms, coverage, *, repairs=0, accepted=True):
        return {
            "case_id": case,
            "arm": arm,
            "client_completed_answer_ms": ms,
            "quality_proxy": {"expected_fact_coverage": coverage},
            "repairs": repairs,
            "accepted": accepted,
        }

    rows = [
        row("slow", "base", 20_000.0, 1.0, repairs=1),
        row("slow", "cheap", 12_000.0, 1.0, repairs=1),
        row("fast", "base", 6_000.0, 1.0),
        row("fast", "cheap", 5_500.0, 0.5, accepted=False),
    ]
    result = _arms_summary(rows, "base", ["base", "cheap"])

    paired = result["against_baseline"]["cheap"]
    assert paired["cases"] == 2 and paired["cases_faster"] == 2
    assert paired["answer_ms_delta_mean"] == pytest.approx(-4_250.0)
    assert paired["cases_with_less_coverage"] == ["fast"], "a faster arm that lost a fact says so"
    cheap = result["arms"]["cheap"]
    assert cheap["acceptance_rate"] == 0.5 and cheap["repair_rate"] == 0.5
    assert result["arms"]["base"]["completed_answer_p90_ms"] == 20_000.0
