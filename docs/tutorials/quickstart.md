# Quickstart: a knowledge base in fifteen minutes

By the end of this tutorial you will have pheasant-kb running, a folder of your
own indexed into it, and a cited answer to a question about it, asked three
ways: in the web UI, over HTTP and over MCP.

You need Docker with Compose v2 and an OpenAI API key (or an Azure AI Foundry
resource: [Use Azure AI Foundry models](../pheasant-kb/foundry.md)).

## 1. Start pheasant-kb

```bash
git clone https://github.com/esatt10/pheasant-kb && cd pheasant-kb
echo "OPENAI_API_KEY=sk-..." > .env
docker compose up -d
```

The first start pulls the images and initializes the database, which takes a
few minutes. Wait until every long-running service reports `healthy`:

```bash
docker compose ps
```

`secrets-init`, `db-init` and `workspace-init` exit when they are done; that is
expected.

## 2. Get the API token

Every request carries a token generated on first boot:

```bash
TOKEN=$(docker compose exec -T api sh -c 'cat /run/pheasant/secrets/api-token')
curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/ready
```

`/ready` answers with the region's state, including the graph generation it
serves. `GET /health` answers without a token; everything else needs it.

## 3. Give it a folder

Make a small folder to index:

```bash
mkdir -p ~/kb-demo
printf '# Project Atlas\n\nAtlas is the billing service. Owner: payments team.\nIt rotates its signing keys every Monday.\n' > ~/kb-demo/atlas.md
printf 'def charge(amount):\n    """Charge a customer through Atlas."""\n    return amount\n' > ~/kb-demo/billing.py
```

A source can only read a path the containers can see, so mount the folder under
`/data` for the indexer (which reads it) and the api tier (which shows the
original in the document viewer). Create `compose.override.yaml` beside
`compose.yaml`:

```yaml
services:
  indexer:
    volumes:
      - ~/kb-demo:/data/kb-demo:ro
  api:
    volumes:
      - ~/kb-demo:/data/kb-demo:ro
```

```bash
docker compose up -d
```

Now register it. In the UI: open <http://127.0.0.1:8765>, paste the token into
**Connect API**, then **Sources → Add** with `/data/kb-demo`. Or over HTTP:

```bash
curl -fsS -X POST http://127.0.0.1:8765/sources/quick-add \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"target": "/data/kb-demo"}'
```

The api tier does not index: it publishes the sync to the queue, and the
indexer claims it. Wait for it to finish:

```bash
curl -fsS -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/sources/kb-demo/wait?timeout_seconds=55"
```

`state: finished` with `health.percent: 100` means every file was read. The
same source shows up on the **Syncs** page with its run and its trace.

Files you just want indexed, without a mount, can be dropped onto the UI's drop
zone instead: they land in the `uploads` source.

## 4. Search it

```bash
curl -fsS -X POST http://127.0.0.1:8765/search \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"query": "who owns billing", "mode": "hybrid", "max_results": 5}'
```

Each result names its source, path, lines and the arm that found it (text,
vector or graph), and the response carries the graph generation that answered.

## 5. Ask it

```bash
curl -fsS -X POST http://127.0.0.1:8765/assistant/chat \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"question": "When does Atlas rotate its signing keys?"}'
```

The answer cites the passages it came from (`[1]` is `atlas.md`), and its
`steps` show what each stage did and how long it took. In the UI, the same
question in the Notebook streams the answer and links each citation to the
passage in the viewer.

## 6. Hand it to an agent

```bash
claude mcp add --transport http pheasant http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $TOKEN"
```

The agent now has `search_context`, `ask_knowledge_base`, `browse_source`,
`read_document`, `compose_artifact`, `record_artifact` and the rest of
[the MCP tools](../mcp_tools.md). Any other MCP client takes the same URL and
bearer header; the UI's **Connect agent** button shows the snippet.

## Where to next

- [Configure pheasant-kb](../pheasant-kb/configure.md): models, sources,
  retrieval, ingestion and the features you can turn off.
- [Configure sources](../how-to/sources.md): repositories, web collections,
  SharePoint and Google Drive, and their sync schedules.
- [Answers, conversations and visuals](../how-to/conversations-and-visuals.md):
  long answers, `@` keywords, diagrams.
- [Index images and audio](multimodal.md).
- [Scale it](../pheasant-kb/scale.md) when the corpus grows.
