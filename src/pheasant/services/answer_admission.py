"""Admitting an answer: the process-wide answer slots, and deep research's own.

``acquire`` is ``services.assistant.answer``'s admission, shared by HTTP, its
stream and MCP (the routes take a permit before a stream opens, so a refusal
can still be a status code). A deep-research request (``@detailed`` /
``@deep``, a pinned ``long``) also takes one of the process's deep slots,
``max_concurrent_deep_answers`` or the operator's figure from the region
controls, and runs under its own spend tracker (``deep_spend``,
``telemetry/answer_spend.py``). Split from ``services/assistant.py``, which
re-exports ``AnswerPermit`` and ``acquire``.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from pheasant.services import ServiceContext
from pheasant.services.errors import AssistantBusy, DeepResearchBusy

if TYPE_CHECKING:
    from pheasant.services.assistant import AnswerRequest

_ADMISSION_LOCK = threading.Lock()
_ADMISSION: dict[int, threading.BoundedSemaphore] = {}
#: Deep-research answers running in this process. A count rather than a
#: semaphore because its limit is the operator's and changes at runtime: a
#: lowered limit admits nobody new until enough of the running ones finish.
_DEEP_ACTIVE = 0


class AnswerPermit:
    """An idempotent slot held until the answer worker actually exits."""

    def __init__(self, semaphore: threading.BoundedSemaphore | None, *, deep: bool = False) -> None:
        self._semaphore = semaphore
        self._released = False
        self._lock = threading.Lock()
        #: Whether this permit holds a deep-research slot too.
        self.deep = deep

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        if self.deep:
            global _DEEP_ACTIVE
            with _ADMISSION_LOCK:
                _DEEP_ACTIVE = max(0, _DEEP_ACTIVE - 1)
        if self._semaphore is not None:
            self._semaphore.release()


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


def acquire(context: ServiceContext, request: AnswerRequest | None = None) -> AnswerPermit:
    """Acquire the process-wide answer limit shared by HTTP, SSE, and MCP.

    A deep-research ``request`` also takes one of the process's deep slots
    (``max_concurrent_deep_answers``, the operator's), within the answer
    limit, so a few minutes-long answers cannot hold every slot an api
    replica has.
    """
    deep = request is not None and is_deep(request, context.config)
    if deep and not _deep_slot(context):
        raise DeepResearchBusy()
    assistant = getattr(context.config, "assistant", None)
    latency = getattr(assistant, "latency", None)
    limit = int(getattr(latency, "max_concurrent_answers", 0) or 0)
    if limit <= 0:
        return AnswerPermit(None, deep=deep)
    with _ADMISSION_LOCK:
        semaphore = _ADMISSION.setdefault(limit, threading.BoundedSemaphore(limit))
    if not semaphore.acquire(blocking=False):
        if deep:
            AnswerPermit(None, deep=True).release()
        raise AssistantBusy()
    return AnswerPermit(semaphore, deep=deep)


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
