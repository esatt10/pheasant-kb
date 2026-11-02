"""Offloading a composition to the region, over MCP.

A mixin plus a registration function, like ``sync_control_tools.py``. The
operation is ``services.assistant.answer`` with intent "compose" -- the one
behind ``ask_knowledge_base(intent="compose")`` and ``POST /assistant/chat``.

This tool exists for MCP clients, and only there on purpose: a composed answer
takes minutes and its full payload runs to hundreds of kilobytes, which
outlived one client's tool timeout and another's result limit. So by default it
runs as a job (``get_job`` follows it) and the result is the artifact -- its
files, what the sources did not cover, compact citations and the review trace
-- without the per-passage evidence. HTTP has ``POST /assistant/chat`` and its
stream for the same work.
"""

from __future__ import annotations

import re
import threading
from typing import Any

_FENCE = re.compile(r"```([^\n]*)\n(.*?)```", re.S)
#: A file name as an answer heads a block with: "### `main.tf`", "**main.tf**".
_FILE_NAME = re.compile(r"([A-Za-z0-9_][\w.\-/]*\.[A-Za-z0-9]+)$")
#: Trace steps a caller deciding whether to trust the artifact reads.
_CITED = re.compile(r"\[(\d+)\]")
_TRACE_STEPS = {"classify", "plan", "retrieve", "decide", "read", "synthesize", "review", "revise"}


def artifact_payload(result: dict) -> dict:
    """The compose answer as an artifact: files by name, gaps, compact evidence."""

    answer = str(result.get("answer") or "")
    files: list[dict] = []
    end = 0
    for number, match in enumerate(_FENCE.finditer(answer), 1):
        lines = [line for line in answer[end : match.start()].splitlines() if line.strip()]
        heading = lines[-1].strip().strip("#*`: ").strip("`") if lines else ""
        named = _FILE_NAME.search(heading)
        files.append(
            {
                "name": named.group(1) if named else f"block-{number}",
                "language": match.group(1).strip() or None,
                "content": match.group(2),
            }
        )
        end = match.end()
    cited = set(_CITED.findall(answer))
    first = _FENCE.search(answer)
    summary = (answer[: first.start()] if first else answer).strip()
    # The first file's heading sits above its block, not in the summary.
    if files and not files[0]["name"].startswith("block-"):
        head, _, last = summary.rpartition("\n")
        if files[0]["name"] in last:
            summary = head.strip()
    return {
        "summary": summary,
        "files": files,
        "not_covered": answer[end:].strip() if files else "",
        "citations": [
            {
                "index": citation.get("index"),
                "source": citation.get("source_id"),
                "path": citation.get("relative_path") or citation.get("title"),
                "section": citation.get("heading_path"),
            }
            for citation in result.get("citations") or []
            # Only the passages the artifact cites: the window holds 64 and a
            # 41-resource configuration cited 30, and the rest was 8 KB of a
            # result that has to fit an MCP client's limit.
            if not cited or str(citation.get("index")) in cited
        ],
        "trace": [
            {"step": step.get("name"), "detail": step.get("detail")}
            for step in result.get("steps") or []
            if step.get("name") in _TRACE_STEPS
        ],
        "route": result.get("route"),
        "answer_mode": result.get("answer_mode"),
        "error": result.get("error"),
        "server_seconds": round(float(result.get("server_processing_ms") or 0) / 1000, 1),
    }


_DECLARED = re.compile(r'^\s*[A-Za-z_][\w-]*\s+"([^"]+)"\s+"[^"]+"\s*\{', re.M)


def declared_types(payload: dict) -> set[str]:
    """Component types the artifact's files declare (``kind "type" "name" {``)."""

    return {kind for f in payload.get("files") or [] for kind in _DECLARED.findall(f["content"])}


def artifact_text(payload: dict) -> str:
    """A composed artifact back as an answer, for the history of a revision."""

    files = "\n\n".join(
        f"### `{f['name']}`\n```{f.get('language') or ''}\n{f['content']}```"
        for f in payload.get("files") or []
    )
    return "\n\n".join(
        part for part in (payload.get("summary"), files, payload.get("not_covered")) if part
    )


class ComposeTools:
    """``services.assistant.answer`` (intent "compose") as a followable job."""

    jobs: Any

    #: ``AssistantTools`` (which this is a base of) provides ``ask_knowledge_base``
    #: and the facade provides ``jobs`` and ``_require_knowledge_base``.

    def compose_artifact(
        self,
        knowledge_base: str,
        request: str,
        source_names: list[str] | None = None,
        source_name: str | None = None,
        history: list[dict] | None = None,
        wait: bool = False,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        options: dict | None = None,
        previous_job: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services.errors import InvalidRequest

        if not str(request or "").strip():
            raise InvalidRequest("compose_artifact needs a request: what to build, and from what.")
        settings = dict(options or {})
        previous: dict = {}
        if previous_job:
            # The region already holds the artifact being revised: sending it
            # back costs the caller every byte of it again (34 KB, measured,
            # for a 41-resource configuration) to say what the region produced.
            # And it is the draft the writer edits, not a turn of conversation:
            # handed over as history, a revision asked for five changes wrote a
            # new artifact that dropped the API policy and both backends.
            earlier = self.jobs.get(previous_job)
            previous = (earlier or {}).get("result") or {}
            if (earlier or {}).get("kind") != "compose" or not previous.get("files"):
                raise InvalidRequest(
                    f"previous_job {previous_job} is not a finished composition this region "
                    "holds; send its files in history instead."
                )
            settings["compose_draft"] = artifact_text(previous)
        turns = list(history or [])
        if not previous_job and turns and "```" in str(turns[-1].get("answer") or ""):
            # An artifact sent back in history is the draft to edit too: as a
            # history turn it reaches the prompt cut to its first 1,200
            # characters, which of a 34 KB configuration is its providers.tf.
            settings["compose_draft"] = str(turns.pop()["answer"])

        def compose(on_step: Any = None) -> dict:
            payload = artifact_payload(
                self.ask_knowledge_base(
                    knowledge_base,
                    request,
                    intent="compose",
                    source_names=source_names,
                    source_name=source_name,
                    history=turns,
                    principal=principal,
                    principal_groups=principal_groups,
                    options=settings or None,
                    on_step=on_step,
                )
            )
            if previous and payload.get("files"):
                # Only against an artifact: a run with none (the model was
                # unreachable) dropped nothing, it wrote nothing.
                # Said outright, so a revision that lost something is seen at
                # once rather than found by diffing the files.
                payload["dropped_from_previous"] = sorted(
                    declared_types(previous) - declared_types(payload)
                )
            return payload

        if wait:
            return compose()
        job = self.jobs.create("compose", f"Composing: {request.strip()[:60]}", ["compose"])

        def step(done: Any) -> None:
            # The workflow's own steps, so a caller polling get_job sees where
            # a several-minute composition is rather than one fixed phase.
            self.jobs.progress(
                job.id,
                phase=str(getattr(done, "name", "") or "composing"),
                detail=str(getattr(done, "detail", "") or "")[:200],
            )

        def run() -> None:
            self.jobs.progress(job.id, phase="composing", detail="planning, retrieving, writing")
            try:
                payload = compose(step)
            except Exception as exc:  # background tool work cannot propagate
                self.jobs.finish(job.id, "failed", error=str(exc))
                return
            # An extractive fallback (the model was unreachable) or a reply with
            # no code is not an artifact, whatever else it holds.
            written = payload.get("answer_mode") == "llm" and payload.get("files")
            self.jobs.finish(
                job.id,
                "succeeded" if written else "failed",
                error=None if written else (payload.get("error") or "no artifact was written"),
                result=payload,
            )

        threading.Thread(target=run, name=f"pheasant-mcp-compose-{job.id}", daemon=True).start()
        return {
            **job.as_dict(),
            "job_id": job.id,
            "poll": "call get_job(job_id) until status is succeeded or failed; the artifact "
            "is in result. A composition takes several minutes.",
        }


def register_compose_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
    @mcp.tool()
    @anticipated
    def compose_artifact(
        knowledge_base: str,
        request: str,
        source_names: list[str] | None = None,
        source_name: str | None = None,
        history: list[dict] | None = None,
        wait: bool = False,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        options: dict | None = None,
        previous_job: str | None = None,
    ) -> dict:
        """Offload building a NEW artifact to the region: a configuration, a
        script, a module or a set of files assembled from parts the sources
        document, when no single passage contains the finished thing.

        Use it instead of searching and assembling yourself when the artifact
        has many components spread across many documents (an infrastructure
        configuration of two dozen resources, a pipeline spanning several
        tools). The region plans one search per component, reads each
        component's own reference page, writes the artifact in one pass, then
        checks it: every argument against its component's page, required
        arguments, undeclared references, invented types, components the
        request asks for and the artifact omits, and components that would not
        do what the request says they are for. What it finds it corrects in an
        edit of the draft before returning. Values the sources do not give
        become placeholder variables. For a question with an answer in the
        sources, use ask_knowledge_base; for passages to judge yourself,
        search_context.

        request says what to build and how it must behave. Best results come
        from naming the output files, listing each component as a numbered
        requirement with what it is for ("failover: two backends ... so that
        when one is unavailable the other serves the same requests"), and
        stating conventions (one resource per sub-component, every
        environment-specific value a variable). source_names keeps every
        search inside the sources that document the parts (list_sources shows
        them).

        By default it returns a job at once: call get_job(job_id) until status
        is "succeeded" or "failed" (several minutes). The job's result is
        {summary, files: [{name, language, content}], not_covered (what the
        sources do not document and the reader must fill in), citations
        [{index, source, path, section}] for the [n] comments in the files,
        trace (the plan, review and edits), route, error}. wait=true blocks
        and returns that result directly, for clients without a short tool
        timeout.

        The region cannot run the artifact's own tool (a compiler, terraform
        validate, a linter). Run it yourself, and review what it cannot judge.
        To revise, call again with previous_job=<that job_id> and a request
        naming exactly what to change (the validator's errors, a design fix):
        the region edits the earlier artifact from its own job, keeping what
        the request does not change, so you send only the changes. The
        result's dropped_from_previous lists component types the revision no
        longer declares. (Without the job -- another region, or after a
        restart -- pass history=[{"question": request, "answer": <the files>}];
        an artifact in the last turn is used as the draft to edit, whole.)
        While a job runs, get_job's progress.phase names the last workflow step
        it finished (plan, retrieve, decide, synthesize, review, revise) and its
        log lists every step so far.
        """

        return tools.compose_artifact(
            knowledge_base,
            request,
            source_names=source_names,
            source_name=source_name,
            history=history,
            wait=wait,
            principal=principal,
            principal_groups=principal_groups,
            options=options,
            previous_job=previous_job,
        )
