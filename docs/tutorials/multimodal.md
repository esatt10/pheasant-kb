# Multi-modal ingest: index images and audio (offline)

pheasant can ingest **images** and **audio** alongside text. It does this by
turning each non-text file into searchable text:

- **Images** (`.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`) are **captioned**.
- **Audio** (`.wav`, `.mp3`, `.m4a`, `.flac`, `.ogg`) is **transcribed**.

The caption/transcript text then flows through the **normal
chunk → embed → graph** path, so it shows up in ordinary search results. The
file becomes an `image` or `audio` artifact node in the graph.

This tutorial uses the **offline stub** captioner and transcriber (the
defaults), so it needs no API keys and no model downloads. Everything is
copy-pasteable.

> [!NOTE]
> **Two ways to get good text, both offline**
>
> 1. **Stub captioner/transcriber (default):** produces a deterministic
>    placeholder caption/transcript derived from the file name plus a stable
>    content fingerprint. Good enough to make files discoverable and to
>    exercise the pipeline; the same file always yields the same text.
> 2. **Authored sidecar (recommended for real content):** drop a
>    `<file>.caption.txt` or `<file>.transcript.txt` next to the media file.
>    pheasant uses its contents **verbatim**, and the sidecar wins over both
>    the stub and any network captioner. This is the offline way to get
>    accurate text without calling a model.

## 1. Add a media folder to your config

Start from the [quickstart](quickstart.md) `pheasant.yaml`. Create a folder with
one image and one audio file:

```bash
mkdir -p ./kb-demo/media
# any small files work; the stub captioner/transcriber never decodes them
printf 'PNGDATA' > ./kb-demo/media/diagram.png
printf 'WAVDATA' > ./kb-demo/media/briefing.wav
```

Add `include` globs that admit the media extensions. **This is the switch** that
turns the feature on: the captioner is only built when a source's `include`
globs admit an image extension, and the transcriber only when they admit an
audio extension. A text-only config never builds either.

```yaml
sources:
  - name: kb-demo
    type: repository
    path: ./kb-demo
    enabled: true
    include:
      - "**/*.md"
      - "**/*.py"
      - "**/*.png"        # (1)
      - "**/*.jpg"
      - "**/*.wav"        # (2)
    chunking:
      enabled: true
      strategy: semantic
    sync:
      on_startup: true
```

1. Admitting an image extension builds the **captioner**.
2. Admitting an audio extension builds the **transcriber**.

You do **not** need to add any `ingestion:` block — the stub provider is the
default. (To customize the provider, see
[Multi-modal ingest (how-to)](../how-to/multimodal-ingest.md).)

## 2. (Optional but recommended) author sidecars

Make the media genuinely findable by authoring the exact text you want indexed:

```bash
printf 'Architecture diagram: billing service talks to the ledger and the payments gateway.\n' \
  > ./kb-demo/media/diagram.png.caption.txt

printf 'Quarterly briefing: the payments team is migrating Atlas billing to the new ledger.\n' \
  > ./kb-demo/media/briefing.wav.transcript.txt
```

The sidecar filename is the media filename **plus** the suffix:

| Media file | Sidecar file |
|---|---|
| `diagram.png` | `diagram.png.caption.txt` |
| `briefing.wav` | `briefing.wav.transcript.txt` |

## 3. Sync

```bash
pheasant sync --config pheasant.yaml --source kb-demo --mode incremental
```

Expected output (counts vary):

```text
Sync kb-demo (incremental): 4 artifacts, 5 chunks indexed, 0 skipped
```

The image and audio files are now `image` / `audio` artifacts whose chunk text
is the caption / transcript.

> [!TIP]
> **Idempotent — never re-captions or re-transcribes unchanged media**
>
> On the next incremental sync, an unchanged image or audio file is skipped by
> content `sha256` **before it is read**, so it is never captioned or
> transcribed again (zero work, same guarantee as the embedder). Change the
> file (or its sidecar) and it is re-processed.

```bash
pheasant sync --config pheasant.yaml --source kb-demo --mode incremental
```

```text
Sync kb-demo (incremental): 0 artifacts, 0 chunks indexed, 4 skipped
```

## 4. Search and surface them

Start the server and search for content that only lives in the media files:

```bash
pheasant start --config pheasant.yaml &
curl -X POST http://localhost:8765/search \
  -H "content-type: application/json" \
  -d '{"query": "payments gateway diagram", "mode": "hybrid", "max_results": 5}'
```

Expected — the image artifact surfaces from its caption text:

```json
{
  "query": "payments gateway diagram",
  "mode": "hybrid",
  "results": [
    {"node_id": "chunk:kb-demo:media/diagram.png:0", "score": 0.88,
     "snippet": "Architecture diagram: billing service talks to the ledger and the payments gateway."}
  ]
}
```

If you authored sidecars, the snippet is your authored text. If you used the
stub default, the snippet is the deterministic placeholder
(`Image diagram.png (visual content, fingerprint …).`).

## What just happened

- Adding image/audio `include` globs **opted the source in** to captioning /
  transcription.
- The offline **stub** produced deterministic text — no network, no model.
- Where present, **sidecars** overrode the stub with your exact text.
- The caption/transcript flowed through the normal chunk/graph path and became
  searchable.

## Going to production

To caption/transcribe with a real model (OpenAI-spec vision model or a Whisper
endpoint, including self-hosted), see
[Multi-modal ingest (how-to)](../how-to/multimodal-ingest.md). The stub stays
the default so your tests and offline runs never touch the network.
