"""`python -m pheasant.deployment.database`: what deployments call before pheasant.

The Container Apps kits waited for Postgres with `python -c 'import psycopg ...'`
and created slot databases the same way; once psycopg left the image, the wait
never ended and the create failed silently. These pin the command that
replaced both, so no deployment file imports a driver again.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import pytest

from pheasant.deployment import database

DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()
postgres = pytest.mark.skipif(not DSN, reason="set PHEASANT_TEST_POSTGRES_DSN to run")


def test_a_database_name_is_an_identifier_never_sql() -> None:
    for name in ("x; DROP DATABASE pheasant", 'a"b', "1abc", "", "a" * 64, "pheasant-kb"):
        with pytest.raises(ValueError, match="not a database name"):
            database.ensure("postgresql://u@127.0.0.1:1/db", [name])


def test_wait_gives_up_on_a_server_that_never_answers() -> None:
    pytest.importorskip("pg8000")
    started = time.monotonic()
    assert not database.wait(
        "postgresql://u:p@127.0.0.1:1/db", timeout=1.0, interval=0.2, connect_timeout=0.5
    )
    assert time.monotonic() - started < 5.0


def test_the_module_runs_and_says_what_is_missing() -> None:
    env = {k: v for k, v in os.environ.items() if k != "PHEASANT_DATABASE_URL"}
    result = subprocess.run(
        [sys.executable, "-m", "pheasant.deployment.database", "wait", "--timeout", "1"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode != 0 and "PHEASANT_DATABASE_URL is not set" in result.stderr


@postgres
def test_wait_returns_at_once_and_ensure_creates_only_what_is_missing() -> None:
    from pheasant.persistence import pg_driver

    assert database.wait(DSN, timeout=10)
    name = f"pheasant_ensure_{uuid.uuid4().hex[:8]}"
    try:
        assert database.ensure(DSN, [name, name]) == [name]
        assert database.ensure(DSN, [name]) == []
        env = dict(os.environ, PHEASANT_ADMIN_DATABASE_URL=DSN)
        result = subprocess.run(
            [sys.executable, "-m", "pheasant.deployment.database", "ensure",
             "--dsn-env", "PHEASANT_ADMIN_DATABASE_URL", name],
            capture_output=True, text=True, env=env, timeout=60,
        )  # fmt: skip
        assert result.returncode == 0, result.stderr
    finally:
        with pg_driver.connect(os.environ["PHEASANT_DATABASE_URL"], autocommit=True) as conn:
            conn.execute_script(f'DROP DATABASE IF EXISTS "{name}"')
