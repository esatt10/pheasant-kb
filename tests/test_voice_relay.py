"""Dictation relayed through the region (``assistant.voice.transport: relay``).

Azure AI Foundry's realtime WebSocket takes a credential only as a header
(``api-key``, or ``Authorization: Bearer``), and a browser cannot set one on a
WebSocket, so the composer cannot open it directly. The relay puts the api in
between. What this holds down, over real sockets -- the region under uvicorn,
a stand-in provider that admits a credential the way Foundry does
(``tests/fake_realtime.py``), and a WebSocket client playing the browser:

* ``auto`` relays exactly a Foundry host and leaves OpenAI ``direct``, so the
  standalone default and every OpenAI region are unchanged;
* the session call asks the provider for nothing: it returns a one-use ticket
  and the region's own socket, ``no-store``;
* a relayed dictation is the whole round trip -- the region's key in a header
  upstream, the session configured by the region, audio forwarded, deltas and
  the completed transcript streamed back -- and the ticket never reaches the
  provider, nor the key the browser;
* a ticket opens one dictation; a missing, forged, spent or expired one is
  refused *before* any connection to the provider;
* a browser cannot reconfigure the session: only audio-buffer events pass;
* the provider refusing the region, the per-process cap and the time limit
  each end it cleanly, with the outcome counted;
* a Foundry relay with no key signs with the process's managed identity.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import urllib.request
from types import SimpleNamespace
from typing import Any

import pytest

from pheasant.config.schema import AssistantVoiceSettings, PheasantConfig
from pheasant.services import voice, voice_relay
from tests.fake_realtime import TRANSCRIPT, FakeRealtime, serve, stop

websockets = pytest.importorskip("websockets")
from websockets.asyncio.client import connect  # noqa: E402

REGION_KEY = "sk-region-relay"
#: ~100 ms of 24 kHz PCM16 silence, as the composer appends it.
APPEND = json.dumps(
    {"type": "input_audio_buffer.append", "audio": base64.b64encode(bytes(4800)).decode()}
)


def _config(tmp_path, provider_url: str, **voice_settings: Any) -> PheasantConfig:
    settings = {
        "base_url": f"{provider_url}/v1",
        "transport": "relay",
        "api_key_env": "VOICE_RELAY_KEY",
        **voice_settings,
    }
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "relay",
                "defaults": "standalone",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "sources": [],
            "assistant": {"voice": settings},
        }
    )


@pytest.fixture
def provider():
    fake = FakeRealtime(header_keys={REGION_KEY})
    server, thread, url = serve(fake.app())
    fake.url = url
    try:
        yield fake
    finally:
        stop(server, thread)


@pytest.fixture
def start_region(tmp_path, monkeypatch, provider):
    """Serve a region (``**voice_settings`` over the relay defaults); its base URL."""

    from pheasant.api.app import create_app

    monkeypatch.setenv("VOICE_RELAY_KEY", REGION_KEY)
    running: list[tuple[Any, Any]] = []

    def start(**voice_settings: Any) -> str:
        config = _config(tmp_path, provider.url, **voice_settings)
        server, thread, url = serve(create_app(config, config_path=str(tmp_path / "p.yaml")))
        running.append((server, thread))
        return url

    yield start
    for server, thread in running:
        stop(server, thread)


def _mint(url: str) -> tuple[dict[str, Any], dict[str, str]]:
    request = urllib.request.Request(
        f"{url}/assistant/voice/session",
        data=b'{"session_id": null}',
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read()), dict(response.headers)


def _socket(url: str, ticket: str | None) -> Any:
    protocols = ["realtime"] + ([f"pheasant-voice-ticket.{ticket}"] if ticket else [])
    return connect(
        "ws" + url[len("http") :] + voice.RELAY_PATH, subprotocols=protocols, open_timeout=10
    )


async def _until(socket: Any, kind: str, timeout: float = 8.0) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = json.loads(await asyncio.wait_for(socket.recv(), deadline - time.monotonic()))
        seen.append(event)
        if event.get("type") == kind:
            return seen
    raise AssertionError(f"no {kind} in {[e.get('type') for e in seen]}")


def _store(tmp_path: Any) -> Any:
    from pheasant.persistence.state_store import StateStore

    store = StateStore(tmp_path / "state.db")
    store.migrate()
    return store


def _counted(outcome: str) -> float:
    from pheasant.telemetry import metrics

    return metrics.REGISTRY.value("pheasant_assistant_voice_relay_total", outcome=outcome) or 0.0


# ---------------------------------------------------------------------------
# which transport
# ---------------------------------------------------------------------------


def test_auto_relays_a_foundry_host_and_leaves_openai_direct() -> None:
    foundry = "https://contoso.cognitiveservices.azure.com/openai/v1"
    assert voice.transport(AssistantVoiceSettings(base_url=foundry)) == "relay"
    assert voice.transport(AssistantVoiceSettings(base_url="https://x.services.ai.azure.com")) == (
        "relay"
    )
    assert voice.transport(AssistantVoiceSettings(base_url="https://api.openai.com/v1")) == "direct"
    assert voice.transport(AssistantVoiceSettings(base_url=foundry, transport="direct")) == "direct"
    with pytest.raises(ValueError, match="transport"):
        AssistantVoiceSettings(transport="carrier-pigeon")


def test_the_standalone_default_is_unchanged() -> None:
    settings = PheasantConfig().assistant.voice
    assert settings.transport == "auto"
    assert settings.base_url is None
    assert voice.transport(settings) == "direct"


# ---------------------------------------------------------------------------
# the session call
# ---------------------------------------------------------------------------


def test_a_relay_session_is_a_ticket_for_the_regions_own_socket(start_region, provider) -> None:
    url = start_region()
    minted, headers = _mint(url)
    assert minted["transport"] == "relay"
    assert minted["websocket_url"] == voice.RELAY_PATH
    assert minted["client_secret"].startswith("pht_")
    assert minted["expires_at"] > time.time()
    assert headers.get("cache-control") == "no-store"
    assert not provider.minted, "the session call asks the provider for nothing"
    assert REGION_KEY not in json.dumps(minted)


# ---------------------------------------------------------------------------
# the relay
# ---------------------------------------------------------------------------


def test_a_relayed_dictation_is_the_whole_round_trip(start_region, provider) -> None:
    url = start_region(language="en")
    ticket = _mint(url)[0]["client_secret"]

    async def dictate() -> list[dict[str, Any]]:
        async with _socket(url, ticket) as socket:
            assert socket.subprotocol == "realtime"
            for _ in range(len(TRANSCRIPT) * provider.appends_per_word):
                await socket.send(APPEND)
            await socket.send(json.dumps({"type": "input_audio_buffer.commit"}))
            return await _until(socket, "conversation.item.input_audio_transcription.completed")

    events = asyncio.run(dictate())
    deltas = [e["delta"] for e in events if e["type"].endswith("input_audio_transcription.delta")]
    assert "".join(deltas) == "".join(TRANSCRIPT)
    assert events[-1]["transcript"] == "".join(TRANSCRIPT)

    session = provider.sessions[0]
    # Upstream: the region's own key, in a header, on the transcription socket.
    assert session.headers["authorization"] == f"Bearer {REGION_KEY}"
    assert session.query == "intent=transcription"
    assert not any(ticket in value for value in session.headers.values())
    assert not any(ticket in protocol for protocol in session.protocols)
    # The region configured the session: the model, PCM at 24 kHz, the language.
    configured = session.updates[0]["session"]
    transcription = configured["audio"]["input"]["transcription"]
    assert configured["type"] == "transcription"
    assert transcription == {"model": "gpt-live-transcribe", "language": "en"}
    assert configured["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert session.audio_bytes == 4800 * len(TRANSCRIPT) * provider.appends_per_word
    assert session.committed


def test_a_ticket_opens_one_dictation_only(start_region, provider) -> None:
    url = start_region()
    ticket = _mint(url)[0]["client_secret"]

    async def twice() -> None:
        async with _socket(url, ticket) as socket:
            await _until(socket, "session.updated")
        with pytest.raises(websockets.InvalidStatus):
            async with _socket(url, ticket):
                pass

    asyncio.run(twice())
    assert len(provider.attempts) == 1


@pytest.mark.parametrize("ticket", [None, "pht_0000_forged", "ek_test_1"])
def test_no_ticket_or_a_forged_one_never_reaches_the_provider(
    start_region, provider, ticket
) -> None:
    url = start_region()
    _mint(url)  # a real ticket exists; it is not the one offered
    before = _counted("refused")

    async def attempt() -> None:
        with pytest.raises(websockets.InvalidStatus):
            async with _socket(url, ticket):
                pass

    asyncio.run(attempt())
    assert provider.attempts == []
    assert _counted("refused") == before + 1


def test_an_expired_ticket_is_refused(start_region, provider, monkeypatch) -> None:
    from datetime import timedelta

    from pheasant.persistence import auth_tokens

    url = start_region(secret_ttl_seconds=10)
    ticket = _mint(url)[0]["client_secret"]
    real_now = auth_tokens._now
    monkeypatch.setattr(auth_tokens, "_now", lambda: real_now() + timedelta(seconds=11))

    async def attempt() -> None:
        with pytest.raises(websockets.InvalidStatus):
            async with _socket(url, ticket):
                pass

    asyncio.run(attempt())
    assert provider.attempts == []


def test_a_browser_cannot_reconfigure_the_session(start_region, provider) -> None:
    url = start_region()
    ticket = _mint(url)[0]["client_secret"]

    async def tamper() -> None:
        async with _socket(url, ticket) as socket:
            await _until(socket, "session.updated")
            await socket.send(
                json.dumps({"type": "session.update", "session": {"model": "the-dearest-one"}})
            )
            await socket.send(json.dumps({"type": "response.create"}))
            await socket.send("not json")
            await socket.send(APPEND)
            await socket.send(json.dumps({"type": "input_audio_buffer.commit"}))
            await _until(socket, "conversation.item.input_audio_transcription.completed")

    asyncio.run(tamper())
    session = provider.sessions[0]
    assert len(session.updates) == 1, "only the region's own session.update reached it"
    assert session.updates[0]["session"]["audio"]["input"]["transcription"]["model"] == (
        "gpt-live-transcribe"
    )
    assert session.other_events == []
    assert session.audio_bytes == 4800


def test_the_provider_refusing_the_region_closes_cleanly(start_region, provider) -> None:
    provider.header_keys = set()  # the region's key is no longer accepted
    url = start_region()
    ticket = _mint(url)[0]["client_secret"]
    before = _counted("provider_unreachable")

    async def attempt() -> None:
        with pytest.raises(websockets.InvalidStatus):
            async with _socket(url, ticket):
                pass

    asyncio.run(attempt())
    assert len(provider.attempts) == 1 and not provider.sessions
    assert _counted("provider_unreachable") == before + 1


def test_the_cap_refuses_rather_than_queues(start_region, provider) -> None:
    url = start_region(max_relay_sessions=1)

    async def crowd() -> None:
        async with _socket(url, _mint(url)[0]["client_secret"]) as first:
            await _until(first, "session.updated")
            with pytest.raises(websockets.InvalidStatus):
                async with _socket(url, _mint(url)[0]["client_secret"]):
                    pass
        # The first has gone, so the next is admitted.
        await asyncio.sleep(0.2)
        async with _socket(url, _mint(url)[0]["client_secret"]) as third:
            await _until(third, "session.updated")

    asyncio.run(crowd())
    assert len(provider.sessions) == 2


def test_a_dictation_ends_at_its_time_limit(start_region, provider, monkeypatch) -> None:
    monkeypatch.setattr(voice_relay, "DRAIN_SECONDS", 0.5)
    url = start_region(max_seconds=1)
    ticket = _mint(url)[0]["client_secret"]

    async def wait_out() -> tuple[int | None, str, float]:
        started = time.monotonic()
        async with _socket(url, ticket) as socket:
            try:
                while True:
                    await asyncio.wait_for(socket.recv(), 10)
            except websockets.ConnectionClosed as closed:
                received = closed.rcvd
                return (
                    received.code if received else None,
                    received.reason if received else "",
                    time.monotonic() - started,
                )
        raise AssertionError("the relay did not close")

    code, reason, elapsed = asyncio.run(wait_out())
    assert code == 1000 and "time limit" in reason
    assert elapsed < 5
    assert provider.sessions[0].closed or provider.sessions[0].committed is False


# ---------------------------------------------------------------------------
# managed identity
# ---------------------------------------------------------------------------


def test_a_foundry_relay_with_no_key_signs_with_the_managed_identity(tmp_path, monkeypatch) -> None:
    from pheasant.security import entra

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(entra, "managed_identity_available", lambda: True)
    monkeypatch.setattr(entra, "managed_identity_token", lambda resource, *a, **k: "mi-token")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {"name": "mi", "defaults": "standalone", "state_path": str(tmp_path)},
            "sources": [],
            "assistant": {
                "voice": {"base_url": "https://contoso.cognitiveservices.azure.com/openai/v1"}
            },
        }
    )
    context = SimpleNamespace(config=config, state=_store(tmp_path))
    status = voice.voice_status(context, env={})
    assert status["available"] and status["transport"] == "relay"

    minted = voice.start_voice_session(context, env={})
    target = voice.open_relay(context, minted["client_secret"], env={})
    assert target is not None
    assert (
        target.url
        == "wss://contoso.cognitiveservices.azure.com/openai/v1/realtime?intent=transcription"
    )
    assert target.headers == {"authorization": "Bearer mi-token"}
    # Spent.
    assert voice.open_relay(context, minted["client_secret"], env={}) is None


def test_a_foundry_relay_with_a_key_sends_it_as_api_key(tmp_path) -> None:
    config = PheasantConfig.model_validate(
        {
            "pheasant": {"name": "key", "defaults": "standalone", "state_path": str(tmp_path)},
            "sources": [],
            "assistant": {
                "voice": {
                    "base_url": "https://contoso.services.ai.azure.com",
                    "api_key_env": "FOUNDRY_KEY",
                }
            },
        }
    )
    context = SimpleNamespace(config=config, state=_store(tmp_path))
    env = {"FOUNDRY_KEY": "foundry-key"}
    target = voice.open_relay(
        context, voice.start_voice_session(context, env=env)["client_secret"], env=env
    )
    assert target is not None
    assert target.url.startswith("wss://contoso.services.ai.azure.com/openai/v1/realtime")
    assert target.headers == {"api-key": "foundry-key"}


def test_only_audio_buffer_events_are_admitted() -> None:
    assert voice_relay.admitted(APPEND)
    assert voice_relay.admitted('{"type": "input_audio_buffer.commit"}')
    assert not voice_relay.admitted('{"type": "session.update"}')
    assert not voice_relay.admitted('{"type": "response.create"}')
    assert not voice_relay.admitted("[]")
    assert not voice_relay.admitted("nope")
    assert not voice_relay.admitted(b'{"type": "input_audio_buffer.append"}')
