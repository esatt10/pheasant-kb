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
3. **Read what the write returned** and wait the matching way:

   | The response has | The region is | Wait by |
   |---|---|---|
   | `status: "queued"` / `queued_tasks`, anywhere in it | a fleet | `describe_source` until `last_indexed_at` is later than step 1 |
   | a job whose `result.status` is `"queued"` | a fleet | the same: the job only published the sync |
   | a job still `running` | single-process | `get_job(job_id)` until `status` is `succeeded` or `failed` |
   | `sync_result` with `status: "healthy"` | finished already | go to step 4 |

   On a fleet, a job from `start_sync_source` or `register_source(sync_now=
   true)` says **`succeeded` as soon as the sync is published**, before any
   indexer has touched it. Read its `result`: `status: "queued"` means the
   work has not started.

   On a fleet, `get_index_queue(knowledge_base)` says whether work is still
   outstanding (`depth`, or each task's `state`). A backend that cannot list
   tasks answers `listing: "unavailable"`: read its `depth`, and do not read
   an empty `tasks` as "done".
4. **For submissions**, `get_ingest_status(knowledge_base,
   submission_id=...)` until every receipt is `indexed` (or `rejected` /
   `failed`, which say why), then `acknowledge_ingest(knowledge_base,
   submission_id)`.
5. **Check the result, not just the status.** A sync finishes `healthy` when
   it read *some* of its items. Compare `totals.documents` from
   `describe_source` with what you added — the number of URLs, files or
   submitted documents. A web collection whose site refuses Pheasant's
   fetcher indexes only the pages that answered, and still reports `healthy`.
   Then search for a phrase that only the new content contains
   (`search_context(query, source_name=...)`).
6. **Say what is missing.** If documents are short, tell the user which ones
   did not arrive and do not present conclusions as if they had. Content you
   fetch yourself can be submitted with `submit_documents` and waited for the
   same way.

## Timing

- Poll every 20-30 seconds. Faster polling does not make indexing faster, and
  each call spends a tool turn.
- A region that scales to zero (the Azure deployment) takes about 90 seconds
  to wake its indexer before anything is fetched.
- Web pages take about a second each to fetch, plus embedding; a 100-page
  collection is a few minutes, a few thousand pages much longer.
- If `last_indexed_at` has not moved and the queue reports no work after
  several minutes, the sync did not run or failed: report it with the
  `get_index_queue` and `describe_source` output rather than waiting on.

## When a write fails

A transient failure (the region waking, a dropped database connection) can
surface as a bare "Error executing tool". Retry the same call once or twice
before concluding it is refused: a deliberate refusal carries its reason in
the error text (an unknown source, a private URL, a URL already listed), and
retrying it changes nothing.
