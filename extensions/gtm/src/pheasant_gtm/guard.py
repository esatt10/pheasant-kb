"""Cheap detection of explicit instructions in publisher data.

The security boundary is capability isolation and promotion policy. These
patterns quarantine recognizable attacks early and avoid storing their text.
"""

from __future__ import annotations

import re
import unicodedata
from html import unescape
from urllib.parse import unquote

_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"<\|\s*(?:system|assistant|developer|user|im_start|im_end)[^<>]{0,40}\|>",
        r"\[(?:/)?(?:inst|system|developer|assistant)\]|<<\s*/?sys\s*>>",
        r"\b(?:ignore|disregard|override|forget|bypass)\b.{0,60}"
        r"\b(?:previous|prior|earlier|above|system|developer|all)\b.{0,30}"
        r"\b(?:instructions?|prompts?|messages?|rules?|polic(?:y|ies))\b",
        r"\b(?:reveal|print|return|send|upload|exfiltrate|disclose|share)\b.{0,60}"
        r"\b(?:system prompts?|developer messages?|api keys?|environment variables?|"
        r"bearer tokens?|credentials?|secrets?)\b",
        r"\b(?:call|invoke|run|execute)\b.{0,36}"
        r"\b(?:a tool|the tool|a function|the function|shell command|terminal command|"
        r"mcp tool|exec_command|apply_patch|review.packet)\b",
        r"\b(?:you are now|you must obey|assistant must|output only)\b.{0,60}"
        r"\b(?:assistant|agent|instructions?|json|tool calls?|system|developer)\b",
    )
)


def suspicious(text: str) -> bool:
    decoded = unicodedata.normalize("NFKC", unquote(unescape(unescape(text))))
    visible = "".join(ch for ch in decoded if unicodedata.category(ch) != "Cf")
    normalized = " ".join(visible.split())
    return any(pattern.search(normalized) for pattern in _PATTERNS)
