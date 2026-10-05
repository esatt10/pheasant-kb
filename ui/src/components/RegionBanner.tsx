import { OPEN_JOBS_EVENT, outstanding, useIndexQueue, useReady } from "../hooks/useRegion";
import { formatDuration } from "./SyncProgress";

/**
 * One line under the top bar whenever the region is not simply "ready with
 * nothing waiting" — and nothing otherwise.
 *
 * The case it exists for is the pre-claim interval. On a role-split region the
 * replica a browser talks to does not index: pressing *Sync* publishes a task,
 * and until an indexer claims it there is no job, no progress bar and no
 * change on the page. That looked exactly like a button that did nothing.
 * The other states (draining, standby, an unreachable store) are rarer and
 * louder, and are said here for the same reason: a page that silently stops
 * updating is worse than one that says why.
 */
export function RegionBanner() {
  const ready = useReady();
  const queue = useIndexQueue();

  const readiness = ready.data;
  const tasks = outstanding(queue.data);
  const waiting = tasks.filter((task) => task.state === "awaiting_claim");
  const lapsed = tasks.filter((task) => task.state === "claim_lapsed");
  const dead = tasks.filter((task) => task.state === "dead");
  const oldest = Math.max(0, ...waiting.map((task) => task.waiting_seconds ?? 0));
  const openJobs = () => window.dispatchEvent(new Event(OPEN_JOBS_EVENT));

  if (readiness && readiness.status !== "ready") {
    const text =
      readiness.status === "draining"
        ? `This replica is shutting down (draining for ${formatDuration(
            readiness.draining_for_seconds ?? 0,
          )}). Requests may start failing until another replica answers.`
        : readiness.status === "standby"
          ? "This replica is a standby: another one holds the leader lease, so scheduled and queued work runs there."
          : `This replica is not ready: ${readiness.reason ?? "unknown reason"}.`;
    return (
      <div className={`region-banner region-banner--${readiness.status === "standby" ? "info" : "error"}`} role="status">
        <span className="region-banner__dot" />
        <span>
          <b>Region {readiness.status.replace("_", " ")}.</b> {text}
        </span>
      </div>
    );
  }

  if (waiting.length > 0) {
    const publishes = readiness?.indexes_locally === false;
    return (
      <div className="region-banner region-banner--warn" role="status">
        <span className="region-banner__dot region-banner__dot--live" />
        <span>
          <b>
            {waiting.length === 1
              ? `A sync of ${waiting[0].source} is queued for an indexer`
              : `${waiting.length} syncs are queued for an indexer`}
          </b>{" "}
          · waiting {formatDuration(oldest)}.{" "}
          {publishes
            ? `This replica publishes syncs rather than running them; ${waiting.length === 1 ? "it" : "each"} will show as indexing once an indexer claims it.`
            : `No indexer has claimed ${waiting.length === 1 ? "it" : "them"} yet.`}
        </span>
        <button className="btn btn--small" onClick={openJobs}>
          Show queue
        </button>
      </div>
    );
  }

  if (lapsed.length > 0 || dead.length > 0) {
    return (
      <div className="region-banner region-banner--warn" role="status">
        <span className="region-banner__dot" />
        <span>
          {lapsed.length > 0 ? (
            <>
              <b>An indexer stopped heartbeating</b> on {lapsed.map((t) => t.source).join(", ")};
              the work will be redelivered.{" "}
            </>
          ) : null}
          {dead.length > 0 ? (
            <>
              <b>
                {dead.length} sync{dead.length === 1 ? "" : "s"} ran out of attempts
              </b>{" "}
              — fix the cause, then <code>pheasant queue requeue-dead</code>.
            </>
          ) : null}
        </span>
        <button className="btn btn--small" onClick={openJobs}>
          Show queue
        </button>
      </div>
    );
  }

  return null;
}
