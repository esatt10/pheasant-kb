export type RunState =
  | "accepted" | "running" | "waiting" | "verifying" | "delivering" | "reconciling"
  | "succeeded" | "partial" | "failed" | "cancelled";

export interface Registration {
  id: string;
  uri: string;
  resolver: "git" | "folder" | "pheasant";
  label: string;
  operations: string[];
  classification: string;
  registered_by: string;
  created_at: number;
  revoked: boolean;
  authorized: boolean;
  freshness_policy: Record<string, unknown>;
}

export interface Budget {
  limits: Record<string, number>;
  spent: Record<string, number>;
  reserved: Record<string, number>;
  remaining: Record<string, number>;
}

export interface Effect {
  operation_id: string;
  adapter: string;
  target: string;
  state: string;
  dispatch_count: number;
  downstream_id: string | null;
  disposition: string | null;
}

export interface RunSummary {
  run_id: string;
  state: RunState;
  workload: string;
  tenant: string;
  actor: string;
  question: string;
  created_at: number;
  updated_at: number;
  last_seq: number;
  result_status?: string | null;
}

export interface Handoff {
  artifacts: Record<string, string> | null;
  verified_criteria: string[];
  unverified_criteria: string[];
  coverage_gaps: string[];
  evidence_count: number;
  executed: { argv: string[]; purpose?: string; exit_status?: number; environment?: Record<string, unknown> }[];
  unresolved: { step: string; action: string; outcome: string }[];
}

export interface RunResult {
  status: string;
  reason?: string;
  candidates?: Record<string, string> | null;
  receipt?: Record<string, string | null>;
  handoff?: Handoff;
  blocked?: { registration_id: string; reason: string; criteria?: string[] }[];
  unverified?: Record<string, string[]>;
  conflict?: string;
  next_action?: string;
  integrity?: { ok: boolean; missing: { object: string; kind: string }[] };
}

export interface RunStatus {
  run_id: string;
  state: RunState;
  workload: string;
  class: string;
  tenant: string;
  actor: string;
  question: string;
  last_seq: number;
  fence: number;
  lease: { owner: string | null; until: number | null; held: boolean };
  cancel_requested: boolean;
  workflow_version: number;
  versions: Record<string, string | number>;
  wait: { reason: string; input?: string[] } | null;
  result: RunResult | null;
  steps: Record<string, number>;
  budget: Budget;
  effects: Effect[];
  created_at: number;
  updated_at: number;
}

export interface Step {
  step_id: string;
  action: string;
  status: string;
  required: boolean;
  effect_class: string;
  candidate: string | null;
  current_attempt: string | null;
  args: Record<string, unknown>;
  outcome: string | null;
  observation: Record<string, unknown>;
  policy: { allowed?: boolean; reason?: string | null; capability?: string };
  created_at: number;
}

export interface Evidence {
  id: string;
  registration_id: string | null;
  revision: string | null;
  locator: Record<string, unknown>;
  method: string;
  status: string;
  extract: string;
  flags: Record<string, boolean>;
  step_id: string | null;
}

export interface Artifact {
  digest: string;
  kind: string;
  name: string;
  media_type: string;
  parent_digest: string | null;
  step_id: string | null;
  provenance: Record<string, unknown>;
  created_at: number;
}

export interface Verification {
  id: string;
  criterion_id: string;
  artifact_digest: string;
  grader: string;
  grader_version: string;
  outcome: "pass" | "fail" | "skipped" | "inconclusive";
  observed: Record<string, unknown>;
  input_digests: string[];
  environment: Record<string, unknown>;
  created_at: number;
}

export interface Sandbox {
  generation: string;
  profile: string;
  state: string;
  image_digest: string;
  created_at: number;
  destroyed_at: number | null;
  disposal_ms: number | null;
  disposal: { reason: string; confirmed: boolean; processes_killed: number[]; within_deadline: boolean } | null;
}

export interface Attempt {
  attempt_id: string;
  step_id: string;
  fence: number;
  worker: string;
  status: string;
  started_at: number;
  finished_at: number | null;
}

export interface Criterion {
  id: string;
  type: string;
  target: string;
  required: boolean;
}

export interface RunDetail extends Omit<RunStatus, "steps"> {
  request: Record<string, unknown>;
  criteria: Criterion[];
  steps: Step[];
  attempts: Attempt[];
  evidence: Evidence[];
  artifacts: Artifact[];
  verifications: Verification[];
  revisions: Record<string, { revision: string; kind: string; entries: number; time_bound: boolean }>;
  sandboxes: Sandbox[];
  isolation: Record<string, unknown>;
  integrity: { ok: boolean; missing: { object: string; kind: string }[] };
  withheld_evidence?: string[];
}

export interface RunEvent {
  seq: number;
  type: string;
  data: Record<string, unknown>;
  created_at: number;
}

export interface ArtifactContent {
  digest: string;
  kind: string;
  name: string;
  media_type: string;
  content: string | null;
  content_base64: string | null;
  bytes: number;
}
