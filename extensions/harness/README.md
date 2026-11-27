# Pheasant agentic runtime harness

An installable Pheasant extension (`harness`, protocol v1) that implements the
*Agentic runtime production-readiness specification v1.1*: the runtime owns
the investigation, opens authoritative material at a pinned revision, builds
the artifact in an ephemeral sandbox, has it checked by an independent
verifier on its exact digest, and delivers it through an adapter that can
prove — or honestly cannot prove — that the write happened once.

One service layer (`service.py`) backs three surfaces:

| Surface | Where | What |
| --- | --- | --- |
| HTTP | `http.py`, `/harness/*` | registrations, grep/read/discover, runs, events (JSON + SSE), artifacts, effects, sandboxes |
| MCP | `mcp.py`, 29 `harness_*` tools | the same operations, same refusal text and codes |
| UI | `ui/`, route `/harness` | run list, composer, lifecycle, budget, fences, delivery receipt, deliverable with clickable `[E…]` citations, verification matrix per candidate digest, evidence, steps/attempts, per-run connection audit, journal, sandboxes; the Connections view (policies, audit) and the approval card for a write through a connection |
| CLI | `pheasant harness worker\|reap\|status\|contract\|isolation` | a coordinator process of its own, the reaper |

## Install

```sh
pip install -e ./extensions/harness            # backend: HTTP, MCP, CLI
PHEASANT_BUILD_EXTENSIONS=harness npm run build  # in ui/: the browser page
docker compose -f compose.yaml -f deploy/compose/docker-compose.harness.yml up -d --build
```

Settings are environment variables: `PHEASANT_HARNESS_SANDBOX_ROOT` (scratch;
never part of recovery), `PHEASANT_HARNESS_DELIVERY_ROOT` (the `file`
adapter's root, default `<exports>/harness`), `PHEASANT_HARNESS_RUNNER=off`
(no in-process background runner; use `pheasant harness worker`).

## How a run works

`accepted → running ⇄ verifying → delivering → succeeded`, with `waiting`,
`reconciling`, `partial`, `failed`, `cancelled` beside it.

* **Coordinator** (`coordinator.py`, `store.py`). The only writer of run state.
  Every transition is one transaction: a fenced `UPDATE … RETURNING last_seq`
  on the run row, the journal event, and the projection writes it justifies.
  A claim takes the lease and moves the fence when the holder changes; a
  stale worker's writes match no row. Results are accepted only for the
  step's current attempt under the current fence, on an active run; a late,
  duplicate or superseded result is recorded with what it spent.
* **Agent loop** (`planner.py`, `ledger.py`). The ledger is rebuilt from
  committed rows (request, pinned revisions, evidence ids, failed avenues,
  budget) and compacted with evidence ids kept. `RulePlanner` is the
  deterministic, model-free path (Pheasant rule 1); `ModelPlanner` lets a
  configured model choose observations and write drafts and falls back to
  rules on any unusable reply. A planner can only say a candidate is
  *ready*; it cannot pass a check.
* **Broker** (`broker.py`). Typed tool arguments, the task contract's
  capabilities, inputs the actor may still read, the authorized destination,
  held-out fixtures out of the agent's reach, and the budget — checked in
  code on every call. Retrieved instructions are flagged and stay data.
* **Registry and resolvers** (`registry.py`). `file://` folders (manifest
  digest, optional snapshot into the blob store), git checkouts (commit),
  and `pheasant://source/prefix` (the region's own index). Pointer-only
  registrations are supported. Revisions that move under a reader raise
  `HARNESS_REVISION_MOVED`.
* **Investigation** (`investigate.py`, `discovery.py`). Grep reports scanned
  vs. skipped (glob, hidden, ignored, binary, generated, too large,
  unreadable, changed) and returns a cursor that resumes at the exact next
  match; only a complete scan yields a count or absence statement.
  Discovery is the region's hybrid search: candidates with the digest the
  index read, so a stale entry becomes a recorded discrepancy, not a citation.
* **Sandboxes** (`sandbox.py`). One generation per run and trust profile,
  inputs read-only, a private working copy, `prlimit` limits, a scrubbed
  environment, a user network namespace where the kernel allows it.
  Checkpoints are trees by digest in the blob store; a fresh generation on
  any host rebuilds from them. The reaper disposes of abandoned
  generations, confirms disposal and quarantines what it cannot confirm.
* **Verifier** (`verify.py`). `citations_resolve`, `claims_grounded`,
  `coverage_stated`, `values_present` (on the print render), `contains`,
  `absent`, `cites_registration`, `diff_scope`, `command` (fresh verifier
  sandbox, fixtures only there), `ui_render` and `render_ok` (Chromium when
  present, else `skipped`), `staging_contract`. Outcomes are
  pass/fail/skipped/inconclusive, keyed by candidate digest.
* **Delivery** (`delivery.py`). `record`, `file` (conditional on the expected
  version, receipt sidecar), `test_sink` (idempotent by operation id),
  `blind_sink` (neither idempotent nor observable). Operation ids are derived
  from run lineage, target and payload digest; an ambiguous outcome is
  reconciled before anything is sent again, and an unknowable one waits for
  an operator (`harness_resolve_effect`).

## The workspace: what a person does

The Harness page opens on a composer (`ui/Composer.tsx`), modelled on how a
coding agent's task composer works: say what to build, choose where it works,
choose where the result goes.

* **Change code** or **Build content.** The harness is an optional build layer,
  not the way questions are answered: nothing routes a chat or a search here
  unless someone types `@harness` (below). (`answer` and `summarize` remain in
  the API and the chat keyword; the composer does not offer them.)
* **Work in a repository, branch and folder.** Pick a connection, a repository
  (or Drive folder, or OneDrive drive), a branch and a directory by browsing
  them (`GET /harness/connections/{name}/browse`, `harness_browse_connection`;
  every listing is a `list` under the connection's policy, and
  `/harness/connections/usable` offers only what a policy enables). The pick is
  registered as `connection://<name>/<repo>/<folder>` pinned to that branch.
* **Attach files** -- every type Pheasant's uploads take -- with the **+** in
  the task box, by dropping them, or over the API (`POST /harness/attachments`
  multipart, `harness_attach_files` with base64; `attachments.py`). They become
  one immutable input, `attachment://<batch>`, stored by digest so any replica
  can read them. Each file is read by **Pheasant's own ingestion**
  (`pheasant.ingestion.pipeline.payload_text`, the dispatch every sync uses):
  documents (PDF, Word, PowerPoint, Excel, RTF, EPUB) and HTML are extracted,
  notebooks read as their cells, images captioned, audio transcribed, text and
  code read as they are. What is not plain text gets a text twin beside the
  original (`<name>.txt`, `<name>.caption.txt`, `<name>.transcript.txt`) so it
  can be searched and cited, while materialize copies the originals into the
  sandbox. ZIP and tar archives, nested ones included, are expanded by
  Pheasant's archive reader into `<archive name>/…` beside the archive, and the
  member types the index skips are counted, not dropped silently. Every entry
  carries the metadata the index gives it (`mime_type`, `artifact_type`,
  `indexable`, `text_from`, and `archive` for a member). The originals land in
  Pheasant's **uploads** source under `harness/<batch>/` through the ordinary
  submission path with that metadata in their receipts (plus the registration,
  the label, who attached them, size and sha256), the source is synced, and
  the Pheasant document each one became is recorded
  (`harness_attachment_uploads`; `GET /harness/attachments/{registration}`,
  `harness_attachment_documents`), so an attachment is also knowledge and can
  be followed both ways. Attaching the same files again is the same batch.
* **Speak the task.** The task box has the chat's microphone: the same
  dictation session and cap (`assistant.voice`), shown only where the server
  offers dictation.
* **Where the result goes:**
  * **a Pheasant artifact** (adapter `artifact`, the default): a document as a
    report, a code change as the files it changed with its release note, filed
    in the `artifacts` source; found again by its operation id, so a crash after
    the save does not save twice;
  * **a branch** (adapter `connection`, `connection://gh/<owner>/<repo>/[folder/]?branch=b&base=main&pr=1`):
    a missing branch is created from `base`; a code change is **one commit** of
    every changed file through the Git Data API (blobs, a tree on the branch
    head, a commit naming the operation, a fast-forward of the ref, never
    forced); every path the commit touches is checked against the policy's write
    resources before anything is sent; `pr=1` opens a pull request once, found
    again by its head branch, also when the worker died between the commit and
    the pull request. A single document can go to one file on a branch the same
    way;
  * **a Drive folder**, or the run's own record.

## From the chat: `@harness`

The chat reaches the harness only when someone asks for it by name, as the
first word: `@harness write a one-page onboarding brief` (alias `@agent`). It is
an extension chat keyword (`pheasant.extensions.ChatCommand`; `chat.py`), so the
core answering pipeline imports nothing of the harness: nothing is searched,
Pheasant calls no model, and the answer is a short confirmation with a link to
the run. The same keyword works over `POST /assistant/chat`, its stream and MCP
`ask_knowledge_base`.

* **What it reads** is where the reader is: the open folder, else the open
  document, else the sources the chat is scoped to (each as a `pheasant://`
  registration), else the places registered for them, newest first. With
  nothing to read it starts nothing and says how to give it something.
* **It cannot widen access.** A `pheasant://` place is registered for the
  reader only when that adds nothing they could not already read: document
  ACLs off, or the reader may manage sources. Otherwise only places they were
  granted are used, and the answer says whom to ask.
* **What kind of run** comes from the first word: *summarize*, *answer* or
  *explain*, else *build content*. The result is kept as a Pheasant artifact
  (an answer stays with the run). A code change (*change*, *fix*, *refactor*…)
  needs a repository, branch and tests, so the answer links to the Harness page
  with the request filled in.
* **Quotes say what to look for.** Phrases in quotes become the run's exact
  identifiers (searched for and cited). With no model connected the run finds
  only what it is told, so the answer asks for quotes when there are none, and
  a *build* request becomes a cited summary of what the sources say -- writing
  new prose needs a model -- and the answer says that too.
* **Once per message.** The run's client key is a digest of the conversation,
  the words and the inputs, so a resent message is the same run.

## Telemetry: the Admin page's Harness tab

`telemetry.py`; `GET /harness/telemetry`, `POST /harness/telemetry/test`,
`GET /harness/runs/{id}/trace`; MCP `harness_telemetry`,
`harness_test_telemetry`, `harness_run_trace`. Viewing telemetry and testing a
destination need `security.manage` (it spans every caller's runs); a run's own
trace is its owner's.

* **The overview** is computed from the harness tables over a window, so every
  replica shows the same thing after a restart: runs by state and kind, success
  rate, typical and slowest times, approvals waiting, unconfirmed writes, budget
  stops, model calls and tokens, tool calls and bytes read, step attempts by
  outcome, checks, deliveries by destination, every connection request by
  provider and outcome with the latest refusals, recent runs with their traces,
  and the raw `pheasant_harness_*` Prometheus series.
* **One trace per finished run**: `pheasant.harness.run` with a child span per
  phase, step attempt, connection call, effect and check, rebuilt from the
  journal and emitted exactly as sync traces are -- an `otel.span` JSON line on
  stdout (which Container Apps ships to Log Analytics) and over OTLP when a
  collector is configured. Its id is derived from the run id, a run is claimed
  in `harness_telemetry_traces` before it is emitted (so replicas emit it once),
  and the runner emits finished runs every ten seconds. Spans carry the
  workload, state, usage and the connection outcome, never the person who
  asked (a tenant that is the asker is left out).
* **Prometheus**: `pheasant_harness_runs_total`, `_run_seconds`,
  `_steps_total`, `_connection_calls_total`, `_verifications_total`,
  `_traces_emitted_total`, and gauges `_runs_live`, `_approvals_pending`,
  `_effects_uncertain` on every process's `/metrics`.
* **Destinations**, each listed whether or not it is configured, with its last
  test: Prometheus and stdout spans (always on), an OpenTelemetry collector
  (`observability.otlp_endpoint` or the standard `OTEL_EXPORTER_OTLP_*`
  variables), and Azure Monitor / Application Insights (reached through that
  collector; the row shows the connection string's ingestion host). **Send a
  test** sends one span through the real exporter -- the OpenTelemetry SDK
  when `pheasant[otel]` is installed, OTLP/JSON over HTTP otherwise -- and
  records what happened and who ran it. With no collector configured the test
  says `not_configured` rather than looking healthy.

## The page

Runs sit in their own rail that scrolls independently, with a search box and a
filter (working, waiting for someone, succeeded, failed or partial); a run's
journal, evidence and files scroll inside their panels. **Places** is its own
view: a form explaining what a place is, beside a filterable grid of cards
that say in words what each place is and what a run may do with it.
**Help it get this right** (formerly "checks and hints") asks plain questions:
lines of *It must mention* / *It must never say* become checks, the words and
code to look for are explained, and the JSON forms stay available for every
check type.

## Working through connected accounts

`connections.py`. A run can read, and deliver into, places the region reaches
through its connected accounts (`pheasant.connections`: the vault behind
Sources > Connect), registered as `connection://<name>/<path>`:

| Provider | Read (`list`, `read`) | Write (`write`) | Pinned to |
| --- | --- | --- | --- |
| `github` | `connection://gh/<owner>/<repo>[/<path>]` | one file on a named branch: `connection://gh/<owner>/<repo>/<path>?branch=<b>` | the commit; every blob is checked against its git hash |
| `google_drive` | `connection://drive/<folder id>[/<path>]` (Google Docs exported as text) | one file in a folder: `connection://drive/<folder id>/<name>` | each file's MD5, each Doc's edit time (checked before and after the export) |
| `microsoft_365` | `connection://m365/<drive id>[/<folder>]` (OneDrive, SharePoint libraries) | not offered | each file's SHA-256 (else its cTag) |

**Nothing is allowed until an operator says so.** A connection with no harness
policy is unusable here. A policy (`PUT /harness/connections/{name}/policy`,
`harness_set_connection_policy`, the Connections view; `security.manage`)
names:

* **actions**: any of `list`, `read`, `write`. `delete`, `share`,
  `permissions`, `admin`, `comment`, `merge` and `invite` can never be enabled;
* **resources** that `list`/`read` may touch, and **write_resources** where a
  delivery may land: glob patterns over `owner/repo:path`,
  `owner/repo@branch:path`, `<folder id>/<path>` or `<drive id>:<path>`;
* **limits** per run: requests, bytes, item size, listing entries. Running out
  stops the run as a budget does; it never reads as "nothing found";
* **approval** for writes: `required` by default, by an operator who is not the
  run's own requester unless `self_approval` is on.

Every change is versioned with who made it (`.../policy/history`).

**The policy is checked at every door, on every call.** At registration (a
pointer outside the policy is refused). In the broker before a tool call is
admitted. At the gateway before every request, including the verifier's re-reads
and reads served from cache. At the start of a run whose destination is a
connection. And again when a write is approved and when it is dispatched. A
narrowed or disabled policy, a removed connection or a revoked grant therefore
stops a run that is already in flight at its next call. A grep reports the files
the policy kept it from reading as unreadable, and says so.

**The credential stays in the gateway.** It is attached as an `Authorization`
header at the last moment, and only to the provider's own API host, over HTTPS.
The gateway also enforces:

* A read may only `GET`. No agent tool takes a URL, a connection or a header.
* A redirect is never followed with the credential. Graph's pre-signed
  download is fetched without it, and only from Microsoft download hosts.
* A next-page link to another host is not followed, and the listing is then
  marked incomplete, so a grep over it cannot support a count or an absence.
* A response that echoes the credential has it removed before anything reads
  the bytes.

Each request, whether allowed, served from cache, denied or failed, is one row
in `harness_connection_calls`: who, which run and purpose, which action on
which resource, the HTTP status, the bytes, and the policy version. Headers are
never recorded. The audit is at `GET /harness/connections/calls` and
`harness_connection_calls`; a run's own rows are on its detail.

**A write is the delivery, once.** A write goes through the `connection`
delivery adapter with a stable operation id:

* GitHub: a conditional contents `PUT` whose commit message names the
  operation.
* Drive: an upload tagged with `appProperties.pheasantOperation`.

After a crash, reconciliation looks for the operation id, so it never writes
again. It finds the commit even if a teammate has since edited the file, and
the teammate's edit is kept. `expected_target_version` makes the write
conditional. A run whose policy wants approval waits in `waiting` with the
exact resource, payload digest and a preview, and
`POST /harness/runs/{id}/approvals/{op}` or `harness_decide_approval` decides
it. An approval binds that payload digest and cannot widen a policy that was
narrowed in the meantime. A rejection writes nothing.

## Acceptance fixtures

`tests/test_harness_*.py`, against real PostgreSQL, real subprocess sandboxes
and a real MCP client session:

| Spec | Test |
| --- | --- |
| A01 stale index, deleted API | `test_a01_stale_index_yields_to_the_current_revision` |
| A02 capped grep, coverage | `test_a02_*` (folder and pinned git commit) |
| A03 no discovery, pointer-only | `test_a03_*` |
| A04 long packet, compaction, failover | `test_a04_*` |
| A05 clipped unit in render | `test_a05_*` |
| A06 pinned dependency, legacy data, held-out tests | `test_a06_*` |
| A07 screenshot + policy + boundaries + release note | `test_a07_*` |
| A08 crash before dispatch / after sink / after receipt | `test_a08_*` (3 cases) |
| A09 unreconcilable sink | `test_a09_*` |
| A10 revocation on resume, caches, derived views | `test_a10_*` |
| A11 duplicate starts, gap-free events, cancel mid-tool | `test_a11_*` |
| A12 staging unreachable, budget exhausted | `test_a12_*` |
| A13 destination changed after validation | `test_a13_*` |
| A14 old workflow version, lossy restore | `test_a14_*` |
| A15 fairness, shedding with retry hint | `test_a15_*` |
| A16 injected instructions, compromised model | `test_a16_*` |
| A17 sandbox lost before/after checkpoint, other host | `test_a17_*` (2 cases) |
| A18 abandoned cleanup, dirty reuse | `test_a18_*` |
| A19 required vs optional branches, late results | `test_a19_*` |
| A20 coordinator failover, duplicate callbacks | `test_a20_*` |
| A21 durable wait, revocation, fresh sandbox | `test_a21_*` |
| HTTP ≡ MCP | `tests/test_harness_surfaces.py` (runs, and connections: policies, approvals, audit) |
| Connections: results and grounding | `tests/test_harness_connections.py`: cited commit/checksum, moved branch, Drive export pinning, OneDrive download without the token, truncated listing, policy-limited grep, approved delivery, crash reconciliation, teammate edit kept, conflict, Drive delivery, sandboxed code change |
| Workspace | `tests/test_harness_workspace.py`: attachments of five formats (Markdown, PDF, Word, PNG, Python) pinned and text-extracted, landed and indexed in `uploads`, path escapes refused, a run citing an attached PDF; browsing branches, folders, Drive and OneDrive under policy; a registration pinned to a branch and folder; one commit on a new branch with a pull request, crashes before the PR, after the commit and after the receipt, a teammate's later edit kept, a forbidden path sending nothing, a moved branch never forced; documents and code changes kept as artifacts once; no credential in any of it |
| Chat, uploads, telemetry | `tests/test_harness_chat_uploads_telemetry.py`: the keyword declared, read first-word only and listed; a built-in keyword never taken over; duplicate and malformed keywords refused at load; `@harness` over HTTP starting one run from the open folder, idempotent per message, the alias; scoped sources, the code-change hand-off, help; nothing to read starts nothing; a Reader cannot widen access by chatting under ACLs; HTTP ≡ MCP; images, audio, notebooks, HTML, nested archives read exactly as `payload_text` reads them; metadata in the uploads receipts and the document each file became, searchable after sync; expansion limits; one trace per run, emitted once, never naming the asker, counted in Prometheus; the overview against the tables; telemetry for operators only; destinations without a cloud, a real local OTLP collector through the SDK and over OTLP/JSON, Azure Monitor through it, an unreachable collector reported; telemetry HTTP ≡ MCP |
| Connections: safety | `tests/test_harness_connection_safety.py`: no policy, resources, list-only, never-actions, unsupported providers, credentials in URIs, disable/remove/revoke mid-run, verifier and cached reads re-checked, write refused at start and at delivery, self-approval, approval cannot widen, exact payload, pinned hosts, redirects, foreign next links, echoed credential, tampered blob, quotas, item size, no URL-taking tool, poisoned content plus a compromised model, a leak scan (with a planted positive control) over every harness table, blob, file, view and log line, permissions |

Fakes for GitHub, Drive and Graph (`tests/harness_connection_fakes.py`) check
the credential the way the services do and record every request. A mutation
pass breaks each safety mechanism in turn (27 mutants, among them redaction,
redirect refusal, host pinning, the cached-read check, the broker's policy
check, the approval gate and self-approval, quotas, checksum and hash checks,
and reconciliation by operation id), and every one fails a test. The workspace
features had their own pass: of nine mutants (per-file write checks, forced ref
updates, branch-from-base, a second pull request, reconciliation by operation id,
browsing outside the policy, path escapes, the receipt's PR link) two survived the
first tests, which exposed a real gap -- a crash between the commit and the pull
request left no PR -- now fixed and covered; one remaining survivor (the artifact
adapter's own duplicate check) is equivalent, because reconciliation finds the
saved artifact first.

## What this build does not claim

* **Comparative parity (§9) is not measured.** There is no comparator run,
  held-out task suite or statistical decision rule here; the fixtures above
  are conformance evidence only.
* **Isolation is process-level**: a session, `prlimit`, a scrubbed
  environment and, where unprivileged user namespaces work, no network. It
  is not a VM or gVisor boundary; `pheasant harness isolation` prints the
  declared manifest. Inside Docker the network namespace may be unavailable,
  and the manifest then says so.
* **Model planner**: wired and tested with scripted models (including a
  compromised one); its quality on real tasks with a real model is not
  evaluated. Without a model, code changes apply caller-supplied candidate
  edits (`proposals`) in order.
* **Serving SLOs, load, regional recovery (§10)** are not exercised.
* **Connections are exercised against fakes**, not against live GitHub, Drive
  or Graph: the fakes implement the endpoints the adapters call and check the
  credential, but provider quirks beyond them (rate limits, Drive shortcuts,
  SharePoint lists and pages) are not covered. A code change is committed on the
  branch's current head (created from `base` when new), not rebased onto the
  exact commit the run pinned; Microsoft 365 is read-only here. A sign-in whose refresh fails surfaces as
  `HARNESS_CONNECTION_FAILED`; the harness does not start a sign-in itself.

## Dependencies

One new third-party dependency, `pillow>=10` (checked at 13.0.0,
https://github.com/python-pillow/Pillow, License-Expression `MIT-CMU`), used
for image inspection and generated figures. Its wheel bundles native
libraries, each checked against the wheel's own license file: FreeType (dual
FTL/GPLv2; used under the FTL, which is permissive but requires crediting the
FreeType project in product documentation), HarfBuzz (Old MIT), Little CMS
(MIT), libavif (BSD-2-Clause), libjpeg (IJG), libpng (libpng license),
libtiff (libtiff license), libwebp and libsharpyuv (BSD-3-Clause), OpenJPEG
(BSD-2-Clause), zstd (BSD), Brotli (MIT), liblzma (public domain), libxcb and
libXau (MIT/X11). The copyleft texts in that file cover XZ's command-line
tools and FreeType's alternative license, neither of which is shipped or
chosen. No restriction on commercial or hosted use was found.

The image installs extensions with `--no-deps`, so add Pillow to an image
that should inspect media; without it, image inspection and generated figures
return `partial` and the rest of the harness is unaffected. Markdown
rendering uses Pheasant's own `markdown-it-py` (MIT). Browser renders use a
Chromium/Playwright already on the host when present and are recorded as
`skipped` otherwise; nothing is installed for them.
