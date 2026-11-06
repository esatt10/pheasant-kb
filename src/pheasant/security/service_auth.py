"""The internal boundaries' credentials: a shared token, or a managed identity.

Each internal call (api -> graph, indexer -> worker, api -> indexer landing)
used to read its shared token straight from the environment on both ends,
in six places. This is the one rule for all of them, so moving a boundary to
managed identity is configuration rather than six edits that can disagree:

* **Sending:** the token variable when it is set -- unchanged -- otherwise,
  with ``security.service_identity`` on, a managed-identity token for its
  ``audience``.
* **Receiving:** the token when one is configured and it matches; otherwise,
  with the identity on, an Entra token for the internal app that carries the
  boundary's app role. A token configured on the receiver still admits a
  caller holding it, so one side can move before the other.

The roles are what keep the boundaries separate under one app registration:
the worker's identity is never granted ``graph.query``, exactly as the worker
was never handed the graph token (``deployment/roles.py``).
"""

from __future__ import annotations

import hmac
import os
import threading
from typing import Any

from pheasant.security.entra import EntraUnavailable, EntraVerifier, managed_identity_token

GRAPH_QUERY = "graph.query"
WORKER_PREPARE = "worker.prepare"
INGESTION_LAND = "ingestion.land"

_VERIFIERS: dict[tuple[str, tuple[str, ...]], EntraVerifier] = {}
_LOCK = threading.Lock()


def identity_enabled(identity: Any) -> bool:
    return bool(identity is not None and getattr(identity, "enabled", False))


def outbound_token(token_env: str, identity: Any = None) -> str:
    """The bearer value to send across a boundary, or ``""`` when there is none."""

    token = os.environ.get(str(token_env or ""), "")
    if token or not identity_enabled(identity):
        return token
    try:
        return managed_identity_token(
            str(identity.audience), str(getattr(identity, "managed_identity_client_id", "") or "")
        )
    except EntraUnavailable:
        return ""


def inbound_configured(token_env: str, identity: Any = None) -> bool:
    """Whether this receiver has *any* way to admit a caller."""

    if os.environ.get(str(token_env or ""), ""):
        return True
    return identity_enabled(identity) and bool(_audiences(identity))


def _audiences(identity: Any) -> tuple[str, ...]:
    values = (getattr(identity, "app_id", ""), getattr(identity, "audience", ""))
    return tuple(sorted({str(v).strip() for v in values if str(v or "").strip()}))


def _verifier(identity: Any) -> EntraVerifier:
    key = (str(identity.tenant_id), _audiences(identity))
    with _LOCK:
        verifier = _VERIFIERS.get(key)
        if verifier is None:
            verifier = _VERIFIERS[key] = EntraVerifier(key[0], key[1])
        return verifier


def inbound_method(
    authorization: str | None, token_env: str, role: str, identity: Any = None
) -> str | None:
    """How a caller proved itself for ``role``, or ``None`` when it did not.

    ``authorization`` is the raw header (``Bearer <value>``) as HTTP and gRPC
    metadata both spell it.
    """

    scheme, _, supplied = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return None
    expected = os.environ.get(str(token_env or ""), "")
    # Bytes, as the API guard compares: a byte above 127 must not turn
    # compare_digest's TypeError into a 500 on input anyone can send.
    if expected and hmac.compare_digest(
        supplied.encode("utf-8", "surrogateescape"), expected.encode("utf-8", "surrogateescape")
    ):
        return "service_token"
    if not identity_enabled(identity):
        return None
    claims = _verifier(identity).verify(supplied)
    if claims is None:
        return None
    roles = claims.get("roles") or []
    return "managed_identity" if isinstance(roles, list) and role in roles else None
