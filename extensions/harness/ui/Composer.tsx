import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { harnessApi } from "./api";
import type { AttachResult, BrowseResult, Registration, UsableConnection } from "./types";

/**
 * The harness as a workspace. Describe what to build, choose where it works --
 * a repository, branch and folder through a connection, a place already
 * registered, files you attach -- and where the verified result goes: a
 * Pheasant artifact, a branch (with a pull request), a Drive folder.
 */

const MODES = [
  { id: "code_change", label: "Change code", hint: "Isolated workspace, your tests, an independent check; one commit when it passes." },
  { id: "build_content", label: "Build content", hint: "A document or brief built from the sources, rendered and checked before it is kept." },
] as const;

type Input =
  | { kind: "registration"; id: string; label: string; detail: string }
  | { kind: "connection"; connection: string; provider: string; location: string; ref: string | null; path: string; label: string }
  | { kind: "attachment"; id: string; label: string; result: AttachResult };

type Deliver = "artifact" | "branch" | "drive" | "record";

const slug = (text: string) => text.toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 40).replace(/^-+|-+$/g, "") || "change";
const list = (text: string) => text.split(",").map((part) => part.trim()).filter(Boolean);
const parseJson = (text: string, fallback: unknown) => (text.trim() ? JSON.parse(text) : fallback);

function ConnectionPicker({ usable, onAdd }: { usable: UsableConnection[]; onAdd: (input: Input) => void }) {
  const [name, setName] = useState(usable[0]?.name ?? "");
  const connection = usable.find((c) => c.name === name);
  const [location, setLocation] = useState("");
  const [ref, setRef] = useState<string | null>(null);
  const [path, setPath] = useState("");
  useEffect(() => { setLocation(connection?.locations[0] ?? ""); setRef(null); setPath(""); }, [name]); // eslint-disable-line react-hooks/exhaustive-deps
  const browse = useQuery<BrowseResult, Error>({
    queryKey: ["harness-browse", name, location, ref, path],
    queryFn: () => harnessApi.browse(name, { location, path, ref: ref ?? undefined }),
    enabled: Boolean(name && location),
  });
  const data = browse.data;
  if (!usable.length) {
    return <p className="muted">No connection is usable yet. An operator allows actions for one under <strong>Connections</strong>.</p>;
  }
  const crumbs = path ? path.split("/") : [];
  const isGithub = connection?.provider === "github";
  return (
    <div className="hx-picker">
      <div className="hx-grid3">
        <label className="field"><span>Connection</span>
          <select className="text-input" value={name} onChange={(e) => setName(e.target.value)}>
            {usable.map((c) => <option key={c.name} value={c.name}>{c.name} · {c.provider}</option>)}
          </select></label>
        <label className="field"><span>{isGithub ? "Repository" : connection?.provider === "google_drive" ? "Folder id" : "Drive id"}</span>
          <input className="text-input mono" list={`hx-loc-${name}`} value={location} onChange={(e) => { setLocation(e.target.value); setPath(""); setRef(null); }}
            placeholder={isGithub ? "owner/repo" : "id"} />
          <datalist id={`hx-loc-${name}`}>{connection?.locations.map((l) => <option key={l} value={l} />)}</datalist></label>
        {isGithub ? (
          <label className="field"><span>Branch</span>
            <select className="text-input mono" value={ref ?? data?.ref ?? ""} onChange={(e) => setRef(e.target.value)} disabled={!data?.branches}>
              {(data?.branches ?? []).map((b) => <option key={b} value={b}>{b}{b === data?.default_branch ? " (default)" : ""}</option>)}
            </select></label>
        ) : <div />}
      </div>
      <nav className="hx-crumbs" aria-label="Folder">
        <button type="button" className="hx-crumb" onClick={() => setPath("")}>{location || "—"}</button>
        {crumbs.map((part, index) => (
          <span key={index}>/<button type="button" className="hx-crumb" onClick={() => setPath(crumbs.slice(0, index + 1).join("/"))}>{part}</button></span>
        ))}
      </nav>
      {browse.error ? <p className="error" role="alert">{browse.error.message}</p> : null}
      <ul className="hx-browse" aria-label="Folder contents">
        {(data?.entries ?? []).filter((e) => e.type === "dir").map((entry) => (
          <li key={entry.path}><button type="button" className="hx-browse__dir" onClick={() => setPath(isGithub || connection?.provider === "microsoft_365" ? entry.path : entry.path)}>▸ {entry.name}</button></li>
        ))}
        {(data?.entries ?? []).filter((e) => e.type === "file").slice(0, 12).map((entry) => (
          <li key={entry.path} className="muted mono hx-browse__file">{entry.name}</li>
        ))}
        {browse.isLoading ? <li className="muted">Loading…</li> : null}
      </ul>
      <button type="button" className="btn btn--small" disabled={!location || browse.isError}
        onClick={() => onAdd({
          kind: "connection", connection: name, provider: connection?.provider ?? "", location,
          ref: isGithub ? (ref ?? data?.ref ?? null) : null, path,
          label: `${location}${isGithub ? `@${ref ?? data?.ref ?? ""}` : ""}${path ? `/${path}` : ""}`,
        })}>
        Work in {location}{path ? `/${path}` : ""}{isGithub ? ` @ ${ref ?? data?.ref ?? "…"}` : ""}
      </button>
    </div>
  );
}

function AttachZone({ onAttached }: { onAttached: (result: AttachResult) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [keep, setKeep] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const attach = useMutation({
    mutationFn: (files: File[]) => harnessApi.attach(files, { keepInUploads: keep }),
    onSuccess: (result) => { setError(null); onAttached(result); },
    onError: (failure: Error) => setError(failure.message),
  });
  const take = (files: FileList | null) => { if (files?.length) attach.mutate(Array.from(files)); };
  return (
    <div className={`hx-drop${dragging ? " is-over" : ""}`}
      onDragOver={(e) => { e.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)}
      onDrop={(e) => { e.preventDefault(); setDragging(false); take(e.dataTransfer.files); }}>
      <input ref={input} type="file" multiple hidden onChange={(e) => take(e.target.files)} aria-label="Attach files" />
      <p><strong>{attach.isPending ? "Attaching…" : "Drop files here"}</strong> or{" "}
        <button type="button" className="btn btn--small btn--ghost" onClick={() => input.current?.click()}>choose files</button></p>
      <p className="muted">Any format. PDF, Word, PowerPoint, Excel, RTF and EPUB are also read as text.</p>
      <label className="hx-check"><input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} />
        <span>Also keep them in Pheasant's <strong>uploads</strong> source (indexed and searchable)</span></label>
      {error ? <p className="error" role="alert">{error}</p> : null}
    </div>
  );
}

export function Composer({ registrations, onStarted }: { registrations: Registration[]; onStarted: (runId: string) => void }) {
  const client = useQueryClient();
  const usable = useQuery({ queryKey: ["harness-usable"], queryFn: harnessApi.usable });
  const connections = usable.data?.connections ?? [];
  const [mode, setMode] = useState<string>("code_change");
  const [prompt, setPrompt] = useState("");
  const [inputs, setInputs] = useState<Input[]>([]);
  const [tab, setTab] = useState<"connection" | "registered" | "attach">("connection");
  const [deliver, setDeliver] = useState<Deliver>("artifact");
  const [title, setTitle] = useState("");
  const [branch, setBranch] = useState("");
  const [base, setBase] = useState("");
  const [openPr, setOpenPr] = useState(true);
  const [filePath, setFilePath] = useState("");
  const [driveFolder, setDriveFolder] = useState("");
  const [testCommand, setTestCommand] = useState("python -m pytest -q -p no:cacheprovider");
  const [proposals, setProposals] = useState("");
  const [acceptance, setAcceptance] = useState("");
  const [identifiers, setIdentifiers] = useState("");
  const [symbols, setSymbols] = useState("");
  const [error, setError] = useState<string | null>(null);
  const repo = inputs.find((i): i is Extract<Input, { kind: "connection" }> => i.kind === "connection" && i.provider === "github");
  const writer = repo ? connections.find((c) => c.name === repo.connection && c.actions.includes("write")) : undefined;
  const drive = connections.find((c) => c.provider === "google_drive" && c.actions.includes("write"));
  useEffect(() => { if (repo) { setBase(repo.ref ?? ""); setBranch((b) => b || `harness/${slug(prompt || "change")}`); } }, [repo?.connection, repo?.location, repo?.ref]); // eslint-disable-line react-hooks/exhaustive-deps
  const hint = useMemo(() => MODES.find((m) => m.id === mode)?.hint, [mode]);
  const readable = registrations.filter((r) => r.authorized && !r.revoked && r.resolver !== "attachment");
  const add = (input: Input) => setInputs((now) => [...now.filter((i) => i.label !== input.label), input]);

  const target = (): Record<string, unknown> => {
    if (deliver === "artifact") return { adapter: "artifact", target: title };
    if (deliver === "record") return { adapter: "record", target: "" };
    if (deliver === "drive") return { adapter: "connection", target: `connection://${drive?.name}/${driveFolder.replace(/^\/|\/$/g, "")}` };
    if (!repo) throw new Error("Choose a GitHub repository to work in first");
    const query = `?branch=${encodeURIComponent(branch)}${base ? `&base=${encodeURIComponent(base)}` : ""}${openPr ? "&pr=1" : ""}`;
    const where = mode === "code_change"
      ? `${repo.location}/${repo.path ? `${repo.path}/` : ""}`
      : `${repo.location}/${filePath.replace(/^\//, "")}`;
    return { adapter: "connection", target: `connection://${repo.connection}/${where}${query}` };
  };

  const start = useMutation({
    mutationFn: async () => {
      const ids: string[] = [];
      for (const input of inputs) {
        if (input.kind === "connection") {
          const uri = `connection://${input.connection}/${input.location}${input.path ? `/${input.path}` : ""}`;
          const reg = await harnessApi.registerWith({ uri, label: input.label, scope: input.ref ? { ref: input.ref } : {} });
          ids.push(reg.id);
        } else ids.push(input.id);
      }
      const request: Record<string, unknown> = {
        workload: mode, question: prompt, inputs: ids, discovery: "off",
        identifiers: list(identifiers), symbols: list(symbols),
        destination: target(), acceptance: parseJson(acceptance, []),
      };
      if (mode === "code_change") {
        request.test_command = testCommand.split(/\s+/).filter(Boolean);
        request.proposals = parseJson(proposals, []);
        request.output = { release_note: true, release_note_title: prompt.split("\n")[0].slice(0, 80) };
      }
      return harnessApi.start({ request, client_key: `ui-${Date.now()}-${Math.random().toString(36).slice(2, 8)}` });
    },
    onSuccess: (status) => { client.invalidateQueries({ queryKey: ["harness-runs"] }); client.invalidateQueries({ queryKey: ["harness-registrations"] }); onStarted(status.run_id); },
    onError: (failure: Error) => setError(failure.message),
  });

  return (
    <form className="hx-composer" onSubmit={(e) => { e.preventDefault(); setError(null); start.mutate(); }}>
      <div className="card hx-composer__task">
        <div className="hx-modes" role="radiogroup" aria-label="What to do">
          {MODES.map((m) => (
            <button key={m.id} type="button" role="radio" aria-checked={mode === m.id}
              className={`hx-workload${mode === m.id ? " hx-workload--active" : ""}`} onClick={() => setMode(m.id)}>{m.label}</button>
          ))}
          <span className="muted hx-hint">{hint}</span>
        </div>
        <textarea className="text-input hx-prompt" rows={4} required value={prompt} onChange={(e) => setPrompt(e.target.value)}
          placeholder={mode === "code_change" ? "Describe the change: what should be different, and how you will know it works." : "Describe what to build, for whom, and what it must contain."}
          aria-label="Task" />
        <div className="hx-chips hx-inputs-chips" aria-label="Working in">
          {inputs.map((input) => (
            <span key={input.label} className={`hx-chip hx-chip--${input.kind}`}>
              {input.kind === "attachment" ? "📎 " : input.kind === "connection" ? "⎇ " : ""}{input.label}
              <button type="button" aria-label={`Remove ${input.label}`} onClick={() => setInputs(inputs.filter((i) => i !== input))}>×</button>
            </span>
          ))}
          {!inputs.length ? <span className="muted">Nothing chosen yet: pick a repository, a registered place or attach files.</span> : null}
        </div>
      </div>

      <section className="card hx-composer__where">
        <div className="tabs" role="tablist" aria-label="Work in">
          {([["connection", "Repository or folder"], ["registered", "Registered places"], ["attach", "Attach files"]] as const).map(([id, label]) => (
            <button key={id} type="button" role="tab" aria-selected={tab === id} className={`tab${tab === id ? " active" : ""}`} onClick={() => setTab(id)}>{label}</button>
          ))}
        </div>
        {tab === "connection" ? <ConnectionPicker usable={connections} onAdd={add} /> : null}
        {tab === "registered" ? (
          <div className="hx-reglist">
            {readable.map((reg) => (
              <label key={reg.id} className="hx-check">
                <input type="checkbox" checked={inputs.some((i) => i.kind === "registration" && i.id === reg.id)}
                  onChange={(e) => e.target.checked ? add({ kind: "registration", id: reg.id, label: reg.label, detail: reg.uri }) : setInputs(inputs.filter((i) => !(i.kind === "registration" && i.id === reg.id)))} />
                <span><strong>{reg.label}</strong> <span className="muted mono">{reg.resolver}</span></span>
              </label>
            ))}
            {!readable.length ? <p className="muted">No registered places yet.</p> : null}
          </div>
        ) : null}
        {tab === "attach" ? (
          <>
            <AttachZone onAttached={(result) => add({ kind: "attachment", id: result.registration.id, label: result.registration.label, result })} />
            {inputs.filter((i): i is Extract<Input, { kind: "attachment" }> => i.kind === "attachment").map((a) => (
              <div key={a.id} className="hx-attached">
                <strong>{a.label}</strong>{" "}
                <span className="muted">{a.result.uploads.status === "skipped" ? `not kept in uploads: ${a.result.uploads.reason}` : a.result.uploads.folder ? `kept in uploads/${a.result.uploads.folder}` : ""}</span>
                <ul>{a.result.files.map((f) => (
                  <li key={f.path} className="mono">{f.path}{f.derived_from ? <span className="hx-chip">text</span> : null}{f.kind === "unreadable" ? <span className="muted"> {f.reason}</span> : null}</li>
                ))}</ul>
              </div>
            ))}
          </>
        ) : null}
      </section>

      <section className="card hx-composer__deliver">
        <h3>Where the result goes</h3>
        <div className="hx-deliver" role="radiogroup" aria-label="Deliver to">
          {([
            ["artifact", "Keep as a Pheasant artifact", "Searchable, filed by day and person, linked to its sources."],
            ["branch", "Commit to a branch", writer ? `${repo?.location}: one commit${mode === "code_change" ? " of every changed file" : ""}, optionally a pull request.` : "Work in a GitHub repository whose connection allows writes."],
            ["drive", "Save to a Drive folder", drive ? `Through ${drive.name}.` : "No Drive connection allows writes."],
            ["record", "Result record only", "Kept with the run; nothing is written anywhere else."],
          ] as const).map(([id, label, note]) => {
            const disabled = (id === "branch" && !writer) || (id === "drive" && !drive);
            return (
              <label key={id} className={`hx-deliver__option${deliver === id ? " is-active" : ""}${disabled ? " is-disabled" : ""}`}>
                <input type="radio" name="deliver" checked={deliver === id} disabled={disabled} onChange={() => setDeliver(id)} />
                <span><strong>{label}</strong><span className="muted">{note}</span></span>
              </label>
            );
          })}
        </div>
        {deliver === "artifact" ? (
          <label className="field"><span>Title</span>
            <input className="text-input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Defaults to the result's first heading" /></label>
        ) : null}
        {deliver === "branch" ? (
          <div className="hx-grid3">
            <label className="field"><span>New or existing branch</span>
              <input className="text-input mono" value={branch} onChange={(e) => setBranch(e.target.value)} /></label>
            <label className="field"><span>Created from</span>
              <input className="text-input mono" value={base} onChange={(e) => setBase(e.target.value)} placeholder="the default branch" /></label>
            {mode === "build_content" ? (
              <label className="field"><span>File in the repository</span>
                <input className="text-input mono" value={filePath} onChange={(e) => setFilePath(e.target.value)} placeholder="docs/answers/brief.md" /></label>
            ) : (
              <label className="hx-check hx-check--inline"><input type="checkbox" checked={openPr} onChange={(e) => setOpenPr(e.target.checked)} />
                <span>Open a pull request into <span className="mono">{base || "the default branch"}</span></span></label>
            )}
            <p className="muted hx-fine">{writer?.approval.write === "required" ? "This connection's policy asks an operator to approve the write before it is sent." : "Writes through this connection are sent once every required check passes."}</p>
          </div>
        ) : null}
        {deliver === "drive" ? (
          <label className="field"><span>Folder id / file name</span>
            <input className="text-input mono" value={driveFolder} onChange={(e) => setDriveFolder(e.target.value)} placeholder="1AbC…/brief.md" /></label>
        ) : null}
      </section>

      <details className="card hx-composer__advanced">
        <summary>Checks and hints</summary>
        <div className="hx-grid2">
          <label className="field"><span>Exact identifiers to find</span>
            <input className="text-input" value={identifiers} onChange={(e) => setIdentifiers(e.target.value)} placeholder="discount_rate, Exception" /></label>
          <label className="field"><span>Symbols to follow</span>
            <input className="text-input" value={symbols} onChange={(e) => setSymbols(e.target.value)} placeholder="badges" /></label>
        </div>
        {mode === "code_change" ? (
          <>
            <label className="field"><span>Test command</span>
              <input className="text-input mono" value={testCommand} onChange={(e) => setTestCommand(e.target.value)} /></label>
            <label className="field"><span>Candidate edits (JSON, without a model)</span>
              <textarea className="text-input mono" rows={3} value={proposals} onChange={(e) => setProposals(e.target.value)}
                placeholder='[[{"path": "app.py", "find": "...", "replace": "..."}]]' /></label>
          </>
        ) : null}
        <label className="field"><span>Acceptance criteria (JSON)</span>
          <textarea className="text-input mono" rows={2} value={acceptance} onChange={(e) => setAcceptance(e.target.value)}
            placeholder='[{"id": "mentions-owner", "type": "contains", "text": "manager"}]' /></label>
      </details>

      {error ? <p className="error" role="alert">{error}</p> : null}
      <div className="hx-new__actions">
        <button className="btn btn--primary" type="submit" disabled={start.isPending || !prompt || !inputs.length}>
          {start.isPending ? "Starting…" : mode === "code_change" ? "Start the change" : "Start building"}
        </button>
      </div>
    </form>
  );
}
