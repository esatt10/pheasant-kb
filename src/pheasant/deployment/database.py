"""What a deployment asks of its database before pheasant starts.

    python -m pheasant.deployment.database wait   [--dsn-env NAME] [--timeout S]
    python -m pheasant.deployment.database ensure [--dsn-env NAME] DATABASE...

Deployments used to ask with a one-line ``python -c 'import psycopg; ...'``,
so the startup command of every tier depended on which driver the image
happened to carry. When psycopg left the image, that line could never succeed:
the Container Apps tiers looped in ``wait`` until their startup probe killed
them, and a slot's database would never have been created, silently, since
that loop runs in the background with its output discarded. A deployment calls
pheasant now, and pheasant uses its own driver (:mod:`pheasant.persistence.pg_driver`).

``wait`` returns 0 once a login succeeds (each attempt bounded by
``--connect-timeout``) and 1 if ``--timeout`` runs out first; 0 waits forever.
``ensure`` creates each named database that does not exist, and returns 0 when
all of them do -- a racing ``CREATE`` that loses is success, because the
database is there. A name is an identifier, never SQL: anything but letters,
digits and ``_`` (63 at most, not starting with a digit) is refused.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

DEFAULT_DSN_ENV = "PHEASANT_DATABASE_URL"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")


def _dsn(name: str) -> str:
    dsn = os.environ.get(name, "").strip()
    if not dsn:
        raise SystemExit(f"{name} is not set")
    return dsn


def wait(
    dsn: str, *, timeout: float = 0.0, interval: float = 2.0, connect_timeout: float = 5.0
) -> bool:
    """Block until a login to ``dsn`` succeeds; ``False`` if ``timeout`` ran out."""

    from pheasant.persistence.pg_driver import connect

    started = time.monotonic()
    while True:
        try:
            connect(dsn, connect_timeout=connect_timeout, autocommit=True).close()
            return True
        except Exception:  # noqa: BLE001 - any failure is "not yet"
            pass
        if timeout and time.monotonic() - started >= timeout:
            return False
        time.sleep(interval)


def ensure(dsn: str, names: list[str]) -> list[str]:
    """Create each database in ``names`` that is missing; the ones created."""

    from pheasant.persistence import pg_driver

    bad = [name for name in names if not _NAME.fullmatch(name)]
    if bad:
        raise ValueError(f"not a database name: {', '.join(bad)}")
    created: list[str] = []
    with pg_driver.connect(dsn, autocommit=True, connect_timeout=5) as conn:
        for name in names:
            if conn.execute("SELECT 1 FROM pg_database WHERE datname = $1", (name,)).rows:
                continue
            try:
                conn.execute_script(f'CREATE DATABASE "{name}"')
                created.append(name)
            except pg_driver.Error as exc:
                if exc.sqlstate != "42P04":  # duplicate_database: another process won
                    raise
    return created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pheasant.deployment.database")
    commands = parser.add_subparsers(dest="command", required=True)
    waiting = commands.add_parser("wait", help="wait until the database accepts a login")
    waiting.add_argument("--dsn-env", default=DEFAULT_DSN_ENV)
    waiting.add_argument("--timeout", type=float, default=0.0, help="seconds; 0 waits forever")
    waiting.add_argument("--interval", type=float, default=2.0)
    waiting.add_argument("--connect-timeout", type=float, default=5.0)
    ensuring = commands.add_parser("ensure", help="create databases that do not exist")
    ensuring.add_argument("--dsn-env", default=DEFAULT_DSN_ENV)
    ensuring.add_argument("databases", nargs="+")
    args = parser.parse_args(argv)
    if args.command == "wait":
        ok = wait(
            _dsn(args.dsn_env),
            timeout=args.timeout,
            interval=args.interval,
            connect_timeout=args.connect_timeout,
        )
        return 0 if ok else 1
    try:
        created = ensure(_dsn(args.dsn_env), list(args.databases))
    except ValueError as exc:
        print(f"pheasant: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - the caller's loop retries
        print(f"pheasant: could not ensure databases: {exc}", file=sys.stderr)
        return 1
    for name in created:
        print(f"pheasant: created database {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
