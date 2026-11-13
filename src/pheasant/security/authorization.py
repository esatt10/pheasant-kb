"""Transport-independent application roles. Local operator calls stay trusted.

An authenticated request never obtains privileges from asserted tool arguments.
The manifest in ``permissions`` is an explicit inventory, not a verb heuristic.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextvars import ContextVar, copy_context
from typing import Any

from pheasant.security.identity import Identity, current_identity

logger = logging.getLogger("pheasant.authorization")

ROLE_PERMISSIONS = {
    "Reader": frozenset({"knowledge.use", "keys.self"}),
    "Contributor": frozenset(
        {
            "knowledge.use",
            "keys.self",
            "sources.manage",
            "artifacts.manage",
            "tuning.manage",
            "evaluation.manage",
            "settings.manage",
            "jobs.manage",
        }
    ),
    "Admin": frozenset(
        {
            "knowledge.use",
            "keys.self",
            "sources.manage",
            "artifacts.manage",
            "tuning.manage",
            "evaluation.manage",
            "settings.manage",
            "jobs.manage",
            "security.manage",
            "infrastructure.manage",
        }
    ),
}
_REFRESH: ContextVar[Callable[[Identity], Identity | None] | None] = ContextVar(
    "pheasant_identity_refresh", default=None
)


class PermissionDenied(ValueError):
    status = 403
    code = "PERMISSION_DENIED"
    retryable = False

    def __init__(self, permission: str):
        self.permission = permission
        super().__init__(f"permission required: {permission}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "code": self.code,
            "status": self.status,
            "retryable": False,
            "permission": self.permission,
        }


def permissions(identity: Identity | None) -> frozenset[str]:
    if identity is None:
        return frozenset()
    granted = frozenset().union(*(ROLE_PERMISSIONS.get(r, frozenset()) for r in identity.roles))
    return granted if identity.permissions is None else granted.intersection(identity.permissions)


def effective_identity() -> Identity | None:
    identity = current_identity()
    refresh = _REFRESH.get()
    return refresh(identity) if identity is not None and refresh is not None else identity


def allowed(permission: str) -> bool:
    # No request context means a local CLI/indexer/operator, not an anonymous
    # HTTP caller: the front-door guard refuses those before dispatch.
    return current_identity() is None or permission in permissions(effective_identity())


def require_permission(permission: str) -> None:
    if not allowed(permission):
        identity = current_identity()
        logger.warning(
            "denied principal=%s token=%s permission=%s",
            identity.principal if identity else "-",
            identity.token_id if identity else "-",
            permission,
        )
        raise PermissionDenied(permission)


def scoped_principal(asserted: str | None, groups: list[str] | None = None):
    identity = current_identity()
    if identity is not None:
        return identity.principal, []  # directory/config groups are expanded by the ACL layer
    return asserted, groups


def background_target(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Capture identity and its revalidation callback before starting a thread."""
    context = copy_context()
    return lambda *args, **kwargs: context.run(fn, *args, **kwargs)


def require_configuration_change(current: dict, candidate: dict) -> None:
    """Contributors edit application settings; only admins edit the perimeter."""
    if allowed("security.manage"):
        return
    safe = (
        "assistant.retrieval",
        "assistant.workflow_options",
        "assistant.workflow",
        "assistant.max_context_chunks",
        "assistant.max_facts",
        "assistant.max_output_tokens",
        "search.ranking",
        "tuning",
        "evaluation",
        "artifacts.learning",
        "memory",
    )

    def compare(before, after, prefix=""):
        if before == after:
            return
        if isinstance(before, dict) or isinstance(after, dict):
            before = before if isinstance(before, dict) else {}
            after = after if isinstance(after, dict) else {}
            for key in before.keys() | after.keys():
                compare(before.get(key), after.get(key), f"{prefix}.{key}".lstrip("."))
            return
        if isinstance(before, list) or isinstance(after, list):
            before = before if isinstance(before, list) else []
            after = after if isinstance(after, list) else []
            for index in range(max(len(before), len(after))):
                compare(
                    before[index] if index < len(before) else None,
                    after[index] if index < len(after) else None,
                    f"{prefix}.{index}",
                )
            return
        field = prefix.rsplit(".", 1)[-1]
        protected = field in {
            "provider",
            "credentials",
            "connection",
            "api_key",
            "secret",
            "command",
        }
        protected = protected or field.endswith(("_env", "_url", "_path", "_token", "_endpoint"))
        if not protected and any(prefix == p or prefix.startswith(p + ".") for p in safe):
            return
        raise PermissionDenied("security.manage")

    compare(current, candidate)
