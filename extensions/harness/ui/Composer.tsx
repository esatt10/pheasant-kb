import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@pheasant/ui/api/client";
import { AttachButton } from "@pheasant/ui/chat/AttachButton";
import { MicButton, useDictation } from "@pheasant/ui/chat/MicButton";
import { dictationUnsupported, spliceAtCaret } from "@pheasant/ui/chat/dictation";
import { harnessApi } from "./api";
import { PLACE_KINDS } from "./places";
import type { AttachedFile, AttachResult, BrowseResult, Registration, UsableConnection } from "./types";

/**
 * The harness as a workspace. Describe what to build, choose where it works --
 * a repository, branch and folder through a connection, a place already
 * registered, files you attach -- and where the verified result goes: a
 * Pheasant artifact, a branch (with a pull request), a Drive folder.
 */

const MODES = [
  { id: "build_content", label: "Build content", hint: "A document or brief built from the sources, rendered and checked before it is kept." },
  { id: "code_change", label: "Change code", hint: "Isolated workspace, your tests, an independent check; one commit when it passes." },
] as const;

/** What read an attached file, in words. */
const READ_BY: Record<string, string> = {
  extractor: "text extracted",
  html: "page text extracted",
  caption: "image described",
  transcript: "audio transcribed",
  notebook: "notebook cells read",
  text: "read as text",
  archive: "archive expanded",
};

type Check = { kind: "mention" | "never"; text: string };

const escapeRegex = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function checksToCriteria(checks: Check[]): Record<string, unknown>[] {
  return checks.filter((c) => c.text.trim()).map((c, index) => c.kind === "mention"
    ? { id: `must-mention-${index + 1}`, type: "contains", text: c.text.trim() }
    : { id: `must-not-say-${index + 1}`, type: "absent", pattern: escapeRegex(c.text.trim()) });
}

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
      <p className="muted">Anything Pheasant's uploads take: documents (PDF, Word, PowerPoint, Excel, RTF, EPUB), web pages, notebooks, images (described), audio (transcribed), code and text, and ZIP or tar archives (expanded). Each file is read the way the index reads it.</p>
      <label className="hx-check"><input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} />
        <span>Also keep them in Pheasant's <strong>uploads</strong> source (indexed and searchable)</span></label>
      {error ? <p className="error" role="alert">{error}</p> : null}
    </div>
  );
}

function AttachedFiles({ result }: { result: AttachResult }) {
  const originals = result.files.filter((f) => !f.derived_from);
  const twins = new Map(result.files.filter((f) => f.derived_from).map((f) => [f.derived_from!, f]));
  const describe = (file: AttachedFile) => {
    const twin = twins.get(file.path);
    if (file.text_from === "archive") {
      const summary = result.archives?.[file.path];
      return summary?.error ? `archive not readable: ${summary.error}` : `archive: ${summary?.members ?? 0} files expanded into ${summary?.folder}/`;
    }
    if (twin?.kind === "unreadable") return twin.reason ?? "no text could be read";
    if (file.text_from) return READ_BY[file.text_from] ?? file.text_from;
    return file.indexable === false ? "kept as is (not a type the index reads)" : "kept as is";
  };
  return (
    <div className="hx-attached">
      <div className="hx-attached__head">
        <strong>📎 {result.registration.label}</strong>
        <span className="muted small">
          {result.uploads.status === "skipped" ? `not kept in uploads: ${result.uploads.reason}`
            : result.uploads.folder ? `kept in uploads/${result.uploads.folder}${result.uploads.status === "syncing" ? " · indexing" : ""}` : ""}
        </span>
      </div>
      <div className="table-scroll hx-scroll hx-scroll--short">
        <table className="data-table hx-attached__table">
          <thead><tr><th>File</th><th>Type</th><th>How it is read</th><th>In Pheasant</th></tr></thead>
          <tbody>
            {originals.map((file) => (
              <tr key={file.path}>
                <td className="mono small">{file.path}{file.archive ? <span className="muted"> (from {file.archive})</span> : null}</td>
                <td className="small">{file.artifact_type ?? "file"}<span className="muted"> · {file.mime_type}</span></td>
                <td className="small">{describe(file)}</td>
                <td className="small mono">{file.pheasant_document ? "indexed as uploads document" : file.archive ? "inside the archive" : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {Object.values(result.archives ?? {}).some((a) => a.skipped && Object.keys(a.skipped).length) ? (
        <p className="muted small">Left out of archives (types the index does not read): {Object.values(result.archives ?? {}).flatMap((a) => Object.entries(a.skipped ?? {})).map(([ext, n]) => `${n} × ${ext}`).join(", ")}</p>
      ) : null}
    </div>
  );
}

export function Composer({ registrations, onStarted, initialPrompt = "" }: { registrations: Registration[]; onStarted: (runId: string) => void; initialPrompt?: string }) {
  const client = useQueryClient();
  const usable = useQuery({ queryKey: ["harness-usable"], queryFn: harnessApi.usable });
  const connections = usable.data?.connections ?? [];
  const [mode, setMode] = useState<string>(/^(change|fix|refactor|patch|implement)\b/i.test(initialPrompt) ? "code_change" : "build_content");
  const [prompt, setPrompt] = useState(initialPrompt);
  const [checks, setChecks] = useState<Check[]>([{ kind: "mention", text: "" }]);
  const [attachError, setAttachError] = useState<string | null>(null);
  const promptRef = useRef<HTMLTextAreaElement>(null);
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

  // The task box's "+": the same attachment path as the Attach files tab.
  const quickAttach = useMutation({
    mutationFn: (files: File[]) => harnessApi.attach(files, { keepInUploads: true }),
    onSuccess: (result) => { setAttachError(null); add({ kind: "attachment", id: result.registration.id, label: result.registration.label, result }); },
    onError: (failure: Error) => setAttachError(failure.message),
  });

  // Dictation into the task box: the chat's microphone, its session and its cap.
  const voice = useQuery({ queryKey: ["assistant-voice", "harness"], queryFn: () => api.voiceStatus(null), staleTime: 60_000 });
  const promptValue = useRef(prompt);
  promptValue.current = prompt;
  const caret = useRef<number | null>(null);
  const dictation = useDictation({
    sessionId: null,
    maxSeconds: voice.data?.max_seconds ?? 60,
    onText: (text, separate) => {
      const el = promptRef.current;
      const value = promptValue.current;
      const start = caret.current ?? el?.selectionStart ?? value.length;
      const end = caret.current ?? el?.selectionEnd ?? value.length;
      const next = spliceAtCaret(value, start, end, text, separate);
      promptValue.current = next.value;
      caret.current = next.caret;
      setPrompt(next.value);
    },
    onLive: () => promptRef.current?.focus(),
  });
  useLayoutEffect(() => {
    if (caret.current === null || !promptRef.current) return;
    promptRef.current.setSelectionRange(caret.current, caret.current);
    caret.current = null;
  }, [prompt]);
  const micUnavailable = voice.data ? (voice.data.available ? dictationUnsupported() : voice.data.reason || "not configured on the server") : "checking…";

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
        destination: target(), acceptance: [...checksToCriteria(checks), ...(parseJson(acceptance, []) as Record<string, unknown>[])],
      };
      if (mode === "code_change") {
        request.test_command = testCommand.split(/\s+/).filter(Boolean);
        request.proposals = parseJson(proposals, []);
        request.output = { release_note: true, release_note_title: prompt.split("\n")[0].slice(0, 80) };
      }
      return harnessApi.start({ request, client_key: `ui-${Date.now()}-${Math.random().toString(36).slice(2, 8)}` });
    },
    onMutate: () => dictation.cancel(),
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
        <div className="hx-promptbox">
          <textarea ref={promptRef} className="text-input hx-prompt" rows={4} required value={prompt} onChange={(e) => setPrompt(e.target.value)}
            placeholder={mode === "code_change" ? "Describe the change: what should be different, and how you will know it works." : "Describe what to build, for whom, and what it must contain."}
            aria-label="Task" />
          <div className="hx-promptbox__tools">
            <AttachButton onFiles={(files) => quickAttach.mutate(files)} label="Attach files to this run" busy={quickAttach.isPending} />
            {voice.data?.available || dictation.state !== "off" ? (
              <MicButton control={dictation} unavailable={dictation.state === "off" ? micUnavailable : null} />
            ) : <span className="muted small" title={micUnavailable ?? undefined}>🎙 {voice.data ? "dictation is off on this server" : ""}</span>}
            {dictation.state === "live" ? <span className="muted small" role="status">Listening · {dictation.remaining}s left</span> : null}
            {dictation.notice ? <span className="muted small" role="status">{dictation.notice}</span> : null}
            {quickAttach.isPending ? <span className="muted small" role="status">Attaching…</span> : null}
            {attachError ? <span className="error small" role="alert">{attachError}</span> : null}
          </div>
        </div>
        <div className="hx-chips hx-inputs-chips" aria-label="Working in">
          {inputs.map((input) => (
            <span key={input.label} className={`hx-chip hx-chip--${input.kind}`}>
              {input.kind === "attachment" ? "📎 " : input.kind === "connection" ? "⎇ " : ""}{input.label}
              <button type="button" aria-label={`Remove ${input.label}`} onClick={() => setInputs(inputs.filter((i) => i !== input))}>×</button>
            </span>
          ))}
          {!inputs.length ? <span className="muted">Nothing chosen yet: pick a repository, a place below, or attach files with +.</span> : null}
        </div>
      </div>

      <section className="card hx-composer__where">
        <div className="tabs" role="tablist" aria-label="Work in">
          {([["connection", "Repository or folder"], ["registered", "Places"], ["attach", "Attach files"]] as const).map(([id, label]) => (
            <button key={id} type="button" role="tab" aria-selected={tab === id} className={`tab${tab === id ? " active" : ""}`} onClick={() => setTab(id)}>{label}</button>
          ))}
        </div>
        {tab === "connection" ? <ConnectionPicker usable={connections} onAdd={add} /> : null}
        {tab === "registered" ? (
          <div className="hx-pickplaces hx-scroll hx-scroll--short">
            {readable.map((reg) => (
              <label key={reg.id} className="hx-check hx-pickplace">
                <input type="checkbox" checked={inputs.some((i) => i.kind === "registration" && i.id === reg.id)}
                  onChange={(e) => e.target.checked ? add({ kind: "registration", id: reg.id, label: reg.label, detail: reg.uri }) : setInputs(inputs.filter((i) => !(i.kind === "registration" && i.id === reg.id)))} />
                <span><strong>{reg.label}</strong> <span className="pill">{PLACE_KINDS[reg.resolver]?.label ?? reg.resolver}</span>
                  <span className="muted mono small hx-pickplace__uri">{reg.uri}</span></span>
              </label>
            ))}
            {!readable.length ? <p className="muted">No places yet. Add one under <strong>Places</strong>, or pick a repository.</p> : null}
          </div>
        ) : null}
        {tab === "attach" ? (
          <>
            <AttachZone onAttached={(result) => add({ kind: "attachment", id: result.registration.id, label: result.registration.label, result })} />
            {inputs.filter((i): i is Extract<Input, { kind: "attachment" }> => i.kind === "attachment").map((a) => (
              <AttachedFiles key={a.id} result={a.result} />
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
        <summary>
          <strong>Help it get this right</strong>
          <span className="muted"> — optional: what it must find, and how to tell when it is done</span>
        </summary>
        <fieldset className="hx-fieldset">
          <legend>When is the result good enough?</legend>
          <p className="muted small">Each line becomes a check the result must pass before it is kept. A failing check is shown on the run, and the run tries again or stops.</p>
          {checks.map((check, index) => (
            <div key={index} className="hx-checkrow">
              <select className="text-input text-input--small" value={check.kind} aria-label="Check"
                onChange={(e) => setChecks(checks.map((c, k) => (k === index ? { ...c, kind: e.target.value as Check["kind"] } : c)))}>
                <option value="mention">It must mention</option>
                <option value="never">It must never say</option>
              </select>
              <input className="text-input" value={check.text} aria-label="Words"
                placeholder={check.kind === "mention" ? "the on-call manager" : "TODO"}
                onChange={(e) => setChecks(checks.map((c, k) => (k === index ? { ...c, text: e.target.value } : c)))} />
              <button type="button" className="btn btn--small btn--ghost" aria-label="Remove this check"
                onClick={() => setChecks(checks.length > 1 ? checks.filter((_, k) => k !== index) : [{ kind: "mention", text: "" }])}>×</button>
            </div>
          ))}
          <button type="button" className="btn btn--small" onClick={() => setChecks([...checks, { kind: "mention", text: "" }])}>Add a check</button>
          <p className="muted small">Every result is also checked for citations that resolve to the sources and claims those sources support.</p>
        </fieldset>
        <fieldset className="hx-fieldset hx-grid2">
          <legend>What it should look for</legend>
          <label className="field"><span>Exact words or names to find</span>
            <input className="text-input" value={identifiers} onChange={(e) => setIdentifiers(e.target.value)} placeholder="discount_rate, PaymentDeclined" />
            <small className="muted">Comma-separated. Each is searched for exactly, and where it appears is cited.</small></label>
          <label className="field"><span>Functions or classes to follow</span>
            <input className="text-input" value={symbols} onChange={(e) => setSymbols(e.target.value)} placeholder="apply_discount" />
            <small className="muted">Their definitions and every caller are read before anything is written.</small></label>
        </fieldset>
        {mode === "code_change" ? (
          <fieldset className="hx-fieldset">
            <legend>How it proves the change works</legend>
            <label className="field"><span>Command that runs the tests</span>
              <input className="text-input mono" value={testCommand} onChange={(e) => setTestCommand(e.target.value)} />
              <small className="muted">Run in an isolated copy before and after the change. The change is kept only if it passes.</small></label>
            <details className="hx-advanced-json">
              <summary className="small">Exact edits to try first (for regions without a model)</summary>
              <textarea className="text-input mono" rows={3} value={proposals} onChange={(e) => setProposals(e.target.value)}
                placeholder='[[{"path": "app.py", "find": "rate = 0.1", "replace": "rate = 0.15"}]]' aria-label="Edits to try" />
              <small className="muted">Find-and-replace sets, tried in order; the first that passes the tests is kept.</small>
            </details>
          </fieldset>
        ) : null}
        <details className="hx-advanced-json">
          <summary className="small">Write checks as JSON instead (every check type)</summary>
          <textarea className="text-input mono" rows={2} value={acceptance} onChange={(e) => setAcceptance(e.target.value)}
            placeholder='[{"id": "states-owner", "type": "contains", "text": "manager"}]' aria-label="Checks as JSON" />
        </details>
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
