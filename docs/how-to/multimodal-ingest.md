# How to ingest images and audio

pheasant indexes images by **captioning** them and audio by **transcribing**
it into text that flows through the normal chunk → embed → graph path. For a
hands-on walkthrough, see the [multi-modal tutorial](../tutorials/multimodal.md);
this page is the configuration reference.

## Turn it on

Captioning/transcription is **opt-in by source include**. The captioner is only
built when a source's `include` globs admit an image extension; the transcriber
only when they admit an audio extension. `pheasant setup` and `pheasant up` use
`**/*` for mixed folders, which admits both. A text-only config builds neither
and is byte-identical to a pheasant without multi-modal.

| Modality | Extensions | Built when `include` admits | Producer |
|---|---|---|---|
| Image | `.png` `.jpg` `.jpeg` `.webp` `.gif` | any image extension | captioner |
| Audio | `.wav` `.mp3` `.m4a` `.flac` `.ogg` | any audio extension | transcriber |

```yaml
sources:
  - name: assets
    type: repository
    path: /workspace
    include:
      - "**/*.png"     # builds the captioner
      - "**/*.wav"     # builds the transcriber
```

## Three sources of text (in priority order)

For each media file, pheasant picks the caption/transcript text like this:

1. **Sidecar file** — if `<file>.caption.txt` (image) or
   `<file>.transcript.txt` (audio) exists next to the media file, its contents
   are used **verbatim**. The sidecar always wins, even over a network provider.
2. **Configured provider** — otherwise the configured `provider` runs.
3. **Stub fallback** — the `stub` provider (the default) produces a
   deterministic placeholder from the file name + a stable content fingerprint.

Sidecars are the offline way to get accurate text — author them by hand or
generate them out-of-band.

## Providers

Configure providers under `ingestion.captioner` and `ingestion.transcriber`.
Both default to `stub` (offline, deterministic, network-free).

### Image captioner — `ingestion.captioner`

```yaml
ingestion:
  captioner:
    provider: stub                 # "stub" | "openai-spec"
    model: gpt-4o-mini             # used by openai-spec
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY    # env var NAME; key never lands in config
    prompt: "Describe this image in one concise sentence for search indexing."
```

- `stub` — `StubCaptioner`. Default. No network. Caption =
  `Image <name> (visual content, fingerprint <digest>).`
- `openai-spec` — `OpenAISpecVisionCaptioner`. Calls
  `POST {base_url}/chat/completions` with an `image_url` content part and reads
  `choices[0].message.content`. Works against any OpenAI-spec vision endpoint,
  including self-hosted (set `base_url`).

### Audio transcriber — `ingestion.transcriber`

```yaml
ingestion:
  transcriber:
    provider: stub                 # "stub" | "openai-spec"
    model: whisper-1               # used by openai-spec
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY    # env var NAME; key never lands in config
```

- `stub` — `StubTranscriber`. Default. No network, **no audio decoder, no ASR
  model, no audio library**. Transcript =
  `Audio <name> (spoken content, fingerprint <digest>).`
- `openai-spec` — `OpenAISpecTranscriber`. Uploads the raw bytes via
  `POST {base_url}/audio/transcriptions` (multipart) and reads the response
  `text`. Works against any OpenAI-spec transcription endpoint.

!!! warning "Network calls in the indexing path"
    Captioning/transcription with `openai-spec` is one of the few sanctioned
    network calls at sync time (the others are the optional embedder). Keep the
    `stub` provider for tests and air-gapped runs — it is the default precisely
    so the offline path stays network-free.

## When to use which

| Situation | Use |
|---|---|
| Tests, CI, offline / air-gapped, "just make it discoverable" | `stub` (default) |
| You have exact text already (alt text, meeting notes) | author a **sidecar** |
| You want real captions/transcripts at scale | `openai-spec` + a hosted or self-hosted endpoint |

## Images your documents reference

A caption makes an image *searchable*; a link makes it *showable*. When a
Markdown or HTML document references an image the same region indexes —
`![Architecture](img/arch.png)`, Obsidian's `![[flow.png|300]]` (the `|300` is
a size, not alt text), or `<img src="…" alt="…">` — the link becomes an
`embeds` edge from the document to the image artifact
([graph model](../graph_model.md#edge-types)). The path is tried relative to the
document first and by path suffix otherwise, preferring the document's own
source. Remote (`https://…`) and `data:` images are not in the corpus and stay
ordinary references.

The indexer also stores each image's bytes, content-addressed, under
`<state_path>/media/` (at most 8 MB per image; larger images are still indexed
and captioned, just not shown). That is what lets a serving replica with no
source mount — or a Drive image with no path at all — still show the
picture. `GET /media?node_id=…` and the MCP `get_image` tool serve them under
the same read check as every content operation; SVG is never ingested or
served, because it can carry script. An image indexed before the store existed
is served from its source path when that is still readable here, and a
`full` sync fills the store.

An answer citing the document then carries the image as a numbered figure —
see [answer length, conversations, visuals and figures](conversations-and-visuals.md#figures-images-your-documents-reference).

Images *embedded inside* PDF, DOCX and PPTX files are not extracted yet: each
would need its own artifact id, and the id grammar is a contract
([graph model](../graph_model.md#stable-ids)) that changes only with a
migration and a decision note.

## Idempotency

An unchanged image or audio file is skipped by content `sha256` **before it is
read**, so it is never re-captioned or re-transcribed on an incremental sync.
Change the media file (or its sidecar) to force reprocessing.

## How the fleet sees it

When an image source is configured, this region's published Synapse contract
adds `"image"` to `capabilities.modalities`; an audio source adds `"audio"`.
A Synapse router can then route modality-filtered queries
(`--modality image` / `--modality audio`) only to regions that advertise them.
This is existing contract data — no schema change. See
[Attach to a Synapse fleet](attach-to-synapse.md).
