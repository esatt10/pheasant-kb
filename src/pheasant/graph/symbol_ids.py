"""Symbol ids that two different names cannot share.

A defined symbol's id is ``symbol:{kb}:{source}:{path}:{name}-{line}`` with the
name slugged (lowercased, leading ``_`` stripped), so two different names on one
line can mint the same id — ctypes' own ``_LARGE_INTEGER = LARGE_INTEGER =
c_longlong``. The second ``symbols`` insert then broke the primary key and
aborted the whole file's write, on SQLite and Postgres alike, and a
stdlib-sized source stopped indexing there.

The first name keeps the plain id. A later, *different* name gets
``-{digest}``, the first 8 hex characters of the sha256 of its exact spelling.
Every id that does not collide is unchanged, and none that collides was ever
stored, so no persisted graph needs a migration (``docs/graph_model.md``
records the decision). The same name on the same line again is one symbol.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping


def unique_symbol_id(mint: Callable[[str], str], seen: Mapping[str, str], name: str) -> str | None:
    """The id for ``name``, or ``None`` when this exact symbol is already recorded.

    ``mint(suffix)`` builds the id with ``suffix`` appended to ``{name}-{line}``;
    ``seen`` maps every id already minted for this artifact to its exact name.
    """

    symbol_id = mint("")
    recorded = seen.get(symbol_id)
    if recorded is None:
        return symbol_id
    if recorded == name:
        return None
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    symbol_id = mint(f"-{digest}")
    return None if symbol_id in seen else symbol_id
