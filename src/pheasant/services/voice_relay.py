"""The dictation relay: one browser WebSocket joined to one provider WebSocket.

The ``relay`` transport of :mod:`pheasant.services.voice`. The browser opens
the region's socket with a one-use ticket; :func:`run` opens the provider's
with the region's credential in a header (the one thing a browser cannot do),
sends the transcription session's settings as the first event, accepts the
browser, and then copies events both ways until either side ends or the
dictation's time is up.

Three rules keep it a dictation relay and not a general-purpose provider
proxy:

* **The session is the region's.** The relay configures it, and drops any
  browser event that is not audio (:data:`BROWSER_EVENTS`), so a ticket
  cannot be used to change the model, the instructions or anything else the
  region's credential pays for. Provider events go to the browser unchanged:
  they are what the composer already reads.
* **A dictation is bounded.** ``assistant.voice.max_seconds`` plus
  :data:`DRAIN_SECONDS` for the last words after a stop, then both sockets
  close. :class:`RelayGate` caps how many run in one process at once.
* **The transport is not this module's.** The browser side is four
  callables (:class:`BrowserSide`), so the api adapter owns Starlette and this
  owns only the provider's client, as :mod:`pheasant.services.voice` owns its
  HTTP call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pheasant.services.voice import RelayTarget
from pheasant.telemetry import metrics

logger = logging.getLogger(__name__)

#: What a browser may send on: the audio buffer, and nothing that configures.
BROWSER_EVENTS = frozenset(
    {"input_audio_buffer.append", "input_audio_buffer.commit", "input_audio_buffer.clear"}
)
#: After ``max_seconds``, how long a stopped dictation may keep delivering
#: its last words before the relay closes it (the composer drains for 4 s).
DRAIN_SECONDS = 15.0
#: How long the provider has to accept the relay's connection.
CONNECT_TIMEOUT_SECONDS = 10.0
#: A frame larger than this from either side ends the relay (an append of
#: ~100 ms of 24 kHz PCM is ~6.4 KB of base64).
MAX_FRAME_BYTES = 1 << 20

#: WebSocket close codes the browser sees.
CLOSE_NORMAL = 1000
CLOSE_POLICY = 1008
CLOSE_UNAVAILABLE = 1011
CLOSE_TRY_AGAIN = 1013


@dataclass(frozen=True)
class BrowserSide:
    """The browser's socket, as the api adapter exposes it."""

    accept: Callable[[], Awaitable[None]]
    #: The next text or binary frame; ``None`` once the browser has gone.
    receive: Callable[[], Awaitable[str | bytes | None]]
    send: Callable[[str | bytes], Awaitable[None]]
    close: Callable[[int, str], Awaitable[None]]


class RelayGate:
    """How many relays one process runs at once; past the cap, refuse."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0

    def acquire(self, limit: int) -> bool:
        with self._lock:
            if self._active >= limit:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    @property
    def active(self) -> int:
        return self._active


# Registered here rather than in `telemetry/metrics.py`, which is at its size
# ceiling (`tests/test_module_budget.py`); registration is idempotent. Each
# dictation counts `connected` once and then how it ended: `refused` is a
# ticket that was missing, spent or expired, `busy` the per-process cap, and
# `provider_unreachable` the transcriber refusing the region's credential or
# not answering -- the one an operator acts on.
RELAY_METRIC = "pheasant_assistant_voice_relay_total"
metrics.REGISTRY.counter(
    RELAY_METRIC,
    "Relayed dictations, by outcome (connected / completed / provider_closed / time_limit / "
    "frame_too_large / provider_unreachable / refused / busy).",
    ("outcome",),
)


def count(outcome: str) -> None:
    metrics.REGISTRY.inc(RELAY_METRIC, outcome=outcome)


def admitted(frame: str | bytes) -> bool:
    """Whether a browser frame is an audio-buffer event the relay forwards."""

    if not isinstance(frame, str):
        return False
    try:
        event = json.loads(frame)
    except ValueError:
        return False
    return isinstance(event, dict) and event.get("type") in BROWSER_EVENTS


async def run(
    browser: BrowserSide,
    target: RelayTarget,
    *,
    connect: Callable[..., Any] | None = None,
    drain_seconds: float | None = None,
) -> str:
    """Relay one dictation; the outcome (also counted) when it ends.

    ``connected`` is never returned: ``completed`` (the browser closed),
    ``provider_closed``, ``time_limit``, ``provider_unreachable`` or
    ``frame_too_large``.
    """

    if connect is None:
        from websockets.asyncio.client import connect as websockets_connect

        connect = websockets_connect
    try:
        upstream = await asyncio.wait_for(
            connect(
                target.url,
                additional_headers=target.headers,
                open_timeout=CONNECT_TIMEOUT_SECONDS,
                max_size=MAX_FRAME_BYTES,
            ),
            CONNECT_TIMEOUT_SECONDS + 1,
        )
    except Exception as exc:  # noqa: BLE001 - every failure here is "not reachable"
        logger.warning("voice relay: the transcriber refused or did not answer: %s", exc)
        count("provider_unreachable")
        await browser.close(CLOSE_UNAVAILABLE, "the transcription service could not be reached")
        return "provider_unreachable"

    outcome = "completed"
    try:
        await upstream.send(json.dumps(target.session_event))
        await browser.accept()
        count("connected")

        async def to_browser() -> str:
            async for message in upstream:
                await browser.send(message)
            return "provider_closed"

        async def to_provider() -> str:
            dropped = 0
            while True:
                frame = await browser.receive()
                if frame is None:
                    return "completed"
                if len(frame) > MAX_FRAME_BYTES:
                    return "frame_too_large"
                if admitted(frame):
                    await upstream.send(frame)
                else:
                    dropped += 1
                    if dropped == 1:
                        logger.info("voice relay: dropped a browser event that is not audio")

        drain = DRAIN_SECONDS if drain_seconds is None else drain_seconds
        tasks = {asyncio.ensure_future(to_browser()), asyncio.ensure_future(to_provider())}
        done, pending = await asyncio.wait(
            tasks,
            timeout=target.max_seconds + drain,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if not done:
            outcome = "time_limit"
        else:
            finished = done.pop()
            outcome = "provider_closed" if finished.exception() else finished.result()
    finally:
        try:
            await upstream.close()
        except Exception:  # noqa: BLE001 - closing a socket that already went
            pass
    reason = {
        "time_limit": "the dictation reached its time limit",
        "frame_too_large": "a frame was too large",
        "provider_closed": "the transcription service closed the session",
    }.get(outcome, "")
    code = CLOSE_POLICY if outcome == "frame_too_large" else CLOSE_NORMAL
    try:
        await browser.close(code, reason)
    except Exception:  # noqa: BLE001 - the browser may already be gone
        pass
    count(outcome)
    return outcome
