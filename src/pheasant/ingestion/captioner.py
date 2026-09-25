"""Image captioning for multi-modal ingestion (Synapse step 25.4 session A).

An image file (``.png``/``.jpg``/``.jpeg``/``.webp``/``.gif``) carries no
indexable text on its own. Synapse architecture §8 ("multi-modal strategy")
projects images into the *one* fleet-pinned **text** embedding space by
**captioning** them: a short natural-language description is derived from the
image and becomes the artifact's indexable text, which then flows through the
normal chunk -> embed -> graph path like any other document. Modality-native
vectors (CLIP etc.) would stay region-local and are explicitly out of scope
here.

Captioning is the **only** sanctioned network call in the indexing path
besides the 21.4 embedder, and like the embedder it must keep a deterministic
**offline stub** so ``pytest`` stays network-free:

- :class:`StubCaptioner` is the default. It returns a deterministic caption
  derived from the file name + a blake2b digest of the image bytes, so the
  same image always produces the same caption (idempotency) and different
  images produce different captions (routing can discriminate). If a sidecar
  ``<image>.caption.txt`` is present next to the image, its contents are used
  verbatim instead — the offline way to author a real caption for a fixture
  or demo. No image decoder, no model, no network.
- :class:`OpenAISpecVisionCaptioner` speaks the standard OpenAI-spec
  ``chat/completions`` shape with an ``image_url`` content part
  (data-URI base64), the same wire format the rest of the fleet uses. It is
  optional/gated: a region configured for ``stub`` never imports it and needs
  no extra dependency.

Idempotency note
----------------
Captioning happens inside :func:`pheasant.ingestion.pipeline.parse_*`, but the
sync engine's pre-read skip (``_can_skip_before_read``) compares the file's
content sha256 *before* reading bytes, so an unchanged image in an incremental
sync is skipped and **never re-captioned** — the same zero-work guarantee the
embedder gets (21.4).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.request import Request, urlopen

from pheasant.ingestion._modal import sidecar_text as _sidecar_text
from pheasant.ingestion._modal import stub_fingerprint

if TYPE_CHECKING:
    from pheasant.config.schema import CaptionerSettings

logger = logging.getLogger(__name__)

# Image extensions captioned into text. Mirrors content_types.IMAGE_EXTENSIONS;
# kept here too so the captioner is usable standalone.
CAPTION_SIDECAR_SUFFIX = ".caption.txt"

# Data-URI mime hints keyed by suffix for the OpenAI-spec vision request.
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


@runtime_checkable
class Captioner(Protocol):
    """Image-bytes -> caption-text provider. ``calls`` counts captions made."""

    model: str
    calls: int

    def caption(self, content: bytes, relative_path: str) -> str: ...


class StubCaptioner:
    """Deterministic, offline captioner for tests and demos.

    The caption is a fixed template over the image's base name plus a short
    blake2b digest of its bytes — stable across processes/platforms and unique
    per distinct image. A sidecar ``<image>.caption.txt`` (passed via
    ``sidecar=``) overrides the template so fixtures can carry an authored,
    searchable caption without any decoder or network.
    """

    def __init__(self, model: str = "stub-caption"):
        self.model = model
        self.calls = 0

    def caption(self, content: bytes, relative_path: str, sidecar: bytes | None = None) -> str:
        self.calls += 1
        authored = _sidecar_text(sidecar)
        if authored is not None:
            return authored
        name = Path(relative_path).stem.replace("_", " ").replace("-", " ").strip()
        digest = stub_fingerprint(content)
        return f"Image {name} (visual content, fingerprint {digest})."


class OpenAISpecVisionCaptioner:
    """OpenAI-spec vision chat captioner (stdlib urllib, no SDK).

    Wire format: ``POST {base_url}/chat/completions`` with a user message whose
    content is ``[{"type": "text", "text": <prompt>}, {"type": "image_url",
    "image_url": {"url": "data:<mime>;base64,<b64>"}}]``; the caption is read
    from ``choices[0].message.content``. The API key is read from the
    environment variable named by ``api_key_env`` at call time and never
    persisted. A sidecar ``<image>.caption.txt`` still short-circuits the
    network call (authored captions win).
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key_env: str = "OPENAI_API_KEY",
        prompt: str = "Describe this image in one concise sentence for search indexing.",
        timeout: int = 30,
    ):
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.model = model
        self.api_key_env = api_key_env
        self.prompt = prompt
        self.timeout = timeout
        self.calls = 0

    def caption(self, content: bytes, relative_path: str, sidecar: bytes | None = None) -> str:
        authored = _sidecar_text(sidecar)
        if authored is not None:
            return authored
        mime = _IMAGE_MIME.get(Path(relative_path).suffix.lower(), "image/png")
        data_uri = f"data:{mime};base64," + base64.b64encode(content).decode("ascii")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.prompt},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
        }
        headers = {"Content-Type": "application/json", "User-Agent": "pheasant/0.1"}
        api_key = os.environ.get(self.api_key_env or "", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.calls += 1
        choices = payload.get("choices") or []
        if not choices:
            raise ValueError("Vision captioner returned no choices")
        text = (choices[0].get("message") or {}).get("content") or ""
        return str(text).strip()


def build_captioner(settings: CaptionerSettings) -> Captioner:
    provider = (settings.provider or "stub").lower()
    if provider == "stub":
        return StubCaptioner(model=settings.model)
    if provider in {"openai-spec", "openai"}:
        return OpenAISpecVisionCaptioner(
            base_url=settings.base_url,
            model=settings.model,
            api_key_env=settings.api_key_env,
            prompt=settings.prompt,
        )
    raise ValueError(
        f"Unsupported ingestion.captioner.provider {settings.provider!r}; "
        "expected 'openai-spec' or 'stub'"
    )


def source_includes_images(source: Any) -> bool:
    """True when a source's include globs admit any image extension.

    Image ingestion is opt-in: the default source ``include`` list is
    text/code/markdown only, so a region only grows image capability when an
    operator explicitly adds e.g. ``**/*.png`` to a source's includes.
    """

    from pheasant.ingestion.content_types import IMAGE_EXTENSIONS, source_includes_zip

    includes = list(getattr(source, "include", None) or [])
    if source_includes_zip(source):
        return True
    broad = {"*", "**", "**/*", "*.*", "**/*.*"}
    return any(
        pattern.replace("\\", "/").lower().rstrip("/") in broad
        or pattern.lower().endswith(tuple(IMAGE_EXTENSIONS))
        for pattern in includes
    )


def config_has_image_source(config: Any) -> bool:
    return any(source_includes_images(source) for source in getattr(config, "sources", []))


def captioner_from_config(config: Any, *, source: Any = None) -> Captioner | None:
    """Build the image captioner, or ``None`` when no image source is configured.

    Returning ``None`` keeps a text-only region byte-identical to pre-25.4
    behavior (no captioner, no possibility of a network call).

    Pass ``source`` to ask about one source rather than the whole config —
    what a source registered *after* the engine was built needs, since the
    config the engine was constructed from never mentioned it.
    """

    wanted = (
        source_includes_images(source) if source is not None else config_has_image_source(config)
    )
    if not wanted:
        return None
    try:
        return build_captioner(config.ingestion.captioner)
    except ValueError as exc:
        logger.warning("Image captioning disabled: %s", exc)
        return None
