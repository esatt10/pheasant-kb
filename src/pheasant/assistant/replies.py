"""Reading a JSON object out of what a model actually replied.

Every structured call the assistant makes — the planner, the grader, the
long-answer outline, the diagram spec — asks for "JSON only" and then has to
cope with what comes back, which depends on the model: a bare object, an
object in a fence after a paragraph, a ``<think>`` block first, a trailing
comma, a Python literal. Three copies of a parser that handled a *leading*
fence and one greedy ``{.*}`` had grown up, one per caller; a reply that the
diagram path could read and the planner could not is the same model reply
failing in one place and not another. One reader now, for all of them.

Pure functions over strings; no model, no I/O.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

_THINK_RE = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```[A-Za-z0-9_-]*\s*\n?(.*?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def json_object(raw: str | None) -> dict[str, Any] | None:
    """The first JSON object in a model reply, or ``None``."""

    found = json_objects(raw)
    return found[0] if found else None


def json_objects(raw: str | None) -> list[dict[str, Any]]:
    """Every top-level JSON object in a model reply, in order.

    Reasoning blocks are dropped first, so an object the model sketched while
    thinking is not mistaken for its answer.
    """

    if not raw:
        return []
    text = _THINK_RE.sub("", str(raw)).strip()
    found: list[dict[str, Any]] = []
    for block in [text, *_FENCE_RE.findall(text)]:
        found.extend(_objects_in(block))
    return found


def _objects_in(text: str) -> list[dict[str, Any]]:
    """Every top-level JSON object ``text`` contains, lenient forms last."""

    text = text.strip()
    found: list[dict[str, Any]] = []
    for attempt in (text, _TRAILING_COMMA_RE.sub(r"\1", text)):
        if found:
            break
        decoder = json.JSONDecoder()
        index = attempt.find("{")
        while index != -1:
            try:
                value, end = decoder.raw_decode(attempt, index)
            except json.JSONDecodeError:
                index = attempt.find("{", index + 1)
                continue
            if isinstance(value, dict):
                found.append(value)
            elif isinstance(value, list):
                found.extend(item for item in value if isinstance(item, dict))
            index = attempt.find("{", end)
    if not found:
        # A Python literal (single quotes, True/None) is what some models
        # write when they forget which language they are in. ``literal_eval``
        # evaluates literals only, never code.
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                value = ast.literal_eval(text[start : end + 1])
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                value = None
            if isinstance(value, dict):
                found.append(value)
    return found
