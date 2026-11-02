"""A connection that fails before the provider answers is retried; nothing else is."""

from __future__ import annotations

import io
import json
import ssl
import urllib.error

import pytest

from pheasant.assistant import providers
from pheasant.assistant.providers import ProviderError, _http_json


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen(outcomes: list):
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Response(json.dumps(outcome).encode())

    return urlopen, calls


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(providers.time, "sleep", lambda seconds: None)


def test_a_tls_failure_before_the_answer_is_retried(monkeypatch) -> None:
    urlopen, calls = _urlopen([ssl.SSLError("bad record mac"), {"ok": True}])
    monkeypatch.setattr(providers.urllib.request, "urlopen", urlopen)
    assert _http_json("https://x/v1", {}, {}, 5) == {"ok": True}
    assert len(calls) == 2


def test_a_connection_that_keeps_failing_is_raised_after_three_attempts(monkeypatch) -> None:
    urlopen, calls = _urlopen([ConnectionResetError("reset")] * 3)
    monkeypatch.setattr(providers.urllib.request, "urlopen", urlopen)
    with pytest.raises(ProviderError, match="connection to provider failed: reset"):
        _http_json("https://x/v1", {}, {}, 5)
    assert len(calls) == 3


def test_an_http_error_and_a_timeout_are_not_retried(monkeypatch) -> None:
    refused = urllib.error.HTTPError("https://x/v1", 429, "busy", {}, io.BytesIO(b"slow down"))
    for outcome, message in ((refused, "429 from provider"), (TimeoutError(), "did not answer")):
        urlopen, calls = _urlopen([outcome])
        monkeypatch.setattr(providers.urllib.request, "urlopen", urlopen)
        with pytest.raises(ProviderError, match=message):
            _http_json("https://x/v1", {}, {}, 5)
        assert len(calls) == 1
