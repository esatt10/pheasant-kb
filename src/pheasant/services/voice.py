"""Dictation for the web UI's composer: a live transcription session, minted here.

The composer's microphone streams speech to an OpenAI Realtime
**transcription** session (``gpt-live-transcribe`` by default) and writes the
transcript deltas into the question box as they arrive. The audio goes from
the browser to the transcriber directly; what the region contributes is the
credential, and it never hands over its own. It asks
``POST {base}/realtime/client_secrets`` for a short-lived secret bound to one
transcription session and returns that, with the WebSocket address to open it
at and the per-dictation cap (``assistant.voice.max_seconds``) the composer
enforces.

HTTP only, like ``source_browser.raw_document``: it exists for a person's
microphone, and an agent has no microphone to dictate with — it sends text.
Query-time only, like the rest of the assistant: nothing here is on the
indexing path, and with no key the microphone is simply not offered.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pheasant import foundry
from pheasant.services import ServiceContext
from pheasant.services.errors import VoiceNotConfigured, VoiceUnavailable

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
#: The Realtime API's PCM input: 16-bit little-endian mono at this rate.
SAMPLE_RATE = 24000
#: Providers whose session key is an OpenAI-wire key (``assistant.catalog``).
_OPENAI_WIRE = frozenset({"openai", "azure"})


def _settings(context: ServiceContext) -> Any:
    assistant = getattr(context.config, "assistant", None)
    return getattr(assistant, "voice", None) if assistant is not None else None


def _resolve_key(
    settings: Any, credential: Any, env: dict[str, str]
) -> tuple[str | None, str | None]:
    """``(api_key, base_url)`` for the transcriber, by the rule the config states.

    ``api_key_env`` when one is named; otherwise the answering model's own key
    when a browser session pasted an OpenAI one (the same account); otherwise
    ``OPENAI_API_KEY``. ``assistant.voice.base_url`` wins over a session's.
    """

    base_url = getattr(settings, "base_url", None)
    named = getattr(settings, "api_key_env", None)
    if named:
        return env.get(str(named)) or None, base_url
    if credential is not None and getattr(credential, "provider", None) in _OPENAI_WIRE:
        return getattr(credential, "api_key", None) or None, base_url or getattr(
            credential, "base_url", None
        )
    return env.get(DEFAULT_API_KEY_ENV) or None, base_url


def _unavailable_reason(
    context: ServiceContext, credential: Any, env: dict[str, str]
) -> str | None:
    assistant = getattr(context.config, "assistant", None)
    settings = _settings(context)
    if assistant is None or not getattr(assistant, "enabled", True):
        return "the assistant is disabled"
    if settings is None or not getattr(settings, "enabled", True):
        return "assistant.voice.enabled is off"
    api_key, _ = _resolve_key(settings, credential, env)
    if not api_key:
        named = getattr(settings, "api_key_env", None) or DEFAULT_API_KEY_ENV
        return f"no OpenAI key reaches assistant.voice (set {named})"
    return None


def websocket_url(base_url: str | None) -> str:
    """The Realtime transcription WebSocket for an OpenAI-spec ``base_url``."""

    base = foundry.openai_v1_url(base_url or DEFAULT_BASE_URL).rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}/realtime?intent=transcription"


def voice_status(
    context: ServiceContext, *, credential: Any = None, env: dict[str, str]
) -> dict[str, Any]:
    """Whether the composer may offer dictation, and on what terms.

    ``available`` is "a session could be minted now", resolved exactly the way
    `start_voice_session` resolves it, so the microphone is never offered by a
    region that would refuse it.
    """

    settings = _settings(context)
    reason = _unavailable_reason(context, credential, env)
    return {
        "available": reason is None,
        "reason": reason,
        "model": getattr(settings, "model", None),
        "max_seconds": int(getattr(settings, "max_seconds", 60) or 60),
        "sample_rate": SAMPLE_RATE,
    }


def session_request_body(settings: Any) -> dict[str, Any]:
    """The ``client_secrets`` request: one transcription session, PCM in."""

    transcription: dict[str, Any] = {"model": str(settings.model)}
    if getattr(settings, "language", None):
        transcription["language"] = str(settings.language)
    transcription.update(dict(getattr(settings, "transcription", None) or {}))
    return {
        "expires_after": {"anchor": "created_at", "seconds": int(settings.secret_ttl_seconds)},
        "session": {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                    "transcription": transcription,
                }
            },
        },
    }


def start_voice_session(
    context: ServiceContext,
    *,
    credential: Any = None,
    env: dict[str, str],
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Mint a short-lived secret for one dictation.

    Returns ``{client_secret, expires_at, model, websocket_url, sample_rate,
    max_seconds}``; the browser opens ``websocket_url`` with the secret and
    streams PCM to it. Refuses with ``VOICE_NOT_CONFIGURED`` when no key
    reaches the transcriber (a setting to change) and ``VOICE_UNAVAILABLE``
    (retryable) when the provider did not mint one.
    """

    reason = _unavailable_reason(context, credential, env)
    if reason is not None:
        raise VoiceNotConfigured(reason)
    settings = _settings(context)
    api_key, base_url = _resolve_key(settings, credential, env)
    assert api_key  # _unavailable_reason established it
    base = foundry.openai_v1_url(base_url or DEFAULT_BASE_URL).rstrip("/")
    request = Request(
        f"{base}/realtime/client_secrets",
        data=json.dumps(session_request_body(settings)).encode("utf-8"),
        headers={"content-type": "application/json", **foundry.auth_headers(base, api_key)},
        method="POST",
    )
    try:
        with (opener or urlopen)(
            request, timeout=float(settings.request_timeout_seconds)
        ) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8") or "{}")
            detail = str((body.get("error") or {}).get("message") or "")
        except Exception:  # noqa: BLE001 - the status is the useful half
            pass
        logger.warning("voice session refused by the provider: HTTP %s %s", exc.code, detail)
        raise VoiceUnavailable(f"HTTP {exc.code}" + (f": {detail}" if detail else "")) from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise VoiceUnavailable(exc) from exc
    # GA returns the secret at the top level; the beta nested it.
    secret = payload.get("value")
    expires_at = payload.get("expires_at")
    nested = payload.get("client_secret")
    if not secret and isinstance(nested, dict):
        secret = nested.get("value")
        expires_at = expires_at or nested.get("expires_at")
    if not secret:
        raise VoiceUnavailable("the provider returned no client secret")
    return {
        "client_secret": str(secret),
        "expires_at": expires_at,
        "model": str(settings.model),
        "websocket_url": websocket_url(base_url),
        "sample_rate": SAMPLE_RATE,
        "max_seconds": int(settings.max_seconds),
    }
