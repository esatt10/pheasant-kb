# Run the web UI (and keep it up to date)

!!! tip "pheasant-kb is the default deployment"
    pheasant-kb serves the UI at <http://127.0.0.1:8765> after one
    `docker compose up -d`. The options below are for running it some other way.
    Start with [Run pheasant-kb](../pheasant-kb/index.md).

You have the `pheasant` CLI working and you want to *see* your knowledge base —
the three-pane workspace with sources, chat, and the graph canvas. This page is
the complete path from a shell prompt to a browser tab, plus the rules for
making sure the UI you are looking at is the current one.

!!! info "The UI is a separate workload"
    The UI is a static React bundle. It has no server of its own: it calls the
    pheasant HTTP API and renders what comes back. So "run the UI" always means
    two things — **something serving the bundle**, and **the pheasant API it can
    reach**. Every option below is just a different answer to those two.

## Pick a path

| Your situation | Use | UI lives at |
|---|---|---|
| I already run `pheasant` from the CLI and just want the UI | [Option 1 — pheasant serves it](#option-1-let-pheasant-serve-the-ui-no-docker) | `http://localhost:8765` |
| I want the whole thing in containers, one command | [Option 2 — `pheasant host`](#option-2-pheasant-host-one-line-containers) | `http://localhost:8080` |
| I want a brand-new container configured entirely in the UI | [Fresh UI-native reset](#fresh-ui-native-reset) | `http://localhost:8765` |
| I have this repo cloned and want the standard stack | [Option 3 — `docker compose`](#option-3-docker-compose-from-this-repo) | `http://localhost:8765` |
| I am editing the UI source | [Option 4 — Vite dev server](#option-4-vite-dev-server-for-ui-development) | `http://localhost:5173` |

Every path serves the same bundle from `ui/`. None of them change how indexing
works.

---

## Option 1: let pheasant serve the UI (no Docker)

Best if you already have the CLI running and do not want a second container.
pheasant mounts a built bundle at the root of its own API port, so the UI and
the API are the same origin and there is nothing to proxy.

**Prerequisites:** Node.js 20+ (only to build the bundle once) and a working
`pheasant` CLI.

```bash
# 1. Build the bundle (from a clone of this repo)
cd ui
npm ci            # first time only; `npm install` is fine too
npm run build     # writes ui/dist
cd ..

# 2. Point pheasant at some content and start it
pheasant up ~/notes --no-serve     # generates pheasant.yaml + indexes
pheasant start --config pheasant.yaml
```

On startup pheasant tells you whether it found a bundle:

```text
API + MCP:   http://0.0.0.0:8765
Web UI:      http://localhost:8765
```

Open that URL. The API routes (`/health`, `/search`, …) keep working — the
bundle is mounted last, so it only catches paths the API does not claim. If the
banner says `Web UI: not served`, the bundle was not found; the two conditions
below are why.

!!! warning "Build with no `VITE_PHEASANT_API_BASE` set"
    A plain `npm run build` produces a bundle that calls the API on its **own
    origin**, which is what this option needs. Do not reuse the sidecar image's
    bundle here: that one is built with `VITE_PHEASANT_API_BASE=/api` and its
    requests would 404 against pheasant directly.

`pheasant up <target>` serves the same way (index, then serve on `:8765`), so
once `ui/dist` exists you get the UI from the one-line command too.

Two conditions must hold or nothing is mounted (pheasant starts normally and
serves API only):

- `server.ui.enabled` is `true` in your config — it is by default.
- A built bundle exists. pheasant looks at `$PHEASANT_UI_DIST` first, then at
  `ui/dist` next to the source checkout. If you installed the package (not a
  clone), there is no `ui/` directory — build the bundle wherever you like and
  export the path:

```bash
PHEASANT_UI_DIST=/path/to/ui/dist pheasant start --config pheasant.yaml
```

Rebuild (`npm run build`) and restart `pheasant start` whenever you pull new UI
changes — the bundle is read from disk at startup.

---

## Option 2: `pheasant host` (one line, containers)

`pheasant host` resolves your targets, writes a config, writes a compose file,
and brings up **both** containers: pheasant and the UI sidecar.

```bash
pheasant host ~/notes
```

```text
Wrote /home/you/pheasant.yaml
Wrote /home/you/pheasant.container.yaml
Wrote /home/you/docker-compose.pheasant.yml
pheasant API + MCP: http://localhost:8765
pheasant UI:        http://localhost:8080
```

Open **<http://localhost:8080>**.

Useful flags:

```bash
pheasant host ~/notes --ui-port 9090        # move the UI off 8080
pheasant host ~/notes --no-ui               # API + MCP only
pheasant host ~/notes --print-only          # write the compose file, run it yourself
pheasant host ~/notes --ui-image ghcr.io/esatt10/pheasant-ui:<version>  # pin a published bundle
```

Which UI bundle you get:

- **From a clone of this repo**, the generated compose file gets a `build:`
  context pointing at your `ui/` directory and `pheasant host` runs
  `docker compose up -d --build` — you always see *your* checkout's UI.
- **From an installed package**, there is no `ui/` to build, so the compose file
  pulls `ghcr.io/esatt10/pheasant-ui:<your pheasant version>` — the UI image is
  published from the same commit as the pheasant image, so the two always match.
- `--ui-image` overrides both and pulls exactly what you name.

The generated `docker-compose.pheasant.yml` is a normal file: edit it, commit
it, or re-run it yourself with `docker compose -f docker-compose.pheasant.yml up -d --build`.

---

## Fresh UI-native reset

From a clone of this repository, run exactly one Docker command:

```bash
docker compose -f deploy/compose/docker-compose.fresh.yml up -d --build --force-recreate
```

Open **<http://localhost:8765>**. The container starts with a freshly generated
container-native config and no sources, ready to configure from the browser.
Files uploaded in the UI are stored inside the persistent state volume.

!!! danger "This resets pheasant's Docker data"
    Every run clears the named config, state, and export volumes before
    startup. It does not delete host files, Docker images, or unrelated Docker
    volumes. Use ordinary `docker compose up -d --build` when you want to keep
    an existing knowledge base.

The compose file is standalone, so Docker does not load this checkout's
`docker-compose.override.yml`; an old `pheasant.yaml` bind mount or repository
workspace therefore cannot be reused accidentally. The standard image then
creates `/config/pheasant.yaml` from its live defaults. If `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, or `GEMINI_API_KEY` exists in your shell or repository
`.env`, Compose passes it into the container without writing its value to YAML.

---

## Option 3: `docker compose` from this repo

The repository keeps its Compose manifests under `deploy/compose/`; the root
stays reserved for source and contributor entry points. Copy the environment
template once, then launch the standard repository stack:

```bash
git clone https://github.com/esatt10/pheasant-kb && cd pheasant-kb
cp deploy/compose/.env.example .env
docker compose --env-file .env -f deploy/compose/docker-compose.yml up -d --build
```

Open **<http://localhost:8765>**. The bundled UI and API share that port.

### Point it at your own content

Add paths or repository URLs from the Sources page, or choose one of the
small, advanced and scalable presets in `deploy/compose/README.md`. That guide owns the
profile-specific variables and commands so they do not drift across pages.

The API publishes to loopback by default. It is unauthenticated, so only widen
the bind address behind an authenticating ingress. See [Security](../security.md).

---

## Option 4: Vite dev server (for UI development)

Hot reload against a pheasant that is already running (from any option above,
or a bare `pheasant start`):

```bash
cd ui
npm ci
npm run dev            # http://localhost:5173
```

The dev server proxies `/api/*` to `http://localhost:8765`. Point it elsewhere:

```bash
PHEASANT_API_BASE=http://localhost:9000 npm run dev
```

Before opening a PR:

```bash
npm run typecheck
npm run build
```

---

## Getting the *current* UI

Almost every "my UI is stale" report is one of these three. The rules:

| Situation | What actually happens | Do this |
|---|---|---|
| Compose reuses a local image | An image with that tag already exists, so `docker compose up -d` never rebuilds it | `docker compose up -d --build` (or `--build pheasant-ui` for just the UI) |
| A `:latest` tag is cached | Docker does not re-pull a tag it already has | `docker compose pull` — or prefer the version-pinned tags, which is what pheasant generates |
| pheasant-served bundle | `ui/dist` is read at process start | `npm run build`, then restart `pheasant start` |

The nuclear option, when you want to be certain nothing is cached:

```bash
docker compose down
docker image rm ghcr.io/esatt10/pheasant-ui:$(python -c "from pheasant.version import __version__; print(__version__)")
docker compose up -d --build
```

Upgrading pheasant itself upgrades the UI with it: the two images are built from
the same commit and published under the same version tag, so
`docker compose pull && docker compose up -d` moves both.

---

## Verify it worked

Work down this list; the first failure tells you which layer is broken.

```bash
# 1. The API is alive
curl -fsS http://localhost:8765/health

# 2. Something is actually indexed (`indexed_artifacts` > 0)
curl -fsS http://localhost:8765/overview

# 3. The bundle is being served
curl -fsSI http://localhost:8080 | head -1        # sidecar
curl -fsSI http://localhost:8765 | head -1        # pheasant-served

# 4. The UI's own path to the API resolves (sidecar only)
curl -fsS http://localhost:8080/api/health
```

Then open the UI. A working first load shows your sources in the left pane. If
the graph looks sparse, that is by design: `concept` and `chunk` nodes are
hidden by default because they dominate a real index — turn them on from the
legend, which doubles as the type filter.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| UI loads but every panel errors / "failed to fetch" | The bundle cannot reach the API | Sidecar: is the `pheasant` container healthy (`docker compose ps`)? The nginx proxy resolves the service name `pheasant`, so both containers must be on the same compose project. pheasant-served: check `curl localhost:8765/health`. |
| `manifest unknown` / `denied` pulling `pheasant-ui` | That version's UI image predates UI image publishing, or the tag does not exist | Build it instead — `docker compose up -d --build` from a clone — or pass `--ui-image` with a tag that exists. |
| UI shows old code after a change | Compose reused the local image | `docker compose up -d --build`; see [Getting the current UI](#getting-the-current-ui). |
| UI is up, canvas is empty | Nothing indexed yet, or the wrong directory was mounted | `curl localhost:8765/overview`. If `indexed_artifacts` is 0, check what is mounted at `/workspace` and the `sources:` in your config; then `pheasant sync --all --mode full` or `POST /sync`. |
| Only one node, labelled with your KB name | The workspace mount is empty (often the `./workspace` default) | Set `PHEASANT_WORKSPACE_PATH` / `deployment.compose.workspace_path` to real content and re-run. |
| Blank page, 404 on a deep link like `/sources` | Serving `dist/` with a static server that has no SPA fallback | Use the provided nginx config or pheasant's own mount — both fall back to `index.html`. |
| `port is already allocated` | 8080 or 8765 in use | `PHEASANT_UI_PORT=9090 docker compose up -d` or `pheasant host --ui-port 9090 --port 9100`. |
| Nothing on 8080 from another machine | Ports publish to loopback by default | Tunnel (`ssh -L 8080:localhost:8080 host`), or set `PHEASANT_BIND` **only** behind an authenticating proxy. |
| `pheasant start` serves the API but not the UI | No built bundle found, or `server.ui.enabled: false` | `npm run build` in `ui/`, or set `PHEASANT_UI_DIST` to a bundle; confirm `server.ui.enabled`. |
| Chat answers are terse / extractive only | No model connected — this is the designed fallback, not a failure | Connect a key in the UI, or see [Ask your knowledge base](chat-and-ui.md). |

More: [Troubleshooting](../troubleshooting.md) ·
[Deployment](../deployment.md) ·
[Ask your knowledge base](chat-and-ui.md) ·
[UI source and design notes](https://github.com/esatt10/pheasant-kb/tree/main/ui)
