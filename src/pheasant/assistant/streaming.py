"""Extract provisional answer text from a streamed combined JSON reply.

The final JSON object is still parsed and verified by the workflow. This
parser only drives the SSE preview and never decides sufficiency or citations.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

_SUFFICIENT = re.compile(r'(?<!\\)"sufficient"\s*:\s*(true|false)(?=\s*[,}])')
_ANSWER = re.compile(r'(?<!\\)"answer"\s*:\s*"')


class JsonAnswerPreview:
    """Emit decoded increments from a sufficient reply's answer string."""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._raw = ""
        self._sent = ""

    def feed(self, delta: str) -> None:
        if not delta:
            return
        self._raw += delta
        decision = _SUFFICIENT.search(self._raw)
        if decision is None or decision.group(1) != "true":
            return
        marker = _ANSWER.search(self._raw)
        if marker is None:
            return
        encoded = self._raw[marker.end() :]
        # Scan to the first unescaped quote. A closing quote may arrive in a
        # later provider chunk; until then decode only complete escape pairs.
        escaped = False
        stop = len(encoded)
        for index, character in enumerate(encoded):
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                stop = index
                break
        try:
            decoded = json.loads('"' + encoded[:stop] + '"')
        except (ValueError, UnicodeError):
            return
        if not isinstance(decoded, str) or not decoded.startswith(self._sent):
            return
        increment = decoded[len(self._sent) :]
        if increment:
            self._sent = decoded
            self._emit(increment)
