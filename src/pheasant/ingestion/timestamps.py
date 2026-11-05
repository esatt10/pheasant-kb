"""Document timestamps in one comparable spelling.

Every connector already reports *when its source says a document last
changed* -- a folder's file mtime, Drive's ``modifiedTime``, a web page's
``Last-Modified``, an API's ``updated_at`` -- and each spells it differently:
``2025-03-01T09:12:44Z``, ``2025-03-01T09:12:44.512Z``, ``Sat, 01 Mar 2025
09:12:44 GMT``, an epoch number. Stored as they arrive, they cannot be
compared, so they cannot be filtered on: an HTTP date sorts by weekday.

:func:`normalize_timestamp` is the one place a source's spelling becomes
``YYYY-MM-DDTHH:MM:SSZ`` in UTC. That form sorts lexically as it sorts in
time, on SQLite and Postgres alike, so a date range is a plain string
comparison in SQL and in Python with no dialect branch. A value it cannot read
is ``None`` -- *unknown* -- and never "now": a fetch time dressed as a source
timestamp is exactly the defect this module exists to stop
(``parse_connector_payload`` used to fall back to ``utc_now()``).

:func:`parse_bound` reads what a *caller* types as the edge of a window, where
a bare date is the natural thing to type and means the start of that day.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any

#: The canonical form: second precision, UTC, ``Z``. Lexical order is time order.
CANONICAL_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: Epoch values above this are milliseconds (it is 2286 in seconds), so an API
#: that reports ``1714560000000`` is read as the date it means.
_EPOCH_MS_THRESHOLD = 10_000_000_000

#: Timestamps outside this range are refused rather than stored: a zero epoch
#: from a FAT volume or an unset field reads as 1970 and would date a document
#: half a century early, which a date filter would then act on.
_EARLIEST_YEAR = 1971
_LATEST_YEAR = 9999

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YEAR_MONTH = re.compile(r"^\d{4}-\d{2}$")
_YEAR = re.compile(r"^\d{4}$")


def canonical(moment: datetime) -> str:
    """``moment`` in the canonical spelling. A naive datetime is taken as UTC."""

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime(CANONICAL_FORMAT)


def _plausible(moment: datetime) -> datetime | None:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    year = moment.astimezone(UTC).year
    return moment if _EARLIEST_YEAR <= year <= _LATEST_YEAR else None


def _parse(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return _plausible(value)
    if isinstance(value, date):
        return _plausible(datetime(value.year, value.month, value.day, tzinfo=UTC))
    if isinstance(value, int | float):
        seconds = float(value)
        if abs(seconds) >= _EPOCH_MS_THRESHOLD:
            seconds /= 1000.0
        try:
            return _plausible(datetime.fromtimestamp(seconds, UTC))
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return _parse(float(text))
    iso = text.replace(" ", "T", 1) if re.match(r"^\d{4}-\d{2}-\d{2} \d", text) else text
    if iso.endswith(("Z", "z")):
        iso = iso[:-1] + "+00:00"
    try:
        return _plausible(datetime.fromisoformat(iso))
    except ValueError:
        pass
    try:
        # RFC 1123 / RFC 850 / asctime: every spelling HTTP allows for
        # ``Last-Modified``, and the RFC 2822 dates mail-shaped APIs send.
        return _plausible(parsedate_to_datetime(text))
    except (TypeError, ValueError, IndexError):
        return None


def normalize_timestamp(value: Any) -> str | None:
    """A source timestamp in canonical form, or ``None`` when it is unknown.

    Accepts ISO 8601 (any precision, ``Z`` or an offset, a space for ``T``),
    the HTTP date formats, epoch seconds or milliseconds (number or digits),
    and ``datetime``/``date`` objects. Anything else -- including an
    implausible year -- is ``None``, because a filter acting on a misread date
    is worse than one that admits it does not know.
    """

    moment = _parse(value)
    return canonical(moment) if moment is not None else None


class InvalidDateBound(ValueError):
    """A caller's date-window edge could not be read."""


def parse_bound(value: Any, *, name: str = "date") -> str | None:
    """A caller's window edge as a canonical timestamp, ``None`` when absent.

    A bare ``2025-03-01`` is midnight UTC that day; ``2025-03`` the first of
    the month; ``2025`` the first of the year -- the *start* of the period
    named, so ``updated_after: 2025`` and ``updated_before: 2026`` together are
    "during 2025". Everything :func:`normalize_timestamp` reads is accepted
    too. Unreadable input raises :class:`InvalidDateBound` naming the field,
    because a silently ignored bound returns the whole corpus to somebody who
    asked for a slice of it.
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _YEAR.fullmatch(text):
        text = f"{text}-01-01"
    elif _YEAR_MONTH.fullmatch(text):
        text = f"{text}-01"
    if _DATE_ONLY.fullmatch(text):
        try:
            day = date.fromisoformat(text)
        except ValueError as exc:
            raise InvalidDateBound(f"{name}: not a calendar date: {value!r}") from exc
        return canonical(datetime(day.year, day.month, day.day, tzinfo=UTC))
    normalized = normalize_timestamp(text)
    if normalized is None:
        raise InvalidDateBound(
            f"{name}: could not read {value!r} as a date; use YYYY-MM-DD or an "
            "ISO 8601 timestamp such as 2025-03-01T09:00:00Z"
        )
    return normalized
