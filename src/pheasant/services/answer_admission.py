"""Admitting an answer: the process-wide answer slots, and deep research's own.

``acquire`` is ``services.assistant.answer``'s admission, shared by HTTP, its
stream and MCP (the routes take a permit before a stream opens, so a refusal
can still be a status code). A deep-research request (``@detailed`` /
``@deep``, a pinned ``long``) also takes one of the process's deep slots,
``max_concurrent_deep_answers`` or the operator's figure from the region
controls, and runs under its own spend tracker (``deep_spend``,
``telemetry/answer_spend.py``). Split from ``services/assistant.py``, which
re-exports ``AnswerPermit``, ``acquire`` and ``acquire_waiting``.

**A full process makes an answer wait, not fail.** An answer holds its slot
while it waits on model calls (15-20 s for a deep one, measured on the hosted
region), so the next asker is admitted within seconds of a slot freeing. The
first version refused at once, and a fifth person asking alongside four others
got ``ASSISTANT_BUSY`` for an answer that would have started three seconds
later. A request now waits up to ``assistant.latency.admission_wait_seconds``
for a slot and is refused only after that; the HTTP routes wait on the event
loop (``acquire_waiting``) so a request that disconnects while waiting holds
nothing, and every other caller waits on a condition. Deep research's own cap
still refuses at once: it is the operator's cost decision, and its refusal
offers an ordinary answer instead.

The answer limit is ``assistant.latency.max_concurrent_answers`` unless an
operator admin set one in the region controls (``max_concurrent_answers``,
``services/region_controls.py``), which every replica follows within
``CONTROLS_TTL_SECONDS``. A lowered limit admits nobody new until enough of the
running answers finish; nothing running is cut off.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Any

from pheasant.services import ServiceContext
from pheasant.services.errors import AssistantBusy, DeepResearchBusy

if TYPE_CHECKING:
    from pheasant.services.assistant import AnswerRequest

_ADMISSION_LOCK = threading.Lock()
#: Signalled whenever a slot frees, for callers waiting on one.
_SLOT_FREED = threading.Condition(_ADMISSION_LOCK)
#: Answers running in this process, against ``max_concurrent_answers``. A count
#: rather than a semaphore for the same reason as the deep one below: the
#: limit is read per request, so changing it never strands a permit.
_ACTIVE = 0
#: How often a request waiting on the event loop looks for a free slot.
_POLL_SECONDS = 0.25
#: The most answers an operator admin may let one process run at once. Past
#: ``server.api.max_concurrent_requests`` the request limit refuses first.
MAX_ANSWER_CONCURRENCY = 256
#: Deep-research answers running in this process. A count rather than a
#: semaphore because its limit is the operator's and changes at runtime: a
#: lowered limit admits nobody new until enough of the running ones finish.
_DEEP_ACTIVE = 0


class AnswerPermit:
    """An idempotent slot held until the answer worker actually exits."""

    def __init__(self, counted: bool = False, *, deep: bool = False) -> None:
        #: Whether this permit holds one of ``max_concurrent_answers``.
        self._counted = bool(counted)
        self._released = False
        self._lock = threading.Lock()
        #: Whether this permit holds a deep-research slot too.
        self.deep = deep

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        global _ACTIVE, _DEEP_ACTIVE
        with _SLOT_FREED:
            if self.deep:
                _DEEP_ACTIVE = max(0, _DEEP_ACTIVE - 1)
            if self._counted:
                _ACTIVE = max(0, _ACTIVE - 1)
            _SLOT_FREED.notify_all()


def is_deep(request: AnswerRequest, config: Any = None) -> bool:
    """Whether ``request`` asks for deep research: ``@detailed`` / ``@deep`` leading
    the question, or a pinned ``long`` (``"deep"``) depth (``routing.pinned_deep``)."""

    from pheasant.assistant.routing import pinned_deep

    if request.intent and str(request.intent).strip().lower() == "compose":
        return False  # compose has its own breadth and review
    settings = getattr(config, "assistant", None)
    return pinned_deep(
        request.question,
        request.depth or (request.options or {}).get("depth"),
        keywords_on=getattr(settings, "keywords", True) is not False,
    )


def _deep_slot(context: ServiceContext) -> bool:
    """Take one of this process's deep-research slots; ``False`` when all are taken."""

    from pheasant.telemetry import answer_spend
    from pheasant.telemetry.spend import GUARD

    global _DEEP_ACTIVE
    limit = answer_spend.effective_concurrency(context.config, GUARD.controls())
    with _ADMISSION_LOCK:
        if limit > 0 and _DEEP_ACTIVE >= limit:
            return False
        _DEEP_ACTIVE += 1
    return True


def _latency(context: ServiceContext) -> Any:
    return getattr(getattr(context.config, "assistant", None), "latency", None)


def configured_answer_limit(config: Any) -> int:
    """The deployment's answer limit; ``0`` turns admission off."""

    latency = getattr(getattr(config, "assistant", None), "latency", None)
    return max(0, int(getattr(latency, "max_concurrent_answers", 0) or 0))


def effective_answer_limit(config: Any, saved: dict[str, Any]) -> int:
    """Answers one process may run at once: the operator's figure, else the deployment's."""

    if saved.get("max_concurrent_answers") is not None:
        return int(saved["max_concurrent_answers"])
    return configured_answer_limit(config)


def _answer_limit(context: ServiceContext) -> int:
    from pheasant.telemetry.spend import GUARD

    return effective_answer_limit(context.config, GUARD.controls())


def _wait_seconds(context: ServiceContext) -> float:
    return max(0.0, float(getattr(_latency(context), "admission_wait_seconds", 0) or 0))


def acquire(
    context: ServiceContext, request: AnswerRequest | None = None, *, wait: float | None = None
) -> AnswerPermit:
    """Acquire the process-wide answer limit shared by HTTP, SSE, and MCP.

    A deep-research ``request`` also takes one of the process's deep slots
    (``max_concurrent_deep_answers``, the operator's), within the answer
    limit, so a few long answers cannot hold every slot an api replica has.
    With every answer slot taken it waits up to ``wait`` seconds (default
    ``admission_wait_seconds``) for one before refusing ``ASSISTANT_BUSY``;
    this blocks the calling thread, so code on an event loop uses
    :func:`acquire_waiting`.
    """
    global _ACTIVE
    deep = request is not None and is_deep(request, context.config)
    if deep and not _deep_slot(context):
        raise DeepResearchBusy()
    limit = _answer_limit(context)
    if limit <= 0:
        return AnswerPermit(deep=deep)
    deadline = time.monotonic() + (_wait_seconds(context) if wait is None else max(0.0, wait))
    with _SLOT_FREED:
        while _ACTIVE >= limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _SLOT_FREED.wait(remaining)
        else:
            _ACTIVE += 1
            return AnswerPermit(True, deep=deep)
    if deep:
        AnswerPermit(deep=True).release()
    raise AssistantBusy()


async def acquire_waiting(
    context: ServiceContext, request: AnswerRequest | None = None
) -> AnswerPermit:
    """:func:`acquire` for an event loop: waits by sleeping, never by blocking.

    Each look is a non-blocking ``acquire``, so a request cancelled while it
    waits (its client went away) has taken nothing and leaks no slot.
    """

    deadline = time.monotonic() + _wait_seconds(context)
    while True:
        try:
            return acquire(context, request, wait=0)
        except AssistantBusy:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            await asyncio.sleep(min(_POLL_SECONDS, remaining))


def deep_spend(context: ServiceContext, request: AnswerRequest) -> Any:
    """The spend tracker a deep-research answer runs under, or ``None``.

    Enforced only where usage is metered, as the monthly limit is: with
    metering off nothing is priced, and the answer's steps say so.
    """

    if not is_deep(request, context.config):
        return None
    from pheasant.telemetry import answer_spend
    from pheasant.telemetry.spend import GUARD

    config = context.config
    currency = getattr(getattr(config, "models", None), "currency", "USD") or "USD"
    return answer_spend.AnswerSpend(
        answer_spend.effective_limit(config, GUARD.controls()),
        enforced=bool(getattr(config.observability, "usage", False) and GUARD.bound),
        currency=currency,
    )
