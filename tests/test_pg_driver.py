"""pheasant's PostgreSQL driver layer (`persistence/pg_driver.py`) on its own.

The offline half pins what a deployment's DSN means, how a statement's
placeholders are rewritten, and which parameter types are sent -- each the
thing psycopg used to decide, and each a way to change behaviour silently by
changing drivers. The real-server half (``PHEASANT_TEST_POSTGRES_DSN``) counts
round trips, because "one statement, one round trip" and "executemany is
pipelined" are the properties that kept commits cheap at Azure's 10-40 ms, and
a loopback test of wall time cannot see them.
"""

from __future__ import annotations

import os
import ssl
import threading
import time
from typing import Any

import pytest

from pheasant.persistence import pg_driver
from pheasant.persistence.pg_liveness import is_disconnect, is_pool_timeout
from pheasant.persistence.sql import POSTGRES

DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()
postgres = pytest.mark.skipif(not DSN, reason="set PHEASANT_TEST_POSTGRES_DSN to run")


# ---------------------------------------------------------------------------
# DSNs
# ---------------------------------------------------------------------------


def test_the_url_every_deployment_builds_parses() -> None:
    params = pg_driver.parse_dsn("postgresql://pheasant:s3cret@postgres:5432/pheasant", environ={})
    assert (params.user, params.password, params.host, params.port, params.database) == (
        "pheasant",
        "s3cret",
        "postgres",
        5432,
        "pheasant",
    )
    assert params.sslmode == "prefer"  # libpq's default, and pg8000's own fallback
    assert "s3cret" not in repr(params) and "s3cret" not in params.redacted()


def test_escaped_credentials_ipv6_and_query_options() -> None:
    params = pg_driver.parse_dsn(
        "postgres://user%40org:p%3Ass%2Fw%40rd@[::1]:6543/db%20one"
        "?sslmode=require&application_name=probe&connect_timeout=7",
        environ={},
    )
    assert params.user == "user@org"
    assert params.password == "p:ss/w@rd"
    assert params.host == "::1" and params.port == 6543
    assert params.database == "db one"
    assert params.sslmode == "require"
    assert params.application_name == "probe"
    assert params.connect_timeout == 7.0


def test_libpq_key_value_dsns_parse_with_quoting() -> None:
    params = pg_driver.parse_dsn(
        "host=db.internal port=5433 dbname=pheasant user=svc password='it\\'s a \\\\ pass' "
        "sslmode=verify-full",
        environ={},
    )
    assert params.host == "db.internal" and params.port == 5433
    assert params.password == "it's a \\ pass"
    assert params.sslmode == "verify-full"


def test_an_option_the_driver_would_ignore_is_refused_by_name() -> None:
    with pytest.raises(pg_driver.InterfaceError, match="target_session_attrs"):
        pg_driver.parse_dsn("postgresql://u@h/db?target_session_attrs=read-write", environ={})
    with pytest.raises(pg_driver.InterfaceError, match="several hosts"):
        pg_driver.parse_dsn("postgresql://u@h1,h2/db", environ={})


def test_what_the_dsn_leaves_out_comes_from_the_pg_environment() -> None:
    params = pg_driver.parse_dsn(
        "postgresql:///pheasant",
        environ={
            "PGHOST": "envhost",
            "PGUSER": "envuser",
            "PGPASSWORD": "pw",
            "PGSSLMODE": "disable",
        },
    )
    assert (params.host, params.user, params.password, params.sslmode) == (
        "envhost",
        "envuser",
        "pw",
        "disable",
    )


def test_sslmodes_map_without_weakening_verification(tmp_path: Any) -> None:
    def context(mode: str, **extra: str) -> Any:
        query = "&".join(f"{key}={value}" for key, value in {"sslmode": mode, **extra}.items())
        return pg_driver._ssl_context(
            pg_driver.parse_dsn(f"postgresql://u@h/db?{query}", environ={})
        )

    assert context("disable") is False
    assert context("prefer") is None and context("allow") is None
    required = context("require")
    assert isinstance(required, ssl.SSLContext) and required.verify_mode == ssl.CERT_NONE
    full = context("verify-full")
    assert full.verify_mode == ssl.CERT_REQUIRED and full.check_hostname
    ca = context("verify-ca", sslrootcert="system")
    assert ca.verify_mode == ssl.CERT_REQUIRED and not ca.check_hostname
    # libpq: `require` with a root certificate verifies the chain.
    root = tmp_path / "root.crt"
    root.write_text(ssl.get_server_certificate(("localhost", 1)) if False else _self_signed())
    chained = context("require", sslrootcert=str(root))
    assert chained.verify_mode == ssl.CERT_REQUIRED
    with pytest.raises(pg_driver.InterfaceError, match="does not exist"):
        context("verify-full", sslrootcert=str(tmp_path / "missing.crt"))


def _self_signed() -> str:
    # A syntactically valid certificate (python's own test CA is not shipped),
    # generated once with `cryptography`, a core dependency.
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pheasant-test")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode()


# ---------------------------------------------------------------------------
# Statements and parameters
# ---------------------------------------------------------------------------


def test_placeholders_are_rewritten_outside_literals_identifiers_and_comments() -> None:
    compiled = POSTGRES.compile(
        "SELECT \"odd?name\", x::text FROM t WHERE a = ? AND b LIKE '%?%' "
        "AND c = ? -- trailing ? comment\nAND d = ?"
    )
    assert compiled.text == (
        "SELECT \"odd?name\", x::text FROM t WHERE a = $1 AND b LIKE '%?%' "
        "AND c = $2 -- trailing ? comment\nAND d = $3"
    )
    assert compiled.names is None
    # `%` is the server's again: nothing is escaped any more.
    assert POSTGRES.translate("SELECT 1 WHERE p LIKE '%/%'") == "SELECT 1 WHERE p LIKE '%/%'"


def test_named_placeholders_bind_by_name_and_reuse_their_number() -> None:
    compiled = POSTGRES.compile("SELECT :a, :b, :a WHERE x = :b")
    assert compiled.text == "SELECT $1, $2, $1 WHERE x = $2"
    assert compiled.bind({"a": 1, "b": "two"}) == (1, "two")
    with pytest.raises(TypeError):
        compiled.bind((1, 2))
    with pytest.raises(ValueError, match="mixes"):
        POSTGRES.compile("SELECT ?, :a")


def test_parameter_types_are_the_ones_psycopg_sent() -> None:
    """Measured against psycopg 3.3 on PostgreSQL 17 before it was removed:
    ``pg_typeof(%s)`` of each value. A different choice changes which
    operator a statement resolves to, so it is a behaviour change."""

    oid = pg_driver.param_oid
    assert oid("text") == 0 and oid(None) == 0  # untyped: the server infers
    assert oid(5) == 21 and oid(70_000) == 23 and oid(2**40) == 20 and oid(2**70) == 1700
    assert oid(1.5) == 701 and oid(True) == 16 and oid(b"ab") == 17
    assert oid([1, 2]) == 1005 and oid([2**40]) == 1016
    assert oid(["a"]) == 0 and oid([]) == 0


def test_the_pg8000_internals_the_driver_reads_still_exist() -> None:
    """pg_driver writes the protocol itself and reads replies with pg8000's
    handlers; an upgrade that renames one of these must fail here, not in a
    commit on a fleet."""

    pytest.importorskip("pg8000")
    import pg8000.converters
    import pg8000.core
    import pg8000.native

    for name in ("Context", "DATA_ROW", "READY_FOR_QUERY", "NULL_BYTE"):
        assert hasattr(pg8000.core, name), name
    for name in ("make_param", "string_in"):
        assert hasattr(pg8000.converters, name), name
    for name in ("execute_simple", "handle_messages", "close"):
        assert callable(getattr(pg8000.core.CoreConnection, name, None)), name
    context = pg8000.core.Context("SELECT 1")
    for attribute in ("columns", "rows", "row_count", "input_funcs", "error"):
        assert hasattr(context, attribute), attribute


def test_disconnects_and_pool_timeouts_are_told_apart_from_answers() -> None:
    assert is_disconnect(pg_driver.ConnectionLost("server closed the connection"))
    assert is_disconnect(pg_driver.OperationalError("terminating", sqlstate="57P01"))
    assert is_disconnect(pg_driver.OperationalError("broken", sqlstate="08006"))
    assert not is_disconnect(pg_driver.QueryCanceled("timeout", sqlstate="57014"))
    assert not is_disconnect(pg_driver.IntegrityError("dup", sqlstate="23505"))
    assert not is_disconnect(pg_driver.PoolTimeout("busy"))
    assert not is_disconnect(OSError("a file, not a database"))
    assert is_pool_timeout(pg_driver.PoolTimeout("busy"))
    assert pg_driver._error_class("42P01") is pg_driver.ProgrammingError
    assert pg_driver._error_class("53300") is pg_driver.OperationalError


def test_psycopg_is_not_a_dependency() -> None:
    import importlib.metadata as metadata

    from packaging.requirements import Requirement

    requirements = [Requirement(text) for text in metadata.requires("pheasant-kb") or []]
    names = {requirement.name.lower() for requirement in requirements}
    assert not names & {"psycopg", "psycopg-binary", "psycopg-pool", "pymupdf", "fitz"}
    core = {requirement.name.lower() for requirement in requirements if not requirement.marker}
    assert {"pg8000", "sqlalchemy"} <= core, "the state store's driver is a core dependency"


# ---------------------------------------------------------------------------
# A real server
# ---------------------------------------------------------------------------


class _Trips:
    """Count requests by counting flushes of the connection's socket."""

    def __init__(self, conn: pg_driver.Connection):
        self.count = 0
        sock = conn._raw._sock
        real = sock.flush

        def flush() -> None:
            self.count += 1
            real()

        sock.flush = flush  # type: ignore[method-assign]


@postgres
def test_a_statement_is_one_round_trip_and_executemany_is_pipelined() -> None:
    with pg_driver.connect(os.environ["PHEASANT_DATABASE_URL"]) as conn:
        conn.execute_script(
            "DROP TABLE IF EXISTS pg_driver_trips; CREATE TABLE pg_driver_trips(id bigint, v text)"
        )
        conn.commit()
        trips = _Trips(conn)
        conn.execute("INSERT INTO pg_driver_trips VALUES ($1, $2)", (1, "a"))  # BEGIN rides along
        assert trips.count == 1
        rows = [(n, "x" * 40) for n in range(2, 302)]
        assert conn.executemany("INSERT INTO pg_driver_trips VALUES ($1, $2)", rows) == 300
        assert trips.count == 2
        assert conn.execute("SELECT count(*) FROM pg_driver_trips", ()).rows == [[301]]
        conn.commit()
        conn.execute_script("DROP TABLE pg_driver_trips")


@postgres
def test_values_round_trip_with_their_types() -> None:
    import datetime
    import decimal

    with pg_driver.connect(os.environ["PHEASANT_DATABASE_URL"], autocommit=True) as conn:
        moment = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
        row = conn.execute(
            "SELECT $1::text, $2::bigint, $3::float8, $4::bool, $5::bytea, $6::numeric, "
            "$7::timestamptz, $8::text[], $9::bigint[], $10::jsonb, $11::text",
            (
                "it's 100% ünïcode",
                2**40,
                0.1,
                True,
                b"\x00\xff",
                decimal.Decimal("1.25"),
                moment,
                ["a,b", 'q"'],
                [1, 2],
                '{"k": [1, null]}',
                None,
            ),
        ).rows[0]
        assert row == [
            "it's 100% ünïcode",
            2**40,
            0.1,
            True,
            b"\x00\xff",
            decimal.Decimal("1.25"),
            moment,
            ["a,b", 'q"'],
            [1, 2],
            {"k": [1, None]},
            None,
        ]
        assert conn.execute("SELECT 1 = ANY($1), 'a' = ANY($2)", ([], ["a"])).rows == [
            [False, True]
        ]


@postgres
def test_a_failed_statement_leaves_a_connection_that_can_roll_back_and_go_on() -> None:
    with pg_driver.connect(os.environ["PHEASANT_DATABASE_URL"]) as conn:
        with pytest.raises(pg_driver.ProgrammingError) as missing:
            conn.execute("SELECT * FROM no_such_table_here", ())
        assert missing.value.sqlstate == "42P01"
        assert conn.transaction_status == "E"
        conn.rollback()
        assert conn.transaction_status == "I"
        assert conn.execute("SELECT 1", ()).rows == [[1]]


@postgres
def test_a_text_run_five_times_is_prepared_and_still_answers() -> None:
    with pg_driver.connect(os.environ["PHEASANT_DATABASE_URL"], autocommit=True) as conn:
        for value in range(8):
            assert conn.execute("SELECT $1::bigint + 1", (value,)).rows == [[value + 1]]
        assert len(conn._prepared) == 1
        # A schema change under a prepared statement: forgotten, re-planned.
        conn.execute_script(
            "DROP TABLE IF EXISTS pg_driver_prep; CREATE TABLE pg_driver_prep(a int)"
        )
        for _ in range(6):
            conn.execute("SELECT * FROM pg_driver_prep", ())
        conn.execute_script("ALTER TABLE pg_driver_prep ADD COLUMN b int")
        with pytest.raises(pg_driver.Error):
            conn.execute("SELECT * FROM pg_driver_prep", ())
        assert conn.execute("SELECT * FROM pg_driver_prep", ()).columns == ["a", "b"]
        conn.execute_script("DROP TABLE pg_driver_prep")


@postgres
def test_the_pool_bounds_connections_and_honours_each_callers_deadline() -> None:
    pool = pg_driver.ConnectionPool(DSN, max_size=2)
    try:
        first, second = pool.getconn(), pool.getconn()
        started = time.monotonic()
        with pytest.raises(pg_driver.PoolTimeout):
            pool.getconn(timeout=0.2)
        assert time.monotonic() - started < 1.0
        assert is_disconnect(pg_driver.PoolTimeout("x")) is False
        pool.putconn(first)
        reused = pool.getconn(timeout=0.2)
        assert reused is first
        pool.putconn(reused)
        pool.putconn(second)
        assert pool.stats["checked_out"] == 0 and pool.stats["idle"] <= 2
    finally:
        pool.close()


@postgres
def test_waiters_beyond_the_pool_complete_or_time_out_and_nothing_leaks() -> None:
    pool = pg_driver.ConnectionPool(DSN, max_size=2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def work() -> None:
        try:
            conn = pool.getconn(timeout=5)
        except pg_driver.PoolTimeout:
            with lock:
                outcomes.append("timeout")
            return
        try:
            conn.execute("SELECT pg_sleep(0.05)", ())
            conn.rollback()
        finally:
            pool.putconn(conn)
        with lock:
            outcomes.append("ok")

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    try:
        assert outcomes.count("ok") == 8
        assert pool.stats["checked_out"] == 0
        with pg_driver.connect(os.environ["PHEASANT_DATABASE_URL"], autocommit=True) as admin:
            name = pool._application_name
            idle_in_transaction = admin.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = $1 "
                "AND state = 'idle in transaction'",
                (name,),
            ).rows[0][0]
        assert idle_in_transaction == 0
    finally:
        pool.close()


def test_a_connect_to_a_blackholed_address_gives_up_on_time() -> None:
    """TEST-NET-1 routes nowhere: the connect must fail within its timeout,
    not hang a pool checkout behind it."""

    pytest.importorskip("pg8000")
    started = time.monotonic()
    with pytest.raises(pg_driver.ConnectionLost, match="could not connect"):
        pg_driver.connect("postgresql://u:p@192.0.2.1:5432/db", connect_timeout=0.5)
    assert time.monotonic() - started < 3.0


def test_the_pool_bounds_a_new_connection_by_the_callers_deadline() -> None:
    pytest.importorskip("sqlalchemy")
    pool = pg_driver.ConnectionPool("postgresql://u:p@192.0.2.1:5432/db?connect_timeout=30")
    try:
        started = time.monotonic()
        with pytest.raises(pg_driver.ConnectionLost):
            pool.getconn(timeout=0.5)
        assert time.monotonic() - started < 3.0
        assert pool.stats["checked_out"] == 0
    finally:
        pool.close()


@postgres
def test_a_statement_timeout_does_not_outlive_its_read() -> None:
    from pheasant.persistence.backends import PostgresBackend
    from pheasant.request_budget import DeadlineExceeded, activate

    backend = PostgresBackend(os.environ["PHEASANT_DATABASE_URL"], pool_size=1)
    try:
        with activate(time.monotonic() + 0.3), pytest.raises((DeadlineExceeded, pg_driver.Error)):
            backend.rows("SELECT pg_sleep(2)")
        assert backend.rows("SHOW statement_timeout")[0]["statement_timeout"] == "0"
    finally:
        backend.close()
