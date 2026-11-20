/**
 * What the Admin page reads: who has access, the model catalog's embedding
 * switch, usage, and the fleet of knowledge bases. Shapes mirror
 * `services/access.py`, `services/usage.py` and `services/fleet.py`.
 */

export type Role = "Reader" | "Contributor" | "Admin";

export interface AccessPerson {
  id: string;
  email: string | null;
  subject: string | null;
  display_name: string | null;
  role: Role;
  tenant_id: string;
  organization: string;
  granted_by: string | null;
  granted_at: string | null;
  /** Granted by name and not signed in yet. */
  pending: boolean;
  you: boolean;
  can_change: boolean;
}

export interface AccessOrganization {
  tenant_id: string;
  name: string;
  domains: string[];
  default_role: Role | null;
  connected_by: string | null;
  connected_at: string | null;
  sign_in_url: string | null;
  admin_consent_url: string;
}

export interface AccessList {
  knowledge_base: string;
  caller: { principal: string; role: Role; tenant_id: string };
  grantable_roles: Role[];
  people: AccessPerson[];
  organizations: AccessOrganization[];
  operator_admins: { subject: string; role: Role; tenant_id: string }[];
  home_tenant_id: string;
}

export interface UsageTotals {
  questions: number;
  searches: number;
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  embedding_tokens: number;
  /** Null unless the caller operates the region. */
  cost: number | null;
}

export interface LatencySummary {
  count: number;
  errors: number;
  mean_ms: number | null;
  /** Upper bound of the bucket the percentile falls in, not an exact figure. */
  p50_ms: number | null;
  p95_ms: number | null;
}

export interface UsageReport {
  knowledge_base: string;
  window: { days: number; from: string; to: string };
  currency: string | null;
  cost_visible: boolean;
  totals: UsageTotals & { active_people: number };
  latency: { answer: LatencySummary; search: LatencySummary; buckets_ms: number[] };
  by_day: (UsageTotals & { day: string })[];
  by_model: (UsageTotals & { kind: string; model: string; priced: boolean | null })[];
  by_organization: (UsageTotals & {
    tenant_id: string;
    organization: string;
    active_people: number;
  })[];
  by_channel: (UsageTotals & { channel: string })[];
  people: (UsageTotals & {
    principal: string;
    tenant_id: string;
    organization: string;
    last_active: string | null;
  })[];
  people_omitted: number;
  unpriced_models: string[];
  size: {
    sources: number;
    documents: number;
    chunks: number;
    indexed_bytes: number;
    limit_gb: number | null;
    limit_used: number | null;
    /** At its limit: syncs that add content are skipped until there is room. */
    full?: boolean;
  };
}

export interface FleetKnowledgeBase {
  slot: string;
  status: "free" | "claimed";
  current: boolean;
  name: string | null;
  description: string | null;
  url: string | null;
  claimed_by: string | null;
  claimed_at: string | null;
  admins: { tenant_id: string; email: string; subject: string }[];
  version: string | null;
  heartbeat_at: string | null;
  size: {
    documents: number | null;
    indexed_bytes: number | null;
    limit_gb: number | null;
    limit_used: number | null;
    full?: boolean;
  };
  /** The operator's view only: the last thirty days. */
  usage: {
    days: number;
    questions: number;
    searches: number;
    active_people: number;
    tokens: number;
    cost: number | null;
    answer_p95_ms: number | null;
    last_active: string | null;
  } | null;
}

export interface CapacityRequest {
  id: string;
  kind: "slots" | "storage";
  slot: string;
  requested_by: string;
  quantity: number;
  reason: string | null;
  status: "open" | "approved" | "declined" | "fulfilled";
  created_at: string;
  resolved_at: string | null;
  resolved_by: string | null;
  note: string | null;
}

export interface FleetReport {
  slot: string;
  operator: boolean;
  currency: string | null;
  capacity: { slots: number; claimed: number; free: number };
  knowledge_bases: FleetKnowledgeBase[];
  requests: CapacityRequest[];
  can_create: boolean;
  can_resolve: boolean;
}

/** Which slice of the usage totals a drill-down follows. */
export interface UsageSelection {
  principal?: string;
  organization?: string;
  model?: string;
}

/** Which calls a drill-down reads: the questions and searches the totals
 *  count (default), or every request the ledger observed, polling included. */
export type TraceCalls = "questions" | "all";

/** How far back the interaction ledger reaches for the selected window. */
export interface TraceRetention {
  ledger_days: number | null;
  complete: boolean;
  note: string | null;
}

export interface UsageSessionSummary {
  /** A conversation id, or `trace:<id>` for a call made outside one. */
  session: string;
  first_at: string;
  last_at: string;
  calls: number;
  errors: number;
  duration_ms: number;
  principal: string | null;
  modality: string | null;
}

export interface UsageSessions {
  window: { days: number; from: string };
  filter: UsageSelection;
  sessions: UsageSessionSummary[];
  truncated: boolean;
  retention: TraceRetention;
  /** A Log Analytics query for this selection's spans, for the window. */
  kql: string | null;
  kql_note: string | null;
}

export interface AnswerTraceStep {
  name: string;
  detail: string;
  model: string | null;
  passages: number | null;
  duration_seconds: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
}

export interface AnswerTrace {
  models?: { chat: string | null; decision: string | null };
  routing?: { mode?: string; outcome?: string; selected?: string | null; decision_model?: string | null } | null;
  route?: { intent?: string; depth?: string; visual?: string } | null;
  workflow?: string | null;
  degraded?: boolean;
  citations?: number;
  tokens?: { input_tokens: number; output_tokens: number };
  server_ms?: number | null;
  steps?: AnswerTraceStep[];
}

export interface UsageCall {
  trace_id: string;
  span_id: string;
  operation: string;
  modality: string;
  principal: string | null;
  organization: string | null;
  started_at: string;
  duration_ms: number | null;
  status: string;
  question: string | null;
  answer: string | null;
  result_count: number | null;
  answer_trace: AnswerTrace | null;
  http_status: number | null;
}

export interface UsageSession {
  session: string;
  window: { days: number; from: string };
  filter?: { calls: TraceCalls; match: string | null };
  first_at: string;
  last_at: string;
  calls: UsageCall[];
  truncated: boolean;
  retention: TraceRetention;
  kql: string;
}

/** The operator's controls over one knowledge base (`GET /region/controls`). */
export interface RegionControls {
  knowledge_base: string;
  /** Whether the caller may change them: an operator admin only. */
  can_change: boolean;
  access: {
    /** The kill switch: on, everyone but an operator admin is refused. */
    suspended: boolean;
    reason: string | null;
    since: string | null;
    by: string | null;
  };
  spend: {
    month: string;
    resets_on: string;
    /** The amounts are an operator admin's; null for anyone else. */
    currency: string | null;
    spent: number | null;
    limit: number | null;
    default_limit: number | null;
    custom_limit: boolean;
    remaining: number | null;
    percent_used: number | null;
    reached: boolean;
    enforced: boolean;
    unpriced_models: string[];
    note: string | null;
  };
  /** Deep research (`@detailed` / `@deep`): one answer's limit and how many run at once. */
  deep_research: {
    /** What one deep answer may spend; an operator admin's figure, null for anyone else. */
    answer_spend_limit: number | null;
    default_answer_spend_limit: number | null;
    custom_answer_spend_limit: boolean;
    currency: string | null;
    /** Research wraps up when spend plus the projected write reaches this share. */
    wrap_up_percent: number;
    enforced: boolean;
    /** Deep answers one api process runs at once; 0 is no separate cap. */
    max_concurrent: number;
    default_max_concurrent: number;
    custom_max_concurrent: boolean;
    note: string | null;
  };
  updated_at: string | null;
  updated_by: string | null;
}

export interface RegionControlsChange {
  suspended?: boolean;
  reason?: string | null;
  /** null returns to the deployment's default, for this and the two below. */
  monthly_spend_limit?: number | null;
  deep_answer_spend_limit?: number | null;
  max_concurrent_deep_answers?: number | null;
}

/** A price per million tokens; null where the model states none. */
export interface ModelPrice {
  input: number | null;
  output: number | null;
  cached_input: number | null;
}

/** What each catalog model costs, and who changed it (`GET /models/prices`, operator admin). */
export interface ModelPrices {
  knowledge_base: string;
  currency: string;
  unit: string;
  models: {
    id: string;
    kind: "chat" | "decision" | "embedding";
    label: string;
    model: string;
    catalog: ModelPrice;
    override: Partial<ModelPrice> | null;
    effective: ModelPrice | null;
  }[];
  history: { at: string; by: string; model: string; from: ModelPrice | null; to: ModelPrice | null }[];
  updated_at: string | null;
  updated_by: string | null;
  note: string;
}
