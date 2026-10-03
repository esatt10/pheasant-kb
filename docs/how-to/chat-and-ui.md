# Ask your knowledge base (the web UI)

The pheasant UI is a three-pane workspace: **Sources** on the left,
**Chat** in the middle, **Knowledge** (graph / facts / node) on the right.
You ask a question in prose; pheasant answers from your own indexed content,
cites the passages it used, and lights up the corresponding nodes on the
knowledge graph.

Start it with `pheasant host <target>` (containers, UI included on `:8080`),
`docker compose -f deploy/compose/docker-compose.yml up -d --build` from a clone, or by building `ui/dist` and
letting `pheasant start` serve it on `:8765`. Full step-by-step for each —
and what to do when the UI will not come up or looks stale — is in
[Run the web UI](run-the-ui.md).

---

## The chat layer

Every answer goes through the same four steps, and all four are inspectable:

1. **Retrieve** — the ordinary hybrid self-search (`text` + `graph`, plus
   `vector` when embeddings are on) runs against your index.
2. **Cite** — hits become numbered passages. The citation strip under an
   answer shows them all; the ones the answer actually used are at full
   strength, retrieved-but-unused ones are dimmed.
3. **Surface facts** — one hop out from each cited passage into the
   entity/symbol/document layer of the graph, rendered as
   subject–predicate–object triples in the **Facts** tab. Facts are
   collected round-robin across the cited sources, so a well-connected
   document cannot crowd out the rest.
4. **Answer** — a chat model writes the answer using *only* those passages,
   citing `[1]`, `[2]`, … Clicking a citation chip in the prose focuses that
   node on the graph and opens it in the **Node** tab.

!!! note "No LLM ever runs during indexing"
    The assistant is a query-time surface only. Indexing stays fully
    deterministic (rule-based parsing, content hashing, stable IDs), so
    re-syncing unchanged content produces byte-identical state whether or
    not a model is connected.

### Without a model

Step 4 is the only step that needs a provider. With none reachable, the
answer is **extractive**: the top passages verbatim, with their citations
and graph facts intact. That is the default, it works air-gapped, and it is
what the offline test suite exercises. Connecting a model upgrades the prose,
not the grounding.

If a *configured* model fails (rate limit, network, bad key), the answer
degrades to the same extractive form and says so explicitly — it will not
claim that no model is connected.

---

### Length, follow-ups, figures and diagrams

- **Length.** The picker under the chat box is *Auto* by default — each
  question decides ("in detail" writes a long, sectioned answer; "briefly" a
  short one) — or pin *Short*, *Medium* or *Long*.
- **Follow-ups.** Each question is sent with the recent answered turns, so
  "and what about the vault?" is understood. The composer says how many turns
  are in play; **New topic** starts fresh without clearing the thread.
- **Figures.** When a cited document shows an image the region indexed, the
  answer can show it inline, and "show me the … diagram" answers with the
  images themselves.
- **Diagrams.** **Draw a diagram** under an answer draws its cited passages;
  ◇ beside a source draws that one passage. Every box and arrow cites a
  passage — dashed ones do not — and a diagram that would be mostly guesswork
  is declined with the reason.
- **Expand and rearrange.** **Expand** on a visual opens it at the full size of
  the window; drag its nodes and the arrows follow, **Reset layout** restores the
  drawn layout, and **Collapse** or Esc goes back with your layout kept. See
  [expand a visual, and move things around](conversations-and-visuals.md#expand-a-visual-and-move-things-around).

Visuals render in pheasant's MCP App view, hosted in a sandboxed frame — the
same view an MCP host such as Claude renders for the `ask_knowledge_base`,
`create_visual` and `get_image` tools. See
[answer length, conversations, visuals and figures](conversations-and-visuals.md).

## Connecting a model

Three providers are supported: **Anthropic**, **OpenAI**, and **Google
Gemini**. There are two ways to supply a key.

### Server environment (operator)

Set the provider's key on the container and leave `assistant.provider` at
`auto`; pheasant picks the first provider whose variable is populated, in the
order Anthropic → OpenAI → Gemini.

```yaml
assistant:
  provider: auto            # or: anthropic | openai | gemini | none
  # model: claude-sonnet-5  # provider default when unset
```

```bash
docker run -e ANTHROPIC_API_KEY=sk-ant-… ghcr.io/esatt10/pheasant
```

The key is read from the environment at request time. It is never copied into
config, state, or logs.

### Session key (user, in the browser)

Click **Connect model**, pick a provider, and paste a key. What happens to it:

- it is sent once and held in the **server process's memory only**, behind an
  opaque session token — never written to `pheasant.yaml`, never to `/state`,
  never logged;
- the browser stores only the token, and only in `sessionStorage`, so it dies
  with the tab;
- the entry expires on a TTL (`assistant.session_key_ttl_minutes`, default
  720), is dropped when you click **Disconnect**, and is gone entirely on a
  container restart — the process holds no persistence hook for it at all;
- API responses return provider/model/expiry metadata and never the key.

Set `assistant.allow_session_keys: false` to disable this route and require
the environment variable instead.

---

## Reading the graph

The graph panel is deliberately quiet by default. `chunk`, `entity` and
`external_reference` nodes are **hidden by default** — one per passage, one
per unresolved import/link — they add volume, not structure. The legend at
the bottom is also the filter: click any type to show or hide it.

(`concept` nodes used to be hidden here too, and were ~87% of a real graph.
Concept extraction was retired on 2026-08-03 — see `docs/graph_model.md` — so
the graph is now roughly a seventh of its former size and what remains is
structure worth drawing.)

- **Colour** encodes node type; **shape** is a small vocabulary (rounded
  rectangles are containers, rectangles are documents, circles are ideas,
  diamonds are code symbols) so the canvas reads as structure rather than
  decoration.
- **Size** grows with connectivity, so hubs stand out without a second legend.
- **Layout** is switchable from the canvas controls: Automatic, Force,
  Concentric or Hierarchy. (A `Kamada-Kawai` option — ELK's stress
  majorization — used to be here; it broke the canvas in practice and was
  removed along with the `cytoscape-elk` dependency, which also shrank the
  UI bundle by about 1.5 MB.)
- **Clicking empty canvas deselects** without resetting your depth, centre or
  answer filter. A selected node also shows **all** of its links, even ones
  whose far end sits outside the current depth horizon — the horizon is a
  drawing budget, not a claim about what a node connects to.
- Asking a question **outlines the cited nodes** and fades the rest to
  context strength.
- Selecting a source in the left rail scopes both retrieval and the graph to
  it.

---

## Connecting a coding agent (MCP)

**Connect agent** in the top bar shows this region's MCP transports, a
ready-to-paste `.mcp.json`, and the full tool list an attached agent gets. The
same knowledge base backs both surfaces — the chat panel and an MCP client are
two front doors onto one index.

```bash
pheasant client-config claude-code -c pheasant.yaml -o .mcp.json
```

---

## Related settings

| Setting | Default | What it does |
|---|---|---|
| `assistant.enabled` | `true` | Turn the chat surface off entirely (`/assistant/chat` returns 403). |
| `assistant.provider` | `auto` | `auto`, `anthropic`, `openai`, `gemini`, or `none`. |
| `assistant.model` | provider default | The single chat and agent workflow model; setup marks the tested provider default as Recommended and also accepts a custom model ID. With `provider: auto`, leave this null. |
| `assistant.base_url` | provider default | Point at a gateway or self-hosted OpenAI-spec endpoint. |
| `assistant.api_key_env` | provider default | Read the key from a differently-named variable. |
| `assistant.allow_session_keys` | `true` | Allow browser-supplied keys. |
| `assistant.session_key_ttl_minutes` | `720` | How long a session key survives. |
| `assistant.max_context_chunks` | `8` | Passages retrieved and offered to the model. |
| `assistant.max_facts` | `12` | Graph facts surfaced per answer. |
| `assistant.workflow` | `auto` | Which agent workflow answers questions. See below. |
| `assistant.workflow_options` | `{}` | Per-workflow tuning. |

Full reference: [Configuration](../configuration.md).

---

## Choosing how questions get answered

**Workflow** in the chat pane header picks the agent that answers. With the
`[agent]` extra installed and a model connected, the default is a LangGraph
agent that plans sub-queries, searches every mode, walks the graph for related
material, grades its own evidence and loops when it is thin, then verifies its
citations — and shows you the trace of what it did under the answer. A
selection made here applies to your next question only; make it permanent with
`assistant.workflow`.

While a question is in flight the pane shows each workflow step as it
finishes. When a short or medium answer is written and checked in one call, its
draft streams in under a "Draft answer · checking citations" label and is
replaced by the verified answer. The trace under a finished answer breaks a
search step into its per-mode timings (for example `vector embeddings · 2
queries` and `hybrid · query 1`). If the region caps concurrent answers
(`assistant.latency.max_concurrent_answers`) and is full, the request is refused
with `429` and `ASSISTANT_BUSY`; retry shortly.

See [Customize the answering workflow](agent-workflows.md) for the tuning
options and for writing your own.

---

## Managing sources and semantic search from the UI

The UI is not a read-only viewport onto a YAML file — everything the config
schema and the HTTP API can express is reachable from it.

- **Sources → + Add source** takes a path, URL, glob or connector name and
  infers the rest (`POST /sources/quick-add`, the same inference
  `pheasant up` uses).
- **Sources → Advanced…** exposes the full source schema: include/exclude
  globs, folder depth, chunking, repository branch policy, sync triggers,
  URLs, and connector settings. The type picker is populated from
  `GET /sources/types`, so installed connector plugins (Notion, Slack,
  Confluence, Google Drive, IMAP, or your own) appear alongside the built-in
  types. Service-backed types skip the directory browser — there is no folder
  to pick — and take their credentials as an `api_key_env` naming an
  environment variable, never the secret itself.
- **Settings → Semantic search** configures `search.embeddings` and
  `search.vector_store`, shows what fraction of the index actually has
  vectors, and can embed already-indexed content without re-reading a single
  source file. See [Vector self-search](vector-search.md).

Every one of those is the same HTTP endpoint a script would call, so a
low-code user and a developer are operating one system rather than two.
