"""Chat-completion clients for OpenAI, Anthropic and Gemini.

Deliberately vendor-neutral and dependency-free: the region already talks
to OpenAI-spec embedding/caption/transcribe endpoints and a SCIM directory
through stdlib ``urllib``, and adding three vendor SDKs to a container that
mostly indexes files is not worth it. Every request funnels through the one
module-level ``_http_json`` so offline tests can monkeypatch a single seam.

Wire shapes (all POST + JSON):

* openai     ``{base_url}/chat/completions`` — Bearer auth; text at
  ``choices[0].message.content``.
* anthropic  ``{base_url}/v1/messages`` — ``x-api-key`` +
  ``anthropic-version`` headers; text is the first ``type == "text"``
  block of ``content``. ``temperature`` is NOT sent: current models
  (Opus 5 / Sonnet 5 / Opus 4.8+) reject sampling parameters with a 400.
* gemini     ``{base_url}/models/{model}:generateContent`` —
  ``x-goog-api-key`` header; text is joined from
  ``candidates[0].content.parts[].text``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from pheasant.assistant.catalog import (
    AUTO_ORDER as CATALOG_AUTO_ORDER,
)
from pheasant.assistant.catalog import (
    PROVIDERS as CATALOG_PROVIDERS,
)
from pheasant.assistant.catalog import (
    ProviderSpec as CatalogProviderSpec,
)
from pheasant.assistant.catalog import (
    resolve_auto_provider as catalog_resolve_auto_provider,
)

DEFAULT_TIMEOUT = 90.0


class ProviderError(RuntimeError):
    """A chat provider could not produce an answer."""


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    default_model: str
    default_base_url: str
    api_key_env: str
    key_hint: str


PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        id="anthropic",
        label="Anthropic",
        # Sonnet 5 is the default: strong grounded synthesis at a price that
        # suits a per-question retrieval surface. Override with
        # assistant.model (e.g. claude-opus-5) for harder corpora.
        default_model="claude-sonnet-5",
        default_base_url="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        key_hint="sk-ant-…",
    ),
    "openai": ProviderSpec(
        id="openai",
        label="OpenAI",
        # Luna tier: enough headroom to actually read the whole-file context
        # the assistant now assembles (see chat.hydrate_citations) and hold a
        # dozen files in mind while writing one grounded answer — which the
        # previous nano default could not, and which is where the answer
        # quality on a large corpus is won. Model ids are per-account: a key
        # that cannot see this one gets a 404 model_not_found, which the chat
        # surface reports verbatim rather than silently substituting.
        # Override with assistant.model.
        default_model="gpt-6-luna",
        default_base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        key_hint="sk-…",
    ),
    "gemini": ProviderSpec(
        id="gemini",
        label="Google Gemini",
        default_model="gemini-2.5-flash",
        default_base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key_env="GEMINI_API_KEY",
        key_hint="AIza…",
    ),
}

# Preference order for assistant.provider == "auto". Anthropic first because
# the grounded-citation prompt below is tuned against it.
AUTO_ORDER = ("anthropic", "openai", "gemini")


def resolve_auto_provider(env: dict[str, str] | None = None) -> str | None:
    """First provider whose default key env var is populated, else None."""
    return catalog_resolve_auto_provider(env)


# The catalogue is the source of truth for setup and runtime.  Keep the
# compatibility names exported by this module while allowing older imports to
# continue working.
ProviderSpec = CatalogProviderSpec
PROVIDERS = CATALOG_PROVIDERS
AUTO_ORDER = CATALOG_AUTO_ORDER


def _http_json(
    url: str,
    payload: dict,
    headers: dict[str, str],
    timeout: float,
) -> dict:
    """POST JSON, return parsed JSON. The single monkeypatch seam."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # pragma: no cover - network path
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise ProviderError(f"{exc.code} from provider: {detail}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - network path
        raise ProviderError(f"could not reach provider: {exc.reason}") from exc


def complete(
    provider: str,
    *,
    api_key: str,
    system: str,
    prompt: str,
    model: str | None = None,
    base_url: str | None = None,
    max_output_tokens: int = 4096,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Single-turn completion. Returns the assistant's text."""
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise ProviderError(
            f"unknown provider {provider!r}; expected one of {', '.join(sorted(PROVIDERS))}"
        )
    if not api_key:
        raise ProviderError(f"no API key available for {spec.label}")
    model = model or spec.default_model
    base = (base_url or spec.default_base_url).rstrip("/")

    if provider == "anthropic":
        return _anthropic(base, api_key, model, system, prompt, max_output_tokens, timeout)
    if provider == "openai":
        return _openai(base, api_key, model, system, prompt, max_output_tokens, timeout)
    return _gemini(base, api_key, model, system, prompt, max_output_tokens, timeout)


def _anthropic(
    base: str, key: str, model: str, system: str, prompt: str, max_tokens: int, timeout: float
) -> str:
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    data = _http_json(f"{base}/v1/messages", payload, headers, timeout)
    # Safety classifiers can decline with a 200 + stop_reason "refusal" and an
    # empty content array, so check that before indexing into content.
    if data.get("stop_reason") == "refusal":
        raise ProviderError("the model declined to answer this question")
    parts = [
        block.get("text", "")
        for block in data.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    text = "".join(parts).strip()
    if not text:
        raise ProviderError("empty response from Anthropic")
    return text


def _openai(
    base: str, key: str, model: str, system: str, prompt: str, max_tokens: int, timeout: float
) -> str:
    # GPT-6 Luna rejects the legacy cap. Other OpenAI-compatible endpoints
    # keep their existing spelling and the error-driven retry below.
    token_field = "max_completion_tokens" if model == "gpt-6-luna" else "max_tokens"
    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        token_field: max_tokens,
    }
    headers = {"authorization": f"Bearer {key}"}
    url = f"{base}/chat/completions"
    try:
        data = _http_json(url, payload, headers, timeout)
    except ProviderError as exc:
        # Reasoning-era models renamed the output cap and reject the old key.
        if "max_tokens" not in payload or "max_tokens" not in str(exc):
            raise
        payload.pop("max_tokens")
        payload["max_completion_tokens"] = max_tokens
        data = _http_json(url, payload, headers, timeout)
    choices = data.get("choices") or []
    text = (choices[0].get("message", {}).get("content") or "").strip() if choices else ""
    if not text:
        raise ProviderError("empty response from OpenAI")
    return text


def _gemini(
    base: str, key: str, model: str, system: str, prompt: str, max_tokens: int, timeout: float
) -> str:
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    headers = {"x-goog-api-key": key}
    data = _http_json(f"{base}/models/{model}:generateContent", payload, headers, timeout)
    candidates = data.get("candidates") or []
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise ProviderError("empty response from Gemini")
    return text
