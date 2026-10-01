"""Dynamic chunking: ``chunking.strategy``, the planner and the packer.

Three promises are held here. ``fixed`` (and the legacy ``semantic``) is
byte-identical to what every source produced before, so no region re-indexes
on upgrade. ``sections``/``auto`` never lose a line, never exceed their
ceiling and are deterministic, so pillar 1 holds per planner version. And the
planner picks the profile a file's type and structure call for, including the
refusals -- a numbered *list* is not a numbered outline.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.ingestion.chunk_plan import (
    PLANNER_VERSION,
    SCAN_HEAD_CHARS,
    SCAN_WINDOW_CHARS,
    SCAN_WINDOWS,
    ChunkPlan,
    heading_allowance,
    plan_chunks,
    sample_spans,
    scan,
    strategy_of,
    structural_rules,
)
from pheasant.ingestion.packing import pack, split_text
from pheasant.ingestion.pipeline import _chunks_and_headings, chunks_for_source
from pheasant.ingestion.taxonomy import MAX_HEADINGS_PER_DOCUMENT, detect_headings
from pheasant.sync.fingerprint import source_fingerprint


def _source(
    strategy: str | None = None,
    *,
    kind: str = "document_folder",
    max_chars: int = 4000,
    overlap: int = 400,
    taxonomy: bool = False,
) -> Any:
    chunking: dict[str, Any] = {"max_chars": max_chars, "overlap_chars": overlap}
    if strategy is not None:
        chunking["strategy"] = strategy
    return PheasantConfig.model_validate(
        {
            "sources": [
                {
                    "name": "s",
                    "type": kind,
                    "path": "/tmp",
                    "chunking": chunking,
                    "taxonomy": {"enabled": taxonomy},
                }
            ]
        }
    ).sources[0]


TARIFF = "\n".join(
    [
        "MISO FERC Electric Tariff",
        "",
        "ARTICLE I DEFINITIONS",
        "",
        *[
            line
            for number in range(1, 9)
            for line in (
                f"1.{number} Defined Term {number}",
                "",
                f"The term {number} means the obligations of the transmission provider "
                "under this tariff, including without limitation settlement. " * 3,
                "",
            )
        ],
        "ARTICLE II SERVICE",
        "",
        *[
            line
            for number in range(1, 6)
            for line in (
                f"2.{number} Service Clause {number}",
                "",
                "Network integration service shall be provided pursuant to this section. " * 60,
                "",
            )
        ],
    ]
)


# -- fixed is the old behaviour ----------------------------------------------


@pytest.mark.parametrize("strategy", [None, "semantic", "fixed", "FIXED"])
@pytest.mark.parametrize("taxonomy", [False, True])
def test_fixed_is_byte_identical_to_the_old_pipeline(strategy: str | None, taxonomy: bool) -> None:
    source = _source(strategy, taxonomy=taxonomy)
    chunks, headings, plan = _chunks_and_headings(source, TARIFF, "tariff.pdf")
    from pheasant.ingestion.taxonomy import headings_for_source

    expected_headings = headings_for_source(source, TARIFF)
    assert plan is None
    assert headings == expected_headings
    assert chunks == chunks_for_source(source, TARIFF, expected_headings)


def test_fixed_keeps_every_stored_fingerprint() -> None:
    baseline = source_fingerprint(_source())  # the default, `semantic`
    assert source_fingerprint(_source("semantic")) == baseline
    assert source_fingerprint(_source("fixed")) == baseline
    # A value nothing ever read is still fingerprinted as written.
    assert source_fingerprint(_source("paragraphs")) != baseline
    assert strategy_of(_source("paragraphs")) == "fixed"


def test_auto_and_sections_carry_the_planner_version() -> None:
    import json

    from pheasant.sync import fingerprint

    captured: list[dict[str, Any]] = []
    original = fingerprint._digest
    try:
        fingerprint._digest = lambda payload: captured.append(payload) or original(payload)  # type: ignore[assignment]
        for strategy in ("auto", "sections", "fixed"):
            source_fingerprint(_source(strategy))
    finally:
        fingerprint._digest = original  # type: ignore[assignment]
    auto, sections, fixed = captured
    assert auto["chunk_planner"] == PLANNER_VERSION
    assert sections["chunk_planner"] == PLANNER_VERSION
    assert "chunk_planner" not in fixed
    assert json.dumps(auto, sort_keys=True) != json.dumps(fixed, sort_keys=True)


def test_the_region_override_moves_every_source() -> None:
    config = PheasantConfig.model_validate(
        {
            "sync": {"source_processing": {"chunk_strategy": "auto"}},
            "sources": [{"name": "s", "type": "document_folder", "path": "/tmp"}],
        }
    )
    assert strategy_of(config.effective_source(config.sources[0])) == "auto"


# -- the packer's invariants -------------------------------------------------


def _random_document(rng: random.Random) -> str:
    lines: list[str] = []
    for section in range(rng.randint(0, 12)):
        if rng.random() < 0.7:
            lines += [f"{section + 1}.{rng.randint(1, 3)} Heading {section}", ""]
        for _ in range(rng.randint(0, 4)):
            words = rng.randint(1, 400)
            lines.append(" ".join(rng.choice(["alpha", "beta", "gamma."]) for _ in range(words)))
            if rng.random() < 0.6:
                lines.append("")
    return "\n".join(lines)


@pytest.mark.parametrize("boundary", ["headings", "paragraphs", "code", "messages", "whole"])
def test_packing_never_loses_a_line_or_passes_its_ceiling(boundary: str) -> None:
    rng = random.Random(boundary)
    for _ in range(150):
        text = _random_document(rng)
        target = rng.choice([200, 600, 1500])
        plan = ChunkPlan(
            profile="t",
            boundary=boundary,
            target_chars=target,
            min_chars=target // 3,
            max_chars=target * 2,
            overlap_chars=rng.choice([0, 50]),
            rules=("numbered",),
        )
        headings = detect_headings(text, rules=("numbered",))
        chunks = pack(text, plan, headings)
        lines = text.splitlines()
        covered: set[int] = set()
        for chunk in chunks:
            assert len(chunk.text) <= plan.max_chars
            assert chunk.text.strip()
            assert 1 <= chunk.start_line <= chunk.end_line <= max(1, len(lines))
            covered.update(range(chunk.start_line, chunk.end_line + 1))
        missing = [n for n, line in enumerate(lines, 1) if line.strip() and n not in covered]
        assert not missing, (text, plan)
        assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
        assert pack(text, plan, headings) == chunks


def test_small_sections_are_merged_under_their_common_heading() -> None:
    source = _source("auto")
    chunks, headings, plan = _chunks_and_headings(source, TARIFF, "tariff.pdf")
    assert plan is not None and plan["profile"] == "structured"
    assert {"keyword", "numbered"} <= set(plan["rules"])
    assert len(headings) == 15
    # Eight short definitions do not become eight chunks...
    definitions = [chunk for chunk in chunks if "Defined Term" in chunk.text]
    assert len(definitions) < 8
    # ...and a chunk holding several names every one of them under the heading
    # they share, so the `section` criterion still finds each by number.
    from pheasant.search.sqlite_store import section_matches

    merged = [chunk for chunk in definitions if chunk.text.count("Defined Term") > 1]
    assert merged
    for chunk in merged:
        label = chunk.heading_path or ""
        assert label.lower().startswith("article i definitions > ")
        for number in range(1, 9):
            if f"1.{number} Defined Term {number}\n" in chunk.text + "\n":
                assert section_matches(label, f"1.{number}"), (number, label)
    # A long clause is split, and its pieces keep the clause's own label.
    clause = [
        chunk for chunk in chunks if (chunk.heading_path or "").endswith("2.1 Service Clause 1")
    ]
    assert len(clause) >= 2


def test_overlap_stays_inside_a_split_and_never_crosses_sections() -> None:
    plan = ChunkPlan("t", "headings", 400, 100, 400, 80, rules=("numbered",))
    text = "1.1 First\n\n" + "alpha beta gamma. " * 60 + "\n\n1.2 Second\n\nshort tail.\n"
    headings = detect_headings(text, rules=("numbered",))
    chunks = pack(text, plan, headings)
    first = [chunk for chunk in chunks if chunk.heading_path and "First" in chunk.heading_path]
    second = [chunk for chunk in chunks if chunk.heading_path and "Second" in chunk.heading_path]
    assert len(first) >= 2 and len(second) == 1
    assert "alpha" not in second[0].text
    assert any(
        a.text[-20:].split()[-1] in b.text[:120] for a, b in zip(first, first[1:], strict=False)
    )


def test_split_text_prefers_paragraphs_then_lines_then_sentences() -> None:
    text = ("word " * 30).strip() + "\n\n" + ("next " * 30).strip()
    pieces = split_text(text, 200, 0)
    assert pieces[0][0].endswith("word") and pieces[1][0].startswith("next")
    assert [(a, b) for _, a, b in pieces] == [(1, 1), (3, 3)]


# -- the planner's choices ---------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "path", "profile", "boundary"),
    [
        ("repository", "src/app.py", "code", "code"),
        ("repository", "deploy/values.yaml", "config", "paragraphs"),
        ("markdown_folder", "notes/guide.md", "markdown", "headings"),
        ("obsidian_vault", "daily/2026-10-01.txt", "markdown", "headings"),
        ("memory", "records/a.md", "memory", "whole"),
        ("document_folder", "budget.xlsx", "tabular", "rows"),
        ("slack", "general-c0123.md", "messages", "messages"),
    ],
)
def test_the_source_type_and_extension_pick_a_profile_for_free(
    kind: str, path: str, profile: str, boundary: str
) -> None:
    plan = plan_chunks(_source("auto", kind=kind), path, "anything\n")
    assert (plan.profile, plan.boundary) == (profile, boundary)


def test_configured_limits_are_ceilings() -> None:
    plan = plan_chunks(_source("auto", max_chars=1000, overlap=40), "tariff.pdf", TARIFF)
    assert plan.max_chars <= 1000 and plan.target_chars <= 1000 and plan.overlap_chars <= 40


def test_prose_without_headings_is_packed_by_paragraph() -> None:
    prose = "\n\n".join("A plain paragraph about cranes and falcons. " * 8 for _ in range(30))
    plan = plan_chunks(_source("auto"), "essay.txt", prose)
    assert (plan.profile, plan.boundary, plan.rules) == ("prose", "paragraphs", ())


def test_a_numbered_list_is_not_an_outline() -> None:
    shopping = "Things to do\n\n" + "\n".join(f"{n}. Buy item {n}" for n in range(1, 12))
    assert "numbered" not in structural_rules(scan(shopping))
    outline = "\n\n".join(
        f"{n}.{m} Part {n} clause {m}\n\nBody text." for n in (1, 2) for m in (1, 2, 3)
    )
    assert "numbered" in structural_rules(scan(outline))


def test_all_caps_is_used_only_when_nothing_else_is() -> None:
    caps = "\n\n".join(
        f"SECTION TITLE {chr(65 + n)}\n\n" + "body text here. " * 20 for n in range(6)
    )
    assert structural_rules(scan(caps)) in {("caps",), ("keyword",)}
    markdown = "# One\n\nBODY IN CAPS LINE\n\n# Two\n\ntext\n"
    assert "caps" not in structural_rules(scan(markdown))


def test_an_explicit_detect_list_narrows_what_auto_may_use() -> None:
    source = _source("auto")
    source.taxonomy.detect = ["keyword"]
    plan = plan_chunks(source, "tariff.pdf", TARIFF)
    assert plan.rules == ("keyword",)


def test_a_slack_transcript_is_recognised_by_its_shape() -> None:
    lines = ["# #general", ""] + [f"**U{n}** (17000000{n}.0001): message {n}" for n in range(20)]
    plan = plan_chunks(_source("auto"), "export.txt", "\n".join(lines))
    assert plan.profile == "messages"
    chunks = pack("\n".join(lines), plan)
    assert all(chunk.text.splitlines()[-1].startswith("**U") for chunk in chunks)


def test_a_spreadsheet_chunk_carries_its_sheet_and_header() -> None:
    rows = ["Q3 Forecast", "Region\tLoad\tPrice"] + [
        f"Zone {n}\t{n * 10}\t{n * 3}" for n in range(400)
    ]
    plan = plan_chunks(_source("auto"), "budget.xlsx", "\n".join(rows))
    chunks = pack("\n".join(rows), plan)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.text.startswith("Q3 Forecast\nRegion\tLoad\tPrice")
        assert chunk.heading_path == "Q3 Forecast"
    assert chunks[0].start_line == 1 and chunks[-1].end_line == len(rows)


def test_the_scan_reads_a_bounded_sample_of_any_length() -> None:
    for length in (10, 160 * 1024, 10**7, 10**9):
        spans = sample_spans(length)
        read = sum(stop - start for start, stop in spans)
        assert read <= SCAN_HEAD_CHARS + SCAN_WINDOWS * SCAN_WINDOW_CHARS + 160 * 1024
        assert all(0 <= start < stop <= length for start, stop in spans) or length == 0


def test_the_heading_cap_scales_only_for_the_planner() -> None:
    assert heading_allowance("x" * 1000) == MAX_HEADINGS_PER_DOCUMENT
    assert heading_allowance("x" * 41_000_000) == 20_500
    # `fixed` keeps the historical cap, so its output does not move.
    many = "\n\n".join(f"{n}.1 Clause {n}\n\nbody" for n in range(1, 2600))
    from pheasant.ingestion.taxonomy import headings_for_source

    assert len(headings_for_source(_source(taxonomy=True), many)) == MAX_HEADINGS_PER_DOCUMENT


def test_heading_nesting_work_per_heading_is_bounded() -> None:
    """The prefix match scanned every earlier heading: quadratic in a document
    whose numbers rarely have their parent present (9.4 s at 10,720 headings)."""

    text = "\n\n".join(f"Section {n}.{n % 7 + 1}.2 Clause\n\nbody" for n in range(1, 3001))
    executed = 0

    def trace(frame, event, arg):  # noqa: ARG001
        nonlocal executed
        if event == "line" and frame.f_code.co_name == "_prefix_parent":
            executed += 1
        return trace

    sys.settrace(trace)
    try:
        headings = detect_headings(text, max_headings=10_000)
    finally:
        sys.settrace(None)
    assert len(headings) == 3000
    assert executed <= 10 * len(headings), executed


def test_a_chunk_plan_survives_the_worker_wire() -> None:
    from pheasant.ingestion.pipeline import ParsedArtifact
    from pheasant.sync.remote_worker import (
        IncompatibleResult,
        parsed_from_wire,
        parsed_to_wire,
    )

    chunks, headings, plan = _chunks_and_headings(_source("auto"), TARIFF, "tariff.pdf")
    artifact = ParsedArtifact(
        id="file:s:tariff.pdf:branch=none",
        source_id="s",
        path="/tmp/tariff.pdf",
        relative_path="tariff.pdf",
        type="document",
        mime_type=None,
        size_bytes=1,
        sha256="0" * 64,
        mtime="2026-10-01T00:00:00Z",
        git_branch=None,
        git_commit=None,
        chunks=chunks,
        headings=headings,
        chunk_plan=plan,
    )
    import json

    wire = json.loads(json.dumps(parsed_to_wire(artifact)))
    assert parsed_from_wire(wire) == artifact
    wire["chunk_plan"]["planner"] = "chunk-plan-v0"
    with pytest.raises(IncompatibleResult):
        parsed_from_wire(wire)


def test_a_previewed_plan_is_printed_without_indexing(tmp_path: Path, capsys: Any) -> None:
    import json

    from pheasant.ingestion.chunk_plan import main

    path = tmp_path / "tariff.txt"
    path.write_text(TARIFF, encoding="utf-8")
    assert main([str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["plan"]["profile"] == "structured"
    assert report["chunks"] == len(_chunks_and_headings(_source("auto"), TARIFF, "t.txt")[0])
