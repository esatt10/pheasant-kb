"""Authorize a resolved route without parsing its body or scanning routes per request."""

from __future__ import annotations

from functools import wraps
from typing import Any

from fastapi.routing import APIRoute
from starlette.responses import JSONResponse

from pheasant.security.authorization import PermissionDenied, require_permission
from pheasant.security.permissions import HTTP_PERMISSIONS


def install_authorization(app: Any) -> None:
    async def denied(_request, exc):
        return JSONResponse({"detail": str(exc), **exc.as_dict()}, status_code=403)

    app.add_exception_handler(PermissionDenied, denied)
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        original = route.app
        path = route.path

        def protect(handler, template):
            @wraps(handler)
            async def guarded(scope, receive, send):
                if not template.startswith(("/internal/", "/auth/")) and template != "/mcp":
                    permission = HTTP_PERMISSIONS.get((scope["method"], template), "unclassified")
                    try:
                        require_permission(permission)
                    except PermissionDenied as exc:
                        await JSONResponse({"detail": str(exc), **exc.as_dict()}, status_code=403)(
                            scope, receive, send
                        )
                        return
                await handler(scope, receive, send)

            return guarded

        route.app = protect(original, path)
