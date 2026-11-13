"""Authorize tools and resources before their bodies execute."""

from __future__ import annotations

import functools
import inspect

from pheasant.security.authorization import require_permission, scoped_principal
from pheasant.security.permissions import MCP_PERMISSIONS


def authorized(fn):
    signature = inspect.signature(fn)
    has_principal = "principal" in signature.parameters
    permission = MCP_PERMISSIONS.get(fn.__name__, "unclassified")

    @functools.wraps(fn)
    def invoke(*args, **kwargs):
        require_permission(permission)
        if has_principal:
            bound = signature.bind_partial(*args, **kwargs)
            principal, groups = scoped_principal(
                bound.arguments.get("principal"), bound.arguments.get("principal_groups")
            )
            bound.arguments["principal"] = principal
            if "principal_groups" in signature.parameters:
                bound.arguments["principal_groups"] = groups
            args, kwargs = bound.args, bound.kwargs
        return fn(*args, **kwargs)

    return invoke
