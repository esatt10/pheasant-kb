"""Renderers: open a produced document the way its reader will.

``print`` is deterministic and always available: a fixed-layout text render
in which a table column has the width its header declares and a cell that
does not fit is cut, as a real fixed-layout medium cuts it. That is what lets
a verifier catch a clipped unit without a browser (fixture A05). ``html`` is
the Markdown rendered as a sandboxed HTML page, and ``screenshot`` opens that
page in Chromium when the host has one; when it does not, the render is
recorded as skipped and every check that needed it stays unverified.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

RENDERER_VERSION = "harness-render-1"
DEFAULT_COLUMN = 12
_WIDTH = re.compile(r"\{w=(\d+)\}")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def render_print(markdown: str) -> dict:
    out: list[str] = []
    clipped: list[dict] = []
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        is_table = (
            line.strip().startswith("|")
            and index + 1 < len(lines)
            and re.match(r"^\s*\|?\s*:?-{3,}", lines[index + 1])
        )
        if not is_table:
            out.append(line)
            index += 1
            continue
        header = _cells(line)
        widths = [int(m.group(1)) if (m := _WIDTH.search(h)) else DEFAULT_COLUMN for h in header]
        names = [_WIDTH.sub("", h).strip() for h in header]
        out.append(" ".join(n[:w].ljust(w) for n, w in zip(names, widths, strict=False)))
        out.append(" ".join("-" * w for w in widths))
        index += 2
        row = 0
        while index < len(lines) and lines[index].strip().startswith("|"):
            cells = _cells(lines[index])
            rendered = []
            for column, (cell, width) in enumerate(zip(cells, widths, strict=False)):
                if len(cell) > width:
                    clipped.append(
                        {"row": row, "column": names[column], "text": cell, "shown": cell[:width]}
                    )
                rendered.append(cell[:width].ljust(width))
            out.append(" ".join(rendered))
            index += 1
            row += 1
    text = "\n".join(out)
    return {"profile": "print", "text": text, "clipped": clipped, "renderer": RENDERER_VERSION}


def render_html(markdown: str, title: str = "Harness artifact") -> str:
    from markdown_it import MarkdownIt

    # Raw HTML off: a document's own markup is text here, never markup.
    body = (
        MarkdownIt("commonmark", {"html": False}).enable("table").render(_WIDTH.sub("", markdown))
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; "
        "style-src 'unsafe-inline'; img-src data:\">"
        f"<title>{html.escape(title)}</title><style>body{{font:15px/1.5 system-ui,sans-serif;"
        "max-width:760px;margin:24px auto;padding:0 16px;color:#1d2125}}table{border-collapse:"
        "collapse}td,th{border:1px solid #c9ccd1;padding:4px 8px}code{background:#f1f2f4}"
        f"</style></head><body>{body}</body></html>"
    )


_SCREENSHOT_JS = r"""
const { chromium } = require('playwright');
(async () => {
  const [input, output, width, height] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: +width, height: +height } });
  await page.goto('file://' + input);
  const overflow = await page.evaluate(() => [...document.querySelectorAll('td,th,p,li,h1,h2,h3')]
    .filter(e => e.scrollWidth > e.clientWidth + 1).map(e => e.textContent.slice(0, 80)));
  await page.screenshot({ path: output, fullPage: true });
  await browser.close();
  console.log(JSON.stringify({ overflow }));
})().catch(e => { console.error(String(e)); process.exit(2); });
"""


def browser_available() -> bool:
    return bool(shutil.which("node")) and os.environ.get("PHEASANT_HARNESS_NO_BROWSER") != "1"


def render_screenshot(page_html: str, viewport: tuple[int, int] = (1024, 768)) -> dict[str, Any]:
    if not browser_available():
        return {"profile": "screenshot", "status": "skipped", "reason": "no browser on this host"}
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.html"
        shot = Path(tmp) / "page.png"
        script = Path(tmp) / "shot.js"
        page.write_text(page_html, encoding="utf-8")
        script.write_text(_SCREENSHOT_JS, encoding="utf-8")
        result = subprocess.run(
            ["node", str(script), str(page), str(shot), str(viewport[0]), str(viewport[1])],
            capture_output=True,
            timeout=90,
            check=False,
        )
        if result.returncode != 0 or not shot.exists():
            return {
                "profile": "screenshot",
                "status": "skipped",
                "reason": (result.stderr.decode(errors="replace") or "browser failed")[:300],
            }
        report = json.loads(result.stdout.decode().strip().splitlines()[-1])
        return {
            "profile": "screenshot",
            "status": "rendered",
            "png": shot.read_bytes(),
            "overflow": report["overflow"],
            "viewport": {"width": viewport[0], "height": viewport[1]},
            "renderer": "chromium via playwright",
        }
