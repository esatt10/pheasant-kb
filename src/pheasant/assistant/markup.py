"""Answer text that holds code: change the prose, never the code.

Citation markers (``[3]``) are prose; an index (``identity[0]``, ``subnet[1]``)
is code, and the two are spelled alike. Every pass that removes or rewrites
markers across a whole answer therefore also rewrites the code inside it.
Measured: the verify step removed ``[0]`` from ``identity[0].principal_id`` in
every composed Terraform artifact that used it, because no passage is numbered
0, and ``terraform validate`` then refused the artifact -- the most frequent
validation failure across a day of runs, and not the model's.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_FENCED = re.compile(r"(```.*?(?:```|$))", re.S)


def outside_code(text: str, change: Callable[[str], str]) -> str:
    """``text`` with ``change`` applied to everything outside fenced code blocks.

    An unclosed fence runs to the end of the text, as a renderer would show it.
    """
    parts = _FENCED.split(text or "")
    return "".join(part if part.startswith("```") else change(part) for part in parts)
