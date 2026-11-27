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
| MCP | `mcp.py`, 20 `harness_*` tools | the same operations, same refusal text and codes |
| UI | `ui/`, route `/harness` | run list, composer, lifecycle, budget, fences, delivery receipt, deliverable with clickable `[E…]` citations, verification matrix per candidate digest, evidence, steps/attempts, journal, sandboxes |
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
| HTTP ≡ MCP | `tests/test_harness_surfaces.py` |

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
