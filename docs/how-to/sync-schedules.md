# Sync schedules, and removing content

Every source has a **sync pipeline**, made when the source is added. The
**Syncs** page is where you see what each one did and change when it runs.

## When a source syncs

A source syncs when any of these happens:

| Trigger | Notes |
|---|---|
| Its **schedule** comes round | Set per source, or by the region's default (below). |
| A watched file changes, or a repository gets a commit | Folders and repositories, where watching is on. |
| Somebody presses **Run now** (or **Run all now**) | Or `sync_source` / `POST /sync/{source}`. |
| The process starts | Only with `sync.scheduler.startup_sync` on (below). |

## Pick a schedule

On the **Syncs** page, select a source, then **Settings → Runs**:

| Choice | Means |
|---|---|
| Region default (every hour) | Follow the **Default schedule** at the top of the page. |
| Every 15 minutes … Every week | This source's own cadence. |
| Manual — only when asked | Only **Run now** or `sync_source`; no schedule, file change or commit starts it. |

**Wait while other sources sync** holds this source while others are syncing or
queued, so it never delays them (four intervals at most). The artifacts source
uses it.

The **Default schedule** at the top of the page sets the cadence for every
source left on "Region default". It is held by the region, so it reaches every
replica at once.

The same settings over the API and MCP:

```bash
# one source: daily
curl -X PATCH "$URL/sync/pipelines/netsuite-docs" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"min_interval_seconds": 86400}'

# back to the region default
curl -X PATCH ... -d '{"min_interval_seconds": null}'

# the region default: hourly
curl -X PUT "$URL/sync/pipelines/defaults" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"default_source_interval_seconds": 3600}'
```

MCP: `configure_sync_pipeline(source, {"min_interval_seconds": 86400})` and
`configure_sync_defaults(3600)`. `list_sync_pipelines` reports each source's
cadence (`schedule.interval_seconds`, and `interval_from`: `source`,
`region default` or `every beat`) and the region's under `defaults`.

### How the schedule runs

The scheduler checks every `sync.scheduler.interval_seconds` (15 minutes),
which is the finest cadence a source can have. Each check syncs the sources
that are due: a source last synced within its cadence waits. A web collection
also re-checks each of its pages on that page's own schedule (from an hour to
days, by how often the page changes); the source's cadence is how often the
collection is re-listed.

### Startup syncs

With `sync.scheduler.startup_sync: true` (the single-container default), each
source syncs when the process starts. A region that scales to zero should turn
it off -- every wake would otherwise re-sync everything -- and let the schedule
do the work:

```yaml
sync:
  scheduler:
    default_source_interval_seconds: 3600   # each source hourly
    startup_sync: false                     # nothing at boot
```

The Azure Container Apps example sets exactly this.

## Artifacts

The `artifacts` source (what pheasant [keeps of what it generates](artifacts.md))
is indexed as soon as something is saved, by default, with its own schedule as
a backstop. Both are in the Artifacts panel's settings.

## Remove documents and folders

To take content out of the index and keep it out of later syncs:

| To remove | Do |
|---|---|
| One file | Open it in the taxonomy view, **Remove**; or `archive_documents(documents=["<source>/<path>"])`, `POST /documents/archive`. |
| A whole folder | Open the folder in the taxonomy view, **Remove folder**; or `archive_documents(folders=["<source>/<path>"])`, `POST /documents/archive` with `folders`. |
| A whole source | **Sources → Remove**, `remove_source`. |

Both buttons ask twice. A file from a folder, repository or connector source
is *archived*: it stays where it is, every later sync skips it, and the
taxonomy view's **Removed** list (or `reinstate_documents`, with `documents` or
`folders`) lets it back. An uploaded file is deleted. A folder removal takes up
to 5,000 documents at a time, and an `artifacts` folder retires the artifacts
in it.

## Related

- [Monitor indexing](monitor-indexing.md): progress, metrics, stalls.
- [Configuration](../configuration.md#scheduler-policy-syncscheduler)
- [Configure sources](sources.md)
