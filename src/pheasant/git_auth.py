"""Non-interactive, scoped authentication for managed GitHub repositories."""

from __future__ import annotations

import base64
import os
import subprocess
from urllib.parse import urlparse

# Checked in this order because GitHub Actions and most local setups already
# use GITHUB_TOKEN; GH_TOKEN remains the ``gh`` CLI-compatible fallback.
GITHUB_TOKEN_ENV_CANDIDATES = ("GITHUB_TOKEN", "GH_TOKEN")


def github_token() -> str | None:
    for name in GITHUB_TOKEN_ENV_CANDIDATES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def is_github_https_url(url: str) -> bool:
    """True for an HTTP(S) github.com remote, never SSH/scp-like forms."""

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.netloc.split("@")[-1].split(":")[0].lower()
    return host == "github.com"


def github_authentication_error(result: subprocess.CompletedProcess[str]) -> bool:
    """Whether Git failed because GitHub rejected an HTTP credential."""

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


def git_env(clone_url: str | None = None) -> dict[str, str]:
    """Return a no-prompt Git environment with a GitHub-only token header.

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
    token = github_token() if clone_url and is_github_https_url(clone_url) else None
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        configs.append(("http.https://github.com/.extraheader", f"AUTHORIZATION: basic {basic}"))
    env["GIT_CONFIG_COUNT"] = str(len(configs))
    for index, (key, value) in enumerate(configs):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env
