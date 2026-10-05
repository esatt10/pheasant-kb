import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { IndexQueueStatus, QueueTask } from "../api/types";

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

/** Tasks a person should be told about — everything the queue still holds. */
export function outstanding(status: IndexQueueStatus | undefined): QueueTask[] {
  return status?.enabled ? status.tasks : [];
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
