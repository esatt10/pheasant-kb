import { request, qs } from "@pheasant/ui/api/client";
import type {
  ArtifactContent, Registration, RunDetail, RunEvent, RunStatus, RunSummary,
} from "./types";

const json = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });

export const harnessApi = {
  contract: () => request<Record<string, unknown>>("/harness/contract"),
  registrations: () => request<{ registrations: Registration[] }>("/harness/registrations"),
  register: (body: { uri: string; label?: string; operations?: string[]; freshness_policy?: Record<string, unknown> }) =>
    request<Registration>("/harness/registrations", json(body)),
  runs: () => request<{ runs: RunSummary[] }>(`/harness/runs${qs({ limit: 60 })}`),
  start: (body: { request: Record<string, unknown>; client_key: string }) =>
    request<RunStatus>("/harness/runs", json(body)),
  detail: (runId: string) => request<RunDetail>(`/harness/runs/${encodeURIComponent(runId)}/detail`),
  events: (runId: string, afterSeq = 0) =>
    request<{ events: RunEvent[]; next_after_seq: number; state: string }>(
      `/harness/runs/${encodeURIComponent(runId)}/events${qs({ after_seq: afterSeq, limit: 500 })}`),
  step: (runId: string) => request<RunStatus>(`/harness/runs/${encodeURIComponent(runId)}/step`, { method: "POST" }),
  resume: (runId: string, input: Record<string, unknown>) =>
    request<RunStatus>(`/harness/runs/${encodeURIComponent(runId)}/resume`, json({ input })),
  cancel: (runId: string) => request<RunStatus>(`/harness/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" }),
  resolveEffect: (runId: string, operationId: string, disposition: "applied" | "not_applied") =>
    request<RunStatus>(`/harness/runs/${encodeURIComponent(runId)}/effects/${encodeURIComponent(operationId)}/resolve`,
      json({ disposition })),
  artifact: (runId: string, digest: string) =>
    request<ArtifactContent>(`/harness/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(digest)}`),
};
