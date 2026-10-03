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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
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


class OutputBudgetExhausted(ProviderError):
    """The model stopped at its output cap having written no visible text.

    Distinct from an empty reply because the remedy is different. A reasoning
    model (GPT-6, Gemini 2.5, any model thinking before it answers) spends
    hidden tokens out of the same ``max_completion_tokens`` /
    ``maxOutputTokens`` the answer comes from, so a cap sized for the *answer*
    can be spent entirely on thinking — a 200 with an empty message and
    ``finish_reason: length``. A caller that knows this can ask again with
    more room; one that reads it as "the model had nothing to say" cannot.
    """


class OutputTruncated(ProviderError):
    """The provider returned visible text but stopped at its output limit."""


@dataclass
class TokenUsage:
    """Actual provider-reported usage for one workflow node, never estimated."""

    calls: int = 0
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    input_reports: int = 0
    output_reports: int = 0
    cached_input_tokens: int = 0
    cached_input_reports: int = 0
    reasoning_tokens: int = 0
    reasoning_reports: int = 0

    @property
    def reported_input(self) -> int | None:
        if not self.calls:
            return 0
        return self.input_tokens if self.input_reports == self.calls else None

    @property
    def reported_output(self) -> int | None:
        if not self.calls:
            return 0
        return self.output_tokens if self.output_reports == self.calls else None

    @property
    def reported_cached_input(self) -> int | None:
        if not self.calls:
            return 0
        return self.cached_input_tokens if self.cached_input_reports == self.calls else None

    @property
    def reported_reasoning(self) -> int | None:
        if not self.calls:
            return 0
        return self.reasoning_tokens if self.reasoning_reports == self.calls else None


_active_usage: ContextVar[TokenUsage | None] = ContextVar("pheasant_model_usage", default=None)


@contextmanager
def collect_token_usage() -> Iterator[TokenUsage]:
    """Scope usage to the current request/node, including concurrent requests."""
    usage = TokenUsage()
    token = _active_usage.set(usage)
    try:
        yield usage
    finally:
        _active_usage.reset(token)


def note_model_call() -> None:
    usage = _active_usage.get()
    if usage is not None:
        usage.calls += 1


def note_model_retry() -> None:
    """Count a compatibility or output-budget retry separately from the call."""
    usage = _active_usage.get()
    if usage is not None:
        usage.retries += 1


def _record_usage(
    input_tokens: object,
    output_tokens: object,
    *,
    cached_input_tokens: object = None,
    reasoning_tokens: object = None,
) -> None:
    usage = _active_usage.get()
    if usage is None:
        return
    if isinstance(input_tokens, int) and not isinstance(input_tokens, bool):
        usage.input_tokens += input_tokens
        usage.input_reports += 1
    if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
        usage.output_tokens += output_tokens
        usage.output_reports += 1
    if isinstance(cached_input_tokens, int) and not isinstance(cached_input_tokens, bool):
        usage.cached_input_tokens += cached_input_tokens
        usage.cached_input_reports += 1
    if isinstance(reasoning_tokens, int) and not isinstance(reasoning_tokens, bool):
        usage.reasoning_tokens += reasoning_tokens
        usage.reasoning_reports += 1


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
    except TimeoutError as exc:
        # A read that times out after the connection opened is not a URLError,
        # and a model that thinks first is the kind that takes long enough.
        raise ProviderError(f"provider did not answer within {timeout:g}s") from exc
    except OSError as exc:  # pragma: no cover - network path
        raise ProviderError(f"connection to provider failed: {exc}") from exc
    except ValueError as exc:
        raise ProviderError("provider returned a response that is not JSON") from exc


def _http_chat_stream(
    url: str,
    payload: dict,
    headers: dict[str, str],
    timeout: float,
    on_delta: Callable[[str], None],
) -> dict:
    """Read Chat Completions SSE chunks, retaining the normal response shape."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "accept": "text/event-stream", **headers},
        method="POST",
    )
    pieces: list[str] = []
    usage: dict = {}
    finish_reason: str | None = None
    refusal: str | None = None
    done = False
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data_text = line[5:].strip()
                if data_text == "[DONE]":
                    done = True
                    break
                if not data_text:
                    continue
                chunk = json.loads(data_text)
                if not isinstance(chunk, dict):
                    raise ProviderError("provider returned a malformed stream event")
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        pieces.append(content)
                        try:
                            on_delta(content)
                        except Exception:
                            # Preview callbacks cannot decide answer success.
                            pass
                    if delta.get("refusal"):
                        refusal = str(delta["refusal"])
                    if choice.get("finish_reason") is not None:
                        finish_reason = str(choice["finish_reason"])
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise ProviderError(f"{exc.code} from provider: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"could not reach provider: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ProviderError(f"provider did not answer within {timeout:g}s") from exc
    except OSError as exc:
        raise ProviderError(f"connection to provider failed: {exc}") from exc
    except ValueError as exc:
        raise ProviderError("provider returned a malformed stream event") from exc
    if not done or finish_reason is None:
        raise ProviderError("provider stream ended before completion")
    return {
        "usage": usage,
        "choices": [
            {
                "message": {"content": "".join(pieces), "refusal": refusal},
                "finish_reason": finish_reason,
            }
        ],
    }


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
    json_mode: bool = False,
    reasoning_effort: str | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> str:
    """Single-turn completion. Returns the assistant's text.

    ``json_mode`` asks the provider for a reply that is one JSON object, where
    the wire has a way to say so (OpenAI ``response_format``, Gemini
    ``responseMimeType``). It is a request, not a guarantee: Anthropic's
    messages API has no such switch, an OpenAI-compatible endpoint may reject
    the field (it is then dropped and the call retried), and every caller
    still parses what comes back defensively.
    """
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
        if reasoning_effort is not None:
            raise ProviderError("reasoning_effort is only supported by the OpenAI provider")
        return _anthropic(base, api_key, model, system, prompt, max_output_tokens, timeout)
    if provider == "openai":
        return _openai(
            base,
            api_key,
            model,
            system,
            prompt,
            max_output_tokens,
            timeout,
            json_mode=json_mode,
            reasoning_effort=reasoning_effort,
            on_delta=on_delta,
        )
    if reasoning_effort is not None:
        raise ProviderError("reasoning_effort is only supported by the OpenAI provider")
    return _gemini(
        base, api_key, model, system, prompt, max_output_tokens, timeout, json_mode=json_mode
    )


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
    note_model_call()
    data = _http_json(f"{base}/v1/messages", payload, headers, timeout)
    reported = data.get("usage") or {}
    _record_usage(
        reported.get("input_tokens"),
        reported.get("output_tokens"),
        cached_input_tokens=reported.get("cache_read_input_tokens"),
    )
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
    if data.get("stop_reason") == "max_tokens" and text:
        raise OutputTruncated(f"Anthropic model {model} stopped at its {max_tokens}-token limit")
    if not text:
        if data.get("stop_reason") == "max_tokens":
            raise OutputBudgetExhausted(
                f"Anthropic stopped at its {max_tokens}-token output cap before writing any text"
            )
        raise ProviderError("empty response from Anthropic")
    return text


def _openai(
    base: str,
    key: str,
    model: str,
    system: str,
    prompt: str,
    max_tokens: int,
    timeout: float,
    *,
    json_mode: bool = False,
    reasoning_effort: str | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> str:
    # GPT-6 models reject the legacy cap. Other OpenAI-compatible endpoints
    # keep their existing spelling and the error-driven retry below.
    token_field = "max_completion_tokens" if model.startswith("gpt-6-") else "max_tokens"
    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        token_field: max_tokens,
    }
    if reasoning_effort is not None:
        if model != "gpt-6-luna":
            raise ProviderError(f"reasoning_effort is not enabled for OpenAI model {model!r}")
        if reasoning_effort not in {"none", "low"}:
            raise ProviderError(f"unsupported reasoning_effort {reasoning_effort!r}")
        payload["reasoning_effort"] = reasoning_effort
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if on_delta is not None:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    headers = {"authorization": f"Bearer {key}"}
    url = f"{base}/chat/completions"
    # Each rejection names the one field an endpoint does not accept, and each
    # is adjusted at most once, so this is bounded by the number of fields.
    while True:
        try:
            note_model_call()
            data = (
                _http_chat_stream(url, payload, headers, timeout, on_delta)
                if on_delta is not None
                else _http_json(url, payload, headers, timeout)
            )
            break
        except ProviderError as exc:
            reason = str(exc)
            if "max_tokens" in payload and "max_tokens" in reason:
                # Reasoning-era models renamed the output cap and reject the old key.
                note_model_retry()
                payload["max_completion_tokens"] = payload.pop("max_tokens")
            elif "response_format" in payload and "response_format" in reason:
                # JSON mode is a request; an endpoint that does not know it
                # still gets asked, and the caller parses what comes back.
                note_model_retry()
                payload.pop("response_format")
            elif "stream_options" in payload and "stream_options" in reason:
                note_model_retry()
                payload.pop("stream_options")
            else:
                raise
    reported = data.get("usage") or {}
    prompt_details = reported.get("prompt_tokens_details") or {}
    completion_details = reported.get("completion_tokens_details") or {}
    _record_usage(
        reported.get("prompt_tokens"),
        reported.get("completion_tokens"),
        cached_input_tokens=prompt_details.get("cached_tokens"),
        reasoning_tokens=completion_details.get("reasoning_tokens"),
    )
    choices = data.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        # Some OpenAI-compatible servers return content parts, as the
        # Responses API does, rather than one string.
        content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    text = (content or "").strip()
    if choice.get("finish_reason") == "length":
        if not text:
            raise OutputBudgetExhausted(
                f"OpenAI model {model} spent its {max_tokens}-token output budget "
                "(reasoning included) before writing any text"
            )
        raise OutputTruncated(
            f"OpenAI model {model} stopped at its {max_tokens}-token output limit"
        )
    if not text:
        if message.get("refusal"):
            raise ProviderError("the model declined to answer this question")
        raise ProviderError("empty response from OpenAI")
    return text


def _gemini(
    base: str,
    key: str,
    model: str,
    system: str,
    prompt: str,
    max_tokens: int,
    timeout: float,
    *,
    json_mode: bool = False,
) -> str:
    generation: dict = {"maxOutputTokens": max_tokens}
    if json_mode:
        generation["responseMimeType"] = "application/json"
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation,
    }
    headers = {"x-goog-api-key": key}
    url = f"{base}/models/{model}:generateContent"
    try:
        note_model_call()
        data = _http_json(url, payload, headers, timeout)
    except ProviderError as exc:
        if "responseMimeType" not in generation or "responseMimeType" not in str(exc):
            raise
        note_model_retry()
        generation.pop("responseMimeType")
        note_model_call()
        data = _http_json(url, payload, headers, timeout)
    reported = data.get("usageMetadata") or {}
    _record_usage(
        reported.get("promptTokenCount"),
        reported.get("candidatesTokenCount"),
        cached_input_tokens=reported.get("cachedContentTokenCount"),
        reasoning_tokens=reported.get("thoughtsTokenCount"),
    )
    candidates = data.get("candidates") or []
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    # A thinking model returns its thought summary as parts flagged
    # ``thought``; they are not the answer.
    text = "".join(part.get("text", "") for part in parts if not part.get("thought")).strip()
    if candidates and candidates[0].get("finishReason") == "MAX_TOKENS" and text:
        raise OutputTruncated(f"Gemini model {model} stopped at its {max_tokens}-token limit")
    if not text:
        if candidates and candidates[0].get("finishReason") == "MAX_TOKENS":
            raise OutputBudgetExhausted(
                f"Gemini model {model} spent its {max_tokens}-token output budget "
                "(thinking included) before writing any text"
            )
        raise ProviderError("empty response from Gemini")
    return text
