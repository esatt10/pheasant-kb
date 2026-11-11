"""Dictation in the composer: the region mints a live transcription session.

The browser streams its microphone to an OpenAI Realtime transcription session
(``gpt-live-transcribe`` by default); the region's part is the credential. What
this holds down, over real HTTP against a stand-in for the provider
(``tests/fake_realtime.py``):

* ``GET /assistant/voice`` offers dictation exactly when a session could be
  minted now, by the same key rule ``POST /assistant/voice/session`` uses, and
  says why not otherwise;
* a minted session is a transcription session of the configured model with
  24 kHz PCM in, signed with the region's key, and what comes back carries the
  short-lived secret and never that key — with ``no-store``, so no cache keeps
  a secret past its dictation;
* the two refusals are split by what a caller should do:
  ``VOICE_NOT_CONFIGURED`` (a setting) and ``VOICE_UNAVAILABLE`` (retryable);
* the key is ``api_key_env`` when named, else a pasted OpenAI session key,
  else ``OPENAI_API_KEY``.

The browser half — the microphone, the caret, the one-minute limit — is
``tests/test_dictation_browser.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pheasant.config.schema import AssistantVoiceSettings, PheasantConfig
from pheasant.services import voice
from pheasant.services.errors import VoiceNotConfigured, VoiceUnavailable
from tests.fake_realtime import FakeRealtime, serve, stop


@pytest.fixture
def provider():
    fake = FakeRealtime()
    server, thread, url = serve(fake.app())
    fake.url = url
    try:
        yield fake
    finally:
        stop(server, thread)


def _config(tmp_path, **voice_settings: Any) -> PheasantConfig:
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "voice",
                "defaults": "standalone",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "sources": [],
            "assistant": {"voice": voice_settings},
        }
    )


def _client(config: PheasantConfig, tmp_path):
    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    return TestClient(create_app(config, config_path=str(tmp_path / "pheasant.yaml")))


def test_defaults_name_the_live_model_and_a_one_minute_cap() -> None:
    settings = AssistantVoiceSettings()
    assert settings.model == "gpt-live-transcribe"
    assert settings.max_seconds == 60
    assert PheasantConfig().assistant.voice.max_seconds == 60
    with pytest.raises(ValueError, match="max_seconds"):
        AssistantVoiceSettings(max_seconds=0)


def test_no_key_means_no_microphone(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = _client(_config(tmp_path), tmp_path)

    status = client.get("/assistant/voice").json()
    assert status["available"] is False
    assert "OPENAI_API_KEY" in status["reason"]
    assert status["max_seconds"] == 60

    refused = client.post("/assistant/voice/session", json={})
    assert refused.status_code == 503
    assert refused.json()["code"] == "VOICE_NOT_CONFIGURED"
    assert refused.json()["retryable"] is False


def test_a_minted_session_is_live_transcription_and_never_the_key(
    tmp_path, monkeypatch, provider
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-region-secret")
    client = _client(_config(tmp_path, base_url=f"{provider.url}/v1", language="en"), tmp_path)

    status = client.get("/assistant/voice").json()
    assert status == {
        "available": True,
        "reason": None,
        "model": "gpt-live-transcribe",
        "max_seconds": 60,
        "sample_rate": 24000,
        # An OpenAI-spec base_url on `auto`: the browser opens the provider itself.
        "transport": "direct",
    }

    response = client.post("/assistant/voice/session", json={})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "sk-region-secret" not in response.text
    minted = response.json()
    assert minted["client_secret"] == "ek_test_1"
    assert minted["model"] == "gpt-live-transcribe"
    assert minted["max_seconds"] == 60
    assert minted["sample_rate"] == 24000
    host = provider.url.removeprefix("http://")
    assert minted["websocket_url"] == f"ws://{host}/v1/realtime?intent=transcription"
    assert minted["transport"] == "direct"

    (request,) = provider.minted
    assert request["headers"]["authorization"] == "Bearer sk-region-secret"
    session = request["body"]["session"]
    assert session["type"] == "transcription"
    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert session["audio"]["input"]["transcription"] == {
        "model": "gpt-live-transcribe",
        "language": "en",
    }
    assert request["body"]["expires_after"] == {"anchor": "created_at", "seconds": 60}


def test_model_options_pass_through_and_the_cap_is_configurable(
    tmp_path, monkeypatch, provider
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    config = _config(
        tmp_path,
        base_url=f"{provider.url}/v1",
        max_seconds=30,
        transcription={"delay": "low", "keywords": ["pheasant"]},
    )
    minted = _client(config, tmp_path).post("/assistant/voice/session", json={}).json()

    assert minted["max_seconds"] == 30
    assert provider.minted[0]["body"]["session"]["audio"]["input"]["transcription"] == {
        "model": "gpt-live-transcribe",
        "delay": "low",
        "keywords": ["pheasant"],
    }


def test_a_provider_refusal_is_retryable_and_says_why(tmp_path, monkeypatch, provider) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    provider.refuse = (401, "Incorrect API key provided")
    response = _client(_config(tmp_path, base_url=f"{provider.url}/v1"), tmp_path).post(
        "/assistant/voice/session", json={}
    )

    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "VOICE_UNAVAILABLE"
    assert body["retryable"] is True
    assert "HTTP 401" in body["detail"] and "Incorrect API key" in body["detail"]


def test_an_unreachable_provider_is_unavailable_not_a_crash(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    response = _client(_config(tmp_path, base_url="http://127.0.0.1:9/v1"), tmp_path).post(
        "/assistant/voice/session", json={}
    )
    assert response.status_code == 502
    assert response.json()["code"] == "VOICE_UNAVAILABLE"


def test_switched_off_is_not_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client = _client(_config(tmp_path, enabled=False), tmp_path)
    assert client.get("/assistant/voice").json()["reason"] == "assistant.voice.enabled is off"
    assert client.post("/assistant/voice/session", json={}).json()["code"] == "VOICE_NOT_CONFIGURED"


def test_a_pasted_openai_session_key_dictates_too(tmp_path, monkeypatch, provider) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = _client(_config(tmp_path, base_url=f"{provider.url}/v1"), tmp_path)
    session_id = client.post(
        "/assistant/key", json={"provider": "openai", "api_key": "sk-pasted"}
    ).json()["session_id"]

    assert client.get("/assistant/voice").json()["available"] is False
    assert (
        client.get("/assistant/voice", params={"session_id": session_id}).json()["available"]
        is True
    )
    minted = client.post("/assistant/voice/session", json={"session_id": session_id})
    assert minted.status_code == 200
    assert provider.minted[0]["headers"]["authorization"] == "Bearer sk-pasted"


def test_the_key_rule_named_env_then_session_then_openai(monkeypatch) -> None:
    env = {"OPENAI_API_KEY": "sk-env", "VOICE_KEY": "sk-named"}
    pasted = SimpleNamespace(provider="openai", api_key="sk-pasted", base_url=None)
    other = SimpleNamespace(provider="anthropic", api_key="sk-ant", base_url=None)

    named = AssistantVoiceSettings(api_key_env="VOICE_KEY")
    assert voice._resolve_key(named, pasted, env)[0] == "sk-named"
    assert voice._resolve_key(named, pasted, {})[0] is None
    plain = AssistantVoiceSettings()
    assert voice._resolve_key(plain, pasted, env)[0] == "sk-pasted"
    assert voice._resolve_key(plain, other, env)[0] == "sk-env"
    assert voice._resolve_key(plain, None, {})[0] is None


def test_websocket_address_follows_the_base_url() -> None:
    assert voice.websocket_url(None) == "wss://api.openai.com/v1/realtime?intent=transcription"
    assert (
        voice.websocket_url("https://acme.services.ai.azure.com/api/projects/p")
        == "wss://acme.services.ai.azure.com/openai/v1/realtime?intent=transcription"
    )
    assert (
        voice.websocket_url("http://localhost:8080/v1/")
        == "ws://localhost:8080/v1/realtime?intent=transcription"
    )


def test_a_beta_shaped_secret_is_read_too(tmp_path, monkeypatch) -> None:
    """The beta endpoints nested the secret under ``client_secret``."""

    import io
    import json

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    context = SimpleNamespace(config=_config(tmp_path))
    beta = {"client_secret": {"value": "ek_beta", "expires_at": 123}}
    minted = voice.start_voice_session(
        context,
        env={"OPENAI_API_KEY": "sk-test"},
        opener=lambda *a, **k: Reply(json.dumps(beta).encode()),
    )
    assert (minted["client_secret"], minted["expires_at"]) == ("ek_beta", 123)

    with pytest.raises(VoiceUnavailable, match="no client secret"):
        voice.start_voice_session(
            context, env={"OPENAI_API_KEY": "sk-test"}, opener=lambda *a, **k: Reply(b"{}")
        )
    with pytest.raises(VoiceNotConfigured):
        voice.start_voice_session(context, env={})
