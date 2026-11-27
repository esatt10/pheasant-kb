import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { harnessApi } from "./api";
import { renderMarkdown } from "./markdown";
import { CallsTable } from "./Connections";
import type { Artifact, Evidence, RunDetail, RunEvent, RunWait, Verification } from "./types";

const ACTIVE = new Set(["accepted", "running", "verifying", "delivering", "reconciling"]);
const LIFECYCLE = ["accepted", "running", "verifying", "delivering", "succeeded"] as const;
const TABS = ["Deliverable", "Verification", "Evidence", "Steps", "Connections", "Journal", "Sandboxes"] as const;
type Tab = (typeof TABS)[number];

const short = (digest: string | null | undefined, size = 12) =>
  digest ? digest.replace(/^tree:/, "tree:").replace(/^(tree:)?sha256:/, "$1").slice(0, size + (digest.startsWith("tree:") ? 5 : 0)) : "—";
const when = (stamp: number | null | undefined) => (stamp ? new Date(stamp * 1000).toLocaleTimeString() : "—");

function tone(state: string): string {
  if (["succeeded", "pass", "committed", "destroyed", "success", "allowed", "approved", "enabled"].includes(state)) return "pill--ok";
  if (["partial", "waiting", "reconciling", "skipped", "inconclusive", "uncertain", "superseded", "closed", "no policy", "unsupported"].includes(state)) return "pill--warn";
  if (["failed", "fail", "cancelled", "denied", "conflict", "quarantined", "permanent_error", "refused", "rejected", "disabled"].includes(state)) return "pill--danger";
  return "pill--accent";
}

export function Pill({ value }: { value: string }) {
  return <span className={`pill ${tone(value)}`}>{value.replace(/_/g, " ")}</span>;
}

function Lifecycle({ state }: { state: string }) {
  const reached = Math.max(LIFECYCLE.indexOf(state as (typeof LIFECYCLE)[number]), state === "reconciling" ? 3 : -1);
  const side = !LIFECYCLE.includes(state as (typeof LIFECYCLE)[number]);
  return (
    <ol className="hx-life" aria-label="Run lifecycle">
      {LIFECYCLE.map((stage, index) => (
        <li key={stage} className={`hx-life__stage${index < reached ? " is-done" : ""}${index === reached ? " is-current" : ""}`}>
          <span className="hx-life__dot" />{stage}
        </li>
      ))}
      {side ? <li className="hx-life__side"><Pill value={state} /></li> : null}
    </ol>
  );
}

function Meter({ label, spent, limit, unit = "" }: { label: string; spent: number; limit: number; unit?: string }) {
  const share = limit ? Math.min(1, spent / limit) : 0;
  const shown = (value: number) => unit === "MB" ? (value / 1048576).toFixed(1) : Number.isInteger(value) ? value : value.toFixed(1);
  return (
    <div className="hx-meter">
      <div className="hx-meter__label"><span>{label}</span><span className="mono">{shown(spent)} / {shown(limit)}{unit ? ` ${unit}` : ""}</span></div>
      <div className="meter"><div className="meter__fill" style={{ width: `${share * 100}%` }} /></div>
    </div>
  );
}

function ArtifactView({ runId, artifact, figures, onCite }: {
  runId: string; artifact: Artifact; figures: Record<string, string>; onCite: (id: string) => void;
}) {
  const content = useQuery({ queryKey: ["harness-artifact", runId, artifact.digest], queryFn: () => harnessApi.artifact(runId, artifact.digest) });
  const body = content.data;
  const html = useMemo(() => body?.content && artifact.media_type === "text/markdown" ? renderMarkdown(body.content, figures) : null,
    [body, artifact.media_type, figures]);
  if (content.isLoading) return <p className="muted">Loading artifact…</p>;
  if (content.error) return <p className="error">{(content.error as Error).message}</p>;
  if (!body) return null;
  if (body.content_base64) {
    return <img className="hx-figure" alt={artifact.name} src={`data:${body.media_type};base64,${body.content_base64}`} />;
  }
  if (html) {
    return (
      <div className="hx-md" dangerouslySetInnerHTML={{ __html: html }}
        onClick={(event) => {
          const cite = (event.target as HTMLElement).closest("[data-ev]");
          if (cite) onCite(cite.getAttribute("data-ev") ?? "");
        }} />
    );
  }
  if (artifact.kind === "tree") {
    const manifest = JSON.parse(body.content ?? "{}");
    return (
      <div className="hx-tree">
        <div className="kv">
          <div className="kv__row"><span className="kv__key">Base</span><span className="kv__value">{manifest.base?.registration_id} @ {short(manifest.base?.revision, 16)}</span></div>
          <div className="kv__row"><span className="kv__key">Parent</span><span className="kv__value">{short(manifest.parent, 16)}</span></div>
        </div>
        <ul className="hx-files">{Object.entries(manifest.files ?? {}).map(([path, digest]) => (
          <li key={path}><span className="mono">{path}</span> <span className="muted mono">{digest ? short(String(digest)) : "deleted"}</span></li>))}</ul>
      </div>
    );
  }
  return <pre className="code-view hx-pre">{body.content}</pre>;
}

function Deliverable({ detail, onCite }: { detail: RunDetail; onCite: (id: string) => void }) {
  const candidates = detail.result?.candidates ?? {};
  const ordered = useMemo(() => {
    const final = new Set(Object.values(candidates));
    return [...detail.artifacts].sort((a, b) => Number(final.has(b.digest)) - Number(final.has(a.digest)) || b.created_at - a.created_at);
  }, [detail.artifacts, candidates]);
  const [chosen, setChosen] = useState<string | null>(null);
  const active = ordered.find((a) => a.digest === chosen) ?? ordered[0];
  const figures = useFigures(detail);
  const patch = detail.steps.filter((s) => s.action === "apply_patch" && s.observation?.candidate === active?.digest)[0];
  if (!active) return <p className="muted">No artifact yet. The runtime inspects before it drafts.</p>;
  return (
    <div className="hx-deliverable">
      <div className="hx-artifacts" role="listbox" aria-label="Artifacts">
        {ordered.map((artifact) => {
          const checks = detail.verifications.filter((v) => v.artifact_digest === artifact.digest);
          const failed = checks.some((v) => v.outcome === "fail");
          const final = detail.state === "succeeded" && Object.values(candidates).includes(artifact.digest);
          return (
            <button key={artifact.digest} role="option" aria-selected={artifact === active}
              className={`hx-artifact${artifact === active ? " is-active" : ""}`} onClick={() => setChosen(artifact.digest)}>
              <span className="hx-artifact__name">{artifact.name}</span>
              <span className="hx-artifact__meta">
                <span className="mono">{short(artifact.digest, 10)}</span>
                {final ? <Pill value="delivered" /> : Object.values(candidates).includes(artifact.digest) ? <Pill value="verified" /> : checks.length ? <Pill value={failed ? "fail" : "pass"} /> : <span className="muted">{artifact.kind}</span>}
              </span>
            </button>
          );
        })}
      </div>
      <div className="hx-artifact-body card">
        <div className="hx-artifact-body__bar">
          <strong>{active.name}</strong>
          <span className="muted mono">{active.digest}</span>
          {active.parent_digest ? <span className="muted">{active.kind === "render" ? "derived from" : "revises"} <span className="mono">{short(active.parent_digest, 10)}</span></span> : null}
        </div>
        <ArtifactView runId={detail.run_id} artifact={active} figures={figures} onCite={onCite} />
        {patch?.observation?.diff ? <pre className="code-view hx-pre hx-diff">{String(patch.observation.diff)}</pre> : null}
      </div>
    </div>
  );
}

function useFigures(detail: RunDetail): Record<string, string> {
  const figure = detail.artifacts.find((a) => a.kind === "figure");
  const content = useQuery({
    queryKey: ["harness-artifact", detail.run_id, figure?.digest], enabled: Boolean(figure),
    queryFn: () => harnessApi.artifact(detail.run_id, figure!.digest),
  });
  return content.data?.content_base64 && figure ? { [figure.name]: `data:image/png;base64,${content.data.content_base64}` } : {};
}

function VerificationMatrix({ detail }: { detail: RunDetail }) {
  const candidates = [...new Set(detail.verifications.map((v) => v.artifact_digest))];
  const [open, setOpen] = useState<Verification | null>(null);
  if (!candidates.length) return <p className="muted">No checks have run. A planner can only say a candidate is ready; the verifier decides.</p>;
  const cell = (criterion: string, digest: string) => {
    const found = detail.verifications.filter((v) => v.criterion_id === criterion && v.artifact_digest === digest);
    return found[found.length - 1];
  };
  const names = Object.fromEntries(detail.artifacts.map((a) => [a.digest, a.name]));
  return (
    <div>
      <div className="table-scroll">
        <table className="hx-table">
          <thead><tr><th>Criterion</th><th>Grader</th>{candidates.map((digest) => <th key={digest}><span>{names[digest] ?? "candidate"}</span><br /><span className="mono muted">{short(digest, 10)}</span></th>)}</tr></thead>
          <tbody>{detail.criteria.map((criterion) => (
            <tr key={criterion.id}>
              <td><strong>{criterion.id}</strong>{criterion.required ? null : <span className="muted"> · optional</span>}</td>
              <td className="mono muted">{criterion.type}</td>
              {candidates.map((digest) => {
                const check = cell(criterion.id, digest);
                return <td key={digest}>{check ? <button className="hx-cell" onClick={() => setOpen(check)}><Pill value={check.outcome} /></button> : <span className="muted">—</span>}</td>;
              })}
            </tr>))}</tbody>
        </table>
      </div>
      {open ? (
        <div className="card hx-observed">
          <div className="hx-artifact-body__bar"><strong>{open.criterion_id}</strong><Pill value={open.outcome} />
            <span className="muted">{open.grader} · {open.grader_version}</span>
            <button className="btn btn--small btn--ghost" onClick={() => setOpen(null)}>Close</button></div>
          <div className="kv">
            <div className="kv__row"><span className="kv__key">Inputs</span><span className="kv__value">{open.input_digests.map((d) => short(d, 16)).join(", ")}</span></div>
            <div className="kv__row"><span className="kv__key">Environment</span><span className="kv__value">{JSON.stringify(open.environment)}</span></div>
          </div>
          <pre className="code-view hx-pre">{JSON.stringify(open.observed, null, 2)}</pre>
        </div>
      ) : null}
    </div>
  );
}

function EvidenceList({ detail, focus }: { detail: RunDetail; focus: string | null }) {
  const refs = useRef<Record<string, HTMLLIElement | null>>({});
  useEffect(() => { if (focus) refs.current[focus]?.scrollIntoView({ block: "center", behavior: "smooth" }); }, [focus]);
  const labels = (item: Evidence) => Object.entries(item.flags).filter(([, on]) => on).map(([flag]) => flag);
  return (
    <ul className="hx-evidence">
      {detail.evidence.map((item) => {
        const loc = item.locator as { path?: string; lines?: number[]; region?: number[] };
        return (
          <li key={item.id} ref={(node) => { refs.current[item.id] = node; }} className={`card hx-ev${focus === item.id ? " is-focused" : ""}`}>
            <div className="hx-ev__head">
              <span className="chip chip--mono">{item.id}</span>
              <span className="pill">{item.method}</span>
              <span className="mono">{loc.path ?? (item.method === "execute" ? "sandbox execution" : "revision")}{loc.lines ? `:${loc.lines[0]}–${loc.lines[1]}` : ""}{loc.region ? ` [${loc.region.join(",")}]` : ""}</span>
              <span className="muted mono">@ {short(item.revision, 12)}</span>
              {labels(item).map((flag) => <Pill key={flag} value={flag} />)}
            </div>
            <pre className="hx-ev__extract">{item.extract.length > 900 ? `${item.extract.slice(0, 900)}…` : item.extract}</pre>
          </li>
        );
      })}
    </ul>
  );
}

function StepsTable({ detail }: { detail: RunDetail }) {
  const fences = Object.fromEntries(detail.attempts.map((a) => [a.attempt_id, a]));
  return (
    <div className="table-scroll">
      <table className="hx-table">
        <thead><tr><th>Step</th><th>Action</th><th>Status</th><th>Outcome</th><th>Attempt · fence</th><th>Why / policy</th></tr></thead>
        <tbody>{detail.steps.map((step) => {
          const attempt = step.current_attempt ? fences[step.current_attempt] : undefined;
          const target = (step.args.path as string) ?? (step.args.pattern as string) ?? ((step.args.criterion as { id?: string })?.id) ?? (step.args.argv as string[])?.join(" ") ?? (step.args.name as string) ?? "";
          return (
            <tr key={step.step_id}>
              <td className="mono">{step.step_id}{step.required ? "" : "*"}</td>
              <td><strong>{step.action}</strong> <span className="muted mono">{String(target).slice(0, 48)}</span></td>
              <td><Pill value={step.status} /></td>
              <td>{step.outcome ? <span className="mono">{step.outcome}</span> : "—"}</td>
              <td className="mono muted">{attempt ? `${attempt.attempt_id.slice(4, 12)} · ${attempt.fence}` : "—"}</td>
              <td className="muted">{step.policy?.reason ?? (step.policy?.allowed ? step.policy.capability : "")}</td>
            </tr>
          );
        })}</tbody>
      </table>
      <p className="muted hx-foot">* optional branch: its absence is a recorded coverage gap, never a blocked delivery.</p>
    </div>
  );
}

function Journal({ runId, live }: { runId: string; live: boolean }) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const after = events.length ? events[events.length - 1].seq : 0;
  useQuery({
    queryKey: ["harness-events", runId, after],
    queryFn: async () => {
      const page = await harnessApi.events(runId, after);
      if (page.events.length) {
        setEvents((prior) => {
          const last = prior.length ? prior[prior.length - 1].seq : 0;
          return [...prior, ...page.events.filter((e) => e.seq > last)];
        });
      }
      return page;
    },
    refetchInterval: live ? 1000 : false,
  });
  const [open, setOpen] = useState<number | null>(null);
  return (
    <ol className="hx-journal">
      {events.map((event) => (
        <li key={event.seq} className={`hx-journal__row hx-type--${event.type.split(".")[0]}`}>
          <button className="hx-journal__head" onClick={() => setOpen(open === event.seq ? null : event.seq)}>
            <span className="mono muted hx-seq">#{event.seq}</span>
            <span className="mono hx-etype">{event.type}</span>
            <span className="muted hx-esum">{summarize(event)}</span>
            <span className="muted mono">{when(event.created_at)}</span>
          </button>
          {open === event.seq ? <pre className="code-view hx-pre">{JSON.stringify(event.data, null, 2)}</pre> : null}
        </li>
      ))}
    </ol>
  );
}

function summarize(event: RunEvent): string {
  const d = event.data as Record<string, any>;
  if (event.type === "step.dispatched") return `${d.step_id} ${d.action}${d.required ? "" : " (optional)"} · fence ${d.fence}`;
  if (event.type === "step.completed") return `${d.step_id} → ${d.outcome}${d.verification ? ` · ${d.verification.criterion}: ${d.verification.outcome}` : ""}${d.evidence?.length ? ` · ${d.evidence.length} evidence` : ""}`;
  if (event.type === "step.denied") return `${d.action}: ${d.decision?.reason}`;
  if (event.type.startsWith("effect.")) return `${d.operation_id ?? ""} ${d.adapter ?? ""} ${d.disposition ?? d.reason ?? ""}`.trim();
  if (event.type === "verification.failed") return (d.findings ?? []).map((f: any) => f.criterion).join(", ");
  if (event.type === "run.accepted") return `${d.task?.workload} · ${(d.criteria ?? []).length} criteria`;
  if (d.reason) return String(d.reason);
  return "";
}

function Sandboxes({ detail }: { detail: RunDetail }) {
  const iso = detail.isolation as Record<string, any>;
  return (
    <div className="hx-sandboxes">
      <div className="card hx-iso">
        <h3>Declared isolation</h3>
        <div className="kv">
          {["mechanism", "network", "credentials", "cleanup_deadline_seconds", "disposal_confirmation"].map((key) => (
            <div className="kv__row" key={key}><span className="kv__key">{key.replace(/_/g, " ")}</span><span className="kv__value">{String(iso[key])}</span></div>))}
        </div>
      </div>
      <div className="table-scroll">
        <table className="hx-table">
          <thead><tr><th>Generation</th><th>Profile</th><th>State</th><th>Created</th><th>Disposed</th><th>Disposal</th></tr></thead>
          <tbody>{detail.sandboxes.map((box) => (
            <tr key={box.generation}>
              <td className="mono">{box.generation}</td><td>{box.profile}</td><td><Pill value={box.state} /></td>
              <td className="mono">{when(box.created_at)}</td><td className="mono">{when(box.destroyed_at)}</td>
              <td className="muted">{box.disposal ? `${box.disposal.reason} · ${box.disposal_ms?.toFixed(0)} ms · ${box.disposal.confirmed ? "confirmed" : "unconfirmed"}` : "—"}</td>
            </tr>))}</tbody>
        </table>
      </div>
    </div>
  );
}

function ApprovalCard({ runId, wait, onDone }: { runId: string; wait: RunWait; onDone: () => void }) {
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const decide = useMutation({
    mutationFn: (decision: "approve" | "reject") => harnessApi.decide(runId, wait.operation_id!, decision, note),
    onSuccess: () => { setError(null); onDone(); },
    onError: (failure: Error) => setError(failure.message),
  });
  return (
    <div className="card hx-approval" role="region" aria-label="Write awaiting approval">
      <div className="hx-approval__head">
        <Pill value="waiting" />
        <strong>One write through <span className="mono">{wait.connection}</span> waits for an operator</strong>
      </div>
      <div className="kv">
        <div className="kv__row"><span className="kv__key">Action</span><span className="kv__value">{wait.action} on {wait.provider}</span></div>
        <div className="kv__row"><span className="kv__key">Resource</span><span className="kv__value mono">{wait.resource}</span></div>
        <div className="kv__row"><span className="kv__key">Exact payload</span><span className="kv__value mono">{wait.payload_digest}</span></div>
        <div className="kv__row"><span className="kv__key">Operation</span><span className="kv__value mono">{wait.operation_id}</span></div>
        <div className="kv__row"><span className="kv__key">Requested by</span><span className="kv__value">{wait.requested_by}{wait.self_approval ? "" : " (may not approve it)"}</span></div>
        <div className="kv__row"><span className="kv__key">Condition</span><span className="kv__value">{wait.expected_target_version ? `target still at ${wait.expected_target_version}` : "none; recorded on the commit"}</span></div>
      </div>
      {wait.preview ? <pre className="code-view hx-pre hx-approval__preview">{wait.preview}</pre> : null}
      <div className="hx-approval__bar">
        <input className="text-input" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Note for the journal (optional)" aria-label="Approval note" />
        <button className="btn btn--small btn--primary" disabled={decide.isPending} onClick={() => decide.mutate("approve")}>Approve this write</button>
        <button className="btn btn--small btn--danger" disabled={decide.isPending} onClick={() => decide.mutate("reject")}>Reject</button>
      </div>
      <p className="muted hx-approval__fine">Approving cannot widen the connection's policy: it is checked again before the request is sent.</p>
      {error ? <p className="error" role="alert">{error}</p> : null}
    </div>
  );
}

export function RunView({ runId }: { runId: string }) {
  const client = useQueryClient();
  const [tab, setTab] = useState<Tab>("Deliverable");
  const [focus, setFocus] = useState<string | null>(null);
  const detail = useQuery({
    queryKey: ["harness-run", runId],
    queryFn: () => harnessApi.detail(runId),
    refetchInterval: (query) => (query.state.data && ACTIVE.has(query.state.data.state) ? 1000 : false),
  });
  const refresh = () => { client.invalidateQueries({ queryKey: ["harness-run", runId] }); client.invalidateQueries({ queryKey: ["harness-runs"] }); };
  const resume = useMutation({ mutationFn: () => harnessApi.resume(runId, { approve: true }), onSuccess: refresh });
  const cancel = useMutation({ mutationFn: () => harnessApi.cancel(runId), onSuccess: refresh });
  const resolve = useMutation({
    mutationFn: (args: { op: string; disposition: "applied" | "not_applied" }) => harnessApi.resolveEffect(runId, args.op, args.disposition),
    onSuccess: refresh,
  });
  const run = detail.data;
  if (detail.isLoading) return <p className="muted">Loading run…</p>;
  if (detail.error) return <p className="error" role="alert">{(detail.error as Error).message}</p>;
  if (!run) return null;
  const spent = run.budget.spent, limits = run.budget.limits;
  const result = run.result;
  const handoff = result?.handoff;
  const uncertain = run.effects.find((effect) => effect.state === "uncertain");
  return (
    <section className="hx-run" aria-label="Run">
      <header className="hx-run__head">
        <div>
          <p className="hx-eyebrow"><span className="mono">{run.run_id}</span> · {run.workload.replace("_", " ")} · {run.class} lane · tenant {run.tenant}</p>
          <h2 className="hx-run__title">{run.question}</h2>
        </div>
        <div className="hx-run__actions">
          {ACTIVE.has(run.state) || run.state === "waiting" ? <button className="btn btn--small btn--danger" onClick={() => cancel.mutate()}>Cancel</button> : null}
        </div>
      </header>
      <Lifecycle state={run.state} />
      {run.state === "waiting" && run.wait?.kind === "approval" ? <ApprovalCard runId={run.run_id} wait={run.wait} onDone={refresh} /> : null}
      {run.state === "waiting" && run.wait && run.wait.kind !== "approval" ? (
        <div className="hx-banner hx-banner--warn"><span>Waiting: {run.wait.reason}. The workspace was released; resuming checks authority again.</span>
          <button className="btn btn--small btn--primary" onClick={() => resume.mutate()}>Approve and resume</button></div>
      ) : null}
      {uncertain ? (
        <div className="hx-banner hx-banner--warn">
          <span>Effect <span className="mono">{uncertain.operation_id}</span> may have reached <strong>{uncertain.adapter}</strong>. It cannot be deduplicated or observed, so it will not be repeated until someone checks the target.</span>
          <span className="hx-banner__buttons">
            <button className="btn btn--small" onClick={() => resolve.mutate({ op: uncertain.operation_id, disposition: "applied" })}>It was applied</button>
            <button className="btn btn--small btn--ghost" onClick={() => resolve.mutate({ op: uncertain.operation_id, disposition: "not_applied" })}>It was not</button>
          </span>
        </div>
      ) : null}
      {result && result.status !== "succeeded" && result.reason ? (
        <div className={`hx-banner ${result.status === "failed" ? "hx-banner--danger" : "hx-banner--warn"}`}>
          <span><strong>{result.status}.</strong> {result.reason}{result.next_action ? ` Next: ${result.next_action}.` : ""}
            {result.blocked?.length ? ` Blocked: ${result.blocked.map((b) => `${b.registration_id} (${b.reason})`).join(", ")}.` : ""}
            {result.unverified ? ` Unverified: ${Object.values(result.unverified).flat().join(", ")}.` : ""}</span>
        </div>
      ) : null}
      <div className="hx-cards">
        <div className="card hx-card">
          <h3>Budget</h3>
          <Meter label="Tool calls" spent={spent.tool_calls ?? 0} limit={limits.tool_calls} />
          <Meter label="Sandbox seconds" spent={spent.sandbox_seconds ?? 0} limit={limits.sandbox_seconds} />
          <Meter label="Bytes scanned" spent={spent.bytes_scanned ?? 0} limit={limits.bytes_scanned} unit="MB" />
          <Meter label="Model calls" spent={spent.model_calls ?? 0} limit={limits.model_calls} />
        </div>
        <div className="card hx-card">
          <h3>Ownership</h3>
          <div className="kv">
            <div className="kv__row"><span className="kv__key">Fence</span><span className="kv__value">{run.fence}</span></div>
            <div className="kv__row"><span className="kv__key">Lease</span><span className="kv__value">{run.lease.held ? `held by ${run.lease.owner}` : "free"}</span></div>
            <div className="kv__row"><span className="kv__key">Journal</span><span className="kv__value">{run.last_seq} events</span></div>
            <div className="kv__row"><span className="kv__key">Workflow</span><span className="kv__value">v{run.workflow_version} · {String(run.versions.planner)}</span></div>
            <div className="kv__row"><span className="kv__key">Integrity</span><span className="kv__value">{run.integrity.ok ? "every object present" : `${run.integrity.missing.length} missing`}</span></div>
          </div>
        </div>
        <div className="card hx-card">
          <h3>Delivery</h3>
          {run.effects.length ? run.effects.map((effect) => (
            <div className="kv" key={effect.operation_id}>
              <div className="kv__row"><span className="kv__key">Operation</span><span className="kv__value">{effect.operation_id}</span></div>
              <div className="kv__row"><span className="kv__key">Target</span><span className="kv__value">{effect.adapter}{effect.target ? ` → ${effect.target}` : ""}</span></div>
              <div className="kv__row"><span className="kv__key">State</span><span className="kv__value"><Pill value={effect.state} /> dispatched {effect.dispatch_count}×</span></div>
              {effect.disposition ? <div className="kv__row"><span className="kv__key">Disposition</span><span className="kv__value">{effect.disposition}</span></div> : null}
            </div>
          )) : <p className="muted">Nothing delivered. Delivery waits for every required check on the exact candidate.</p>}
          {handoff ? <p className="hx-handoff"><Pill value={`${handoff.verified_criteria.length} verified`} />{" "}
            {handoff.unverified_criteria.length ? <Pill value={`${handoff.unverified_criteria.length} unverified`} /> : null}{" "}
            {handoff.coverage_gaps.length ? <span className="muted">gap: {handoff.coverage_gaps.join(", ")}</span> : null}</p> : null}
        </div>
      </div>
      <div className="tabs hx-tabs" role="tablist">
        {TABS.map((name) => (
          <button key={name} role="tab" aria-selected={tab === name} className={`tab${tab === name ? " active" : ""}`} onClick={() => setTab(name)}>
            {name}{name === "Evidence" ? ` ${run.evidence.length}` : name === "Steps" ? ` ${run.steps.length}` : name === "Connections" ? ` ${run.connection_calls?.length ?? 0}` : ""}
          </button>
        ))}
      </div>
      <div className="hx-tabpanel">
        {tab === "Deliverable" ? <Deliverable detail={run} onCite={(id) => { setFocus(id); setTab("Evidence"); }} /> : null}
        {tab === "Verification" ? <VerificationMatrix detail={run} /> : null}
        {tab === "Evidence" ? <EvidenceList detail={run} focus={focus} /> : null}
        {tab === "Steps" ? <StepsTable detail={run} /> : null}
        {tab === "Connections" ? <CallsTable calls={run.connection_calls ?? []} showRun={false} /> : null}
        {tab === "Journal" ? <Journal runId={run.run_id} live={ACTIVE.has(run.state)} /> : null}
        {tab === "Sandboxes" ? <Sandboxes detail={run} /> : null}
      </div>
    </section>
  );
}
