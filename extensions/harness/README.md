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
| MCP | `mcp.py`, 26 `harness_*` tools | the same operations, same refusal text and codes |
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
| Connections: safety | `tests/test_harness_connection_safety.py`: no policy, resources, list-only, never-actions, unsupported providers, credentials in URIs, disable/remove/revoke mid-run, verifier and cached reads re-checked, write refused at start and at delivery, self-approval, approval cannot widen, exact payload, pinned hosts, redirects, foreign next links, echoed credential, tampered blob, quotas, item size, no URL-taking tool, poisoned content plus a compromised model, a leak scan (with a planted positive control) over every harness table, blob, file, view and log line, permissions |

Fakes for GitHub, Drive and Graph (`tests/harness_connection_fakes.py`) check
the credential the way the services do and record every request. A mutation
pass breaks each safety mechanism in turn (27 mutants, among them redaction,
redirect refusal, host pinning, the cached-read check, the broker's policy
check, the approval gate and self-approval, quotas, checksum and hash checks,
and reconciliation by operation id), and every one fails a test.

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
  SharePoint lists and pages) are not covered. Writes are one file per
  delivery; a multi-file commit or a pull request is not offered. Microsoft 365
  is read-only here. A sign-in whose refresh fails surfaces as
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
