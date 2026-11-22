# Troubleshooting

## Health checks fail

- Confirm the container is running and port `8765` is exposed.
- Check `PHEASANT_CONFIG` points to `/config/pheasant.yaml`.
- Confirm `/state` is writable.

## Image pull fails

- Confirm the image exists at the semver tag from `pyproject.toml`.
- Check the GitHub Actions container publish workflow completed successfully.
- If the package is private, authenticate Docker with a token that can read GitHub Packages or make the package public in GitHub Packages.

## Sources are not indexed

- Verify source paths exist inside the container, not just on the host.
- Confirm paths resolve under allowlisted workspace roots.
- Check include/exclude patterns are not filtering everything.
- Inspect sync status and event logs for parser or permission errors.

## Watcher events are missed

Docker mount watcher behavior differs across macOS, Windows/WSL2, and Linux. Keep scheduled fallback sync enabled and use explicit `sync_source` after agent commits.

## Obsidian vault is too noisy

Disable chunk notes, keep file notes concise, and store large graph exports under `/state` or `/exports` instead of the vault.

## State appears stale or inconsistent

Validate, then repair or re-sync. There is no `rebuild` command — use `repair`
(rebuilds missing/invalid state from manifests + DB) or a `full` sync (rebuilds
everything for a source):

```bash
pheasant validate pheasant.yaml
pheasant repair --config pheasant.yaml
pheasant sync --config pheasant.yaml --source <source> --mode repair
pheasant sync --config pheasant.yaml --source <source> --mode full
pheasant sync --config pheasant.yaml --all --mode full
```

`validate_only` mode checks readability without writing anything, which is handy
for diagnosing a source before a heavier rebuild:

```bash
pheasant sync --config pheasant.yaml --source <source> --mode validate_only
```

## Images are not indexed

- The captioner is only built when a source's `include` globs admit an image
  extension (`.png` `.jpg` `.jpeg` `.webp` `.gif`). Add the glob, e.g.
  `"**/*.png"`, then re-sync.
- An unchanged image is skipped by content `sha256` on incremental sync — that
  is expected (it was already captioned). Use `--mode full` to force.
- See [Multi-modal ingest](how-to/multimodal-ingest.md).

## Audio is not transcribed

- The transcriber is only built when a source's `include` globs admit an audio
  extension (`.wav` `.mp3` `.m4a` `.flac` `.ogg`). Add the glob, then re-sync.
- An unchanged audio file is skipped by `sha256` on incremental sync — expected.
  Use `--mode full` to force re-transcription.
- The default `stub` transcriber needs no model; to use a real one, set
  `ingestion.transcriber.provider: openai-spec`. See
  [Multi-modal ingest](how-to/multimodal-ingest.md).

## Embedding requests fail

- Vector search is off unless `search.embeddings.enabled: true`. With it off,
  text/graph search still works.
- For `provider: openai-spec`, confirm the env var named by `api_key_env`
  (default `OPENAI_API_KEY`) is set in the container/process, and that
  `base_url` is reachable.
- For air-gapped or test runs, set `provider: stub` (deterministic, offline).
- If `vector_store.provider: lancedb` errors, install the extra
  (`pip install 'pheasant-kb[vector]'`) or switch to `provider: numpy`.
- See [Vector self-search](how-to/vector-search.md).

## Multiple instances conflict

Do not point multiple independent pheasant containers at the same writable `/state` volume. Use one namespace/PVC per instance.
