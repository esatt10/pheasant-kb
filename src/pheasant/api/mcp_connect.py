"""What ``GET /mcp/info`` tells a person about attaching an agent.

The dialog used to advertise ``http://<bind host>:<port>/mcp`` and a stdio
command naming this process's config path. Both are right for a laptop and
wrong for anything deployed: a region bound to ``0.0.0.0`` behind an ingress
told a browser to connect to ``http://0.0.0.0:8765/mcp``, and the stdio
command named ``/config/pheasant.yaml``, a file inside a container nobody
attaching an agent can reach.

So the URL is the one the region is actually reached at --
``security.entra.public_url`` when set, otherwise the origin of the request
asking (a browser is, by construction, somewhere that reaches it) -- and the
configs are for a *remote* server whenever that origin is not this machine:
an HTTP entry per client, with the way to authenticate the region supports.
HTTP only: an agent attached over MCP already has the connection this
describes.
"""

from __future__ import annotations

import json
from typing import Any

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
#: A bind address, never somewhere a client can connect to.
UNSPECIFIED_HOSTS = frozenset({"0.0.0.0", "::", "[::]", ""})


def request_origin(headers: Any, scheme: str, netloc: str) -> str:
    """``scheme://host`` as the caller reached this region.

    ``X-Forwarded-Proto``/``-Host`` win, because a region behind an ingress
    (Container Apps, a Kubernetes Ingress) is reached over HTTPS while the
    socket it answers on is plain HTTP.
    """

    forwarded_proto = str(headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    forwarded_host = str(headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host = forwarded_host or str(headers.get("host") or "") or netloc
    return f"{forwarded_proto or scheme}://{host}".rstrip("/")


def _hostname(origin: str) -> str:
    authority = origin.split("://", 1)[-1]
    if authority.startswith("["):
        return authority.split("]", 1)[0] + "]"
    return authority.rsplit(":", 1)[0] if authority.count(":") == 1 else authority


def base_url(config: Any, public_url: str, origin: str) -> str:
    if public_url:
        return public_url.rstrip("/")
    if origin and _hostname(origin) not in UNSPECIFIED_HOSTS:
        return origin
    host = config.server.host
    host = "127.0.0.1" if host in UNSPECIFIED_HOSTS else host
    return f"http://{host}:{config.server.port}"


def remote_configs(name: str, url: str, *, oauth: bool, token: bool) -> dict[str, str]:
    """One HTTP entry per client, authenticated the way this region allows.

    With OAuth the entry is the URL alone: the client discovers sign-in from
    the 401's ``resource_metadata`` and opens a browser. Without it, a bearer
    header read from the environment (Claude Code expands ``${VAR}``) or from
    a prompt (VS Code's ``inputs``), so no key is written into a file.
    """

    claude: dict[str, Any] = {"type": "http", "url": url}
    vscode: dict[str, Any] = {"type": "http", "url": url}
    configs: dict[str, str] = {}
    command = f"claude mcp add --transport http {name} {url}"
    if not oauth and token:
        claude["headers"] = {"Authorization": "Bearer ${PHEASANT_API_TOKEN}"}
        vscode["headers"] = {"Authorization": "Bearer ${input:pheasant-token}"}
        command += ' --header "Authorization: Bearer $PHEASANT_API_TOKEN"'
    configs["claude-code-command"] = command
    configs["claude-code"] = json.dumps({"mcpServers": {name: claude}}, indent=2)
    vscode_file: dict[str, Any] = {"servers": {name: vscode}}
    if "headers" in vscode:
        vscode_file["inputs"] = [
            {
                "type": "promptString",
                "id": "pheasant-token",
                "description": f"{name} API token or personal key",
                "password": True,
            }
        ]
    configs["vscode"] = json.dumps(vscode_file, indent=2)
    return configs


def connection_info(
    config: Any,
    identity: Any,
    *,
    origin: str,
    config_path: str,
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    from pheasant.mcp_client.agents import agent_mcp_config, render_agent_mcp_json

    transports = dict(config.server.mcp.transports)
    public_url = identity.public_url("") if identity is not None else ""
    base = base_url(config, public_url, origin)
    remote = _hostname(base) not in LOOPBACK_HOSTS
    http_url = f"{base}/mcp" if transports.get("streamable_http") else None
    entra = bool(identity is not None and identity.entra_enabled)
    auth = {
        # MCP OAuth exists only with a public URL (api/auth.oauth_routes).
        "oauth": entra and bool(public_url),
        "api_token": bool(identity is not None and identity.shared_token_enabled),
        "personal_keys": entra,
    }
    name = str(config.pheasant.name or "pheasant")
    if remote and http_url:
        clients = remote_configs(name, http_url, oauth=auth["oauth"], token=auth["api_token"])
    else:
        local = render_agent_mcp_json(agent_mcp_config("local", config_path=config_path))
        clients = {"claude-code": local, "cursor": local}
    return {
        "enabled": config.server.mcp.enabled,
        "transports": transports,
        "mode": "remote" if remote and http_url else "local",
        "server_name": name,
        "streamable_http_url": http_url,
        "stdio_command": ["pheasant", "mcp", "--transport", "stdio"],
        "config_path": config_path,
        "auth": auth,
        "tools": tools,
        "client_configs": clients,
    }
