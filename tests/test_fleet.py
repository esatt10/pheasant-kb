"""Several knowledge bases on one operator's resources.

Offline: two real regions in one process, each with its own state, sharing a
fleet directory (a SQLite file here, a database on the shared server in a
deployment). Sign-in is the fixture `test_entra_identity.py` builds.

What must hold: a slot is claimed by a row changing and by one caller only;
the claimer administers the new knowledge base and nothing else carries over
(its Readers and Contributors are its own); when nothing is free the answer is
a refusal and a request, never a deployment; only the operator answers a
request or sees cost and other people's knowledge bases; and a region outside
a fleet opens no directory at all.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("cryptography")

from fastapi.testclient import TestClient  # noqa: E402

from pheasant.api.app import create_app  # noqa: E402
from pheasant.config.schema import PheasantConfig  # noqa: E402
from pheasant.persistence import fleet_store  # noqa: E402
from pheasant.telemetry.usage import METER  # noqa: E402
from tests.test_entra_identity import BASE, CLIENT, TENANT, Tenant  # noqa: E402

ORG = "99999999-8888-7777-6666-555555555555"
OPERATOR = "00000000-0000-0000-0000-0000000000a0"


def _oid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def _config(tmp_path: Path, slot: str, *, claimed: bool, fleet: bool = True) -> PheasantConfig:
    root = tmp_path / slot
    for name in ("ws", "state", "exports"):
        (root / name).mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "pheasant": {
            "name": slot,
            "description": f"Knowledge base {slot}",
            "defaults": "standalone",
            "workspace_root": str(root / "ws"),
            "state_path": str(root / "state"),
            "exports_path": str(root / "exports"),
        },
        "observability": {"usage": True},
        "storage": {"knowledge_base_limit_gb": 50},
        "models": {
            "chat": [{"id": "m", "provider": "azure", "model": "m", "input_price": 2.0}],
        },
        "security": {
            "api_auth": {"token_env": "PHEASANT_API_TOKEN"},
            "entra": {
                "enabled": True,
                "tenant_id": TENANT,
                "client_id": CLIENT,
                "public_url": f"https://{slot}.example.test",
                "delegated_access": True,
                "role_assignments": {OPERATOR: "Admin"},
            },
        },
        "sources": [],
    }
    if fleet:
        data["fleet"] = {
            "enabled": True,
            "slot": slot,
            "url": f"https://{slot}.example.test",
            "claimed": claimed,
        }
    config = PheasantConfig.model_validate(data)
    config.artifacts.index_on_save = False
    return config


@pytest.fixture(autouse=True)
def _fleet_directory(fresh_database: Callable[[], str], monkeypatch: pytest.MonkeyPatch) -> None:
    """The directory every stack in a test shares: a database of its own."""

    monkeypatch.setenv("PHEASANT_FLEET_DATABASE_URL", fresh_database())


@pytest.fixture
def tenant(monkeypatch: pytest.MonkeyPatch) -> Tenant:
    from pheasant.security import entra

    fake = Tenant()
    monkeypatch.setattr(entra, "_http_json", fake.http)
    return fake


@pytest.fixture
def fleet(tmp_path: Path, tenant: Tenant, monkeypatch: pytest.MonkeyPatch):
    """Region ``kb-01`` in use, ``kb-02`` and ``kb-03`` provisioned and empty."""

    monkeypatch.setenv("PHEASANT_API_TOKEN", "shared-admin")
    apps, clients = {}, {}
    for slot in ("kb-01", "kb-02", "kb-03"):
        app = create_app(
            _config(tmp_path, slot, claimed=slot == "kb-01"),
            config_path=str(tmp_path / slot / "pheasant.yaml"),
        )
        apps[slot] = app
        clients[slot] = TestClient(app, base_url=BASE, follow_redirects=False)
        clients[slot].__enter__()
    yield apps, clients
    for slot, client in clients.items():
        client.__exit__(None, None, None)
        apps[slot].state.engine.close()
    METER.unbind()
    fleet_store.close_all()


def _person(tenant: Tenant, name: str, n: int, tid: str = TENANT) -> dict[str, str]:
    signed = tenant.sign(
        tenant.claims(
            oid=_oid(n),
            preferred_username=name,
            roles=[],
            tid=tid,
            iss=f"https://login.microsoftonline.com/{tid}/v2.0",
        )
    )
    return {"Authorization": f"Bearer {signed}"}


def _operator(tenant: Tenant) -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + tenant.sign(tenant.claims(oid=OPERATOR, preferred_username="op@host.example", roles=[]))
    }


def _customer_admin(clients, tenant: Tenant) -> dict[str, str]:
    """kim@contoso.com: Admin of kb-01, from a connected customer directory."""

    op = _operator(tenant)
    first = clients["kb-01"]
    assert (
        first.post("/access/organizations", json={"tenant_id": ORG}, headers=op).status_code == 200
    )
    granted = first.post(
        "/access/grants",
        json={"email": "kim@contoso.com", "role": "Admin", "organization": ORG},
        headers=op,
    )
    assert granted.status_code == 200, granted.text
    return _person(tenant, "kim@contoso.com", 20, ORG)


def test_every_stack_registers_and_the_operator_sees_all_of_them(fleet, tenant):
    _, clients = fleet
    seen = clients["kb-01"].get("/fleet", headers=_operator(tenant)).json()
    assert seen["capacity"] == {"slots": 3, "claimed": 1, "free": 2}
    assert [(kb["slot"], kb["status"]) for kb in seen["knowledge_bases"]] == [
        ("kb-01", "claimed"),
        ("kb-02", "free"),
        ("kb-03", "free"),
    ]
    first = seen["knowledge_bases"][0]
    assert first["current"] is True and first["name"] == "Knowledge base kb-01"
    assert first["url"] == "https://kb-01.example.test" and first["size"]["limit_gb"] == 50
    assert seen["operator"] is True and seen["can_create"] is True and seen["can_resolve"] is True


def test_an_admin_claims_a_slot_and_administers_only_that(fleet, tenant):
    _, clients = fleet
    kim = _customer_admin(clients, tenant)
    lee = _person(tenant, "lee@contoso.com", 21, ORG)
    # Before the claim, kb-02 knows nobody from that directory.
    assert clients["kb-02"].get("/auth/me", headers=kim).status_code == 401

    created = clients["kb-01"].post(
        "/fleet/knowledge-bases",
        json={"name": "Research", "description": "Lab notes", "admins": ["Lee@Contoso.com"]},
        headers=kim,
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["knowledge_base"]["slot"] == "kb-02" and body["slots_free"] == 1
    assert body["sign_in_url"] == f"https://kb-02.example.test/auth/login?org={ORG}"
    assert [a["email"] for a in body["knowledge_base"]["admins"]] == [
        "kim@contoso.com",
        "lee@contoso.com",
    ]

    second = clients["kb-02"]
    # The claimer and the admin they named are Admins there; nobody else is.
    for person in (kim, lee):
        assert second.get("/auth/me", headers=person).json()["role"] == "Admin"
    stranger = _person(tenant, "sam@contoso.com", 22, ORG)
    assert second.get("/auth/me", headers=stranger).status_code == 401
    # The claimer is known by object id: someone else carrying her sign-in
    # name is not her, and neither is the same name in another directory.
    impostor = _person(tenant, "kim@contoso.com", 99, ORG)
    assert second.get("/auth/me", headers=impostor).status_code == 401
    elsewhere = _person(tenant, "kim@contoso.com", 20)
    assert second.get("/auth/me", headers=elsewhere).status_code == 401

    # Readers and Contributors are the new knowledge base's own.
    granted = second.post(
        "/access/grants", json={"email": "sam@contoso.com", "role": "Reader"}, headers=kim
    )
    assert granted.status_code == 200, granted.text
    assert second.get("/auth/me", headers=stranger).json()["role"] == "Reader"
    assert clients["kb-01"].get("/auth/me", headers=stranger).status_code == 401
    assert clients["kb-03"].get("/auth/me", headers=kim).status_code == 401

    # Kim sees the two she administers and not the free slot's details.
    mine = clients["kb-01"].get("/fleet", headers=kim).json()
    assert [kb["slot"] for kb in mine["knowledge_bases"]] == ["kb-01", "kb-02"]
    assert mine["capacity"]["free"] == 1 and mine["operator"] is False
    assert all(kb["usage"] is None for kb in mine["knowledge_bases"]) and mine["currency"] is None
    duplicate = clients["kb-01"].post(
        "/fleet/knowledge-bases", json={"name": "research"}, headers=kim
    )
    assert duplicate.status_code == 422


def test_when_nothing_is_free_the_answer_is_a_refusal_and_a_request(fleet, tenant):
    _, clients = fleet
    kim, op = _customer_admin(clients, tenant), _operator(tenant)
    first = clients["kb-01"]
    for name in ("Research", "Sales"):
        assert (
            first.post("/fleet/knowledge-bases", json={"name": name}, headers=kim).status_code
            == 200
        )
    full = first.post("/fleet/knowledge-bases", json={"name": "Legal"}, headers=kim)
    assert full.status_code == 409 and full.json()["code"] == "FLEET_NO_CAPACITY"
    assert "capacity request" in full.json()["detail"]
    assert first.get("/fleet", headers=kim).json()["can_create"] is False

    filed = first.post(
        "/fleet/requests", json={"quantity": 2, "reason": "Legal and Finance teams"}, headers=kim
    )
    assert filed.status_code == 200 and filed.json()["request"]["status"] == "open"
    request_id = filed.json()["request"]["id"]
    # Asking provisioned nothing.
    assert first.get("/fleet", headers=op).json()["capacity"]["slots"] == 3

    answer = {"status": "approved", "note": "two more slots on Friday"}
    refused = first.patch(f"/fleet/requests/{request_id}", json=answer, headers=kim)
    assert refused.status_code == 403 and refused.json()["code"] == "FLEET_OPERATOR_REQUIRED"
    resolved = first.patch(f"/fleet/requests/{request_id}", json=answer, headers=op)
    assert resolved.status_code == 200
    seen = first.get("/fleet", headers=kim).json()["requests"]
    assert [(r["status"], r["note"], r["resolved_by"]) for r in seen] == [
        ("approved", "two more slots on Friday", "op@host.example")
    ]
    assert first.post("/fleet/requests", json={"kind": "cpu"}, headers=kim).status_code == 422


def test_two_claims_for_the_last_slot_cannot_both_win(fleet):
    apps, _ = fleet
    store = fleet_store.open_store(apps["kb-01"].state.config)
    admins = [{"tenant_id": ORG, "email": "a@contoso.com", "subject": ""}]
    won = [
        fleet_store.claim(store, name=name, description="", admins=admins, claimed_by="a")
        for name in ("One", "Two", "Three")
    ]
    assert [w["slot"] if w else None for w in won] == ["kb-02", "kb-03", None]
    # A claimed row is never re-registered as free by its stack restarting.
    fleet_store.register(
        store,
        slot="kb-02",
        kb_id="kb-02",
        url="u",
        claimed=False,
        name=None,
        version="x",
        limit_gb=1,
    )
    assert fleet_store.get_slot(store, "kb-02")["status"] == "claimed"
    assert fleet_store.get_slot(store, "kb-02")["name"] == "One"


def test_the_operator_reads_every_knowledge_bases_usage_in_one_place(fleet, tenant):
    apps, clients = fleet
    kim = _customer_admin(clients, tenant)
    clients["kb-01"].post("/fleet/knowledge-bases", json={"name": "Research"}, headers=kim)
    # Tokens spent in kb-02, by whoever: the meter mirrors them to the directory.
    METER.bind_for(apps["kb-02"].state.config, apps["kb-02"].state.state)
    METER.model_call("chat", "m", 1_000_000, 0)
    METER.operation("answer", 1.2)
    seen = clients["kb-01"].get("/fleet", headers=_operator(tenant)).json()
    research = next(kb for kb in seen["knowledge_bases"] if kb["slot"] == "kb-02")
    assert research["name"] == "Research"
    assert research["usage"]["tokens"] == 1_000_000 and research["usage"]["questions"] == 1
    assert research["usage"]["cost"] == pytest.approx(2.0) and seen["currency"] == "USD"
    assert research["usage"]["answer_p95_ms"] == 2000


def test_only_an_admin_reaches_the_fleet(fleet, tenant):
    _, clients = fleet
    op, first = _operator(tenant), clients["kb-01"]
    first.post(
        "/access/grants", json={"email": "bob@example.com", "role": "Contributor"}, headers=op
    )
    bob = _person(tenant, "bob@example.com", 5)
    for call in (
        lambda: first.get("/fleet", headers=bob),
        lambda: first.post("/fleet/knowledge-bases", json={"name": "Mine"}, headers=bob),
        lambda: first.post("/fleet/requests", json={}, headers=bob),
    ):
        response = call()
        assert response.status_code == 403 and response.json()["code"] == "PERMISSION_DENIED"


def test_a_region_outside_a_fleet_opens_no_directory(tmp_path, tenant, monkeypatch):
    monkeypatch.setenv("PHEASANT_API_TOKEN", "shared-admin")
    app = create_app(
        _config(tmp_path, "solo", claimed=False, fleet=False),
        config_path=str(tmp_path / "solo" / "pheasant.yaml"),
    )
    with TestClient(app, base_url=BASE) as client:
        refused = client.get("/fleet", headers={"Authorization": "Bearer shared-admin"})
        assert refused.status_code == 404 and refused.json()["code"] == "FLEET_NOT_ENABLED"
        assert fleet_store.open_store(app.state.config) is None
        assert not (tmp_path / "fleet.db").exists()
        assert fleet_store.slot_admins(app.state.config) == []
    app.state.engine.close()
    METER.unbind()
