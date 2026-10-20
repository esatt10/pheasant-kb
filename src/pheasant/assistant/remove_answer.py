"""``@remove``: take documents out of the index from the chat, with no model.

The chat's spelling of ``archive_documents`` / ``POST /documents/archive``,
and the same operation (``services.archive``), handed in by the service layer
as ``remove`` so this module needs no transport and no engine. ``@remove
<names>`` removes the named documents and ``@current @remove`` the one the
caller has open; a bare ``@remove`` removes nothing and says how to ask.

A refusal is answered, not raised: a name matching no document, or several,
is something the reader fixes by typing a better name, and the answer says
what the refusal said. Nothing is searched and nothing is rewritten from
history, so the same message removes the same documents.
"""

from __future__ import annotations

import time
from typing import Any


def answer(
    names: tuple[str, ...],
    *,
    current_document: str | None,
    remove: Any,
    report: Any,
) -> tuple[Any, dict[str, Any] | None]:
    """A ``WorkflowResult`` saying what was removed, and the operation's payload."""

    from pheasant.assistant.workflows import WorkflowResult, WorkflowStep
    from pheasant.services.errors import ServiceError

    targets = list(names) or ([current_document] if current_document else [])
    started = time.perf_counter()
    payload: dict[str, Any] | None = None
    if remove is None:
        text = "Removing documents is not available here."
    elif not targets:
        text = (
            "Name the documents to remove, for example `@remove notes/old-runbook.md`, "
            "or open one and say `@current @remove`."
        )
    else:
        try:
            payload = remove(targets)
            text = render(payload)
        except ServiceError as exc:
            text = f"Nothing was removed: {exc}."
    step = WorkflowStep(
        name="remove",
        detail=f"@remove: {len((payload or {}).get('documents') or [])} document(s), no model",
        duration_seconds=time.perf_counter() - started,
    )
    report(step)
    return (
        WorkflowResult(
            answer=text,
            mode="remove",
            search_mode="none",
            counts={"intent": "remove", "depth": "short"},
            steps=[step],
            workflow="remove",
            route={
                "intent": "remove",
                "depth": "short",
                "why": {"intent": "@remove changes the index", "depth": "a confirmation"},
                "decided_by": {"intent": "keyword", "depth": "keyword"},
            },
        ),
        payload,
    )


def render(payload: dict[str, Any]) -> str:
    lines = []
    for document in payload.get("documents") or []:
        where = f"`{document['source_name']}/{document['path']}`"
        if document.get("disposition") == "removed":
            lines.append(f"- {where}: deleted (an upload, so it cannot be reinstated)")
        else:
            lines.append(f"- {where}: archived; reinstate it to index it again")
    status = payload.get("status")
    if status == "removed":
        lead = "Removed from the index, and kept out of every later sync:"
    elif status == "queued":
        lead = "Removal queued; the indexer removes these with its next sync of the source:"
    else:
        lead = "Archived; a sync of the source is running and removes these as it finishes:"
    return "\n".join([lead, *lines])
