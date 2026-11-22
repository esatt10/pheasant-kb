# Attach your knowledge base to a coding agent

Give Claude Code, Cursor, or any MCP client direct access to your indexed
knowledge — search, graph traversal, and provenance-tagged context — in
about five minutes.

## 1. Index something

With pheasant-kb running ([Run pheasant-kb](../pheasant-kb/index.md)), add a
source: paste a path or URL into **+ Add source** in the UI, or mount a host
folder ([Index a folder on the host](../pheasant-kb/configure.md#index-a-folder-on-the-host)).
The **Syncs** page shows when it has finished.

## 2. Point the agent at the region

The MCP endpoint is `http://127.0.0.1:8765/mcp` (streamable HTTP) and every
call carries the API token. For Claude Code:

```bash
claude mcp add --transport http pheasant http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $(docker compose exec -T api sh -c 'cat /run/pheasant/secrets/api-token')"
```

VS Code, Cursor and stdio-only clients are in [MCP client setup](../mcp_client.md).

## 3. Use it

Restart the agent (or approve the new MCP server when prompted). The agent
now sees the pheasant tools — `search_context`, `get_relevant_files`, graph
neighbours, `ask_knowledge_base`, sync and ingest tools — over your indexed
sources. Ask it things like *"find the notes where I compared retention
policies"* and it will cite chunks with file-level provenance.

An agent that adds content (`register_source`, `submit_documents`) should
wait for the sync before relying on it: `wait_for_sync`, as the server's own
instructions say. The skills under `.agents/skills/` describe each workflow
for an agent: retrieval, composing an artifact, and ingesting.

## Several knowledge bases

One region is one knowledge base. An agent that needs several attaches each
region as its own MCP server, under its own name and token.
