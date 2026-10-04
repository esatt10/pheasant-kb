# How to attach pheasant to a Synapse fleet

A standalone pheasant is a complete knowledge base. This guide shows how to also
make it a **federated region** in a **Synapse** fleet: it publishes a bounded
**semantic contract** describing what it knows, and a Synapse **router** uses
that contract to route global, cross-region queries to it.

!!! tip "Standalone-first is the default"
    Every setting on this page is **off by default**. With no `synapse:` block,
    pheasant behaves exactly like a router-less knowledge base. Opting in is
    purely additive and never changes how the region indexes or self-searches.

## How it fits together

```text
                  ┌──────────────────────────┐
   global query → │  Synapse router          │  (pheasant-flock repo)
                  │  scores contracts,       │
                  │  routes + fans out       │
                  └───────────┬──────────────┘
                              │ fan-out (HTTP)
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
   ┌─────────┐          ┌─────────┐          ┌─────────┐
   │pheasant │          │pheasant │          │pheasant │   ← regions (this repo)
   │ region  │          │ region  │          │ region  │
   │ contract│          │ contract│          │ contract│
   └─────────┘          └─────────┘          └─────────┘
```

The boundary is **contract JSON over HTTP** — there is no Python dependency
between pheasant and the router.

## Step 1 — opt in

Add a `synapse:` block to `pheasant.yaml`. All keys default to off/`null`:

```yaml
synapse:
  publish: true                          # gate contract publication + event stream
  router_url: http://synapse-router:8000 # the router's base URL
  fleet_id: my-fleet                     # fleet label stamped into the contract
  endpoint: http://my-region:8765        # this region's reachable base URL
  webhook_timeout_seconds: 5.0
```

| Key | Default | Meaning |
|---|---|---|
| `publish` | `false` | Master switch: publish a contract + emit the event stream after each sync. |
| `router_url` | `null` | Where to POST `sync.completed` events. If unset, the contract is still written locally but nothing is pushed. |
| `fleet_id` | `null` | Fleet label written into the contract. |
| `endpoint` | `null` | This region's reachable base URL, so the router can pull `GET /contract` and fan out queries here. |
| `webhook_timeout_seconds` | `5.0` | Timeout for the router webhook. |
| `signing_key_ref` | `null` | Optional Ed25519 signing key reference (see [step 5](#step-5-optional-sign-the-contract)). |

## Step 2 — the contract auto-publishes

With `synapse.publish: true`, **each successful indexing sync**:

1. (Re)writes the contract to `<state>/contract.latest.json`.
2. Appends a `sync.completed` record to `<state>/events/YYYY-MM-DD.ndjson`.
3. If `router_url` is set, POSTs the event (with the inline contract) to
   `<router_url>/v1/synapse/events`. **Webhook failures are logged, never
   raised** — a sync never fails because the router is down; the region simply
   ages toward staleness until the next push or pull.

The contract is a bounded JSON projection of this region's content: an
embedding-space signature (cluster centroids), a concept vocabulary with a
MinHash signature, capabilities, and a freshness watermark. It is **derived
deterministically** from your index — no LLM call.

## Step 3 — inspect the published contract

The contract is served read-only at `GET /contract`:

```bash
curl http://localhost:8765/contract | head -c 400
```

It's also available as an MCP resource
(`pheasant://knowledge-bases/{kb_id}/contract`) and via the
`get_contract` MCP tool.

Confirm `capabilities.modalities` reflects what you index — it auto-includes
`"image"` when an image source is configured and `"audio"` when an audio source
is configured (see [Multi-modal ingest](multimodal-ingest.md)), so a router can
route `--modality image` / `--modality audio` queries to you.

`capabilities.source_types` lists the *kinds* of source this region is built
from — `repository`, `gdrive`, `web_collection` and so on. Where
`modalities` says what media the region can answer about, this says where its
content came from, which is the question a fleet operator actually asks ("who
has our design docs?"). It is derived from the enabled sources, so it stays
correct as sources are added and removed.

## Step 4 — agree on the embedding space

The fleet compares contracts in one embedding space, so every region must use
the **same embedding model**. Pin one `model`/`dimensions` across the fleet (see
[Vector self-search](vector-search.md) → "The fleet-pinned model"). A region
whose embedding space doesn't match is rejected at the router.

## Step 5 (optional) — sign the contract

To let the router verify a contract's authenticity, sign it with an Ed25519 key.

1. Install the signing extra:

    ```bash
    pip install 'pheasant-kb[a2a]'
    ```

2. Provide the key as a **secret reference** — never the key itself in config:

    ```yaml
    synapse:
      publish: true
      router_url: http://synapse-router:8000
      signing_key_ref: env://SYNAPSE_SIGNING_KEY   # or a bare env var name
    ```

    `signing_key_ref` resolves to a base64-encoded 32-byte Ed25519 seed read
    from the environment at runtime. The plaintext key **never lands in config
    or on disk**.

When set, the contract's `integrity.signature` is filled with the Ed25519
signature; when unset it stays `null` and the region is unsigned (and a
standalone pheasant is unchanged). The signing key's **public key** is
distributed to the router **out-of-band** (it lives in the router's trust store
config), so the contract wire format is unchanged.

## Step 6 — query the fleet

Routing, fan-out, merge, and the global search experience live on the **router**
side. Once your region is publishing and reachable, head to the
[pheasant-flock documentation site](https://github.com/esatt10/pheasant-flock)
to register the region and run global queries across the fleet.

## Verifying the attach

- [x] `GET /contract` returns a JSON contract.
- [x] After a sync, `<state>/contract.latest.json` is fresh and a
      `sync.completed` line appears in `<state>/events/<date>.ndjson`.
- [x] With `router_url` set, the router shows the region registered (check the
      router's `GET /v1/synapse/kbs`).
- [x] `capabilities.modalities` lists `image`/`audio` if you index them.
- [x] `capabilities.source_types` lists the connectors this region indexes.
- [x] If signing, the router accepts the contract under its trust store.

## Standalone is never harmed

Set `synapse.publish: false` (or omit the block) and **all** of the above
no-ops: no contract is pushed, no event webhook fires, and the region indexes
and self-searches exactly as before. You can join and leave a fleet without
re-indexing.
