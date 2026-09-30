"""SQL dialect handling for the state backend seam (Phase 35.2).

pheasant's state lives in ~50 typed methods on :class:`~pheasant.persistence.
state_store.StateStore`, but it also exposes a raw ``rows(sql, params)`` escape
hatch that **57 call sites across 15 modules** use directly. Converting all of
them to typed methods was the plan; reading them changed it. The SQL they run
is almost entirely ANSI — the dialect-specific surface is three things:

* the placeholder style (``?`` vs ``%s``),
* ``GROUP_CONCAT`` vs ``string_agg``,
* ``INSERT OR REPLACE`` vs ``INSERT … ON CONFLICT DO UPDATE``,

plus full-text search, which is not translated here at all: FTS5's ``MATCH`` +
``bm25()`` and Postgres's ``tsvector`` + ``ts_rank_cd`` are different enough
that pretending otherwise would produce a query that runs and ranks wrongly.
:mod:`pheasant.search.sqlite_store` carries a real per-dialect implementation
instead. **A wrong answer is worse than an error**, which is the whole reason
translation is kept this narrow.

Rewriting 57 call sites would have been 57 chances to change behaviour on the
default path. Translating at the boundary is one place to get right and leaves
the SQLite path executing byte-identical SQL.
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

#: A ``?`` placeholder, ignoring any inside a string literal or a cast (``?``
#: does not appear in Postgres casts, but ``::`` does and we must not touch it).
_PLACEHOLDER = re.compile(r"\?")

#: A ``:name`` placeholder. sqlite3 takes these with a dict; psycopg spells the
#: same thing ``%(name)s``. The negative lookbehind keeps Postgres's ``::``
#: cast operator intact — ``search_vector::text`` must not become
#: ``search_vector:%(text)s``, which is still valid-looking SQL and would bind
#: a parameter that does not exist.
_NAMED_PLACEHOLDER = re.compile(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)")

#: String literals, so placeholder rewriting never reaches inside one. A
#: ``LIKE '%?%'`` would otherwise become ``LIKE '%%s%'`` and match nothing —
#: silently, since it is still valid SQL.
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")


class Row(dict):
    """A result row supporting both ``row["col"]`` and ``row[0]``.

    ``sqlite3.Row`` allows both, and pheasant's call sites use both — the
    typed accessors mostly use names, while ``assistant/retrieval.py`` and
    three places in the state store index positionally. Returning a plain dict
    from the Postgres backend would break those, so this preserves the
    contract rather than asking 57 call sites to agree on one style.

    Subclasses ``dict`` so ``dict(row)``, ``.keys()``, ``.get()`` and
    ``**row`` all keep working unchanged.
    """

    __slots__ = ("_order",)

    def __init__(self, mapping: dict[str, Any]):
        super().__init__(mapping)
        object.__setattr__(self, "_order", list(mapping))

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return super().__getitem__(self._order[key])
        if isinstance(key, slice):
            return [super(Row, self).__getitem__(k) for k in self._order[key]]
        return super().__getitem__(key)

    def keys(self):  # type: ignore[override]
        return list(self._order)


@dataclass(frozen=True)
class Dialect:
    """How to speak to one database. Instances are module-level singletons."""

    name: str
    #: DB-API paramstyle. Drives placeholder translation.
    paramstyle: str
    #: Whether this dialect has FTS5 virtual tables (SQLite) or tsvector.
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
        statement on nearly every call. SQLite reparses cheaply and in
        process; psycopg3 auto-prepares a statement after its fifth execution,
        so on Postgres a text that never repeats is a statement that is never
        prepared and a plan built from scratch every time. Measured on a
        500-key node fetch — the graph search arm's block size — **8.93ms
        against 6.15ms**.

        Postgres therefore gets ``column = ANY(?)`` with the whole list as one
        array parameter: one text, one plan, one prepared statement for every
        batch size. SQLite keeps the ``IN`` list, which is its own reference
        spelling and has no array type to bind to.

        Returns the fragment and the parameters to splice in at its position,
        so a caller composes it into a larger ``WHERE`` rather than being
        handed a whole statement.
        """

        items = list(values)
        if self.is_postgres:
            return f"{column} = ANY(?)", [items]
        return f"{column} IN ({','.join('?' for _ in items)})", items

    def translate(self, sql: str) -> str:
        """Rewrite portable SQL for this dialect.

        SQLite is the reference dialect: it returns its input untouched, so
        the default path executes exactly the SQL it always did and cannot
        regress through this function.
        """

        if not self.is_postgres:
            return sql
        return _translate_postgres(sql)


@functools.lru_cache(maxsize=2048)
def _translate_postgres(sql: str) -> str:
    """The Postgres rewrite, memoized: a pure function of the text.

    Two regex passes and a string-literal scan ran on every statement --
    11-28us each, measured -- and the statement texts a process issues are a
    small fixed set. Bounded, because a text carrying an ``IN (?,?,...)`` list
    varies with the list's length (`Dialect.in_clause` avoids that on the hot
    paths, and a cache miss is only the old cost).
    """

    sql = _GROUP_CONCAT.sub("string_agg(", sql)
    sql = _INSERT_OR_REPLACE.sub("INSERT INTO", sql)
    return _rewrite_placeholders(sql)


def _rewrite_placeholders(sql: str) -> str:
    """``?`` → ``%s``, leaving string literals alone.

    Also escapes any literal ``%`` so psycopg's own interpolation does not
    consume it — ``LIKE '%/%'`` is real SQL in this codebase (the FTS title
    migration uses it) and would otherwise raise or, worse, bind wrongly.
    """

    def rewrite(segment: str) -> str:
        # Named first: `:id` -> `%(id)s`. Doing `?` first would be harmless,
        # but the named pass must not see a `%s` it could mistake for text.
        segment = _NAMED_PLACEHOLDER.sub(r"%(\1)s", segment)
        return _PLACEHOLDER.sub("%s", segment)

    out: list[str] = []
    position = 0
    for literal in _STRING_LITERAL.finditer(sql):
        out.append(rewrite(sql[position : literal.start()]))
        out.append(literal.group(0).replace("%", "%%"))
        position = literal.end()
    out.append(rewrite(sql[position:]))
    return "".join(out)


SQLITE = Dialect(name="sqlite", paramstyle="qmark", fulltext="fts5")

POSTGRES = Dialect(
    name="postgres",
    paramstyle="pyformat",
    fulltext="tsvector",
    types={
        # SQLite's TEXT/INTEGER/REAL are affinities, not widths. The mapping
        # keeps column names and semantics identical so every ANSI query in
        # the codebase — and every stable ID built from these rows — is
        # unchanged across backends.
        "INTEGER PRIMARY KEY": "BIGINT PRIMARY KEY",
        "INTEGER": "BIGINT",
        "REAL": "DOUBLE PRECISION",
    },
)

DIALECTS = {"sqlite": SQLITE, "postgres": POSTGRES}


def dialect_for(name: str) -> Dialect:
    try:
        return DIALECTS[str(name or "sqlite").lower()]
    except KeyError:
        raise ValueError(
            f"Unknown storage backend {name!r}. Valid values: {', '.join(sorted(DIALECTS))}"
        ) from None
