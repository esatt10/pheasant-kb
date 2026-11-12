# Keep what pheasant generates: artifacts

An **artifact** is something pheasant generated that you decided to keep: the
files a `@compose` answer wrote, an answer saved as a report (with its
diagram), or a whole conversation saved as a thread. Kept artifacts are
**knowledge**, not attachments: they live in a source called `artifacts`, so
they are searchable, browsable in the taxonomy view, linked in the graph to
the documents they were built from, and removable like any other document.

Everything below works the same from the web UI, the HTTP API and MCP.

## Keep an answer

Keeping is a choice you make after an answer arrives:

| Where | How |
|---|---|
| Web UI, one answer | **Save files** under a `@compose` answer, **Save as report** under any other. |
| Web UI, a conversation | **Save thread** beside the composer: every question and answer since the last **New topic**. |
| Web UI, a diagram | **Save** in a diagram's toolbar (beside **Redraw as**) keeps it as drawn, redraws included. |
| HTTP | `save: true` on `POST /assistant/chat`, or `POST /artifacts` afterwards. |
| MCP | `save: true` on `ask_knowledge_base` / `compose_artifact`, or `save_artifact`. |

To keep every composed answer without asking, turn on **Auto-save composed
answers** at the top of the Artifacts panel (or `configure_artifacts` /
`PUT /artifacts/settings` with `{"auto_save": true}`). It is off by default
and applies to every user of the region.

## What an artifact holds

Each artifact is a folder in the `artifacts` source:

```
artifacts/<yyyy>/<mm>/<dd>/<person>/<conversation>/<title>--<id>/
  README.md        what it is, who kept it, and its sources as links
  <its files>      a compose answer's files, by name
  report.html      the rendered report, when it holds a diagram
```

Date first, then the person who kept it, then the conversation it came from:
a day's folder stays small however many people use the region, and a
conversation's artifacts stay together. The Artifacts panel lists them the same
way, newest day first, then by person.

The record behind the folder also keeps the artifact's **trace**: the
conversation and channel (`ui`, `api` or `mcp`) it came from, the answer and
trace ids, the `@` keywords and the routed intent, how sure the answering loop
was (`decisions`: sufficiency and acceptance with their probabilities, and
whether the answer was degraded), and the steps it took. The panel shows a
summary; `get_artifact` and the Parquet export have all of it.

### Evidence, and how it is numbered

An artifact's `README.md` lists every document its answer cited, as a link to
that document's path. pheasant turns each link into a `references` edge in the
graph, so a graph walk goes from the artifact to what it was built from:
**the citations are the relationships**.

A saved **thread** gives its evidence one numbering. Each answer numbers its own
sources from `[1]`, so `[2]` in the first answer and `[2]` in the third are
usually different documents. In a thread, each distinct source (a document and
section) has one number, and every `[n]` in the transcript is that number;
the Sources list says which turns cited each one:

```
1. docs · sync-engine.md (turn 1, 3)
2. docs · chunking.md (turn 1)
3. docs · graph.md (turn 3)
```

Each turn also keeps its answer exactly as written, its own numbers beside the
thread's, and the map between them (`turns[n].citation_map`). A `@compose`
turn's files are code and are never rewritten; the transcript gives the
mapping for their `[n]` comments ("This turn's files cite its own numbering:
[1] = [3], [2] = [1]").

## Find artifacts

- **Artifacts panel**: the Notebook's **Artifacts** tab (a section of its own on
  a phone). Open one to see its files rendered: highlighted code, Markdown,
  an HTML report running in a sandbox that cannot reach the network, and the
  diagram drawn by pheasant's viewer. **Download**, **.zip**, **Export HTML**
  and **Markdown** save it. **Live** / **Archived** switch the list.
- **Search**: artifacts are part of every search and answer unless you leave
  them out. The composer's **use artifacts** box, `artifacts: "exclude"` on
  `POST /search`, `/assistant/chat`, `search_context` or `ask_knowledge_base`,
  leaves them out; `"only"` searches nothing else.
- **Taxonomy view**: **Browse all** (or **Folder** on an open artifact) opens
  the `artifacts` source as folders.
- **API / MCP**: `GET /artifacts` / `list_artifacts`, filtered by `owner`,
  `conversation_id` or `kind`, and `archived=true`.

An artifact is readable the moment it is saved. It becomes searchable once the
`artifacts` source is indexed, which by default starts when you save
(**Index right after saving**); until then the list marks it
**not indexed yet**.

On a fleet, saving commits the artifact and a durable file-publication intent
in one database transaction. The response does not wait for the indexer to
wake, accept files, embed them or update the graph. The indexer retries pending
intents after a restart, and batches file removals for an artifact. Deleting
an artifact excludes its documents from retrieval immediately; physical file,
vector and graph cleanup follows on the writer. Saving, searchability and
physical cleanup are separate milestones.

## Remove artifacts

| To remove | Do |
|---|---|
| One artifact | **Delete** in its viewer, `DELETE /artifacts/{id}`, `delete_artifact`. |
| Several | Tick them in the Artifacts panel, then **Delete** (or **Archive**). |
| By person, conversation, kind or date | `POST /artifacts/delete` / `delete_artifacts` with `owner`, `conversation_id`, `kind` or `before`. MCP's is a dry run unless told otherwise; check what it lists, then run it with `dry_run: false`. |
| A whole folder (a day, a person, a conversation) | **Remove folder** in the taxonomy view, or `archive_documents` with `folders: ["artifacts/2026/11/09/ada"]`. |

**Archive** is the reversible form: an archived artifact leaves search and the
graph but is kept, listed under **Archived**, and **Restore** brings it back.
Delete cannot be undone.

## Settings

The Artifacts panel's **Settings**, `GET`/`PUT /artifacts/settings` and MCP
`get_artifact_settings` / `configure_artifacts` read and change the same
values. They are held by the region, so a change reaches every user and every
replica at once.

| Setting | Default | What it does |
|---|---|---|
| Saving enabled (`enabled`) | on | Whether anything can be saved. |
| Auto-save composed answers (`auto_save`) | off | Keep every `@compose` answer without asking. |
| Index right after saving (`index_on_save`) | on | Start the `artifacts` sync when something is saved. |
| Also index every (`index_interval_seconds`) | 30 min | The schedule that indexes artifacts as a backstop. |
| Give way to content syncs (`yield_to_content`) | on | The scheduled artifacts sync waits while other sources sync (four intervals at most). |
| Archive after (`retention_days`) | 0 (keep) | Archive artifacts older than this, on the scheduler's beat. |
| `retention_kinds` | all | Which kinds retention archives: `files`, `report`, `thread`. |

The config file's `artifacts:` section ([Configuration](../configuration.md))
sets the defaults the region starts from.

## For agents

An agent that composes with `compose_artifact` gets the artifact back in the
job's result; with `save: true` it is also kept, and `artifact_id` names it.
To keep a conversation, pass the `ask_knowledge_base` results it holds as
`turns` to `save_artifact(kind="thread")`. To keep a diagram `create_visual`
returned, pass it as `visual` with its `citations`. `list_artifacts` and
`get_artifact` read them back; `search_context(artifacts="only")` searches them.

## Export

`pheasant export parquet` writes `saved_artifacts.parquet` beside the other
tables: one row per artifact, with its files, citations and trace in
`payload_json` ([export schema](../reference/export-schema.md)).

## Related

- [Sync schedules and removing content](sync-schedules.md): when sources,
  artifacts included, are indexed.
- [Answer length, conversations & visuals](conversations-and-visuals.md)
- [MCP tools](../mcp_tools.md) and [HTTP API](../reference/http-api.md)
