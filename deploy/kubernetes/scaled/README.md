# The role-split fleet

`deploy/kubernetes/` (one Deployment, `role: all`) is the default and needs no
infrastructure. **These manifests are the opposite trade**, and they are worth
it only past the point where one container stops being enough — see
[capacity planning](../../../docs/how-to/capacity-planning.md).

```bash
kubectl apply -f deploy/kubernetes/namespace.yaml
kubectl apply -f deploy/kubernetes/scaled/
```

## What you must provide first

These are hard requirements, not recommendations. Each one exists because of
something in pheasant's design, so they are stated with the reason:

1. **Postgres** (`storage.backend: postgres`). SQLite permits one writer per
   file, and it is not one file across pods anyway. The DSN is read from the
   env var named by `storage.dsn_env` — put it in a Secret, never in the
   ConfigMap.

2. **A ReadWriteMany volume for `/state`.** The knowledge graph is a file the
   indexer writes and the graph-query service reads; API replicas also read
   shared vector/manifests. RWO (one node) cannot work with replicas spread
   across nodes. EFS, Azure Files, CephFS and NFS qualify; most default
   StorageClasses do not. Set `storageClassName` in `state-pvc.yaml`.

   The graph role polls that file (`server.api.graph_refresh_seconds`, 30s) and
   atomically reloads it. API replicas keep no full graph resident and fail
   readiness if their authenticated graph-service dependency is unavailable.

3. **A volume for `/exports`**, if anything outside pheasant consumes the
   corpus. `exports-cronjob.yaml` writes the Parquet extract there nightly and
   readers mount the same claim read-only — a warehouse loader, an analytics
   job, an object-store sync. It is RWX for the same reason `/state` is: a
   reader scheduled on another node cannot mount an RWO claim. It was an
   `emptyDir`, which meant the indexer's export landed somewhere the api
   replicas could not see and vanished on restart.

   The export reads `/state` read-only, which works *because* this fleet is on
   Postgres — the tables come from the database and only the graph file is read
   from disk. Losing this volume costs a re-export, never data. See
   [Parquet exports](../../../docs/how-to/parquet-exports.md).

4. **The durable queue** (`sync.queue.enabled: true`). An api replica
   publishes index work rather than running it; without a queue it would
   accept syncs that go nowhere, and `--role api` refuses to start.

5. **A metrics adapter**, if you want the worker HPA. Scaling on
   `pheasant_index_queue_depth` needs [prometheus-adapter] or [KEDA] to expose
   it; `worker-hpa.yaml` ships the KEDA form and the CPU fallback.

6. **Three distinct bearer tokens** in `pheasant-secrets` (see
   `secret.example.yaml`), one `openssl rand -hex 32` each:
   `PHEASANT_API_TOKEN` for callers of the region's API — every serving pod
   binds `0.0.0.0` and refuses to start without it, unless the ConfigMap sets
   `security.api_auth.behind_authenticating_proxy`;
   `PHEASANT_GRAPH_SERVICE_TOKEN` for API/MCP clients of the graph service;
   and `PHEASANT_INDEX_WORKER_TOKEN`, the only secret the worker Deployment
   mounts. Reusing one value across the last two is refused at startup —
   workers hold the worker token by necessity, so sharing it would hand every
   worker the credential for the whole graph. Never place any of their values
   in the ConfigMap.

7. **A CNI that enforces NetworkPolicy**, if `networkpolicy.yaml` is to do
   anything. It default-denies ingress across the pheasant pods and adds back
   one allowance per real caller, so the graph service and the workers are
   reachable only from pheasant's own pods and the monitoring namespace. On a
   CNI that ignores NetworkPolicy the file is inert and the tokens are the
   only control.

[prometheus-adapter]: https://github.com/kubernetes-sigs/prometheus-adapter
[KEDA]: https://keda.sh

## The shape

| Workload | Kind | Scales on | Why |
|---|---|---|---|
| `pheasant-api` | Deployment | CPU (HPA) | Request traffic. Stateless — the MCP server is `stateless_http`, so an agent's two requests may land on different replicas. |
| `pheasant-graph` | Deployment | graph-query CPU/latency | Owns and refreshes the resident graph; scale replicas for availability/query throughput, or deploy one stack per KB shard. |
| `pheasant-indexer` | StatefulSet, 1 replica | not autoscaled | Owns the watcher and scheduler. **One per shard**, not one per cluster: two indexers on one shard is not faster, it is two processes taking turns. |
| `pheasant-worker` | Deployment | queue depth (HPA/KEDA) | Parse/chunk only. The elastic tier — no state, no credentials, safe to kill. |

Chunking is planned per file (`sync.source_processing.chunk_strategy: auto`),
and a PDF longer than 500 pages has its text read by several worker pods at
once in page ranges (`remote_worker_pdf_pages_per_task`) — over the same HTTP
Service, so no extra port, probe or NetworkPolicy rule. See
`docs/how-to/dynamic-chunking.md`.

Sharding across several knowledge bases means one of these stacks per shard,
each with its own `pheasant.name` and its own database. `pheasant shard plan`
proposes the split.

## Retrieval and autoscaling scope

The scaled ConfigMap fans assistant retrieval over `vector`, `graph`, and
`hybrid`. Hybrid already executes text, vector, and graph internally, so a
standalone text arm would repeat PostgreSQL full-text ranking. Exact text mode
remains available to API/MCP callers, and lexical evidence remains in hybrid.
The local profiles keep their existing defaults.

Treat one namespace/Helm release as one knowledge-base shard and scope every
metric by that shard's labels. Do not let the queue depth from shard A scale
workers for shard B. Each shard needs a distinct `pheasant.name`, database
scope, NATS durable/subject, state claim, and internal tokens. Synapse or an
upstream router fans search across shards; Kubernetes replicas inside one
shard all serve the same complete index.

| Workload | Scale-out condition | Scale-down/stability rule |
|---|---|---|
| API | sustained request CPU/latency | keep at least two; retain the five-minute stabilization window |
| Graph | sustained graph-query latency/CPU or failure-domain availability | each pod needs memory for old + new snapshots during refresh |
| Worker | queue depth + in-flight sources + preparation backlog | KEDA may reach zero; keep the 300 s cooldown |
| Indexer | never for throughput | one active writer; add only a standby or another complete shard |

Graph save, enrichment, embedding-provider throttling, and ordered commits are
not worker/API HPA signals. If save/enrichment becomes dominant, split whole
repositories or document collections between shards so each shard owns a
smaller graph and an independent commit authority.

## What is deliberately not here

* **No HPA on the indexer.** Its work is serialized per source by a lease, so
  a second replica would spend its life losing races. Add indexers by adding
  *shards*.
* **No `PodDisruptionPolicy` allowing the indexer to be evicted freely.** Its
  PDB is `minAvailable: 0` on purpose — one replica cannot satisfy anything
  higher, and pretending otherwise blocks node drains forever. A drained
  indexer's in-flight task is redelivered by the queue's visibility timeout,
  which is what makes that safe.
* **No NetworkPolicy for the worker port.** `deploy/kubernetes/networkpolicy.yaml`
  covers the base install; the preparation endpoints are bearer-authenticated
  and disabled by default, but they should still not be reachable from outside
  the namespace. Extend that policy for your cluster.
