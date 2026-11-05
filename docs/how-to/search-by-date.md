# Search and ask by date

Every indexed document carries two dates, and a search or a question can be
limited to documents last updated in a range of either.

| Clock (`date_field`) | Column | Means | Where it comes from |
|---|---|---|---|
| `source` (default) | `source_modified_at` | When the document's **source of truth** says it last changed | the source itself — see below |
| `ingested` | `ingested_at` | When **this region** took in the content it holds now | the indexer; moves only when the bytes change |

## Ask a question about a period

In a chat message, anywhere:

```text
@since 2025-01 what changed in credential rotation?
@since 2024 @before 2025 how was rotation done then?
@before 2024-07-01 what did the runbook say about the vault?
```

Or on the request (`POST /assistant/chat`, MCP `ask_knowledge_base`):

```json
{"question": "how is rotation done?", "updated_after": "2024", "updated_before": "2025"}
```

Every search the answer runs, including follow-up rounds and graph walks,
stays inside the window. Each cited passage says when its document was last
updated (`citations[].source_modified_at`), the model is told the same in each
passage's header, and the answer's `date_window` echoes what was applied. A
keyword wins over the request's own bound.

## Search raw

`POST /search` and MCP `search_context` take the same three fields:

```json
{"query": "credential rotation", "updated_after": "2025-01-01", "date_field": "source"}
```

Hits then carry `provenance.source_modified_at` and `provenance.ingested_at`,
and the response a `date_window` block. The window narrows every arm: in the
text arm's SQL, and for the vector and graph arms against the document each hit
belongs to, with the same over-fetch every other filter uses, so `max_results`
still means "give me this many".

## What a bound means

- `updated_after` is **on or after**, `updated_before` is **strictly before**,
  so two adjacent windows never count one document twice.
- A bound is a year (`2025`), a month (`2025-03`), a day (`2025-03-01`), or an
  ISO 8601 timestamp. A bare date is midnight UTC, the *start* of the period
  it names: `updated_after: 2025, updated_before: 2026` is exactly 2025.
- An unreadable bound, an unknown `date_field`, or a start that is not before
  its end is refused with `INVALID_DATE_RANGE`, never ignored. An ignored
  bound would answer a question about one month from the whole corpus.

## Where the source date comes from

| Source | `source_modified_at` |
|---|---|
| folder, single file, memory, uploads | the file's modification time |
| `repository` | the committer date of the **last commit that touched the file**; for a file with uncommitted changes, or untracked, its modification time. An archive member takes its archive's date. |
| Google Drive | `modifiedTime` |
| `web_collection` | the page's `Last-Modified` header, or **undated** when the server sends none |
| `api` | the item's `updated_at` (or `mtime`), in ISO 8601, an HTTP date, or epoch seconds/milliseconds |

Every spelling is normalized to `YYYY-MM-DDTHH:MM:SSZ` in UTC. Anything that
cannot be read, or reads as an implausible year (a zero epoch), is stored as
**unknown** rather than guessed. In particular a page fetched without a
`Last-Modified` header is not dated "now": the fetch time is not a source date.

**An undated document is outside every window.** The response's
`date_window.undated_hits` counts the candidates left out for that reason, so
"nothing matched" and "nothing was dated" read differently. To date such
content, use `date_field: ingested` instead.

### Repositories and shallow clones

A checkout's file times are when it was checked out, which is why a repository
is dated by git. The first sync after the upgrade walks history once, newest
first, stopping as soon as every file is dated. After that only the files in
`git diff <last walked HEAD> <HEAD>` are re-dated, so an incremental sync reads
a handful of commits. A **shallow clone** lists every file at its boundary
commit as added there. Those files are left undated rather than dated to
wherever the clone was cut: deepen the clone (`git fetch --deepen`) to date
them.

### Keeping dates current without re-indexing

A date can move without the bytes moving: a file touched, a Drive document
renamed, a new commit. After every sync pheasant compares each document's
stored date with what the listing (or git) reports now, and updates only the
rows that differ. Nothing is re-read or re-embedded (the sync's details report
`redated_artifacts`), and an unchanged corpus writes nothing. The same pass
dates every document indexed before this feature existed, on the first sync
after the upgrade. `ingested_at` is backfilled from `last_indexed_at` at
migration.

## What this is not

pheasant holds **one version** of each document, the current one. A window
selects documents by when their current version last changed; it does not
recover what a document said before it was edited. "What did the runbook say
in March?" is answerable when the March text is still the current text, or
when the old text lives in its own file. It is not answerable once the file
has been overwritten. Two features do keep history: agent memory's `as_of`
(a correction supersedes, and the old record stays readable at a past instant;
see [agent memory](agent-memory.md)) and a git repository's own log, outside
pheasant. Corpus-level `as_of` stays `unsupported` in the
[readiness contract](../stress-test-readiness.md).
