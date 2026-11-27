"""``@harness`` in Pheasant's chat: start a run from the conversation.

The harness is an optional build layer, not a way to answer questions, so the
chat reaches it only when somebody asks for it by name: ``@harness`` as the
first word, like ``@compose``. Everything else in the chat is unchanged.

What the run reads is taken from where the person is, in this order:

1. the folder or document they have open (``@dir`` / ``@current`` context the
   UI already sends);
2. the sources the chat is scoped to;
3. the places already registered for them, newest first.

A source of the index becomes a ``pheasant://`` registration (read as the
index holds it). That registration is made for them only when it cannot widen
what they can read: with document ACLs off every source is readable through
search anyway, and someone who may manage sources may register any. Otherwise
only registrations they were already granted are used.

The result is kept as a Pheasant artifact (an ``answer`` stays with the run),
the run id is idempotent per conversation and message, and a code change is
sent to the Harness page, because it needs a repository, a branch and tests
that a chat message does not carry.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from pheasant_harness.common import HarnessError, digest_value

#: The first word of the request says what kind of run it is.
_WORKLOADS = {
    "summarize": "summarize",
    "summarise": "summarize",
    "summary": "summarize",
    "tldr": "summarize",
    "answer": "answer",
    "ask": "answer",
    "explain": "answer",
    "change": "code_change",
    "fix": "code_change",
    "refactor": "code_change",
    "patch": "code_change",
    "implement": "code_change",
}
_LABELS = {
    "build_content": "build content",
    "summarize": "summarize",
    "answer": "answer a question",
    "code_change": "change code",
}
MAX_INPUTS = 6

HELP = (
    "`@harness` starts a harness run: an agent that reads your sources, builds what you "
    "ask for, checks it, and keeps the verified result as a Pheasant artifact.\n\n"
    "- `@harness write a one-page onboarding brief for new on-call engineers`\n"
    "- `@harness summarize the incident reports in this folder` (with a folder open)\n"
    "- `@harness answer which services still read the legacy config`\n\n"
    "It reads the folder or document you have open, else the sources this chat is "
    "scoped to, else the places registered for you on the Harness page."
)


def workload_of(text: str) -> str:
    first = re.sub(r"[^a-z;]", "", (text.split() or [""])[0].lower()).replace(";", "")
    return _WORKLOADS.get(first, "build_content")


def quoted_phrases(text: str) -> list[str]:
    """Phrases in quotes: what a run greps for exactly and cites (`identifiers`)."""
    found = re.findall(r'"([^"]{2,120})"|“([^”]{2,120})”', text)
    return list(dict.fromkeys(a or b for a, b in found))[:12]


def _title(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else "Harness result"
    return line[:80].rstrip(" .,:;")


def _may_register(services: Any) -> bool:
    from pheasant.security.authorization import allowed

    acl = bool(getattr(getattr(services.config, "security", None), "acl_enforced", False))
    return not acl or allowed("sources.manage")


def _places(services: Any, request: Any) -> list[tuple[str, str, str]]:
    """(uri, label, why) for what the person is looking at, most specific first."""
    from pheasant.services import scope

    found: list[tuple[str, str, str]] = []
    folder = scope.locate(services, request.current_directory, directory=True)
    document = scope.locate(services, request.current_document, directory=False)
    if folder:
        source, path = folder
        found.append((_uri(source, path), f"{source}/{path}".rstrip("/"), "the open folder"))
    elif document:
        source, path = document
        found.append((_uri(source, path), f"{source}/{path}", "the open document"))
    else:
        names = list(request.source_names) or ([request.source_name] if request.source_name else [])
        for name in names[:MAX_INPUTS]:
            if services.state.get_source(name):
                found.append((_uri(name, ""), name, "this chat's source scope"))
    return found


def _uri(source: str, path: str) -> str:
    return f"pheasant://{quote(source, safe='')}" + (f"/{quote(path, safe='/')}" if path else "")


def _inputs(services: Any, request: Any, who: str) -> tuple[list[dict], str]:
    from pheasant_harness.context import harness_for

    registry = harness_for(services).registry
    mine = [r for r in registry.list(who) if r["authorized"] and not r["revoked"]]
    places = _places(services, request)
    if places:
        chosen = []
        for uri, label, _why in places:
            existing = next((r for r in mine if r["uri"] == uri), None)
            if existing is None:
                if not _may_register(services):
                    raise HarnessError(
                        f"{label} is not registered for you yet. Ask someone who manages "
                        "sources to register it on the Harness page, then ask again."
                    )
                existing = registry.register(uri, who, label=f"{label} (from chat)")
            chosen.append({"id": existing["id"], "label": existing["label"], "uri": uri})
        return chosen, places[0][2]
    recent = sorted(mine, key=lambda r: r["created_at"], reverse=True)[:MAX_INPUTS]
    if not recent:
        raise HarnessError(
            "there is nothing for it to read yet. Open a folder or document, scope the chat "
            "to a source, or register a place on the Harness page."
        )
    return (
        [{"id": r["id"], "label": r["label"], "uri": r["uri"]} for r in recent],
        "the places registered for you",
    )


def run_command(services: Any, request: Any) -> dict[str, Any]:
    """The ``@harness`` keyword's operation (`pheasant.extensions.ChatCommand.run`)."""
    from pheasant_harness import connections as connections_mod
    from pheasant_harness import service
    from pheasant_harness.models import RunRequest, StartRequest

    text = (request.text or "").strip()
    if not text or text.lower() in {"help", "?"}:
        return {"text": HELP, "status": "help"}
    workload = workload_of(text)
    if workload == "code_change":
        link = f"/harness?new=1&prompt={quote(text)}"
        return {
            "text": (
                "A code change needs a repository, a branch and the tests that prove it works, "
                "so it is started on the Harness page. "
                f"[Open it with this request filled in]({link})."
            ),
            "status": "needs_page",
            "link": link,
            "workload": workload,
        }
    # Writing new prose needs a model; without one the rules planner can gather
    # and cite what the sources say, which is a summary, and is said to be one.
    modelless = service._llm(services) is None
    downgraded = workload == "build_content" and modelless
    if downgraded:
        workload = "summarize"
    who = service.actor()
    with connections_mod.caller(actor=who, purpose="chat"):
        try:
            inputs, why = _inputs(services, request, who)
        except HarnessError as exc:
            raise ValueError(str(exc)) from exc
        destination = (
            {"adapter": "record", "target": ""}
            if workload == "answer"
            else {"adapter": "artifact", "target": _title(text)}
        )
        phrases = quoted_phrases(text)
        run_request = RunRequest(
            workload=workload,
            question=text,
            inputs=[i["id"] for i in inputs],
            destination=destination,
            purpose="started from the chat",
            identifiers=phrases,
        )
        key = (
            "chat-"
            + digest_value([request.conversation_id or "", text, [i["id"] for i in inputs]])[7:31]
        )
        try:
            status = service.start_run(
                services, StartRequest(request=run_request, client_key=key), request.knowledge_base
            )
        except HarnessError as exc:
            raise ValueError(str(exc)) from exc
    run_id = status["run_id"]
    link = f"/harness?run={quote(run_id)}"
    where = ", ".join(f"`{i['label']}`" for i in inputs)
    kept = (
        "keeps the verified answer with the run"
        if workload == "answer"
        else "keeps the verified result as a Pheasant artifact"
    )
    looks_for = (
        " It looks for " + ", ".join(f"“{p}”" for p in phrases) + " exactly." if phrases else ""
    )
    hint = (
        "\n\n_No model is connected, so it only finds what you name: put exact phrases in "
        'quotes (`@harness write a brief on "rollback approval"`)._'
        if not phrases and modelless
        else ""
    )
    if downgraded:
        hint += (
            "\n\n_No model is connected, so instead of writing new prose it gathers and cites "
            "what the sources say about it._"
        )
    reply = (
        f"Started a harness run to **{_LABELS[workload]}** from {where} ({why}).{looks_for}\n\n"
        f"It works in the background: it reads those sources, builds a draft, checks the draft "
        f"against its checks, and {kept}. You can keep chatting meanwhile.\n\n"
        f"[Open the run]({link}) · {status.get('state', 'accepted')}{hint}"
    )
    return {
        "text": reply,
        "status": "started",
        "run_id": run_id,
        "state": status.get("state"),
        "workload": workload,
        "identifiers": phrases,
        "inputs": inputs,
        "destination": destination,
        "link": link,
    }
