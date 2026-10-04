"""Decide, per file, how it should be chunked: the chunk planner.

`chunking` used to be one shape for everything: fixed ``max_chars`` windows,
or one chunk per section when a source turned taxonomy on. A code file, a
Slack channel, a spreadsheet and an 8,000-page tariff were cut the same way,
and the one global override (`sync.source_processing`) moved all of them at
once. ``chunking.strategy`` existed and nothing read it.

It is read now:

``fixed`` (and the legacy ``semantic``, the default)
    Exactly what every source did before: windows, or section-aligned chunks
    with taxonomy on. Byte-identical, so no region re-indexes on upgrade.
``sections``
    The operator says this source is structured: detect headings with the
    source's taxonomy rules and pack sections to ``chunking.max_chars``.
``auto``
    This module decides, per file, in two layers. The source type and the
    file's extension pick a profile for free (code, configuration, Markdown,
    memory, chat, spreadsheets). Anything else -- PDFs, Word, plain text, web
    pages -- gets a **bounded structural scan**: the first 32 KB and 48 evenly
    spaced 4 KB windows, classified line by line with the taxonomy's own
    heading rules. That is ~3 ms on 41M characters, flat in document length,
    against ~11 s to extract that text. From it the planner turns on only the
    heading rules the document really uses, tells numbered *sections* from
    numbered *lists*, and sizes chunks to the sections it saw.

The plan is a pure function of ``(source config, path, text)`` and
:data:`PLANNER_VERSION`, which is in the source fingerprint whenever the
strategy is not ``fixed`` -- so a re-sync of unchanged content reproduces it
exactly, and changing a rule here re-indexes exactly the sources that use it.
The chosen plan is recorded on the artifact's graph node (``chunk_plan``) so
"why was this file cut this way" has an answer, and
``python -m pheasant.ingestion.chunk_plan FILE [--source NAME]`` prints the plan
and the chunks it would produce without indexing anything.

Configured limits are ceilings: no profile exceeds ``chunking.max_chars`` or
``chunking.overlap_chars``, because those are what an operator sets to fit an
embedding model's input.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pheasant.ingestion.taxonomy import (
    MAX_HEADINGS_PER_DOCUMENT,
    _classify,
    _raw_ordinal,
    parse_ordinal,
    rules_for_source,
)

logger = logging.getLogger(__name__)

#: Folded into the source fingerprint for `sections`/`auto`. Bump it whenever a
#: change here or in `packing.py` would cut an unchanged file differently.
PLANNER_VERSION = "chunk-plan-v1"

STRATEGIES = ("fixed", "sections", "auto")
_ALIASES = {"semantic": "fixed", "": "fixed"}

CODE_SUFFIXES = frozenset(
    {
        ".py", ".js", ".jsx", ".ts", ".tsx", ".sh", ".css", ".go", ".rs", ".java",
        ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift",
        ".scala", ".sql", ".lua", ".r", ".m", ".pl", ".ps1",
    }
)  # fmt: skip
CONFIG_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".toml", ".xml", ".ini", ".cfg"})
MARKDOWN_SUFFIXES = frozenset({".md", ".mdx", ".markdown"})
TABULAR_SUFFIXES = frozenset({".xlsx"})

#: Below this the whole text *is* the scan.
SCAN_WHOLE_CHARS = 160 * 1024
SCAN_HEAD_CHARS = 32 * 1024
SCAN_WINDOWS = 48
SCAN_WINDOW_CHARS = 4 * 1024

#: One heading per this many characters, at least the historical cap. A long
#: structured document has more sections than a short one; a fixed 2,000 left
#: most of an 8,000-page tariff labelled with heading two-thousand.
CHARS_PER_HEADING_ALLOWANCE = 2_000


@dataclass(frozen=True)
class ChunkPlan:
    """How one file is chunked. ``boundary`` names the unit `packing` merges."""

    profile: str
    boundary: str  # headings | paragraphs | code | messages | rows | whole
    target_chars: int
    min_chars: int
    max_chars: int
    overlap_chars: int
    rules: tuple[str, ...] = ()
    max_headings: int = MAX_HEADINGS_PER_DOCUMENT
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["rules"] = list(self.rules)
        payload["planner"] = PLANNER_VERSION
        return payload


def strategy_of(source: Any) -> str:
    raw = str(getattr(getattr(source, "chunking", None), "strategy", "") or "").strip().lower()
    strategy = _ALIASES.get(raw, raw)
    if strategy not in STRATEGIES:
        logger.warning(
            "chunking.strategy %r for source %r is not one of %s; using fixed",
            raw,
            getattr(source, "name", "?"),
            ", ".join(STRATEGIES),
        )
        return "fixed"
    return strategy


def heading_allowance(text: str) -> int:
    return max(MAX_HEADINGS_PER_DOCUMENT, len(text) // CHARS_PER_HEADING_ALLOWANCE)


def _ceilings(source: Any) -> tuple[int, int]:
    chunking = getattr(source, "chunking", None)
    return (
        max(200, int(getattr(chunking, "max_chars", 4000) or 4000)),
        max(0, int(getattr(chunking, "overlap_chars", 0) or 0)),
    )


def _sized(
    source: Any,
    profile: str,
    boundary: str,
    target: int,
    maximum: int,
    overlap: int,
    *,
    rules: tuple[str, ...] = (),
    text: str = "",
    reason: str = "",
    minimum: int | None = None,
) -> ChunkPlan:
    ceiling, overlap_ceiling = _ceilings(source)
    maximum = min(maximum, ceiling)
    target = min(target, maximum)
    return ChunkPlan(
        profile=profile,
        boundary=boundary,
        target_chars=target,
        min_chars=min(target, target // 3 if minimum is None else minimum),
        max_chars=maximum,
        overlap_chars=min(overlap, overlap_ceiling, target // 4),
        rules=rules,
        max_headings=heading_allowance(text),
        reason=reason,
    )


def plan_chunks(source: Any, relative_path: str, text: str) -> ChunkPlan:
    """The plan for one file under its source's (non-fixed) strategy."""

    if strategy_of(source) == "sections":
        ceiling, overlap = _ceilings(source)
        return _sized(
            source,
            "sections",
            "headings",
            ceiling,
            ceiling,
            overlap,
            rules=tuple(rules_for_source(source)),
            text=text,
            reason="chunking.strategy: sections",
            minimum=ceiling // 8,
        )
    source_type = str(getattr(getattr(source, "type", None), "value", getattr(source, "type", "")))
    suffix = Path(relative_path).suffix.lower()
    if source_type == "memory":
        return _sized(source, "memory", "whole", 8000, 8000, 0, reason="one record per chunk")
    if suffix in TABULAR_SUFFIXES:
        return _sized(source, "tabular", "rows", 1500, 3000, 0, reason="rows with their header")
    if suffix in CODE_SUFFIXES:
        return _sized(source, "code", "code", 1500, 3000, 0, reason="top-level blocks")
    if suffix in CONFIG_SUFFIXES:
        return _sized(source, "config", "paragraphs", 1500, 3000, 0, reason="blank-line blocks")
    if source_type == "slack":
        return _sized(source, "messages", "messages", 1500, 3000, 0, reason="whole messages")
    if suffix in MARKDOWN_SUFFIXES or source_type == "markdown_folder":
        allowed = _allowed(source, ("markdown",))
        return _sized(
            source,
            "markdown",
            "headings",
            1800,
            3000,
            150,
            rules=allowed,
            text=text,
            reason="Markdown headings",
        )
    return _scanned(source, text)


def _allowed(source: Any, rules: tuple[str, ...]) -> tuple[str, ...]:
    """``rules`` narrowed by an explicit ``taxonomy.detect``, when one is set."""

    configured = list(getattr(getattr(source, "taxonomy", None), "detect", None) or ())
    if not configured:
        return rules
    permitted = set(rules_for_source(source))
    return tuple(rule for rule in rules if rule in permitted)


# -- the structural scan ----------------------------------------------------


@dataclass
class Scan:
    """What the bounded sample says about a document's structure."""

    sampled_chars: int = 0
    lines: int = 0
    blank: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    #: Numbered candidates whose previous non-blank line was also one: a
    #: *list* runs on consecutive lines, sections have text between them.
    numbered_in_runs: int = 0
    multi_part: int = 0
    messages: int = 0


def sample_spans(length: int) -> list[tuple[int, int]]:
    """Character spans the scan reads: all of a short text, a fixed sample of a long one."""

    if length <= SCAN_WHOLE_CHARS:
        return [(0, length)]
    spans = [(0, SCAN_HEAD_CHARS)]
    step = (length - SCAN_HEAD_CHARS) // SCAN_WINDOWS
    for window in range(SCAN_WINDOWS):
        start = SCAN_HEAD_CHARS + window * step
        spans.append((start, min(length, start + SCAN_WINDOW_CHARS)))
    return spans


def scan(text: str) -> Scan:
    from pheasant.ingestion.packing import MESSAGE_LINE

    result = Scan()
    counts = result.counts
    for start, stop in sample_spans(len(text)):
        window = text[start:stop]
        if start:
            # Start on a line boundary, so a window never classifies half a line.
            newline = window.find("\n")
            window = window[newline + 1 :] if newline != -1 else ""
        previous_numbered = False
        for line in window.splitlines():
            result.lines += 1
            if not line.strip():
                result.blank += 1
                continue
            result.sampled_chars += len(line) + 1
            if MESSAGE_LINE.match(line):
                result.messages += 1
            classified = _classify(line)
            if classified is None:
                previous_numbered = False
                continue
            _level, number, _title, kind = classified
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "numbered":
                if previous_numbered:
                    result.numbered_in_runs += 1
                ordinal = parse_ordinal(_raw_ordinal(number, kind), kind)
                if ordinal is not None and len(ordinal.parts) > 1:
                    result.multi_part += 1
            previous_numbered = kind == "numbered"
    return result


def structural_rules(found: Scan) -> tuple[str, ...]:
    """The heading rules this document actually uses.

    Each rule earns its place: ``numbered`` only when its lines are not
    consecutive list items (``1. Buy milk``) and either nest (``4.2``) or sit
    beside another heading convention; ``lettered`` sub-clauses only under a
    numbered or keyword structure; ALL-CAPS -- the noisiest rule -- only when
    nothing else fired and caps lines are rare enough to be headings.
    """

    counts = found.counts
    nonblank = max(1, found.lines - found.blank)
    rules: list[str] = []
    for rule in ("markdown", "keyword", "code"):
        if counts.get(rule, 0) >= 2:
            rules.append(rule)
    numbered = counts.get("numbered", 0)
    if numbered >= 3 and found.numbered_in_runs <= numbered // 2:
        if found.multi_part * 5 >= numbered or {"keyword", "code"} & set(rules):
            rules.append("numbered")
    if counts.get("lettered", 0) >= 2 and {"numbered", "keyword", "code"} & set(rules):
        rules.append("lettered")
    caps = counts.get("caps", 0)
    if not rules and caps >= 3 and caps * 10 <= nonblank:
        rules.append("caps")
    return tuple(rules)


def _scanned(source: Any, text: str) -> ChunkPlan:
    found = scan(text)
    if found.messages >= 3 and found.messages * 2 >= found.lines - found.blank:
        return _sized(source, "messages", "messages", 1500, 3000, 0, reason="chat transcript")
    rules = _allowed(source, structural_rules(found))
    if not rules:
        return _sized(
            source, "prose", "paragraphs", 2000, 3000, 200, text=text, reason="no headings"
        )
    counts = found.counts
    headings = sum(counts.get(rule, 0) for rule in rules)
    section = found.sampled_chars // max(1, headings)
    # Bucketed, so the size is a statement about the document's shape rather
    # than a number that moves with every edit.
    if section < 800:
        target, why = 1200, "short clauses, merged"
    elif section < 1600:
        target, why = 1600, "section-sized"
    else:
        target, why = 2000, "long sections, split at paragraphs"
    return _sized(
        source,
        "structured",
        "headings",
        target,
        3000,
        150,
        rules=rules,
        text=text,
        reason=f"{'+'.join(rules)}; ~{section} chars a section; {why}",
    )


def main(argv: list[str] | None = None) -> int:
    """Print the plan for one file and what it would produce. Indexes nothing."""

    import argparse
    import json
    import os

    from pheasant.config.loader import load_config
    from pheasant.config.schema import PheasantConfig
    from pheasant.ingestion.extractor import build_extractor
    from pheasant.ingestion.pipeline import _chunks_and_headings, read_text

    parser = argparse.ArgumentParser(prog="python -m pheasant.ingestion.chunk_plan")
    parser.add_argument("path", type=Path)
    parser.add_argument("--config", "-c", default=os.environ.get("PHEASANT_CONFIG"))
    parser.add_argument("--source", "-s", help="plan as this configured source would")
    parser.add_argument("--strategy", default="auto", help="when no --source: fixed|sections|auto")
    args = parser.parse_args(argv)

    if args.source:
        config = load_config(args.config)
        matches = [source for source in config.sources if source.name == args.source]
        if not matches:
            parser.error(f"no source named {args.source!r}")
        source = config.effective_source(matches[0])
        extractor_settings = config.ingestion.extractor
    else:
        config = PheasantConfig.model_validate(
            {
                "sources": [
                    {
                        "name": "preview",
                        "type": "document_folder",
                        "path": str(args.path.parent),
                        "chunking": {"strategy": args.strategy},
                    }
                ]
            }
        )
        source = config.sources[0]
        extractor_settings = config.ingestion.extractor
    text = read_text(args.path, build_extractor(extractor_settings))
    chunks, headings, plan = _chunks_and_headings(source, text, args.path.name)
    sizes = sorted(len(chunk.text) for chunk in chunks)
    print(
        json.dumps(
            {
                "strategy": strategy_of(source),
                "plan": plan,
                "chars": len(text),
                "chunks": len(chunks),
                "chunk_chars": {
                    "min": sizes[0] if sizes else 0,
                    "median": sizes[len(sizes) // 2] if sizes else 0,
                    "max": sizes[-1] if sizes else 0,
                },
                "headings": len(headings),
                "labelled": sum(1 for chunk in chunks if chunk.heading_path),
                "first_chunks": [
                    {
                        "lines": [chunk.start_line, chunk.end_line],
                        "heading_path": chunk.heading_path,
                        "preview": chunk.text[:120],
                    }
                    for chunk in chunks[:5]
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
