import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { IndexQueueStatus, QueueTask, RegionStatus } from "../api/types";

/**
 * The region's own view of whether work is waiting: `/ready` for this
 * replica, `/queue` for syncs it published and nobody has claimed yet.
 *
 * Shared by the banner, the jobs tray and the Sources page through one query
 * key each, so three components ask the server once. A server that predates
 * `/queue` answers 404; that reads as "no queue" (`retry: false`), which is
 * what such a server has.
 */
export function useIndexQueue() {
  return useQuery({
    queryKey: ["index-queue"],
    queryFn: api.indexQueue,
    retry: false,
    // A pre-claim task's clock is the thing being shown, so it ticks every
    // second while anything waits and drops to a slow discovery poll after.
    refetchInterval: (query) => (outstanding(query.state.data).length > 0 ? 1000 : 5000),
    refetchIntervalInBackground: false,
  });
}

export function useReady() {
  return useQuery({
    queryKey: ["ready"],
    queryFn: api.ready,
    retry: false,
    refetchInterval: (query) => (query.state.data?.status === "ready" ? 15000 : 3000),
    refetchIntervalInBackground: false,
  });
}

/**
 * What is still waking after the region scaled up from idle. Polled every
 * 3 s while anything is, and every 30 s once everything is ready, so the
 * banner leaves as soon as the last part answers and costs nothing after.
 *
 * ``urgent`` (the replica's `/ready` says it is not ready) holds it at 3 s
 * too: a fetch that fails mid-restart keeps the last status, "ready", and
 * waiting 30 s for the next one left a wake showing as "Region not ready"
 * for half a minute (watched on Azure).
 */
export function useRegionStatus(urgent = false) {
  return useQuery({
    queryKey: ["region-status"],
    queryFn: api.regionStatus,
    retry: false,
    refetchInterval: (query) => (!urgent && regionSettled(query.state.data) ? 30000 : 3000),
    refetchIntervalInBackground: false,
  });
}

/** Nothing to tell anyone: ready, or a server with no status to give. */
export function regionSettled(status: RegionStatus | null | undefined): boolean {
  return !status || status.status === "ready";
}

/** Tasks a person should be told about — everything the queue still holds. */
export function outstanding(status: IndexQueueStatus | undefined): QueueTask[] {
  return status?.enabled ? status.tasks : [];
}

/**
 * Tasks waiting for an indexer that the queue can count but not list (NATS).
 * `tasks` is empty there, and reading it alone hid a requested sync for as
 * long as it waited behind another source's long one.
 */
export function unlistedWaiting(status: IndexQueueStatus | undefined): number {
  if (!status?.enabled || status.listing !== "unavailable") return 0;
  return Math.max(0, Number(status.depth?.pending ?? 0));
}

export const QUEUE_STATE_LABEL: Record<QueueTask["state"], string> = {
  awaiting_claim: "awaiting an indexer",
  retry_scheduled: "retry scheduled",
  claimed: "indexer claimed",
  claim_lapsed: "claim lapsed",
  dead: "dead-lettered",
};

/** Opens the jobs tray from anywhere (the banner's "Show queue"). */
export const OPEN_JOBS_EVENT = "pheasant:open-jobs";
