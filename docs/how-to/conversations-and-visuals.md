# Answer length, conversations, visuals and figures

The assistant reads three things off every question, and each is a choice you
can also make yourself:

| Axis | Values | Decided by |
|---|---|---|
| **intent** | `knowledge` · `procedural` | rules, then the planner ([answer shapes](agent-workflows.md#two-answer-shapes)) |
| **depth** | `short` (default) · `medium` · `long` | rules, then the planner; or pinned |
| **visual** | `none` · `diagram` · `image` | rules; or pinned |

None of them costs a model call to decide. The rules are deterministic, so an
offline region and a connected one read a question the same way, and the
agentic planner — which already returns JSON for its search plan — may
overrule the *depth* rule in the same reply, but never a value you pinned.
`pheasant_assistant_route_total{intent,depth,visual,depth_by}` counts every
routed question; a planner that overrules the rule most of the time is telling
you the rule is wrong.

## Answer length

* **short** is the answer pheasant has always given. Its prompts, retrieval
  sizes and output limits are unchanged, so every existing evaluation
  baseline stays comparable.
* **medium** reads more (16 passages instead of the intent's default), and
  asks for a direct opening plus 3–5 headed sections.
* **long** is written in three steps: an **outline** (one call returning a
  direct overview and up to five sections, each naming the passages it will
  use), the **sections** (written in parallel, each from *only* its own
  passages, under their original `[n]` numbers so citations still verify),
  and a deterministic **stitch**. A section that errors or runs past
  `deadline_seconds` (150) is written from its passages instead, and the
  step list says which — a long answer that times out whole is worse than a
  medium one. The single-pass workflow answers `long` as `medium` in one call
  and says so.

Phrases that route: "briefly", "tl;dr" → short; "overview of", "compare",
"trade-offs", "walk me through" → medium; "in detail", "comprehensive",
"deep dive", "write-up", "report on" → long. Pin it per request with
`depth`, or with the length picker under the chat box. Every knob of a
profile is an ordinary workflow option, so anything you set explicitly
(`max_context_passages`, `max_sections`, `section_concurrency`,
`deadline_seconds`, …) wins over the profile.

## Conversations

pheasant keeps **no conversation state** — the MCP transport is stateless by
design, and only the caller knows which turns belong together. So the caller
sends the recent turns, and the region decides what they may do:

```json
POST /assistant/chat
{"question": "and what about the vault?",
 "history": [{"question": "how does credential rotation work?",
              "answer": "It rotates nightly at 02:00 [1]."}]}
```

1. **The question is found.** A follow-up ("what about it?", "the second
   one", "tell me more", or anything four words or shorter after a turn) is
   rewritten into a standalone search question — by the model when one is
   connected (one short call, only for follow-ups), and otherwise by joining
   it to the question it follows. Only *retrieval* sees the rewrite; the
   question answered is always yours. The response carries `search_question`.
2. **The evidence is kept.** The previous question is searched again and its
   hits join at half weight (`retrieved_by: "carried"`). Re-searched, not
   fetched by id: ids arrive from the caller, and re-running the question
   applies the same ACL, criteria and memory policy as any search.
3. **The model sees the conversation**, with each earlier answer's `[n]`
   markers stripped — those numbers belong to a citation list that no longer
   exists — and cut to 1,200 characters.

At most the last 6 turns are used and 50 are accepted. A question with no
history is answered byte-for-byte as before. The UI sends history
automatically; **New topic** under the chat box starts a fresh context
without clearing the thread, and **New conversation** clears both.

## Visuals

Ask for one ("draw a diagram of the release process", "visualize how
retrieval fuses its arms") or use **Draw a diagram** under an answer — for
all its cited passages, or ◇ next to one source for *that passage only*. Over
the API it is `visual: "diagram"` on a chat request, or on demand:

```json
POST /assistant/visual
{"request": "the release process", "node_ids": ["chunk:docs:release.md:…"]}
```

A visual is **grounded or it is declined**. The model returns a small diagram
spec — nodes and edges, each listing the passages (`cites`) that support it —
and pheasant checks it the way it checks `[n]` markers: a citation to a
passage that was not given is dropped, an element left with none is kept but
marked `inferred` (drawn dashed), and a diagram that is mostly inference is
declined with a reason rather than drawn. With no model connected, the
diagram is the graph's own edges between the cited sources, grounded by
construction. `visual.mermaid` is the same diagram as Mermaid text, for
hosts that render it and for copying out. The model never writes markup:
every renderer draws from the spec.

`kind` is `flow`, `sequence`, `hierarchy`, `concept` or `timeline`, chosen by
the model unless you pass one. In a streamed answer the text arrives first and
the visual follows as its own `visual` event, so a reader never waits for the
picture to read the answer.

## Figures: images your documents reference

When a Markdown or HTML document shows an image the corpus holds —
`![Architecture](img/arch.png)`, `![[flow.png]]`, `<img src="…" alt="…">` —
that link becomes an `embeds` edge from the document to the image artifact
(see [multi-modal ingest](multimodal-ingest.md#images-your-documents-reference)).
An answer citing that document then carries the image as a numbered **figure**:

```json
"figures": [{"figure": 1, "node_id": "file:docs:images/arch.png:branch=none",
             "relative_path": "images/arch.png", "caption": "…", "alt": "Architecture",
             "cited_in": [2], "shown": true}]
```

The model may place `[fig:1]` on its own line where the image helps; a marker
that names no figure is dropped, exactly like a dangling `[n]`. Asking to be
*shown* something ("show me the architecture diagram", "find the screenshot of
…") routes to `visual: "image"`, which answers with the figures themselves —
and, if none of the cited sources holds one, draws a diagram from the same
passages and says it was drawn rather than found.

Bytes are served by `GET /media?node_id=…` and the MCP `get_image` tool, under
the same read check as every other content operation.

## MCP Apps

`ask_knowledge_base`, `create_visual` and `get_image` each declare
`_meta.ui.resourceUri: "ui://pheasant/knowledge-view.html"` (and the
deprecated flat `ui/resourceUri`, for hosts built on the draft). A host that
supports the [MCP Apps extension](https://github.com/modelcontextprotocol/ext-apps)
renders that view — the answer with its citations, a diagram, or an image —
in a sandboxed iframe; a host that does not reads the same JSON and loses
nothing it had before.

The view is one self-contained file (`mcp_server/apps/knowledge_view.html`):
no network, no CDN, and nothing from a result is ever parsed as HTML. It
reaches back to the region only through the host (`tools/call get_image`),
so an image is read under the same ACL over MCP as over HTTP. Clicking a
diagram node sends `ui/message` — "tell me more about …" — which the host
may turn into the next turn.

**pheasant's own UI hosts the same view.** The chat panel loads it from
`GET /assistant/apps/knowledge-view` into `<iframe sandbox="allow-scripts">`
and speaks the host half of the protocol, so a diagram looks the same in
Claude as it does in pheasant, and a fix to it lands in both.

## Related

- [Customize the answering workflow](agent-workflows.md)
- [MCP tools](../mcp_tools.md) · [HTTP API](../reference/http-api.md)
- [Multi-modal ingest](multimodal-ingest.md)
