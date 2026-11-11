"""Non-interactive, scoped authentication for managed Git repositories.

A clone or fetch is signed for its own host only, with the first of:

1. the connection the source names (``connector.connection``), set for the
   duration of a materialization by :func:`using_connection`;
2. for github.com, ``GITHUB_TOKEN`` / ``GH_TOKEN`` -- what it always was;
3. the one connection of the provider that serves the host (GitHub, Azure
   DevOps, GitLab), so a pasted private repository URL needs nothing else
   once its account is connected.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import os
import subprocess
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from typing import Any, TypeVar
from urllib.parse import urlparse

# Checked in this order because GitHub Actions and most local setups already
# use GITHUB_TOKEN; GH_TOKEN remains the ``gh`` CLI-compatible fallback.
GITHUB_TOKEN_ENV_CANDIDATES = ("GITHUB_TOKEN", "GH_TOKEN")

T = TypeVar("T")

#: The providers whose connections sign a git remote.
GIT_PROVIDERS = ("github", "azure_devops", "gitlab")

#: The connection a materialization in progress reads with.
_connection: ContextVar[str | None] = ContextVar("pheasant_git_connection", default=None)


@contextlib.contextmanager
def using_connection(name: str | None) -> Iterator[None]:
    token = _connection.set(name or None)
    try:
        yield
    finally:
        _connection.reset(token)


def signed_by_target(function: Callable[..., T]) -> Callable[..., T]:
    """Run ``function(target, ...)`` with the target's ``connector.connection``
    signing every git command it makes."""

    @functools.wraps(function)
    def wrapper(target: Any, *args: Any, **kwargs: Any) -> T:
        value = str((getattr(target, "connector", None) or {}).get("connection") or "")
        with using_connection(value.strip() or None):
            return function(target, *args, **kwargs)

    return wrapper


def github_token() -> str | None:
    for name in GITHUB_TOKEN_ENV_CANDIDATES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def is_github_https_url(url: str) -> bool:
    """True for an HTTP(S) github.com remote, never SSH/scp-like forms."""

    return _https_host(url) == "github.com"


def _https_host(url: str) -> str | None:
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        return None
    return parsed.netloc.split("@")[-1].split(":")[0].lower() or None


def github_authentication_error(result: subprocess.CompletedProcess[str]) -> bool:
    """Whether Git failed because the host rejected an HTTP credential."""

    detail = f"{result.stderr}\n{result.stdout}".lower()
    return any(
        marker in detail
        for marker in (
            "could not read username",
            "authentication failed",
            "http basic: access denied",
            "invalid username or token",
        )
    )


def credential(clone_url: str | None) -> tuple[str, str] | None:
    """``(host, authorization)`` to sign ``clone_url`` with, or ``None``.

    ``authorization`` is ``basic <user:token>`` for a token, or ``bearer
    <token>`` for a Microsoft sign-in, which Azure DevOps accepts as itself.
    Raises :class:`pheasant.connections.ConnectionUnavailable` when the source
    names a connection this process cannot use: a clone that silently went
    anonymous would fail later with a message about the wrong thing.
    """

    host = _https_host(clone_url or "")
    if host is None:
        return None
    from pheasant import connections
    from pheasant.connections import providers, resolve

    name = _connection.get()
    if name:
        row, secret = connections.named(name, providers_allowed=GIT_PROVIDERS)
        return _signed(host, row, resolve.token(row, secret))
    entry = providers.git_provider_for(host)
    if entry is None:
        return None
    if entry.id == "github":
        token = github_token()
        if token:
            return host, _basic("x-access-token", token)
    row = connections.ambient(entry.id)
    if row is None:
        return None
    try:
        return _signed(host, row, resolve.token(row, resolve.opened(row)))
    except connections.ConnectionUnavailable:
        return None


def _basic(user: str, token: str) -> str:
    return "basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


def _signed(host: str, row: object, token: str | None) -> tuple[str, str] | None:
    from pheasant.connections import providers, resolve

    if not token:
        return None
    if resolve.is_bearer(row):  # type: ignore[arg-type]
        return host, f"bearer {token}"
    entry = providers.provider(row.provider)  # type: ignore[attr-defined]
    return host, _basic(entry.git_user or "x-access-token", token)


def has_credential(clone_url: str | None) -> bool:
    """Whether a clone of ``clone_url`` would be signed (for the anonymous retry)."""

    try:
        return credential(clone_url) is not None
    except Exception:  # an unusable named connection is reported by the clone itself
        return False


def git_env(clone_url: str | None = None) -> dict[str, str]:
    """Return a no-prompt Git environment with a host-scoped token header.

    The token is placed in Git's config environment, never the URL or argv,
    so it neither leaks through process listings nor reaches other remotes.
    """

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_ASKPASS", "")
    configs = [
        ("protocol.allow", "never"),
        ("protocol.https.allow", "always"),
        ("protocol.http.allow", "always"),
        ("protocol.ssh.allow", "always"),
        ("protocol.git.allow", "always"),
    ]
    signed = credential(clone_url) if clone_url else None
    if signed:
        host, authorization = signed
        configs.append((f"http.https://{host}/.extraheader", f"AUTHORIZATION: {authorization}"))
    env["GIT_CONFIG_COUNT"] = str(len(configs))
    for index, (key, value) in enumerate(configs):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env
