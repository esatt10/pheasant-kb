import { useEffect } from "react";
import {
  OPEN_JOBS_EVENT,
  outstanding,
  regionSettled,
  useIndexQueue,
  useReady,
  useRegionStatus,
} from "../hooks/useRegion";
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
/** Minutes and seconds: a wake is about a minute and a half, which "1m" undersells. */
function wakeDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total}s`;
  const rest = total % 60;
  return rest ? `${Math.floor(total / 60)}m ${rest}s` : `${Math.floor(total / 60)}m`;
}

export function RegionBanner() {
  const ready = useReady();
  const queue = useIndexQueue();
  const region = useRegionStatus();
  // `/ready` notices a wake first (it is polled every 15 s, the status every
  // 30 s while all is well), so ask what is waking the moment it goes not
  // ready -- otherwise a wake showed as "Region not ready" for up to half a
  // minute before the banner could say what was starting.
  const notReady = ready.data?.status === "not_ready";
  const refetchRegion = region.refetch;
  useEffect(() => {
    if (notReady) void refetchRegion();
  }, [notReady, ready.data?.reason, refetchRegion]);

  const readiness = ready.data;
  const tasks = outstanding(queue.data);
  const waiting = tasks.filter((task) => task.state === "awaiting_claim");
  const lapsed = tasks.filter((task) => task.state === "claim_lapsed");
  const dead = tasks.filter((task) => task.state === "dead");
  const oldest = Math.max(0, ...waiting.map((task) => task.waiting_seconds ?? 0));
  const openJobs = () => window.dispatchEvent(new Event(OPEN_JOBS_EVENT));

  // Waking after being idle comes first: while a part is still starting,
  // "not ready" is expected and says less than what is starting and for how
  // long. Draining and standby are this replica's own states, not a wake's.
  const wake = region.data;
  if (wake && !regionSettled(wake) && readiness?.status !== "draining" && readiness?.status !== "standby") {
    const pending = wake.components.filter((part) => part.state !== "ready");
    const tone = wake.status === "unavailable" ? "error" : wake.status === "warming" ? "info" : "warn";
    const title =
      wake.status === "waking" ? "Waking up" : wake.status === "warming" ? "Warming up" : "Not answering";
    const after =
      wake.status === "waking"
        ? `${wakeDuration(wake.elapsed_seconds)} so far; a wake usually takes about ${wakeDuration(
            wake.typical_seconds,
          )}. Requests that need it may fail until then.`
        : wake.status === "warming"
          ? "Everything answers; the first answers may be slower."
          : "";
    return (
      <div className={`region-banner region-banner--${tone}`} role="status">
        <span className={`region-banner__dot${wake.status === "unavailable" ? "" : " region-banner__dot--live"}`} />
        <span>
          <b>{title}</b> · {pending.map((part) => `${part.label}: ${part.detail}`).join(" · ")}. {after}
        </span>
      </div>
    );
  }

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
