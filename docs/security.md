# Security

pheasant indexes local content for agents, so it must be conservative about paths, secrets, and execution.

## Required controls

- Only index paths under configured allowlisted roots.
- Reject path traversal and unsafe symlinks that escape allowlisted roots.
- Exclude secrets and generated dependency/build folders by default.
- Do not execute code from indexed repositories.
- Keep MCP tools limited to retrieval, sync, registration, export, and status operations.
- Prefer read-only source mounts in Docker and Kubernetes.
- Bind local API/UI carefully and protect enterprise ingress with cluster controls.
- Where a corpus must exclude specific content (benchmark answer keys, evaluation
  artifacts), enforce it with `readiness.corpus_denylist` rather than by convention —
  it refuses the write at every door rather than detecting the leak afterwards.

## Trust model for the HTTP API

**One container: no authentication, and the bind address is the control.**
Every route — including the ones that write config, register sources and
trigger syncs — is open to anything that can reach the port. That is a
deliberate local-first choice for a tool running on somebody's own machine
behind their own perimeter, and it makes two controls load-bearing:

- **Bind address.** Loopback by default on both paths: `pheasant up`
  generates `host: 127.0.0.1`, and compose publishes
  `127.0.0.1:8765:8765`. The container itself still binds `0.0.0.0`, because
  binding loopback *inside* a container makes it unreachable from the host —
  it is the published port that is restricted. Set `PHEASANT_BIND=0.0.0.0`
  to expose it, and only behind an authenticating ingress. Note that Docker's
  port publishing writes its own iptables rules, so a host firewall is not a
  substitute for this.
- **CORS origins.** `server.api.cors_origins` is an allowlist, not `*`.
  Without it, any web page the user visits can script the whole API from
  their browser: read the index, rewrite the config, or repoint the
  embedding provider at an attacker's host and ship a server-held API key
  with the next request. The bundled UI proxies `/api/*` same-origin in
  both dev (Vite) and compose (nginx), so it needs no CORS entry at all.
  `server.api.cors_allow_all_origins: true` restores the wildcard for
  deployments that authenticate upstream.

Anything reachable over that API is reachable by whoever can reach the port.
Treat "who can open :8765" as the real authorization boundary.

### A fleet is the other case, and it is enforced

That posture does not survive the role split, and until Phase 35.8 it shipped
into it unchanged. In a fleet the API is a multi-replica Service; the pods bind
`0.0.0.0` because a Service cannot reach a loopback-bound pod, so the bind
address stops being a control at all — and the surface behind it can register a
source over any allow-listed path and read what it finds. "Unauthenticated"
plus "can mount anything" plus "one port-publishing decision away from the
network" is a combination that stays safe by luck.

So every role **but `all`** refuses to start on a routable bind unless one of
two things is true:

| Setting | Means |
|---|---|
| `security.api_auth.token_env` resolves to a value | Callers send `Authorization: Bearer <value>`. Everything outside `security.api_auth.public_paths` (`/health`, `/ready`, `/metrics`) is refused with `401`. |
| `security.api_auth.behind_authenticating_proxy: true` | "Something in front of this authenticates callers." Explicit, because the failure it suppresses is silent. |

`all` is exempt, and that exemption is the point: a laptop, a `pheasant up`
and every existing standalone container keep starting with no configuration at
all. The token is deliberately a static shared secret and nothing more — enough
to make "behind an authenticating ingress" the default rather than the
instruction, and small enough that it cannot rot into a half-built identity
system. Richer authentication belongs to the ingress.

`/internal/*` is exempt from the API token structurally, not by configuration:
those routes already enforce a token for their own boundary (the worker token,
the graph token), and requiring a second would mean handing the region's
front-door credential to every preparation worker.

The fleet profiles also turn `security.acl_enforced` on with
`default_visibility: public`, so ACLs are honoured where they exist without
requiring every artifact to carry one first.

### One secret per boundary

The fleet has three, and they must be three distinct values:

| Variable | Boundary |
|---|---|
| `PHEASANT_API_TOKEN` | callers → the region's API |
| `PHEASANT_GRAPH_SERVICE_TOKEN` | API/MCP replicas → the internal graph API |
| `PHEASANT_INDEX_WORKER_TOKEN` | the indexer → the preparation workers |

The shipped Compose file used to wire the graph token to the worker token's
value. Workers are the least-trusted tier — no database, no keys, no volumes,
and the one place third-party parse code runs — and they hold the indexing
token by necessity, so one value meant compromising any worker also yielded the
credential for the internal graph API. A serving process now refuses to start
when the two resolve to the same value, or when both settings name one
variable.

### What a role may hold

`validate_role` is a per-role allow-list, not just a set of "can it work"
checks. A worker refuses to start holding a database DSN, any model provider
key, the IdP token, the graph-service token, a source list, or a non-SQLite
state backend — none of which it can use, since it is handed bytes and returns
chunks. The refusal names the variable and says where to put it instead
(a worker's own `environment:` block, or its own Secret).

A worker also serves no knowledge-base API: `server.api.enabled: false` is
enforced rather than declared, so such a process answers `/health`, `/ready`,
`/metrics` and its own `/internal` preparation routes and `404`s everything
else. That flag had been in `deploy/compose/worker.yaml` since the tier
existed, and nothing read it.

## Path and write policy

### Indexing any readable path — the deliberate tradeoff

`security.allow_user_selected_source_paths` defaults to `true`: a source may
name **any path the pheasant process can read**, not just one under
`allow_workspace_roots`. This is a deliberate product decision — pointing
pheasant at a folder without first editing an allowlist is the whole
quickstart experience — and it means the process's own filesystem access is
the boundary. Four controls compensate, and they are why the tradeoff is
tenable:

1. **Credentials never enter the index.** `security.default_exclude_secrets`
   (on by default) unions `SECRET_EXCLUDES` into every filesystem source's
   exclude list — SSH and GPG keys, `.env`, `~/.aws`, `~/.kube`,
   `~/.docker/config.json`, `~/.config/gh`, `.netrc`, `.npmrc`,
   `.git-credentials` and more. Critically, this happens *after* any
   caller-supplied `exclude`, because supplying that list replaces the field
   wholesale. Without this, indexing `$HOME` with
   `include: ["**/*.json", "**/*.yaml"]` sweeps up live tokens.
2. **The traversal is bounded** (`sync.limits`) and refuses rather than
   truncates, so a mistaken source is a clear stop, not an OOM.
3. **The API is not exposed to the network by default** — `pheasant up`
   generates `host: 127.0.0.1`, and compose publishes to loopback. Since the
   API is unauthenticated, this is what keeps "can read any path" from
   meaning "anyone on the network can read any path".
4. **The container does not run as root**, so in a Docker deployment the
   reachable filesystem is narrower than the host's.

Set `allow_user_selected_source_paths: false` (with explicit
`allow_workspace_roots`) for a multi-user or exposed deployment where
callers should not choose paths at all. The cost is that the UI file browser
can no longer leave the configured roots; the CLI is unaffected either way.

**What this does not protect against:** anything that can reach the API can
still ask it to index any path the process can read, and read the result
back. If you expose the port, put an authenticating proxy in front of it and
turn the flag off.
- **Config writes.** Source promotion (`POST /sources/{id}/promote`, MCP
  `promote_runtime_source_to_config`) may only write this server's own
  config file or a path under a configured root. It deliberately does *not*
  consult `allow_user_selected_source_paths`: choosing what to index and
  choosing where the server writes YAML are different permissions.
- **Remote fetching.** The `web_collection` and `api` connectors fetch
  `http`/`https` only. `file://` URLs are refused (and skipped with a
  warning rather than failing the sync) so a "web collection" cannot be
  used to read and index the host filesystem. Index local content with a
  filesystem source, which goes through path policy.
- **Cloning.** Clone URLs must name a known transport (`http`, `https`,
  `ssh`, `git`) or the `user@host:path` form. Transport helpers such as
  `ext::` (which name a command for git to run) and anything starting with
  `-` (which git parses as an option) are refused before `git clone` sees
  them; the clone subprocess additionally runs with `protocol.allow=never`
  plus explicit per-protocol allowances and `GIT_TERMINAL_PROMPT=0`.
- **Backup restore.** Archive members are checked for traversal *and* for
  links whose target escapes the destination, then extracted with the
  stdlib `data` filter.

### Content that may never be indexed (`readiness.corpus_denylist`)

Path policy answers "which paths may this process **read**". This answers a
different question: "which paths may become **retrievable**". They are not the
same — a benchmark answer key is a file pheasant is entitled to read and must
never be able to return.

```yaml
readiness:
  corpus_denylist:
    - "benchmark/*"
    - "*.answers.json"
```

Patterns are fnmatch, tested against both an item's relative path and its bare
filename. Empty by default, and an empty list costs one truth test per item.

**It is enforced at every door into the corpus, and that is the whole point of
the control.** The rule is `security/corpus_policy.py`, and both write paths
call it:

| Door | Behaviour |
|---|---|
| `POST /ingest/submit`, `submit_documents` | Refused per item with `CORPUS_DENYLISTED` (HTTP 403), before the bytes are written. |
| Any source the engine syncs — folder, git, upload directory, every connector | Refused per item **before the item is read**, logged at WARNING, and counted in the sync report's `refused` / `refused_total`. |

The first version enforced it on submissions only. A region with a denylist
configured therefore still indexed an answer key that arrived through a folder
source, the UI drop zone or a git repository — while the readiness gate
reported the boundary intact. A control on one of several doors is a door with
a sign on it, and the general shape is worth keeping: when a rule protects
*the corpus*, it belongs where everything enters the corpus, not where the
first caller happened to be.

**What it does not do.** Enforcement stops new arrivals; it does not remove
content indexed before the denylist existed. `pheasant sync --mode full`
rebuilds a source from scratch and so clears it, and
`pheasant readiness check` scans `artifacts` directly — the
`benchmark_contamination` gate is a statement about what this corpus holds, not
about what this code refuses, which is why it can fail on a region whose code
is correct.

### The readiness plane writes, and only in two places

`POST /readiness/check` (and `pheasant readiness check` / the
`run_readiness_check` tool) performs real work: it submits documents, indexes
them, seals snapshots and runs searches. Three properties bound it:

- **It is opt-in.** Gated on `readiness.enabled`, which is off by default, so a
  stranger reaching an unauthenticated port cannot start one. The *contract*
  endpoint is deliberately not gated — answering "can this region be measured"
  with a 404 is indistinguishable from an old build that has none — and it
  reports `readiness_enabled` so the caller can tell.
- **It writes only to a scratch source it owns.** Everything lands under
  `<state_path>/uploads/__readiness__probe`, registered as an ordinary
  `document_folder` source. No caller input reaches that path: it is derived
  from the server's own `state_path` and a module constant. It never writes to
  a configured source and never writes memory.
- **It never takes `sync_lock`.** Same posture as the evaluation and tuning
  planes, and for the same reason.

`POST /ingest/submit` is a general write surface, not a readiness-only one, and
is **not** gated on `readiness.enabled` — it is the same trust posture as
`POST /sources/upload`, which it sits beside. Both write caller-supplied bytes
under `<state_path>/uploads/<source>`:

- The source name and every path component are reduced to a single safe
  filename each (`ingestion/landing.py`), so `../../etc/passwd` cannot escape
  the upload root.
- Per-item size is bounded by `sync.limits.max_file_size_mb`; there is no
  aggregate bound, so on an exposed deployment these are among the routes an
  authenticating proxy must cover.
- Receipts are written per item, which means an unexpected write is *visible*:
  `GET /ingest/reconcile` reports what was submitted against what the region
  holds.

## Prompt injection posture

Indexed documents are untrusted data. Retrieval responses should preserve provenance and should not cause agents to run instructions found inside indexed content unless explicitly requested by the user and independently validated.

## Default excluded content

Two separate lists, because they answer different questions:

- **`SECRET_EXCLUDES`** — disclosure. SSH/GPG keys, PEM/`.key`/`.p12`,
  `.env*`, `~/.aws`, `~/.azure`, `~/.kube`, `~/.gnupg`,
  `~/.docker/config.json`, `~/.config/gh`, `~/.config/gcloud`, `.netrc`,
  `.npmrc`, `.pypirc`, `.git-credentials`, keychains and password stores.
  Governed by `security.default_exclude_secrets` and unioned into every
  filesystem source **after** any caller-supplied `exclude`, so they cannot
  be dropped by accident.
- **`NOISE_EXCLUDES`** — cost. `.git`, `node_modules`, `__pycache__`,
  virtualenvs, `dist`/`build`/`target`, and tool caches. These are ordinary
  defaults an operator may legitimately replace, and the walker *prunes*
  them, so an excluded subtree is never descended into.

If you deliberately need to index something on the secret list, set
`security.default_exclude_secrets: false` and take responsibility for the
source's own `exclude`.

## Upgrade note: container user

The image runs as uid 10001 rather than root. A `/state` volume created by an
older root-running image will still be owned by root and the new container
will fail to write to it. Fix it once with:

```bash
docker run --rm -v pheasant_pheasant-state:/state alpine chown -R 10001:10001 /state
```

## Artifact ACLs and principal-aware retrieval (Phase 32)

SaaS connectors capture source permissions into a canonical per-artifact ACL
(`{"allow": ["user:…", "group:…"], "public": bool}`). Enforcement is opt-in:

```yaml
security:
  acl_enforced: true          # default false = pre-32 behavior, byte-identical
  default_visibility: public  # un-ACL'd artifacts; "private" requires a principal
  groups:                     # deterministic config-mapped principal -> groups
    carol:
      - eng
```

With enforcement on, `search_context` (library, MCP, HTTP `/search`) accepts
`principal` + `principal_groups` and filters candidates against artifact ACLs
before results are merged. The trust model: the region enforces *visibility*;
the caller (the Synapse router, or your deployment perimeter) authenticates.

Enforcement covers **every** surface that returns indexed content, not just
`/search`: `get_relevant_files` / `POST /relevant-files` run the same filter,
and the raw-content endpoints (`GET /files/summary`, `GET /nodes/content`)
take a `principal` query parameter and answer `403` for a caller the
artifact's ACL does not admit. Filtering search results while serving the
same bytes from another route is not enforcement. All of it is a no-op when
`acl_enforced` is false.

### External IdP group sync (Step 32.4)

Group membership can also be synced from a SCIM 2.0 directory instead of
hand-maintained config:

```yaml
security:
  idp:
    enabled: true
    provider: scim
    base_url: https://idp.example.com/scim/v2
    api_key_env: IDP_TOKEN        # bearer token env var; never stored
    sync_interval_minutes: 60     # scheduler-beat refresh cadence
    staleness_max_minutes: 1440   # the SLA
```

The mapping persists in the region's SQLite state and refreshes on the
scheduler beat or on demand (`POST /security/idp/sync`;
`GET /security/idp/status` reports the last heartbeat and SLA verdict).
**Staleness SLA:** if the last successful sync is older than
`staleness_max_minutes`, IdP-derived grants are dropped — fail closed —
until the next successful sync. Config-mapped groups and explicit caller
groups are unaffected.
