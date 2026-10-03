# Customize the question-answering workflow

Every question — from the UI's chat panel, from `POST /assistant/chat`, or
from the MCP tool `ask_knowledge_base` — is answered by a **workflow**. A
workflow turns a question into a grounded answer using pheasant's own search;
which one runs is configuration, and writing your own is a registration
rather than a fork.

pheasant ships three.

| Workflow | What it does | Needs |
|---|---|---|
| `knowledge-summary` | The agentic graph pinned to summarising: reads more files, less of each, and answers with what things are and how they fit together. Pick it to orient yourself in an unfamiliar corpus. | `pip install 'pheasant-kb[agent]'` |
| `agentic` | The same [LangGraph](https://langchain-ai.github.io/langgraph/) state graph, reading each question for itself — as a knowledge summary or a procedural how-to — then planning sub-queries, retrieving across every search mode, walking the knowledge graph, grading its own evidence, looping when it is thin, and verifying its citations. | `pip install 'pheasant-kb[agent]'` |
| `simple` | One retrieval pass, one model call. Predictable and fast. | nothing |

```yaml
assistant:
  workflow: auto        # auto | knowledge-summary | agentic | simple | <your plugin>
```

`auto` (the default) resolves to `agentic` when the `[agent]` extra is
installed **and** a model is reachable, otherwise `simple`. A planner with no
model to plan with is pure overhead, so the fallback is deliberate — and it is
never an error: an unknown or failing workflow degrades to `simple` with the
reason attached to the answer rather than returning a 500.

!!! note "Still no LLM in the indexing path"
    Workflows are a query-time surface. Nothing here runs during a sync, so
    re-indexing unchanged content stays byte-for-byte deterministic. With no
    provider reachable both workflows still answer **extractively** — the top
    passages with citations and graph facts intact.

---

## Two answer shapes

"What does this repository do?" and "How do I use this tool?" are not the same
question with different wording. They want different **evidence** and different
**output**, and a single prompt trying to serve both lands in the middle and
serves neither.

| | `knowledge` | `procedural` |
|---|---|---|
| The reader wants | to understand what something is and how its parts relate | to *do* something, with steps that run |
| Evidence | **breadth** — more files, less of each (`max_context_passages: 12`) | **depth** — fewer files read further (`passage_chars: 9000`) |
| Enough? | you can say what it is and what its parts are for | there is a followable sequence **and** a real example with the actual identifiers. Naming the right file is *not* enough — this is what sends the loop back to `plan` |
| Answer | direct answer, then components and how they fit, naming real paths | numbered steps, fenced code copied from the passages, never an invented API |

The intent is classified deterministically from the question before any model
call (so it costs nothing and reads the same offline), and the planner — which
sees the corpus structure — may overturn it. Either way it appears in the trace
as the `classify` step and in `counts.intent`, so you can see how a question was
read before the answer arrives.

```yaml
assistant:
  workflow: agentic
  workflow_options:
    intent: auto        # auto | knowledge | procedural
```

`knowledge-summary` is `agentic` with `intent: knowledge` pinned. To pin the
other direction, set `intent: procedural`.

---

### Length, conversations and visuals

Beside the answer *shape*, every question is also routed to a **depth**
(`short`, `medium`, `long`) and an optional **visual** (`diagram`, `image`),
and may carry the conversation so far (`history`). All three work for every
workflow, a plugin included — they are applied around it in
`assistant.answering`. See
[answer length, conversations, visuals and figures](conversations-and-visuals.md).

## The agentic graph

For latency-sensitive deployments, `assistant.workflow_options.agentic.staged_retrieval: true`
skips the initial planner call and runs one direct hybrid search. Hybrid searches text, vector,
and graph together. If the grader finds that evidence insufficient, the workflow plans one
targeted query and runs hybrid once more. Without this option, the standard plan-first flow below
remains in effect.

`hybrid` already fans out to text, vector and graph internally. When it appears
alongside those modes, the workflow issues one hybrid search instead of repeating
the standalone arms. Staged retrieval uses hybrid first, so its vector and graph
candidates participate in the same fused result without separate duplicate calls.

```
                ┌──────────────────── retry while evidence is thin
                ▼                                                │
START → classify → plan → retrieve → expand → grade ─ sufficient ─→ synthesize → verify → END
```

| Node | What it does |
|---|---|
| `classify` | Reads the question as a knowledge summary or a procedural how-to. Deterministic, model-free, and runs once — how a question was *asked* does not change because a search came back thin, so the retry edge re-enters at `plan`. |
| `plan` | Asks the model for 2–4 sub-queries and a search mode per query, given a **structural description of the corpus**: its sources and their types, directory layout, file types and languages, the vocabulary its own documents use, and the symbols its code defines. Queries reusing real paths and identifiers hit the lexical index exactly; generic paraphrases do not. Model-free or on a parse failure it falls back to the question verbatim — the graph never stalls on a bad plan. |
| `retrieve` | Fans the plan out over `hybrid`, `vector`, `text` and `graph`, merging by passage. Modes the deployment cannot serve (`vector` without embeddings) are dropped, not errored. |
| `expand` | Walks the knowledge graph out of the best hits, pulling in material lexical search would never surface. This is the step that makes a *graph* worth having. |
| `grade` | Asks the model whether the evidence answers the question, against the bar for the classified intent. "No" sends it back to `plan` with what it learned, up to `max_rounds`. |
| `read` | Rebuilds each cited **file** from its chunks — in order, with line spans, headings and artifact metadata. Search scores chunks and the SQL layer caps a preview at 500 characters; answering from those windows is how you get an answer that names exactly the right files and says nothing about them. |
| `synthesize` | Writes the answer from the selected passages only, in the shape the intent calls for, citing `[1]`, `[2]`, … |
| `verify` | Drops `[n]` markers that do not resolve to a real passage, so a hallucinated citation cannot reach the UI. |

Every step is recorded. The answer carries a `steps[]` trace (name, detail,
passage count), which the chat panel renders under the answer — so "why did it
say that" is inspectable rather than asserted.

### Tuning it

Options merge over the workflow's defaults. Set them globally:

```yaml
assistant:
  workflow: agentic
  workflow_options:
    agentic:
      max_rounds: 3
      retrieval_modes: [hybrid, vector, graph]
      staged_retrieval: true
      expand_depth: 2
      max_context_passages: 14
```

…or per request, which wins:

```bash
curl -X POST http://localhost:8765/assistant/chat \
  -H 'content-type: application/json' \
  -d '{"question": "how do we charge customers",
       "workflow": "agentic",
       "options": {"max_rounds": 1, "grade_evidence": false}}'
```

| Option | Default | Effect |
|---|---|---|
| `intent` | `auto` | Answer shape: `auto` \| `knowledge` \| `procedural`. Also sets retrieval breadth-vs-depth and the sufficiency bar. |
| `max_rounds` | `2` | plan → retrieve → grade loops before answering with what it has. |
| `retrieval_modes` | `["hybrid", "vector"]` | Search modes to fan out over. If hybrid is selected, its text/vector/graph arms subsume those standalone modes for that query. Unavailable modes are dropped. |
| `staged_retrieval` | `false` | When hybrid is available, search once with hybrid before planning; if evidence is insufficient, plan a targeted hybrid query. Without hybrid, use graph/vector first. |
| `expand_graph` | `true` | Walk the graph out of the best hits. |
| `expand_depth` | `1` | Hops the walk takes. |
| `expand_per_node` | `3` | Related documents pulled in per hit. |
| `per_query_results` | `6` | Passages fetched per query per mode. |
| `max_context_passages` | `10` | Passages offered to the answering step. |
| `grade_evidence` | `true` | Ask the model whether its evidence is sufficient. |
| `verify_citations` | `true` | Drop `[n]` markers with no matching passage. |
| `max_facts` | `12` | Graph facts surfaced alongside the answer. |

`intent` sets `max_context_passages`, `per_query_results`, `expand_per_node`
and `passage_chars` to its profile — but only where you have not set them
yourself. A key you name in `workflow_options` always wins.

### How much of each file the model sees

Both workflows send whole **files**, rebuilt from their chunks, rather than the
500-character search preview. How much comes back is decided by what the file
*is*, from its original on-disk size:

| Option | Default | Effect |
|---|---|---|
| `include_full_content` | `true` | Turn off to fall back to chunk previews. Answers get noticeably thinner. |
| `passage_chars` | `6000` | Prose allowance per file. |
| `code_passage_chars` | `24000` | Ceiling for code and config, which are **never** excerpted — a module missing its imports is not a smaller answer, it is what makes a model invent one. This exists for a vendored bundle, not for anything a person wrote. |
| `large_file_bytes` | `40000` | Original size above which prose is cut to the matched chunks and their neighbours instead of read whole. Filling the budget with unrelated chunks of a 400 KB document dilutes the evidence rather than adding to it. |
| `context_budget_chars` | `60000` | Total across all passages. Files are funded in citation order, so the best hit is never the one that gets starved. |

Excerpts are marked inline (`--- … 6 chunk(s) omitted … ---`) and the answering
prompt tells the model not to claim an excerpted file contains nothing else.

In the UI these are all under **Workflow** in the chat pane header; a
selection there applies to your next question only, so you can compare
behavior without editing config.

---

## Writing your own

A workflow implements exactly one method:

```python
from pheasant.assistant.workflows import WorkflowRequest, WorkflowResult, WorkflowStep

class CitationsOnlyWorkflow:
    name = "citations-only"

    def run(self, request: WorkflowRequest, retriever, llm) -> WorkflowResult:
        passages = retriever.search(request.question, mode="hybrid", limit=5)
        return WorkflowResult(
            answer="\n\n".join(p.snippet for p in passages),
            citations=[{"index": i + 1, "title": p.title, "node_id": p.node_id}
                       for i, p in enumerate(passages)],
            steps=[WorkflowStep("retrieve", "hybrid", len(passages))],
            workflow=self.name,
        )
```

`llm` may be `None` — a workflow **must** still return something useful in
that case. pheasant is expected to work air-gapped, and the retrieval half of
an answer needs no model.

### The retriever

`retriever` is a `PheasantRetriever`: the whole search surface, framework
agnostic, so a workflow written for LangGraph and one written for anything
else use the same toolbelt.

| Method | Returns |
|---|---|
| `search(query, mode=…, limit=…, source_name=…)` | `Passage`es from `text` / `graph` / `vector` / `hybrid`. |
| `multi_search(queries, modes=…)` | The above fanned out and merged deterministically by score then id. |
| `expand(passages, depth=…, per_node=…)` | Graph-walk neighbours of hits, as passages. |
| `neighbors(node_id, depth, edge_types)` / `slice(node_id, …)` | Raw graph navigation. |
| `facts(node_ids, limit)` | Subject–predicate–object triples around the cited nodes. |
| `content(node_id, max_chars)` | Full text behind a node. |
| `capabilities()` | Knowledge base, sources, available modes, whether vectors exist, node-type counts — and `as_prompt_context()` to put that in a system prompt. |

ACL enforcement (`security.acl_enforced`) is applied inside the retriever, so
a custom workflow inherits principal filtering without doing anything.

### Registering it

Programmatically, for an embedding application or a test:

```python
from pheasant.assistant.workflows import register_workflow
register_workflow("citations-only", CitationsOnlyWorkflow)
```

Or ship it in a package, exactly like the
[Connector SDK](../reference/connector-sdk.md):

```toml
[project.entry-points."pheasant.agent_workflows"]
citations-only = "my_pkg.flows:CitationsOnlyWorkflow"
```

Then select it anywhere a workflow name is accepted:

```yaml
assistant:
  workflow: citations-only
```

Installed plugins show up in `GET /assistant/workflows` and in the UI's
workflow picker automatically, marked as plugins.

### Overriding one node

You do not have to rewrite the agentic graph to change part of it. Its nodes
are a plain dict, and `AgenticWorkflow` takes overrides:

```python
from pheasant.assistant.workflows.agentic import AgenticWorkflow

def my_grade(state, ctx):
    return {"sufficient": len(state.get("passages", [])) >= 3}

register_workflow("agentic-cheap", lambda: AgenticWorkflow(nodes={"grade": my_grade}))
```

Each node is a function of `(state, ctx)` returning a state patch, so a
replacement is testable on its own without building a graph at all.

---

## Related

- [Ask your knowledge base (web UI)](chat-and-ui.md)
- [Vector self-search](vector-search.md) — gives the agent a second retrieval mode
- [Connector SDK](../reference/connector-sdk.md) — the same plugin pattern, for sources
