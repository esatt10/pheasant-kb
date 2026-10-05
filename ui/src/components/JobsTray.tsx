import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { JobRecord, QueueTask, SourceProgress } from "../api/types";
import { OPEN_JOBS_EVENT, QUEUE_STATE_LABEL, outstanding, useIndexQueue } from "../hooks/useRegion";
import { ProgressBar, formatDuration, formatRate, progressCaption } from "./SyncProgress";

/**
 * Everything running in the background, with real progress.
 *
 * Before this, a multi-minute first index showed up as the word "Syncing…" and
 * nothing else — indistinguishable from a hang for exactly as long as it took.
 * The tray collapses to a one-line summary and expands to per-job phase,
 * counter and the last file each one touched.
 *
 * Phase 35.1 breaks a job down **per source**. A `sync_all` over eight sources
 * was one bar over one counter, so the one source that was stuck looked exactly
 * like the seven that were fine — and the denominator moved under your feet as
 * each source discovered its own file list.
 *
 * Polling, not SSE: the server offers `/jobs/stream`, but polling survives a
 * proxy that buffers event streams. A slow idle poll discovers work claimed by
 * another process; active work switches to one-second updates. The stream is
 * there for clients that want it.
 */
export function JobsTray() {
  const [expanded, setExpanded] = useState(false);
  const queryClient = useQueryClient();
  const clear = useMutation({
    mutationFn: (jobId?: string) => (jobId ? api.clearJob(jobId) : api.clearJobs()),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });

  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api.jobs(false),
    // Fleet jobs start in another process, so an idle tray must keep a slow
    // discovery poll; once one appears, switch to one-second progress updates.
    refetchInterval: (query) => (query.state.data?.active_count ? 1000 : 5000),
    refetchIntervalInBackground: false,
    staleTime: 500,
  });

  // Work published to the index queue is not a job until an indexer claims
  // it, and on a role-split region the indexer is another process: without
  // this the tray stayed hidden for exactly the interval somebody was
  // waiting through. A claimed task is already a job somewhere, but not
  // necessarily one *this* process can see, so it is listed too.
  const queue = useIndexQueue();
  const queued = outstanding(queue.data);
  const preClaim = queued.filter((task) => task.state === "awaiting_claim");

  useEffect(() => {
    const open = () => setExpanded(true);
    window.addEventListener(OPEN_JOBS_EVENT, open);
    return () => window.removeEventListener(OPEN_JOBS_EVENT, open);
  }, []);

  const records = jobs.data?.jobs ?? [];
  const active = records.filter((job) => job.active);
  const recent = records.filter((job) => !job.active).slice(0, 5);
  const failed = recent.filter((job) => job.status === "failed");
  const stalled = active.some((job) => job.stalled);

  if (active.length === 0 && failed.length === 0 && queued.length === 0 && !expanded) {
    return null;
  }

  return (
    <div
      className={`jobs-tray${expanded ? " jobs-tray--open" : ""}${
        stalled ? " jobs-tray--stalled" : ""
      }`}
    >
      <button
        className="jobs-tray__toggle"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
        title={expanded ? "Hide background work" : "Show background work"}
      >
        {active.length > 0 || (preClaim.length === 0 && queued.some((task) => task.state === "claimed")) ? (
          <span className="spinner" />
        ) : preClaim.length > 0 ? (
          <span className="queue-glyph" aria-hidden>
            ⧗
          </span>
        ) : null}
        <span className="jobs-tray__summary">
          {active.length > 0
            ? `${summarize(active[0])}${active.length > 1 ? ` +${active.length - 1}` : ""}`
            : preClaim.length > 0
              ? `${preClaim.length === 1 ? `Sync ${preClaim[0].source}` : `${preClaim.length} syncs`} queued · awaiting an indexer`
              : queued.length > 0
                ? `Sync ${queued[0].source} · ${QUEUE_STATE_LABEL[queued[0].state]}${queued[0].claimed_by ? ` (${queued[0].claimed_by})` : ""}${queued.length > 1 ? ` +${queued.length - 1}` : ""}`
                : failed.length > 0
                  ? `${failed.length} job${failed.length === 1 ? "" : "s"} failed`
                  : "Background work"}
        </span>
        {active.length > 0 && preClaim.length > 0 ? (
          <span className="pill pill--warn">+{preClaim.length} queued</span>
        ) : null}
        <span className="jobs-tray__chevron" aria-hidden>
          {expanded ? "▾" : "▴"}
        </span>
      </button>

      {expanded ? (
        <div className="jobs-tray__body">
          {recent.length > 0 ? (
            <div className="jobs-tray__actions">
              <button className="button button--ghost" onClick={() => clear.mutate(undefined)}>
                Clear finished
              </button>
            </div>
          ) : null}
          {queued.length > 0 ? (
            <div className="queue-list">
              <div className="queue-list__title muted small">
                Index queue · {queue.data?.backend ?? "local"}
              </div>
              {queued.map((task) => (
                <QueueRow key={task.task_id} task={task} />
              ))}
            </div>
          ) : null}
          {records.length === 0 && queued.length === 0 ? (
            <p className="muted small" style={{ margin: 0 }}>
              Nothing running. Syncs, uploads and re-indexes show up here with
              live progress.
            </p>
          ) : null}
          {[...active, ...recent].map((job) => (
            <JobRow key={job.id} job={job} onClear={() => clear.mutate(job.id)} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function JobRow({ job, onClear }: { job: JobRecord; onClear: () => void }) {
  // Sources are only broken out when there is more than one: for a single
  // source the rollup and the source row carry identical numbers, and showing
  // both is noise.
  const sources = job.sources ?? [];
  const perSource = sources.length > 1;

  return (
    <div className={`job job--${job.status}${job.stalled ? " job--stalled" : ""}`}>
      <div className="job__head">
        <span className="job__label">{job.label}</span>
        <span className="job__status">{job.active ? job.progress.phase : job.status}</span>
        {!job.active ? (
          <button
            className="job__clear"
            onClick={onClear}
            aria-label={`Clear ${job.label}`}
            title="Clear"
          >
            ×
          </button>
        ) : null}
      </div>

      <ProgressBar
        fraction={
          job.active
            ? job.progress.fraction
            : job.status === "succeeded"
              ? 1
              : (job.progress.fraction ?? 0)
        }
        active={job.active}
        stalled={job.stalled}
      />

      <div className="job__detail muted small">
        {job.error ? (
          <span className="job__error">{job.error}</span>
        ) : perSource ? (
          <>
            {sources.filter((row) => !row.active).length}/{sources.length} sources done
          </>
        ) : (
          <>
            {job.progress.total
              ? `${job.progress.current} / ${job.progress.total}`
              : job.progress.current
                ? `${job.progress.current}`
                : null}
            {sources[0] ? <> · {progressCaption(sources[0])}</> : null}
            {job.progress.detail ? ` · ${job.progress.detail}` : null}
          </>
        )}
      </div>

      {perSource ? (
        <div className="job__sources">
          {sources.map((row) => (
            <SourceRow key={row.source} row={row} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

/**
 * One published sync. The pre-claim row draws a hatched bar rather than a
 * spinner on purpose: nothing is being processed, and a spinner would claim
 * that something is.
 */
function QueueRow({ task }: { task: QueueTask }) {
  const waited = task.waiting_seconds != null ? formatDuration(task.waiting_seconds) : "—";
  const caption =
    task.state === "awaiting_claim"
      ? `No indexer has claimed it yet${task.position ? ` · position ${task.position}` : ""}. It is stored in the queue and survives restarts.`
      : task.state === "claimed"
        ? `Claimed by ${task.claimed_by ?? "an indexer"}; progress appears where that indexer reports it.`
        : task.state === "claim_lapsed"
          ? "Its indexer stopped heartbeating. The task will be redelivered to another one."
          : task.state === "retry_scheduled"
            ? `Attempt ${task.attempts} of ${task.max_attempts} failed; retrying${task.last_error ? ` · ${task.last_error}` : ""}.`
            : `Out of attempts${task.last_error ? ` · ${task.last_error}` : ""}. Fix the cause, then pheasant queue requeue-dead.`;
  return (
    <div className={`queue-task queue-task--${task.state}`}>
      <div className="job__head">
        <span className={`pill queue-task__state queue-task__state--${task.state}`}>
          {QUEUE_STATE_LABEL[task.state]}
        </span>
        <span className="job__label">
          Sync {task.source}
          {task.mode !== "incremental" ? ` (${task.mode})` : ""}
        </span>
        <span className="job__status mono" title={task.task_id}>
          {waited}
        </span>
      </div>
      {task.state === "awaiting_claim" || task.state === "claim_lapsed" ? (
        <div className="queue-task__bar" />
      ) : null}
      <div className="job__detail muted small">{caption}</div>
    </div>
  );
}

function SourceRow({ row }: { row: SourceProgress }) {
  return (
    <div className={`job-source${row.stalled ? " job-source--stalled" : ""}`}>
      <div className="job-source__head">
        <span className="job-source__name">{row.source}</span>
        <span className="job-source__phase muted small">
          {row.active ? row.phase : row.status}
          {row.total ? ` ${row.current}/${row.total}` : null}
        </span>
      </div>
      <ProgressBar fraction={row.fraction} stalled={row.stalled} compact />
      <div className="job-source__meta muted small">{progressCaption(row)}</div>
    </div>
  );
}

function summarize(job: JobRecord): string {
  const { current, total, phase } = job.progress;
  const rate = job.sources?.find((row) => row.active && row.files_per_second);
  const suffix = rate?.files_per_second ? ` · ${formatRate(rate.files_per_second)}` : "";
  const eta = rate?.eta_seconds ? ` · ${formatDuration(rate.eta_seconds)} left` : "";
  if (total) return `${job.label} — ${phase} ${current}/${total}${suffix}${eta}`;
  return `${job.label} — ${phase}${suffix}`;
}
