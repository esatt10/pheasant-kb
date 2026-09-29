"""Long answers: an outline, then each section from only its own passages.

One prompt holding two dozen whole files and asked for a report produces a
worse report than the same model writing five sections from five small piles
of evidence — long contexts lose the middle, and a single call makes the
reader wait for the whole thing before seeing any of it. So a ``long`` answer
is three steps:

1. **Outline** (one call). The model reads short previews of every passage and
   returns a two-or-three sentence direct answer plus up to ``max_sections``
   headings, each naming the passage numbers it will draw on.
2. **Fill** (one call per section, in parallel up to ``section_concurrency``).
   Each section sees only its own passages — hydrated in full — under their
   *original* numbers, so the ``[n]`` markers it writes index the one citation
   list the answer ships with and ``verify_node`` checks them unchanged.
3. **Stitch**. Deterministic: the overview, then each section under a
   ``###`` heading, in outline order.

It degrades rather than fails. No usable outline: passages are grouped by
directory instead. A section that errors or misses the deadline is written
extractively from its passages, and the step list says which.
"""

from __future__ import annotations

import contextvars
import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import PurePosixPath
from typing import Any

from pheasant.assistant.chat import build_prompt, system_prompt_for
from pheasant.assistant.workflows import WorkflowStep

OUTLINE_SYSTEM = """You plan a long, sectioned answer from retrieved passages.

Reply with JSON only, no prose:
{"overview": "two or three sentences that answer the question directly, \
citing passages as [n]",
 "sections": [{"heading": "short heading", "passages": [1, 4]}]}

Rules:
- 3 to {max_sections} sections, ordered so the answer reads top to bottom.
- Every section lists the passage numbers it will draw on. Use only numbers \
you were given; a passage may serve more than one section.
- Headings name the real components, steps or themes the passages show."""

SECTION_RULES = """

You are writing ONE section of a longer answer. Write only the body of the \
section titled below — no heading, no introduction to the whole answer, no \
conclusion for it. Cite the passages you use with their [n] numbers exactly as \
shown. 120 to 350 words."""


def outline_prompt(question: str, citations: list[dict], history_text: str = "") -> str:
    lines = [history_text] if history_text else []
    lines.append("Passages:")
    for citation in citations:
        where = citation.get("relative_path") or citation.get("title") or ""
        preview = " ".join(str(citation.get("snippet") or "").split())[:300]
        lines.append(f"[{citation['index']}] {where}: {preview}")
    lines.append("")
    lines.append(f"Question: {question}")
    return "\n".join(lines)


def plan_sections(raw: str | None, citations: list[dict], max_sections: int) -> dict[str, Any]:
    """Validate the outline, or fall back to grouping by directory."""

    valid = {int(c["index"]) for c in citations}
    parsed = _parse_json(raw) or {}
    sections: list[dict[str, Any]] = []
    for entry in parsed.get("sections") or []:
        if not isinstance(entry, dict):
            continue
        heading = " ".join(str(entry.get("heading") or "").split())[:120]
        numbers = []
        for item in entry.get("passages") or []:
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            if number in valid and number not in numbers:
                numbers.append(number)
        if heading and numbers:
            sections.append({"heading": heading, "passages": numbers})
        if len(sections) >= max_sections:
            break
    overview = " ".join(str(parsed.get("overview") or "").split())
    if len(sections) >= 2:
        return {"overview": overview, "sections": sections, "planned_by": "model"}
    return {
        "overview": overview,
        "sections": group_by_location(citations, max_sections),
        "planned_by": "location",
    }


def group_by_location(citations: list[dict], max_sections: int) -> list[dict[str, Any]]:
    """Sections from where the passages live: deterministic and model-free."""

    groups: dict[str, list[int]] = {}
    for citation in citations:
        path = str(citation.get("relative_path") or "")
        parent = str(PurePosixPath(path).parent) if path else ""
        key = parent if parent not in {"", "."} else (citation.get("source_id") or "General")
        groups.setdefault(str(key), []).append(int(citation["index"]))
    ordered = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    sections = [{"heading": name, "passages": numbers} for name, numbers in ordered]
    if len(sections) > max_sections:
        head, tail = sections[: max_sections - 1], sections[max_sections - 1 :]
        head.append({"heading": "Other", "passages": [n for s in tail for n in s["passages"]]})
        sections = head
    return sections


def write_sections(
    *,
    outline: dict[str, Any],
    citations: list[dict],
    write: Callable[[dict[str, Any], list[dict]], str],
    fallback: Callable[[list[dict]], str],
    concurrency: int,
    deadline_seconds: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fill every section; return ``(sections, degraded_headings)``.

    ``write(section, its_citations)`` is the model call and may raise;
    ``fallback(its_citations)`` is the extractive text used when it does or
    when the deadline passes first. Calls run under a copy of the caller's
    context so token accounting (a ``ContextVar``) still sees them.
    """

    by_index = {int(c["index"]): c for c in citations}
    jobs = [
        (section, [by_index[n] for n in section["passages"] if n in by_index])
        for section in outline["sections"]
    ]
    results: list[dict[str, Any]] = []
    degraded: list[str] = []
    started = time.monotonic()
    # Not a `with` block: leaving one joins every worker, which would make the
    # deadline a suggestion — the answer would still wait for the slowest call.
    pool = ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(jobs) or 1)))
    try:
        futures = [
            pool.submit(contextvars.copy_context().run, write, section, own)
            for section, own in jobs
        ]
        for (section, own), future in zip(jobs, futures, strict=True):
            remaining = max(0.0, deadline_seconds - (time.monotonic() - started))
            try:
                text = future.result(timeout=remaining).strip()
            except FutureTimeout:
                future.cancel()
                text, reason = fallback(own), "time budget reached"
                degraded.append(f"{section['heading']} ({reason})")
            except Exception as exc:  # a failed section must not fail the answer
                text, reason = fallback(own), short(str(exc))
                degraded.append(f"{section['heading']} ({reason})")
            else:
                if not text:
                    text = fallback(own)
                    degraded.append(f"{section['heading']} (empty reply)")
            results.append({"heading": section["heading"], "text": text})
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results, degraded


def stitch(overview: str, sections: list[dict[str, Any]]) -> str:
    parts = [overview.strip()] if overview.strip() else []
    for section in sections:
        parts.append(f"### {section['heading']}\n\n{section['text'].strip()}")
    return "\n\n".join(parts)


def extractive_section(citations: list[dict]) -> str:
    lines = []
    for citation in citations[:4]:
        snippet = " ".join(str(citation.get("snippet") or "").split())
        if len(snippet) > 300:
            snippet = snippet[:300].rstrip() + "…"
        if snippet:
            lines.append(f"- {snippet} [{citation['index']}]")
    return "\n".join(lines) or "_No passage text was available for this section._"


def short(error: str) -> str:
    first = (error or "error").strip().splitlines()[0]
    return first if len(first) <= 80 else first[:77].rstrip() + "…"


def _parse_json(raw: str | None) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def write_long(
    state: dict,
    ctx: dict,
    options: dict,
    documents: dict,
    history_text: str,
    evidence: dict,
) -> tuple[str, list[WorkflowStep]]:
    """Outline → sections in parallel → stitch. Raises only if the outline call does."""

    llm = ctx["llm"]
    request = ctx.get("request")
    intent = str(state.get("intent") or "knowledge")
    citations, facts, figures = evidence["citations"], evidence["facts"], evidence["figures"]
    max_sections = int(options.get("max_sections") or 5)
    steps: list[WorkflowStep] = []

    started = time.perf_counter()
    raw = llm.complete(
        OUTLINE_SYSTEM.replace("{max_sections}", str(max_sections)),
        outline_prompt(state["question"], citations, history_text),
        max_output_tokens=int(options.get("outline_output_tokens") or 700),
    )
    outline = plan_sections(raw, citations, max_sections)
    steps.append(
        WorkflowStep(
            name="outline",
            detail=f"{len(outline['sections'])} sections, planned by "
            + ("the model" if outline["planned_by"] == "model" else "where the passages live"),
            passages=len(citations),
            duration_seconds=time.perf_counter() - started,
        )
    )
    if request is not None:
        request.report(steps[-1])

    section_system = system_prompt_for(intent, figures=bool(figures)) + SECTION_RULES
    budget = int(options.get("section_output_tokens") or 1200)

    def write(section: dict, own: list[dict]) -> str:
        own_documents = {c["index"]: documents[c["index"]] for c in own if c["index"] in documents}
        return llm.complete(
            section_system,
            build_prompt(
                f"{state['question']}\n\nSection to write: {section['heading']}",
                own,
                [f for f in facts if f.get("subject_id") in {c.get("node_id") for c in own}],
                own_documents,
                history_text=history_text,
                figures=[
                    f for f in figures if set(f.get("cited_in") or []) & {c["index"] for c in own}
                ],
            ),
            max_output_tokens=budget,
        )

    started = time.perf_counter()
    sections, degraded = write_sections(
        outline=outline,
        citations=citations,
        write=write,
        fallback=extractive_section,
        concurrency=int(options.get("section_concurrency") or 4),
        deadline_seconds=float(options.get("deadline_seconds") or 150),
    )
    detail = f"wrote {len(sections)} sections in parallel"
    if degraded:
        detail += f"; {len(degraded)} from passages instead: {', '.join(degraded)}"
    steps.append(
        WorkflowStep(
            name="sections",
            detail=detail,
            passages=len(citations),
            duration_seconds=time.perf_counter() - started,
        )
    )
    overview = outline.get("overview") or ""
    return stitch(overview, sections), steps
