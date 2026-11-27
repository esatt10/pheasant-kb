import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { harnessApi } from "./api";
import { Pill } from "./RunView";
import type { ConnectionCall, ConnectionInfo, ConnectionsOverview } from "./types";

const when = (stamp: number) => new Date(stamp * 1000).toLocaleTimeString();
const lines = (text: string) => text.split("\n").map((line) => line.trim()).filter(Boolean);

export function CallsTable({ calls, showRun = true }: { calls: ConnectionCall[]; showRun?: boolean }) {
  if (!calls.length) return <p className="muted">No requests through a connection yet.</p>;
  return (
    <div className="table-scroll">
      <table className="hx-table hx-calls">
        <thead><tr><th>Time</th>{showRun ? <th>Run</th> : null}<th>Who</th><th>Connection</th><th>Action</th>
          <th>Resource</th><th>Outcome</th><th>HTTP</th><th>Bytes</th><th>Why</th></tr></thead>
        <tbody>{calls.map((call) => (
          <tr key={call.id} className={call.outcome === "denied" ? "is-denied" : ""}>
            <td className="mono">{when(call.at)}</td>
            {showRun ? <td className="mono">{call.run_id ? call.run_id.slice(0, 12) : "direct"}</td> : null}
            <td>{call.actor}<span className="muted"> · {call.purpose}</span></td>
            <td><strong>{call.connection}</strong><span className="muted"> · v{call.policy_version ?? "—"}</span></td>
            <td><span className="pill">{call.action}</span>{call.method ? <span className="muted mono"> {call.method}</span> : null}</td>
            <td className="mono hx-calls__resource" title={call.resource}>{call.resource}</td>
            <td><Pill value={call.outcome} /></td>
            <td className="mono">{call.status_code ?? "—"}</td>
            <td className="mono">{call.bytes || "—"}</td>
            <td className="muted hx-calls__why">{call.reason ?? (call.flags.reflected_credential_removed ? "credential echoed by the service; removed" : "")}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function PolicyEditor({ connection, overview }: { connection: ConnectionInfo; overview: ConnectionsOverview }) {
  const client = useQueryClient();
  const policy = connection.policy;
  const [actions, setActions] = useState<string[]>(policy?.actions ?? []);
  const [resources, setResources] = useState((policy?.resources ?? []).join("\n"));
  const [writes, setWrites] = useState((policy?.write_resources ?? []).join("\n"));
  const [approval, setApproval] = useState(policy?.approval.write ?? "required");
  const [selfApproval, setSelfApproval] = useState(policy?.approval.self_approval ?? false);
  const [requests, setRequests] = useState(String(policy?.limits.max_requests_per_run ?? overview.defaults.limits.max_requests_per_run));
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setActions(policy?.actions ?? []);
    setResources((policy?.resources ?? []).join("\n"));
    setWrites((policy?.write_resources ?? []).join("\n"));
  }, [policy?.version]); // eslint-disable-line react-hooks/exhaustive-deps
  const refresh = () => client.invalidateQueries({ queryKey: ["harness-connections"] });
  const save = useMutation({
    mutationFn: () => harnessApi.setPolicy(connection.name, {
      actions,
      resources: lines(resources),
      write_resources: actions.includes("write") ? lines(writes) : [],
      limits: { max_requests_per_run: Number(requests) || overview.defaults.limits.max_requests_per_run },
      approval: { write: approval, self_approval: selfApproval },
      enabled: true,
    }),
    onSuccess: () => { setError(null); refresh(); },
    onError: (failure: Error) => setError(failure.message),
  });
  const disable = useMutation({ mutationFn: () => harnessApi.disablePolicy(connection.name), onSuccess: refresh });
  const toggle = (action: string) => setActions(actions.includes(action) ? actions.filter((a) => a !== action) : [...actions, action]);
  return (
    <form className="hx-policy" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
      <fieldset className="hx-policy__actions">
        <legend>Allowed actions</legend>
        {Object.entries(overview.actions).map(([name, spec]) => {
          const offered = connection.offers.includes(name);
          return (
            <label key={name} className={`hx-action${offered ? "" : " is-unavailable"}${spec.kind === "write" ? " is-write" : ""}`} title={spec.description}>
              <input type="checkbox" disabled={!offered} checked={actions.includes(name)} onChange={() => toggle(name)} />
              <span><strong>{name}</strong> <span className="muted">{offered ? spec.methods.join("/") : `not offered by ${connection.provider}`}</span></span>
            </label>
          );
        })}
        <div className="hx-never">
          {overview.never.map((name) => <span key={name} className="hx-never__item" title="The harness never performs this through anyone's account">{name}</span>)}
          <span className="muted"> never, whatever the policy says</span>
        </div>
      </fieldset>
      <div className="hx-grid2">
        <label className="field"><span>Read resources <span className="muted">({connection.resource_grammar})</span></span>
          <textarea className="text-input mono" rows={3} value={resources} onChange={(e) => setResources(e.target.value)}
            placeholder={connection.provider === "github" ? "acme/handbook:*" : "folder-id/*"} /></label>
        <label className="field"><span>Write resources <span className="muted">(where a delivery may land)</span></span>
          <textarea className="text-input mono" rows={3} value={writes} disabled={!actions.includes("write")} onChange={(e) => setWrites(e.target.value)}
            placeholder={connection.provider === "github" ? "acme/handbook@harness/*:answers/*" : "folder-id/answers-*"} /></label>
      </div>
      <div className="hx-grid3">
        <label className="field"><span>Writes need approval</span>
          <select className="text-input" value={approval} onChange={(e) => setApproval(e.target.value as "required" | "none")}>
            <option value="required">Yes, by an operator</option>
            <option value="none">No</option>
          </select></label>
        <label className="hx-check hx-check--inline"><input type="checkbox" checked={selfApproval} onChange={(e) => setSelfApproval(e.target.checked)} />
          <span>Requester may approve their own write</span></label>
        <label className="field"><span>Requests per run</span>
          <input className="text-input mono" inputMode="numeric" value={requests} onChange={(e) => setRequests(e.target.value)} /></label>
      </div>
      {error ? <p className="error" role="alert">{error}</p> : null}
      <div className="hx-policy__bar">
        <button className="btn btn--small btn--primary" data-permission="security.manage" disabled={save.isPending}>
          {policy ? `Save as v${policy.version + 1}` : "Allow these actions"}</button>
        {policy?.enabled ? <button type="button" className="btn btn--small btn--danger" data-permission="security.manage" onClick={() => disable.mutate()}>Disable now</button> : null}
        {policy ? <span className="muted">v{policy.version} by {policy.updated_by} · {when(policy.updated_at)}</span> : null}
      </div>
    </form>
  );
}

export function ConnectionsPanel() {
  const overview = useQuery({ queryKey: ["harness-connections"], queryFn: harnessApi.connections });
  const calls = useQuery({ queryKey: ["harness-connection-calls"], queryFn: () => harnessApi.connectionCalls({ limit: 80 }), refetchInterval: 4000 });
  const [open, setOpen] = useState<string | null>(null);
  if (overview.isLoading) return <p className="muted">Loading connections…</p>;
  if (overview.error) return <p className="error" role="alert">{(overview.error as Error).message}</p>;
  const data = overview.data!;
  const summary = calls.data?.summary ?? {};
  return (
    <section className="hx-connections" aria-label="Connections">
      <header className="hx-new__head">
        <h2>Connections</h2>
        <p className="muted">Accounts this region has connected. The harness can use one only after an operator says what it may do: which actions,
          on which resources, how much per run, and whether a write waits for approval. The credential never leaves the gateway.</p>
      </header>
      <div className="hx-conn-grid">
        {data.connections.map((connection) => {
          const policy = connection.policy;
          const state = !connection.supported ? "unsupported" : !policy ? "no policy" : policy.enabled ? "enabled" : "disabled";
          return (
            <article key={connection.name} className={`card hx-conn${open === connection.name ? " is-open" : ""}`}>
              <button className="hx-conn__head" onClick={() => setOpen(open === connection.name ? null : connection.name)} aria-expanded={open === connection.name}>
                <span className="hx-conn__name"><strong>{connection.name}</strong><span className="pill">{connection.provider}</span></span>
                <span className="hx-conn__state"><Pill value={state} /></span>
              </button>
              <div className="hx-conn__summary">
                {policy ? (
                  <>
                    <span className="hx-chips">{policy.actions.map((a) => <span key={a} className={`hx-chip${a === "write" ? " hx-chip--write" : ""}`}>{a}</span>)}</span>
                    <span className="mono muted hx-conn__res">{[...policy.resources, ...policy.write_resources.map((w) => `✎ ${w}`)].join("  ·  ")}</span>
                    {policy.actions.includes("write") ? <span className="muted">writes: {policy.approval.write === "required" ? `operator approval${policy.approval.self_approval ? " (self allowed)" : ", never the requester"}` : "no approval"}</span> : null}
                  </>
                ) : <span className="muted">{connection.supported ? "Unusable by the harness until a policy allows something." : "The harness does not work through this provider."}</span>}
              </div>
              {open === connection.name && connection.supported ? <PolicyEditor connection={connection} overview={data} /> : null}
            </article>
          );
        })}
        {!data.connections.length ? <p className="muted">No connected accounts. Connect one on the Sources page first.</p> : null}
      </div>
      <div className="hx-audit">
        <div className="hx-audit__head">
          <h3 className="section__title">Requests through connections</h3>
          <span className="hx-chips">{Object.entries(summary).map(([k, v]) => <span key={k} className="hx-chip">{v} {k}</span>)}</span>
        </div>
        <CallsTable calls={calls.data?.calls ?? []} />
      </div>
    </section>
  );
}
