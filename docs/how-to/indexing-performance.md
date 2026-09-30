# Speed up indexing

pheasant separates work that can scale from state that must stay ordered:

1. discover and stat items in stable order;
2. prepare files concurrently (read, SHA-256, skip unchanged, parse and chunk);
3. embed changed chunks in bounded provider-sized batches;
4. commit SQLite, graph, manifests and vectors through one coordinator;
5. run global graph enrichment and save.

This preserves stable IDs, incremental skips and deterministic graph bytes at
every worker count.

In a fleet, scale the **preparation workers**, not the indexer. Each shard has
one elected indexer/commit authority; additional indexer replicas are hot
standbys. If commit/enrichment/graph-save time dominates after preparation is
fast, split sources into another shard instead of adding writers to the same
graph.

Keep the dispatch window bounded. A remote batch holds every file's bytes on
the indexer and worker. Each source keeps
`remote_worker_max_inflight_batches` batches in flight (default: two per
configured URL, never more than `max_parallel_files`), with the same number
again queued behind them, so the practical in-flight payload is roughly
`2 * remote_worker_max_inflight_batches * remote_worker_batch_size` files.

The two-per-URL default is right for a list of individual workers and wrong
for one URL that is a load-balanced Service: it cannot see the pods behind it,
so a source kept two of them busy however many the autoscaler started. Set
`remote_worker_max_inflight_batches` explicitly in that case. It spreads the
work; whether it also shortens the sync depends on which side is slower. On a
1,200-file code corpus against three workers behind one URL, raising it from
the default to 6 moved the third worker from zero requests to a third of them
and left wall time within noise (9.2s against 8.9-9.2s), because the indexer
spent 0.06s of an 11.5s profiled sync waiting on preparation: commit and
enrichment were the ceiling. It can only shorten a sync whose indexer is
waiting on its workers -- plausibly one of large documents, which was not
measured.

The fleet profile uses 16 x 16 (256 files), four worker containers with two
request threads each, `remote_worker_max_inflight_batches: 8` to match them,
and 8 embedding requests in flight. That leaves CPU and memory for Postgres, NATS,
the API and the graph owner on an 8-core development host.

## Choose a local executor

```yaml
sync:
  concurrency:
    max_parallel_sources: 2
    max_parallel_files: 8
    max_parallel_embeddings: 4
    file_executor: thread
    lock_timeout_seconds: 120
```

Use `thread` when reads, remote connectors or document handlers dominate. Use
`process` for CPU-heavy, ordinary text/code/Markdown corpora:

```yaml
sync:
  concurrency:
    max_parallel_files: 8
    file_executor: process
```

Process workers are capped by the CPU quota visible to the process. Sources
requiring local document/modal/taxonomy handler state, and repair passes that
inspect the live graph, fall back to thread workers safely.

Embedding concurrency is independent. Keep it within the provider's rate and
connection limits; retries already honor transient failures and `Retry-After`.

## Add remote worker nodes

Give every worker and coordinator the same secret environment variable:

```bash
export PHEASANT_INDEX_WORKER_TOKEN='replace-with-a-long-random-value'
```

On each worker:

```yaml
sync:
  concurrency:
    remote_worker_enabled: true
    remote_worker_token_env: PHEASANT_INDEX_WORKER_TOKEN
```

Run pheasant normally behind TLS or an authenticated private ingress. On the
coordinator:

```yaml
sync:
  concurrency:
    file_executor: remote
    max_parallel_files: 16
    remote_worker_urls:
      - https://index-worker-1.internal
      - https://index-worker-2.internal
    remote_worker_token_env: PHEASANT_INDEX_WORKER_TOKEN
    remote_worker_timeout_seconds: 120
```

The coordinator reads each connector payload and sends immutable content plus
source/chunking metadata round-robin. Workers return deterministic parsed chunks
and never receive write access to `/state`. PDF, DOCX and EPUB extraction is
remote-safe and each binary document gets its own bounded task envelope. Images,
audio, taxonomy-enabled sources and repair passes still prepare locally because
they rely on credentials, sidecars or live graph state.

## What a sync walks, and what it no longer walks

The indexer keeps the graph in memory as its working set — the builder mutates
it and the enrichment passes walk it. Three of those passes were reading the
whole graph for a slice of it, which is both sync time and the reason the
working set has to be as large as it is. Measured at 100k files (630k nodes):

| Pass | Was | Now |
|---|---|---|
| Cross-source resolution's node list | 2.95 s, +160 MB (every node copied) | 319 ms, +24 MB (the 15% it reads) |
| Removing a source or an artifact | 1.16 s snapshot before the filter | iterated under the lock |
| Similarity edges | a full walk and copy per sync | retired; it emitted nothing |

The similarity pass keyed off `concept_terms`, and concept extraction was
retired — so it had been building an index over an empty term set and emitting
zero `similar_to` edges for as long as that has been true. It is a no-op now,
asserted as one.

One O(total) cost remains on the indexer: removing nodes walks the edge table,
because an edge goes when *either* endpoint does and only the outgoing
direction is indexed. It measured ~120 ms at 100k files and fires once per full
sync and on a memory-maintenance beat; an in-adjacency index would remove it
for about 15% more working-set memory, which is the wrong trade against a plan
to stop holding the graph at all.

## Delta generations and recovery

Unchanged CLI/worker syncs defer graph deserialization. They list and compare
content-addressed manifest entries, update the checkpoint, and read counts from
the publication record; the full graph is loaded only when a changed artifact
must mutate it. Changed generations publish the graph first and the source
manifests last, so a crash causes safe reprocessing instead of an
ahead-of-graph manifest.

On the default `storage.graph_format: rows` a changed generation writes only
the rows that changed, in the same transaction as the artifacts and chunks — so
a commit costs what the change costs rather than what the graph weighs
(measured 1.1 ms versus 6.15 s at 100k files), and the graph can no longer
disagree with the chunks after a crash between two writes. On
`node_link_json` every commit re-serializes the whole graph, which is the
cost `storage.graph_checkpoint_seconds` exists to space out.

A redelivered **full** task resumes as an incremental delta only after a
graph+manifest checkpoint was durably published. Before that boundary it
restarts as full. This avoids repeating an entire large repository after a
late provider failure without trusting an uncommitted partial manifest.

These boundaries adopt the useful parts of GitHub Blackbird's architecture:
event-driven delta crawling, an ordered ingest stream, immutable index
generations and later compaction. Pheasant keeps source/repository shards
rather than blob shards because cross-document graph relationships are part of
its retrieval contract; use `pheasant shard plan` when graph commit/save time,
not parsing, becomes the limiting phase.

Do not expose `/internal/indexing/prepare` to the public internet. It is disabled
by default and bearer-authenticated when enabled, but task payloads contain the
source text being indexed.

## Measure before tuning

```bash
python -m pheasant.sync.benchmark --workers 1,2,4,8
python -m pheasant.sync.benchmark --workers 1,2,4 --executor process
python -m pheasant.sync.benchmark --workers 1,2,4 --embeddings
```

The harness creates a deterministic temporary corpus, stays offline, warms
parser/filesystem caches, and prints median seconds, individual trials,
files/second and speedup for a clean full index and the immediately unchanged
incremental pass (`--repeats 3` by default). The incremental report includes
embedding calls and should always say zero. Also test truly cold storage on a
representative deployment; the harness deliberately warms caches so worker-count
comparisons are not biased by whichever run touched the fixture first.
