"""PostgreSQL connections for pheasant: pg8000 underneath, shaped for the store.

pheasant used psycopg 3 and psycopg_pool. Both are LGPL, and the replacement is
pg8000 (BSD-3-Clause, pure Python) for the wire protocol and SQLAlchemy's
``QueuePool`` (MIT) for pooling. This module (with its parts
``pg_dsn``, ``pg_errors`` and ``pg_pool``) is the only place either is
imported, so the rest of pheasant speaks to one small surface:

* :func:`parse_dsn` / :func:`connect` -- a URL or libpq ``key=value`` DSN
  (:mod:`pheasant.persistence.pg_dsn`),
  TLS by ``sslmode``, TCP keepalives, a bounded connect.
* :class:`Connection` -- ``execute`` / ``executemany`` / ``execute_script``,
  ``commit`` / ``rollback``, ``autocommit``; results as column names + rows.
* :class:`ConnectionPool` -- bounded, a per-checkout deadline, idle checks
  (:mod:`pheasant.persistence.pg_pool`).
* :class:`Error` and its subclasses -- one hierarchy with a ``sqlstate``,
  whatever raised underneath (:mod:`pheasant.persistence.pg_errors`).

**Why the protocol is driven here rather than through pg8000's DB-API.**
pg8000's ``Cursor.execute`` (``CoreConnection.execute_unnamed``) waits for the
server three times per statement -- after Parse, after Describe, after
Execute -- and its ``executemany`` is that, once per row. psycopg sends one
statement in one round trip and pipelines ``executemany``. On loopback the
difference hides; at the 10-40 ms between Container Apps and their Postgres it
is the difference between a commit and three. So :class:`Connection` writes
Parse/Describe/Bind/Execute and one Sync together, and reads once:

* a statement is **one round trip**, exactly as with psycopg;
* a statement text this connection has run five times is prepared (named),
  and runs as Bind/Execute -- psycopg's ``prepare_threshold=5`` and
  ``prepared_max=100``, so planning behaviour is what it was;
* ``executemany`` is Parse once and Bind/Execute per row under one Sync per
  block of rows, the same number of round trips as psycopg's pipeline;
* a transaction's ``BEGIN`` rides in the same round trip as its first
  statement.

The messages are written here (the protocol is PostgreSQL's, documented and
stable) and read by pg8000's own message handlers, except ``DataRow``, which
:func:`_read_messages` decodes inline. That touches pg8000 internals
(``CoreConnection._sock``, ``message_types``, ``_transaction_status``,
``Context``), which are not its documented API, so the version is pinned in
``pyproject.toml`` and ``tests/test_pg_driver.py`` fails if one this module
reads disappears. Parameter types are sent the way
psycopg sends them (an ``int`` as the smallest integer type that holds it, a
``str`` untyped so the server infers it), so a statement that resolved its
types one way under psycopg resolves them the same way here.

Nothing here imports pheasant above the persistence layer.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import itertools
import logging
import socket
import ssl
import uuid
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pheasant.persistence.pg_dsn import (
    DEFAULT_CONNECT_TIMEOUT,
    KEEPALIVE_COUNT,
    KEEPALIVE_IDLE_SECONDS,
    KEEPALIVE_INTERVAL_SECONDS,
    ConnectParams,
    _ssl_context,
    _startup_options,
    apply_keepalives,
    parse_dsn,
)
from pheasant.persistence.pg_errors import (
    ConnectionLost,
    DatabaseError,
    DataError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    OperationalError,
    PoolTimeout,
    ProgrammingError,
    QueryCanceled,
    _error_class,
    translate_error,
)

#: The one surface the rest of pheasant imports: the driver proper, plus the
#: DSN, error and pool parts it is split into.
__all__ = [
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_POOL_TIMEOUT",
    "EXECUTEMANY_BLOCK_BYTES",
    "EXECUTEMANY_BLOCK_ROWS",
    "IDLE_CHECK_SECONDS",
    "KEEPALIVE_COUNT",
    "KEEPALIVE_IDLE_SECONDS",
    "KEEPALIVE_INTERVAL_SECONDS",
    "PREPARED_MAX",
    "PREPARE_THRESHOLD",
    "ConnectParams",
    "Connection",
    "ConnectionLost",
    "ConnectionPool",
    "Cursor",
    "DataError",
    "DatabaseError",
    "Error",
    "IntegrityError",
    "InterfaceError",
    "InternalError",
    "OperationalError",
    "PoolTimeout",
    "ProgrammingError",
    "QueryCanceled",
    "Result",
    "_error_class",
    "_ssl_context",
    "apply_keepalives",
    "connect",
    "param_oid",
    "parse_dsn",
    "translate_error",
]

logger = logging.getLogger(__name__)

#: A statement text run this many times on one connection is prepared:
#: psycopg's default, kept so planning behaviour does not move with the driver.
PREPARE_THRESHOLD = 5

#: Prepared statements kept per connection; the least recently used is closed.
PREPARED_MAX = 100

#: Rows per Sync in :meth:`Connection.executemany`. Bounded so the server's
#: replies to one block can never fill the socket while this side is still
#: writing it (which would deadlock both ends).
EXECUTEMANY_BLOCK_ROWS = 1000

#: Bytes of parameters per Sync in :meth:`Connection.executemany`.
EXECUTEMANY_BLOCK_BYTES = 4 * 1024 * 1024

#: Socket read buffer per connection (see :func:`connect`).
_READ_BUFFER_BYTES = 256 * 1024


def connect(
    dsn: str | ConnectParams,
    *,
    autocommit: bool = False,
    application_name: str | None = None,
    connect_timeout: float | None = None,
    settings: Mapping[str, str] | None = None,
) -> Connection:
    """Open one connection.

    ``connect_timeout`` bounds the whole handshake (TCP connect, TLS, startup,
    authentication); after it the socket blocks, like libpq's, and keepalives
    are what find a dead peer under a long statement. ``settings`` become
    ``-c name=value`` startup options (``statement_timeout`` for a heartbeat).
    """

    import pg8000.native

    params = dsn if isinstance(dsn, ConnectParams) else parse_dsn(dsn)
    timeout = connect_timeout or params.connect_timeout or DEFAULT_CONNECT_TIMEOUT
    name = application_name or params.application_name
    sock: socket.socket | None = None
    try:
        unix = params.unix_socket
        if unix is not None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]
            sock.settimeout(timeout)
            sock.connect(unix)
        else:
            sock = socket.create_connection((params.hostaddr or params.host, params.port), timeout)
            # libpq always does this: a request is written whole and then
            # waited on, so Nagle's algorithm only ever adds a delayed ACK.
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            apply_keepalives(sock)
        ssl_context = _ssl_context(params)
        if isinstance(ssl_context, ssl.SSLContext) and params.hostaddr:
            # TLS verifies the name, not the address used to reach it.
            pass
        raw = pg8000.native.Connection(
            params.user,
            host=params.host,
            database=params.database,
            port=params.port,
            password=params.password,
            ssl_context=ssl_context,
            application_name=name,
            startup_params=_startup_options(params.options, settings) or None,
            sock=sock,
        )
    except Exception as exc:  # noqa: BLE001 - normalized below
        if sock is not None:
            try:
                sock.close()
            except OSError:  # pragma: no cover
                pass
        error = translate_error(exc)
        if isinstance(error, ConnectionLost):
            raise ConnectionLost(
                f"could not connect to {params.redacted()}: {error}", sqlstate=None
            ) from exc
        if error is exc:
            raise
        raise error from exc
    # The handshake had a deadline; statements do not (a commit may be long).
    raw._usock.settimeout(None)
    # pg8000 reads through an 8 KiB buffer. A large result then drains the
    # kernel's receive queue 8 KiB per call, and over a real network the
    # server stalls waiting for the window to reopen: a 600 KB read took 3x
    # psycopg's at 10 ms RTT. Read through a buffer that drains it in a few
    # calls instead. Startup is over, so nothing is buffered yet.
    raw._sock = raw._usock.makefile(mode="rwb", buffering=_READ_BUFFER_BYTES)
    return Connection(raw, autocommit=autocommit)


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

_INT2, _INT4, _INT8, _NUMERIC = 21, 23, 20, 1700
_INT2_ARRAY, _INT4_ARRAY, _INT8_ARRAY = 1005, 1007, 1016
_FLOAT8, _BOOL, _BYTEA, _UUID = 701, 16, 17, 2950
_TIMESTAMP, _TIMESTAMPTZ, _DATE, _TIME, _INTERVAL = 1114, 1184, 1082, 1083, 1186
_UNKNOWN = 0


def _int_oid(value: int) -> int:
    if -32768 <= value <= 32767:
        return _INT2
    if -2147483648 <= value <= 2147483647:
        return _INT4
    if -9223372036854775808 <= value <= 9223372036854775807:
        return _INT8
    return _NUMERIC


def param_oid(value: Any) -> int:
    """The type a parameter is sent as -- psycopg's choice for the same value.

    ``str`` and ``None`` are untyped, so the server infers them from the
    statement; an ``int`` is the smallest integer type holding it; a list of
    ints the matching array; any other list untyped.
    """

    kind = type(value)
    if kind is str or value is None:
        return _UNKNOWN
    if kind is bool:
        return _BOOL
    if kind is int:
        return _int_oid(value)
    if kind is float:
        return _FLOAT8
    if kind in (bytes, bytearray, memoryview):
        return _BYTEA
    if kind is list or kind is tuple:
        items = [item for item in value if item is not None]
        if items and all(type(item) is int for item in items):
            widest = max(_int_oid(item) for item in items)
            return {_INT2: _INT2_ARRAY, _INT4: _INT4_ARRAY, _INT8: _INT8_ARRAY}.get(
                widest, _UNKNOWN
            )
        return _UNKNOWN
    if isinstance(value, _dt.datetime):
        return _TIMESTAMPTZ if value.tzinfo is not None else _TIMESTAMP
    if isinstance(value, _dt.date):
        return _DATE
    if isinstance(value, _dt.time):
        return _TIME
    if isinstance(value, _dt.timedelta):
        return _INTERVAL
    if isinstance(value, decimal.Decimal):
        return _NUMERIC
    if isinstance(value, uuid.UUID):
        return _UUID
    return _UNKNOWN


_LENGTH = __import__("struct").Struct("!i").unpack_from
_HEADER = __import__("struct").Struct("!ci").unpack_from


def _install_row_decoder(raw: Any) -> None:
    """Prepare the per-connection state :func:`_read_messages` needs."""

    from pg8000.converters import string_in

    raw._pheasant_identity = string_in
    raw._pheasant_plans = {}


def _read_messages(raw: Any, context: Any) -> None:
    """Read the server's replies up to ``ReadyForQuery``, then raise any error.

    What pg8000's ``handle_messages`` does, with the per-row work inlined.
    pg8000 reads each message with two buffered reads, copies each into a
    ``bytearray`` and back, and calls a converter for every value -- for text,
    most of what pheasant reads, one that returns its argument. Profiled on a
    2,500-row read, that per-message overhead was the whole difference from
    psycopg's C decoder. Values are exactly what pg8000's converters produce.
    """

    from pg8000.core import DATA_ROW, READY_FOR_QUERY

    read = raw._sock.read
    handlers = raw.message_types
    encoding = raw._client_encoding
    identity = raw._pheasant_identity
    plans = raw._pheasant_plans
    length = _LENGTH
    header = _HEADER
    plan_funcs: Any = None
    plan: tuple[Any, ...] = ()
    rows: list[Any] | None = None
    try:
        while True:
            head = read(5)
            if len(head) < 5:
                head = _read_rest(read, head, 5)
            code, size = header(head)
            size -= 4
            data = read(size) if size else b""
            if len(data) < size:
                data = _read_rest(read, data, size)
            if code == DATA_ROW:
                funcs = context.input_funcs
                if funcs is not plan_funcs:
                    cached = plans.get(id(funcs))
                    if cached is None or cached[0] is not funcs:
                        cached = (funcs, tuple(None if f is identity else f for f in funcs))
                        if len(plans) > 64:
                            plans.clear()
                        plans[id(funcs)] = cached
                    plan_funcs, plan = funcs, cached[1]
                    rows = context.rows
                row: list[Any] = []
                append = row.append
                position = 2
                for func in plan:
                    value_size = length(data, position)[0]
                    position += 4
                    if value_size < 0:
                        append(None)
                        continue
                    end = position + value_size
                    text = data[position:end].decode(encoding)
                    position = end
                    append(text if func is None else func(text))
                rows.append(row)  # type: ignore[union-attr]
                continue
            handlers[code](data, context)
            if code == READY_FOR_QUERY:
                break
            plan_funcs = None
    except OSError as exc:
        import pg8000.exceptions

        raise pg8000.exceptions.InterfaceError("network error") from exc
    if context.error is not None:
        raise context.error


def _read_rest(read: Any, first: bytes, size: int) -> bytes:
    buffer = bytearray(first)
    while len(buffer) < size:
        block = read(size - len(buffer))
        if not block:
            import pg8000.exceptions

            raise pg8000.exceptions.InterfaceError("network error")
        buffer.extend(block)
    return bytes(buffer)


_PACK_I32 = __import__("struct").Struct("!i").pack
_PACK_I16 = __import__("struct").Struct("!h").pack
_NULL_VALUE = _PACK_I32(-1)


def _message(code: bytes, body: bytes | bytearray) -> bytes:
    return code + _PACK_I32(len(body) + 4) + bytes(body)


_SYNC = _message(b"S", b"")
_EXECUTE = _message(b"E", b"\x00" + _PACK_I32(0))


def _parse_message(name: bytes, sql: str, oids: Sequence[int], encoding: str) -> bytes:
    body = bytearray(name)
    body += sql.encode(encoding) + b"\x00"
    body += _PACK_I16(len(oids))
    for oid in oids:
        body += _PACK_I32(oid)
    return _message(b"P", body)


def _bind_message(name: bytes, encoded: Sequence[bytes | None]) -> bytes:
    """``Bind`` of the unnamed portal to statement ``name``: text in, text out."""

    body = bytearray(b"\x00")
    body += name
    body += b"\x00\x00"
    body += _PACK_I16(len(encoded))
    for value in encoded:
        if value is None:
            body += _NULL_VALUE
        else:
            body += _PACK_I32(len(value))
            body += value
    body += b"\x00\x00"
    return _message(b"B", body)


_BEGIN_MESSAGES = _message(b"P", b"\x00BEGIN\x00\x00\x00") + _bind_message(b"\x00", ()) + _EXECUTE


def _encode_values(raw: Any, values: Sequence[Any]) -> list[bytes | None]:
    """Parameters as the text pg8000's converters write, encoded.

    The common types are written inline (identical text: ``str`` as is,
    ``int`` and ``float`` by ``str()``, ``bool`` as ``true``/``false``,
    ``bytes`` as ``\\x`` hex); anything else goes through pg8000's own
    converter table.
    """

    encoding = raw._client_encoding
    out: list[bytes | None] = []
    append = out.append
    for value in values:
        kind = type(value)
        if value is None:
            append(None)
        elif kind is str:
            append(value.encode(encoding))
        elif kind is int or kind is float:
            append(str(value).encode("ascii"))
        elif kind is bool:
            append(b"true" if value else b"false")
        elif kind is bytes or kind is bytearray or kind is memoryview:
            append(b"\\x" + bytes(value).hex().encode("ascii"))
        else:
            from pg8000.converters import make_param

            text = make_param(raw.py_types, list(value) if kind is tuple else value)
            append(None if text is None else str(text).encode(encoding))
    return out


@dataclass
class Result:
    """What one statement returned. ``columns`` is ``None`` for no result set."""

    columns: list[str] | None
    rows: list[list[Any]]
    rowcount: int


@dataclass
class _Prepared:
    name: bytes
    columns: Any
    input_funcs: Any


class Connection:
    """One PostgreSQL session over pg8000's protocol primitives.

    Statements take pheasant's compiled SQL -- ``$1`` placeholders, which is
    what :meth:`pheasant.persistence.sql.Dialect.compile` produces -- and a
    tuple of parameters. ``autocommit=False`` behaves as psycopg's default: the
    first statement opens a transaction that lasts until :meth:`commit` or
    :meth:`rollback`.
    """

    def __init__(self, raw: Any, *, autocommit: bool = False):
        self._raw = raw
        _install_row_decoder(raw)
        self._autocommit = autocommit
        self._broken = False
        self._uses: dict[tuple[str, tuple[int, ...]], int] = {}
        self._prepared: OrderedDict[tuple[str, tuple[int, ...]], _Prepared] = OrderedDict()
        self._names = itertools.count(1)
        self._pending_close: list[bytes] = []
        #: When this connection last went back to a pool (monotonic seconds).
        self.returned_at: float | None = None

    # -- state ---------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._broken or getattr(self._raw, "_sock", None) is None

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        if bool(value) != self._autocommit and self.in_transaction:
            raise InterfaceError("cannot change autocommit inside a transaction")
        self._autocommit = bool(value)

    @property
    def transaction_status(self) -> str:
        """``"I"`` idle, ``"T"`` in a transaction, ``"E"`` in a failed one."""

        status = getattr(self._raw, "_transaction_status", None)
        return status.decode("ascii") if isinstance(status, bytes) else "I"

    @property
    def in_transaction(self) -> bool:
        return self.transaction_status != "I"

    @property
    def backend_pid(self) -> int | None:
        data = getattr(self._raw, "_backend_key_data", None)
        return int.from_bytes(data[:4], "big") if data else None

    # -- the protocol --------------------------------------------------------

    def _guard(self) -> Any:
        if self.closed:
            raise ConnectionLost("the connection is closed")
        return self._raw

    def _fail(self, exc: BaseException) -> BaseException:
        error = translate_error(exc)
        if isinstance(error, ConnectionLost):
            self._broken = True
        if isinstance(error, DatabaseError) and error.sqlstate in {"26000", "0A000"}:
            # A prepared statement the server no longer has, or whose plan a
            # schema change invalidated: forget them all and plan afresh.
            self._prepared.clear()
            self._uses.clear()
        return error

    def _begin_bytes(self) -> bytes:
        """``BEGIN`` if this statement opens a transaction, sent with the statement."""

        if self._autocommit or self.in_transaction:
            return b""
        return _BEGIN_MESSAGES

    def _close_bytes(self, raw: Any) -> bytes:
        if not self._pending_close:
            return b""
        out = bytearray()
        while self._pending_close:
            name = self._pending_close.pop()
            out += _message(b"C", b"S" + name)
            raw._statement_nums.discard(name)
        return bytes(out)

    def _send_and_read(self, raw: Any, payload: bytearray | bytes, context: Any) -> None:
        sock = raw._sock
        try:
            sock.write(payload)
            sock.write(_SYNC)
            sock.flush()
        except (OSError, ValueError, AttributeError) as exc:
            import pg8000.exceptions

            raise pg8000.exceptions.InterfaceError("network error") from exc
        _read_messages(raw, context)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        """Run one statement in one round trip."""

        raw = self._guard()
        from pg8000.core import NULL_BYTE, Context

        values = tuple(params)
        oids = tuple(param_oid(value) for value in values)
        key = (sql, oids)
        prepared = self._prepared.get(key)
        context = Context(sql)
        name = NULL_BYTE
        try:
            encoded = _encode_values(raw, values)
            payload = bytearray(self._close_bytes(raw))
            payload += self._begin_bytes()
            if prepared is not None:
                self._prepared.move_to_end(key)
                context.columns = prepared.columns
                context.input_funcs = prepared.input_funcs
                if prepared.columns is not None:
                    context.rows = []
                payload += _bind_message(prepared.name, encoded)
            else:
                uses = self._uses.get(key, 0) + 1
                self._uses[key] = uses
                if uses >= PREPARE_THRESHOLD:
                    name = f"pheasant_{next(self._names)}".encode("ascii") + NULL_BYTE
                payload += _parse_message(name, sql, oids, raw._client_encoding)
                payload += _message(b"D", b"S" + name)
                payload += _bind_message(name, encoded)
            payload += _EXECUTE
            self._send_and_read(raw, payload, context)
        except Exception as exc:  # noqa: BLE001 - normalized
            raise self._fail(exc) from exc
        if prepared is None and name != NULL_BYTE:
            self._uses.pop(key, None)
            raw._statement_nums.add(name)
            self._prepared[key] = _Prepared(name, context.columns, context.input_funcs)
            if len(self._prepared) > PREPARED_MAX:
                _, evicted = self._prepared.popitem(last=False)
                self._pending_close.append(evicted.name)
        if len(self._uses) > 4 * PREPARED_MAX:
            self._uses.clear()
        return self._result(context)

    @staticmethod
    def _result(context: Any) -> Result:
        columns = None
        if context.columns:
            columns = [str(column["name"]) for column in context.columns]
        rows = context.rows if context.rows is not None else []
        rowcount = context.row_count
        return Result(columns, rows, -1 if rowcount is None else int(rowcount))

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> int:
        """Run one statement once per parameter tuple; the summed row count.

        One Parse, then Bind/Execute per row, one Sync per block of
        :data:`EXECUTEMANY_BLOCK_ROWS` rows -- psycopg's pipeline, not one
        round trip per row. The first failing row ends the whole call (and,
        outside autocommit, fails the transaction), as before.
        """

        rows = [tuple(row) for row in seq]
        if not rows:
            return 0
        raw = self._guard()
        from pg8000.core import NULL_BYTE, Context

        width = len(rows[0])
        oids: list[int] = []
        for column in range(width):
            kinds = {param_oid(row[column]) for row in rows if row[column] is not None}
            oids.append(kinds.pop() if len(kinds) == 1 else _UNKNOWN)
        total = 0
        try:
            payload = bytearray(self._close_bytes(raw))
            payload += self._begin_bytes()
            payload += _parse_message(NULL_BYTE, sql, tuple(oids), raw._client_encoding)
            payload += _message(b"D", b"S" + NULL_BYTE)
            pending = 0
            context = Context(sql)
            for row in rows:
                payload += _bind_message(NULL_BYTE, _encode_values(raw, row))
                payload += _EXECUTE
                pending += 1
                if pending >= EXECUTEMANY_BLOCK_ROWS or len(payload) >= EXECUTEMANY_BLOCK_BYTES:
                    self._send_and_read(raw, payload, context)
                    total += max(0, context.row_count or 0)
                    context = Context(sql, columns=context.columns, input_funcs=context.input_funcs)
                    payload = bytearray()
                    pending = 0
            if pending:
                self._send_and_read(raw, payload, context)
                total += max(0, context.row_count or 0)
        except Exception as exc:  # noqa: BLE001 - normalized
            raise self._fail(exc) from exc
        return total

    def execute_script(self, script: str) -> Result:
        """Run text with no parameters over the simple protocol (several statements allowed)."""

        raw = self._guard()
        try:
            if not self._autocommit and not self.in_transaction:
                raw.execute_simple("BEGIN")
            context = raw.execute_simple(script)
        except Exception as exc:  # noqa: BLE001 - normalized
            raise self._fail(exc) from exc
        return self._result(context)

    def commit(self) -> None:
        if self.closed or not self.in_transaction:
            return
        try:
            context = self._raw.execute_simple("COMMIT")
        except Exception as exc:  # noqa: BLE001 - normalized
            raise self._fail(exc) from exc
        del context

    def rollback(self) -> None:
        """Roll back, sending nothing when there is nothing to roll back."""

        if self.closed or not self.in_transaction:
            return
        try:
            self._raw.execute_simple("ROLLBACK")
        except Exception as exc:  # noqa: BLE001 - normalized
            raise self._fail(exc) from exc

    def ping(self) -> None:
        """One round trip: an empty query, the cheapest thing a server answers."""

        try:
            self._guard().execute_simple("")
        except Exception as exc:  # noqa: BLE001 - normalized
            raise self._fail(exc) from exc

    def close(self) -> None:
        raw, self._broken = self._raw, True
        try:
            raw.close()
        except Exception:  # noqa: BLE001 - closing a broken connection is fine
            pass

    # -- conveniences for maintenance code, scripts and tests ------------------

    def query(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Cursor:
        """Run pheasant-portable SQL (``?`` / ``:name``) and return a cursor-like result."""

        from pheasant.persistence.sql import POSTGRES

        compiled = POSTGRES.compile(sql)
        values = compiled.bind(params)
        if values:
            return Cursor(self.execute(compiled.text, values))
        # No parameters: the simple protocol, which also runs several
        # statements at once (a maintenance script).
        return Cursor(self.execute_script(compiled.text))

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()


class Cursor:
    """A finished result shaped like a DB-API cursor: ``fetchone`` / ``fetchall`` / iteration."""

    def __init__(self, result: Result):
        self._result = result
        self._position = 0

    @property
    def description(self) -> list[tuple[str]] | None:
        if self._result.columns is None:
            return None
        return [(name,) for name in self._result.columns]

    @property
    def rowcount(self) -> int:
        return self._result.rowcount

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._position >= len(self._result.rows):
            return None
        row = self._result.rows[self._position]
        self._position += 1
        return tuple(row)

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows = self._result.rows[self._position :]
        self._position = len(self._result.rows)
        return [tuple(row) for row in rows]

    def __iter__(self):
        return iter(self.fetchall())


# The pool imports this module lazily, so importing it here, last, is no cycle.
from pheasant.persistence.pg_pool import (  # noqa: E402
    DEFAULT_POOL_TIMEOUT,
    IDLE_CHECK_SECONDS,
    ConnectionPool,
)
