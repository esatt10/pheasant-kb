import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { CandidateReview } from "./CandidateReview";

export function ArtifactRecords() {
  const client = useQueryClient();
  const [text, setText] = useState("");
  const [scope, setScope] = useState("user");
  const [history, setHistory] = useState(false);
  const [supersedes, setSupersedes] = useState<string | null>(null);
  const records = useQuery({ queryKey: ["artifact-records", history], queryFn: () => api.artifactRecords({ current_only: !history }) });
  const save = useMutation({
    mutationFn: () => api.recordArtifact({ text, scope, supersedes }),
    onSuccess: () => {
      setText("");
      setSupersedes(null);
      client.invalidateQueries({ queryKey: ["artifact-records"] });
      client.invalidateQueries({ queryKey: ["artifacts"] });
      client.invalidateQueries({ queryKey: ["browse"] });
    },
  });
  return <section className="card artifact-records">
    <h2>Knowledge records</h2>
    <p className="muted small">Corrections create a new artifact and retain the evidence and earlier version.</p>
    <label className="field"><span>Artifact content</span>
      <textarea className="text-input artifact-records__editor" value={text} onChange={event => setText(event.target.value)} aria-label="Artifact content" />
    </label>
    <div className="artifact-records__actions">
    <label className="field">Scope <select className="text-input" value={scope} onChange={event => setScope(event.target.value)}>
      <option value="user">Personal</option><option value="session">Session</option><option value="org">Shared</option>
    </select></label>
    <button data-permission="artifacts.manage" className="btn" disabled={!text.trim() || save.isPending} onClick={() => save.mutate()}>
      {save.isPending ? "Saving…" : supersedes ? "Save correction" : "Save artifact"}
    </button>
    {supersedes ? <button data-permission="artifacts.manage" className="btn" onClick={() => { setSupersedes(null); setText(""); }}>Cancel correction</button> : null}
    </div>
    {save.isError ? <p className="error">{(save.error as Error).message}</p> : null}
    {records.isError ? <p className="error">{(records.error as Error).message}</p> : null}
    <p className="muted small">{records.data?.records.length ?? 0} current knowledge records</p>
    <label><input type="checkbox" checked={history} onChange={event => setHistory(event.target.checked)} /> Include history</label>
    <details><summary>Review knowledge records</summary>
      {records.data?.records.map(record => <article key={record.record_id} className="memory-card">
        <p>{record.text}</p><span className="badge">{record.scope}</span>
        <button data-permission="artifacts.manage" className="btn btn--small" onClick={() => { setText(record.text); setScope(record.scope); setSupersedes(record.record_id); }}>Correct</button>
      </article>)}
    </details>
    <CandidateReview />
  </section>;
}
