// Shapes returned by the pheasant HTTP API. Graph node/edge payloads are
// intentionally open (Record) because enrichment attaches type-specific fields.

export interface GraphNode {
  id: string;
  type?: string;
  label?: string;
  relative_path?: string;
  source_id?: string;
  hash?: string;
  summary?: string;
  provenance?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface GraphLink {
  source: string;
  target: string;
  key?: number;
  type?: string;
  confidence?: number;
  [key: string]: unknown;
}

export interface NodeLinkGraph {
  nodes: GraphNode[];
  links: GraphLink[];
  total_nodes?: number;
  total_links?: number;
  /** Nodes matching the active type/source filter, before the node limit. */
  matched_nodes?: number;
  filtered?: boolean;
  truncated?: boolean;
}

export interface GraphSlice {
  node_id: string;
  depth: number;
  nodes: GraphNode[];
  links: GraphLink[];
  /** Hop distance from the slice's center, nearest wins. Center is 0. */
  depths?: Record<string, number>;
  /** True when the neighbour budget omitted nodes from this slice. */
  truncated?: boolean;
}

export interface NeighborEntry {
  node_id: string;
  depth: number;
  edge_types: string[];
  path: string[];
  node: GraphNode;
}

export interface NeighborsResponse {
  node_id: string;
  depth: number;
  neighbors: NeighborEntry[];
}

export interface ExplainResponse {
  node_id: string;
  type?: string;
  label?: string;
  explanation: string;
  provenance?: Record<string, unknown>;
  node?: GraphNode;
}

export interface SourceRecord {
  id: string;
  name: string;
  type: string;
  path: string;
  enabled: number | boolean;
  config_json?: string;
  last_status?: string;
  last_indexed_at?: string;
  checkpoint?: Record<string, unknown> | null;
  /** ZIPs currently stored in this UI-owned upload source. */
  uploaded_archives?: string[];
  /** A background sync (registered/triggered with `wait: false`) is running now. */
  syncing?: boolean;
  /** Error from the most recent *background* sync, cleared by the next one. Independent of `last_status`. */
  sync_error?: string | null;
  /** The running job behind `syncing`, with its phase and counter. */
  job?: JobRecord | null;
  /** This source's own slice of that job — its counter, not the whole run's. */
  progress?: SourceProgress | null;
  /** Commit evidence for a repository materialized from a remote URL. */
  repository?: {
    managed?: boolean;
    remote_url?: string;
    requested_ref?: string | null;
    tracking_ref?: string;
    branch?: string;
    local_commit?: string;
    remote_commit?: string;
    indexed_commit?: string;
    fresh?: boolean;
  };
  [key: string]: unknown;
}

export interface SourceTypeInfo {
  id: string;
  label: string;
  description: string;
  /** "required" — the connector reads this path; "unused" — schema ceremony. */
  path_role: "required" | "unused";
  builtin: boolean;
}

export interface SourceTypeCatalog {
  types: SourceTypeInfo[];
  /** What to send as `path` for a type whose path_role is "unused". */
  placeholder_path: string;
}

export interface SourceWritePayload {
  name?: string;
  type?: string;
  path?: string;
  description?: string;
  enabled?: boolean;
  max_depth?: number | null;
  include?: string[];
  exclude?: string[];
  repo?: Record<string, unknown>;
  chunking?: Record<string, unknown>;
  /** Per-source structural taxonomy extraction; off unless enabled here. */
  taxonomy?: Record<string, unknown>;
  sync?: Record<string, unknown>;
  connector?: Record<string, unknown>;
  urls?: string[];
  sync_now?: boolean;
  sync_mode?: string;
  /** false = register/sync without blocking the response; poll SourceRecord.syncing instead. Default true. */
  wait?: boolean;
}

export interface FsEntry {
  name: string;
  path: string;
  is_dir: boolean;
  // Root entries only: false when a configured allowlist root isn't mounted.
  mounted?: boolean;
}

export interface FsListing {
  path: string | null;
  parent: string | null;
  roots: string[];
  entries: FsEntry[];
}

export interface ConfigResponse {
  path: string;
  effective: Record<string, any>;
  raw_yaml: string | null;
  profiles: string[];
}

export type SearchMode = "hybrid" | "text" | "graph" | "vector";

/** Where a hit came from. `source_type` is the *kind* of source, not its name. */
export interface HitProvenance {
  source_id?: string;
  source_type?: string;
  path?: string;
  relative_path?: string;
  heading_path?: string;
  [key: string]: unknown;
}

export interface SearchResultItem {
  node_id?: string;
  provenance?: HitProvenance;
  kind?: "node" | "relationship" | "chunk";
  type?: string;
  title?: string;
  label?: string;
  relative_path?: string;
  source_id?: string;
  summary?: string;
  score?: number;
  match?: string;
  edge_type?: string;
  source?: string;
  target?: string;
  source_label?: string;
  target_label?: string;
  [key: string]: unknown;
}

export interface SearchResponse {
  results: SearchResultItem[];
  mode?: SearchMode;
  counts?: { text?: number; graph?: number; vector?: number; returned?: number };
  [key: string]: unknown;
}

export interface SyncResult {
  source_id: string;
  indexed_artifacts: number;
  skipped_artifacts: number;
  graph_nodes: number;
  graph_edges: number;
  status: string;
  error?: string;
}

/** GET /overview — everything the shell needs to decide what to render. */
export interface Overview {
  knowledge_base: string;
  name: string;
  description?: string;
  version: string;
  sources: SourceRecord[];
  source_count: number;
  indexed_artifacts: number;
  chunk_count: number;
  node_counts: Record<string, number>;
  total_nodes: number;
  total_links: number;
  /** False when there is nothing but the knowledge-base/source scaffolding. */
  has_content: boolean;
  config_path: string;
}

export type AssistantProviderId = "anthropic" | "openai" | "gemini";

export interface AssistantProvider {
  id: AssistantProviderId;
  label: string;
  default_model: string;
  api_key_env: string;
  key_hint: string;
  env_key_present: boolean;
}

export interface AssistantSession {
  provider: string;
  model: string | null;
  expires_at: string;
}

export interface AssistantStatus {
  enabled: boolean;
  providers: AssistantProvider[];
  configured_provider: string | null;
  configured_model: string | null;
  allow_session_keys: boolean;
  session: AssistantSession | null;
  /** False means answers are extractive rather than model-synthesized. */
  ready: boolean;
  /** Where the resolvable credential came from, or null when there is none. */
  credential_source: "session" | "environment" | null;
}

export interface Citation {
  index: number;
  node_id?: string;
  chunk_id?: string;
  title: string;
  relative_path?: string;
  source_id?: string;
  type?: string;
  score?: number;
  snippet: string;
  used: boolean;
  /** Breadcrumb of the section this passage came from; absent unless the
   *  source has taxonomy extraction enabled. */
  heading_path?: string;
  /** Present only when this passage is a remembered assertion rather than a
   *  document, so the UI can say so instead of the reader having to infer it
   *  from a path that starts with a scope directory. */
  memory?: CitationMemory;
}

export interface CitationMemory {
  record_id?: string;
  scope?: MemoryScope;
  subject?: string | null;
  kind?: string;
  asserted_at?: string;
}

export type MemoryScope = "session" | "user" | "org";

export interface MemoryRecord {
  record_id: string;
  scope: MemoryScope;
  subject?: string | null;
  text: string;
  asserted_at: string;
  supersedes?: string | null;
  tags: string[];
  path: string;
  schema_version: number;
  kind: string;
  written_by?: string | null;
  valid_from?: string;
  valid_until?: string | null;
}

export interface MemoryListResponse {
  source: string;
  records: MemoryRecord[];
}

/** A proposal formation has made. Deliberately not a `MemoryRecord`: nothing
 *  here is retrievable, and nothing becomes retrievable until it is promoted. */
export interface MemoryCandidate {
  id: string;
  rule_id: string;
  scope: string;
  subject?: string | null;
  kind: string;
  text: string;
  written_by?: string | null;
  /** Why the rule proposed this: counts, the tokens, the sessions involved. */
  evidence_json?: string | null;
  observations: number;
  sessions: number;
  first_seen: string;
  last_seen: string;
  status: string;
  admitted_by?: string | null;
  record_id?: string | null;
  decided_at?: string | null;
}

/** One ledger row behind a proposal: what was asked, what came back, and the
 *  span that carried it. */
export interface CandidateInteraction {
  id: string;
  trace_id: string;
  span_id: string;
  parent_span_id?: string | null;
  modality: string;
  operation: string;
  principal?: string | null;
  session_id?: string | null;
  started_at: string;
  duration_ms: number | null;
  status: string;
  query_text?: string | null;
  answer_text?: string | null;
  criteria_json?: string | null;
  result_ids_json?: string | null;
  result_paths_json?: string | null;
  result_count?: number | null;
  top_score?: number | null;
}

export interface MemoryCandidateEvidence {
  candidate: MemoryCandidate;
  evidence: Record<string, unknown>;
  interactions: CandidateInteraction[];
  /** How many calls the rule recorded, against how many are still retained —
   *  the hot window ages out from under a pending proposal. */
  named: number;
  found: number;
}

export interface MemoryCandidateListResponse {
  candidates: MemoryCandidate[];
  counts: Record<string, number>;
}

export interface MemoryWriteResponse {
  record: MemoryRecord;
  created: boolean;
  source: string;
  sync?: SyncResult;
}

export interface MemoryConsolidateResponse {
  source?: string;
  skipped?: string;
  report?: {
    archived: number;
    kept: number;
    archived_superseded: string[];
    archived_expired: string[];
  };
  pruned?: string[];
}

/** How memory takes part in one search. Mirrors `MemoryPolicy` server-side. */
export type MemoryMode = "auto" | "off" | "only" | "prefer";

export interface GraphFact {
  subject: string;
  subject_id: string;
  predicate: string;
  /** Object-first phrasing, e.g. "mentioned in". */
  predicate_passive?: string;
  edge_type: string;
  object: string;
  object_id: string;
  object_type: string;
  confidence?: number;
}

/** One recorded step of an agent workflow, for the "what did it do" trace. */
export interface QueryFanoutTiming {
  mode: string;
  phase: "search" | "embedding";
  query_index?: number | null;
  query_label?: string;
  query_count?: number;
  duration_seconds: number;
  passages?: number;
}

export interface WorkflowStep {
  name: string;
  detail: string;
  passages: number;
  duration_seconds?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  fanout_timings?: QueryFanoutTiming[];
}

export interface WorkflowInfo {
  name: string;
  label: string;
  description: string;
  builtin: boolean;
}

export interface WorkflowCatalog {
  workflows: WorkflowInfo[];
  configured: string;
  /** What would actually run right now, after resolving "auto". */
  active: string;
  agent_extra_installed: boolean;
  options: Record<string, unknown>;
  option_defaults: Record<string, Record<string, unknown>>;
}

export interface EmbeddingsProvider {
  id: string;
  label: string;
  needs_key: boolean;
  description: string;
}

/** A vector backend, with whether its optional dependency is installed here. */
export interface VectorStoreProvider {
  id: string;
  label: string;
  available: boolean;
  hint: string | null;
}

export interface EmbeddingsStatus {
  enabled: boolean;
  /** Config says on AND a usable indexer was actually built. */
  active: boolean;
  provider: string;
  model: string;
  base_url: string;
  api_key_env: string;
  api_key_present: boolean;
  /** `null` = unset, the provider applies the model's own native size. */
  dimensions: number | null;
  batch_size: number;
  store_provider: string;
  store_path: string;
  vector_count: number;
  chunk_count: number;
  /** Fraction of indexed passages that have a vector. */
  coverage: number;
  dimensions_on_disk: number | null;
  store_error: string | null;
  providers: EmbeddingsProvider[];
  store_providers: VectorStoreProvider[];
  wrote_config?: boolean;
  vectors_invalidated?: boolean;
  vectors_dropped?: number;
  reindex?: { embedded_chunks: number; artifacts_scanned: number; vector_count: number };
  embedded_chunks?: number;
  artifacts_scanned?: number;
}

export interface ChatAnswer {
  question: string;
  answer: string;
  mode: "llm" | "extractive";
  provider: string | null;
  model: string | null;
  credential_source: "session" | "environment" | null;
  error: string | null;
  citations: Citation[];
  facts: GraphFact[];
  focus_node_ids: string[];
  search_mode: string;
  counts: Record<string, number>;
  /** Which workflow produced this answer. */
  workflow?: string;
  /** The agent's trace; empty for the single-pass workflow. */
  steps?: WorkflowStep[];
  /** Images the cited documents show, for `[fig:n]` markers in `answer`. */
  figures?: Figure[];
  /** How the router read the question. */
  route?: AnswerRoute;
  /** A grounded diagram or the cited images; `pending` while it is drawn. */
  visual?: AnswerVisual | null;
  /** What was searched when the question was a follow-up. */
  search_question?: string;
}

/** One earlier exchange sent back so a follow-up can be understood. */
export interface HistoryTurn {
  question: string;
  answer: string;
}

export type AnswerDepth = "short" | "medium" | "long";

export interface AnswerRoute {
  intent: string;
  depth: AnswerDepth;
  visual: "none" | "diagram" | "image";
  why?: Record<string, string>;
  decided_by?: Record<string, string>;
}

export interface Figure {
  figure: number;
  node_id: string;
  relative_path: string | null;
  caption: string;
  alt: string;
  cited_in: number[];
  shown: boolean;
}

export interface DiagramNode {
  id: string;
  label: string;
  cites: number[];
  inferred: boolean;
  group?: string;
}

export interface DiagramEdge {
  from: string;
  to: string;
  label: string;
  cites: number[];
  inferred: boolean;
}

export interface AnswerVisual {
  type: "diagram" | "images" | "image";
  status: "ok" | "declined" | "pending";
  reason?: string;
  note?: string;
  source?: "model" | "graph";
  diagram?: {
    /** One of the vocabulary's shapes (`assistant.visual_specs.KINDS`). */
    kind: string;
    title: string;
    summary: string;
    viewpoint?: string;
    nodes: DiagramNode[];
    edges: DiagramEdge[];
  };
  mermaid?: string | null;
  /** A table visual as Markdown. */
  markdown?: string;
  grounding?: { cited: number; inferred: number; ratio: number };
  figures?: Figure[];
  /** What the view needs to redraw the same passages in another shape. */
  redraw?: { request: string; node_ids: string[]; knowledge_base: string | null; kinds: string[] };
}

/** `POST /assistant/visual`: a visual on demand, with the evidence it drew on. */
export interface VisualResponse {
  request: string;
  visual: AnswerVisual | null;
  citations: Citation[];
  facts: GraphFact[];
  figures: Figure[];
}

export interface McpToolSummary {
  name: string;
  description: string;
}

export interface McpInfo {
  enabled: boolean;
  transports: Record<string, boolean>;
  streamable_http_url: string | null;
  stdio_command: string[];
  config_path: string;
  tools: McpToolSummary[];
  client_configs: Record<string, string>;
}

export interface QuickAddResponse {
  status: string;
  sources: { name: string; type: string; path: string; description?: string }[];
  sync_results: SyncResult[];
  /** Names of sources whose first sync was handed off to a background thread (wait: false). */
  syncing: string[];
}

/** One node of a document's extracted outline (`GET /taxonomy`). */
export interface TaxonomyNode {
  line: number;
  level: number;
  number?: string | null;
  title: string;
  kind: string;
  path: string;
  children: TaxonomyNode[];
}

/**
 * A numbering defect found while reconciling a series: a gap, a repeated
 * number, or one that goes backwards.
 */
export interface TaxonomyIssue {
  kind: "gap" | "duplicate" | "out_of_order";
  series: string;
  parent: string;
  after?: string | null;
  at?: string | null;
  line: number;
  /** Present on `gap` only: the numbers that are missing between the two. */
  missing?: number[];
}

export interface TaxonomyDocument {
  relative_path: string;
  heading_count: number;
  tree: TaxonomyNode[];
  issues: TaxonomyIssue[];
}

export interface TaxonomyResponse {
  documents: TaxonomyDocument[];
  heading_count: number;
  issue_count: number;
  truncated: boolean;
}

// ---------------------------------------------------------------------------
// Background jobs — what the jobs tray renders.
// ---------------------------------------------------------------------------

export interface JobProgress {
  phase: string;
  current: number;
  /** null until the work knows its own size (a sync, until listing finishes). */
  total: number | null;
  detail: string;
  /** 0..1, or null when `total` is unknown — render indeterminate, not 0%. */
  fraction: number | null;
}

/**
 * One source's slice of a job (Phase 35.1).
 *
 * A `sync_all` over eight sources used to be one job with one counter, so the
 * one source that was stuck looked exactly like the seven that were fine.
 * Throughput and ETA are observed server-side from update timings, not
 * reported by the indexer, so they exist even for callers that emit neither.
 */
export interface SourceProgress {
  source: string;
  phase: string;
  current: number;
  /** null until the connector has finished listing. A made-up denominator lies. */
  total: number | null;
  detail: string;
  status: string;
  active: boolean;
  fraction: number | null;
  indexed: number;
  skipped: number;
  failed: number;
  bytes_done: number;
  files_per_second: number | null;
  eta_seconds: number | null;
  /** Always present, so a UI can say "last update 4s ago" during healthy work. */
  seconds_since_progress: number;
  /** Only true after the server-side stall window — slow is not stuck. */
  stalled: boolean;
  started_at: string;
  last_progress_at: string;
  finished_at: string | null;
  phase_seconds: Record<string, number>;
  failures: { path: string; error: string }[];
}

export interface JobRecord {
  id: string;
  kind: string;
  label: string;
  targets: string[];
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  /** Rollup across `sources`. Kept for callers that predate the per-source split. */
  progress: JobProgress;
  started_at: string;
  finished_at: string | null;
  error: string | null;
  result: Record<string, unknown> | null;
  log: string[];
  active: boolean;
  sources: SourceProgress[];
  stalled: boolean;
  failed_files: number;
}

// ---------------------------------------------------------------------------
// Uploads and host paths.
// ---------------------------------------------------------------------------

export interface UploadResponse {
  status: string;
  source_name: string;
  path: string;
  stored: { filename: string; path: string; size_bytes: number }[];
  rejected: { filename: string; error: string }[];
  syncing: boolean;
  job_id: string | null;
}

export interface MountRemedy {
  host_path: string;
  container_path: string;
  compose_volume: string;
  docker_run_flag: string;
  config_patch: Record<string, unknown>;
  cli: string;
}

export interface HostPathReport {
  /** native = not containerised; visible = mounted; not_mounted = needs a bind mount. */
  status: "native" | "visible" | "not_mounted" | "unknown";
  host_path: string;
  container_path: string | null;
  exists: boolean;
  remedy: MountRemedy | null;
  in_container: boolean;
  allowed: boolean;
  policy_error?: string;
}

// ---------------------------------------------------------------------------
// Retrieval tuning.
// ---------------------------------------------------------------------------

export interface RetrievalSettings {
  max_rounds: number | null;
  per_query_results: number | null;
  max_context_passages: number | null;
  retrieval_modes: string[] | null;
  expand_graph: boolean | null;
  expand_depth: number | null;
  expand_per_node: number | null;
  grade_evidence: boolean | null;
  grader_model: string | null;
  verify_citations: boolean | null;
  max_facts: number | null;
}

export interface RetrievalResponse {
  retrieval: RetrievalSettings;
  /** What actually reaches the workflow once workflow_options is layered on. */
  effective: Record<string, unknown>;
  workflow_options: Record<string, unknown>;
  defaults: Record<string, unknown>;
  field_help: Record<string, string>;
}

// ---------------------------------------------------------------------------
// Live config sections + knowledge-base identity.
// ---------------------------------------------------------------------------

export interface ConfigSection {
  id: string;
  values: Record<string, unknown>;
  /** false means the file is updated but the running process keeps the old value. */
  live_applicable: boolean;
}

export interface ConfigSectionResult {
  status: string;
  section: string;
  applied: boolean;
  restart_required: boolean;
  wrote_config: boolean;
  values: Record<string, unknown>;
}

export interface KnowledgeBaseInfo {
  id: string;
  name: string;
  description: string;
  environment: string;
  version: string;
  state_path: string;
  config_path: string;
}

export interface KnowledgeBaseUpdate extends KnowledgeBaseInfo {
  status: string;
  changed: string[];
  wrote_config: boolean;
  restart_required: boolean;
  /** Present only when the name changed: renaming orphans the indexed graph. */
  rename: {
    previous: string;
    current: string;
    reindex_required: boolean;
    detail: string;
  } | null;
}

// ---------------------------------------------------------------------------
// Graph diagnostics.
// ---------------------------------------------------------------------------

export interface GraphHub {
  node_id: string;
  degree: number;
  label: string | null;
  type: string | null;
}

export interface GraphDiagnostics {
  total_nodes: number;
  total_links: number;
  node_types: Record<string, number>;
  edge_types: Record<string, number>;
  orphan_count: number;
  orphan_sample: string[];
  density: number;
  hubs: GraphHub[];
}

export interface GraphPath {
  source: string;
  target: string;
  found: boolean;
  hops: number | null;
  path: (GraphNode & { node_id: string })[];
}

// ---------------------------------------------------------------------------
// The evaluation plane.
//
// Every number here arrives with its denominator, its status and its stated
// limitation, and the UI's job is to keep them together: a value rendered
// without its `denominator` and `status` is exactly the bare score the whole
// plane exists to avoid producing. `value: null` with an
// `insufficient_evidence` status is a real answer — "we could not measure" —
// and must render as a gap rather than as a zero.
// ---------------------------------------------------------------------------

export type EvaluationMetricStatus =
  | "pass"
  | "warn"
  | "fail"
  | "informational"
  | "insufficient_evidence"
  | "not_applicable";

export type EvaluationClassification =
  | "demonstrated"
  | "structural"
  | "diagnostic"
  | "operational";

/** A batch's live position, read from `/state` rather than from a process. */
export interface EvaluationStatus {
  status:
    | "running"
    | "completed"
    | "truncated"
    | "invalid"
    | "failed"
    | "interrupted"
    | "none"
    | "unknown";
  run_id?: string;
  snapshot_id?: string;
  mode?: string;
  phase?: string | null;
  phase_detail?: string | null;
  completed_units?: number;
  total_units?: number;
  /** 0–1, or null when the run has not planned its units yet. */
  fraction?: number | null;
  started_at?: string;
  finished_at?: string | null;
  heartbeat_at?: string | null;
  owner?: string | null;
  /** Above 1 means an earlier attempt was interrupted and this one resumed it. */
  attempts?: number;
  error?: string | null;
  gates_passed?: boolean;
  active?: boolean;
  detail?: string;
  enabled?: boolean;
  promotion_enabled?: boolean;
  auto_trigger?: boolean;
}

export interface EvaluationHealthEntry {
  /**
   * The metric's own id, which is deliberately not the display label — the
   * vector says `known_positive_retrieval_at_5` where the metric is
   * `known_positive_recall_at_5`. Carried so a client can fetch the
   * calculation behind a tile without duplicating the mapping.
   */
  metric_id?: string;
  value: number | null;
  status: EvaluationMetricStatus;
  numerator?: number | null;
  denominator?: number | null;
  classification?: EvaluationClassification;
}

export interface EvaluationGate {
  gate_id: string;
  passed: boolean;
  observed: number;
  maximum: number;
  detail: string;
  evidence: Record<string, unknown>;
}

export interface EvaluationMetricResult {
  metric_id: string;
  metric_version: number;
  classification: EvaluationClassification;
  optional: boolean;
  scope: {
    snapshot_id: string;
    cohort_id: string | null;
    variant_id: string | null;
    query_id: string | null;
  };
  result: {
    value: number | null;
    numerator: number | null;
    denominator: number | null;
    unit: string;
    status: EvaluationMetricStatus;
    threshold: number | null;
  };
  calculation: {
    formula: string;
    substituted: string;
    operands: Record<string, unknown>;
  };
  evidence: {
    proof_ids: string[];
    interaction_ids: string[];
    artifact_ids: string[];
    excluded_count: number;
    exclusion_reasons: Record<string, number>;
  };
  interpretation: {
    summary: string;
    supports_claim: string;
    does_not_support: string;
  };
}

export interface EvaluationCandidateDecision {
  candidate_id: string;
  rule_id: string;
  kind: string;
  decision: string;
  reasons: string[];
  evidence: Record<string, unknown>;
  applied?: boolean;
  note?: string;
  error?: string;
}

export interface EvaluationReport {
  schema_version: number;
  run_identity: Record<string, unknown> & {
    run_id: string;
    snapshot_id: string;
    mode: string;
    primary_variant: string;
    baseline_variant: string;
    attempts?: number;
    resumed_replays?: number;
  };
  snapshot_integrity: {
    complete: boolean;
    incomplete_sections: string[];
    manifest: Record<string, unknown>;
  };
  evidence_coverage: {
    sufficient: boolean;
    eligible_queries: number;
    evidenced_queries: number;
    independent_interactions: number;
    max_single_query_share: number;
    reasons: string[];
  };
  health_vector: Record<string, EvaluationHealthEntry>;
  classification_breakdown: Record<string, string[]>;
  baseline_comparison: EvaluationMetricResult[];
  memory_attribution: EvaluationMetricResult[];
  generalization: {
    learned: EvaluationMetricResult | null;
    temporal_holdout: EvaluationMetricResult | null;
    gap: EvaluationMetricResult | null;
    note: string;
  };
  controls_and_regressions: EvaluationMetricResult | null;
  gates: EvaluationGate[];
  optional_diagnostics: Record<string, unknown>;
  candidate_decisions: EvaluationCandidateDecision[];
  composite: Record<string, unknown>;
  limitations: {
    unjudged_share: number | null;
    failed_queries: Record<string, Record<string, string>>;
    truncated_replays: Record<string, number>;
    metrics_withheld: { metric_id: string; problems: string[] }[];
  };
  longitudinal: {
    previous_snapshot_id: string | null;
    snapshot_diff: string[];
    material_change: string[];
  };
  explanations: {
    end_user: string;
    agent: Record<string, unknown>;
    developer: Record<string, unknown>;
  };
}

export interface EvaluationRunSummary {
  run_id: string;
  snapshot_id: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  mode: string;
  gates_passed: number | boolean;
}

export interface EvaluationCohortSummary {
  cohort_id: string;
  name: string;
  purpose: string;
  query_count: number;
  frozen: boolean;
  created_at: string;
  window_start: string | null;
  window_end: string | null;
}

export interface EvaluationTrendPoint {
  started_at: string;
  snapshot_id: string;
  run_id: string;
  value: number | null;
  numerator: number | null;
  denominator: number | null;
  status: EvaluationMetricStatus;
  variant_id: string | null;
  cohort_id: string | null;
}

export interface EvaluationTaxonomyEvent {
  event_type: string;
  polarity: "positive" | "negative" | "unknown";
  strength: "weak" | "moderate" | "strong" | "conclusive";
  note: string;
}

// --------------------------------------------------------------------------
// The tuning plane
//
// Where the evaluation types describe *how well* retrieval is doing, these
// describe **which step** of it is failing. The shapes below are the API's,
// verbatim, rather than a UI-convenient flattening of them: a diagnosis is
// evidence for a decision somebody may have to argue with later, and a view
// model that dropped the denominator or the rationale would make that
// impossible on exactly the screen where it matters.

/** One pipeline stage's share of the misses. */
export interface TuningStageEntry {
  stage: string;
  count: number;
}

/**
 * The diagnosis: how misses distribute over the retrieval pipeline.
 *
 * `actionable_share` is the fraction a ranking parameter could plausibly move.
 * It is nullable, and the null means "there were no misses to attribute" —
 * which is not the same as zero, and must not render as a 0% bar.
 */
export interface TuningHistogram {
  counts: Record<string, number>;
  evaluated: number;
  served: number;
  misses: number;
  actionable_misses: number;
  actionable_share: number | null;
  dominant_stage: string | null;
  ranked: TuningStageEntry[];
}

export interface TuningDiagnosis {
  diagnosis_id: string;
  cohort_name: string;
  histogram: TuningHistogram;
  unevidenced_queries: number;
  summary: string;
}

export interface TuningPoint {
  point_id: string;
  values: Record<string, number>;
  delta: Record<string, [number, number]>;
  delta_description: string;
}

export interface TuningProposal {
  point: TuningPoint;
  motivating_stage: string;
  rationale: string;
  cost_class: string;
  strategy: string;
  generation: number;
}

export interface TuningTrial {
  trial_id: string;
  proposal: TuningProposal;
  cohort_name: string;
  metrics: Record<string, number>;
  histogram: TuningHistogram;
  evaluated_queries: number;
  excluded_queries: number;
  searches: number;
  duration_ms: number;
  failed: string;
}

export interface TuningComparison {
  metric: string;
  baseline_value: number;
  treatment_value: number;
  delta: number;
  paired_queries: number;
  improved_queries: number;
  regressed_queries: number;
  excluded_queries: number;
  formula: string;
  substituted: string;
}

/** A gate is not a metric: it is evaluated before aggregation and blocks. */
export interface TuningGate {
  gate_id: string;
  passed: boolean;
  blocking: boolean;
  summary: string;
  observed: unknown;
  threshold: unknown;
}

export interface TuningDecision {
  decision_id: string;
  outcome: "promote" | "reject" | "no_change" | "insufficient_evidence";
  reason: string;
  winning_point_id: string;
  comparisons: TuningComparison[];
  gates: TuningGate[];
  gates_passed: boolean;
  holdout_confirmed: boolean;
  control_regressed: boolean;
}

export interface TuningBundle {
  bundle_id: string;
  kb_id: string;
  experiment_id: string;
  decision_id: string;
  parameters: Record<string, number>;
  replaces: Record<string, number>;
  metrics: Record<string, number>;
  gates: TuningGate[];
  motivating_stage: string;
  rationale: string;
  created_at: string;
  applied_at?: string;
  applied_by?: string;
  superseded_at?: string;
  active?: boolean;
}

export interface TuningReport {
  experiment: { experiment_id: string; snapshot_id: string; cohort_id: string };
  objective?: TuningObjective & {
    baseline_score?: number | null;
    baseline_substituted?: string;
  };
  mechanisms?: Record<string, TuningMechanism>;
  diagnosis: TuningDiagnosis;
  decision: TuningDecision;
  bundle: TuningBundle | null;
  baseline: TuningTrial;
  trials: TuningTrial[];
  trial_count: number;
  searches: number;
  primary_metric: string;
}

export interface TuningStatus {
  experiment_id: string;
  status: string;
  phase: string;
  phase_detail: string;
  completed_units: number;
  total_units: number;
  progress: number | null;
  searches: number;
  attempts: number;
  error: string;
  enabled?: boolean;
  auto_enabled?: boolean;
  auto_apply?: boolean;
  tracking_backend?: string;
}

export interface TuningExperimentSummary {
  experiment_id: string;
  status: string;
  phase: string;
  started_at: string;
  finished_at: string;
  searches: number;
  progress: number | null;
}

export interface TuningParameterSpec {
  name: string;
  stage: string;
  cost_class: string;
  candidates: number[];
  bounds: number[];
  rationale: string;
}

export interface TuningParameters {
  active: {
    provenance: string;
    bundle_id: string;
    values: Record<string, number>;
    bundle: TuningBundle | null;
    /** The configured floor a rollback returns to. */
    base: { values: Record<string, number>; source: string; explanation: string };
    /** The promoted bundle layered over it, if any. */
    overlay: {
      values: Record<string, number>;
      bundle_id: string;
      applied_at: string;
      applied_by: string;
      explanation: string;
    };
    /** Only the parameters where active differs from base. */
    changes: { parameter: string; stage: string; base: number; active: number }[];
  };
  objective?: TuningObjective;
  explanations?: Record<string, GlossaryEntry>;
  space: {
    digest: string;
    pinned: string[];
    parameters: TuningParameterSpec[];
    cost_classes: Record<string, string[]>;
  };
  config_fragment: { search: { ranking: Record<string, number> } };
}

/** One point on a parameter's sweep: what it was set to, and what that scored. */
export interface TuningSweepPoint {
  trial_id: string;
  value: number | null;
  baseline_value: number | null;
  metric: number | null;
  stage: string;
  cost_class: string;
}

export interface TuningTrialRow {
  trial_id: string;
  point_id: string;
  cohort_name: string;
  cost_class: string;
  motivating_stage: string;
  generation: number;
  metrics: Record<string, number>;
  point: { delta_description?: string; values?: Record<string, number> };
  proposal: { rationale?: string };
  evaluated_queries: number;
  searches: number;
}

export interface TuningTrials {
  experiment_id: string;
  primary_metric: string;
  trials: TuningTrialRow[];
  sweeps: Record<string, TuningSweepPoint[]>;
}

/**
 * Pipeline behaviour on live traffic.
 *
 * `status` is `insufficient_evidence` until enough searches have been sampled.
 * That is a distinct state from "healthy", and the UI must render it as an
 * absence rather than as zeroes — a 0% empty rate over four searches is not
 * good news, it is no news.
 */
export interface TuningHealth {
  status: "measured" | "insufficient_evidence";
  classification?: string;
  samples: number;
  minimum_samples?: number;
  reason?: string;
  sample_rate?: number;
  observation_enabled?: boolean;
  empty?: {
    count: number;
    rate: number;
    by_stage: Record<string, { count: number; share: number | null }>;
  };
  arms?: Record<
    string,
    {
      observed: number;
      contributed: number;
      contribution_rate: number | null;
      failed: number;
    }
  >;
  filters?: Record<string, { dropped: number; per_search: number }>;
  truncation?: { count: number; rate: number };
  results_per_search?: number;
  bundles?: Record<string, number>;
  mixed_configurations?: boolean;
  does_not_support?: string;
  explanations?: Record<string, GlossaryEntry>;
}

/**
 * One measure explained.
 *
 * `does_not_mean` is the load-bearing field. A dashboard of rates gets read
 * confidently and wrongly — a 42% truncation rate looks alarming and is
 * normal, a 0% empty rate looks fine and proves nothing — so the misreading
 * each number invites travels with it rather than living in docs a reader
 * reaches after the mistake.
 */
export interface GlossaryEntry {
  term: string;
  label: string;
  kind: string;
  means: string;
  impact: string;
  does_not_mean: string;
  direction: "higher" | "lower" | "neutral";
  stage?: string;
  cost_class?: string;
  bounds?: number[];
}

export interface TuningGlossary {
  metrics: GlossaryEntry[];
  health: GlossaryEntry[];
  stages: GlossaryEntry[];
  gates: GlossaryEntry[];
  parameters: GlossaryEntry[];
  parameters_without_explanation: string[];
  reading_notes: string[];
}

/** A definition of "better", with what it accepts getting worse. */
export interface TuningObjective {
  objective_id: string;
  label: string;
  weights: Record<string, number>;
  primary_metric: string;
  summary: string;
  trades_away: string;
  higher_is_better: boolean;
}

export interface TuningObjectives {
  active: TuningObjective;
  available: TuningObjective[];
  configured_by: string;
}

/** One configuration this region served, and what it displaced. */
export interface TuningLineageEntry {
  bundle_id: string;
  experiment_id: string;
  decision_id: string;
  created_at: string;
  applied_at: string;
  applied_by: string;
  superseded_at: string;
  active: boolean;
  parameters: Record<string, number>;
  replaced: Record<string, number>;
}

export interface TuningLineage {
  lineage: TuningLineageEntry[];
  base_explanation: string;
}

/** One retrieval mechanism scored alone, and what the merge adds over it. */
export interface TuningMechanism {
  metrics: Record<string, number>;
  objective_score: number | null;
  evaluated_queries: number;
  hybrid_gain?: number;
  /** Vector arm only: which embedder produced this score. */
  provider?: string;
  /** False when the score came from the offline `stub` embedder. */
  semantic?: boolean;
}

/**
 * `GET /ready` — whether this process should be receiving traffic, and why
 * not. Answered with a 503 *and this body* when it is not ready.
 */
export interface ReadyStatus {
  status: "ready" | "not_ready" | "draining" | "standby";
  reason?: string;
  role?: string;
  indexes_locally?: boolean;
  drains_queue?: boolean;
  leader?: boolean;
  draining_for_seconds?: number;
  graph_generation?: { loaded?: string | null; published?: string | null; source?: string };
}

/**
 * Where a published sync is between being requested and being indexed. Only
 * `claimed` means an indexer is working on it; `awaiting_claim` is the
 * pre-claim interval a role-split region used to report as nothing at all.
 */
export type QueueTaskState =
  | "awaiting_claim"
  | "retry_scheduled"
  | "claimed"
  | "claim_lapsed"
  | "dead";

export interface QueueTask {
  task_id: string;
  source: string;
  mode: string;
  state: QueueTaskState;
  enqueued_at: string | null;
  visible_at: string | null;
  waiting_seconds: number | null;
  claimed_by: string | null;
  attempts: number;
  max_attempts: number;
  last_error: string | null;
  /** 1-based place in line among `awaiting_claim` tasks; null otherwise. */
  position: number | null;
}

/** `GET /queue`. `listing: "unavailable"` means "cannot list", never "empty". */
export interface IndexQueueStatus {
  knowledge_base: string;
  enabled: boolean;
  backend: string | null;
  listing: "complete" | "unavailable" | "not_applicable";
  depth: Record<string, number> | null;
  tasks: QueueTask[];
  counts: Record<QueueTaskState, number>;
}
