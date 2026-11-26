"""Install first-class extension routes on the region's normal HTTP surface."""

from fastapi import FastAPI
from fastapi.routing import APIRoute

from pheasant.extensions import API_VERSION, installed_extensions


def register_extension_routes(app: FastAPI, services) -> None:
    @app.get("/extensions")
    def list_extensions() -> dict:
        return {
            "api_version": API_VERSION,
            "extensions": [
                {"id": extension.id, "ui_routes": list(extension.ui_routes)}
                for extension in installed_extensions()
            ],
        }

    def operations(routes) -> set[tuple[str, str]]:
        return {
            (method, route.path)
            for route in routes
            if isinstance(route, APIRoute)
            for method in route.methods or ()
        }

    registered = operations(app.routes)
    for extension in installed_extensions():
        if extension.register_http is None:
            if extension.http_permissions:
                raise RuntimeError(
                    f"Extension {extension.id} declares HTTP permissions without routes"
                )
            continue
        start = len(app.routes)
        extension.register_http(app, services)
        contributed = operations(app.routes[start:])
        reserved = any(path.split("/")[1] in {"auth", "internal", "mcp"} for _, path in contributed)
        if reserved or contributed & registered or contributed != set(extension.http_permissions):
            raise RuntimeError(f"Invalid or unclassified HTTP routes in extension {extension.id}")
        registered.update(contributed)
