"""Model prices an operator sets at runtime, applying to future calls only.

Acceptance:

1. Each call is costed when it is made, at the price in force then, and the
   cost is stored with its tokens: changing a price never reprices what was
   already spent, in the spend limit, the Usage report or anything read from
   the ledger, and a call metered before a change and written after keeps
   the old price.
2. Totals recorded before costs were stored are priced once (at the first
   start, or the first write to the row), at the price in force then, and
   stay so.
3. An override is set by catalog id, per field, cleared with null, kept with
   a history; refused for an unknown id or an impossible figure, and for
   anyone but an operator admin.
4. A stored cost can be added to and never lowered or cleared, on SQLite and
   on a real Postgres.
5. Both surfaces read and set the same prices.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pheasant.persistence import usage_ledger
from pheasant.persistence.state_store import StateStore
from pheasant.security.identity import ENTRA_TOKEN, Identity, bind_identity, reset_identity
from pheasant.services import ServiceContext, model_prices, usage
from pheasant.services.errors import InvalidRequest, OperatorRequired
from pheasant.telemetry import spend
from pheasant.telemetry.usage import METER, Totals
from tests.test_region_controls import _mcp, _mcp_body
from tests.test_region_controls import served as served  # noqa: F401 - shared fixture
from tests.test_usage_metering import _config

DAY = datetime.now(UTC).date().isoformat()
MILLION = 1_000_000


@pytest.fixture
def region(tmp_path: Path):
    config = _config(tmp_path)
    state = StateStore(tmp_path / "prices.db")
    state.migrate()
    METER.bind(state, "kb")
    spend.GUARD.bind(config, state)
    yield ServiceContext(config, state, None)
    METER.unbind(state)
    spend.GUARD.unbind(state)
    state.close()


@pytest.fixture
def operator():
    token = bind_identity(Identity("shared-token", "api_token", roles=("Admin",)))
    yield
    reset_identity(token)


def _stored(state: Any) -> dict[str, Any]:
    return {
        row["model"]: row["cost_micros"]
        for row in state.rows("SELECT model, cost_micros FROM usage_daily", ())
    }


def test_a_price_change_applies_to_later_calls_only(region, operator, monkeypatch):
    state = region.state
    monkeypatch.setattr(METER, "_maybe_flush", lambda: None)
    METER.model_call("chat", "gpt-6-luna", MILLION)  # $1 at the catalog's 1.0
    METER.flush()
    assert _stored(state)["gpt-6-luna"] == 1 * MILLION

    METER.model_call("chat", "gpt-6-luna", MILLION)  # metered at $1 ...
    model_prices.update(region, {"prices": {"gpt-6-luna": {"input": 10}}})
    METER.flush()  # ... and written after the change: still $1
    assert _stored(state)["gpt-6-luna"] == 2 * MILLION

    METER.model_call("chat", "gpt-6-luna", MILLION)  # $10 now
    METER.flush()
    assert _stored(state)["gpt-6-luna"] == 12 * MILLION
    assert spend.month_spend(region.config, state)["spent"] == pytest.approx(12.0)
    report = usage.describe(region, days=1)
    luna = next(m for m in report["by_model"] if m["model"] == "gpt-6-luna")
    assert luna["cost"] == pytest.approx(12.0) and report["totals"]["cost"] == pytest.approx(12.0)

    model_prices.update(region, {"prices": {"gpt-6-luna": {"input": 0.5}}})
    assert spend.month_spend(region.config, state)["spent"] == pytest.approx(12.0), (
        "lowering a price lowers nothing already spent"
    )


def test_totals_from_before_costs_were_stored_are_priced_once(region, operator):
    """A row with no stored cost is priced at the first start, at the price
    then, and a later change leaves it alone."""

    state, config = region.state, region.config
    usage_ledger.add(  # as a release before this one wrote it: no cost
        state,
        "kb",
        {(DAY, "chat", "gpt-6-luna", "", "a", "ui"): Totals(requests=1, input_tokens=3 * MILLION)},
    )
    usage_ledger.add(
        state,
        "kb",
        {(DAY, "chat", "gpt-6-sol", "", "a", "ui"): Totals(requests=1, input_tokens=MILLION)},
    )
    assert _stored(state) == {"gpt-6-luna": None, "gpt-6-sol": None}
    state.execute("DELETE FROM pheasant_schema_meta WHERE key='usage_cost_frozen_v1'")
    spend.GUARD.bind(config, state)  # the first start of this release
    assert _stored(state) == {"gpt-6-luna": 3 * MILLION, "gpt-6-sol": None}, (
        "unpriced stays unpriced"
    )
    model_prices.update(region, {"prices": {"gpt-6-luna": {"input": 50}}})
    assert spend.month_spend(config, state)["spent"] == pytest.approx(3.0)
    assert spend.month_spend(config, state)["unpriced_models"] == ["gpt-6-sol"]


def test_the_first_write_to_an_unpriced_row_prices_what_it_held(region):
    """The same freeze, inside the upsert, for a row nothing priced at start."""

    state = region.state
    usage_ledger.add(
        state,
        "kb",
        {(DAY, "chat", "gpt-6-luna", "", "b", "ui"): Totals(requests=1, input_tokens=2 * MILLION)},
    )
    usage_ledger.add(
        state,
        "kb",
        {
            (DAY, "chat", "gpt-6-luna", "", "b", "ui"): Totals(
                requests=1, input_tokens=MILLION, cost_micros=7 * MILLION
            )
        },
        price_of=lambda model: (4.0, None, None),
    )
    assert _stored(state)["gpt-6-luna"] == 2 * 4 * MILLION + 7 * MILLION


def _assert_cost_only_grows(state: Any) -> None:
    usage_ledger.add(
        state, "kb", {(DAY, "embedding", "e", "", "c", "ui"): Totals(requests=1, input_tokens=10)}
    )
    state.execute("UPDATE usage_daily SET cost_micros=5 WHERE model='e'")  # pricing a NULL row
    state.execute("UPDATE usage_daily SET cost_micros=6 WHERE model='e'")  # raising
    for statement in (
        "UPDATE usage_daily SET cost_micros=1 WHERE model='e'",
        "UPDATE usage_daily SET cost_micros=NULL WHERE model='e'",
    ):
        with pytest.raises(Exception, match="append-only"):
            state.execute(statement)
        if hasattr(state.backend, "rollback"):
            state.backend.rollback()
    assert _stored(state)["e"] == 6


def test_a_stored_cost_only_grows(region):
    _assert_cost_only_grows(region.state)


@pytest.mark.skipif(
    not os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip(),
    reason="set PHEASANT_TEST_POSTGRES_DSN to a throwaway database",
)
def test_costs_on_a_real_postgres(tmp_path):
    from pheasant.persistence.backends import open_backend

    state = StateStore(
        backend=open_backend(backend="postgres", dsn=os.environ["PHEASANT_TEST_POSTGRES_DSN"])
    )
    try:
        state.migrate()
        state.execute("ALTER TABLE usage_daily DISABLE TRIGGER usage_daily_append_only")
        state.execute("DELETE FROM usage_daily WHERE kb_id='kb'")
        state.execute("ALTER TABLE usage_daily ENABLE TRIGGER usage_daily_append_only")
        key = (DAY, "chat", "gpt-6-luna", "", "b", "ui")
        usage_ledger.add(state, "kb", {key: Totals(requests=1, input_tokens=2 * MILLION)})
        usage_ledger.add(
            state,
            "kb",
            {key: Totals(requests=1, input_tokens=MILLION, cost_micros=7 * MILLION)},
            price_of=lambda model: (4.0, 0.0, None),
        )
        assert _stored(state)["gpt-6-luna"] == 15 * MILLION
        usage_ledger.add(state, "kb", {key: Totals(requests=1, input_tokens=1)})  # unpriced call
        assert _stored(state)["gpt-6-luna"] == 15 * MILLION
        frozen_key = (DAY, "chat", "frozen", "", "b", "ui")
        usage_ledger.add(state, "kb", {frozen_key: Totals(requests=1, input_tokens=MILLION)})
        assert usage_ledger.freeze_costs(state, "kb", {"frozen": (2.0, None, None)}) == 1
        assert _stored(state)["frozen"] == 2 * MILLION
        _assert_cost_only_grows(state)
    finally:
        state.close()


def test_overrides_are_validated_kept_and_cleared(region, operator):
    described = model_prices.describe(region)
    luna = next(m for m in described["models"] if m["id"] == "gpt-6-luna")
    assert luna["catalog"] == {"input": 1.0, "output": 4.0, "cached_input": 0.1}
    assert luna["override"] is None and luna["effective"] == luna["catalog"]
    sol = next(m for m in described["models"] if m["id"] == "gpt-6-sol")
    assert sol["effective"] is None, "no stated price"

    changed = model_prices.update(
        region, {"prices": {"gpt-6-luna": {"output": 8}, "gpt-6-sol": {"input": 2, "output": 6}}}
    )
    luna = next(m for m in changed["models"] if m["id"] == "gpt-6-luna")
    assert luna["effective"] == {"input": 1.0, "output": 8.0, "cached_input": 0.1}, (
        "unnamed fields keep the catalog's"
    )
    assert [(h["model"], h["by"]) for h in changed["history"]][:2] in (
        [("gpt-6-sol", "shared-token"), ("gpt-6-luna", "shared-token")],
        [("gpt-6-luna", "shared-token"), ("gpt-6-sol", "shared-token")],
    )
    assert spend.GUARD.price_of("gpt-6-sol") == (2.0, 6.0, None), "every check reads it at once"

    cleared = model_prices.update(region, {"prices": {"gpt-6-luna": None}})
    luna = next(m for m in cleared["models"] if m["id"] == "gpt-6-luna")
    assert luna["override"] is None and luna["effective"]["output"] == 4.0
    assert cleared["history"][0]["to"]["output"] == 4.0

    for bad, message in (
        ({"prices": {"nope": {"input": 1}}}, "unknown model id"),
        ({"prices": {"gpt-6-luna": {"input": -1}}}, "between 0 and"),
        ({"prices": {"gpt-6-luna": {"input": "cheap"}}}, "is a number"),
        ({"prices": {"gpt-6-luna": {"tokens": 1}}}, "unknown price field"),
        ({"prices": {}}, "send"),
    ):
        with pytest.raises(InvalidRequest, match=message):
            model_prices.update(region, bad)


def test_prices_are_an_operator_admins(region):
    config = region.config
    config.security.entra.delegated_access = True
    config.security.entra.tenant_id = "t"
    token = bind_identity(
        Identity("ada@contoso.com", ENTRA_TOKEN, subject="s", roles=("Admin",), tenant_id="t")
    )
    try:
        with pytest.raises(OperatorRequired):
            model_prices.describe(region)
        with pytest.raises(OperatorRequired):
            model_prices.update(region, {"prices": {"gpt-6-luna": {"input": 0}}})
    finally:
        reset_identity(token)


ADMIN = {"Authorization": "Bearer admin-token"}
READER = {"Authorization": "Bearer reader-token"}


def test_both_surfaces_read_and_set_the_same_prices(served):
    app, client = served
    http = client.get("/models/prices", headers=ADMIN).json()
    over_mcp = json.loads(
        _mcp_body(_mcp(client, ADMIN, "get_model_prices", {}))["result"]["content"][0]["text"]
    )
    assert over_mcp == http
    put = client.put("/models/prices", json={"prices": {"gpt-6-luna": {"input": 3}}}, headers=ADMIN)
    assert put.status_code == 200
    set_ = _mcp_body(_mcp(client, ADMIN, "set_model_prices", {"prices": {"gpt-6-luna": None}}))
    luna = next(
        m
        for m in json.loads(set_["result"]["content"][0]["text"])["models"]
        if m["id"] == "gpt-6-luna"
    )
    assert luna["override"] is None
    assert client.get("/models/prices", headers=READER).status_code == 403
