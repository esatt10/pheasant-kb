"""The intent router: how long an answer should be, and whether it is a picture.

Two axes beside the existing knowledge/procedural *intent* (``chat.classify_intent``),
each read the same way that one is — deterministic rules first, so the offline
path and the model path agree, and the planner may overrule a rule only when
the axis was left on ``auto``:

* **depth** — ``short`` (the answer pheasant has always given, and the
  default: its prompts and limits are unchanged), ``medium`` (a few headed
  sections over more evidence) and ``long`` (an outline, then each section
  written from only its own passages, then stitched — see
  ``assistant.longform``).
* **visual** — ``none``, ``diagram`` (a grounded diagram built from the cited
  passages, ``assistant.visuals``) or ``image`` (show the images the corpus
  itself holds and its documents reference).

Neither axis costs a model call to decide. That is the performance half of
the design: routing is a regex over the question plus a field in the JSON the
agentic planner already returns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

DEPTHS = ("short", "medium", "long")
VISUALS = ("none", "diagram", "image")

#: Retrieval and generation settings per depth, layered over the intent
#: profile. ``short`` is empty on purpose: it *is* the pre-router behaviour,
#: which keeps every existing evaluation baseline comparable.
DEPTH_PROFILES: dict[str, dict[str, Any]] = {
    "short": {},
    "medium": {
        "per_query_results": 8,
        "max_context_passages": 16,
        "expand_per_node": 4,
        "passage_chars": 6000,
        "max_output_tokens": 2000,
    },
    "long": {
        "per_query_results": 10,
        "max_context_passages": 24,
        "max_rounds": 4,
        "expand_per_node": 4,
        # Less of each file, because each section reads only its own few.
        "passage_chars": 5000,
        "max_sections": 5,
        "section_output_tokens": 1200,
        "outline_output_tokens": 700,
        "section_concurrency": 4,
        # Wall-clock budget for the section fill. Past it, unfinished sections
        # are answered from their passages extractively and the step says so —
        # a long answer that times out whole is worse than a medium one.
        "deadline_seconds": 150,
    },
}

#: The length instruction appended to the intent's answering prompt.
DEPTH_INSTRUCTIONS = {
    "short": "",
    "medium": """

LENGTH: a medium-length answer. Open with two or three sentences that answer \
the question directly, then 3 to 5 short sections, each under a `### ` \
heading, roughly 300–600 words in all. Every section cites the passages it \
rests on.""",
}

_SHORT = re.compile(
    r"\b(?:tl;?dr|briefly|brief(?:ly)?|quick(?:ly)?|in (?:one|a) (?:line|sentence|word)|"
    r"short answer|one[- ]liner|just (?:tell|say))\b"
)
_LONG = re.compile(
    r"\b(?:in (?:great |full |more )?detail|detailed|comprehensive(?:ly)?|thorough(?:ly)?|"
    r"deep[- ]?dive|everything (?:about|on)|exhaustive(?:ly)?|full (?:overview|report|"
    r"write-?up|explanation|picture)|write-?up|report on|long[- ]form|end[- ]to[- ]end)\b"
)
_MEDIUM = re.compile(
    r"\b(?:overview of|elaborate|expand on|compare|comparison|versus|vs\.?|pros and cons|"
    r"trade-?offs?|outline|a few paragraphs|walk me through|break (?:it |this )?down)\b"
)
_IMAGE = re.compile(
    r"\b(?:show|display|find|see|open|pull up|where is)\b.{0,40}\b(?:image|picture|photo|"
    r"screenshot|figure(?! out)|illustration|the (?:\w+ )?(?:diagram|chart|graphic))\b|"
    r"\b(?:image|picture|figure|screenshot|diagram)s? (?:from|in|of) (?:the )?\w+ "
    r"(?:doc|document|file|page|slide|deck|readme|spec)\b"
)
# Not a bare "visual": "configure Visual Studio" is not a request for a picture.
_DIAGRAM = re.compile(
    r"\b(?:draw|diagram|flow ?chart|visuali[sz]e|a visual|visual (?:of|for|explaining|"
    r"showing)|sketch|map out|mind ?map|sequence diagram|chart the|graph of how|picture of how)\b"
)


@dataclass
class Route:
    """How one question was read, and by what."""

    intent: str = "knowledge"
    depth: str = "short"
    visual: str = "none"
    #: One line per axis saying why, for the classify step and the payload.
    why: dict[str, str] = field(default_factory=dict)
    #: ``rule`` / ``planner`` / ``pinned`` per axis.
    decided_by: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "depth": self.depth,
            "visual": self.visual,
            "why": dict(self.why),
            "decided_by": dict(self.decided_by),
        }


def _pinned(value: Any, allowed: tuple[str, ...]) -> str | None:
    text = str(value or "auto").strip().lower()
    return text if text in allowed else None


def classify_depth(question: str, configured: Any = None) -> tuple[str, str, str]:
    """``(depth, why, decided_by)``. ``short`` is the default and the safe miss:
    a short answer to a question that wanted a report is one follow-up away,
    while a report nobody asked for costs the reader the time to skim it."""

    pinned = _pinned(configured, DEPTHS)
    if pinned:
        return pinned, "pinned by the caller", "pinned"
    text = " ".join((question or "").lower().split())
    if _SHORT.search(text):
        return "short", "asks for a brief answer", "rule"
    if _LONG.search(text):
        return "long", "asks for detail or a full write-up", "rule"
    if _MEDIUM.search(text):
        return "medium", "asks for an overview, comparison or walkthrough", "rule"
    return "short", "no length signal; the default", "rule"


def classify_visual(question: str, configured: Any = None) -> tuple[str, str, str]:
    """``(visual, why, decided_by)``. ``auto`` reads the question."""

    pinned = _pinned(configured, VISUALS)
    if pinned:
        return pinned, "pinned by the caller", "pinned"
    text = " ".join((question or "").lower().split())
    if _IMAGE.search(text):
        return "image", "asks to see an image the corpus holds", "rule"
    if _DIAGRAM.search(text):
        return "diagram", "asks for a diagram or visual", "rule"
    return "none", "no visual requested", "rule"


def route_question(
    question: str,
    *,
    intent: tuple[str, str],
    depth: Any = None,
    visual: Any = None,
    intent_pinned: bool = False,
) -> Route:
    """Read all three axes. ``intent`` is ``(intent, why)`` from the caller,
    which already owns that classification."""

    route = Route(intent=intent[0])
    route.why["intent"] = intent[1]
    route.decided_by["intent"] = "pinned" if intent_pinned else "rule"
    route.depth, route.why["depth"], route.decided_by["depth"] = classify_depth(question, depth)
    route.visual, route.why["visual"], route.decided_by["visual"] = classify_visual(
        question, visual
    )
    return route


def record_route(route: dict[str, Any]) -> None:
    """Count one routed question. In-memory; never on a database."""

    from pheasant.telemetry import metrics

    metrics.REGISTRY.inc(
        "pheasant_assistant_route_total",
        intent=str(route.get("intent") or "knowledge"),
        depth=str(route.get("depth") or "short"),
        visual=str(route.get("visual") or "none"),
        depth_by=str((route.get("decided_by") or {}).get("depth") or "rule"),
    )


def record_visual(visual: dict[str, Any] | None) -> None:
    if not visual:
        return
    from pheasant.telemetry import metrics

    metrics.REGISTRY.inc(
        "pheasant_assistant_visual_total",
        type=str(visual.get("type") or "diagram"),
        status=str(visual.get("status") or "ok"),
    )


def depth_options(depth: str, options: dict[str, Any], explicit: set[str]) -> dict[str, Any]:
    """``options`` with the depth profile applied under anything set explicitly."""

    merged = dict(options)
    for key, value in DEPTH_PROFILES.get(depth, {}).items():
        if key not in explicit:
            merged[key] = value
    return merged


def depth_instruction(depth: str) -> str:
    return DEPTH_INSTRUCTIONS.get(depth, "")
