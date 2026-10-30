# Use Azure AI Foundry models

pheasant-kb calls OpenAI directly by default. Every model call it makes can go
to an **Azure AI Foundry** resource instead: answers, evidence judgements,
embeddings, image captions and audio transcripts. The same setup works for
OpenAI models deployed on Foundry and for Microsoft's own models.

This is an opt-in overlay. `compose.yaml` and
`deploy/compose/pheasant-kb.yaml` are not changed. You add four lines to `.env`
and keep a config file of your own, and removing those lines switches back.

## What goes where

| pheasant calls | Setting | Foundry endpoint | Deploy |
|---|---|---|---|
| Answers and query planning | `assistant` (`provider: azure`) | `{resource}/openai/v1/chat/completions` | A chat model: `gpt-6-luna`, or a Foundry model such as MAI-Thinking-1 |
| Evidence and answer judgements | `assistant.decisions` | `{resource}/openai/v1/decisions` (`api: decisions`) or `{resource}/providers/microsoft/v1/systemone` (`api: systemone`) | `gpt-6-luna`, or Microsoft-Decision-1 |
| Embeddings | `search.embeddings` | `{resource}/openai/v1/embeddings` | `text-embedding-3-small` |
| Image captions | `ingestion.captioner` | `{resource}/openai/v1/chat/completions` | A vision-capable chat model (`gpt-6-luna`) |
| Audio transcripts | `ingestion.transcriber` | `{resource}/openai/v1/audio/transcriptions` | `whisper` (or `gpt-4o-transcribe`) |

Two things are the same in every row:

- **`model` is a deployment name.** Foundry names a deployment after its model
  unless you choose another name. The example config assumes you kept the
  default names.
- **The key is sent as `api-key`.** pheasant does this for any URL on
  `*.services.ai.azure.com`, `*.openai.azure.com` or
  `*.cognitiveservices.azure.com`. Every other URL keeps
  `Authorization: Bearer`, so an OpenAI setup behaves as it did before.

## 1. Prepare the Foundry resource

1. In the [Foundry portal](https://ai.azure.com), create or choose a Foundry
   resource. Its endpoint is `https://<resource>.services.ai.azure.com`.
2. Deploy the models from the table above, keeping their default deployment
   names: `gpt-6-luna`, `text-embedding-3-small` and `whisper`. Skip `whisper`
   if your sources hold no audio, and set
   `ingestion.transcriber.provider: stub` instead.
3. To use Microsoft-Decision-1 for judgements, deploy it too. Azure refuses
   deployment names that contain "Microsoft", so call it something like
   `ms-decision-1`.
4. Copy a key from the resource's **Keys and Endpoint** page.

New subscriptions often start with no quota for these models, so request quota
before you deploy. The Decisions API on `gpt-6-luna` is not available in every
region; gateway docs report it missing in East US 2. Check your region's model
availability.

## 2. `.env`

Add these lines to the `.env` beside `compose.yaml`:

```bash
# Azure AI Foundry instead of api.openai.com — docs/pheasant-kb/foundry.md
AZURE_FOUNDRY_API_KEY=<key from Keys and Endpoint>
PHEASANT_KB_CONFIG=./pheasant.yaml
COMPOSE_FILE=compose.yaml:deploy/compose/foundry/compose.foundry.yaml
# compose.yaml requires a value here. The Foundry override clears it in every
# container, so no tier holds an OpenAI key.
OPENAI_API_KEY=unused
```

| Line | Why |
|---|---|
| `AZURE_FOUNDRY_API_KEY` | The resource key. It lives only in `.env` and in the environment of the tiers that call a model. It is never written to the config or to `/state`. |
| `PHEASANT_KB_CONFIG` | Points every tier at your config file (step 3) instead of the shipped one. |
| `COMPOSE_FILE` | Adds [`compose.foundry.yaml`](https://github.com/esatt10/pheasant-kb/blob/main/deploy/compose/foundry/compose.foundry.yaml) on top of `compose.yaml`. It passes the Foundry key to `api`, `graph`, `indexer`, `logger` and `db-init`, and blanks `OPENAI_API_KEY` there. It does not touch the worker tier, which calls no model and refuses to start holding a model key. On Windows, separate the two files with `;` instead of `:`. |
| `OPENAI_API_KEY=unused` | `compose.yaml` stops if this is unset. Compose checks the base file before it applies the override, so `.env` still needs a placeholder. No container receives it. |

If you already use your own `compose.override.yaml`, list it as well:
`COMPOSE_FILE=compose.yaml:compose.override.yaml:deploy/compose/foundry/compose.foundry.yaml`.
Once `COMPOSE_FILE` is set, Compose stops loading `compose.override.yaml` on
its own.

## 3. The config file

```bash
cp deploy/compose/foundry/pheasant-kb.foundry.yaml pheasant.yaml   # gitignored
sed -i 's/YOUR-RESOURCE/contoso-ai/g' pheasant.yaml                # your resource name
```

The file only holds what differs from pheasant-kb. Everything else
(Postgres, NATS, retrieval, memory, security) comes from pheasant-kb's
defaults, because the file names `defaults: flagship`. This is the part that
matters:

```yaml
search:
  embeddings:
    base_url: https://contoso-ai.services.ai.azure.com/openai/v1
    api_key_env: AZURE_FOUNDRY_API_KEY
    model: text-embedding-3-small      # deployment name
    dimensions: 1536
ingestion:
  captioner:   {base_url: …, api_key_env: AZURE_FOUNDRY_API_KEY, model: gpt-6-luna}
  transcriber: {base_url: …, api_key_env: AZURE_FOUNDRY_API_KEY, model: whisper}
assistant:
  provider: azure
  base_url: https://contoso-ai.services.ai.azure.com/openai/v1
  api_key_env: AZURE_FOUNDRY_API_KEY
  model: gpt-6-luna                    # deployment name
  retrieval:
    grader_model: gpt-6-luna
  decisions:
    api: decisions                     # or systemone (Microsoft-Decision-1), below
    model: gpt-6-luna
```

`base_url` accepts the resource root, a project endpoint
(`…/api/projects/<name>`) or the `…/openai/v1` form. pheasant reads any URL on
a Foundry host as its OpenAI v1 surface. `assistant.decisions` uses the
assistant's resource and key unless you set its own `base_url` and
`api_key_env`.

Check the file before you start the stack:

```bash
docker compose run --rm --no-deps api validate /config/pheasant.yaml
docker compose run --rm --no-deps api config show -c /config/pheasant.yaml | grep -n "openai.com"   # expect no output
```

## 4. Start it

```bash
docker compose up -d                      # first start
docker compose up -d --force-recreate     # if the stack was already running
```

`GET /assistant/status` should then report `"configured_provider": "azure"`
and `"ready": true`. Ask a question in the UI: its trace names the provider and
the deployment, and the `decisions` block names the `api` that judged. A wrong
key or deployment name appears there and in `docker compose logs api indexer`
as `401` or `404 DeploymentNotFound`. A `404` from the judgement endpoint stops
further judgement calls until the process restarts, and the loop falls back to
model-written judgements.

### Switching a running pheasant-kb over

No re-index is needed if the embedding deployment is the same model with the
same `dimensions` (`text-embedding-3-small`, 1536). The provider URL is not
part of what decides whether a chunk needs a new vector. A different embedding
model or dimension re-embeds every chunk on the next sync, as it would with
OpenAI.

## Microsoft's own models

### Answers from a Foundry model

Set `assistant.model` and `assistant.retrieval.grader_model` to the deployment,
for example `mai-thinking-1`. Anything Foundry serves on
`/openai/v1/chat/completions` works.

A few optimisations are tuned to GPT-6 Luna and apply only to a deployment
named `gpt-6-luna`: low-effort first drafts, effort levels for rewrites, and
streamed draft previews. Any other model writes at its own defaults. pheasant
still asks it for JSON where it needs JSON, and drops that request if the model
rejects it. A model that spends its output budget on reasoning before it
writes gets extra room automatically, as it does on any provider.

### Judgements from Microsoft-Decision-1

```yaml
assistant:
  decisions:
    api: systemone
    model: ms-decision-1        # your deployment's name
```

Microsoft-Decision-1 does not use OpenAI's Decisions API. It answers
**System One** questions (`noul`, `choice`, `score`) about a `state`, at
`{resource}/providers/microsoft/v1/systemone`. pheasant translates its
questions to that format and the answers back. The gates do not change, and
`assess_evidence` over MCP and HTTP uses the same path.

**Recalibrate the thresholds before relying on it.** The shipped
`sufficient_threshold` (0.25) and `accept_threshold` (0.55) were set from
GPT-6 Luna's probabilities on SciFact's expert labels. A different model's
probabilities sit on a different scale. Run the same calibration against your
deployment and copy the thresholds it prints into the config:

```bash
python scripts/calibrate_decisions.py --api systemone \
  --base-url https://contoso-ai.services.ai.azure.com \
  --model ms-decision-1 --api-key-env AZURE_FOUNDRY_API_KEY \
  --scifact .cache/scifact --out calibration.json
```

To compare the two models before switching, run with
`assistant.decisions.mode: shadow` for a while. Shadow mode asks for judgements
and records whether they agree with the model-written ones
(`pheasant_assistant_decision_shadow_total`), but acts on none of them.

### Embeddings

This guide found no Microsoft (MAI) embedding model on Foundry's OpenAI
surface, so the example keeps `text-embedding-3-small` deployed on Foundry. Any embedding deployment that
answers `/openai/v1/embeddings` works. A different model means re-embedding
(above).

## Limits

- **Keys only.** Microsoft Entra ID tokens and managed identity are not wired
  in. A token expires within the hour and would need a refresh loop that
  pheasant does not have.
- **Setup wizard.** `pheasant setup` does not offer Foundry. Use the config
  file.
- **UI-pasted keys.** pheasant-kb turns these off (`allow_session_keys:
  false`). Where they are on, a pasted Foundry key goes to the resource in
  `assistant.base_url`.
- **Not tested against a live resource.** The request formats follow
  Microsoft's Foundry documentation and an open-source gateway that serves
  both judgement endpoints. Run step 4's checks and a few questions before you
  depend on this.

## Other deployment shapes

The single container and the generic fleet read the same config sections. Set
the four model sections as in step 3 and pass `AZURE_FOUNDRY_API_KEY` to the
process. In a one-container setup that means
`docker run -e AZURE_FOUNDRY_API_KEY=…` or the `.env` its compose file reads.

## Switch back

Remove the four lines from `.env`, put your real `OPENAI_API_KEY` back, and run
`docker compose up -d --force-recreate`.
