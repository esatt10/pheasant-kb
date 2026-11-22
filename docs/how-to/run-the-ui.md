# Run the web UI (and keep it up to date)

The web UI is a static React bundle (`ui/`) baked into the pheasant image and
served by the **api** tier at <http://127.0.0.1:8765>. It has no server of its
own: it calls the same HTTP API agents and your applications call, with the
API token you paste into **Connect API**. There is no separate UI container.

## Open it

With pheasant-kb running ([Run pheasant-kb](../pheasant-kb/index.md)):

1. Go to <http://127.0.0.1:8765>.
2. Paste the token into **Connect API**:

    ```bash
    docker compose exec api sh -c 'cat /run/pheasant/secrets/api-token'
    ```

The browser keeps the token for the session only. On a region that signs in
with Entra, **Sign in** replaces the token ([Host a region](hosted-regions.md)).

A deep link (`/sources`, `/syncs`, `/artifacts`, `/admin`) reloads into the app
rather than the API route of the same name: a document navigation gets the UI,
`fetch()` and `curl` get the API.

## Develop the UI

Hot reload against a running pheasant-kb:

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

The browser tests (`tests/test_mcp_app_expand.py`,
`tests/test_dictation_browser.py`) run against the built bundle in Chromium.

## Getting the current UI

The UI is built from the same commit as the rest of the image and moves with
it, so upgrading pheasant-kb upgrades the UI:

```bash
docker compose pull && docker compose up -d
```

| Situation | What happens | Do this |
|---|---|---|
| `compose.yaml` pins an older image | The api serves the bundle that image was built with | Update the pin, or set `PHEASANT_IMAGE` in `.env` |
| A local image of the same tag | Compose does not rebuild it | `docker compose build api && docker compose up -d` from a clone |
| The browser cached an old bundle | The hashed asset names change with each build, so a reload fetches the new ones | Hard-reload once |

## Verify it worked

Work down this list; the first failure tells you which layer is broken.

```bash
TOKEN=$(docker compose exec -T api sh -c 'cat /run/pheasant/secrets/api-token')
# 1. The api is alive (no token needed)
curl -fsS http://127.0.0.1:8765/health
# 2. The token is accepted and the region is ready
curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/ready
# 3. Something is indexed
curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/knowledge-base/overview
# 4. The bundle is served
curl -fsSI http://127.0.0.1:8765/ | head -1
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every panel says the API refused | No token, or an old one | Paste the current token into **Connect API** (it changes only if the `pheasant-secrets` volume is deleted) |
| A banner says the region is waking | The database is starting after a scale-to-zero | It clears by itself once every part reports ready (`GET /region/status`) |
| The jobs tray shows a sync as queued and nothing moves | No indexer is draining the queue | `docker compose ps indexer`; `GET /queue` shows the depth |
| The page is up, the canvas is empty | Nothing indexed yet | Add a source, then watch it on the **Syncs** page |
| `port is already allocated` | 8765 in use | Set `PHEASANT_PORT` in `.env` |
