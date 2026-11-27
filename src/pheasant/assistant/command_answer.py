"""An extension's chat keyword (``@harness …``), answered by the extension.

The chat's spelling of an extension operation, the way ``@remove`` is the
chat's spelling of ``archive_documents``. The service layer resolves the
keyword to the installed extension's :class:`~pheasant.extensions.ChatCommand`
and hands this module ``run``, so the answering pipeline needs no knowledge of
any extension and imports none. Nothing is searched, no history is rewritten
and Pheasant calls no model: what the extension does with the words is its own.

A refusal is answered, not raised. An extension raising ``ValueError`` (which
every :class:`~pheasant.services.errors.ServiceError` is) has its message
shown; anything else is an extension failure, said as such, so a broken
extension costs one answer rather than the chat.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def answer(
    keyword: str,
    text: str,
    *,
    run: Any,
    report: Any,
) -> tuple[Any, dict[str, Any] | None]:
    """A ``WorkflowResult`` with the extension's text, and its structured result."""

    from pheasant.assistant.workflows import WorkflowResult, WorkflowStep

    started = time.perf_counter()
    payload: dict[str, Any] | None = None
    if run is None:
        reply = f"`@{keyword}` is not available here."
    else:
        try:
            result = dict(run(keyword, text) or {})
        except ValueError as exc:
            reply = f"`@{keyword}` did not start: {exc}"
        except Exception as exc:  # noqa: BLE001 - one answer, not the chat
            logger.warning("chat keyword @%s failed", keyword, exc_info=True)
            reply = f"`@{keyword}` failed: {type(exc).__name__}. Nothing was started."
        else:
            reply = str(result.pop("text", "") or f"`@{keyword}` finished.")
            payload = {"keyword": f"@{keyword}", **result}
    step = WorkflowStep(
        name="extension",
        detail=f"@{keyword}: answered by its extension, no model",
        duration_seconds=time.perf_counter() - started,
    )
    report(step)
    return (
        WorkflowResult(
            answer=reply,
            mode="extension",
            search_mode="none",
            counts={"intent": "extension", "depth": "short"},
            steps=[step],
            workflow="extension",
            route={
                "intent": "extension",
                "depth": "short",
                "why": {
                    "intent": f"@{keyword} is answered by an installed extension",
                    "depth": "a confirmation",
                },
                "decided_by": {"intent": "keyword", "depth": "keyword"},
            },
        ),
        payload,
    )
