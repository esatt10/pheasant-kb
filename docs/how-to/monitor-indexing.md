# Watch an index, and scrape it

A first index of a large collection takes minutes to hours. The question during
that time is always the same — *is this working, or is it stuck?* — and until
Phase 35.1 pheasant could not answer it: a source row said `syncing: true` and
nothing else, and `GET /metrics` returned the literal string `pheasant_up 1`.

Two surfaces answer it now: per-source progress (for a human) and Prometheus
metrics (for a scheduler).

## Per-source progress

Every source-listing route (`GET /sources`, `GET /overview`) carries a
`progress` object for any source currently being indexed:

```jsonc
{
  "source": "handbook",
  "phase": "preparing",
  "current": 4120,
  "total": 12667,          // null until the connector finishes listing
  "fraction": 0.325,
  "indexed": 4118,
  "skipped": 2,
  "bytes_done": 51221904,
  "files_per_second": 18.4,
  "eta_seconds": 462.3,
  "seconds_since_progress": 0.9,
  "stalled": false,
  "phase_seconds": {"listing": 12.1, "preparing": 224.0},
  "failures": []
}
```

In a role-split fleet, indexers write these job snapshots atomically under
`/state/jobs` and API replicas read them from the shared state mount. This is
why the Jobs tray and Sources rows continue to show live progress even though
the process serving the UI never performs indexing itself.

The API mount remains read-only. When a user clears a finished notification,
the API clears its local cards and asks the authenticated indexer to remove the
persisted snapshot; active work is never cancelled.

`GET /jobs` carries the same records under each job's `sources`, plus a
job-level `progress` rollup.

**Throughput and ETA are observed, not reported.** The server times the updates
it receives and derives the rate over a sliding window, so they exist even for
callers that emit neither, and the ETA reacts when a pass genuinely slows down
rather than quoting an average from the fast early minutes.

**`total` is `null` until the connector has finished listing.** A sync does not
know how many files it will index until then, and a made-up denominator is
worse than none — the UI renders an indeterminate bar for this period rather
than one frozen at 0%, which reads as "stuck".

### Slow is not stuck

`seconds_since_progress` is always present, so a client can say "last update 4s
ago" during healthy work. `stalled` only becomes true after five minutes of
silence from a running source. The window is deliberately generous: one large
PDF, or an embedding provider serving a retry with backoff, is slow but fine,
and an indicator that cries wolf gets ignored.

A stalled source is styled as a **warning**, not a failure — the pass may still
recover.

### What "unchanged" tells you

On an incremental pass, `skipped` is often the number that matters:
`2,996 unchanged · 4 indexed` is the difference between "working correctly" and
"re-indexing everything again", and nothing in the UI used to say which.

!!! note "Per-file failures abort the pass today"

    `failed` and `failures` are reported but currently always empty: a file
    that fails to prepare raises and ends the whole sync, which surfaces as the
    job's `error` instead. Per-item fault tolerance and a dead-letter queue are
    Phase 35.4; the fields are here because that is where the counts will land.

## Metrics

`GET /metrics` serves Prometheus exposition text (`text/plain; version=0.0.4`).
No extra dependency and no configuration — it is always on.

| Metric | Type | Use |
|---|---|---|
| `pheasant_index_queue_depth` | gauge | Sources still queued in the durable source queue. |
| `pheasant_index_preparation_backlog` | gauge | Files still awaiting preparation in active jobs; the worker autoscaling signal after a source is claimed. |
| `pheasant_index_inflight` | gauge | Index jobs currently running. |
| `pheasant_indexer_leader` | gauge | 1 on the elected orchestrator, 0 on hot-standby indexers. |
| `pheasant_index_progress_ratio{source}` | gauge | 0–1 completion of the current pass. |
| `pheasant_index_files_per_second{source}` | gauge | Observed throughput. |
| `pheasant_index_eta_seconds{source}` | gauge | Estimated seconds remaining. |
| `pheasant_index_stalled{source}` | gauge | 1 when a running source has gone quiet. |
| `pheasant_index_files_total{source,outcome}` | counter | Files resolved, by outcome. |
| `pheasant_index_bytes_total{source}` | counter | Bytes read. |
| `pheasant_sync_last_success_timestamp_seconds{source}` | gauge | Freshness; alert on age. |
| `pheasant_search_duration_seconds{mode}` | histogram | Query latency. |
| `pheasant_search_total{mode,outcome}` | counter | Query volume and errors. |
| `pheasant_embedding_requests_total{outcome}` | counter | Provider health. |
| `pheasant_graph_nodes`, `pheasant_graph_edges` | gauge | Graph size — the RAM driver. |
| `pheasant_memory_records{scope,tier}` | gauge | Live memory records, per scope and tier — a `tier="cold"` count rising is compaction working. |
| `pheasant_memory_writes_total{outcome}` | counter | `memory_write` calls: `created`, `reinforced`, or `duplicate`. |
| `pheasant_memory_l0_folds_total{kind}` | counter | Writes folded by L0, by `kind`: `exact` (byte-identical, the dedup that predates reinforcement) or `normalized` (a paraphrase matched). |
| `pheasant_memory_reinforcement_ratio` | gauge | Of the writes that either created a record or were folded as a **paraphrase**, the fraction folded — "is reinforcement earning its keep". Byte-identical repeats are in neither half: they never would have become a record. |
| `pheasant_memory_maintenance_seconds` | histogram | One consolidation pass (archival + capacity pruning). |
| `pheasant_memory_compactions_total{op}` | counter | New `memory_compactions` ledger rows, by `op` (`subsume`, `synthesize`). |
| `pheasant_memory_compaction_seconds` | histogram | One L1/L2 clustering pass, when `memory.compaction_enabled`. |
| `pheasant_memory_synthesis_calls_total{outcome}` | counter | L3 synthesis cluster attempts (`synthesized`, `cached`, `empty`, `collision`) — only moves when `memory.synthesis.enabled` and `memory_synthesize` is called; never on the scheduler beat. |
| `pheasant_process_resident_bytes` | gauge | This process's RSS. |
| `pheasant_requests_capacity_remaining` | gauge | Free API admission slots when request limiting is enabled. |
| `pheasant_build_info{version}` | gauge | Always 1; the version is the label. |
| `pheasant_up` | gauge | 1 while serving. |

### Scrape it

```yaml
scrape_configs:
  - job_name: pheasant
    static_configs:
      - targets: ["pheasant:8765"]
```

### Useful queries

```promql
# Is anything actually stuck?
max by (source) (pheasant_index_stalled) > 0

# Sources not indexed successfully in the last day.
time() - pheasant_sync_last_success_timestamp_seconds > 86400

# Search p95 latency.
histogram_quantile(0.95, sum by (le, mode) (rate(pheasant_search_duration_seconds_bucket[5m])))

# Is memory compaction keeping up with an agent's write rate?
sum(rate(pheasant_memory_writes_total[1h])) and sum(rate(pheasant_memory_compactions_total[1h]))
```

!!! warning "Metrics are per process"

    Counters live in the process that serves the scrape. Indexing runs in a
    **child process**, so its throughput reaches `/metrics` through the job
    registry rather than from a counter the indexer owns — which is also why
    the indexing series are gauges sampled at scrape time rather than counters
    you can `rate()` over. Run one scrape target per pheasant container and
    aggregate in Prometheus, not in pheasant.

## Related

- [Speed up indexing](indexing-performance.md) — worker counts and executors.
- [Deployment](../deployment.md) — probes and ports.
