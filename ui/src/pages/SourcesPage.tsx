import { Fragment, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "../api/client";
import type { QueueTask, SourceRecord } from "../api/types";
import { SourceSyncProgress, formatDuration } from "../components/SyncProgress";
import { QUEUE_STATE_LABEL, outstanding, useIndexQueue } from "../hooks/useRegion";
import { AddSourceWizard } from "../sources/AddSourceWizard";
import { TaxonomyOutline } from "../sources/TaxonomyOutline";
import { QuickAdd } from "../components/QuickAdd";

const SYNC_MODES = ["incremental", "full", "validate_only", "repair"];

/**
 * Whether this source extracts a section taxonomy, read from the stored
 * config. Only those sources have an outline to show, so the button appears
 * only for them rather than leading everyone to an empty panel.
 */
export function hasTaxonomy(source: SourceRecord): boolean {
  if (!source.config_json) return false;
  try {
    const config = JSON.parse(source.config_json) as { taxonomy?: { enabled?: boolean } };
    return Boolean(config.taxonomy?.enabled);
  } catch {
    return false;
  }
}

export function SourcesPage() {
  const queryClient = useQueryClient();
  const sources = useQuery({
    queryKey: ["sources"],
    queryFn: api.sources,
    // Sync now runs in the background (wait: false) rather than blocking
    // the request that started it, so this is how its progress actually
    // reaches the page. A role-split indexer starts in another process, so a
    // slow idle poll is required to discover it; active work updates each second.
    refetchInterval: (query) => (query.state.data?.some((s) => s.syncing) ? 1000 : 5000),
  });
  const [quickAdd, setQuickAdd] = useState(false);
  const [showWizard, setShowWizard] = useState(false);
  const [editingSource, setEditingSource] = useState<SourceRecord | null>(null);
  const [patch, setPatch] = useState<string | null>(null);
  const [outlineFor, setOutlineFor] = useState<string | null>(null);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["sources"] });
    queryClient.invalidateQueries({ queryKey: ["graph"] });
    queryClient.invalidateQueries({ queryKey: ["overview"] });
  };

  const queue = useIndexQueue();
  const queuedBySource = new Map<string, QueueTask>();
  for (const task of outstanding(queue.data)) {
    if (!queuedBySource.has(task.source)) queuedBySource.set(task.source, task);
  }
  const [notice, setNotice] = useState<SyncNotice | null>(null);

  // `wait: false` answers one of three words, and only `syncing` means this
  // process started work. The other two used to be dropped, so a sync that
  // was queued for an indexer, or folded into one already running, looked
  // like a button that did nothing.
  const sync = useMutation({
    mutationFn: ({ name, mode }: { name: string; mode: string }) => api.syncSource(name, mode),
    onSuccess: (data, { name }) => {
      const status = (data as { status?: string }).status;
      const tasks = (data as { queued_tasks?: string[] }).queued_tasks ?? [];
      if (status === "queued") {
        setNotice({
          kind: "warn",
          text: `${name}: queued for an indexer${tasks[0] ? ` (task ${tasks[0]})` : ""}. This replica publishes syncs rather than running them; the source shows as indexing once an indexer claims it.`,
          awaiting: tasks,
        });
      } else if (status === "already_syncing") {
        setNotice({
          kind: "info",
          text: `${name} is already syncing — another job holds it, so no second sync was started. Changes since it began are picked up by the next one.`,
        });
      } else {
        setNotice(null);
      }
      invalidate();
      queryClient.invalidateQueries({ queryKey: ["index-queue"] });
    },
    onError: (error, variables) => {
      if (error instanceof ApiError && error.code === "REGION_BUSY") {
        setNotice({
          kind: "warn",
          text: `${variables.name}: the region is busy (${error.message}). Nothing was started; it is safe to retry.`,
          retry: variables,
        });
      } else {
        setNotice({ kind: "error", text: `${variables.name}: ${(error as Error).message}` });
      }
    },
  });
  const disable = useMutation({
    mutationFn: (name: string) => api.disableSource(name),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (name: string) => api.removeSource(name),
    onSuccess: invalidate,
  });
  const promote = useMutation({
    mutationFn: (name: string) => api.promoteSource(name, false),
    onSuccess: (data) => setPatch(data.yaml_patch),
  });

  return (
    <div className="page page--wide">
      <div className="page__header">
        <h1>Sources</h1>
        <div className="button-row">
          <button className="btn" onClick={() => setShowWizard(true)}>
            Advanced…
          </button>
          <button className="btn btn--primary" onClick={() => setQuickAdd(true)}>
            + Add source
          </button>
        </div>
      </div>

      {sources.isLoading && (
        <p className="muted">
          <span className="spinner" /> Loading sources…
        </p>
      )}
      {notice && !superseded(notice, queue.data?.tasks) ? (
        <div className={`banner banner--${notice.kind} sync-notice`} role="status">
          <span>{notice.text}</span>
          {notice.retry ? (
            <button className="btn btn--small" onClick={() => sync.mutate(notice.retry!)}>
              Retry
            </button>
          ) : null}
          <button className="btn btn--small btn--ghost" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </div>
      ) : null}
      {sources.isError && (
        <div className="banner banner--error">{(sources.error as Error).message}</div>
      )}

      <div className="table-scroll">
        <table className="data-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Type</th>
            <th>Path</th>
            <th>Status</th>
            <th style={{ textAlign: "right" }}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {sources.data?.map((source: SourceRecord) => (
            <Fragment key={source.id}>
            <tr className={source.enabled ? "" : "row--disabled"}>
              <td>
                {source.name}
                {source.uploaded_archives?.length ? (
                  <div className="muted small">ZIP: {source.uploaded_archives.join(", ")}</div>
                ) : null}
              </td>
              <td>
                <span className="pill">{source.type}</span>
              </td>
              <td className="path-cell" title={source.path}>
                {source.path}
              </td>
              <td className="muted small">
                {source.syncing ? (
                  <span
                    className={`thinking${source.progress?.stalled ? " thinking--stalled" : ""}`}
                  >
                    <span className="spinner" />{" "}
                    {source.progress?.stalled ? "no progress" : (source.progress?.phase ?? "syncing…")}
                  </span>
                ) : queuedBySource.has(source.name) ? (
                  <QueueState task={queuedBySource.get(source.name)!} />
                ) : source.sync_error ? (
                  <span className="error" title={source.sync_error}>
                    sync failed
                  </span>
                ) : (
                  (source.last_status ?? "—")
                )}
                {source.repository?.managed ? (
                  <div
                    className={
                      source.repository.fresh || repositoryVerificationPending(source)
                        ? ""
                        : "error"
                    }
                    title={repositoryFreshnessTitle(source)}
                  >
                    remote{" "}
                    {source.repository.fresh
                      ? "current"
                      : repositoryVerificationPending(source)
                        ? "verification pending"
                        : "not verified"}
                    {source.repository.indexed_commit
                      ? ` · ${source.repository.indexed_commit.slice(0, 8)}`
                      : ""}
                  </div>
                ) : null}
              </td>
              <td className="actions-cell">
                <SyncControl
                  disabled={Boolean(source.syncing)}
                  onSync={(mode) => sync.mutate({ name: source.name, mode })}
                />
                <button className="btn btn--small" onClick={() => setEditingSource(source)}>
                  edit
                </button>
                {hasTaxonomy(source) ? (
                  <button
                    className="btn btn--small"
                    onClick={() => setOutlineFor(outlineFor === source.name ? null : source.name)}
                  >
                    {outlineFor === source.name ? "hide outline" : "outline"}
                  </button>
                ) : null}
                <button className="btn btn--small" onClick={() => promote.mutate(source.name)}>
                  promote
                </button>
                <button className="btn btn--small" onClick={() => disable.mutate(source.name)}>
                  disable
                </button>
                <button
                  className="btn btn--small btn--danger"
                  onClick={() => remove.mutate(source.name)}
                >
                  remove
                </button>
              </td>
            </tr>
            {/* The whole point of Phase 35.1: a first index of a large source
                takes minutes to hours, and the row above can only say that it
                is happening. This says how fast, how far, and how long — and
                distinguishes "slow" from "stuck". */}
            {source.progress ? (
              <tr className="row--progress">
                <td colSpan={5}>
                  <SourceSyncProgress row={source.progress} />
                </td>
              </tr>
            ) : null}
            {outlineFor === source.name ? (
              <tr>
                <td colSpan={5}>
                  <TaxonomyOutline sourceName={source.name} />
                </td>
              </tr>
            ) : null}
            </Fragment>
          ))}
          {sources.data?.length === 0 ? (
            <tr>
              <td colSpan={5} className="muted small" style={{ textAlign: "center" }}>
                No sources yet.
              </td>
            </tr>
          ) : null}
        </tbody>
        </table>
      </div>

      <p className="muted small" style={{ marginTop: 14 }}>
        <strong>+ Add source</strong> takes a path, URL or glob and infers the rest.{" "}
        <strong>Advanced…</strong> exposes every field the YAML schema has — include and
        exclude globs, chunking, branch policy, sync triggers, and connector settings for
        Google Drive or any installed plugin.
      </p>

      {quickAdd ? (
        <QuickAdd
          onClose={() => setQuickAdd(false)}
          onAdded={() => {
            setQuickAdd(false);
            invalidate();
          }}
        />
      ) : null}
      {showWizard && <AddSourceWizard onClose={() => setShowWizard(false)} />}
      {editingSource && (
        <AddSourceWizard source={editingSource} onClose={() => setEditingSource(null)} />
      )}

      {patch && (
        <div className="modal-scrim" onClick={() => setPatch(null)}>
          <div className="modal modal--narrow" onClick={(e) => e.stopPropagation()}>
            <header className="modal__header">
              <h2>YAML patch</h2>
              <button className="btn btn--ghost btn--icon" onClick={() => setPatch(null)}>
                ✕
              </button>
            </header>
            <p className="muted small">
              Add this to your pheasant.yaml to make the source durable across restarts.
            </p>
            <pre className="content-block">{patch}</pre>
          </div>
        </div>
      )}
    </div>
  );
}

function repositoryFreshnessTitle(source: SourceRecord): string {
  const repository = source.repository;
  if (!repository) return "";
  return [
    repository.remote_url,
    repository.tracking_ref ? `tracking: ${repository.tracking_ref}` : null,
    repository.remote_commit ? `remote: ${repository.remote_commit}` : null,
    repository.local_commit ? `checkout: ${repository.local_commit}` : null,
    repository.indexed_commit ? `indexed: ${repository.indexed_commit}` : null,
  ]
    .filter(Boolean)
    .join("\n");
}

function repositoryVerificationPending(source: SourceRecord): boolean {
  return Boolean(
    source.repository?.managed &&
      !source.repository.fresh &&
      !source.repository.indexed_commit &&
      source.last_status === "registered",
  );
}

function SyncControl({
  onSync,
  disabled,
}: {
  onSync: (mode: string) => void;
  disabled?: boolean;
}) {
  const [mode, setMode] = useState("incremental");
  return (
    <span className="sync-control">
      <select
        className="text-input text-input--small"
        value={mode}
        onChange={(e) => setMode(e.target.value)}
        style={{ width: "auto" }}
        disabled={disabled}
      >
        {SYNC_MODES.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </select>
      <button
        className="btn btn--small btn--primary"
        onClick={() => onSync(mode)}
        disabled={disabled}
      >
        {disabled ? "syncing…" : "sync"}
      </button>
    </span>
  );
}

interface SyncNotice {
  kind: "info" | "warn" | "error";
  text: string;
  retry?: { name: string; mode: string };
  /** Task ids this notice is about; it stands down once none still awaits a claim. */
  awaiting?: string[];
}

/**
 * A "queued for an indexer" notice is true until an indexer claims the task,
 * and then it is the stale half of the story: the row's badge already says
 * "indexer claimed". Hiding it then, rather than leaving it for a dismiss, is
 * what keeps the page from contradicting itself.
 */
function superseded(notice: SyncNotice, tasks: QueueTask[] | undefined): boolean {
  if (!notice.awaiting?.length || !tasks) return false;
  return !tasks.some(
    (task) => notice.awaiting!.includes(task.task_id) && task.state === "awaiting_claim",
  );
}

/** A source whose sync was published and is not (yet) a job in this process. */
function QueueState({ task }: { task: QueueTask }) {
  const tone = task.state === "claimed" ? "pill--accent" : task.state === "dead" ? "pill--danger" : "pill--warn";
  return (
    <span className="source-queue-state" title={`${task.task_id}${task.last_error ? ` · ${task.last_error}` : ""}`}>
      <span className={`pill ${tone}`}>
        {task.state === "awaiting_claim" ? "⧗ queued · " : ""}
        {QUEUE_STATE_LABEL[task.state]}
        {task.state === "claimed" && task.claimed_by ? ` · ${task.claimed_by}` : ""}
      </span>
      {task.waiting_seconds != null && task.state !== "dead" ? (
        <span className="mono">{formatDuration(task.waiting_seconds)}</span>
      ) : null}
    </span>
  );
}
