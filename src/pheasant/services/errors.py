"""Refusals the transports translate, rather than a transport's own errors.

An operation cannot raise `HTTPException` — that would put FastAPI inside the
layer both an HTTP server and an MCP server call. It cannot raise the MCP
SDK's `ToolError` either, for the mirror-image reason. So it raises these, and
each adapter maps them to its own vocabulary: a status code on one side, a
`ToolError` at the SDK boundary on the other.

Every one subclasses ``ValueError``. That is deliberate and is what makes this
change safe to land incrementally: `mcp_server/server.py` already translates
`ValueError`/`KeyError` into an informative `ToolError`, and the HTTP surface
already turns them into a 400. A service refusal therefore behaves correctly
on both surfaces from the first commit, and the adapters can be taught the
richer mapping one at a time rather than all at once.

The **message is part of the contract**. "Unknown source: notes" is what an
agent reads when it mistypes a name, and an agent told only that something
failed will retry the same call. The conformance test asserts both surfaces
produce the same text, because two spellings of one refusal is the same defect
as two implementations of one operation, one level down.

**And so is the code.** A message is for a reader; a harness deciding whether
to retry needs something it can branch on, and branching on English is how a
client ends up matching ``"Unknown source"`` with a regex that a reworded
sentence silently breaks. Every refusal therefore carries a stable ``code``
and a ``retryable`` flag, and the two are separate answers to separate
questions: `SNAPSHOT_DRIFTED` is not retryable because repeating the call
cannot help, while `REGION_BUSY` is, and a caller that cannot tell them apart
either gives up on a transient failure or hammers a permanent one. The codes
are public API in the same way tool names are (rule 8) — additive evolution
only.
"""

from __future__ import annotations


class ServiceError(ValueError):
    """Base for every deliberate refusal an operation makes.

    ``status`` is a hint for an HTTP adapter, not a coupling: nothing in the
    layer imports a web framework, and an adapter is free to ignore it.

    ``code`` and ``retryable`` are the machine-readable half of the same
    refusal. They are class attributes rather than constructor arguments so
    that a subclass declares them once and every raise site gets them — a code
    a caller has to remember to pass is a code that is missing from the raise
    that mattered.
    """

    status: int = 400
    #: Stable, screaming-snake identity for this refusal. Public API: a client
    #: branches on it, so it is added to and deprecated, never renamed.
    code: str = "INVALID_REQUEST"
    #: Whether repeating the identical call could succeed. ``False`` for every
    #: refusal about the *request*; ``True`` only where the region is telling
    #: the caller to come back.
    retryable: bool = False

    def as_dict(self) -> dict[str, object]:
        """The refusal as a payload both adapters can marshal.

        One shape, so an agent reading an MCP error and a UI reading an HTTP
        body are looking at the same fields rather than at two renderings a
        reader has to reconcile.
        """

        return {
            "error": str(self),
            "code": self.code,
            "retryable": self.retryable,
            "status": self.status,
        }


class UnknownKnowledgeBase(ServiceError):
    status = 404
    code = "UNKNOWN_KNOWLEDGE_BASE"

    def __init__(self, requested: str) -> None:
        super().__init__(f"Unknown knowledge base: {requested}")
        self.requested = requested


class SourceNotFound(ServiceError):
    status = 404
    code = "UNKNOWN_SOURCE"

    def __init__(self, name: str) -> None:
        super().__init__(f"Unknown source: {name}")
        self.name = name


class NodeNotFound(ServiceError):
    status = 404
    code = "UNKNOWN_NODE"

    def __init__(self, node_id: str) -> None:
        super().__init__(f"Unknown node: {node_id}")
        self.node_id = node_id


class NotPermitted(ServiceError):
    """The caller may not read this artifact.

    Deliberately says nothing about whether it exists: an ACL that
    distinguishes "forbidden" from "absent" is an enumeration oracle.
    """

    status = 403
    code = "NOT_PERMITTED"

    def __init__(self, detail: str = "Not permitted") -> None:
        super().__init__(detail)


class InvalidRequest(ServiceError):
    """A request the operation cannot make sense of."""

    status = 422
    code = "INVALID_REQUEST"


class SnapshotNotFound(ServiceError):
    """A snapshot id this region has never sealed."""

    status = 404
    code = "UNKNOWN_SNAPSHOT"

    def __init__(self, snapshot_id: str) -> None:
        super().__init__(f"Unknown snapshot: {snapshot_id}")
        self.snapshot_id = snapshot_id


class SnapshotDrifted(ServiceError):
    """The corpus no longer digests to the snapshot the caller pinned.

    The refusal *is* the immutability guarantee. This region does not hold old
    versions of its corpus, so it cannot serve a sealed snapshot's results
    after the corpus behind it has moved. What it can do — and what a scored
    experiment actually needs — is never answer such a query with a *different*
    corpus while claiming it is the same one. So a pinned search verifies the
    manifest and refuses on drift rather than silently answering.

    Not retryable: the corpus will not move back, and a caller that retries is
    a caller that has not been told what happened.
    """

    status = 409
    code = "SNAPSHOT_DRIFTED"

    def __init__(self, snapshot_id: str, drifted: list[str]) -> None:
        fields = ", ".join(sorted(drifted)) or "unknown"
        super().__init__(
            f"Snapshot {snapshot_id} no longer describes this region: {fields} changed "
            "since it was sealed. Seal a new snapshot, or re-run against the corpus "
            "this one was sealed over."
        )
        self.snapshot_id = snapshot_id
        self.drifted = list(drifted)


class CapabilityUnsupported(ServiceError):
    """A capability this build does not have, named as such.

    Distinct from `InvalidRequest` on purpose: a harness's preflight has to
    separate "you called it wrongly" from "this region cannot do that at all",
    and the second is a build decision the caller cannot fix by changing its
    arguments.
    """

    status = 501
    code = "CAPABILITY_UNSUPPORTED"

    def __init__(self, capability: str, reason: str = "") -> None:
        detail = f"Capability not supported: {capability}"
        if reason:
            detail = f"{detail} ({reason})"
        super().__init__(detail)
        self.capability = capability
        self.reason = reason


class RegionBusy(ServiceError):
    """Another holder owns the lease this operation needs.

    The one retryable refusal in this module, which is the whole reason
    ``retryable`` is a field rather than something a caller infers from the
    status code: 409 is also what `SnapshotDrifted` returns, and retrying that
    one forever is exactly the behaviour the flag exists to prevent.
    """

    status = 409
    code = "REGION_BUSY"
    retryable = True

    def __init__(self, what: str) -> None:
        super().__init__(f"Busy: {what}")
        self.what = what


class ContaminationRefused(InvalidRequest):
    """A write matching ``readiness.corpus_denylist``.

    Its own type because a harness has to tell this apart from an ordinary
    validation failure: a rejected oversized file is a file to shrink, and this
    is a file that must never be in the corpus at all. Reporting both as
    `INVALID_REQUEST` would let a contamination refusal be retried with a
    smaller payload.
    """

    status = 403
    code = "CORPUS_DENYLISTED"

    def __init__(self, relative: str, pattern: str) -> None:
        super().__init__(
            f"Refused: {relative} matches readiness.corpus_denylist pattern {pattern!r}. "
            "Evaluation artifacts must not enter the searchable corpus."
        )
        self.relative_path = relative
        self.pattern = pattern
