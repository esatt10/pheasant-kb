# Demo: microsoft/agent-framework with OpenAI embeddings + agentic chat

A complete, runnable demo of the parts of pheasant that are off by default:
semantic indexing through OpenAI embeddings, and answer summation by the
LangGraph agent loop instead of a single-pass call.

| Piece | Default stack | This demo |
|---|---|---|
| Index | keyword only (SQLite FTS5) | FTS5 **+ OpenAI `text-embedding-3-small` @ 1536 dims** |
| Search | `hybrid` (text + graph) | `hybrid` incl. vector candidates, plus `mode=vector` |
| Answers | single pass, or extractive with no model | **nano-tier chat model, `workflow: agentic`** |
| Content | this repo | **microsoft/agent-framework**, read-only |
| Image extras | `[mcp]` | `[mcp,agent]` (adds langgraph) |
| State | `pheasant-state` volume | separate `pheasant-demo-state` volume |

## 1. Set two values

```bash
cp examples/demo-agent-framework/.env.demo.example .env
```

Edit `.env` (it is gitignored — the key never enters a config file, the image,
`/state`, or logs; the configs name the *env var* only):

```bash
OPENAI_API_KEY=sk-...
AGENT_FRAMEWORK_PATH=C:/src/agent-framework
```

If you do not have the repo yet:

```bash
git clone --depth 1 https://github.com/microsoft/agent-framework C:/src/agent-framework
```

## 2. Run it

```bash
docker compose -f docker-compose.yml \
               -f examples/demo-agent-framework/docker-compose.demo.yml \
               up -d --build
```

UI at <http://localhost:8080>, API at <http://127.0.0.1:8765>.

The first sync embeds every chunk, so it costs real tokens and takes a few
minutes. Re-syncing is close to free: the pre-read sha256 skip means an
unchanged file never reaches the embedder.

Watch it land:

```bash
docker compose logs -f pheasant
curl -s http://127.0.0.1:8765/search/embeddings | python -m json.tool
```

## 3. Check the wiring

```bash
# vectors exist and the store is populated
curl -s http://127.0.0.1:8765/search/embeddings

# semantic search, no keyword overlap required
curl -s -X POST http://127.0.0.1:8765/search \
  -H 'content-type: application/json' \
  -d '{"query":"how do agents hand work to each other","mode":"vector","max_results":5}'

# the agentic loop: `workflow` comes back as "agentic" and `steps` is the trace
curl -s -X POST http://127.0.0.1:8765/assistant/chat \
  -H 'content-type: application/json' \
  -d '{"question":"What is the workflow abstraction in this framework?"}'
```

## Model ids are per-account

`assistant.model` must be an id **your key can reach**, or the chat surface
reports a 404 `model_not_found` verbatim (it never silently substitutes). List
what is available:

```bash
curl -s https://api.openai.com/v1/models   -H "Authorization: Bearer $OPENAI_API_KEY" | grep -o '"id": "gpt-6[^"]*"'
```

The demo ships `gpt-6-luna`. Swap it in the config, or override per browser
session from the UI's "Connect model" dialog without touching the config.

## Notes

- **Cost control.** `search.embeddings.enabled: false` turns embedding off
  without touching anything else; already-embedded content stays queryable.
- **Determinism is unchanged.** Embedding is the one sanctioned network call in
  the indexing path, and it is content-addressed — the graph and index built
  from unchanged bytes are identical.
- **`workflow: agentic` is deliberate, not `auto`.** If the `[agent]` extra is
  missing, this fails loudly in the logs instead of quietly downgrading to a
  single-pass answer. `PHEASANT_EXTRAS=mcp,agent` in `.env` is what puts
  langgraph in the image.
- **Bigger corpora:** switch `search.vector_store.provider` to `lancedb` and add
  `vector` to `PHEASANT_EXTRAS`. `numpy` is the zero-dependency default and is
  fine for one repo.
- **Ports and containers are shared** with the default stack (same compose
  project), so bringing this up replaces a running default stack. The state
  volumes are separate, so nothing you indexed there is touched.
