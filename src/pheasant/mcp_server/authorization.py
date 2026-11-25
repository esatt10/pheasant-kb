"""Authorize tools and resources before their bodies execute, and list only those."""

from __future__ import annotations

import functools
import inspect
from typing import Any

from pheasant.extensions import extension_permissions
from pheasant.security.authorization import allowed, require_permission, scoped_principal
from pheasant.security.permissions import MCP_PERMISSIONS


def authorized(fn):
    signature = inspect.signature(fn)
    has_principal = "principal" in signature.parameters
    permission = extension_permissions("mcp", MCP_PERMISSIONS).get(fn.__name__, "unclassified")

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


def list_only_callable_tools(mcp: Any) -> None:
    """``tools/list`` shows each caller the tools its role may call.

    Every tool is already refused at call time by the permission
    ``MCP_PERMISSIONS`` names for it (:func:`authorized`). Listing
    all of them anyway handed a Reader's agent every tool, most of which it
    could only be refused by, and an agent picks from what it is shown. So
    the list is the call rule read ahead of time: an Admin sees every tool,
    a Contributor everything but ``security.manage``, and a Reader the
    ``knowledge.use`` tools. One predicate (``allowed``) decides both, so a
    listed tool is one the caller may call. A tool missing from the table is
    ``unclassified``, which no role holds, so it is hidden as it is refused.
    With no caller bound (stdio, the local operator) everything is listed,
    as everything may be called.

    It is an instance override of the SDK's ``list_tools``, which its
    ``tools/list`` handler calls; ``tools/call`` still finds a hidden tool and
    refuses it with the permission it needs, rather than "unknown tool".
    """

    every_tool = mcp.list_tools
    permissions = extension_permissions("mcp", MCP_PERMISSIONS)

    async def list_tools() -> list[Any]:
        return [
            tool
            for tool in await every_tool()
            if allowed(permissions.get(tool.name, "unclassified"))
        ]

    mcp.list_tools = list_tools
