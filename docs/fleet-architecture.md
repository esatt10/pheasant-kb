# The fleet, with everything switched on

One container is the default and needs no infrastructure. This page is the other
trade: `deploy/compose/docker-compose.scale.yml` with `fleet.yaml` — Postgres,
NATS JetStream, gRPC preparation workers, LanceDB, and every optional plane
(observation, evaluation, tuning, readiness) enabled at once.

The diagram is drawn from the shipped compose file, `fleet.yaml` and
`deployment/roles.py` rather than from prose, so the numbers on it are the ones
those files actually set. It will go stale if they change.

[![The pheasant role-split fleet at maximum configuration: sources and SDK connectors feed one indexer that is the sole commit authority, which hands bytes to N gRPC preparation workers and drains a NATS JetStream index queue; it is the only writer of PostgreSQL and the state volume, which N graph-free API replicas and the graph tier read; agents, operators and a Synapse router consume the API; model endpoints, the observation, evaluation, tuning and readiness planes, and three separate service tokens sit alongside](assets/fleet-architecture.svg)](assets/fleet-architecture.svg)

*Open the image on its own for a full-size, readable view.*

---

## The same picture, in plain language

One analogy carries the whole diagram: think of a region as a **library**.
Material arrives, one cataloguer owns the catalogue, assistants do the reading,
and the reference desk answers questions.

### What comes in

**Mounted sources** — `repository · folder · file · vault · web · s3 · memory`

The material the library agrees to hold, mounted read-only: pheasant catalogues,
it never edits. Every item gets a sha256 fingerprint, so on the next pass it can
tell "this hasn't changed" *before* it opens the file. That is why re-syncing an
untouched corpus costs almost nothing.

**SDK connectors** — `notion · gdrive · slack · confluence · imap`

The same idea for material that lives behind an API rather than on a disk. Write
your own and it can run inside a WASM sandbox — a sealed glovebox where the
connector only reaches the tools you passed through. Ask for one you did not
wire up and it will not even load.

### The part that does the work

**The indexer** — `serve --role indexer`, exactly one

The head cataloguer, and the most important box on the diagram. It watches
folders, wakes on a timer, pulls jobs off the queue — and it is **the only thing
allowed to write to the catalogue**. Not by convention: the graph, the vectors
and the full-text index all have to change together, in one consistent moment,
and you cannot have two people rewriting the card catalogue at once.

The consequence is worth internalising: **more indexers will not make ingestion
faster.** A second one waits as a warm spare. When the first is genuinely maxed
out — `pheasant_commit_authority_saturation` above 0.8 — the answer is to split
the collection in two, not to hire a second cataloguer for the same one.

**Preparation workers** — `worker --transport grpc`, as many as you like

The assistants, and the tier where you actually buy speed. They crack open PDFs
and Word documents, cut text into chunks, caption images, transcribe audio, and
hand the results back. What makes them safe is what they are *not* given: no
database password, no model key, not even a list of what the library holds. The
process refuses to boot holding any of it.

**NATS JetStream** — the durable job queue

The ticket spike. The indexer drops work on it and claims work off it, and
because it is durable, an indexer that dies mid-job loses nothing — the work is
simply picked up again. Jobs are named by their content, so "index this file"
submitted twice is one job, not two.

### Where it is all kept

**PostgreSQL** — the catalogue itself

Every artifact, every chunk, the searchable index, the graph's nodes and edges,
and the bookkeeping. One decision earns a mention: the graph is stored as *rows*,
not one big file. The old way rewrote the entire graph for a one-file edit and
got slower as the library grew; now a change costs what the change costs. That is
most of why the fleet scales at all.

**`/state`** — the working drawer, and user data

Vectors (the "meaning" index), manifests, snapshots, the live tuning bundle. The
indexer holds the only key; everyone else reads over its shoulder. This is
operational truth, so schema changes ship a migration that preserves the original
rather than replacing it.

**`/exports`** — the photocopy room

Parquet tables, logs rolled to cold storage, per-trial tuning data. Everything
here can be regenerated from the catalogue, and it is the one directory designed
to be read by something that is not pheasant — mount it and point your own tools
at it. Nothing is served over HTTP.

### The part that answers questions

**The graph service** — `serve --role graph`

The one process that keeps the whole knowledge graph in its head, for the passes
that genuinely need to see all of it at once — like working out that a reference
in one repository points at a file in another. It answers graph questions for
everyone else over an internal API.

**API / MCP replicas** — `serve --role api`, scale freely

The reference desk. Each question is searched three ways at once — keywords,
meaning, and a walk through the graph — then merged by *rank position* rather
than score, because the three arms score on scales that cannot be compared and
naive merging quietly collapsed into keyword-only. Same desk, two windows: HTTP
for the UI, MCP for agents, one implementation behind both.

**Who is asking** — agents, operators, the Synapse router

Agents over MCP, humans through the bundled UI, and a router that federates
across several libraries: it asks "which of these is likely to know?" and
forwards accordingly. The router speaks JSON over HTTP and nothing else — the two
codebases deliberately share no code.

### Everything switched on

**Model endpoints** — the only outbound calls

Embeddings, image captions, audio transcripts, and the assistant that writes
grounded answers. Everything else runs offline, and one rule is absolute: *no
model call may influence how something gets indexed.* Indexing has to be
reproducible, and "ask an AI what this means" is not.

**Observation** (`observability.interactions`) — recording what was asked and what
came back, kept as rows and deliberately never as searchable documents. A
conversation *about* the library does not get to become part of the library.

**Evaluation** (`evaluation.*`) — "how good are our answers, really?" It counts
only evidence where somebody actually said so: a citation, an accept, a reject.
Silence is not failure — finding your answer at result one and leaving looks
identical to finding nothing at all.

**Tuning** (`tuning.*`) — not "how well" but *which step* failed. Six very
different causes all look identical from outside, and this tells them apart. Its
most valuable output is saying "nothing I can adjust would fix this" instead of
shipping a change that merely looks good.

**Readiness** (`readiness.*`) — "can an outsider trust these numbers?" It
publishes a machine-readable list of what this build can and cannot do, then runs
probes to prove each claim against this corpus. Three gates passing and one
unrunnable reports as INCOMPLETE, never as PASS.

**The three tokens** — three doors, three keys

Visitors → the library, reference desk → the graph service, cataloguer → the
assistants. They must be genuinely different values and the server refuses to
start if two match: a compose file once wired two together as a convenience,
which meant any compromised assistant held the key to the whole graph.

---

## Four axes, and one of them has a ceiling

| Axis | Mechanism | Scales on |
|---|---|---|
| Request traffic | `serve --role api` replicas; publish instead of index | CPU / RPS |
| Ingest throughput | `--role indexer` claiming from the durable queue, `--role worker` preparing | `pheasant_index_queue_depth` |
| Corpus size | `pheasant shard plan` packs whole sources per region | graph nodes |
| Observation volume | `--role logger` draining its own queue (`log_tasks`, never `index_tasks`) | `pheasant_log_queue_depth` |

**The second axis is the one that stops.** One indexer is the sole commit
authority per shard — graph, vectors and the full-text index are a single
coordinated commit stream — so extra indexers are elected hot standbys rather
than throughput. When `pheasant_commit_authority_saturation` holds above 0.8,
more workers will not help and the third axis is the only way past it.

**The short version: one writer, many readers, and everything expensive kept off
the request path.**

## See also

- [Architecture](architecture.md) — the measured bottlenecks behind these choices
- [Capacity planning](how-to/capacity-planning.md) — when one container stops being enough
- [Running a worker fleet](how-to/worker-fleet.md) — standing the tiers up
- [Security](security.md) — the three boundaries and the startup refusals that hold them
