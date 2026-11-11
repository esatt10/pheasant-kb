"""Dictation in the composer, in a real browser, end to end.

The real region serving the built web UI, Chromium with a fake microphone,
and a stand-in for OpenAI's Realtime transcription wire on a real socket
(``tests/fake_realtime.py``). What it holds down:

* the microphone button sits beside Send and says whether it is on or off
  (``aria-pressed``, ``data-state``), with the time left while it listens;
* pressing it mints a session through the region and streams real 16-bit PCM
  at 24 kHz to it — the stand-in counts the bytes against the clock, so a
  broken resampler or an empty stream fails here, not in production;
* transcript deltas stream into the question box **at the caret**, word by
  word, between text that was already there;
* pressing it again stops at once (the button is off before the last words
  arrive) and the final words still land; a new dictation starts with no wait;
* a dictation stops itself at ``assistant.voice.max_seconds`` and says so;
* sending the question ends a dictation, and nothing more is written.

Needs the built UI (``ui/dist``, ``npm run build``) and Chromium; skipped
without either unless ``PHEASANT_REQUIRE_BROWSER=1``, which the CI browser job
sets after building the UI, so that job cannot go green by testing nothing.
"""

from __future__ import annotations

import glob
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fake_realtime import TRANSCRIPT, FakeRealtime, serve, stop

REQUIRED = os.environ.get("PHEASANT_REQUIRE_BROWSER") == "1"
DIST = Path(__file__).resolve().parents[1] / "ui" / "dist"
#: Short, so the limit is reached inside a test; the shipped default is 60.
LIMIT_SECONDS = 4
CHROMIUM_ARGS = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]
SPOKEN = "".join(TRANSCRIPT)
BOX = "textarea[aria-label='Ask a question']"


def _skip(reason: str) -> None:
    if REQUIRED:
        pytest.fail(f"{reason} (PHEASANT_REQUIRE_BROWSER=1)")
    pytest.skip(reason)


def _launch(playwright: Any) -> Any:
    try:
        return playwright.chromium.launch(args=CHROMIUM_ARGS)
    except Exception:  # noqa: BLE001 - a missing bundled build; try a shared one
        pass
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    for candidate in sorted(glob.glob(os.path.join(root, "chromium*", "chrome-linux*", "chrome"))):
        try:
            return playwright.chromium.launch(executable_path=candidate, args=CHROMIUM_ARGS)
        except Exception:  # noqa: BLE001
            continue
    _skip("no Chromium available for Playwright")


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _skip("playwright is not installed")
    if not (DIST / "index.html").is_file():
        _skip("the web UI is not built (cd ui && npm ci && npm run build)")
    with sync_playwright() as playwright:
        chromium = _launch(playwright)
        try:
            yield chromium
        finally:
            chromium.close()


@pytest.fixture
def region(tmp_path, monkeypatch) -> Iterator[dict[str, Any]]:
    yield from _region(tmp_path, monkeypatch, transport="direct")


@pytest.fixture
def relay_region(tmp_path, monkeypatch) -> Iterator[dict[str, Any]]:
    """A region relaying dictation, to a provider that takes a header only (Foundry's rule)."""

    yield from _region(tmp_path, monkeypatch, transport="relay")


def _region(tmp_path, monkeypatch, *, transport: str) -> Iterator[dict[str, Any]]:
    from pheasant.api.app import create_app
    from pheasant.config.schema import PheasantConfig

    provider = FakeRealtime()
    if transport == "relay":
        provider.header_keys = {"sk-region-secret"}
    provider_server, provider_thread, provider_url = serve(provider.app())
    monkeypatch.setenv("PHEASANT_UI_DIST", str(DIST))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-region-secret")
    # One indexed note, so the Notebook shows the chat rather than onboarding.
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "sync.md").write_text(
        "# Sync lease\n\nOne indexer holds the sync lease.\n", encoding="utf-8"
    )
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "dictation",
                "defaults": "standalone",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "storage": {"graph_snapshots": False},
            "sources": [
                {
                    "name": "notes",
                    "type": "markdown_folder",
                    "path": str(notes),
                    "include": ["**/*.md"],
                }
            ],
            # No chat model: a sent question is answered extractively, offline.
            "assistant": {
                "provider": "none",
                "voice": {
                    "base_url": f"{provider_url}/v1",
                    "max_seconds": LIMIT_SECONDS,
                    "transport": transport,
                },
            },
        }
    )
    from pheasant.mcp_server.tools import PheasantTools

    PheasantTools(config).engine.sync_source("notes", "full")
    app = create_app(config, config_path=str(tmp_path / "pheasant.yaml"))
    server, thread, url = serve(app)
    try:
        yield {"url": url, "provider": provider}
    finally:
        stop(server, thread)
        stop(provider_server, provider_thread)


#: Keeps every microphone stream the page opens, so a test can ask whether
#: its tracks were released (`readyState === "ended"`).
TRACK_STREAMS = """
const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
window.__streams = [];
navigator.mediaDevices.getUserMedia = async (constraints) => {
  const stream = await original(constraints);
  window.__streams.push(stream);
  return stream;
};
"""


def _live_tracks(page: Any) -> int:
    return page.evaluate(
        "() => window.__streams.flatMap(s => s.getTracks())"
        ".filter(t => t.readyState === 'live').length"
    )


def _page(browser: Any, url: str) -> Any:
    context = browser.new_context(permissions=["microphone"])
    context.add_init_script(TRACK_STREAMS)
    page = context.new_page()
    page.goto(url)
    page.wait_for_selector(BOX)
    return page


def _value(page: Any) -> str:
    return page.eval_on_selector(BOX, "el => el.value")


def _mic(page: Any) -> Any:
    return page.locator(".composer__mic-btn")


def _wait_state(page: Any, state: str, timeout: float = 10_000) -> None:
    page.wait_for_function(
        "s => document.querySelector('.composer__mic-btn')?.dataset.state === s",
        arg=state,
        timeout=timeout,
    )


def _wait_value(page: Any, expected: str, *, prefix: bool = False, timeout: float = 8_000) -> None:
    """Until the question box holds ``expected`` (or starts with it)."""

    page.wait_for_function(
        "([sel, text, prefix]) => { const v = document.querySelector(sel).value;"
        " return prefix ? v.startsWith(text) : v === text; }",
        arg=[BOX, expected, prefix],
        timeout=timeout,
    )


def test_speech_streams_in_at_the_caret_and_stops_when_asked(browser, region) -> None:
    page = _page(browser, region["url"])
    box = page.locator(BOX)
    box.fill("Tell me about  please")
    # The caret between the two spaces: dictation is written there.
    box.evaluate("el => { el.focus(); el.setSelectionRange(14, 14); }")

    mic = _mic(page)
    mic.wait_for(state="visible")
    assert mic.get_attribute("aria-pressed") == "false"
    assert mic.get_attribute("data-state") == "off"
    # Beside Send, in the composer.
    assert page.locator(".composer .composer__mic + .composer__send").count() == 1

    mic.click()
    _wait_state(page, "live")
    assert mic.get_attribute("aria-pressed") == "true"
    assert page.locator(".composer__mic-time").inner_text().startswith("0:0")

    # Words arrive one at a time, in place, while the person is still speaking.
    seen: list[str] = []
    deadline = time.monotonic() + 8
    while (
        time.monotonic() < deadline
        and _value(page).count(" ") < len("Tell me about  please".split()) + 3
    ):
        current = _value(page)
        if not seen or seen[-1] != current:
            seen.append(current)
        page.wait_for_timeout(50)
    seen.append(_value(page))
    streamed = [value for value in seen if value != "Tell me about  please"]
    assert len(set(streamed)) >= 3, f"text did not stream word by word: {seen}"
    for value in streamed:
        assert value.startswith("Tell me about What"), value
        assert value.endswith(" please"), value

    mic.click()
    # Off at once — before the last words have arrived.
    assert mic.get_attribute("aria-pressed") == "false"
    _wait_value(page, "Tell me about " + SPOKEN + " please")
    caret = box.evaluate("el => el.selectionStart")
    assert caret == len("Tell me about " + SPOKEN)
    assert _live_tracks(page) == 0, "the microphone is still on after stopping"

    provider = region["provider"]
    session = provider.sessions[0]
    assert session.committed
    assert session.query == "intent=transcription"
    assert "realtime" in session.protocols
    # Real PCM16 at 24 kHz: 48,000 bytes a second of streaming, within slack.
    seconds = session.last_audio_at - session.first_audio_at
    assert session.audio_bytes % 2 == 0
    assert seconds > 0.5
    rate = session.audio_bytes / 2 / seconds
    assert 18_000 < rate < 30_000, f"{rate:.0f} samples/s is not a 24 kHz stream"
    # The region minted it with its own key, and the browser never saw that key.
    assert provider.minted[0]["headers"]["authorization"] == "Bearer sk-region-secret"
    assert "sk-region-secret" not in page.content()


def test_a_relayed_dictation_streams_through_the_region(browser, relay_region) -> None:
    """The same dictation with ``transport: relay``: the browser talks only to the region.

    The stand-in admits a credential only as a header, as Azure AI Foundry does,
    so this passes only if the region's relay opened the provider's socket with
    its own key -- and the page never held that key or a provider secret.
    """

    page = _page(browser, relay_region["url"])
    sockets: list[str] = []
    page.on("websocket", lambda socket: sockets.append(socket.url))
    box = page.locator(BOX)
    box.fill("Ask ")
    box.evaluate("el => { el.focus(); el.setSelectionRange(4, 4); }")
    mic = _mic(page)
    mic.wait_for(state="visible")
    mic.click()
    _wait_state(page, "live")
    _wait_value(page, "Ask What", prefix=True)
    mic.click()
    _wait_value(page, "Ask " + SPOKEN)
    assert _live_tracks(page) == 0

    provider = relay_region["provider"]
    # The page opened the region's relay and nothing else.
    region_host = relay_region["url"].split("//", 1)[1]
    # (`/api/…` or `/…`, whichever API base the bundle was built with.)
    assert sockets and all(
        url.startswith(f"ws://{region_host}/") and url.endswith("/assistant/voice/relay")
        for url in sockets
    ), sockets
    assert not provider.minted, "a relayed dictation asks the provider for no secret"
    session = provider.sessions[0]
    assert session.headers["authorization"] == "Bearer sk-region-secret"
    assert not any(p.startswith("openai-insecure-api-key.") for p in session.protocols)
    assert session.updates and session.committed
    # Real PCM16 crossed the relay (it stops at the first word: a few hundred ms).
    assert session.audio_bytes > 4_800 and session.audio_bytes % 2 == 0
    assert "sk-region-secret" not in page.content()


def test_the_limit_turns_it_off_and_another_starts_at_once(browser, region) -> None:
    page = _page(browser, region["url"])
    box = page.locator(BOX)
    box.fill("Question:")
    box.evaluate("el => { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }")
    mic = _mic(page)

    mic.click()
    _wait_state(page, "live")
    started = time.monotonic()
    _wait_state(page, "off", timeout=(LIMIT_SECONDS + 4) * 1000)
    elapsed = time.monotonic() - started
    assert LIMIT_SECONDS - 0.5 <= elapsed <= LIMIT_SECONDS + 1.5, elapsed
    assert mic.get_attribute("aria-pressed") == "false"
    assert f"{LIMIT_SECONDS}-second limit" in page.locator(".composer__mic-note").inner_text()
    _wait_value(page, "Question: " + SPOKEN)

    # No wait between dictations: the next one is live straight away.
    mic.click()
    _wait_state(page, "live", timeout=5_000)
    assert len(region["provider"].minted) == 2
    _wait_value(page, "Question: " + SPOKEN + " What", prefix=True, timeout=5_000)
    mic.click()
    _wait_value(page, "Question: " + SPOKEN + " " + SPOKEN)


def test_sending_the_question_ends_dictation_and_writes_nothing_more(browser, region) -> None:
    page = _page(browser, region["url"])
    box = page.locator(BOX)
    mic = _mic(page)
    box.focus()
    mic.click()
    _wait_value(page, "What", prefix=True)
    box.press("Enter")
    _wait_state(page, "off")
    page.wait_for_timeout(1500)
    assert _value(page) == ""
    assert not region["provider"].sessions[0].committed


def test_a_refused_session_turns_off_says_why_and_frees_the_microphone(browser, region) -> None:
    region["provider"].refuse = (401, "Incorrect API key provided")
    page = _page(browser, region["url"])
    page.locator(BOX).focus()
    _mic(page).click()
    page.wait_for_selector(".composer__mic-note")
    _wait_state(page, "off")
    note = page.locator(".composer__mic-note").inner_text()
    assert "did not start a session" in note and "Incorrect API key" in note
    assert _mic(page).get_attribute("aria-pressed") == "false"
    page.wait_for_function("() => window.__streams.length > 0")
    assert _live_tracks(page) == 0
    assert _value(page) == ""


def test_a_phone_still_says_why_dictation_stopped(browser, region) -> None:
    """The phone layout hides the composer's notes to save a row; dictation's
    is the one place a blocked microphone or a refusal is said, so it stays."""

    region["provider"].refuse = (401, "Incorrect API key provided")
    context = browser.new_context(
        permissions=["microphone"], viewport={"width": 390, "height": 760}, is_mobile=True
    )
    context.add_init_script(TRACK_STREAMS)
    page = context.new_page()
    page.goto(region["url"])
    page.wait_for_selector(BOX)
    _mic(page).click()
    _wait_state(page, "off")
    note = page.locator(".composer__mic-note")
    note.wait_for(state="visible")
    assert "Incorrect API key" in note.inner_text()
