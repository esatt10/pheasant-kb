"""A composed answer as files: the one reading every surface shares.

A compose answer is Markdown in which each file is a fenced block under a
heading naming it ("### `main.tf`"). MCP's ``compose_artifact`` returns it as
``files``, the region keeps it as an artifact (`services/artifacts.py`), and
the web UI's artifact viewer shows it: one parser, so the three cannot
disagree about which block is which file. Moved here from
``mcp_server/compose_tools.py`` when the second and third readers arrived.
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"```([^\n]*)\n(.*?)```", re.S)
#: A file name as an answer heads a block with: "### `main.tf`", "**main.tf**".
_FILE_NAME = re.compile(r"([A-Za-z0-9_][\w.\-/]*\.[A-Za-z0-9]+)$")
#: Trace steps a caller deciding whether to trust the artifact reads.
_CITED = re.compile(r"\[(\d+)\]")
_TRACE_STEPS = {"classify", "plan", "retrieve", "decide", "read", "synthesize", "review", "revise"}


#: The extension a block's language implies, for a block whose heading names
#: no file. An HTML report written as an unnamed ```html block was "block-1"
#: and every viewer showed its source: whether a file is drawn is read from
#: its extension.
EXTENSIONS = {
    "html": ".html",
    "htm": ".html",
    "svg": ".svg",
    "xml": ".xml",
    "markdown": ".md",
    "md": ".md",
    "javascript": ".js",
    "js": ".js",
    "typescript": ".ts",
    "ts": ".ts",
    "python": ".py",
    "py": ".py",
    "json": ".json",
    "yaml": ".yaml",
    "yml": ".yaml",
    "css": ".css",
    "sql": ".sql",
    "bash": ".sh",
    "sh": ".sh",
    "shell": ".sh",
    "powershell": ".ps1",
    "hcl": ".tf",
    "terraform": ".tf",
    "java": ".java",
    "csharp": ".cs",
    "go": ".go",
    "csv": ".csv",
    "text": ".txt",
}


def extension_for(language: str | None) -> str:
    """``.html`` for ``html``; empty for a language this does not know."""

    return EXTENSIONS.get(str(language or "").strip().lower().split()[0] if language else "", "")


def artifact_payload(result: dict) -> dict:
    """The compose answer as an artifact: files by name, gaps, compact evidence."""

    answer = str(result.get("answer") or "")
    # Only a written answer holds an artifact: an extractive fallback (the
    # model was unreachable) quotes passages whose code is not files.
    written = result.get("answer_mode") in (None, "llm")
    files: list[dict] = []
    end = 0
    for number, match in enumerate(_FENCE.finditer(answer) if written else (), 1):
        lines = [line for line in answer[end : match.start()].splitlines() if line.strip()]
        heading = lines[-1].strip().strip("#*`: ").strip("`") if lines else ""
        named = _FILE_NAME.search(heading)
        files.append(
            {
                "name": named.group(1)
                if named
                else f"block-{number}{extension_for(match.group(1).strip())}",
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
        # Where the region keeps it (`services/artifacts.py`): `get_artifact`
        # reads it again, `export_artifact` renders it.
        "artifact_id": (result.get("artifact") or {}).get("id"),
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
