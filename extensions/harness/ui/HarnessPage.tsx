import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { harnessApi } from "./api";
import { NewRun } from "./NewRun";
import { Pill, RunView } from "./RunView";
import type { Registration } from "./types";

const ago = (stamp: number) => {
  const seconds = Math.max(0, Date.now() / 1000 - stamp);
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
};

const shortUri = (uri: string) => {
  const match = /^([a-z+]+:\/\/)(.*)$/.exec(uri);
  if (!match) return uri;
  const parts = match[2].split("/").filter(Boolean);
  return parts.length > 2 ? `${match[1]}…/${parts.slice(-2).join("/")}` : uri;
};

function Registrations({ registrations }: { registrations: Registration[] }) {
  const client = useQueryClient();
  const [uri, setUri] = useState("");
  const [pointerOnly, setPointerOnly] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const register = useMutation({
    mutationFn: () => harnessApi.register({ uri, operations: pointerOnly ? ["resolve", "read"] : undefined }),
    onSuccess: () => { setUri(""); client.invalidateQueries({ queryKey: ["harness-registrations"] }); },
    onError: (failure: Error) => setError(failure.message),
  });
  return (
    <section className="hx-regs">
      <h3 className="section__title">Registered places to look</h3>
      <ul className="hx-reglist">
        {registrations.map((reg) => (
          <li key={reg.id} className={reg.authorized ? "" : "is-denied"}>
            <div className="hx-reg__line"><strong>{reg.label}</strong><span className="pill">{reg.resolver}</span>
              {!reg.authorized ? <Pill value="denied" /> : null}</div>
            <div className="muted mono hx-reg__uri" title={reg.uri}>{shortUri(reg.uri)}</div>
            <div className="muted hx-reg__ops">{reg.operations.join(" · ")}</div>
          </li>
        ))}
      </ul>
      <form className="hx-reg__form" onSubmit={(event) => { event.preventDefault(); setError(null); register.mutate(); }}>
        <input className="text-input" value={uri} onChange={(e) => setUri(e.target.value)}
          placeholder="file:///data/repo or pheasant://source/prefix" aria-label="URI to register" />
        <label className="hx-check"><input type="checkbox" checked={pointerOnly} onChange={(e) => setPointerOnly(e.target.checked)} />
          <span>Pointer only (read by locator, no enumeration)</span></label>
        <button className="btn btn--small" data-permission="sources.manage" disabled={!uri || register.isPending}>Register</button>
        {error ? <p className="error" role="alert">{error}</p> : null}
      </form>
    </section>
  );
}

export function HarnessPage() {
  const [search, setSearch] = useSearchParams();
  const selected = search.get("run");
  const composing = search.get("new") === "1" || !selected;
  const runs = useQuery({
    queryKey: ["harness-runs"],
    queryFn: harnessApi.runs,
    refetchInterval: (query) => query.state.data?.runs.some((run) =>
      ["accepted", "running", "verifying", "delivering", "reconciling"].includes(run.state)) ? 1500 : 6000,
  });
  const registrations = useQuery({ queryKey: ["harness-registrations"], queryFn: harnessApi.registrations });
  const regs = registrations.data?.registrations ?? [];
  return (
    <div className="page page--wide hx-page">
      <header className="page__header">
        <div>
          <h1>Harness</h1>
          <p className="muted">Durable, verified runs. The runtime investigates the sources, builds the result and proves it before delivering.</p>
        </div>
        <button className="btn btn--primary" onClick={() => setSearch({ new: "1" })}>New run</button>
      </header>
      <div className="hx-layout">
        <aside className="hx-rail">
          <h3 className="section__title">Runs</h3>
          {runs.isLoading ? <p className="muted">Loading…</p> : null}
          <ul className="hx-runlist">
            {(runs.data?.runs ?? []).map((run) => (
              <li key={run.run_id}>
                <button className={`hx-runitem${run.run_id === selected && !composing ? " is-active" : ""}`}
                  onClick={() => setSearch({ run: run.run_id })}>
                  <span className="hx-runitem__top"><Pill value={run.state} /><span className="muted">{run.workload.replace("_", " ")}</span>
                    <span className="muted hx-runitem__time">{ago(run.updated_at)}</span></span>
                  <span className="hx-runitem__q">{run.question}</span>
                </button>
              </li>
            ))}
          </ul>
          {runs.data && !runs.data.runs.length ? <p className="muted">No runs yet.</p> : null}
          <Registrations registrations={regs} />
        </aside>
        <main className="hx-main">
          {composing ? <NewRun registrations={regs} onStarted={(runId) => setSearch({ run: runId })} /> : <RunView key={selected!} runId={selected!} />}
        </main>
      </div>
    </div>
  );
}
