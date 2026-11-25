from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pheasant import extensions
from pheasant.api import extension_routes


def test_extension_discovery_rejects_incompatible_protocol(monkeypatch):
    entry = SimpleNamespace(
        name="example", load=lambda: lambda: extensions.Extension("example", api_version=2)
    )
    monkeypatch.setattr(extensions, "entry_points", lambda **_: [entry])
    with pytest.raises(RuntimeError, match="incompatible Pheasant extension: example"):
        extensions.installed_extensions.__wrapped__()


def test_optional_http_contribution_and_discovery(monkeypatch):
    app = FastAPI()
    monkeypatch.setattr(extension_routes, "installed_extensions", lambda: ())
    extension_routes.register_extension_routes(app, None)
    client = TestClient(app)
    assert client.get("/extensions").json() == {"api_version": 1, "extensions": []}
    assert client.get("/example/report").status_code == 404

    def register(app, _services):
        app.get("/example/report")(lambda: {"report": True})

    manifest = extensions.Extension(
        "example",
        1,
        register_http=register,
        http_permissions={("GET", "/example/report"): "knowledge.use"},
        ui_routes=("/example",),
    )
    monkeypatch.setattr(extension_routes, "installed_extensions", lambda: (manifest,))
    app = FastAPI()
    extension_routes.register_extension_routes(app, None)
    client = TestClient(app)
    assert client.get("/example/report").json() == {"report": True}
    assert client.get("/extensions").json()["extensions"] == [
        {"id": "example", "ui_routes": ["/example"]}
    ]

    collision = FastAPI()
    register(collision, None)
    with pytest.raises(RuntimeError, match="unclassified HTTP routes"):
        extension_routes.register_extension_routes(collision, None)
