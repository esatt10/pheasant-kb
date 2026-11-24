"""Access granted by the people who use a region, and customer organizations.

Offline, on the fixtures `test_entra_identity.py` builds: tokens are signed
with a local key and the only network hop answers with its JWKS. What runs is
the real guard, identity service, access service and both transports.

The rules under test: an Admin grants any role, a Contributor grants
Contributor or Reader in their own organization, a Reader grants nothing and
sees nothing; nobody changes their own access; a tenant nobody connected never
gets in; and with ``delegated_access`` off none of this exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("cryptography")

from fastapi.testclient import TestClient  # noqa: E402

from pheasant.api.app import create_app  # noqa: E402
from pheasant.persistence import access_grants  # noqa: E402
from pheasant.security.identity import Identity, bind_identity, reset_identity  # noqa: E402
from tests.test_entra_identity import BASE, CLIENT, TENANT, Tenant, _config  # noqa: E402

ORG = "99999999-8888-7777-6666-555555555555"
OTHER = "12121212-3434-5656-7878-909090909090"
OPERATOR = "00000000-0000-0000-0000-0000000000a0"


def _oid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def _token(tenant: Tenant, name: str, n: int, tid: str = TENANT, **claims: Any) -> dict[str, str]:
    """A bearer header for one person. No role claims: grants decide."""

    signed = tenant.sign(
        tenant.claims(
            oid=_oid(n),
            preferred_username=name,
            name=name.split("@")[0],
            roles=[],
            tid=tid,
            iss=f"https://login.microsoftonline.com/{tid}/v2.0",
            **claims,
        )
    )
    return {"Authorization": f"Bearer {signed}"}


@pytest.fixture
def tenant(monkeypatch: pytest.MonkeyPatch) -> Tenant:
    from pheasant.security import entra

    fake = Tenant()
    monkeypatch.setattr(entra, "_http_json", fake.http)
    return fake


@pytest.fixture
def region(tmp_path: Path, tenant: Tenant, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PHEASANT_API_TOKEN", "shared-admin")
    monkeypatch.setenv("PHEASANT_ENTRA_CLIENT_SECRET", "client-secret")
    config = _config(tmp_path, delegated_access=True, role_assignments={OPERATOR: "Admin"})
    config.artifacts.index_on_save = False
    app = create_app(config, config_path=str(tmp_path / "pheasant.yaml"))
    with TestClient(app, base_url=BASE, follow_redirects=False) as client:
        yield app, client
    app.state.engine.close()


def _operator(tenant: Tenant) -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + tenant.sign(tenant.claims(oid=OPERATOR, preferred_username="op@host.example", roles=[]))
    }


def _role(client: TestClient, headers: dict[str, str]) -> str | None:
    response = client.get("/auth/me", headers=headers)
    return response.json()["role"] if response.status_code == 200 else None


def test_nobody_gets_in_without_a_grant_and_the_operator_admin_always_does(region, tenant):
    _, client = region
    stranger = _token(tenant, "bob@example.com", 1)
    assert client.get("/auth/me", headers=stranger).status_code == 401
    # A role claim in the token is not a grant: claims are not consulted.
    claimed = {
        "Authorization": "Bearer "
        + tenant.sign(tenant.claims(oid=_oid(2), preferred_username="eve@example.com"))
    }
    assert client.get("/auth/me", headers=claimed).status_code == 401
    assert _role(client, _operator(tenant)) == "Admin"
    listed = client.get("/access", headers=_operator(tenant)).json()
    assert listed["grantable_roles"] == ["Reader", "Contributor", "Admin"]
    assert listed["operator_admins"] == [
        {"subject": OPERATOR, "role": "Admin", "tenant_id": TENANT}
    ]


def test_the_grant_ladder(region, tenant):
    _, client = region
    admin = _operator(tenant)
    bob = _token(tenant, "bob@example.com", 1)
    carol = _token(tenant, "carol@example.com", 3)
    dave = _token(tenant, "dave@example.com", 4)

    granted = client.post(
        "/access/grants", json={"email": "Bob@Example.com", "role": "contributor"}, headers=admin
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["status"] == "granted"
    assert granted.json()["grant"]["pending"] is True
    assert granted.json()["sign_in_url"] == f"{BASE}/auth/login"
    assert _role(client, bob) == "Contributor"

    # A Contributor grants Contributor and Reader, and not Admin.
    for email, role in (("carol@example.com", "Reader"), ("dave@example.com", "Contributor")):
        ok = client.post("/access/grants", json={"email": email, "role": role}, headers=bob)
        assert ok.status_code == 200, ok.text
    assert _role(client, carol) == "Reader" and _role(client, dave) == "Contributor"
    refused = client.post(
        "/access/grants", json={"email": "erin@example.com", "role": "Admin"}, headers=bob
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "ACCESS_ROLE_NOT_GRANTABLE"
    assert "Contributor may grant Reader or Contributor" in refused.json()["detail"]

    # A Reader neither grants nor sees who has access.
    for call in (
        lambda: client.get("/access", headers=carol),
        lambda: client.post(
            "/access/grants", json={"email": "x@example.com", "role": "Reader"}, headers=carol
        ),
    ):
        response = call()
        assert response.status_code == 403 and response.json()["code"] == "PERMISSION_DENIED"

    # First use bound each grant to the object id that proved it.
    people = {p["email"]: p for p in client.get("/access", headers=admin).json()["people"]}
    assert people["bob@example.com"]["subject"] == _oid(1)
    assert people["bob@example.com"]["pending"] is False


def test_a_grant_above_the_caller_cannot_be_changed_or_removed_by_them(region, tenant):
    _, client = region
    admin = _operator(tenant)
    bob = _token(tenant, "bob@example.com", 1)
    for email, role in (("bob@example.com", "Contributor"), ("alma@example.com", "Admin")):
        client.post("/access/grants", json={"email": email, "role": role}, headers=admin)
    assert _role(client, _token(tenant, "alma@example.com", 5)) == "Admin"
    alma = next(
        p
        for p in client.get("/access", headers=admin).json()["people"]
        if p["email"] == "alma@example.com"
    )
    # Demoting an Admin is taking Admin away, which a Contributor may not do.
    demote = client.post(
        "/access/grants", json={"email": "alma@example.com", "role": "Reader"}, headers=bob
    )
    remove = client.delete(f"/access/grants/{alma['id']}", headers=bob)
    assert demote.status_code == remove.status_code == 403
    assert _role(client, _token(tenant, "alma@example.com", 5)) == "Admin"
    # Nobody edits their own access, in either direction.
    own = client.post(
        "/access/grants", json={"email": "bob@example.com", "role": "Reader"}, headers=bob
    )
    assert own.status_code == 403 and "your own access" in own.json()["detail"]


def test_a_revoked_person_is_out_at_once_and_a_downgrade_reaches_their_keys(region, tenant):
    _, client = region
    admin = _operator(tenant)
    bob = _token(tenant, "bob@example.com", 1)
    client.post(
        "/access/grants", json={"email": "bob@example.com", "role": "Contributor"}, headers=admin
    )
    key = client.post("/auth/keys", json={"name": "ci"}, headers=bob).json()["key"]
    held = {"Authorization": f"Bearer {key}"}
    assert _role(client, held) == "Contributor"

    client.post(
        "/access/grants", json={"email": "bob@example.com", "role": "Reader"}, headers=admin
    )
    assert _role(client, held) == "Reader"
    assert client.post("/sync", headers=held).status_code == 403
    # Back up: the person follows their grant, a key keeps what it was minted with
    # at most, and never more than the grant.
    client.post(
        "/access/grants", json={"email": "bob@example.com", "role": "Contributor"}, headers=admin
    )
    assert _role(client, bob) == "Contributor" and _role(client, held) == "Contributor"

    grant = client.get("/access", headers=admin).json()["people"][0]
    assert (
        client.delete(f"/access/grants/{grant['id']}", headers=admin).json()["status"] == "revoked"
    )
    assert client.get("/auth/me", headers=bob).status_code == 401
    assert client.get("/auth/me", headers=held).status_code == 401


def test_a_recycled_address_does_not_inherit_access(region, tenant):
    _, client = region
    client.post(
        "/access/grants",
        json={"email": "bob@example.com", "role": "Reader"},
        headers=_operator(tenant),
    )
    assert _role(client, _token(tenant, "bob@example.com", 1)) == "Reader"  # binds oid 1
    assert client.get("/auth/me", headers=_token(tenant, "bob@example.com", 77)).status_code == 401
    # ...and the person keeps theirs through a rename.
    assert _role(client, _token(tenant, "robert@example.com", 1)) == "Reader"


def test_a_guest_signed_in_with_another_account_finds_the_grant_to_their_invited_address(
    region, tenant, caplog
):
    """Production: invited as one address, redeemed with a Microsoft account.

    The token's ``preferred_username`` is the account they signed in with and
    the invited address is only its ``email`` claim, so a grant made by that
    address never matched and every sign-in was a bare 401.
    """

    _, client = region
    client.post(
        "/access/grants",
        json={"email": "guest@gmail.example", "role": "Contributor"},
        headers=_operator(tenant),
    )
    guest = _token(tenant, "guest@outlook.example", 61, email="Guest@Gmail.example")
    assert _role(client, guest) == "Contributor"
    # Bound to the object id now, so the account keeps it whichever name it shows.
    assert _role(client, _token(tenant, "guest@outlook.example", 61)) == "Contributor"
    # A different person carrying the invited address as `email` is still refused.
    other = _token(tenant, "someone@outlook.example", 62, email="guest@gmail.example")
    assert client.get("/auth/me", headers=other).status_code == 401


def test_a_refused_sign_in_says_why_in_the_log(region, tenant, caplog):
    import logging

    _, client = region
    login = client.get("/auth/login", params={"next": "/"})
    query = parse_qs(urlsplit(login.headers["location"]).query)
    tenant.next_id_token = tenant.sign(
        tenant.claims(
            nonce=query["nonce"][0], oid=_oid(63), preferred_username="nobody@example.com", roles=[]
        )
    )
    with caplog.at_level(logging.WARNING):
        refused = client.get("/auth/callback", params={"code": "c", "state": query["state"][0]})
    assert refused.status_code == 401
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "sign-in refused" in logged and _oid(63) in logged and "nobody@example.com" in logged
    assert "sign-in did not complete" in logged and "not been granted access" in logged


def test_a_customer_organization_signs_in_only_once_connected(region, tenant):
    _, client = region
    admin = _operator(tenant)
    member = _token(tenant, "kim@contoso.com", 20, tid=ORG)
    assert client.get("/auth/me", headers=member).status_code == 401
    assert client.get("/auth/config").json()["organizations"] is False

    connected = client.post(
        "/access/organizations",
        json={"tenant_id": ORG.upper(), "name": "Contoso", "domains": ["Contoso.com"]},
        headers=admin,
    )
    assert connected.status_code == 200, connected.text
    org = connected.json()["organization"]
    assert org["sign_in_url"] == f"{BASE}/auth/login?org=contoso.com"
    assert org["admin_consent_url"].startswith(
        f"https://login.microsoftonline.com/{ORG}/adminconsent?client_id={CLIENT}"
    )
    assert client.get("/auth/config").json()["organizations"] is True
    # Connected is not granted: no default role, so members are added singly.
    assert client.get("/auth/me", headers=member).status_code == 401
    granted = client.post(
        "/access/grants",
        json={"email": "kim@contoso.com", "role": "Contributor", "organization": "contoso.com"},
        headers=admin,
    )
    assert granted.json()["sign_in_url"] == f"{BASE}/auth/login?org={ORG}"
    assert _role(client, member) == "Contributor"
    # The same name in the operator's tenant is a different person.
    assert client.get("/auth/me", headers=_token(tenant, "kim@contoso.com", 21)).status_code == 401
    # A token from a tenant nobody connected is refused whatever it claims.
    assert (
        client.get("/auth/me", headers=_token(tenant, "kim@contoso.com", 20, tid=OTHER)).status_code
        == 401
    )

    # The customer's Contributor manages their own organization, and only it.
    lee = client.post(
        "/access/grants", json={"email": "lee@contoso.com", "role": "Reader"}, headers=member
    )
    assert lee.status_code == 200 and lee.json()["grant"]["tenant_id"] == ORG
    outside = client.post(
        "/access/grants",
        json={"email": "x@host.example", "role": "Reader", "organization": "operator"},
        headers=member,
    )
    assert outside.status_code == 403
    seen = client.get("/access", headers=member).json()
    assert {p["tenant_id"] for p in seen["people"]} == {ORG} and seen["operator_admins"] == []
    assert (
        client.post("/access/organizations", json={"tenant_id": OTHER}, headers=member).status_code
        == 403
    )

    gone = client.delete(f"/access/organizations/{ORG}", headers=admin).json()
    assert gone == {"status": "disconnected", "tenant_id": ORG, "grants_removed": 2}
    assert client.get("/auth/me", headers=member).status_code == 401


def test_an_organization_default_role_admits_every_member_but_never_as_admin(region, tenant):
    _, client = region
    admin = _operator(tenant)
    refused = client.post(
        "/access/organizations", json={"tenant_id": ORG, "default_role": "Admin"}, headers=admin
    )
    assert refused.status_code == 422
    client.post(
        "/access/organizations", json={"tenant_id": ORG, "default_role": "Reader"}, headers=admin
    )
    assert _role(client, _token(tenant, "anyone@contoso.com", 30, tid=ORG)) == "Reader"
    client.post(
        "/access/grants",
        json={"email": "lead@contoso.com", "role": "Admin", "organization": ORG},
        headers=admin,
    )
    assert _role(client, _token(tenant, "lead@contoso.com", 31, tid=ORG)) == "Admin"


def test_sign_in_asks_which_organization_and_accepts_only_that_tenants_token(region, tenant):
    _, client = region
    admin = _operator(tenant)
    client.post(
        "/access/organizations",
        json={"tenant_id": ORG, "domains": ["contoso.com"], "default_role": "Reader"},
        headers=admin,
    )
    # With an organization connected, a bare sign-in asks; a named one does not.
    asked = client.get("/auth/login", params={"next": "/sources"})
    assert asked.status_code == 302
    choice = asked.headers["location"]
    assert choice.startswith(f"{BASE}/auth/choose?request=pht_")
    request = parse_qs(urlsplit(choice).query)["request"][0]
    assert "Organization domain" in client.get("/auth/choose", params={"request": request}).text
    unknown = client.get("/auth/login", params={"org": "fabrikam.com"})
    assert unknown.status_code == 401 and "not connected" in unknown.text

    chosen = client.post("/auth/choose", data={"request": request, "org": "contoso.com"})
    assert chosen.status_code == 302
    authorize = chosen.headers["location"]
    assert authorize.startswith(f"https://login.microsoftonline.com/{ORG}/oauth2/v2.0/authorize?")
    query = parse_qs(urlsplit(authorize).query)

    def id_token(tid: str) -> str:
        return tenant.sign(
            tenant.claims(
                nonce=query["nonce"][0],
                oid=_oid(40),
                preferred_username="kim@contoso.com",
                roles=[],
                tid=tid,
                iss=f"https://login.microsoftonline.com/{tid}/v2.0",
            )
        )

    # A token from the operator's tenant, returned to a sign-in that was sent
    # to the organization's, proves nothing.
    tenant.next_id_token = id_token(TENANT)
    wrong = client.get("/auth/callback", params={"code": "c", "state": query["state"][0]})
    assert wrong.status_code == 401

    again = client.get("/auth/login", params={"org": ORG, "next": "/sources"})
    query = parse_qs(urlsplit(again.headers["location"]).query)
    tenant.next_id_token = id_token(ORG)
    done = client.get("/auth/callback", params={"code": "c", "state": query["state"][0]})
    assert done.status_code == 302, done.text
    session = done.headers["location"].split("pheasant_session=", 1)[1]
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {session}"}).json()
    assert (me["principal"], me["role"], me["tenant_id"]) == ("kim@contoso.com", "Reader", ORG)
    # A choice is one use.
    assert client.post("/auth/choose", data={"request": request, "org": ""}).status_code == 401

    approved = client.get("/auth/callback", params={"admin_consent": "True", "tenant": ORG})
    assert approved.status_code == 200 and "Organization approved" in approved.text


def test_both_surfaces_refuse_in_the_same_words(region, tenant):
    from pheasant.services import access
    from pheasant.services.errors import AccessRoleNotGrantable

    app, client = region
    http = client.post(
        "/access/grants",
        json={"email": "x@example.com", "role": "Admin"},
        headers={"Authorization": "Bearer shared-admin"},
    )
    assert http.status_code == 200  # the shared token is an Admin credential
    from pheasant.mcp_server.access_tools import AccessTools

    class Tools(AccessTools):
        services = None

    tools = Tools()
    tools.services = _services(app)
    token = bind_identity(
        Identity("bob@example.com", "personal_key", roles=("Contributor",), tenant_id=TENANT)
    )
    try:
        with pytest.raises(AccessRoleNotGrantable) as mcp:
            tools.grant_access("y@example.com", "Admin")
        with pytest.raises(AccessRoleNotGrantable) as direct:
            access.grant_access(
                tools.services, access.GrantRequest(role="Admin", email="y@example.com")
            )
        assert str(mcp.value) == str(direct.value)
        assert tools.list_access()["grantable_roles"] == ["Reader", "Contributor"]
    finally:
        reset_identity(token)


def _services(app):
    from pheasant.services import ServiceContext

    return ServiceContext(config=app.state.config, state=app.state.state, searcher=None)


def test_without_delegated_access_nothing_changes(tmp_path, tenant, monkeypatch):
    monkeypatch.setenv("PHEASANT_API_TOKEN", "shared-admin")
    app = create_app(_config(tmp_path), config_path=str(tmp_path / "pheasant.yaml"))
    with TestClient(app, base_url=BASE, follow_redirects=False) as client:
        shared = {"Authorization": "Bearer shared-admin"}
        response = client.get("/access", headers=shared)
        assert response.status_code == 404
        assert response.json()["code"] == "ACCESS_NOT_DELEGATED"
        # Role claims still decide, exactly as before.
        claimed = {"Authorization": "Bearer " + tenant.sign(tenant.claims())}
        assert _role(client, claimed) == "Reader"
        described = client.get("/auth/config").json()
        assert described["delegated_access"] is False and described["organizations"] is False
        login = client.get("/auth/login")
        assert login.headers["location"].startswith(
            f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize?"
        )
    app.state.engine.close()


def test_a_warm_request_reads_no_grants(region, tenant, monkeypatch):
    app, client = region
    client.post(
        "/access/grants",
        json={"email": "bob@example.com", "role": "Reader"},
        headers=_operator(tenant),
    )
    bob = _token(tenant, "bob@example.com", 1)
    assert _role(client, bob) == "Reader"
    monkeypatch.setattr(
        access_grants, "rows", lambda *a, **k: pytest.fail("a warm request read the grants table")
    )
    for _ in range(50):
        assert _role(client, bob) == "Reader"
