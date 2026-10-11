# pheasant Web UI

A React + Vite front end for pheasant: a three-pane research workspace over
your indexed knowledge base. It is a **separate workload** — it builds to a
static bundle and talks to the pheasant container purely over its HTTP API, so
the indexing container is unchanged. The published image bakes this bundle in
and serves it on the same port; this directory is the source for that build and
for the optional standalone `pheasant-ui` image.

```
┌──────────┬──────────────────────────┬──────────────────────┐
│ Sources  │ Chat                     │ Graph / Facts / Node │
│  scope   │  question → grounded     │  cited nodes lit up  │
│  + add   │  answer with [1] chips   │  surfaced triples    │
└──────────┴──────────────────────────┴──────────────────────┘
```

## Features

- **Chat** — ask in prose; answers are grounded in your own index and cite the
  passages they came from. Clicking a `[1]` chip focuses that node on the graph
  and opens it in the inspector. Works with **Anthropic**, **OpenAI** or
  **Gemini**, and works *without* any of them (extractive answers). Full
  behavior: [Ask your knowledge base](../docs/how-to/chat-and-ui.md).
- **Facts** — subject–predicate–object triples read straight off the graph one
  hop from each cited passage, collected round-robin so one hub document cannot
  crowd out the rest. Both endpoints of a fact are clickable.
- **Graph** — Cytoscape canvas with a small shape vocabulary and a legend that
  doubles as the type filter. `concept` and `chunk` are hidden by default (they
  routinely make up most of a real index). Expand a node's sub-network, pivot
  across relationships, inspect identity/links/content.
- **Workflow** — pick the agent that answers questions. With the `[agent]`
  extra and a model connected, the default is a LangGraph agent that plans
  sub-queries, searches every mode, walks the graph, grades its own evidence
  and retries when it is thin, then verifies its citations — with the trace
  shown under the answer. Custom workflows registered under the
  `pheasant.agent_workflows` entry-point group appear here too:
  [Customize the answering workflow](../docs/how-to/agent-workflows.md).
- **Sources** — one field takes a path, URL, glob or connector name and
  pheasant detects the rest; selecting a source scopes both chat and graph to
  it. **Advanced…** exposes the whole source schema — include/exclude globs,
  depth, chunking, branch policy, sync triggers, connector settings — with the
  type list read from the server, so installed connector plugins (Notion,
  Slack, Confluence, Drive, IMAP, or your own) are offered alongside the
  built-ins. Service-backed types skip the directory browser entirely.
- **Semantic search** — turn embeddings on, pick a provider and a vector
  backend (only ones installed here are offered), see what fraction of the
  index actually has vectors, and embed already-indexed content without
  re-reading a single source file.
- **Onboarding** — when nothing is indexed you get an actionable empty state
  (add a source, or the one-line command), not an empty canvas.
- **Connect agent** — MCP transports, a ready-to-paste `.mcp.json`, and the
  tool list an attached coding agent gets.
- **Settings** — edit every config section via a form, drop to raw YAML, and
  preview a diff before validating and writing.

## Design notes

- **Light by default.** This is a reading tool; dark is opt-in via the theme
  toggle and persists in `localStorage`. Both themes come from the CSS custom
  properties at the top of `styles.css` — change a token, not a rule.
- **Small shape vocabulary.** Four shapes, not twelve: rounded rectangles for
  containers, rectangles for documents, circles for ideas, diamonds for code
  symbols. Colour carries the finer distinction; size grows with connectivity.
- **Nothing secret is stored here.** A chat API key the user pastes goes to the
  server, which holds it in memory behind an opaque token; the browser keeps
  only that token, in `sessionStorage`. Connector credentials are never typed
  in at all — a source stores the *name* of an environment variable.
- **Parity with the API.** Every control is a call the server already exposes;
  the UI adds no capability of its own. If something is configurable in
  `pheasant.yaml` it should be reachable here, and what a deployment can offer
  (source types, vector backends, workflows) is read from the server rather
  than hardcoded in this bundle — an installed plugin shows up without a
  rebuild.

## Develop

```bash
cd ui
npm install
npm run dev        # http://localhost:5173, proxying /api -> :8765
```

Point the proxy elsewhere with `PHEASANT_API_BASE`:

```bash
PHEASANT_API_BASE=http://localhost:9000 npm run dev
```

## Build

```bash
npm run typecheck
npm run build      # outputs ui/dist
npm run preview    # serve the production build locally
```

| Build variable | Default | Purpose |
|---|---|---|
| `VITE_PHEASANT_API_BASE` | `/api` | Where the API lives. Set it to an empty string when building a bundle served directly by the Pheasant API. |
| `VITE_PHEASANT_GRAPH_NODE_LIMIT` | `1200` | Node budget per graph request. |
| `VITE_PHEASANT_GRAPH_LINK_LIMIT` | `3600` | Link budget per graph request. |

The bounded preview keeps large indexes from blocking the browser; node
expansion still fetches focused graph slices on demand.

## Deploy

Step-by-step for all of these, plus how to avoid serving a stale bundle:
[Run the web UI](../docs/how-to/run-the-ui.md).

- **Sidecar (default):** `docker compose up -d --build` — or
  `pheasant host <target>` — builds this image and serves it behind nginx,
  proxying `/api/*` to the pheasant container, on `http://localhost:8080`.
  Pass `--build` whenever the UI source changed: Compose reuses an existing
  local image for the tag otherwise, so edits appear to do nothing.
- **Served by pheasant:** `VITE_PHEASANT_API_BASE="" npm run build` and the API mounts `dist/`
  automatically at its own port (`http://localhost:8765`), or point
  `PHEASANT_UI_DIST` at a bundle elsewhere. Mounted only when
  `server.ui.enabled` is true and the directory exists. The empty base makes
  this bundle call the same-origin API rather than going through `/api`.
- **Published image:** `ghcr.io/esatt10/pheasant-ui:<pheasant version>` — built
  from the same commit as the pheasant image and tagged with the same version,
  so the pair always match.

## Backend routes used

`/overview`, `/graph`, `/graph/slice`, `/graph/neighbors`, `/nodes/explain`,
`/nodes/content`, `/files/summary`, `/sources` (GET/POST),
`/sources/quick-add`, `/sources/{name}/disable|promote`,
`DELETE /sources/{name}`, `/sync/{name}`, `/fs/list`, `/search`,
`/assistant/status|key|chat`, `/mcp/info`, `/config` (GET/PUT), `/health`.
