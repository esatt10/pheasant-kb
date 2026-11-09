"""One renderer for a kept artifact, whichever surface shows it.

The web UI's artifact viewer, ``GET /artifacts/{id}/view``, MCP's
``export_artifact`` and the downloads all show an artifact through these
functions, so a report looks the same in a browser tab, in Claude and in the
file somebody saved -- and a fix lands in all of them. Pheasant's own palette
and its syntax colours (``ui/src/styles.css``), light and dark.

What an artifact holds was written by a model, so every document starts with
:data:`DOCUMENT_POLICY`: it may run its own inline scripts (an HTML report
draws its charts, a saved visual draws itself) and it may not reach the
network, load anything from anywhere, or submit a form. Markdown is rendered
with raw HTML off; code goes through pygments, which escapes it; only an HTML
*file*, shown as a preview, is its author's markup, and it runs under the same
policy inside whatever sandbox shows it.
"""

from __future__ import annotations

import html
import io
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name, get_lexer_for_filename
from pygments.util import ClassNotFound

#: Inline scripts and styles, data: images and fonts, and nothing fetched.
DOCUMENT_POLICY = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; media-src data: blob:; frame-src 'self' data: blob:; "
    "form-action 'none'; base-uri 'none'"
)
#: The same policy as a response header, in an opaque origin, for a document
#: opened directly: never the API's origin.
VIEW_HEADER_POLICY = "sandbox allow-scripts; " + DOCUMENT_POLICY

#: Files a reader can see drawn rather than as source.
PREVIEWABLE = {".html", ".htm", ".md", ".markdown", ".svg"}

KIND_LABELS = {"files": "Composed files", "report": "Report", "thread": "Conversation"}

_MARKDOWN = (
    MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    .enable("table")
    .enable("strikethrough")
)
#: ``[n]``, not already a link's text. Adjacent ones ("[11][7]") are each a
#: citation: answers cite in runs, and a lookahead for ``[`` left the first unlinked.
_CITATION = re.compile(r"\[(\d{1,3})\](?!\()")
_KNOWLEDGE_VIEW = (
    Path(__file__).resolve().parents[1] / "mcp_server" / "apps" / "knowledge_view.html"
)

# Pygments' token classes onto pheasant's syntax colours, so code here reads
# exactly as it does in the UI's source viewer.
_SYNTAX = {
    "keyword": ".k,.kc,.kd,.kn,.kp,.kr,.ow",
    "type": ".kt,.nb,.bp,.ne",
    "string": ".s,.sa,.sb,.sc,.dl,.sd,.s2,.se,.sh,.si,.sx,.sr,.s1,.ss",
    "number": ".m,.mb,.mf,.mh,.mi,.il,.mo",
    "comment": ".c,.ch,.cm,.c1,.cs,.cpf",
    "title": ".nf,.fm,.nc,.nt,.nn",
    "attr": ".na,.nv,.vc,.vg,.vi",
    "meta": ".cp,.nd",
}
_SYNTAX_CSS = "\n".join(
    ",".join(f".hl {selector}" for selector in selectors.split(","))
    + f"{{color:var(--syn-{name})}}"
    for name, selectors in _SYNTAX.items()
) + (
    "\n.hl .c,.hl .ch,.hl .cm,.hl .c1,.hl .cs{font-style:italic}"
    "\n.hl .gi{background:var(--syn-addition)}.hl .gd{background:var(--syn-deletion)}"
    "\n.hl .err{color:inherit;border:0}"
)

_LIGHT = (
    "--bg:#ffffff;--bg-sunken:#f7f5f0;--border:#e3ded3;--text:#1f1c17;--text-soft:#57524a;"
    "--muted:#8a8377;--accent:#5f6b45;--accent-soft:#eef0e7;--accent-text:#46512f;"
    "--warn:#856022;--warn-soft:#f7efdd;"
    "--syn-keyword:#8a3b6b;--syn-string:#3d6b2f;--syn-number:#9a5b13;--syn-comment:#8a8377;"
    "--syn-title:#2f5d8a;--syn-type:#6a4c93;--syn-attr:#7a5c1e;--syn-meta:#5f6b45;"
    "--syn-addition:#e6efd9;--syn-deletion:#f8e3df;color-scheme:light;"
)
_DARK = (
    "--bg:#191713;--bg-sunken:#131110;--border:#302c26;--text:#ece7dd;--text-soft:#bcb4a6;"
    "--muted:#8e8778;--accent:#a9b485;--accent-soft:#23271a;--accent-text:#bfc99c;"
    "--warn:#e0b07a;--warn-soft:#2c2416;"
    "--syn-keyword:#d79bc4;--syn-string:#a9c98b;--syn-number:#e0b07a;--syn-comment:#7f786b;"
    "--syn-title:#8fb8e0;--syn-type:#bfa6e6;--syn-attr:#d9bf86;--syn-meta:#a9b485;"
    "--syn-addition:#25301d;--syn-deletion:#3a2320;color-scheme:dark;"
)

_STYLE = f"""
:root{{{_LIGHT}}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{{_DARK}}}}}
:root[data-theme="dark"]{{{_DARK}}}
*{{box-sizing:border-box}}
html,body{{margin:0;background:var(--bg);color:var(--text)}}
body{{font:14px/1.55 "Inter",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  padding:18px 16px 40px}}
main{{max-width:880px;margin:0 auto}}
h1{{font-size:1.3rem;line-height:1.25;margin:0 0 .25rem}}
h2{{font-size:1.08rem;margin:1.6rem 0 .5rem}}
h3{{font-size:1rem;margin:1.5rem 0 .4rem}}
p,li{{color:var(--text)}}
a{{color:var(--accent-text)}}
.meta{{color:var(--muted);font-size:.85rem;margin:0 0 1.25rem}}
.badge{{display:inline-block;font-size:.75rem;color:var(--accent-text);background:var(--accent-soft);
  border:1px solid var(--border);border-radius:999px;padding:0 .6rem;margin-right:.4rem}}
.question{{border-left:3px solid var(--accent);background:var(--bg-sunken);padding:.6rem .9rem;
  border-radius:0 8px 8px 0;margin:0 0 1.25rem;color:var(--text-soft)}}
pre{{background:var(--bg-sunken);border:1px solid var(--border);border-radius:8px;padding:12px 14px;
  overflow:auto;font-size:13px;line-height:1.5}}
code{{font-family:ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,monospace;font-size:.92em}}
:not(pre)>code{{background:var(--bg-sunken);border:1px solid var(--border);border-radius:4px;
  padding:0 .3em}}
table{{border-collapse:collapse;margin:1rem 0;font-size:.92rem}}
th,td{{border:1px solid var(--border);padding:.35rem .6rem;text-align:left;vertical-align:top}}
th{{background:var(--bg-sunken)}}
blockquote{{margin:1rem 0;padding:.2rem 1rem;border-left:3px solid var(--border);
  color:var(--text-soft)}}
.file{{margin:1.75rem 0}}
.file__name{{display:flex;gap:.5rem;align-items:baseline;font-family:ui-monospace,Menlo,monospace;
  font-size:.9rem;margin:0 0 .4rem}}
.file__lang{{color:var(--muted);font-family:inherit;font-size:.8rem}}
.gaps{{background:var(--warn-soft);border:1px solid var(--border);border-radius:8px;
  padding:.4rem 1rem;margin:1.75rem 0}}
.gaps h2{{margin-top:.6rem;color:var(--warn)}}
.sources{{margin-top:2.5rem;border-top:1px solid var(--border);padding-top:.75rem;font-size:.85rem}}
.sources li{{color:var(--text-soft)}}
.sources li:target{{background:var(--accent-soft)}}
sup a,.cite{{text-decoration:none;font-size:.75em;color:var(--accent-text);
  background:var(--accent-soft);border-radius:6px;padding:0 .3em}}
.visual-frame{{width:100%;height:420px;border:1px solid var(--border);border-radius:10px;
  background:var(--bg);margin:1rem 0}}
.svg-preview{{max-width:100%;height:auto;display:block;margin:0 auto}}
.html-frame{{width:100%;height:520px;border:1px solid var(--border);border-radius:10px;
  background:#fff;display:block}}
details{{margin:.4rem 0}}summary{{cursor:pointer;color:var(--muted);font-size:.85rem}}
footer{{margin-top:3rem;color:var(--muted);font-size:.8rem}}
{_SYNTAX_CSS}
"""

# A saved report's visual is drawn by pheasant's own MCP App view, hosted
# here exactly as the web UI and an MCP host host it: initialize, then the
# result. Nothing is fetched; "Redraw as" has no region to ask and says so.
_VISUAL_HOST = """
(() => {
  const frame = document.getElementById("visual");
  const data = JSON.parse(document.getElementById("visual-data").textContent);
  const root = document.documentElement;
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const theme = root.dataset.theme || (dark ? "dark" : "light");
  const post = (message) => frame.contentWindow.postMessage({ jsonrpc: "2.0", ...message }, "*");
  addEventListener("message", (event) => {
    if (event.source !== frame.contentWindow) return;
    const m = event.data || {};
    if (m.method === "ui/initialize") {
      post({ id: m.id, result: { protocolVersion: (m.params || {}).protocolVersion,
        hostInfo: { name: "pheasant-saved-report", version: "1" }, hostCapabilities: {},
        hostContext: { theme, displayMode: "inline", availableDisplayModes: ["inline"] } } });
    } else if (m.method === "ui/notifications/initialized") {
      post({ method: "ui/notifications/tool-result",
        params: { content: [], structuredContent: data } });
    } else if (m.method === "ui/notifications/size-changed") {
      frame.style.height = Math.min(1600, Math.max(140, (m.params || {}).height || 0)) + "px";
    } else if (m.id !== undefined) {
      post({ id: m.id, error: { code: -32601, message: "a saved report cannot ask the region" } });
    }
  });
})();
"""


def _esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def lexer_for(name: str, language: str | None = None) -> Any:
    for attempt in (
        lambda: get_lexer_for_filename(name),
        lambda: get_lexer_by_name(language or ""),
    ):
        try:
            return attempt()
        except ClassNotFound:
            continue
    return TextLexer()


def highlighted(code: str, name: str = "", language: str | None = None) -> str:
    """``code`` as pheasant-coloured HTML in a ``<pre>``."""

    body = highlight(code, lexer_for(name, language), HtmlFormatter(nowrap=True))
    return f'<pre class="hl"><code>{body}</code></pre>'


def _fence(code: str, language: str, _attrs: Any) -> str:
    return highlight(code, lexer_for("", language or None), HtmlFormatter(nowrap=True))


_MARKDOWN.options["highlight"] = _fence


def markdown_html(text: str, citations: set[int] | None = None) -> str:
    """Markdown with raw HTML off, its ``[n]`` linked to the sources list."""

    if citations:
        parts = str(text or "").split("```")
        for index in range(0, len(parts), 2):  # prose, never inside a fence
            parts[index] = _CITATION.sub(
                lambda m: (
                    f"[[{m.group(1)}]](#source-{m.group(1)})"
                    if int(m.group(1)) in citations
                    else m.group(0)
                ),
                parts[index],
            )
        text = "```".join(parts)
    rendered = _MARKDOWN.render(str(text or ""))
    return rendered.replace("<pre><code", '<pre class="hl"><code')


def _page(title: str, body: str, *, theme: str | None = None, script: str = "") -> str:
    themed = f' data-theme="{_esc(theme)}"' if theme in ("light", "dark") else ""
    scripts = f"<script>{script}</script>" if script else ""
    return (
        f'<!doctype html><html lang="en"{themed}><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{DOCUMENT_POLICY}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main>{body}</main>{scripts}</body></html>"
    )


def _header(artifact: dict[str, Any]) -> str:
    payload = artifact.get("payload") or {}
    kind = KIND_LABELS.get(str(artifact.get("kind")), str(artifact.get("kind") or "Artifact"))
    created = str(artifact.get("created_at") or "")[:16].replace("T", " ")
    files = payload.get("files") or []
    counts = f" · {len(files)} file{'s' if len(files) != 1 else ''}" if files else ""
    question = payload.get("question")
    return (
        f"<h1>{_esc(artifact.get('title'))}</h1>"
        f'<p class="meta"><span class="badge">{_esc(kind)}</span>{_esc(created)} UTC{counts}</p>'
        + (f'<p class="question">{_esc(question)}</p>' if question else "")
    )


def _sources(citations: list[dict[str, Any]]) -> str:
    if not citations:
        return ""
    items = "".join(
        f'<li id="source-{_esc(c.get("index"))}" value="{_esc(c.get("index"))}">'
        f"{_esc(c.get('source'))} · {_esc(c.get('path'))}"
        + (f" — {_esc(c.get('section'))}" if c.get("section") else "")
        + "</li>"
        for c in citations
        if c.get("index") is not None
    )
    return f'<section class="sources"><h2>Sources</h2><ol>{items}</ol></section>'


def _visual(payload: dict[str, Any]) -> tuple[str, str]:
    visual = payload.get("visual")
    # A drawn diagram. An image gallery is not kept: each picture is fetched
    # from the region by node, and a saved report has no region to ask.
    if not isinstance(visual, dict) or visual.get("status") != "ok" or not visual.get("diagram"):
        return "", ""
    try:
        view = _KNOWLEDGE_VIEW.read_text(encoding="utf-8")
    except OSError:  # an install without the view's asset: the report stands without it
        return "", ""
    data = json.dumps(
        {
            "visual": visual,
            "citations": payload.get("visual_citations") or payload.get("citations") or [],
        },
        default=str,
    ).replace("<", "\\u003c")
    frame = (
        f'<iframe id="visual" class="visual-frame" sandbox="allow-scripts" '
        f'title="{_esc(visual.get("title") or "Visual")}" srcdoc="{_esc(view)}"></iframe>'
        f'<script type="application/json" id="visual-data">{data}</script>'
    )
    return frame, _VISUAL_HOST


def has_visual(payload: dict[str, Any]) -> bool:
    """Whether a saved document draws a diagram (see :func:`_visual`)."""

    visual = payload.get("visual")
    return isinstance(visual, dict) and visual.get("status") == "ok" and bool(visual.get("diagram"))


def _file_section(file: dict[str, Any]) -> str:
    """One file in the whole-artifact page: drawn when it is a picture or a page.

    An HTML file is its page, running in a frame sandboxed to scripts alone
    under :data:`DOCUMENT_POLICY` (its charts draw; it reaches nothing), and an
    SVG is a picture. Either keeps its source one click away; everything else
    is its source.
    """

    name = str(file.get("name") or "file")
    content = str(file.get("content") or "")
    suffix = suffix_of(name, file.get("language"))
    head = (
        f'<section class="file"><p class="file__name"><strong>{_esc(name)}</strong>'
        f'<span class="file__lang">{_esc(file.get("language"))}</span></p>'
    )
    source = highlighted(content, name, file.get("language"))
    if suffix in (".html", ".htm"):
        drawn = (
            f'<iframe class="html-frame" sandbox="allow-scripts" title="{_esc(name)}" '
            f'srcdoc="{_esc(with_policy(content))}"></iframe>'
        )
    elif suffix == ".svg":
        import base64

        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        drawn = (
            f'<img class="svg-preview" alt="{_esc(name)}" '
            f'src="data:image/svg+xml;base64,{encoded}">'
        )
    else:
        return head + source + "</section>"
    return head + drawn + f"<details><summary>Source</summary>{source}</details></section>"


def overview(artifact: dict[str, Any], *, theme: str | None = None) -> str:
    """The whole artifact as one document: the report, or every file in order."""

    payload = artifact.get("payload") or {}
    citations = payload.get("citations") or []
    cited = {int(c["index"]) for c in citations if str(c.get("index", "")).isdigit()}
    parts = [_header(artifact)]
    if artifact.get("kind") in ("report", "thread"):
        parts.append(markdown_html(payload.get("answer") or "", cited))
    elif payload.get("summary"):
        parts.append(markdown_html(payload["summary"], cited))
    visual, script = _visual(payload)
    parts.append(visual)
    for file in payload.get("files") or []:
        parts.append(_file_section(file))
    if payload.get("not_covered"):
        parts.append(
            '<section class="gaps"><h2>Not covered by the sources</h2>'
            f"{markdown_html(payload['not_covered'], cited)}</section>"
        )
    parts.append(_sources(citations))
    parts.append("<footer>Saved from pheasant.</footer>")
    return _page(
        str(artifact.get("title") or "Artifact"), "".join(parts), theme=theme, script=script
    )


def find_file(artifact: dict[str, Any], name: str) -> dict[str, Any] | None:
    for file in (artifact.get("payload") or {}).get("files") or []:
        if file.get("name") == name:
            return file
    return None


def suffix_of(name: str, language: str | None = None) -> str:
    """A file's kind: its extension, or for one with none, what its language implies."""

    from pheasant.assistant.artifact_files import extension_for

    return PurePosixPath(name).suffix.lower() or extension_for(language)


def previewable(name: str, language: str | None = None) -> bool:
    return suffix_of(name, language) in PREVIEWABLE


def with_policy(document: str) -> str:
    """An HTML file with :data:`DOCUMENT_POLICY` ahead of everything it says."""

    meta = f'<meta http-equiv="Content-Security-Policy" content="{DOCUMENT_POLICY}">'
    head = re.search(r"<head[^>]*>", document, re.I)
    if head:
        return document[: head.end()] + meta + document[head.end() :]
    return meta + document


def file_document(
    artifact: dict[str, Any], file: dict[str, Any], *, mode: str = "code", theme: str | None = None
) -> str:
    """One file: its source, pheasant-coloured, or drawn as what it is."""

    name = str(file.get("name") or "file")
    content = str(file.get("content") or "")
    suffix = suffix_of(name, file.get("language"))
    if mode == "preview" and suffix in (".html", ".htm"):
        return with_policy(content)
    if mode == "preview" and suffix in (".md", ".markdown"):
        return _page(name, markdown_html(content), theme=theme)
    if mode == "preview" and suffix == ".svg":
        import base64

        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        # As an image: an SVG drawn by <img> runs none of its scripts.
        return _page(
            name,
            f'<img class="svg-preview" alt="{_esc(name)}" '
            f'src="data:image/svg+xml;base64,{encoded}">',
            theme=theme,
        )
    body = (
        f'<p class="file__name"><strong>{_esc(name)}</strong>'
        f'<span class="file__lang">{_esc(file.get("language"))}</span></p>'
        f"{highlighted(content, name, file.get('language'))}"
    )
    return _page(f"{name} — {artifact.get('title') or ''}", body, theme=theme)


def markdown_export(artifact: dict[str, Any]) -> str:
    """The artifact as one Markdown file: what a reader pastes elsewhere."""

    payload = artifact.get("payload") or {}
    parts = [f"# {artifact.get('title') or 'Artifact'}"]
    if payload.get("question"):
        parts.append(f"> {payload['question']}")
    if artifact.get("kind") in ("report", "thread"):
        parts.append(str(payload.get("answer") or ""))
    elif payload.get("summary"):
        parts.append(str(payload["summary"]))
    for file in payload.get("files") or []:
        parts.append(
            f"## `{file.get('name')}`\n\n```{file.get('language') or ''}\n"
            f"{file.get('content') or ''}\n```"
        )
    if payload.get("not_covered"):
        parts.append(f"## Not covered by the sources\n\n{payload['not_covered']}")
    citations = payload.get("citations") or []
    if citations:
        parts.append(
            "## Sources\n\n"
            + "\n".join(
                f"{c.get('index')}. {c.get('source')} · {c.get('path')}"
                + (f" — {c.get('section')}" if c.get("section") else "")
                for c in citations
            )
        )
    return "\n\n".join(part for part in parts if part.strip()) + "\n"


def safe_name(name: str, fallback: str) -> str:
    """A path inside the archive: relative, no ``..``, never empty."""

    parts = [
        part
        for part in PurePosixPath(str(name).replace("\\", "/")).parts
        if part not in ("", ".", "..", "/")
    ]
    return "/".join(parts) or fallback


def slug(title: str) -> str:
    return (
        re.sub(r"[^A-Za-z0-9]+", "-", str(title or "artifact")).strip("-").lower()[:60]
        or "artifact"
    )


def zip_export(artifact: dict[str, Any]) -> bytes:
    """Every file at its own path, the whole artifact as HTML, and a README."""

    payload = artifact.get("payload") or {}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        taken: set[str] = set()
        for number, file in enumerate(payload.get("files") or [], 1):
            path = safe_name(str(file.get("name") or ""), f"file-{number}.txt")
            if path in taken:
                path = f"{number}-{path}"
            taken.add(path)
            archive.writestr(path, str(file.get("content") or ""))
        archive.writestr("README.md", markdown_export(artifact))
        archive.writestr(f"{slug(str(artifact.get('title')))}.html", overview(artifact))
    return buffer.getvalue()
