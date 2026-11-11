"""``assistant.voice`` — dictation in the composer (one section, one module).

Lives outside ``schema.py`` for the reason ``assistant_decisions.py`` does:
that module is at its size ceiling (``tests/test_module_budget.py``), whose
stated remedy is one module per section. ``schema.py`` imports and nests it
like any other section, so ``model_validate``, the wizard and the docs see no
difference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pheasant.config.model import ModelMixin

#: How the browser reaches the transcriber (the class docstring).
TRANSPORTS = ("auto", "direct", "relay")


@dataclass
class AssistantVoiceSettings(ModelMixin):
    """Dictation in the web UI's composer: speech streamed to a live transcriber.

    The browser records the microphone and streams it to a Realtime
    **transcription** session, one of two ways (``transport``):

    ``direct``  the browser opens the provider's WebSocket itself, with a
                short-lived client secret the region mints for it
                (``POST /assistant/voice/session``). The region's key never
                reaches a browser and no audio passes through the region.
                This is OpenAI's design, and what an OpenAI ``base_url`` uses.
    ``relay``   the browser opens the region's own WebSocket
                (``/assistant/voice/relay``) with a one-use ticket, and the api
                opens the provider's with its key in a header, or with its
                managed identity. Audio stays inside the region's network and
                the provider's firewall needs no browser addresses. Required
                for Azure AI Foundry, whose realtime WebSocket accepts a
                credential only as a header -- which a browser cannot set.
    ``auto``    ``relay`` for a Foundry ``base_url``, ``direct`` otherwise.

    The key is ``api_key_env`` when one is named, otherwise the answering
    model's own key when that is OpenAI (a browser session's pasted key
    included), otherwise ``OPENAI_API_KEY``. A relayed Foundry session needs
    no key where the process has a managed identity. No credential means the
    microphone is not offered, which is the standalone default by construction.

    ``max_relay_sessions`` caps the dictations one api process relays at once;
    past it a new one is refused (retryable) rather than queued.

    ``max_seconds`` caps one dictation: the composer stops itself there and a
    new one can start at once. ``language`` is an ISO-639-1 hint, and
    ``transcription`` is merged into the session's transcription object as it
    stands, for model options this schema does not name (``delay``,
    ``keywords`` …) without a code change.
    """

    enabled: bool = True
    model: str = "gpt-live-transcribe"
    max_seconds: int = 60
    base_url: str | None = None
    api_key_env: str | None = None
    language: str | None = None
    #: How long the minted secret may wait to open its session.
    secret_ttl_seconds: int = 60
    request_timeout_seconds: float = 10.0
    transcription: dict[str, Any] = field(default_factory=dict)
    transport: str = "auto"
    max_relay_sessions: int = 16

    def __post_init__(self) -> None:
        if not 1 <= int(self.max_seconds) <= 600:
            raise ValueError("assistant.voice.max_seconds must be between 1 and 600")
        if not 10 <= int(self.secret_ttl_seconds) <= 7200:
            raise ValueError("assistant.voice.secret_ttl_seconds must be between 10 and 7200")
        if self.transport not in TRANSPORTS:
            raise ValueError(f"assistant.voice.transport must be one of {', '.join(TRANSPORTS)}")
        if not 1 <= int(self.max_relay_sessions) <= 1000:
            raise ValueError("assistant.voice.max_relay_sessions must be between 1 and 1000")
