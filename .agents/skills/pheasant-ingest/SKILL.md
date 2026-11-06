---
name: pheasant-ingest
description: Add content to a Pheasant region and wait until it is searchable before relying on it. Use when an agent connected to Pheasant over MCP registers a source (register_source), adds or removes web pages (edit_source_urls), submits documents (submit_documents) or starts a sync (sync_source, start_sync_source), and then needs to search, ask or compose over what it just added — or when a sync comes back queued and the agent is unsure whether indexing has finished.
---

# Add content to Pheasant, then wait for it

Writing to a region and being able to search what you wrote are two separate
events. On a single-container region the sync runs where you asked and you
get a job to follow. On a fleet (pheasant-kb, and the Azure deployment) the
write only *publishes* a sync: an indexer in another process picks it up,
fetches, parses and embeds, and only then commits. Between the two, searches
return nothing new, and a composition or answer run in that gap is built
without the content you just added.

So do not move on after the write call returns. Wait for the index, then
check that it holds what you meant to add.

## Workflow

1. **Note where the source stands.** `describe_source(knowledge_base,
   source_name)`: keep `source.last_indexed_at` (absent for a new source) and
   `totals.documents`.
2. **Write, and ask for a sync.** `register_source(..., sync_now=true)`,
   `edit_source_urls(..., sync_now=true)` (its default), or `sync_source`.
   For `submit_documents`, keep the `submission_id` and then
   `sync_source(knowledge_base, source_name)`.
3. **Wait for it.** `wait_for_sync(knowledge_base, source_name,
   since=<last_indexed_at from step 1>)` waits on the region's side, up to 55
   seconds a call, and answers with a `state`:

   | `state` | Means | Do |
   |---|---|---|
   | `finished` | a sync finished after `since` | go to step 4 |
   | `running` | an indexer is working on it | call `wait_for_sync` again |
   | `queued` | published, no indexer has taken it yet | call `wait_for_sync` again |
   | `not_started` | nothing running or queued, nothing finished since `since` | `sync_source`, then wait again |

   `timed_out: true` means the wait ran out while work was outstanding: call
   again with the same `since`. For a new source, omit `since`.

   Do not take a write's own answer as done. On a fleet, a job from
   `start_sync_source` or `register_source(sync_now=true)` says **`succeeded`
   as soon as the sync is published**, before any indexer has touched it, with
   `result.status: "queued"`.
4. **For submissions**, `get_ingest_status(knowledge_base,
   submission_id=...)` until every receipt is `indexed` (or `rejected` /
   `failed`, which say why), then `acknowledge_ingest(knowledge_base,
   submission_id)`.
5. **Check the result, not just the state.** A finished wait carries
   `documents` (what the source holds now) and `last_sync`. Compare
   `documents` with what you added -- the number of URLs, files or submitted
   documents. `last_sync.status` is `degraded` when the sync could not read
   some items, and `last_sync.unreadable` names them with the error (a site
   refusing Pheasant's fetcher shows up here as an HTTP error per page).
   `describe_source` shows the same `last_sync` at any time. Then search for a
   phrase only the new content contains (`search_context(query,
   source_name=...)`).
6. **Say what is missing.** If documents are short, tell the user which ones
   did not arrive and do not present conclusions as if they had. Content you
   fetch yourself can be submitted with `submit_documents` and waited for the
   same way.

### On a region without `wait_for_sync`

An older region has no `wait_for_sync`. Call `describe_source` every 20-30
seconds until `source.last_indexed_at` is later than step 1;
`get_index_queue(knowledge_base)` says whether work is outstanding (a backend
that cannot list tasks answers `listing: "unavailable"` -- read its `depth`,
and do not read an empty `tasks` as "done").

## Timing

- A region that scales to zero (the Azure deployment) takes about 90 seconds
  to wake its indexer before anything is fetched: expect two or three
  `wait_for_sync` calls for a first sync.
- Web pages take about a second each to fetch, plus embedding; a 100-page
  collection is a few minutes, a few thousand pages much longer.
- `queued` for several minutes with `queued_tasks[].waiting_seconds` only
  growing means no indexer is taking work: report it with that output rather
  than waiting on.

## When a write fails

A refusal names its reason. One that says the knowledge base's database did
not answer and to **retry the call** is temporary (code
`STATE_STORE_UNAVAILABLE`): retry the same call. Any other refusal -- an
unknown source, a private URL, a malformed `since` -- fails the same way
every time, so fix the request instead. A bare "Error executing tool" with no
reason is a defect in the region: retry once, then report it.
