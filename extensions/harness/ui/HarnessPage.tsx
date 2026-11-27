import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { harnessApi } from "./api";
import { ConnectionsPanel } from "./Connections";
import { Composer } from "./Composer";
import { Pill, RunView } from "./RunView";
import { OPERATIONS, PLACE_KINDS } from "./places";
import type { Registration } from "./types";

const ago = (stamp: number) => {
  const seconds = Math.max(0, Date.now() / 1000 - stamp);
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
};

const ACTIVE = ["accepted", "running", "verifying", "delivering", "reconciling"];

function Places({ registrations }: { registrations: Registration[] }) {
  const client = useQueryClient();
  const [uri, setUri] = useState("");
  const [label, setLabel] = useState("");
  const [pointerOnly, setPointerOnly] = useState(false);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState<string | null>(null);
  const register = useMutation({
    mutationFn: () => harnessApi.register({ uri, label: label || undefined, operations: pointerOnly ? ["resolve", "read"] : undefined }),
    onSuccess: () => { setUri(""); setLabel(""); client.invalidateQueries({ queryKey: ["harness-registrations"] }); },
    onError: (failure: Error) => setError(failure.message),
  });
  const shown = registrations.filter((reg) =>
    !filter || `${reg.label} ${reg.uri} ${reg.resolver}`.toLowerCase().includes(filter.toLowerCase()));
  return (
    <div className="hx-places">
      <section className="card hx-places__add">
        <h2 className="hx-h2">Add a place to look</h2>
        <p className="muted">
          A place is somewhere a run may read: a source Pheasant already indexes, a folder or Git repository on the
          server, or a repository or folder in a connected account. Runs only ever read the places they are given.
        </p>
        <form className="hx-places__form" onSubmit={(event) => { event.preventDefault(); setError(null); register.mutate(); }}>
          <label className="field hx-places__uri"><span>Where</span>
            <input className="text-input mono" value={uri} onChange={(e) => setUri(e.target.value)}
              placeholder="pheasant://notes  ·  file:///data/repo  ·  connection://github/acme/handbook" aria-label="URI to register" /></label>
          <label className="field"><span>Name (optional)</span>
            <input className="text-input" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Team handbook" /></label>
          <label className="hx-check"><input type="checkbox" checked={pointerOnly} onChange={(e) => setPointerOnly(e.target.checked)} />
            <span>Read named files only — a run cannot list or search this place</span></label>
          <button className="btn btn--primary" data-permission="sources.manage" disabled={!uri || register.isPending}>
            {register.isPending ? "Adding…" : "Add place"}
          </button>
          {error ? <p className="error" role="alert">{error}</p> : null}
        </form>
      </section>
      <section className="hx-places__list">
        <div className="hx-section-head">
          <h2 className="hx-h2">Places runs can read <span className="muted">({registrations.length})</span></h2>
          <input className="text-input text-input--small" placeholder="Filter places…" value={filter}
            onChange={(e) => setFilter(e.target.value)} aria-label="Filter places" />
        </div>
        <ul className="hx-placegrid hx-scroll" aria-label="Registered places">
          {shown.map((reg) => {
            const kind = PLACE_KINDS[reg.resolver] ?? { label: reg.resolver, reads: "" };
            return (
              <li key={reg.id} className={`card hx-place${reg.authorized ? "" : " is-denied"}`}>
                <div className="hx-place__head">
                  <strong className="hx-place__name">{reg.label}</strong>
                  <span className="pill">{kind.label}</span>
                  {!reg.authorized ? <Pill value="no access" /> : null}
                  {reg.revoked ? <Pill value="revoked" /> : null}
                </div>
                <div className="mono small hx-place__uri" title={reg.uri}>{reg.uri}</div>
                <p className="muted small">Reads {kind.reads}. A run may {reg.operations.map((op) => OPERATIONS[op] ?? op).join(", ")}.</p>
              </li>
            );
          })}
          {!shown.length ? <li className="muted">{registrations.length ? "No place matches the filter." : "No places yet. Add one above, attach files to a run, or pick a repository when you start one."}</li> : null}
        </ul>
      </section>
    </div>
  );
}

function RunsRail({ selected, onOpen }: { selected: string | null; onOpen: (runId: string) => void }) {
  const [filter, setFilter] = useState("");
  const [state, setState] = useState<"all" | "active" | "waiting" | "done" | "problem">("all");
  const runs = useQuery({
    queryKey: ["harness-runs"],
    queryFn: harnessApi.runs,
    refetchInterval: (query) => query.state.data?.runs.some((run) => ACTIVE.includes(run.state)) ? 1500 : 6000,
  });
  const all = runs.data?.runs ?? [];
  const shown = useMemo(() => all.filter((run) => {
    if (filter && !`${run.question} ${run.workload} ${run.run_id}`.toLowerCase().includes(filter.toLowerCase())) return false;
    if (state === "active") return ACTIVE.includes(run.state);
    if (state === "waiting") return run.state === "waiting";
    if (state === "done") return run.state === "succeeded";
    if (state === "problem") return ["failed", "partial", "cancelled"].includes(run.state);
    return true;
  }), [all, filter, state]);
  const live = all.filter((run) => ACTIVE.includes(run.state)).length;
  return (
    <aside className="hx-rail" aria-label="Runs">
      <div className="hx-section-head">
        <h3 className="section__title">Runs <span className="muted">({all.length}{live ? ` · ${live} working` : ""})</span></h3>
      </div>
      <div className="hx-rail__filters">
        <input className="text-input text-input--small" placeholder="Find a run…" value={filter}
          onChange={(e) => setFilter(e.target.value)} aria-label="Find a run" />
        <select className="text-input text-input--small" value={state} onChange={(e) => setState(e.target.value as typeof state)} aria-label="Show">
          <option value="all">All</option>
          <option value="active">Working</option>
          <option value="waiting">Waiting for someone</option>
          <option value="done">Succeeded</option>
          <option value="problem">Failed or partial</option>
        </select>
      </div>
      {runs.isLoading ? <p className="muted">Loading…</p> : null}
      <ul className="hx-runlist hx-scroll">
        {shown.map((run) => (
          <li key={run.run_id}>
            <button className={`hx-runitem${run.run_id === selected ? " is-active" : ""}`} onClick={() => onOpen(run.run_id)}>
              <span className="hx-runitem__top"><Pill value={run.state} /><span className="muted">{run.workload.replace(/_/g, " ")}</span>
                <span className="muted hx-runitem__time">{ago(run.updated_at)}</span></span>
              <span className="hx-runitem__q">{run.question}</span>
            </button>
          </li>
        ))}
        {runs.data && !shown.length ? <li className="muted hx-empty">{all.length ? "No run matches." : "No runs yet. Describe what to build to start one, or type @harness in the chat."}</li> : null}
      </ul>
    </aside>
  );
}

export function HarnessPage() {
  const [search, setSearch] = useSearchParams();
  const selected = search.get("run");
  const view = search.get("view");
  const composing = !view && (search.get("new") === "1" || !selected);
  const registrations = useQuery({ queryKey: ["harness-registrations"], queryFn: harnessApi.registrations });
  const regs = registrations.data?.registrations ?? [];
  const tabs = [
    { id: "work", label: "Runs", active: !view, go: () => setSearch(selected ? { run: selected } : {}) },
    { id: "places", label: `Places (${regs.length})`, active: view === "places", go: () => setSearch({ view: "places" }) },
    { id: "connections", label: "Connections", active: view === "connections", go: () => setSearch({ view: "connections" }), permission: "sources.manage" },
  ];
  return (
    <div className="page page--wide hx-page">
      <header className="page__header hx-header">
        <div>
          <h1>Harness</h1>
          <p className="muted">Build and change things from what Pheasant knows: work in a repository, branch and folder, attach files, and keep the verified result as an artifact or a commit. In the chat, start one with <span className="mono">@harness</span>.</p>
        </div>
        <div className="hx-head-actions">
          <nav className="tabs" role="tablist" aria-label="Harness views">
            {tabs.map((tab) => (
              <button key={tab.id} role="tab" aria-selected={tab.active} className={`tab${tab.active ? " active" : ""}`}
                data-permission={tab.permission} onClick={tab.go}>{tab.label}</button>
            ))}
          </nav>
          <button className="btn btn--primary" onClick={() => setSearch({ new: "1" })}>New run</button>
        </div>
      </header>
      {view === "places" ? <Places registrations={regs} />
        : view === "connections" ? <ConnectionsPanel />
        : (
          <div className="hx-layout">
            <RunsRail selected={composing ? null : selected} onOpen={(runId) => setSearch({ run: runId })} />
            <main className="hx-main">
              {composing
                ? <Composer registrations={regs} initialPrompt={search.get("prompt") ?? ""} onStarted={(runId) => setSearch({ run: runId })} />
                : <RunView key={selected!} runId={selected!} />}
            </main>
          </div>
        )}
    </div>
  );
}
