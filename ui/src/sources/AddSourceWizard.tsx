import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { SourceRecord, SourceTypeInfo, SourceWritePayload } from "../api/types";
import { DirectoryBrowser } from "./DirectoryBrowser";

const SYNC_MODES = ["incremental", "full", "validate_only", "repair"];

// Shown when the type catalog hasn't loaded yet, so the form is never empty.
const FALLBACK_TYPES: SourceTypeInfo[] = [
  {
    id: "document_folder",
    label: "Folder of documents",
    description: "",
    path_role: "required",
    builtin: true,
  },
];

interface AddSourceWizardProps {
  source?: SourceRecord | null;
  onClose: () => void;
}

export function AddSourceWizard({ source, onClose }: AddSourceWizardProps) {
  const queryClient = useQueryClient();
  const editing = Boolean(source);
  const initial = useMemo(() => sourceConfig(source), [source]);
  const [browsePath, setBrowsePath] = useState<string | null>(null);
  const [chosen, setChosen] = useState<string | null>((initial.path as string | undefined) ?? null);
  const [name, setName] = useState(String(initial.name ?? ""));
  const [type, setType] = useState(String(initial.type ?? "document_folder"));
  const [description, setDescription] = useState(String(initial.description ?? ""));
  const [enabled, setEnabled] = useState(Boolean(initial.enabled ?? true));
  const [maxDepth, setMaxDepth] = useState(
    initial.max_depth === null || initial.max_depth === undefined ? "" : String(initial.max_depth),
  );
  const [includeText, setIncludeText] = useState(lines(initial.include));
  const [excludeText, setExcludeText] = useState(lines(initial.exclude));
  const chunking = (initial.chunking ?? {}) as Record<string, unknown>;
  const repo = (initial.repo ?? {}) as Record<string, unknown>;
  const sync = (initial.sync ?? {}) as Record<string, unknown>;
  const connector = (initial.connector ?? {}) as Record<string, unknown>;
  const [chunkingEnabled, setChunkingEnabled] = useState(Boolean(chunking.enabled ?? true));
  const [chunkStrategy, setChunkStrategy] = useState(String(chunking.strategy ?? "semantic"));
  const [chunkMax, setChunkMax] = useState(String(chunking.max_chars ?? 4000));
  const [chunkOverlap, setChunkOverlap] = useState(String(chunking.overlap_chars ?? 400));
  // Structural taxonomy: off by default, and per-source on purpose — it is a
  // claim that this corpus really is structured documentation.
  const taxonomy = (initial.taxonomy ?? {}) as Record<string, unknown>;
  const [taxonomyEnabled, setTaxonomyEnabled] = useState(Boolean(taxonomy.enabled ?? false));
  const [taxonomySplit, setTaxonomySplit] = useState(Boolean(taxonomy.split_on_sections ?? true));
  const [taxonomyGraphNodes, setTaxonomyGraphNodes] = useState(Boolean(taxonomy.graph_nodes ?? true));
  const [repoBranchPolicy, setRepoBranchPolicy] = useState(String(repo.branch_policy ?? "current"));
  const [repoIncludeUncommitted, setRepoIncludeUncommitted] = useState(
    Boolean(repo.include_uncommitted ?? true),
  );
  const [repoCommitTrigger, setRepoCommitTrigger] = useState(Boolean(repo.commit_trigger ?? true));
  const [syncOnStartup, setSyncOnStartup] = useState(Boolean(sync.on_startup ?? true));
  const [syncOnFileChange, setSyncOnFileChange] = useState(String(sync.on_file_change ?? "debounce"));
  const [syncOnGitCommit, setSyncOnGitCommit] = useState(Boolean(sync.on_git_commit ?? true));
  const [syncInterval, setSyncInterval] = useState(
    sync.interval_seconds === null || sync.interval_seconds === undefined
      ? ""
      : String(sync.interval_seconds),
  );
  const [urlsText, setUrlsText] = useState(lines(initial.urls));
  const [connectorText, setConnectorText] = useState(JSON.stringify(connector, null, 2));
  // The one connector setting nearly every service-backed source needs. It
  // holds an environment variable *name*, never a token, so promoting it out
  // of the raw JSON costs nothing in safety and saves hand-writing JSON.
  const [apiKeyEnv, setApiKeyEnv] = useState(String(connector.api_key_env ?? ""));
  const [syncNow, setSyncNow] = useState(!editing);
  const [syncMode, setSyncMode] = useState("incremental");
  const [error, setError] = useState<string | null>(null);

  // The set of types is a property of the deployment, not of this bundle:
  // an installed connector plugin adds one, and the form has to offer
  // exactly what YAML would accept.
  const catalog = useQuery({ queryKey: ["source-types"], queryFn: api.sourceTypes });
  const sourceTypes = catalog.data?.types ?? FALLBACK_TYPES;
  const placeholderPath = catalog.data?.placeholder_path ?? "/unused";
  const typeInfo = sourceTypes.find((entry) => entry.id === type);
  // Service-backed types (web, S3, connector plugins) still carry the
  // schema's mandatory path; it is a placeholder nothing ever opens, so
  // don't make anyone browse for a directory that has no meaning.
  const needsPath = (typeInfo?.path_role ?? "required") === "required";
  const effectivePath = needsPath ? chosen : (chosen ?? placeholderPath);

  const mutation = useMutation({
    mutationFn: async () => {
      const payload = buildPayload();
      if (!editing) {
        return api.registerSource(payload as SourceWritePayload & { name: string; path: string });
      }
      const result = await api.updateSource(source!.name, payload);
      // Updating a source clears its indexed artifacts server-side, so kick
      // off an incremental re-sync; otherwise the graph stays empty until the
      // user remembers to sync manually. Failures surface on the sources page
      // via last_status rather than blocking the save.
      api
        .syncSource(source!.name, "incremental")
        .catch(() => undefined)
        .finally(() => {
          queryClient.invalidateQueries({ queryKey: ["sources"] });
          queryClient.invalidateQueries({ queryKey: ["graph"] });
        });
      return result;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sources"] });
      queryClient.invalidateQueries({ queryKey: ["graph"] });
      onClose();
    },
    onError: (err) => setError((err as Error).message),
  });

  const choose = (path: string) => {
    setChosen(path);
    if (!name) {
      const leaf = path.split(/[\\/]/).filter(Boolean).pop() ?? "source";
      setName(leaf.replace(/[^a-zA-Z0-9-_]/g, "-").toLowerCase());
    }
  };

  const buildPayload = (): SourceWritePayload & { name?: string; path?: string } => {
    setError(null);
    let connectorPayload: Record<string, unknown>;
    try {
      connectorPayload = JSON.parse(connectorText || "{}");
    } catch {
      throw new Error("Connector settings must be valid JSON.");
    }
    // The dedicated field wins over whatever the JSON says, so editing the
    // obvious control cannot be silently undone by a stale JSON blob.
    if (apiKeyEnv.trim()) connectorPayload.api_key_env = apiKeyEnv.trim();
    const payload: SourceWritePayload = {
      type,
      path: effectivePath ?? undefined,
      description: description || undefined,
      enabled,
      max_depth: maxDepth.trim() ? Number(maxDepth) : null,
      // On create, omit empty pattern lists so the server's defaults apply
      // (curated text-file includes, .git/node_modules excludes). Sending []
      // would override those defaults and index everything. On edit the
      // fields are prefilled from the existing config, so send them as-is —
      // clearing them there is an explicit choice.
      include: editing ? patterns(includeText) : orUndefined(patterns(includeText)),
      exclude: editing ? patterns(excludeText) : orUndefined(patterns(excludeText)),
      chunking: {
        enabled: chunkingEnabled,
        strategy: chunkStrategy,
        max_chars: Number(chunkMax),
        overlap_chars: Number(chunkOverlap),
      },
      taxonomy: {
        enabled: taxonomyEnabled,
        split_on_sections: taxonomySplit,
        graph_nodes: taxonomyGraphNodes,
      },
      repo: {
        // URL quick-add persists its managed checkout recipe here. Editing
        // ordinary repository knobs must not silently turn that source back
        // into an unmanaged, stale local folder.
        ...repo,
        branch_policy: repoBranchPolicy,
        include_uncommitted: repoIncludeUncommitted,
        commit_trigger: repoCommitTrigger,
      },
      sync: {
        on_startup: syncOnStartup,
        on_file_change: syncOnFileChange,
        on_git_commit: syncOnGitCommit,
        interval_seconds: syncInterval.trim() ? Number(syncInterval) : null,
      },
      connector: connectorPayload,
      urls: patterns(urlsText),
    };
    if (!editing) {
      payload.name = name;
      payload.sync_now = syncNow;
      payload.sync_mode = syncMode;
      // The first sync of a freshly registered source is exactly the case
      // that produced a 504 for a large repo (indexing minutes long, well
      // past what a browser/reverse proxy holds a request open for) — never
      // block registration on it; the Sources page shows live progress.
      payload.wait = false;
    }
    return payload;
  };

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal modal--wide" onClick={(e) => e.stopPropagation()}>
        <header className="modal__header">
          <h2>{editing ? "Edit source" : "Add a source"}</h2>
          <button className="btn btn--ghost" onClick={onClose}>
            close
          </button>
        </header>

        <div className="wizard">
          <div className="wizard__col">
            <h4>{needsPath ? "Directory" : "Connection"}</h4>
            {needsPath ? (
              <DirectoryBrowser
                path={browsePath}
                onNavigate={setBrowsePath}
                onChoose={choose}
                allowFiles={type === "single_file"}
              />
            ) : (
              <div className="muted small">
                <p>
                  <strong>{typeInfo?.label ?? type}</strong> pulls from its own service, so
                  there is no folder to pick.
                </p>
                <p>{typeInfo?.description}</p>
                {type === "web_collection" ? (
                  <label className="field">
                    <span>Page URLs, one per line</span>
                    <textarea
                      className="text-area"
                      value={urlsText}
                      onChange={(e) => setUrlsText(e.target.value)}
                      placeholder={"https://example.com/blog/post\nhttps://example.com/report.pdf"}
                      aria-label="Page URLs"
                    />
                    <span className="muted small">
                      Each page is fetched, stored as text, and re-checked on its own schedule:
                      hourly at first, less often while it stays unchanged.
                    </span>
                  </label>
                ) : (
                  <p>
                    Point it at your account with Connector JSON, in Sync and connectors —
                    typically an api_key_env naming the environment variable that holds the
                    token. The token itself is read from the container environment and never
                    stored in your config.
                  </p>
                )}
              </div>
            )}
          </div>

          <div className="wizard__col wizard__col--form">
            <h4>Source settings</h4>
            <div className="form-grid">
              <label className="field">
                <span>{needsPath ? "Chosen path" : "Path (unused)"}</span>
                <input
                  className="text-input"
                  value={chosen ?? ""}
                  disabled={!needsPath}
                  onChange={(event) => setChosen(event.target.value)}
                  placeholder={needsPath ? "Container-visible path" : placeholderPath}
                />
              </label>
              <label className="field">
                <span>Name</span>
                <input
                  className="text-input"
                  value={name}
                  disabled={editing}
                  onChange={(event) => setName(event.target.value)}
                />
              </label>
              <label className="field">
                <span>Type</span>
                <select className="text-input" value={type} onChange={(e) => setType(e.target.value)}>
                  {sourceTypes.map((entry) => (
                    <option key={entry.id} value={entry.id}>
                      {entry.label}
                      {entry.builtin ? "" : " · plugin"}
                    </option>
                  ))}
                  {/* An existing source may use a type whose plugin is no
                      longer installed — keep it visible instead of silently
                      rewriting the source to something else on save. */}
                  {sourceTypes.some((entry) => entry.id === type) ? null : (
                    <option value={type}>{type} (not installed)</option>
                  )}
                </select>
                {typeInfo ? <span className="muted small">{typeInfo.description}</span> : null}
              </label>
              {needsPath ? (
                <label className="field">
                  <span>Folder depth</span>
                  <input
                    className="text-input"
                    type="number"
                    min="0"
                    value={maxDepth}
                    onChange={(event) => setMaxDepth(event.target.value)}
                    placeholder="unlimited"
                  />
                </label>
              ) : type === "web_collection" ? null : (
                <label className="field">
                  <span>API key environment variable</span>
                  <input
                    className="text-input"
                    value={apiKeyEnv}
                    onChange={(event) => setApiKeyEnv(event.target.value)}
                    placeholder="e.g. NOTION_TOKEN — the variable name, not the token"
                  />
                </label>
              )}
            </div>

            <label className="field">
              <span>Description</span>
              <input
                className="text-input"
                value={description}
                onChange={(event) => setDescription(event.target.value)}
              />
            </label>

            <div className="form-grid">
              <label className="field">
                <span>Include patterns</span>
                <textarea
                  className="text-area"
                  value={includeText}
                  onChange={(e) => setIncludeText(e.target.value)}
                  placeholder={editing ? undefined : "empty = server defaults (**/*.py, **/*.md, …)"}
                />
              </label>
              <label className="field">
                <span>Exclude patterns</span>
                <textarea
                  className="text-area"
                  value={excludeText}
                  onChange={(e) => setExcludeText(e.target.value)}
                  placeholder={editing ? undefined : "empty = server defaults (.git, node_modules, …)"}
                />
              </label>
            </div>

            <details className="settings-group" open>
              <summary>Indexing</summary>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={chunkingEnabled}
                  onChange={(e) => setChunkingEnabled(e.target.checked)}
                />
                Chunk content for retrieval
              </label>
              <div className="form-grid">
                <label className="field">
                  <span>Chunk strategy</span>
                  <input className="text-input" value={chunkStrategy} onChange={(e) => setChunkStrategy(e.target.value)} />
                </label>
                <label className="field">
                  <span>Max chars</span>
                  <input className="text-input" type="number" value={chunkMax} onChange={(e) => setChunkMax(e.target.value)} />
                </label>
                <label className="field">
                  <span>Overlap chars</span>
                  <input className="text-input" type="number" value={chunkOverlap} onChange={(e) => setChunkOverlap(e.target.value)} />
                </label>
              </div>

              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={taxonomyEnabled}
                  onChange={(e) => setTaxonomyEnabled(e.target.checked)}
                />
                Extract a section taxonomy (chapters, articles, § codes)
              </label>
              <p className="muted small">
                For books, standards, procedures and contracts. Results then say which section
                matched, and the outline is browsable. Leave off for code or ordinary notes —
                numbered lines there are usually list items, not sections.
              </p>
              {taxonomyEnabled ? (
                <div className="form-grid">
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={taxonomySplit}
                      onChange={(e) => setTaxonomySplit(e.target.checked)}
                    />
                    Cut chunks at section boundaries
                  </label>
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={taxonomyGraphNodes}
                      onChange={(e) => setTaxonomyGraphNodes(e.target.checked)}
                    />
                    Add section nodes to the graph
                  </label>
                </div>
              ) : null}
            </details>

            <details className="settings-group">
              <summary>Sync and connectors</summary>
              <div className="form-grid">
                <label className="checkbox">
                  <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
                  Enabled
                </label>
                <label className="checkbox">
                  <input type="checkbox" checked={syncOnStartup} onChange={(e) => setSyncOnStartup(e.target.checked)} />
                  Sync on startup
                </label>
                <label className="checkbox">
                  <input type="checkbox" checked={syncOnGitCommit} onChange={(e) => setSyncOnGitCommit(e.target.checked)} />
                  Sync on git commit
                </label>
                <label className="field">
                  <span>File changes</span>
                  <input className="text-input" value={syncOnFileChange} onChange={(e) => setSyncOnFileChange(e.target.value)} />
                </label>
                <label className="field">
                  <span>Interval seconds</span>
                  <input className="text-input" type="number" value={syncInterval} onChange={(e) => setSyncInterval(e.target.value)} />
                </label>
              </div>
              {type !== "web_collection" && (
                <label className="field">
                  <span>URLs</span>
                  <textarea className="text-area" value={urlsText} onChange={(e) => setUrlsText(e.target.value)} />
                </label>
              )}
              <label className="field">
                <span>Connector JSON</span>
                <textarea className="text-area text-area--code" value={connectorText} onChange={(e) => setConnectorText(e.target.value)} />
              </label>
            </details>

            <details className="settings-group">
              <summary>Repository</summary>
              <div className="form-grid">
                <label className="field">
                  <span>Branch policy</span>
                  <input className="text-input" value={repoBranchPolicy} onChange={(e) => setRepoBranchPolicy(e.target.value)} />
                </label>
                <label className="checkbox">
                  <input type="checkbox" checked={repoIncludeUncommitted} onChange={(e) => setRepoIncludeUncommitted(e.target.checked)} />
                  Include uncommitted
                </label>
                <label className="checkbox">
                  <input type="checkbox" checked={repoCommitTrigger} onChange={(e) => setRepoCommitTrigger(e.target.checked)} />
                  Commit trigger
                </label>
              </div>
            </details>

            {!editing && (
              <div className="form-grid">
                <label className="checkbox">
                  <input type="checkbox" checked={syncNow} onChange={(e) => setSyncNow(e.target.checked)} />
                  Sync now
                </label>
                <label className="field">
                  <span>Sync mode</span>
                  <select className="text-input" value={syncMode} onChange={(e) => setSyncMode(e.target.value)}>
                    {SYNC_MODES.map((mode) => (
                      <option key={mode} value={mode}>
                        {mode}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            )}

            {(editing || syncNow) && (
              <div className="banner banner--warn" style={{ marginBottom: 0 }}>
                {editing
                  ? "Saving re-syncs this source in the background — search and chat may respond more slowly until it finishes."
                  : "Search and chat may respond more slowly while this source syncs in the background, especially for a large one — that settles once it finishes. Progress shows on the Sources page and the \"Syncing…\" indicator at the top of every page."}
              </div>
            )}

            {(error || mutation.isError) && <p className="error">{error ?? (mutation.error as Error).message}</p>}

            <button
              className="btn btn--primary"
              disabled={
                (needsPath && !chosen) ||
                (type === "web_collection" && patterns(urlsText).length === 0) ||
                !name ||
                mutation.isPending
              }
              onClick={() => mutation.mutate()}
            >
              {mutation.isPending ? "Saving..." : editing ? "Save source" : "Register source"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function sourceConfig(source?: SourceRecord | null): Record<string, unknown> {
  if (!source) return {};
  if (source.config_json) {
    try {
      return JSON.parse(source.config_json);
    } catch {
      return source;
    }
  }
  return source;
}

function orUndefined(value: string[]): string[] | undefined {
  return value.length > 0 ? value : undefined;
}

function patterns(value: string): string[] {
  return value
    .split(/\r?\n|,/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function lines(value: unknown): string {
  return Array.isArray(value) ? value.join("\n") : "";
}
