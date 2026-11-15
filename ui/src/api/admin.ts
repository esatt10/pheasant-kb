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
