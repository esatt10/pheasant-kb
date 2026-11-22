"""Compiling pheasant's portable SQL for PostgreSQL.

The state store's raw ``rows(sql, params)`` escape hatch is used directly by
dozens of call sites, written with ``?`` (or ``:name``) placeholders. They are
compiled once per statement text, at the boundary: placeholders to ``$n``,
``GROUP_CONCAT`` to ``string_agg``, ``INSERT OR REPLACE`` to ``INSERT``. The
schema's portable type spellings are rewritten by :data:`POSTGRES.types`.
Full-text search is not translated here at all: it is written for Postgres
where it is used (``search/text_store.py``, ``search/pg_textsearch.py``).
"""

from __future__ import annotations

import functools
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

#: ``GROUP_CONCAT(x, sep)`` → ``string_agg(x, sep)``. Postgres has no
#: ``GROUP_CONCAT`` at all, so an untranslated one is a hard error rather than
#: a silent difference — which is the failure mode we want.
_GROUP_CONCAT = re.compile(r"\bGROUP_CONCAT\s*\(", re.IGNORECASE)

#: ``INSERT OR REPLACE INTO t`` → ``INSERT INTO t``; the caller must supply the
#: conflict target, so this is only rewritten where the store knows the key.
_INSERT_OR_REPLACE = re.compile(r"\bINSERT\s+OR\s+REPLACE\s+INTO\b", re.IGNORECASE)


class Row(dict):
    """A result row supporting both ``row["col"]`` and ``row[0]``.

    pheasant's call sites use both — the typed accessors mostly use names,
    while ``assistant/retrieval.py`` and three places in the state store index
    positionally. A row is a mapping, so iterating one yields its column
    names: ``row.values()`` is its values.

    Subclasses ``dict`` so ``dict(row)``, ``.keys()``, ``.get()`` and
    ``**row`` all keep working unchanged.

    Positions go through ``__missing__`` rather than an overridden
    ``__getitem__``, so a lookup by name is the C dict lookup with no Python
    frame. Names are almost every access, and a whole-graph load makes 5.4M of
    them: ~20 s of a 157 s profile went to the override's two ``isinstance``
    tests. ``order`` lets a backend share one column list across a result set
    instead of building one per row.

    No slices: nothing slices a row, and before Python 3.12 a slice is not
    hashable, so the dict lookup refuses it before ``__missing__`` is asked.
    """

    __slots__ = ("_order",)

    def __init__(self, mapping: Any, order: list[str] | None = None):
        super().__init__(mapping)
        object.__setattr__(self, "_order", list(self) if order is None else order)

    def __missing__(self, key: Any) -> Any:
        if isinstance(key, int):
            return dict.__getitem__(self, self._order[key])
        raise KeyError(key)

    def keys(self):  # type: ignore[override]
        return list(self._order)


@dataclass(frozen=True)
class Dialect:
    """How to speak to one database. Instances are module-level singletons."""

    name: str
    #: DB-API paramstyle. Drives placeholder translation.
    paramstyle: str
    #: The full-text mechanism: ``tsvector``.
    fulltext: str
    #: Type substitutions applied to the portable DDL, longest key first so
    #: ``INTEGER PRIMARY KEY`` is never half-replaced by ``INTEGER``.
    types: dict[str, str] = field(default_factory=dict)

    @property
    def is_postgres(self) -> bool:
        return self.name == "postgres"

    def in_clause(self, column: str, values: Sequence[Any]) -> tuple[str, list[Any]]:
        """A key-set predicate, spelled so the driver can reuse the plan.

        ``column IN (?,?,?)`` makes the *statement text* a function of how
        many keys were passed, so a batched read issues a differently-spelled
        statement on nearly every call. `pg_driver` prepares a statement after
        its fifth execution, so a text that never repeats is a statement that
        is never prepared and a plan built from scratch every time. Measured on a
        500-key node fetch — the graph search arm's block size — **8.93ms
        against 6.15ms**.

        So it is ``column = ANY(?)`` with the whole list as one array
        parameter: one text, one plan, one prepared statement for every batch
        size.

        Returns the fragment and the parameters to splice in at its position,
        so a caller composes it into a larger ``WHERE`` rather than being
        handed a whole statement.
        """

        return f"{column} = ANY(?)", [list(values)]

    def translate(self, sql: str) -> str:
        """Rewrite portable SQL for Postgres."""

        return _compile_postgres(sql).text

    def compile(self, sql: str) -> Compiled:
        """:meth:`translate`, plus how to bind ``params`` to the result."""

        return _compile_postgres(sql)


@dataclass(frozen=True)
class Compiled:
    """A statement in a driver's own placeholder syntax.

    ``names`` is the parameter name behind each ``$n`` when the portable SQL
    used ``:name`` placeholders, so a mapping of parameters can be bound by
    position; ``None`` when it used ``?``.
    """

    text: str
    names: tuple[str, ...] | None

    def bind(self, params: Any) -> tuple[Any, ...]:
        if params is None:
            return ()
        if self.names is not None:
            if not isinstance(params, dict):
                raise TypeError("this statement takes named parameters; pass a dict")
            return tuple(params[name] for name in self.names)
        if isinstance(params, dict):
            raise TypeError("this statement takes positional parameters; pass a tuple")
        return tuple(params)


@functools.lru_cache(maxsize=2048)
def _compile_postgres(sql: str) -> Compiled:
    """The Postgres rewrite, memoized: a pure function of the text.

    The scan and the two regex passes ran on every statement before they
    were cached -- 11-28us each, measured -- and the statement texts a
    process issues are a small fixed set. Bounded, because a text carrying an
    ``IN (?,?,...)`` list varies with the list's length (`Dialect.in_clause`
    avoids that on the hot paths, and a cache miss is only the old cost).
    """

    sql = _GROUP_CONCAT.sub("string_agg(", sql)
    sql = _INSERT_OR_REPLACE.sub("INSERT INTO", sql)
    return _rewrite_placeholders(sql)


def _rewrite_placeholders(sql: str) -> Compiled:
    """``?`` and ``:name`` to ``$n``, outside literals, identifiers and comments.

    A ``LIKE '%?%'`` keeps its ``?``, ``search_vector::text`` keeps its cast
    (``::`` is never a name), and a repeated ``:name`` reuses its number. No
    other character is touched: the server sees ``%`` exactly as written,
    which a driver interpolating ``%s`` could not promise.
    """

    out: list[str] = []
    names: list[str] = []
    positional = 0
    i, length = 0, len(sql)
    while i < length:
        char = sql[i]
        if char == "'" or char == '"':
            end = i + 1
            while end < length:
                if sql[end] == char:
                    if end + 1 < length and sql[end + 1] == char:
                        end += 2
                        continue
                    break
                end += 1
            out.append(sql[i : end + 1])
            i = end + 1
        elif char == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            end = length if end < 0 else end
            out.append(sql[i:end])
            i = end
        elif char == "?":
            positional += 1
            out.append(f"${positional}")
            i += 1
        elif (
            char == ":"
            and i + 1 < length
            and (sql[i + 1].isalpha() or sql[i + 1] == "_")
            and (i == 0 or sql[i - 1] != ":")
        ):
            end = i + 1
            while end < length and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            name = sql[i + 1 : end]
            if name not in names:
                names.append(name)
            out.append(f"${names.index(name) + 1}")
            i = end
        else:
            out.append(char)
            i += 1
    if positional and names:
        raise ValueError("a statement mixes ? and :name placeholders")
    return Compiled("".join(out), tuple(names) if names else None)


POSTGRES = Dialect(
    name="postgres",
    paramstyle="numeric",
    fulltext="tsvector",
    types={
        # The schema's portable spellings. Column names and semantics stay as
        # written, so every stable ID built from these rows is unchanged.
        "INTEGER PRIMARY KEY": "BIGINT PRIMARY KEY",
        "INTEGER": "BIGINT",
        "REAL": "DOUBLE PRECISION",
    },
)
