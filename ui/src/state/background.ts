import { useSyncExternalStore } from "react";
import { api, uploadPath } from "../api/client";

/**
 * Background work that outlives the panel that started it.
 *
 * An upload's bytes travel from this tab before the server has a job for them,
 * and an export started here should download when it finishes even if the
 * panel that asked for it was closed long ago. Both belong to the page, not to
 * a component: kept here, read with `useBackground`, and drawn by the jobs
 * tray, which is always mounted. So the person drops files or starts an export
 * and carries on with anything else.
 *
 * Idempotent from this side too: the same files dropped again while their
 * upload is still sending are not sent twice (the server dedupes the rest),
 * and an export asked for twice resolves to one job on the server.
 */

export type Transfer = {
  id: string;
  source: string;
  names: string[];
  signature: string;
  loaded: number;
  total: number;
  status: "uploading" | "accepted" | "failed";
  jobId?: string;
  error?: string;
};

type Snapshot = { transfers: Transfer[] };

let transfers: Transfer[] = [];
let snapshot: Snapshot = { transfers };
const listeners = new Set<() => void>();
/** Export jobs this tab asked for, by job id: download when they finish. */
const wanted = new Map<string, "pending" | "taken">();
/** The job answering each export this tab started, by `source/path/format`. */
const exportJobs = new Map<string, string>();

function emit(): void {
  snapshot = { transfers };
  listeners.forEach((listener) => listener());
}

function update(id: string, change: Partial<Transfer>): void {
  transfers = transfers.map((t) => (t.id === id ? { ...t, ...change } : t));
  emit();
}

function signatureOf(files: File[], source: string): string {
  const parts = files.map((f) => `${uploadPath(f)}:${f.size}:${f.lastModified}`).sort();
  return `${source}|${parts.join("|")}`;
}

/** Send `files` to `source` in the background. `false` when the same drop is already sending. */
export function startUpload(files: File[], source: string): boolean {
  const signature = signatureOf(files, source);
  if (transfers.some((t) => t.signature === signature && t.status === "uploading")) return false;
  const id = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  const total = files.reduce((sum, f) => sum + f.size, 0);
  transfers = [
    ...transfers,
    { id, source, names: files.map(uploadPath), signature, loaded: 0, total, status: "uploading" },
  ];
  emit();
  api
    .uploadInBackground(files, source, (loaded, sent) => update(id, { loaded, total: sent }))
    .then((accepted) => {
      update(id, { status: "accepted", loaded: total, jobId: accepted.job_id });
      // The server's job carries it from here; the tray lists that instead.
      setTimeout(() => {
        transfers = transfers.filter((t) => t.id !== id);
        emit();
      }, 4000);
    })
    .catch((error: Error) => update(id, { status: "failed", error: error.message }));
  return true;
}

export function dismissTransfer(id: string): void {
  transfers = transfers.filter((t) => t.id !== id);
  emit();
}

/** Remember that this tab wants `jobId`'s file once it is ready. */
export function wantExport(key: string, jobId: string): void {
  exportJobs.set(key, jobId);
  if (!wanted.has(jobId)) wanted.set(jobId, "pending");
}

/** The export job this tab started for `key`, if any. */
export function exportJobFor(key: string): string | undefined {
  return exportJobs.get(key);
}

/** True once per wanted job: the caller downloads it. */
export function takeExport(jobId: string): boolean {
  if (wanted.get(jobId) !== "pending") return false;
  wanted.set(jobId, "taken");
  return true;
}

export function useBackground(): Snapshot {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => snapshot,
    () => snapshot,
  );
}
