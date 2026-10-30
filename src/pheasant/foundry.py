"""Azure AI Foundry: which URLs belong to it, and how a request to one is signed.

Foundry serves the OpenAI wire unchanged on the unversioned v1 surface of a
resource, ``https://<resource>.services.ai.azure.com/openai/v1`` (the classic
``<resource>.openai.azure.com`` host answers the same paths), so the existing
stdlib clients need only two things to reach it: the base URL, which is
configuration, and the credential's header, which is here. Foundry reads a
resource key from ``api-key``; ``Authorization: Bearer`` is how it reads a
Microsoft Entra token, so sending a key there is a 401 rather than a fallback.
``model`` in a request body is the *deployment* name, which is why a Foundry
config names deployments wherever an OpenAI config names models.

Every client decides by the base URL's host rather than by a provider name,
so ``search.embeddings``, ``ingestion.captioner`` and ``ingestion.transcriber``
reach Foundry with ``provider: openai-spec`` and a Foundry ``base_url`` — no
second client, and an OpenAI URL is signed exactly as before.

Microsoft's own decision model, Microsoft-Decision-1, is not on the OpenAI
surface: it answers the System One API at the resource root
(:data:`SYSTEMONE_PATH`, no ``api-version``), which
:mod:`pheasant.assistant.systemone` translates.
"""

from __future__ import annotations

from urllib.parse import urlsplit

#: The variable the shipped Foundry examples read a resource key from. Any
#: name works (every section has its own ``api_key_env``); this one is listed
#: among the model keys a preparation worker refuses to hold.
API_KEY_ENV = "AZURE_FOUNDRY_API_KEY"

#: Hosts a Foundry (or Azure OpenAI) resource answers on.
HOST_SUFFIXES = (".services.ai.azure.com", ".openai.azure.com", ".cognitiveservices.azure.com")

#: The OpenAI-compatible surface of a resource, relative to its root.
OPENAI_V1_PATH = "/openai/v1"

#: Where Foundry serves Microsoft-Decision-1, relative to the resource root.
SYSTEMONE_PATH = "/providers/microsoft/v1/systemone"


def is_foundry_url(url: str | None) -> bool:
    """True when ``url`` is on a Foundry or Azure OpenAI resource host."""

    host = (urlsplit(url or "").hostname or "").lower()
    return host.endswith(HOST_SUFFIXES)


def auth_headers(url: str | None, api_key: str) -> dict[str, str]:
    """The header that carries ``api_key`` to ``url``.

    ``api-key`` for a Foundry host, ``Authorization: Bearer`` for everything
    else — which is every OpenAI-spec server this region already talks to.
    """

    if is_foundry_url(url):
        return {"api-key": api_key}
    return {"authorization": f"Bearer {api_key}"}


def resource_root(url: str) -> str:
    """``scheme://host[:port]`` of a Foundry URL, whatever path it carries."""

    parts = urlsplit(url)
    return f"{parts.scheme or 'https'}://{parts.netloc}"


def openai_v1_url(url: str) -> str:
    """The OpenAI v1 base for a Foundry URL an operator pasted.

    Accepts the resource root, a project endpoint
    (``…/api/projects/<name>``) or a URL already ending in ``/openai/v1``, so
    the endpoint shown on the resource's overview page works as it is copied.
    A URL that is not on a Foundry host is returned unchanged.
    """

    url = (url or "").strip().rstrip("/")
    if not is_foundry_url(url):
        return url
    path = urlsplit(url).path
    if OPENAI_V1_PATH in path:
        return resource_root(url) + path[: path.index(OPENAI_V1_PATH) + len(OPENAI_V1_PATH)]
    return resource_root(url) + OPENAI_V1_PATH


def systemone_url(url: str) -> str:
    """Microsoft-Decision-1's endpoint for any URL on the same resource."""

    return resource_root(url) + SYSTEMONE_PATH
