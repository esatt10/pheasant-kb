import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "../api/client";
import type { ArtifactSummary } from "../api/types";

/**
 * The Artifacts tab: what the region keeps for this reader, and a viewer.
 *
 * A composed answer is kept by the region as an artifact the moment it is
 * written, and any answer can be saved as a report, so neither depends on
 * the chat that produced it: a reload empties the conversation (the region
 * keeps no chat state) and leaves every artifact here, the open one included.
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

function ArtifactList({ onSelect }: { onSelect: (id: string) => void }) {
  const list = useQuery({ queryKey: ["artifacts"], queryFn: api.artifacts, staleTime: 10_000 });
  if (list.isLoading) return <div className="artifacts__empty"><span className="spinner" /></div>;
  if (list.isError) return <p className="error small artifacts__empty">{(list.error as Error).message}</p>;
  const items = list.data?.artifacts ?? [];
  if (items.length === 0) {
    return (
      <div className="artifacts__empty muted small">
        <p>No artifacts yet.</p>
        <p>
          Ask with <code>@compose</code> and the files it writes are kept here. Any other answer can be saved
          with <strong>Save as report</strong>. Artifacts stay until you delete them.
        </p>
      </div>
    );
  }
  return (
    <ul className="artifacts__list">
      {items.map((item) => (
        <li key={item.id}>
          <button type="button" className="artifacts__item" onClick={() => onSelect(item.id)}>
            <span className="artifacts__item-title">{item.title}</span>
            <span className="muted small">{describe(item)}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

/** "Composed files · 2 files · Nov 9, 01:12". */
export function describe(item: ArtifactSummary): string {
  const kind = item.kind === "files" ? "Composed files" : "Report";
  const files = item.files.length ? ` · ${item.files.length} file${item.files.length === 1 ? "" : "s"}` : "";
  const when = new Date(item.created_at);
  const date = Number.isNaN(when.getTime())
    ? ""
    : ` · ${when.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}`;
  return `${kind}${files}${date}`;
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
          {gone ? "This artifact is no longer kept: it was deleted, or belongs to someone else." : (artifact.error as Error).message}
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
          <span className="muted small">{describe(data)}</span>
        </div>
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
            if (window.confirm(`Delete "${data.title}"? It cannot be undone.`)) remove.mutate();
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
