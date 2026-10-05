"""The MCP App view, expanded: full size, rearrangeable, and back again.

``ui/request-display-mode`` is how an MCP Apps view asks its host for the whole
window. What this holds down, in a real browser against the real view file and
the real spec validator (a jsdom would not lay out an SVG, and the whole point
is where the nodes end up):

* the Expand button exists only where the host says it can give the view
  ``fullscreen`` — a host that never advertised the mode is never asked;
* expanded, a node can be dragged and **every edge that touches it follows**,
  for each of the twelve shapes whose nodes are free to move; Reset puts the
  drawn layout back exactly, and collapsing keeps the layout that was made;
* a shape whose nodes sit on something (a lifeline, an axis, a table) expands
  but is not draggable, rather than letting a drag detach a node from what it
  is a node *of*;
* a click on a node is still "tell me more" inline, and is *not* once expanded
  — that message would land in a conversation the frame is covering;
* Escape and the Collapse button both leave the mode, and tell the host.

Skipped where Playwright or a Chromium build is absent (like lancedb and
wasmtime, a runtime the suite cannot assume). ``PHEASANT_REQUIRE_BROWSER=1`` —
set by the CI job that installs one — turns that skip into a failure, so the
job cannot go green by testing nothing.
"""

from __future__ import annotations

import glob
import json
import os
from collections.abc import Iterator
from typing import Any

import pytest

from pheasant.assistant import visuals
from pheasant.mcp_server.assistant_tools import app_html
from tests.test_visual_shapes import CITATIONS, SPECS

REQUIRED = os.environ.get("PHEASANT_REQUIRE_BROWSER") == "1"

#: How long the stand-in host takes to grant a display mode (see HOST).
HOST_LAG_MS = 120

#: Nodes free to move: every edge is drawn between the boxes, so it can follow.
MOVABLE = (
    "flow",
    "hierarchy",
    "mindmap",
    "concept",
    "cycle",
    "swimlane",
    "layers",
    "canvas",
    "class",
    "activity",
    "state",
    "usecase",
)
#: Nodes that sit on a lifeline, an axis, a grid or in a table.
FIXED = ("sequence", "timeline", "groups", "table", "quadrant", "chart")

#: A stand-in host: the half of the protocol that matters here. It answers
#: ``ui/initialize`` with the modes it can give, grants ``ui/request-display-mode``
#: by restyling the same frame (as pheasant's UI does) and logs everything the
#: view sends so a test can say what did and did not reach the host.
HOST = """<!doctype html><meta charset=utf-8>
<style>
  body { margin: 0; font: 14px sans-serif; }
  #f { width: 720px; height: 420px; border: 1px solid #999; display: block; }
  #f.full { position: fixed; inset: 0; width: 100vw; height: 100vh; z-index: 9; }
</style>
<iframe id=f sandbox=allow-scripts></iframe>
<script>
window.__log = [];
window.__mode = "inline";
const frame = document.getElementById("f");
const modes = %(modes)s;
const send = (m) => frame.contentWindow.postMessage({ jsonrpc: "2.0", ...m }, "*");
window.addEventListener("message", (e) => {
  if (e.source !== frame.contentWindow) return;
  const m = e.data;
  if (!m || m.jsonrpc !== "2.0" || !m.method) return;
  window.__log.push({ method: m.method, params: m.params || {} });
  if (m.method === "ui/initialize") {
    send({ id: m.id, result: { protocolVersion: "2026-01-26",
      hostInfo: { name: "test", version: "1" },
      hostContext: { theme: "light", displayMode: "inline", availableDisplayModes: modes } } });
  } else if (m.method === "ui/notifications/initialized") {
    send({ method: "ui/notifications/tool-result",
      params: { content: [], structuredContent: %(result)s } });
  } else if (m.method === "ui/request-display-mode") {
    // A real host takes a beat to restyle its frame, and the view does not wait
    // for it when collapsing. Without this lag the stand-in answers before a
    // test can act, and a test that races the host passes here and fails on a
    // slower machine.
    setTimeout(() => {
      const mode = modes.includes(m.params.mode) ? m.params.mode : window.__mode;
      window.__mode = mode;
      frame.classList.toggle("full", mode === "fullscreen");
      send({ id: m.id, result: { mode } });
      send({ method: "ui/notifications/host-context-changed", params: { displayMode: mode } });
    }, %(lag)d);
  } else if (m.id !== undefined) {
    send({ id: m.id, error: { code: -32601, message: "not in this host" } });
  }
});
frame.srcdoc = %(view)s;
</script>"""


def _browser_launch(playwright: Any) -> Any:
    try:
        return playwright.chromium.launch()
    except Exception:  # noqa: BLE001 - any launch failure means "no usable browser"
        pass
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    for candidate in sorted(glob.glob(os.path.join(root, "chromium*", "chrome-linux*", "chrome"))):
        try:
            return playwright.chromium.launch(executable_path=candidate)
        except Exception:  # noqa: BLE001
            continue
    if REQUIRED:
        pytest.fail("PHEASANT_REQUIRE_BROWSER=1 but no Chromium could be launched")
    pytest.skip("no Chromium available for Playwright")


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if REQUIRED:
            pytest.fail("PHEASANT_REQUIRE_BROWSER=1 but playwright is not installed")
        pytest.skip("playwright is not installed")
    with sync_playwright() as playwright:
        chromium = _browser_launch(playwright)
        try:
            yield chromium
        finally:
            chromium.close()


def _visual(kind: str) -> dict:
    """The real validator's verdict on one representative spec of this kind."""
    visual = visuals.validate_spec({"kind": kind, "title": kind, **SPECS[kind]}, CITATIONS)
    assert visual["status"] == "ok", visual
    return visual


def _open(browser: Any, kind: str, *, modes: tuple[str, ...] = ("inline", "fullscreen")) -> Any:
    page = browser.new_page(viewport={"width": 1100, "height": 760})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.errors = errors  # type: ignore[attr-defined]
    payload = {"visual": _visual(kind), "citations": []}
    page.set_content(
        HOST
        % {
            "modes": json.dumps(list(modes)),
            "lag": HOST_LAG_MS,
            "result": json.dumps(payload).replace("</", "<\\/"),
            # `</script>` inside a script element ends it, whatever string it is in.
            "view": json.dumps(app_html()).replace("</", "<\\/"),
        }
    )
    frame = page.frame_locator("#f")
    frame.locator("svg, .diagram > *").first.wait_for()
    return page, frame


def _log(page: Any, method: str) -> list[dict]:
    return [entry for entry in page.evaluate("window.__log") if entry["method"] == method]


def _edge_paths(frame: Any) -> list[str]:
    return frame.locator("svg g.edge path").evaluate_all("els => els.map(e => e.getAttribute('d'))")


def _biggest_node(frame: Any) -> Any:
    """The largest node: a tiny UML start marker is a poor thing to aim at."""
    nodes = frame.locator("svg g.node")
    best, area = None, -1.0
    for i in range(nodes.count()):
        box = nodes.nth(i).bounding_box()
        if box and box["width"] * box["height"] > area:
            best, area = nodes.nth(i), box["width"] * box["height"]
    return best


def _drag(page: Any, node: Any, dx: float, dy: float) -> None:
    box = node.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx / 2, y + dy / 2, steps=6)
    page.mouse.move(x + dx, y + dy, steps=6)
    page.mouse.up()


def _host_in(page: Any, mode: str) -> None:
    """Wait until the *host* is in ``mode`` and has restyled its frame to match.

    The view leaves fullscreen the moment Collapse is pressed — it must not be
    stranded by a host that never answers — so "the view says inline" and "the
    host has resized the frame" are different instants. A click or a read in
    between lands on a frame that is still moving.
    """
    page.wait_for_function(
        "(mode) => window.__mode === mode && "
        "document.getElementById('f').classList.contains('full') === (mode === 'fullscreen')",
        arg=mode,
    )


def _expand(page: Any, frame: Any) -> None:
    frame.locator("button.tool", has_text="Expand").click()
    frame.locator("body.fs").wait_for()
    _host_in(page, "fullscreen")


def _collapse(page: Any, frame: Any) -> None:
    frame.locator("button.tool", has_text="Collapse").click()
    frame.locator("body:not(.fs)").wait_for()
    _host_in(page, "inline")


def _messages(page: Any, count: int) -> list[dict]:
    """Wait for ``count`` ``ui/message`` posts to reach the host, and no more.

    A post crosses a frame boundary asynchronously, so reading the host's log
    straight after a click reads what had arrived, not what was sent. A count
    that must *not* grow is given a moment to prove it.
    """
    if count:
        try:
            page.wait_for_function(
                "(n) => window.__log.filter((e) => e.method === 'ui/message').length >= n",
                arg=count,
                timeout=10_000,
            )
        except Exception as error:  # noqa: BLE001 - re-raised with what did arrive
            seen = [entry["method"] for entry in page.evaluate("window.__log")]
            raise AssertionError(f"expected {count} ui/message post(s); host saw {seen}") from error
    else:
        page.wait_for_timeout(300)
    return _log(page, "ui/message")


@pytest.mark.parametrize("kind", MOVABLE)
def test_a_dragged_node_carries_its_edges_and_reset_puts_them_back(browser: Any, kind: str) -> None:
    page, frame = _open(browser, kind)
    try:
        _expand(page, frame)
        assert page.evaluate("window.__mode") == "fullscreen"
        frame.locator(".diagram.movable").wait_for()
        drawn = _edge_paths(frame)

        node = _biggest_node(frame)
        before = node.bounding_box()
        _drag(page, node, 140, 70)
        after = node.bounding_box()
        assert abs(after["x"] - before["x"]) > 40 or abs(after["y"] - before["y"]) > 20, kind
        assert node.get_attribute("transform"), "a moved node is translated, not redrawn"
        if drawn:
            assert _edge_paths(frame) != drawn, f"{kind}: no edge followed the node"

        frame.locator("button.tool", has_text="Reset layout").click()
        assert frame.locator("svg [transform^='translate']").count() == 0
        assert _edge_paths(frame) == drawn, "reset restores the layout's own paths"

        # Move again and leave: the inline picture keeps the layout that was made.
        _drag(page, _biggest_node(frame), -90, 60)
        _collapse(page, frame)
        assert frame.locator("svg g.node[transform]").count() >= 1
        assert page.errors == [], page.errors  # type: ignore[attr-defined]
    finally:
        page.close()


@pytest.mark.parametrize("kind", FIXED)
def test_a_shape_tied_to_an_axis_or_a_grid_expands_but_does_not_come_apart(
    browser: Any, kind: str
) -> None:
    page, frame = _open(browser, kind)
    try:
        _expand(page, frame)
        assert frame.locator(".diagram.movable").count() == 0
        assert frame.locator(".hint").count() == 0
        nodes = frame.locator("svg g.node")
        if nodes.count():
            before = nodes.first.bounding_box()
            _drag(page, nodes.first, 120, 60)
            assert nodes.first.bounding_box() == before, f"{kind} nodes must not move"
        _collapse(page, frame)
        assert page.errors == [], page.errors  # type: ignore[attr-defined]
    finally:
        page.close()


def test_the_button_exists_only_where_the_host_offers_fullscreen(browser: Any) -> None:
    page, frame = _open(browser, "flow", modes=("inline",))
    try:
        frame.locator("svg g.node").first.wait_for()
        assert frame.locator("button.tool", has_text="Expand").count() == 0
        assert _log(page, "ui/request-display-mode") == []
    finally:
        page.close()


def test_a_click_asks_about_a_node_inline_and_not_once_expanded(browser: Any) -> None:
    page, frame = _open(browser, "flow")
    try:
        frame.locator("svg g.node").first.click()
        assert len(_messages(page, 1)) == 1, "inline, a click is still 'tell me more'"

        _expand(page, frame)
        frame.locator("svg g.node").first.click()
        _drag(page, frame.locator("svg g.node").first, 60, 40)
        assert len(_messages(page, 0)) == 1, "expanded, neither a click nor a drag asks"

        _collapse(page, frame)
        frame.locator("svg g.node").first.click()
        assert len(_messages(page, 2)) == 2, "back inline, it asks again"
    finally:
        page.close()


def test_escape_collapses_and_tells_the_host(browser: Any) -> None:
    page, frame = _open(browser, "flow")
    try:
        _expand(page, frame)
        frame.locator("body").press("Escape")
        frame.locator("body:not(.fs)").wait_for()
        _host_in(page, "inline")
        modes = [e["params"]["mode"] for e in _log(page, "ui/request-display-mode")]
        assert modes == ["fullscreen", "inline"]
    finally:
        page.close()


def test_a_host_that_collapses_the_view_itself_is_followed(browser: Any) -> None:
    page, frame = _open(browser, "flow")
    try:
        _expand(page, frame)
        page.evaluate(
            "() => document.getElementById('f').contentWindow.postMessage("
            "{jsonrpc:'2.0', method:'ui/notifications/host-context-changed',"
            " params:{displayMode:'inline'}}, '*')"
        )
        frame.locator("body:not(.fs)").wait_for()
        assert frame.locator("button.tool", has_text="Expand").count() == 1
    finally:
        page.close()


def test_the_expanded_stage_is_the_frame_and_a_node_cannot_leave_it(browser: Any) -> None:
    page, frame = _open(browser, "flow")
    try:
        _expand(page, frame)
        area = frame.locator(".diagram").bounding_box()
        node = _biggest_node(frame)
        _drag(page, node, 5000, 5000)
        box = node.bounding_box()
        assert box["x"] + box["width"] <= area["x"] + area["width"] + 1
        assert box["y"] + box["height"] <= area["y"] + area["height"] + 1
        _drag(page, node, -9000, -9000)
        box = node.bounding_box()
        assert box["x"] >= area["x"] - 1 and box["y"] >= area["y"] - 1
    finally:
        page.close()


def test_a_new_result_leaves_the_mode_rather_than_stranding_the_host(browser: Any) -> None:
    page, frame = _open(browser, "flow")
    try:
        _expand(page, frame)
        page.evaluate(
            "(r) => document.getElementById('f').contentWindow.postMessage("
            "{jsonrpc:'2.0', method:'ui/notifications/tool-result', params:{content:[],"
            " structuredContent:r}}, '*')",
            {"visual": _visual("concept"), "citations": []},
        )
        # A new result replaces the page's contents, so the mode is left rather
        # than stranding the host in fullscreen on nothing.
        frame.locator("body:not(.fs)").wait_for()
        _host_in(page, "inline")
    finally:
        page.close()


# --------------------------------------------------------------------------
# Zoom, pan and fit: the viewBox is the camera, inline and expanded
# --------------------------------------------------------------------------


def _view(frame: Any) -> list[float]:
    raw = frame.locator(".diagram svg").first.get_attribute("viewBox")
    # The browser serializes a viewBox through float32; compare what it means.
    return [round(float(part), 2) for part in raw.split()]


def test_zoom_in_and_out_then_fit_restores_the_framing(browser: Any) -> None:
    """Available on every diagram whether or not the host can expand it."""

    page, frame = _open(browser, "flow", modes=("inline",))
    try:
        home = _view(frame)
        frame.locator("button.tool[aria-label^='Zoom in']").click()
        zoomed = _view(frame)
        assert zoomed[2] < home[2] and zoomed[3] < home[3], "zooming in narrows the view"
        frame.locator("button.tool[aria-label='Zoom out']").click()
        frame.locator("button.tool[aria-label='Zoom out']").click()
        assert _view(frame)[2] > zoomed[2]
        frame.locator("button.tool", has_text="Fit").click()
        assert _view(frame) == home, "fit returns to the drawn framing exactly"
        assert page.errors == []
    finally:
        page.close()


def test_dragging_the_background_pans_and_asks_nothing(browser: Any) -> None:
    page, frame = _open(browser, "flow")
    try:
        frame.locator("button.tool[aria-label^='Zoom in']").click()
        before = _view(frame)
        box = frame.locator(".diagram svg").first.bounding_box()
        # A corner of the canvas: background, not a node.
        x, y = box["x"] + 6, box["y"] + 6
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 40, y + 25, steps=8)
        page.mouse.up()
        after = _view(frame)
        assert after[0] < before[0] and after[1] < before[1], "the view follows the drag"
        assert after[2:] == before[2:], "panning does not zoom"
        assert len(_messages(page, 0)) == 0, "a pan is not a question about a node"
    finally:
        page.close()


def test_ctrl_wheel_zooms_inline_and_a_plain_wheel_does_not(browser: Any) -> None:
    """Inline, a plain wheel must still scroll the conversation past the diagram."""

    page, frame = _open(browser, "flow")
    try:
        home = _view(frame)
        box = frame.locator(".diagram svg").first.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.wheel(0, -200)
        assert _view(frame) == home
        page.keyboard.down("Control")
        page.mouse.wheel(0, -200)
        page.keyboard.up("Control")
        assert _view(frame)[2] < home[2]
    finally:
        page.close()


def test_expanded_zoom_and_fit_return_to_the_stage(browser: Any) -> None:
    page, frame = _open(browser, "flow")
    try:
        _expand(page, frame)
        stage = _view(frame)
        frame.locator("button.tool[aria-label^='Zoom in']").click()
        assert _view(frame)[2] < stage[2]
        frame.locator("button.tool", has_text="Fit").click()
        assert _view(frame) == stage
    finally:
        page.close()
