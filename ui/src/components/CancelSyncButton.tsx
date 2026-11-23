import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "../api/client";
import type { CancelSyncResult } from "../api/types";

/**
 * Stop one source's sync, whether it is running or still waiting for an
 * indexer (`POST /sources/{name}/cancel`).
 *
 * The answer arrives before the stop does: a running sync stops at its next
 * item, a moment later and maybe in another process, so the button says
 * "Stopping…" until the views it refreshes show the run gone. A queued one
 * never starts, and says so. Nothing indexed so far is lost either way.
 */
export function CancelSyncButton({
  source,
  className = "btn btn--small",
  label = "Cancel sync",
  onCancelled,
}: {
  source: string;
  className?: string;
  label?: string;
  onCancelled?: (result: CancelSyncResult) => void;
}) {
  const queryClient = useQueryClient();
  const [answer, setAnswer] = useState<string | null>(null);
  const cancel = useMutation({
    mutationFn: () => api.cancelSync(source),
    onSuccess: (result) => {
      setAnswer(result.stage === "queued" ? "Cancelled" : "Stopping…");
      for (const key of [["pipelines"], ["pipeline", source], ["index-queue"], ["jobs"], ["sources"]]) {
        queryClient.invalidateQueries({ queryKey: key });
      }
      onCancelled?.(result);
    },
  });
  const error = cancel.error as Error | null;
  // Nothing left to stop is not a failure the person needs to act on: the
  // sync finished (or was stopped by someone else) between render and click.
  const finished = error instanceof ApiError && error.code === "SYNC_NOT_RUNNING";
  return (
    <>
      <button
        data-permission="sources.manage"
        type="button"
        className={className}
        disabled={cancel.isPending || answer !== null || finished}
        onClick={() => cancel.mutate()}
        title="Stop this sync. Everything it indexed so far is kept, and the next sync continues from there."
      >
        {cancel.isPending ? "Cancelling…" : finished ? "Already finished" : (answer ?? label)}
      </button>
      {error && !finished ? <span className="error small">{error.message}</span> : null}
    </>
  );
}
