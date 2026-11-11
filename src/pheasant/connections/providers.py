"""What can be connected: one entry per service, and how each is checked.

The catalog is the single description every surface reads. The web UI draws a
toggle per entry and a field per ``Field``; MCP ``list_connections`` and
``GET /connections`` return the same entries, so an agent learns what to send
from the region rather than from a document.

``env`` is the environment variable a connection stands in for, which is how a
connected OpenAI key reaches the embedder, the captioner and the chat model
without any of them knowing about connections: they read
``connections.secret("OPENAI_API_KEY")``. ``hosts`` are the git hosts a token
signs clones for, and ``source_types`` the sources that read with it.

``check`` makes one cheap, read-only request with the secret and answers
``ok`` plus the account it names. A service that answers 401/403 *rejected*
the secret (``CheckResult.rejected``), which is a refusal to store it; an
unreachable service is not, because a region behind a firewall must still be
able to hold a key it cannot test from here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pheasant.connections import oauth

CHECK_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    secret: bool = False
    required: bool = True
    placeholder: str = ""
    help: str = ""
    #: ``text`` | ``textarea`` (a pasted JSON key) | ``url``
    kind: str = "text"

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "secret": self.secret,
            "required": self.required,
            "placeholder": self.placeholder,
            "help": self.help,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class Method:
    id: str
    label: str
    fields: tuple[Field, ...]
    #: A browser round trip to the service (``POST /connections/oauth/start``).
    browser: bool = False
    #: The service's sign-in, for a browser method.
    oauth: oauth.OAuthSpec | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "browser": self.browser,
            "fields": [item.public() for item in self.fields],
            "oauth": self.oauth.public() if self.oauth else None,
        }


def sign_in(spec: oauth.OAuthSpec) -> Method:
    """A "Sign in with ..." method. The two fields are for a region with no OAuth
    client configured for the service; the form hides them when it has one."""

    return Method(
        "oauth",
        f"Sign in with {spec.service}",
        (
            Field("client_id", "OAuth client ID", required=False),
            Field("client_secret", "OAuth client secret", secret=True, required=False),
        ),
        browser=True,
        oauth=spec,
    )


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    detail: str
    account: str | None = None
    rejected: bool = False

    def public(self, checked_at: str) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "account": self.account,
            "checked_at": checked_at,
        }


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    #: ``source`` (read a source with it) or ``model`` (a model service's key).
    kind: str
    description: str
    methods: tuple[Method, ...]
    env: str | None = None
    hosts: tuple[str, ...] = ()
    git_user: str | None = None
    source_types: tuple[str, ...] = ()
    token_url: str | None = None
    check: Callable[[dict[str, Any]], CheckResult] | None = field(default=None, compare=False)

    def sign_in(self) -> Method | None:
        return next((method for method in self.methods if method.browser), None)

    def method(self, method_id: str | None) -> Method:
        if not method_id:
            return self.methods[0]
        for method in self.methods:
            if method.id == method_id:
                return method
        known = ", ".join(m.id for m in self.methods)
        raise KeyError(f"{self.label} connects with {known}, not {method_id!r}")

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "description": self.description,
            "env": self.env,
            "hosts": list(self.hosts),
            "source_types": list(self.source_types),
            "token_url": self.token_url,
            "checkable": self.check is not None,
            "methods": [method.public() for method in self.methods],
        }


def _request(url: str, headers: dict[str, str], *, data: bytes | None = None) -> Any:
    """One HTTP call for a check; module-level so tests replace it."""

    request = Request(url, headers=headers, data=data, method="POST" if data else "GET")
    with urlopen(request, timeout=CHECK_TIMEOUT_SECONDS) as response:
        body = response.read()
    return json.loads(body.decode("utf-8")) if body else {}


def _checked(
    label: str,
    call: Callable[[], Any],
    account: Callable[[Any], str | None] | None = None,
    summary: Callable[[Any], str | None] | None = None,
) -> CheckResult:
    """One request with the secret: ``account`` names who it is (a login, an
    email), ``summary`` what it reaches (a model count) for a key with no owner."""

    try:
        payload = call()
    except HTTPError as exc:
        if exc.code in (401, 403):
            return CheckResult(False, f"{label} rejected it ({exc.code})", rejected=True)
        return CheckResult(False, f"{label} answered {exc.code}; stored without a check")
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return CheckResult(False, f"could not reach {label} ({reason}); stored without a check")
    said = summary(payload) if summary else None
    detail = f"{label} accepted it" + (f" ({said})" if said else "")
    return CheckResult(True, detail, account=account(payload) if account else None)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "User-Agent": "pheasant"}


def signed_in(secret: dict[str, Any]) -> bool:
    """Whether a sealed secret is a sign-in's token response."""

    return bool(secret.get("access_token") or secret.get("refresh_token"))


def _token(spec: oauth.OAuthSpec, secret: dict[str, Any], field_name: str = "token") -> str:
    if signed_in(secret):
        return oauth.access_token_for(spec, secret)
    return str(secret[field_name])


def _check_github(secret: dict[str, Any]) -> CheckResult:
    base = str(secret.get("api_url") or "https://api.github.com").rstrip("/")
    try:
        token = _token(oauth.GITHUB, secret)
    except oauth.OAuthError as exc:
        return CheckResult(False, str(exc), rejected=exc.rejected)
    headers = {**_bearer(token), "Accept": "application/vnd.github+json"}
    return _checked(
        "GitHub",
        lambda: _request(f"{base}/user", headers),
        lambda body: body.get("login"),
    )


def _check_gitlab(secret: dict[str, Any]) -> CheckResult:
    base = str(secret.get("api_url") or "https://gitlab.com/api/v4").rstrip("/")
    if signed_in(secret):
        try:
            headers = _bearer(oauth.access_token_for(oauth.GITLAB, secret))
        except oauth.OAuthError as exc:
            return CheckResult(False, str(exc), rejected=exc.rejected)
    else:
        headers = {"PRIVATE-TOKEN": str(secret["token"])}
    return _checked(
        "GitLab",
        lambda: _request(f"{base}/user", headers),
        lambda body: body.get("username"),
    )


def _check_azure_devops(secret: dict[str, Any]) -> CheckResult:
    if not signed_in(secret):
        # A PAT scoped to Code (Read) cannot read the profile, so a check would
        # refuse a token that clones fine. The first clone is its check.
        return CheckResult(False, "a personal access token is checked on its first clone")
    try:
        token = oauth.access_token_for(oauth.MICROSOFT_DEVOPS, secret)
    except oauth.OAuthError as exc:
        return CheckResult(False, str(exc), rejected=exc.rejected)
    url = "https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=7.1"
    return _checked(
        "Azure DevOps",
        lambda: _request(url, _bearer(token)),
        lambda body: body.get("emailAddress") or body.get("displayName"),
    )


def _model_count(body: Any) -> str | None:
    items = body.get("data") if isinstance(body, dict) else None
    if items is None and isinstance(body, dict):
        items = body.get("models")
    return f"{len(items)} models" if isinstance(items, list) else None


def _check_openai(secret: dict[str, Any]) -> CheckResult:
    base = str(secret.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    return _checked(
        "OpenAI",
        lambda: _request(f"{base}/models", _bearer(str(secret["api_key"]))),
        summary=_model_count,
    )


def _check_foundry(secret: dict[str, Any]) -> CheckResult:
    from pheasant import foundry

    endpoint = str(secret.get("endpoint") or "").strip()
    if not endpoint:
        return CheckResult(False, "no endpoint given; stored without a check")
    url = f"{foundry.openai_v1_url(endpoint).rstrip('/')}/models"
    headers = foundry.auth_headers(url, str(secret["api_key"]))
    return _checked("Azure AI Foundry", lambda: _request(url, headers), summary=_model_count)


def _check_anthropic(secret: dict[str, Any]) -> CheckResult:
    headers = {"x-api-key": str(secret["api_key"]), "anthropic-version": "2023-06-01"}
    return _checked(
        "Anthropic",
        lambda: _request("https://api.anthropic.com/v1/models", headers),
        summary=_model_count,
    )


def _check_gemini(secret: dict[str, Any]) -> CheckResult:
    headers = {"x-goog-api-key": str(secret["api_key"])}
    return _checked(
        "Gemini",
        lambda: _request("https://generativelanguage.googleapis.com/v1beta/models", headers),
        summary=_model_count,
    )


def _check_drive(secret: dict[str, Any]) -> CheckResult:
    from pheasant.connections import google

    try:
        token = google.access_token_for(secret)
    except google.GoogleAuthError as exc:
        return CheckResult(False, str(exc), rejected=exc.rejected)
    return _checked(
        "Google Drive",
        lambda: _request(
            "https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)",
            _bearer(token),
        ),
        lambda body: (body.get("user") or {}).get("emailAddress"),
    )


TOKEN = Field("token", "Token", secret=True)
API_KEY = Field("api_key", "API key", secret=True)

PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="github",
        label="GitHub",
        kind="source",
        description="Private repositories on github.com: paste a repository URL to add one.",
        methods=(
            sign_in(oauth.GITHUB),
            Method(
                "token",
                "Personal access token",
                (
                    Field(
                        "token",
                        "Personal access token",
                        secret=True,
                        placeholder="github_pat_… or ghp_…",
                        help="Read access to the repositories' contents is enough.",
                    ),
                ),
            ),
        ),
        env="GITHUB_TOKEN",
        hosts=("github.com",),
        git_user="x-access-token",
        source_types=("repository",),
        token_url="https://github.com/settings/personal-access-tokens/new",
        check=_check_github,
    ),
    Provider(
        id="azure_devops",
        label="Azure DevOps",
        kind="source",
        description="Private Azure Repos (dev.azure.com): paste a repository URL to add one.",
        methods=(
            sign_in(oauth.MICROSOFT_DEVOPS),
            Method(
                "token",
                "Personal access token",
                (
                    Field(
                        "token",
                        "Personal access token",
                        secret=True,
                        help="Scope Code (Read). It is checked on the first clone.",
                    ),
                ),
            ),
        ),
        hosts=("dev.azure.com", "visualstudio.com"),
        git_user="pheasant",
        source_types=("repository",),
        token_url="https://learn.microsoft.com/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate",
        check=_check_azure_devops,
    ),
    Provider(
        id="gitlab",
        label="GitLab",
        kind="source",
        description="Private projects on gitlab.com: paste a project URL to add one.",
        methods=(
            sign_in(oauth.GITLAB),
            Method(
                "token",
                "Access token",
                (Field("token", "Access token", secret=True, placeholder="glpat-…"),),
            ),
        ),
        hosts=("gitlab.com",),
        git_user="oauth2",
        source_types=("repository",),
        token_url="https://gitlab.com/-/user_settings/personal_access_tokens",
        check=_check_gitlab,
    ),
    Provider(
        id="google_drive",
        label="Google Drive",
        kind="source",
        description="Files a Google account or service account can read; add gdrive: for them.",
        methods=(
            sign_in(oauth.GOOGLE_DRIVE),
            Method(
                "service_account",
                "Service account",
                (
                    Field(
                        "service_account_json",
                        "Service account key (JSON)",
                        secret=True,
                        kind="textarea",
                        help="Share the folders with the service account's email.",
                    ),
                    Field(
                        "subject",
                        "Act as (domain-wide delegation)",
                        required=False,
                        placeholder="name@yourdomain.com",
                    ),
                ),
            ),
            Method(
                "token",
                "Access token",
                (
                    Field(
                        "token",
                        "OAuth access token",
                        secret=True,
                        help="Expires within an hour; sign in or use a service account to keep it.",
                    ),
                ),
            ),
        ),
        source_types=("gdrive",),
        token_url="https://console.cloud.google.com/apis/credentials",
        check=_check_drive,
    ),
    Provider(
        id="api_token",
        label="API token",
        kind="source",
        description="A bearer token sent with every request an API or web source makes.",
        methods=(
            Method(
                "token",
                "Bearer token",
                (
                    TOKEN,
                    Field(
                        "header",
                        "Header",
                        required=False,
                        placeholder="Authorization",
                        help="Sent as 'Authorization: Bearer <token>' unless named otherwise.",
                    ),
                ),
            ),
        ),
        source_types=("api", "web_collection"),
    ),
    Provider(
        id="openai",
        label="OpenAI",
        kind="model",
        description="Embeddings, answers, captions and dictation (stands in for OPENAI_API_KEY).",
        methods=(
            Method(
                "token",
                "API key",
                (
                    Field("api_key", "API key", secret=True, placeholder="sk-…"),
                    Field(
                        "base_url",
                        "Base URL",
                        required=False,
                        placeholder="https://api.openai.com/v1",
                        kind="url",
                    ),
                ),
            ),
        ),
        env="OPENAI_API_KEY",
        token_url="https://platform.openai.com/api-keys",
        check=_check_openai,
    ),
    Provider(
        id="azure_foundry",
        label="Azure AI Foundry",
        kind="model",
        description="A Foundry resource's key (stands in for AZURE_FOUNDRY_API_KEY).",
        methods=(
            Method(
                "token",
                "Resource key",
                (
                    Field("api_key", "Key", secret=True),
                    Field(
                        "endpoint",
                        "Endpoint",
                        placeholder="https://<resource>.services.ai.azure.com",
                        kind="url",
                        help="Used to check the key.",
                    ),
                ),
            ),
        ),
        env="AZURE_FOUNDRY_API_KEY",
        token_url="https://ai.azure.com",
        check=_check_foundry,
    ),
    Provider(
        id="anthropic",
        label="Anthropic",
        kind="model",
        description="Claude for answers (stands in for ANTHROPIC_API_KEY).",
        methods=(Method("token", "API key", (Field("api_key", "API key", secret=True),)),),
        env="ANTHROPIC_API_KEY",
        token_url="https://console.anthropic.com/settings/keys",
        check=_check_anthropic,
    ),
    Provider(
        id="gemini",
        label="Gemini",
        kind="model",
        description="Gemini for answers (stands in for GEMINI_API_KEY).",
        methods=(Method("token", "API key", (Field("api_key", "API key", secret=True),)),),
        env="GEMINI_API_KEY",
        token_url="https://aistudio.google.com/apikey",
        check=_check_gemini,
    ),
    Provider(
        id="environment",
        label="Environment variable",
        kind="model",
        description="Any variable a setting names (api_key_env and friends), held here instead.",
        methods=(
            Method(
                "token",
                "Value",
                (
                    Field(
                        "variable",
                        "Variable name",
                        placeholder="MY_SERVICE_TOKEN",
                        help="The name a setting reads; the connection is named after it.",
                    ),
                    Field("value", "Value", secret=True),
                ),
            ),
        ),
    ),
)

BY_ID: dict[str, Provider] = {provider.id: provider for provider in PROVIDERS}


def provider(provider_id: str) -> Provider:
    try:
        return BY_ID[provider_id]
    except KeyError:
        known = ", ".join(BY_ID)
        raise KeyError(f"unknown provider {provider_id!r}; one of {known}") from None


def secret_value(provider_id: str, secret: dict[str, Any]) -> str | None:
    """The one string a connection stands in for (a token, a key, a value)."""

    for name in ("token", "api_key", "value"):
        value = str(secret.get(name) or "").strip()
        if value:
            return value
    return None


def bound_env(connection_provider: str, details: dict[str, Any]) -> str | None:
    """The environment variable a connection answers for, if any."""

    if connection_provider == "environment":
        return str(details.get("variable") or "") or None
    entry = BY_ID.get(connection_provider)
    return entry.env if entry else None


def git_provider_for(host: str) -> Provider | None:
    host = host.lower()
    for entry in PROVIDERS:
        if any(host == known or host.endswith("." + known) for known in entry.hosts):
            return entry
    return None
