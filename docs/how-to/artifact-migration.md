# Replace memory with artifacts

Artifacts are the canonical surface for scoped assertions, retrieval rules,
generated reports, files and conversation threads. The UI's former Memory
route redirects to Artifacts. Existing `/memory` routes and MCP memory tools
remain callable, with deprecation metadata and canonical replacements.

## Existing knowledge

Source registration adopts `type: memory` as `type: artifact` in place. It
preserves source names, directories, record/file IDs and original Markdown
bytes. Unchanged chunk content keeps its deterministic identity; enabling
taxonomy can replace derived chunks as sections are extracted. The database records the original source definition once.
Keep existing persistent mounts, including mounts named `/memory`.
The `memory_*` tables remain derived compatibility projections; they do not
hold a second, independently writable content store.

Use `POST /artifacts/records` or MCP `record_artifact` for assertions and
alias/preference/exclusion rules. `GET /artifacts/records` and
`list_artifact_records` list visible records; `current_only: false` includes
replacement history. Corrections name `supersedes` and retain scope/writer.
Session, user and organization scopes, expiry, reinforcement, formation,
candidate evidence/review, deterministic compaction, explicit synthesis and
query steering retain their existing policies and logging.

The canonical settings are `artifacts.learning`; an existing `memory` block
is accepted as a fallback. The UI edits the canonical block through
`PATCH /config/section/artifacts`. New source definitions use `type: artifact`.
`artifacts.taxonomy_enabled` defaults to true: typed records participate in
taxonomy, ordinary document browsing and graph traversal. Existing stable IDs
and historical graph edges remain meaningful.

## Evidence

Artifact prose uses a compact first-use numbering shared by its rendered
report and exports. Evidence identity includes source, document, revision and
chunk/location metadata when supplied. Two sources with the same relative
path remain distinct. Inline, fenced and indented code and existing Markdown
links are preserved. Executable files retain their exact bytes; a compact
local-to-artifact citation map explains their original numbers. Structured
diagrams use the same global numbers as the report, including diagram-only
evidence. Unknown references are explicit instead of linking to an unrelated
renumbered source.

Saving captures bounded indexed evidence with the artifact, subject to
visibility. HTML links open the pinned excerpt, and the UI/MCP view can follow
the qualified source to its current indexed content. Markdown and ZIP exports
contain local evidence targets; ZIP also contains individual evidence files.
Unavailable or withheld evidence is explicit. Source-qualified
`pheasant://document/<source>/<path>` links in index pages resolve through the
graph instead of guessing among matching filenames.

## Outputs and jobs have separate lifetimes

Saved artifacts are published by default; `published: false` saves a draft.
`replaces: <id>` publishes a new output and supersedes the old one. Immutable
versions and publication receipts live in the state database independently of
operational jobs. Age retention archives unpublished drafts, while published
outputs remain until explicitly deprecated or replaced. Session record expiry
and declared validity continue to apply.

Reading a published artifact includes `revision_id` and `publication_status`.
Its immutable content is available at
`GET /artifacts/publications/<id>/versions/<revision_id>`, including after job deletion.

Upgrade migration also seals completed evaluation reports and previously
promoted tuning bundles before job retention runs, preserving existing outputs.
Ranking promotion commits the active overlay and its publication atomically;
a failed publication leaves the previous configuration in force.

Sync/maintenance job snapshots are shared through the configured SQLite or
PostgreSQL state backend. Evaluation runs and tuning experiments retain their
existing resumable progress, replay/metric/trial history and cold exports.
The scheduler prunes terminal history after `artifacts.job_retention_days`
(default 30; 0 disables age cleanup). Active jobs cannot be deleted.

Delete terminal evaluation history with `DELETE /evaluation/runs/<id>` or
MCP `delete_evaluation_run`; delete tuning history with the existing prune
route or `delete_tuning_experiment`. Published reports and promoted ranking
configurations remain readable/exportable under Artifacts. Evaluation reports
are not indexed into their own evidence corpus. Deleting a generic job also
leaves its published output unchanged.

## Restore a known ranking configuration

Before changing a mounted base configuration, use **Tuning → Save configured
base**, `POST /tuning/baselines/<name>`, or MCP `save_retrieval_baseline`.
This creates a self-contained named bundle of the configured ranking values,
including reciprocal-rank fusion parameters. Apply it through the ordinary
bundle promotion action, even after reconfiguration or job deletion.
Other accepted tuned bundles remain promotable through that same action.
Rollback to `default` still means the currently configured base; a named base
preserves an earlier one explicitly. Retiring the active tuning output is
refused until another configuration is selected.

All migrations are additive and idempotent. Back up state and existing source
mounts using the normal deployment procedure before rolling out a new image.
Do not remove legacy source directories or rename IDs as a migration step.

Local checks and measured performance are recorded in
[Artifact transition validation](../artifact-transition-validation.md).
