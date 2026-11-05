---
name: pheasant-compose
description: Offload building a new artifact (a configuration, script, module or set of files) to a Pheasant region, which assembles it from the parts its sources document and checks it against them before returning. Use when an agent connected to Pheasant over MCP (or HTTP) has to produce something no single document contains, made of many components spread across many documents — an infrastructure configuration of a dozen or more resources, a pipeline across several tools — or is asked to "compose", "build", "scaffold" or "write the configuration for" something from a knowledge base.
---

# Offload a composition to Pheasant

Pheasant has three ways to get knowledge out of a region. Pick by what you
need back:

| You need | Call | What the region does |
|---|---|---|
| Passages to judge yourself | `search_context` (see `pheasant-retrieval`) | Ranks evidence. No model. |
| An answer that exists in the sources | `ask_knowledge_base` | Plans, retrieves, grades, writes a cited answer. |
| A **new artifact** built from documented parts | **`compose_artifact`** (this skill) | Plans one search per component, writes the whole artifact, checks it identifier by identifier, corrects what it finds. |

Offload when assembling it yourself would mean dozens of searches and reading
each component's reference page: the region does that retrieval and the
checking in one call, inside its own context budget rather than yours.

## Workflow

1. **Find the sources.** `list_sources(knowledge_base)` (or
   `describe_knowledge_base`). Note the ones that document the parts; you will
   scope to them with `source_names`.
2. **Write the request** (see *Writing the request*).
3. **Start it.** `compose_artifact(knowledge_base, request, source_names=[...])`
   returns a job at once (`job_id`, `status: "running"`).
4. **Follow it.** Call `get_job(job_id)` every 30-60 seconds until `status` is
   `succeeded` or `failed`; `progress.phase` names the last step it finished
   (plan, retrieve, decide, synthesize, review, revise) and `log` lists them all. A composition of two dozen components takes about
   4-6 minutes. (`wait=true` blocks instead, for a client whose tool calls may
   run that long.)
5. **Read the result** (`get_job(...).result`):
   - `files`: `[{name, language, content}]`, one per file the request named.
   - `not_covered`: what the sources do not document and the reader must fill
     in. Values the sources do not give are placeholder variables in the files.
   - `citations`: `[{index, source, path, section}]` for the `# [n]` comments.
   - `trace`: what the region did — the plan, and the `review` / `revise`
     steps that say what it checked and corrected.
   - A `failed` job carries `error` (for example the model was unreachable,
     or no artifact was written) and, when there is one, the partial `result`.
6. **Validate it with the artifact's own tool** — the region cannot run it:
   write the files out and run the compiler, linter or validator
   (`terraform init -backend=false && terraform validate`, `tsc`, `ruff`, ...).
7. **Review what the validator cannot judge**: references that should be
   attributes but are free variables, credentials where an identity would do,
   design that does not match a stated purpose.
8. **Iterate with `previous_job`.** Call `compose_artifact` again with the same
   `source_names`, `previous_job=<the job_id>`, and a request naming exactly
   what to change ("terraform validate reports: …; fix exactly these", or your
   review's list). The region edits the earlier artifact from its own job,
   keeping every line the request does not change, so you send only the
   changes, not the files (34 KB for a 41-resource configuration). Check the
   result's `dropped_from_previous`: it lists component types the revision no
   longer declares. If the job is gone — another region, or after a restart —
   pass `history=[{"question": <the request>, "answer": <the files>}]` instead;
   an artifact in the last turn is used as the draft to edit, whole.

## Writing the request

What the region writes is only as specific as what it is asked. Measured on a
26-component infrastructure request, a structured request with stated purposes
was complete in every run where a one-paragraph request left components out:

- **Name the output files** ("as providers.tf, variables.tf, main.tf,
  outputs.tf and terraform.tfvars.example").
- **One numbered requirement per component**, each saying **what it is for**,
  not only what it is. "Two backends combined into a pool" was built from two
  different models; "failover: two backends … so that when one is unavailable
  the other serves the same requests" was not.
- **State conventions**: one top-level resource per sub-component; every
  environment-specific value a variable with a placeholder default; components
  connected by reference; which provider or library to prefer, said neutrally
  ("use X for everything it supports, and Y only for what X does not
  provide"). An emphatic preference ("check X really has none before using
  Y") produced a resource type X does not have.
- **Do not ask for a summary table first.** Asking the answer to open with a
  requirement-by-requirement table turned it into a gap analysis and made the
  region decline the whole request in a third of runs.
- Leading the request with `@compose` is not needed here: `compose_artifact`
  always composes.

## What it will and will not do

- Every identifier is taken from the sources and checked against the
  component's own reference page. It does not invent values: anything the
  sources leave open is a placeholder, listed in `not_covered`.
- It corrects what its checks find in one edit of the draft, rechecks that
  edit, and makes at most one more. It does not loop until the artifact is
  valid — that is step 7, with the real validator.
- It judges design only against what the request states. A purpose you do not
  state is one it cannot check.
- Over HTTP the same work is `POST /assistant/chat` with `"intent":
  "compose"` (and a stream at `/assistant/chat/stream`); `ask_knowledge_base`
  with `intent="compose"` is the same composition with the full payload,
  which can exceed an MCP client's result limit.
