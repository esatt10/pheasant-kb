"""Audio transcription for multi-modal ingestion (Synapse step 25.4 session B).

An audio file (``.wav``/``.mp3``/``.m4a``/``.flac``/``.ogg``) carries no
indexable text on its own. Synapse architecture §8 ("multi-modal strategy")
projects audio into the *one* fleet-pinned **text** embedding space by
**transcribing** it: a natural-language transcript is derived from the audio
and becomes the artifact's indexable text, which then flows through the normal
chunk -> embed -> graph path like any other document. Modality-native audio
vectors would stay region-local and are explicitly out of scope here. This is
the audio twin of :mod:`pheasant.ingestion.captioner` (image, session A) —
same shape, a transcriber instead of a captioner.

Transcription is a sanctioned network call in the indexing path (alongside the
21.4 embedder and the 25.4A captioner), and like them it must keep a
deterministic **offline stub** so ``pytest`` stays network-free:

- :class:`StubTranscriber` is the default. It returns a deterministic
  transcript derived from the file name + a blake2b digest of the audio bytes,
  so the same audio always produces the same transcript (idempotency) and
  different audio produces different transcripts (routing can discriminate). If
  a sidecar ``<audio>.transcript.txt`` is present next to the audio, its
  contents are used verbatim instead — the offline way to author a real
  transcript for a fixture or demo. No audio decoder, no ASR model, no network.
- :class:`OpenAISpecTranscriber` speaks the standard OpenAI-spec
  ``audio/transcriptions`` shape (multipart upload), the same wire format the
  rest of the fleet uses. It is optional/gated: a region configured for
  ``stub`` never imports it and needs no extra dependency or audio library.

Idempotency note
----------------
Transcription happens inside :func:`pheasant.ingestion.pipeline.parse_*`, but
the sync engine's pre-read skip (``_can_skip_before_read``) compares the file's
content sha256 *before* reading bytes, so an unchanged audio file in an
incremental sync is skipped and **never re-transcribed** — the same zero-work
guarantee the embedder (21.4) and the image captioner (25.4A) get.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.request import Request, urlopen

from pheasant.ingestion._modal import sidecar_text, stub_fingerprint

if TYPE_CHECKING:
    from pheasant.config.schema import TranscriberSettings

logger = logging.getLogger(__name__)

TRANSCRIPT_SIDECAR_SUFFIX = ".transcript.txt"

# Multipart content-type hints keyed by suffix for the OpenAI-spec request.
_AUDIO_MIME = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
}


@runtime_checkable
class Transcriber(Protocol):
    """Audio-bytes -> transcript-text provider. ``calls`` counts transcripts."""

    model: str
    calls: int

    def transcribe(self, content: bytes, relative_path: str) -> str: ...


class StubTranscriber:
    """Deterministic, offline transcriber for tests and demos.

    The transcript is a fixed template over the audio's base name plus a short
    blake2b digest of its bytes — stable across processes/platforms and unique
    per distinct file. A sidecar ``<audio>.transcript.txt`` (passed via
    ``sidecar=``) overrides the template so fixtures can carry an authored,
    searchable transcript without any decoder or network.
    """

    def __init__(self, model: str = "stub-transcribe"):
        self.model = model
        self.calls = 0

    def transcribe(self, content: bytes, relative_path: str, sidecar: bytes | None = None) -> str:
        self.calls += 1
        authored = sidecar_text(sidecar)
        if authored is not None:
            return authored
        name = Path(relative_path).stem.replace("_", " ").replace("-", " ").strip()
        digest = stub_fingerprint(content)
        return f"Audio {name} (spoken content, fingerprint {digest})."


class OpenAISpecTranscriber:
    """OpenAI-spec audio transcriber (stdlib urllib, no SDK, no audio library).

    Wire format: ``POST {base_url}/audio/transcriptions`` as a multipart upload
    with ``model`` + the raw ``file`` bytes; the transcript is read from the
    response ``text`` field. The API key is read from the environment variable
    named by ``api_key_env`` at call time and never persisted. A sidecar
    ``<audio>.transcript.txt`` still short-circuits the network call (authored
    transcripts win).
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key_env: str = "OPENAI_API_KEY",
        timeout: int = 60,
    ):
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.model = model
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.calls = 0

    def transcribe(self, content: bytes, relative_path: str, sidecar: bytes | None = None) -> str:
        authored = sidecar_text(sidecar)
        if authored is not None:
            return authored
        body, content_type = self._multipart(content, relative_path)
        headers = {"Content-Type": content_type, "User-Agent": "pheasant/0.1"}
        api_key = os.environ.get(self.api_key_env or "", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = Request(
            f"{self.base_url}/audio/transcriptions",
            data=body,
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.calls += 1
        text = payload.get("text")
        if text is None:
            raise ValueError("Audio transcriber returned no text")
        return str(text).strip()

    def _multipart(self, content: bytes, relative_path: str) -> tuple[bytes, str]:
        boundary = f"----pheasant{uuid.uuid4().hex}"
        name = Path(relative_path).name
        mime = _AUDIO_MIME.get(Path(relative_path).suffix.lower(), "application/octet-stream")
        crlf = b"\r\n"
        parts: list[bytes] = []
        parts.append(f"--{boundary}".encode())
        parts.append(b'Content-Disposition: form-data; name="model"')
        parts.append(b"")
        parts.append(self.model.encode("utf-8"))
        parts.append(f"--{boundary}".encode())
        parts.append(f'Content-Disposition: form-data; name="file"; filename="{name}"'.encode())
        parts.append(f"Content-Type: {mime}".encode())
        parts.append(b"")
        body = crlf.join(parts) + crlf + content + crlf + f"--{boundary}--{crlf.decode()}".encode()
        return body, f"multipart/form-data; boundary={boundary}"


def build_transcriber(settings: TranscriberSettings) -> Transcriber:
    provider = (settings.provider or "stub").lower()
    if provider == "stub":
        return StubTranscriber(model=settings.model)
    if provider in {"openai-spec", "openai"}:
        return OpenAISpecTranscriber(
            base_url=settings.base_url,
            model=settings.model,
            api_key_env=settings.api_key_env,
        )
    raise ValueError(
        f"Unsupported ingestion.transcriber.provider {settings.provider!r}; "
        "expected 'openai-spec' or 'stub'"
    )


def source_includes_audio(source: Any) -> bool:
    """True when a source's include globs admit any audio extension.

    Audio ingestion is opt-in: the default source ``include`` list is
    text/code/markdown only, so a region only grows audio capability when an
    operator explicitly adds e.g. ``**/*.wav`` to a source's includes.
    """

    from pheasant.ingestion.content_types import AUDIO_EXTENSIONS, source_includes_zip

    includes = list(getattr(source, "include", None) or [])
    if source_includes_zip(source):
        return True
    broad = {"*", "**", "**/*", "*.*", "**/*.*"}
    return any(
        pattern.replace("\\", "/").lower().rstrip("/") in broad
        or pattern.lower().endswith(tuple(AUDIO_EXTENSIONS))
        for pattern in includes
    )


def config_has_audio_source(config: Any) -> bool:
    return any(source_includes_audio(source) for source in getattr(config, "sources", []))


def transcriber_from_config(config: Any, *, source: Any = None) -> Transcriber | None:
    """Build the audio transcriber, or ``None`` when no audio source is configured.

    Returning ``None`` keeps a text-only region byte-identical to pre-25.4
    behavior (no transcriber, no possibility of a network call).

    Pass ``source`` to ask about one source rather than the whole config —
    what a source registered *after* the engine was built needs, since the
    config the engine was constructed from never mentioned it.
    """

    wanted = (
        source_includes_audio(source) if source is not None else config_has_audio_source(config)
    )
    if not wanted:
        return None
    try:
        return build_transcriber(config.ingestion.transcriber)
    except ValueError as exc:
        logger.warning("Audio transcription disabled: %s", exc)
        return None
