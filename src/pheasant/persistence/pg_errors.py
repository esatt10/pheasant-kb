"""PostgreSQL failures as one hierarchy, whatever raised them underneath.

Part of :mod:`pheasant.persistence.pg_driver` (re-exported there): every error
the driver raises is an :class:`Error` with the server's ``sqlstate``, so the
layers above classify a failure by what it is -- a dropped connection, a
constraint, a cancelled statement -- rather than by which library threw it.
"""

from __future__ import annotations

import socket


class Error(Exception):
    """Anything the database or the connection to it refused.

    ``sqlstate`` is the server's five-character code, or ``None`` when the
    failure never reached a server answer (a dropped or refused connection).
    ``fields`` carries the server's whole error report (``M`` message, ``D``
    detail, ``C`` code...), for a caller that wants more than the message.
    """

    def __init__(self, message: str, *, sqlstate: str | None = None, fields: dict | None = None):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.fields = dict(fields or {})


class InterfaceError(Error):
    """The client was used wrongly: a closed connection, a bad DSN."""


class DatabaseError(Error):
    """The server refused a statement."""


class OperationalError(DatabaseError):
    """The connection, not the statement: dropped, refused, shut down, out of resources."""


class ConnectionLost(OperationalError):
    """The connection failed under a statement; it cannot be used again."""


class QueryCanceled(OperationalError):
    """``57014``: a statement timeout or a cancel. The server answered; not a disconnect."""


class PoolTimeout(OperationalError):
    """No pooled connection became free before the caller's deadline."""


class IntegrityError(DatabaseError):
    """Class ``23``: a constraint refused the write."""


class DataError(DatabaseError):
    """Class ``22``: a value the column cannot hold."""


class ProgrammingError(DatabaseError):
    """Class ``42`` (and ``26``, ``34``): the statement itself is wrong."""


class InternalError(DatabaseError):
    """Class ``XX`` and ``25``: the server's own state, including a failed transaction."""


def _error_class(sqlstate: str) -> type[Error]:
    if sqlstate == "57014":
        return QueryCanceled
    head = sqlstate[:2]
    if head in {"08", "53", "57", "58"}:
        return OperationalError
    if head == "23":
        return IntegrityError
    if head == "22":
        return DataError
    if head in {"42", "26", "34", "3F", "3D", "0A"}:
        return ProgrammingError
    if head in {"XX", "25", "40", "55", "P0"}:
        return InternalError
    return DatabaseError


def translate_error(exc: BaseException) -> BaseException:
    """``exc`` as one of this module's errors; anything else unchanged."""

    if isinstance(exc, Error):
        return exc
    try:
        import pg8000.exceptions as pg_exceptions
    except ModuleNotFoundError:  # a SQLite install without the postgres extra
        return exc

    if isinstance(exc, pg_exceptions.DatabaseError) and exc.args and isinstance(exc.args[0], dict):
        fields = {str(key): str(value) for key, value in exc.args[0].items()}
        sqlstate = fields.get("C") or None
        message = fields.get("M") or str(exc)
        return _error_class(sqlstate or "")(message, sqlstate=sqlstate, fields=fields)
    if isinstance(exc, pg_exceptions.InterfaceError):
        text = str(exc.args[0]) if exc.args else str(exc)
        cause = exc.__cause__
        if text in {"network error", "connection is closed"} or isinstance(cause, OSError):
            detail = f": {cause}" if cause is not None else ""
            return ConnectionLost(f"server closed the connection unexpectedly ({text}{detail})")
        return InterfaceError(text)
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ConnectionLost(f"the connection timed out: {exc}")
    if isinstance(exc, OSError):
        return ConnectionLost(f"the connection failed: {exc}")
    return exc
