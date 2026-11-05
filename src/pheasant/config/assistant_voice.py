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


@dataclass
class AssistantVoiceSettings(ModelMixin):
    """Dictation in the web UI's composer: speech streamed to a live transcriber.

    The browser records the microphone and streams it straight to an OpenAI
    Realtime **transcription** session; the server only mints the short-lived
    client secret that session is opened with (``POST /assistant/voice/session``),
    so the region's API key never reaches a browser and no audio passes through
    the region. Query-time only, like the rest of the assistant.

    The key is ``api_key_env`` when one is named, otherwise the answering
    model's own key when that is OpenAI (a browser session's pasted key
    included), otherwise ``OPENAI_API_KEY``. No key means the microphone is not
    offered, which is the standalone default by construction.

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

    def __post_init__(self) -> None:
        if not 1 <= int(self.max_seconds) <= 600:
            raise ValueError("assistant.voice.max_seconds must be between 1 and 600")
        if not 10 <= int(self.secret_ttl_seconds) <= 7200:
            raise ValueError("assistant.voice.secret_ttl_seconds must be between 10 and 7200")
