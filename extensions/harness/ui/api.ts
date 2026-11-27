import { apiBase, errorDetail, qs, request, requestHeaders, uploadPath } from "@pheasant/ui/api/client";
import type {
  ArtifactContent, AttachResult, BrowseResult, ConnectionCall, ConnectionPolicy, ConnectionsOverview, Registration,
  RunDetail, RunEvent, RunStatus, RunSummary, TelemetryDestination, TelemetryOverview, TraceSpan, UsableConnection,
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
  decide: (runId: string, operationId: string, decision: "approve" | "reject", note = "") =>
    request<RunStatus>(`/harness/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(operationId)}`,
      json({ decision, note })),
  connections: () => request<ConnectionsOverview>("/harness/connections"),
  usable: () => request<{ connections: UsableConnection[] }>("/harness/connections/usable"),
  browse: (connection: string, params: { location?: string; path?: string; ref?: string }) =>
    request<BrowseResult>(`/harness/connections/${encodeURIComponent(connection)}/browse${qs(params)}`),
  registerWith: (body: { uri: string; label?: string; scope?: Record<string, unknown> }) =>
    request<Registration>("/harness/registrations", json(body)),
  // Multipart, so not `request()`: the browser sets the boundary; the bearer token is still sent.
  attach: async (files: File[], options: { label?: string; keepInUploads?: boolean } = {}): Promise<AttachResult> => {
    const form = new FormData();
    for (const file of files) form.append("files", file, uploadPath(file));
    if (options.label) form.append("label", options.label);
    form.append("keep_in_uploads", String(options.keepInUploads ?? true));
    form.append("sync", "true");
    const response = await fetch(`${apiBase()}/harness/attachments`, {
      method: "POST", headers: requestHeaders(undefined, false), body: form,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorDetail(body, `${response.status} ${response.statusText}`));
    return body as AttachResult;
  },
  setPolicy: (name: string, body: Omit<ConnectionPolicy, "connection" | "provider" | "version" | "updated_by" | "updated_at">) =>
    request<{ policy: ConnectionPolicy; previous_version: number }>(
      `/harness/connections/${encodeURIComponent(name)}/policy`, { method: "PUT", body: JSON.stringify(body) }),
  disablePolicy: (name: string) =>
    request<{ policy: ConnectionPolicy }>(`/harness/connections/${encodeURIComponent(name)}/policy/disable`, { method: "POST" }),
  connectionCalls: (params: { run_id?: string; connection?: string; limit?: number } = {}) =>
    request<{ calls: ConnectionCall[]; summary: Record<string, number> }>(`/harness/connections/calls${qs(params)}`),
  telemetry: (windowHours = 168) => request<TelemetryOverview>(`/harness/telemetry${qs({ window_hours: windowHours })}`),
  testTelemetry: (destination: string) =>
    request<{ destination: string; ok: boolean; outcome: string; message: string } & Partial<NonNullable<TelemetryDestination["last_test"]>>>(
      "/harness/telemetry/test", json({ destination })),
  trace: (runId: string) => request<{ run_id: string; trace_id: string; spans: TraceSpan[] }>(`/harness/runs/${encodeURIComponent(runId)}/trace`),
  artifact: (runId: string, digest: string) =>
    request<ArtifactContent>(`/harness/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(digest)}`),
};
