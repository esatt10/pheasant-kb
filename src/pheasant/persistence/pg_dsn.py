"""PostgreSQL DSNs, TLS and the socket a connection runs over.

Part of :mod:`pheasant.persistence.pg_driver` (re-exported there). pheasant's
driver reads the DSN itself rather than through libpq: a ``postgresql://`` URL
or a libpq ``key=value`` string, ``PG*`` environment fallbacks, ``sslmode``
mapped without weakening verification, and every option it would otherwise
ignore refused by name (``docs/configuration.md#postgresql-dsns``).
"""

from __future__ import annotations

import os
import socket
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from pheasant.persistence.pg_errors import InterfaceError

#: Seconds a connect may take when the DSN names no ``connect_timeout``.
#: libpq waits forever; a pool waiting forever on a blackholed host is a hung
#: request, so pheasant has always passed a timeout where it mattered.
DEFAULT_CONNECT_TIMEOUT = 10.0

#: TCP keepalives on every connection, so an idle connection keeps its network
#: flow alive (Azure expires idle flows after four minutes by default) and a
#: dead peer is detected rather than found by the next statement. The values
#: libpq was given before, applied as socket options.
KEEPALIVE_IDLE_SECONDS = 60
KEEPALIVE_INTERVAL_SECONDS = 10
KEEPALIVE_COUNT = 3


_SSLMODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}

#: libpq keys pheasant reads. Anything else is refused by name rather than
#: silently ignored: a DSN that says `target_session_attrs=read-write` and is
#: quietly connected to a standby is worse than a startup error.
_SUPPORTED_KEYS = {
    "host",
    "hostaddr",
    "port",
    "dbname",
    "user",
    "password",
    "sslmode",
    "sslrootcert",
    "sslcert",
    "sslkey",
    "sslpassword",
    "application_name",
    "connect_timeout",
    "options",
    # Accepted for compatibility: keepalives are always on, with these values.
    "keepalives",
    "keepalives_idle",
    "keepalives_interval",
    "keepalives_count",
    "client_encoding",
    "gssencmode",
}


@dataclass(frozen=True)
class ConnectParams:
    """One parsed DSN. ``repr`` never shows the password."""

    host: str = "localhost"
    port: int = 5432
    user: str = "postgres"
    database: str | None = None
    password: str | None = field(default=None, repr=False)
    sslmode: str = "prefer"
    sslrootcert: str | None = None
    sslcert: str | None = None
    sslkey: str | None = None
    sslpassword: str | None = field(default=None, repr=False)
    application_name: str | None = None
    connect_timeout: float | None = None
    options: str | None = None
    hostaddr: str | None = None

    @property
    def unix_socket(self) -> str | None:
        """The socket path when ``host`` is a directory, as libpq reads it."""

        if self.host.startswith("/"):
            return f"{self.host.rstrip('/')}/.s.PGSQL.{self.port}"
        return None

    def redacted(self) -> str:
        """Something safe to put in a log line or an error."""

        return f"postgresql://{self.user}@{self.host}:{self.port}/{self.database or ''}"


def _conninfo_pairs(text: str) -> list[tuple[str, str]]:
    """libpq ``key=value`` pairs, with single-quoted values and ``\\`` escapes."""

    pairs: list[tuple[str, str]] = []
    position, length = 0, len(text)
    while position < length:
        while position < length and text[position].isspace():
            position += 1
        if position >= length:
            break
        start = position
        while position < length and text[position] not in "= \t\n":
            position += 1
        key = text[start:position]
        while position < length and text[position].isspace():
            position += 1
        if position >= length or text[position] != "=":
            raise InterfaceError(f"invalid DSN: missing '=' after {key!r}")
        position += 1
        while position < length and text[position].isspace():
            position += 1
        value_chars: list[str] = []
        if position < length and text[position] == "'":
            position += 1
            while True:
                if position >= length:
                    raise InterfaceError("invalid DSN: unterminated quoted value")
                char = text[position]
                if char == "\\" and position + 1 < length:
                    value_chars.append(text[position + 1])
                    position += 2
                    continue
                if char == "'":
                    position += 1
                    break
                value_chars.append(char)
                position += 1
        else:
            while position < length and not text[position].isspace():
                char = text[position]
                if char == "\\" and position + 1 < length:
                    value_chars.append(text[position + 1])
                    position += 2
                    continue
                value_chars.append(char)
                position += 1
        pairs.append((key, "".join(value_chars)))
    return pairs


def parse_dsn(dsn: str, *, environ: Mapping[str, str] | None = None) -> ConnectParams:
    """A ``postgresql://`` URL or libpq ``key=value`` string as :class:`ConnectParams`.

    Fills what the DSN leaves out from the ``PG*`` environment variables, as
    libpq did (``PGHOST``, ``PGPORT``, ``PGUSER``, ``PGPASSWORD``,
    ``PGDATABASE``, ``PGSSLMODE``, ``PGSSLROOTCERT``, ``PGAPPNAME``,
    ``PGCONNECT_TIMEOUT``). Refuses a key it would otherwise ignore.
    """

    env = os.environ if environ is None else environ
    text = (dsn or "").strip()
    values: dict[str, str] = {}
    if text.startswith(("postgresql://", "postgres://")):
        parts = urlsplit(text)
        netloc = parts.netloc
        if "@" in netloc:
            credentials, hostport = netloc.rsplit("@", 1)
            if ":" in credentials:
                user, password = credentials.split(":", 1)
                values["password"] = unquote(password)
            else:
                user = credentials
            if user:
                values["user"] = unquote(user)
        else:
            hostport = netloc
        if "," in hostport:
            raise InterfaceError("DSN names several hosts; pheasant connects to exactly one")
        if hostport.startswith("["):
            close = hostport.find("]")
            if close < 0:
                raise InterfaceError("invalid DSN: unterminated IPv6 address")
            host = hostport[1:close]
            rest = hostport[close + 1 :]
            port = rest[1:] if rest.startswith(":") else ""
        elif ":" in hostport:
            host, port = hostport.rsplit(":", 1)
        else:
            host, port = hostport, ""
        if host:
            values["host"] = unquote(host)
        if port:
            values["port"] = port
        database = unquote(parts.path.lstrip("/"))
        if database:
            values["dbname"] = database
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            values[key] = value
    elif text:
        values.update(_conninfo_pairs(text))
    unknown = sorted(set(values) - _SUPPORTED_KEYS)
    if unknown:
        raise InterfaceError(
            f"DSN option(s) {', '.join(unknown)} are not supported by pheasant's PostgreSQL "
            "driver; supported: " + ", ".join(sorted(_SUPPORTED_KEYS))
        )

    def pick(key: str, env_key: str, default: str | None = None) -> str | None:
        value = values.get(key)
        if value not in (None, ""):
            return value
        value = env.get(env_key)
        return value if value not in (None, "") else default

    sslmode = (pick("sslmode", "PGSSLMODE", "prefer") or "prefer").lower()
    if sslmode not in _SSLMODES:
        raise InterfaceError(f"invalid sslmode {sslmode!r}; one of {', '.join(sorted(_SSLMODES))}")
    port_text = pick("port", "PGPORT", "5432") or "5432"
    try:
        port = int(port_text)
    except ValueError:
        raise InterfaceError(f"invalid port {port_text!r} in DSN") from None
    timeout_text = pick("connect_timeout", "PGCONNECT_TIMEOUT")
    connect_timeout: float | None = None
    if timeout_text:
        try:
            connect_timeout = float(timeout_text)
        except ValueError:
            raise InterfaceError(f"invalid connect_timeout {timeout_text!r}") from None
        if connect_timeout <= 0:
            connect_timeout = None
    user = pick("user", "PGUSER")
    if not user:
        import getpass

        user = getpass.getuser()
    return ConnectParams(
        host=pick("host", "PGHOST", "localhost") or "localhost",
        hostaddr=values.get("hostaddr") or None,
        port=port,
        user=user,
        database=pick("dbname", "PGDATABASE"),
        password=pick("password", "PGPASSWORD"),
        sslmode=sslmode,
        sslrootcert=pick("sslrootcert", "PGSSLROOTCERT"),
        sslcert=values.get("sslcert") or None,
        sslkey=values.get("sslkey") or None,
        sslpassword=values.get("sslpassword") or None,
        application_name=pick("application_name", "PGAPPNAME"),
        connect_timeout=connect_timeout,
        options=values.get("options") or None,
    )


def _ssl_context(params: ConnectParams) -> ssl.SSLContext | bool | None:
    """pg8000's ``ssl_context`` for one ``sslmode``.

    ``None`` is pg8000's own "try TLS without verifying, else plain", which is
    libpq's ``prefer`` (and ``allow``, which differs only in trying plain
    first). ``require`` encrypts without verifying -- unless a root
    certificate is named, which libpq treats as ``verify-ca``.
    ``verify-full`` is never weakened: no CA file means the system's.
    """

    mode = params.sslmode
    if mode == "disable":
        return False
    if mode in {"allow", "prefer"}:
        return None
    root = params.sslrootcert
    if mode == "require" and not root:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    else:
        if mode == "require":
            mode = "verify-ca"
        if root and root != "system":
            path = Path(root).expanduser()
            if not path.is_file():
                raise InterfaceError(f"sslrootcert {root!r} does not exist")
            context = ssl.create_default_context(cafile=str(path))
        elif root == "system":
            context = ssl.create_default_context()
        else:
            default = Path("~/.postgresql/root.crt").expanduser()
            context = ssl.create_default_context(cafile=str(default) if default.is_file() else None)
        context.check_hostname = mode == "verify-full"
        context.verify_mode = ssl.CERT_REQUIRED
    if params.sslcert:
        context.load_cert_chain(
            str(Path(params.sslcert).expanduser()),
            str(Path(params.sslkey).expanduser()) if params.sslkey else None,
            password=params.sslpassword,
        )
    return context


def apply_keepalives(sock: socket.socket) -> None:
    """TCP keepalives with idle 60 s / interval 10 s / count 3 where the OS allows.

    Linux and macOS take all three as socket options. Windows takes idle and
    interval through ``SIO_KEEPALIVE_VALS`` (its probe count is fixed by the
    OS, 10); recent Windows also accepts the three options, which are tried
    first.
    """

    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    applied = False
    for name, value in (
        ("TCP_KEEPIDLE", KEEPALIVE_IDLE_SECONDS),
        ("TCP_KEEPALIVE", KEEPALIVE_IDLE_SECONDS),  # macOS spelling of KEEPIDLE
        ("TCP_KEEPINTVL", KEEPALIVE_INTERVAL_SECONDS),
        ("TCP_KEEPCNT", KEEPALIVE_COUNT),
    ):
        option = getattr(socket, name, None)
        if option is None:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, option, value)
            applied = True
        except OSError:
            continue
    if not applied and hasattr(socket, "SIO_KEEPALIVE_VALS"):
        try:
            sock.ioctl(  # type: ignore[attr-defined]
                socket.SIO_KEEPALIVE_VALS,  # type: ignore[attr-defined]
                (1, KEEPALIVE_IDLE_SECONDS * 1000, KEEPALIVE_INTERVAL_SECONDS * 1000),
            )
        except OSError:  # pragma: no cover - a platform refusing it keeps SO_KEEPALIVE
            pass


def _startup_options(options: str | None, extra: Mapping[str, str] | None) -> dict[str, str]:
    params: dict[str, str] = {}
    pieces = [options] if options else []
    for name, value in (extra or {}).items():
        pieces.append(f"-c {name}={value}")
    if pieces:
        params["options"] = " ".join(pieces)
    return params
