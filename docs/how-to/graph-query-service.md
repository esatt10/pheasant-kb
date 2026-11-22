# Run graph queries as a separate service

pheasant-kb runs a `graph` tier: an internal service that answers graph
search, walks, slices and the taxonomy for the api replicas, on its own
connection pool. It is optional. The graph is rows in PostgreSQL, so any
replica can query it directly; the service exists to give graph reads their
own scaling curve and keep their load off the api process.

## What is separated

The indexer remains the single commit authority for a knowledge-base shard and
writes the graph's rows. The `graph` role reads them; api and MCP replicas send
graph operations to it over authenticated HTTP.

```text
api/MCP replicas -- authenticated HTTP --> graph service replica(s)
       |                                      |
       +-- text/vector --> PostgreSQL/LanceDB +-- graph rows (PostgreSQL)

indexer --> NATS task --> preparation workers --> PostgreSQL/LanceDB
```

The internal API exposes query-shaped operations: search, nodes, neighbours,
slices, paths, taxonomy, diagnostics, in-edges and bounded exports. It is not a
write API. Every request carries `X-Pheasant-Remaining-Ms`, which the service
turns into each read's `statement_timeout`, so a walk the caller abandoned
stops in Postgres too (504 when it runs out). A graph-service outage makes api
readiness fail rather than quietly answering without the graph arm.

Service discovery resolves every current A record and distributes calls
round-robin. Replicas are interchangeable readers of **one shard**; actual
data sharding is one region per knowledge base (`pheasant shard plan`).

## Configure it

pheasant-kb's config already has it:

```yaml
graph:
  query_service_url: http://graph:8765
  query_service_token_env: PHEASANT_GRAPH_SERVICE_TOKEN
  query_service_timeout_seconds: 30
```

The environment variable holds the bearer token, not the YAML. `secrets-init`
generates it, and it must differ from the worker token: a serving process
refuses to start when the two resolve to one value.

Unset `query_service_url` to have the api read the rows itself, and stop the
`graph` service. The answers are the same.

Scale graph replicas only after measuring graph-query saturation:

```bash
docker compose up -d --scale graph=2
```

On one Docker host a second replica competes for the same CPU and the same
Postgres: a graph-only 40-request, concurrency-8 run took 13.39 s with one
replica and 14.84 s with two. It pays on several machines, where another
replica adds CPU and availability.

## Why it exists: the 1.x measurement

The tier was introduced when the graph was one snapshot file that every
process answering a graph query held resident. Measured on 2026-08-26 over
10,055 artifacts, 92,009 nodes and 212,270 edges (PostgreSQL, NATS, one
indexer, four workers, one api, one graph service, one host):

| Measurement | Graph in the api | Graph service |
|---|---:|---:|
| Idle api RSS | 1.919 GiB | 102 MiB |
| 40 mixed searches, concurrency 8 | 33.2 s | 20.2-20.9 s |

With the graph in rows no replica holds it, so the memory half of that case is
gone; the [capacity measurements](capacity-planning.md) are the current
numbers. What remains is isolation: graph walks off hub nodes are the most
expensive reads a region serves, and on their own tier they cannot starve the
api's request threads.

The same work found that the high-fan-out cost was lexical ranking, not the
graph: a high-frequency term set measured text search at 18.44 s median
against 598 ms for graph and 839 ms for vector. That is why the assistant's
fanout is `[vector, graph, hybrid]` (hybrid already contains the text arm),
and it is what the pg_textsearch BM25 index later fixed
([configuration](../configuration.md#bm25-on-postgres-storagelexical_index)).

CI protects the boundary with an offline architecture gate; see
[Architecture regression testing](architecture-regression.md).
