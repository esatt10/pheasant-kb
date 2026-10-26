"""`Row`: the dual name/position contract the Postgres backend hands back.

`sqlite3.Row` answers `row["col"]` and `row[0]`, and so must the Postgres
result, because call sites use both. Names are moved onto the C dict lookup
(positions resolve in `__missing__`), so these pin that both spellings, and
every dict behaviour a caller leans on, still answer exactly as before.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pheasant.persistence.backends import _result_rows
from pheasant.persistence.sql import Row


def test_a_row_answers_by_name_and_by_position() -> None:
    row = Row({"id": "a", "type": "doc", "size": 3})
    assert row["type"] == "doc"
    assert row[0] == "a"
    assert row[-1] == 3
    assert row.keys() == ["id", "type", "size"]
    assert dict(row) == {"id": "a", "type": "doc", "size": 3}
    assert {**row}["size"] == 3
    assert row.get("missing") is None
    assert "id" in row


def test_a_missing_name_or_position_still_raises() -> None:
    row = Row({"id": "a"})
    with pytest.raises(KeyError):
        row["nope"]
    with pytest.raises(IndexError):
        row[5]


class _Cursor:
    def __init__(self, names: list[str], values: list[tuple]) -> None:
        self.description = [SimpleNamespace(name=name) for name in names]
        self._values = values

    def fetchall(self) -> list[tuple]:
        return self._values


def test_a_result_set_shares_one_column_order() -> None:
    rows = _result_rows(_Cursor(["id", "type"], [("a", "doc"), ("b", "image")]))
    assert [row["type"] for row in rows] == ["doc", "image"]
    assert [row[0] for row in rows] == ["a", "b"]
    assert rows[0]._order is rows[1]._order
    # Each caller gets its own copy of the keys, so sharing is invisible.
    rows[0].keys().append("x")
    assert rows[1].keys() == ["id", "type"]


def test_a_column_named_twice_keeps_the_first_position_and_the_last_value() -> None:
    """What `Row(dict(zip(...)))` always did, which positional readers rely on."""

    (row,) = _result_rows(_Cursor(["id", "id", "type"], [("first", "second", "doc")]))
    assert row["id"] == "second"
    assert row.keys() == ["id", "type"]
    assert row[1] == "doc"
