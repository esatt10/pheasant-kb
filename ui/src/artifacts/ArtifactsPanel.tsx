import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "../api/client";
import type { ArtifactSummary } from "../api/types";
import { useSession } from "../state/session";

/** The source every kept artifact lives in (`services/artifacts.py`). */
export const ARTIFACTS_SOURCE = "artifacts";

/**
 * The Artifacts tab: what the region keeps, and a viewer.
 *
 * Keeping is a choice made after an answer (Save on the answer, Save thread
 * in the composer) unless auto-save is on -- a switch at the top of this
 * list, held by the region for everyone (`services/artifact_settings.py`).
 * Nothing kept depends on the chat that produced it: a reload empties the
 * conversation (the region keeps no chat state) and leaves every artifact
 * here, the open one included.
 *
 * Every document shown is rendered by the region in pheasant's style
 * (`services/artifact_render.py`, the same renderer MCP and the downloads
 * use) and goes into a frame sandboxed to scripts alone, with an opaque
 * origin: an HTML report runs, and can reach neither this page, its token,
 * nor the network (the document's own policy forbids every fetch).
 */
export function ArtifactsPanel({
  selectedId,
  onSelect,
}: {
  selectedId: string | null;
  onSelect: (id: string | null) => void;
}) {
  if (selectedId) return <ArtifactViewer id={selectedId} onBack={() => onSelect(null)} />;
  return <ArtifactList onSelect={onSelect} />;
}

/**
 * The region's artifact settings: auto-save, the indexing schedule and
 * retention. A change here is a change for every replica and every reader.
 */
function ArtifactSettingsBar() {
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ["artifact-settings"], queryFn: api.artifactSettings, staleTime: 30_000 });
  const [open, setOpen] = useState(false);
  const change = useMutation({
    mutationFn: api.configureArtifacts,
    onSuccess: (data) => queryClient.setQueryData(["artifact-settings"], data),
  });
  const current = settings.data?.settings;
  if (!current) return null;
  const number = (key: "index_interval_seconds" | "retention_days", value: string) => {
    const parsed = Number.parseInt(value, 10);
    if (Number.isFinite(parsed) && parsed >= 0 && parsed !== current[key]) change.mutate({ [key]: parsed });
  };
  return (
    <div className="artifacts__settings">
      <label className="artifacts__switch" title="Keep every composed answer the moment it is written">
        <input
          type="checkbox"
          checked={current.auto_save}
          disabled={change.isPending}
          onChange={(event) => change.mutate({ auto_save: event.target.checked })}
        />
        Auto-save composed answers
      </label>
      <button type="button" className="btn btn--small" onClick={() => setOpen(!open)} aria-expanded={open}>
        {open ? "Hide settings" : "Settings"}
      </button>
      {open ? (
        <div className="artifacts__settings-detail small">
          <label>
            Index every{" "}
            <input
              type="number"
              min={0}
              defaultValue={Math.round(current.index_interval_seconds / 60)}
              onBlur={(event) => number("index_interval_seconds", String(Number(event.target.value) * 60))}
            />{" "}
            min
          </label>
          <label title="Hold artifact indexing while content sources sync">
            <input
              type="checkbox"
              checked={current.yield_to_content}
              onChange={(event) => change.mutate({ yield_to_content: event.target.checked })}
            />{" "}
            Give way to content syncs
          </label>
          <label>
            Archive after{" "}
            <input
              type="number"
              min={0}
              defaultValue={current.retention_days}
              onBlur={(event) => number("retention_days", event.target.value)}
            />{" "}
            days (0 keeps)
          </label>
          <label>
            <input
              type="checkbox"
              checked={current.enabled}
              onChange={(event) => change.mutate({ enabled: event.target.checked })}
            />{" "}
            Saving enabled
          </label>
          {settings.data?.updated_by ? (
            <span className="muted">Last changed by {settings.data.updated_by}</span>
          ) : null}
          {change.isError ? <span className="error">{(change.error as Error).message}</span> : null}
        </div>
      ) : null}
    </div>
  );
}

/**
 * The list as the directory it is: by day, then by who kept each artifact --
 * the same folders the `artifacts` source holds them in, which the taxonomy
 * view browses. Select several to delete or archive them together.
 */
function ArtifactList({ onSelect }: { onSelect: (id: string) => void }) {
  const [archived, setArchived] = useState(false);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const queryClient = useQueryClient();
  const list = useQuery({
    queryKey: ["artifacts", archived],
    queryFn: () => api.artifacts({ archived }),
    staleTime: 10_000,
  });
  const { dispatch } = useSession();
  const bulk = useMutation({
    mutationFn: async (action: "delete" | "archive" | "restore") => api.artifactsBulk(action, [...chosen]),
    onSuccess: () => {
      setChosen(new Set());
      void queryClient.invalidateQueries({ queryKey: ["artifacts"] });
    },
  });
  const toggle = (id: string) =>
    setChosen((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const items = list.data?.artifacts ?? [];
  const groups = new Map<string, ArtifactSummary[]>();
  for (const item of items) {
    const [year, month, day, who] = (item.folder ?? "").split("/");
    const key = item.folder ? `${year}/${month}/${day}/${who}` : "unfiled";
    groups.set(key, [...(groups.get(key) ?? []), item]);
  }
  return (
    <div className="artifacts__dir">
      <ArtifactSettingsBar />
      <div className="artifacts__dir-head">
        <span className="artifacts__views">
          <button type="button" className={`chip${archived ? "" : " chip--active"}`} onClick={() => setArchived(false)}>
            Live
          </button>
          <button type="button" className={`chip${archived ? " chip--active" : ""}`} onClick={() => setArchived(true)}>
            Archived
          </button>
        </span>
        <button
          type="button"
          className="btn btn--small"
          onClick={() => dispatch({ type: "open-browser", source: ARTIFACTS_SOURCE, path: "" })}
          title="Browse every artifact's files in the taxonomy view"
        >
          Browse all
        </button>
      </div>
      {chosen.size ? (
        <div className="artifacts__bulk">
          <span className="small">{chosen.size} selected</span>
          {archived ? (
            <button type="button" className="btn btn--small" onClick={() => bulk.mutate("restore")}>
              Restore
            </button>
          ) : (
            <button type="button" className="btn btn--small" onClick={() => bulk.mutate("archive")}>
              Archive
            </button>
          )}
          <button
            type="button"
            className="btn btn--small btn--danger"
            onClick={() => {
              if (window.confirm(`Delete ${chosen.size} artifact(s)? This cannot be undone.`)) bulk.mutate("delete");
            }}
          >
            Delete
          </button>
          <button type="button" className="btn btn--small" onClick={() => setChosen(new Set())}>
            Clear
          </button>
          {bulk.isError ? <span className="error small">{(bulk.error as Error).message}</span> : null}
          {bulk.data && "note" in bulk.data ? <span className="muted small">{String(bulk.data.note)}</span> : null}
        </div>
      ) : null}
      {list.isLoading ? <div className="artifacts__empty"><span className="spinner" /></div> : null}
      {list.isError ? <p className="error small artifacts__empty">{(list.error as Error).message}</p> : null}
      {!list.isLoading && items.length === 0 ? (
        <div className="artifacts__empty muted small">
          {archived ? (
            <p>Nothing archived.</p>
          ) : (
            <>
              <p>No artifacts yet.</p>
              <p>
                Save an answer with <strong>Save</strong>, a whole conversation with <strong>Save thread</strong>, or
                turn on auto-save above to keep every <code>@compose</code> answer.
              </p>
            </>
          )}
        </div>
      ) : null}
      {[...groups.entries()].map(([key, members]) => {
        const [year, month, day, who] = key.split("/");
        return (
          <section key={key} className="artifacts__group">
            <button
              type="button"
              className="artifacts__group-head"
              onClick={() => dispatch({ type: "open-browser", source: ARTIFACTS_SOURCE, path: key === "unfiled" ? "" : key })}
              title="Open this folder in the taxonomy view"
            >
              <span>{day ? `${year}-${month}-${day}` : "Unfiled"}</span>
              {who ? <span className="muted small">{who === "shared" ? "Shared" : who}</span> : null}
            </button>
            <ul className="artifacts__list">
              {members.map((item) => (
                <li key={item.id} className="artifacts__row">
                  <input
                    type="checkbox"
                    aria-label={`Select ${item.title}`}
                    checked={chosen.has(item.id)}
                    onChange={() => toggle(item.id)}
                  />
                  <button type="button" className="artifacts__item" onClick={() => onSelect(item.id)}>
                    <span className="artifacts__item-title">{item.title}</span>
                    <span className="muted small">
                      {describe(item)}
                      {item.index_status === "pending" ? (
                        <span className="artifacts__badge" title="Searchable once the scheduled artifacts sync runs">
                          {" "}· not indexed yet
                        </span>
                      ) : null}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

/** "Composed files · 2 files · ui · Nov 9, 01:12". */
export function describe(item: ArtifactSummary): string {
  const kind = item.kind === "files" ? "Composed files" : item.kind === "thread" ? "Conversation" : "Report";
  const files = item.files.length ? ` · ${item.files.length} file${item.files.length === 1 ? "" : "s"}` : "";
  const channel = item.channel ? ` · ${item.channel}` : "";
  const when = new Date(item.created_at);
  const date = Number.isNaN(when.getTime())
    ? ""
    : ` · ${when.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}`;
  return `${kind}${files}${channel}${date}`;
}

/** The theme the page is drawn in, which a rendered document follows. */
function useTheme(): "light" | "dark" {
  const read = (): "light" | "dark" => {
    const set = document.documentElement.dataset.theme;
    if (set === "dark" || set === "light") return set;
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  };
  const [theme, setTheme] = useState(read);
  useEffect(() => {
    const observer = new MutationObserver(() => setTheme(read()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);
  return theme;
}

function ArtifactViewer({ id, onBack }: { id: string; onBack: () => void }) {
  const queryClient = useQueryClient();
  const { dispatch } = useSession();
  const theme = useTheme();
  const [file, setFile] = useState<string | null>(null);
  const [mode, setMode] = useState<"preview" | "code">("preview");
  const [copied, setCopied] = useState(false);
  const artifact = useQuery({ queryKey: ["artifact", id], queryFn: () => api.artifact(id), retry: false });
  const current = file ? artifact.data?.files.find((f) => f.name === file) : undefined;
  const shownMode = current?.previewable ? mode : "code";
  const document_ = useQuery({
    queryKey: ["artifact-document", id, file, shownMode, theme],
    queryFn: () => api.artifactDocument(id, { file, mode: shownMode, theme }),
    enabled: Boolean(artifact.data),
    staleTime: 60_000,
  });
  const download = useMutation({
    mutationFn: (options: { format?: "zip" | "html" | "markdown"; file?: string | null }) =>
      api.downloadArtifact(id, options),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteArtifact(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["artifacts"] });
      // Its files left the index too: the taxonomy and the overview are stale.
      queryClient.invalidateQueries({ queryKey: ["browse"] });
      queryClient.invalidateQueries({ queryKey: ["overview"] });
      onBack();
    },
  });
  useEffect(() => {
    setFile(null);
    setCopied(false);
  }, [id]);

  if (artifact.isError) {
    const gone = artifact.error instanceof ApiError && artifact.error.status === 404;
    return (
      <div className="artifacts__empty">
        <p className="muted small">
          {gone ? "This artifact is no longer kept: it was deleted or removed from the index." : (artifact.error as Error).message}
        </p>
        <button type="button" className="btn btn--small" onClick={onBack}>
          All artifacts
        </button>
      </div>
    );
  }
  const data = artifact.data;
  if (!data) return <div className="artifacts__empty"><span className="spinner" /></div>;

  const copy = async () => {
    const content = data.payload.files.find((f) => f.name === file)?.content ?? "";
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };
  const error = (download.error ?? remove.error) as Error | null;

  return (
    <div className="artifact">
      <div className="artifact__head">
        <button type="button" className="btn btn--small btn--ghost" onClick={onBack} title="All artifacts">
          ←
        </button>
        <div className="artifact__title">
          <strong>{data.title}</strong>
          <span className="muted small">
            {describe(data)}
            {data.owner ? ` · ${data.owner}` : ""}
          </span>
        </div>
        {data.folder ? (
          <button
            type="button"
            className="btn btn--small"
            onClick={() => dispatch({ type: "open-browser", source: data.source ?? ARTIFACTS_SOURCE, path: data.folder ?? "" })}
            title={`Its files in the taxonomy view: ${data.source}/${data.folder}`}
          >
            Folder
          </button>
        ) : null}
      </div>
      <div className="artifact__tabs" role="tablist" aria-label="Files">
        <button
          type="button"
          role="tab"
          aria-selected={file === null}
          className={`chip${file === null ? " chip--active" : ""}`}
          onClick={() => setFile(null)}
        >
          {data.kind === "report" ? "Report" : "Everything"}
        </button>
        {data.files.map((f) => (
          <button
            type="button"
            role="tab"
            key={f.name}
            aria-selected={file === f.name}
            className={`chip chip--mono${file === f.name ? " chip--active" : ""}`}
            onClick={() => setFile(f.name)}
          >
            {f.name}
          </button>
        ))}
      </div>
      <div className="artifact__actions">
        {current?.previewable ? (
          <div className="segmented" role="group" aria-label="Show as">
            <button type="button" className={mode === "preview" ? "active" : ""} onClick={() => setMode("preview")}>
              Preview
            </button>
            <button type="button" className={mode === "code" ? "active" : ""} onClick={() => setMode("code")}>
              Code
            </button>
          </div>
        ) : null}
        {current ? (
          <>
            <button type="button" className="btn btn--small" onClick={copy}>
              {copied ? "Copied" : "Copy"}
            </button>
            <button type="button" className="btn btn--small" onClick={() => download.mutate({ file: current.name })}>
              Download
            </button>
          </>
        ) : null}
        <span className="artifact__spacer" />
        {data.files.length ? (
          <button type="button" className="btn btn--small" onClick={() => download.mutate({ format: "zip" })} title="Every file, a README and the HTML report">
            Download .zip
          </button>
        ) : null}
        <button type="button" className="btn btn--small" onClick={() => download.mutate({ format: "html" })} title="One self-contained page, in pheasant's style">
          Export HTML
        </button>
        <button type="button" className="btn btn--small" onClick={() => download.mutate({ format: "markdown" })}>
          Markdown
        </button>
        <button
          type="button"
          className="btn btn--small btn--ghost"
          onClick={() => {
            if (window.confirm(`Delete "${data.title}" and remove its files from the index? It cannot be undone.`))
              remove.mutate();
          }}
          title="Delete this artifact"
        >
          Delete
        </button>
      </div>
      {error ? <p className="error small">{error.message}</p> : null}
      <div className="artifact__frame-wrap">
        {document_.data !== undefined ? (
          <iframe
            className="artifact__frame"
            title={current ? current.name : data.title}
            sandbox="allow-scripts"
            srcDoc={document_.data}
          />
        ) : document_.isError ? (
          <p className="error small">{(document_.error as Error).message}</p>
        ) : (
          <span className="spinner" />
        )}
      </div>
    </div>
  );
}

/**
 * Asks the Notebook to show the Artifacts section. On a phone the chat and the
 * artifacts are separate sections, so opening one from the chat must move there.
 */
export const SHOW_ARTIFACTS_EVENT = "pheasant:show-artifacts";

export function showArtifacts(): void {
  window.dispatchEvent(new Event(SHOW_ARTIFACTS_EVENT));
}

/** A kept artifact in the chat, the way Claude shows one: open it, or take it. */
export function ArtifactCard({ artifact, onOpen }: { artifact: ArtifactSummary; onOpen: () => void }) {
  const download = useMutation({ mutationFn: () => api.downloadArtifact(artifact.id, { format: "zip" }) });
  return (
    <div className="artifact-card">
      <button
        type="button"
        className="artifact-card__open"
        onClick={() => {
          onOpen();
          showArtifacts();
        }}
        title="Open in the Artifacts tab"
      >
        <span className="artifact-card__icon" aria-hidden>
          {artifact.kind === "files" ? "{ }" : "≡"}
        </span>
        <span className="artifact-card__text">
          <strong>{artifact.title}</strong>
          <span className="muted small">
            {artifact.files.length ? artifact.files.map((f) => f.name).join(" · ") : describe(artifact)}
          </span>
        </span>
      </button>
      {artifact.files.length ? (
        <button type="button" className="btn btn--small" onClick={() => download.mutate()} disabled={download.isPending}>
          {download.isPending ? "…" : "Download .zip"}
        </button>
      ) : null}
    </div>
  );
}
