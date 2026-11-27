"""Digests, identifiers, clocks and refusals every harness module shares.

Identity is a digest wherever it can be: a revision, an artifact, a payload
and an operation are all named by what they contain, so two replicas that
hold the same bytes agree on the name without coordinating, and a changed
payload cannot hide behind a reused name.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from pheasant.services.errors import ServiceError

#: The journal and record schema a run is pinned to. A run started under one
#: version is replayed by a reader for that version (see ``coordinator``).
WORKFLOW_VERSION = 2
SCHEMA_VERSION = 1
POLICY_VERSION = "harness-policy-1"


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_text(text: str) -> str:
    return digest_bytes(text.encode("utf-8"))


def digest_value(value: Any) -> str:
    return digest_text(canonical(value))


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def now() -> float:
    return time.time()


def loads(text: str | None, default: Any = None) -> Any:
    if text in (None, ""):
        return default
    return json.loads(text)


# ---------------------------------------------------------------------------
# Refusals. Each carries a stable code; the HTTP and MCP adapters both read it.
# ---------------------------------------------------------------------------


class HarnessError(ServiceError):
    code = "HARNESS_INVALID_REQUEST"


class NotFound(HarnessError):
    status = 404
    code = "HARNESS_NOT_FOUND"


class AccessDenied(HarnessError):
    status = 403
    code = "HARNESS_ACCESS_DENIED"


class RunConflict(HarnessError):
    """One client key, two different requests."""

    status = 409
    code = "HARNESS_RUN_CONFLICT"


class VersionConflict(HarnessError):
    status = 409
    code = "HARNESS_VERSION_CONFLICT"


class StaleFence(HarnessError):
    """A worker whose lease was taken over tried to advance the run."""

    status = 409
    code = "HARNESS_STALE_FENCE"


class QueueFull(HarnessError):
    status = 429
    code = "HARNESS_QUEUE_FULL"
    retryable = True

    def __init__(self, message: str, retry_after: int = 5):
        super().__init__(message)
        self.retry_after = retry_after

    def as_dict(self) -> dict[str, object]:
        return {**super().as_dict(), "retry_after_seconds": self.retry_after}


class Unsupported(HarnessError):
    status = 422
    code = "HARNESS_OPERATION_UNSUPPORTED"


# ---------------------------------------------------------------------------
# Result classification (§2): every tool result names which of these it is,
# so truncation, a timeout or an access filter never reads as an empty success.
# ---------------------------------------------------------------------------

OUTCOMES = (
    "success",
    "partial",
    "denied",
    "invalid_input",
    "version_conflict",
    "transient_error",
    "permanent_error",
    "unknown_effect",
)


def outcome(kind: str, **fields: Any) -> dict[str, Any]:
    if kind not in OUTCOMES:
        raise ValueError(f"unknown outcome {kind}")
    return {"outcome": kind, **fields}
