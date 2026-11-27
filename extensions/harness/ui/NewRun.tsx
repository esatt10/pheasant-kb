import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { harnessApi } from "./api";
import type { Registration } from "./types";

const WORKLOADS = [
  { id: "answer", label: "Answer a question", hint: "Search and grep with coverage, read in context, cite exact locations." },
  { id: "summarize", label: "Summarize", hint: "Inventory every document, read all of it, keep exceptions and dates." },
  { id: "build_content", label: "Build content", hint: "A brief with figures, rendered and checked as its reader sees it." },
  { id: "code_change", label: "Change code", hint: "Pinned base, isolated workspace, builder and independent checks." },
] as const;

const list = (text: string) => text.split(",").map((part) => part.trim()).filter(Boolean);

function parseJson(text: string, fallback: unknown) {
  if (!text.trim()) return fallback;
  return JSON.parse(text);
}

export function NewRun({ registrations, onStarted }: { registrations: Registration[]; onStarted: (runId: string) => void }) {
  const client = useQueryClient();
  const readable = registrations.filter((reg) => reg.authorized && !reg.revoked);
  const [workload, setWorkload] = useState<string>("answer");
  const [question, setQuestion] = useState("");
  const [inputs, setInputs] = useState<string[]>([]);
  const [identifiers, setIdentifiers] = useState("");
  const [symbols, setSymbols] = useState("");
  const [discovery, setDiscovery] = useState(true);
  const [adapter, setAdapter] = useState("record");
  const [target, setTarget] = useState("");
  const [testCommand, setTestCommand] = useState("python -m pytest -q -p no:cacheprovider");
  const [proposals, setProposals] = useState("");
  const [acceptance, setAcceptance] = useState("");
  const [error, setError] = useState<string | null>(null);
  const hint = useMemo(() => WORKLOADS.find((w) => w.id === workload)?.hint, [workload]);

  const start = useMutation({
    mutationFn: () => {
      const request: Record<string, unknown> = {
        workload, question, inputs, identifiers: list(identifiers), symbols: list(symbols),
        discovery: discovery ? "auto" : "off",
        destination: { adapter, target },
        acceptance: parseJson(acceptance, []),
      };
      if (workload === "code_change") {
        request.test_command = testCommand.split(/\s+/).filter(Boolean);
        request.proposals = parseJson(proposals, []);
      }
      return harnessApi.start({ request, client_key: `ui-${Date.now()}-${Math.random().toString(36).slice(2, 8)}` });
    },
    onSuccess: (status) => {
      client.invalidateQueries({ queryKey: ["harness-runs"] });
      onStarted(status.run_id);
    },
    onError: (failure: Error) => setError(failure.message),
  });

  return (
    <form className="card hx-new" onSubmit={(event) => { event.preventDefault(); setError(null); start.mutate(); }}>
      <div className="hx-new__head">
        <h2>New run</h2>
        <p className="muted">The runtime owns the investigation. You state the result and how it will be checked.</p>
      </div>
      <div className="hx-workloads" role="radiogroup" aria-label="Workload">
        {WORKLOADS.map((option) => (
          <button key={option.id} type="button" role="radio" aria-checked={workload === option.id}
            className={`hx-workload${workload === option.id ? " hx-workload--active" : ""}`}
            onClick={() => setWorkload(option.id)}>
            {option.label}
          </button>
        ))}
      </div>
      <p className="muted hx-hint">{hint}</p>
      <label className="field">
        <span>Request</span>
        <textarea className="text-input" rows={3} required value={question} onChange={(e) => setQuestion(e.target.value)}
          placeholder="What should the runtime produce, for whom?" />
      </label>
      <fieldset className="field hx-inputs">
        <legend>Where to look</legend>
        {readable.length ? readable.map((reg) => (
          <label key={reg.id} className="hx-check">
            <input type="checkbox" checked={inputs.includes(reg.id)}
              onChange={(e) => setInputs(e.target.checked ? [...inputs, reg.id] : inputs.filter((id) => id !== reg.id))} />
            <span><strong>{reg.label}</strong> <span className="muted mono">{reg.resolver}</span></span>
          </label>
        )) : <p className="muted">Register a source below first.</p>}
      </fieldset>
      <div className="hx-grid2">
        <label className="field"><span>Exact identifiers</span>
          <input className="text-input" value={identifiers} onChange={(e) => setIdentifiers(e.target.value)} placeholder="SECRET_KEY, retry_after" /></label>
        <label className="field"><span>Symbols to follow</span>
          <input className="text-input" value={symbols} onChange={(e) => setSymbols(e.target.value)} placeholder="charge, badges" /></label>
      </div>
      <div className="hx-grid2">
        <label className="field"><span>Deliver to</span>
          <select className="text-input" value={adapter} onChange={(e) => setAdapter(e.target.value)}>
            <option value="record">Result record</option>
            <option value="file">File (conditional write)</option>
            <option value="test_sink">Idempotent sink</option>
            <option value="blind_sink">Sink without dedup</option>
            <option value="connection">Through a connection (policy + approval)</option>
          </select></label>
        <label className="field"><span>Target</span>
          <input className="text-input" value={target} onChange={(e) => setTarget(e.target.value)}
            placeholder={adapter === "connection" ? "connection://gh/owner/repo/answers/x.md?branch=harness/x" : "reports/answer.md"} /></label>
      </div>
      <label className="hx-check"><input type="checkbox" checked={discovery} onChange={(e) => setDiscovery(e.target.checked)} />
        <span>Use the region's search index for candidate locations</span></label>
      {workload === "code_change" ? (
        <>
          <label className="field"><span>Test command</span>
            <input className="text-input mono" value={testCommand} onChange={(e) => setTestCommand(e.target.value)} /></label>
          <label className="field"><span>Candidate edits (JSON, model-free mode)</span>
            <textarea className="text-input mono" rows={3} value={proposals} onChange={(e) => setProposals(e.target.value)}
              placeholder='[[{"path": "app.py", "find": "...", "replace": "..."}]]' /></label>
        </>
      ) : null}
      <label className="field"><span>Acceptance criteria (JSON)</span>
        <textarea className="text-input mono" rows={2} value={acceptance} onChange={(e) => setAcceptance(e.target.value)}
          placeholder='[{"id": "mentions-owner", "type": "contains", "text": "manager"}]' /></label>
      {error ? <p className="error" role="alert">{error}</p> : null}
      <div className="hx-new__actions">
        <button className="btn btn--primary" type="submit" disabled={start.isPending || !question || !inputs.length}>
          {start.isPending ? "Starting…" : "Start run"}
        </button>
      </div>
    </form>
  );
}
