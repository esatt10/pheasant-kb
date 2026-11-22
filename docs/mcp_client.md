# MCP client setup

Every pheasant region serves MCP at `/mcp` (streamable HTTP) on the api tier,
and every call carries the API token as a bearer header. With pheasant-kb
running ([Run pheasant-kb](pheasant-kb/index.md)) that is
`http://127.0.0.1:8765/mcp`. Read the token with:

```bash
docker compose exec api sh -c 'cat /run/pheasant/secrets/api-token'
```

On a region that signs in with Entra, an MCP client that supports OAuth signs
in instead of sending the shared token ([Host a region](how-to/hosted-regions.md)).

## Claude Code

```bash
claude mcp add --transport http pheasant http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $(docker compose exec -T api sh -c 'cat /run/pheasant/secrets/api-token')"
```

## VS Code

Copy the template, which asks for the token once and keeps it in VS Code's
secret storage:

```bash
mkdir -p .vscode
cp examples/vscode/mcp.json .vscode/mcp.json
```

```json
{
  "inputs": [
    {"type": "promptString", "id": "pheasant-token", "description": "pheasant API token", "password": true}
  ],
  "servers": {
    "pheasant": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {"Authorization": "Bearer ${input:pheasant-token}"}
    }
  }
}
```

Then run **MCP: List Servers**, start `pheasant`, and use its tools from Chat
in Agent mode. `.vscode/mcp.json` is user-specific; do not commit it.

## Cursor and other clients

Any client that speaks streamable HTTP takes the same URL and an
`Authorization: Bearer <token>` header. The tools are listed in
[MCP tools and resources](mcp_tools.md).

## stdio, inside the api container

A client that only speaks stdio can run the server inside the api container,
which needs no token because `docker exec` already is the operator's access:

```bash
pheasant client-config vscode --container-name pheasant-kb-api-1 --output .vscode/mcp.json
pheasant client-config claude-code --mode docker-exec --container-name pheasant-kb-api-1 -o .mcp.json
```

Both emit `docker exec -i <container> python -m pheasant mcp --config
/config/pheasant.yaml --transport stdio`.
