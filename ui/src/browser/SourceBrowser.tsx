import { useMemo, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { BrowseFile, BrowseFolder, BrowseResponse, FileQuality, FolderHealth } from "../api/types";
import { DEFAULT_TREE_WIDTH, useSession } from "../state/session";
import { TileDock, TileFold, TileGrip } from "../components/Tiles";
import { ArchivedList } from "./ArchivedList";
import { DragHandle } from "./DragHandle";
import { RemoveButton } from "./RemoveButton";
import { ScopeToggle } from "./ScopeToggle";
import { FileViewer } from "./FileViewer";
import { fileIcon, formatBytes } from "./fileIcons";

const PAGE_SIZE = 200;

/**
 * The taxonomy view: a source as a file collection, beside the chat.
 *
 * The left column is one folder at a time — sub-folders with how many
 * documents each holds, then files — walked down by opening a folder and back
 * up by the breadcrumbs, the ↑ button, or ←/Backspace. The top level is the
 * sources themselves. Opening a file shows it on the right, rendered for its
 * format (`FileViewer`). Everything listed is what the index holds
 * (`GET /documents/tree`), so it reads the same on a replica with no mount.
 *
 * It is one of the Notebook's tiles: folded, the tile shows a header strip
 * that keeps the place, and the tile can be dragged to another column.
 */
export function SourceBrowser({
  onAsk,
  onShowInGraph,
}: {
  onAsk: (text: string) => void;
  onShowInGraph: (nodeId: string) => void;
}) {
  const { state, dispatch } = useSession();
  const { browser, browserCollapsed, browserTreeWidth } = state;
  const bodyRef = useRef<HTMLDivElement>(null);
  const [filter, setFilter] = useState("");

  const listing = useInfiniteQuery({
    queryKey: ["browse", browser.source, browser.path],
    queryFn: ({ pageParam }) =>
      api.browse({ source: browser.source, path: browser.path, offset: pageParam, limit: PAGE_SIZE }),
    initialPageParam: 0,
    getNextPageParam: (last: BrowseResponse) =>
      last.page.has_more ? last.page.offset + last.page.returned : undefined,
    enabled: browser.open && !browserCollapsed,
  });

  const first = listing.data?.pages[0];
  const folders = first?.directories ?? [];
  const files = useMemo(() => listing.data?.pages.flatMap((page) => page.files) ?? [], [listing.data]);
  const needle = filter.trim().toLowerCase();
  const shownFolders = needle ? folders.filter((f) => f.name.toLowerCase().includes(needle)) : folders;
  const shownFiles = needle ? files.filter((f) => f.name.toLowerCase().includes(needle)) : files;

  const go = (source: string | null, path: string) => {
    setFilter("");
    dispatch({ type: "open-browser", source, path });
  };
  const up = () => {
    if (!browser.source) return;
    if (!browser.path) go(null, "");
    else go(browser.source, browser.path.includes("/") ? browser.path.slice(0, browser.path.lastIndexOf("/")) : "");
  };
  const openFolder = (folder: BrowseFolder) =>
    folder.source && !browser.source ? go(folder.source, "") : go(browser.source, folder.path);
  const openFile = (file: BrowseFile) => {
    if (!browser.source || file.id === null) return;
    dispatch({ type: "open-file", file: { source: browser.source, path: file.path } });
  };

  /** Whether questions are currently answered from exactly this folder or file. */
  const scopes = (source: string | null | undefined, path: string) =>
    path
      ? state.pathScope?.source === source && state.pathScope?.path === path
      : !state.pathScope && Boolean(source) && state.sourceFilter === source && !browser.source;

  const title = browser.source ? [browser.source, ...browser.path.split("/").filter(Boolean)].join(" / ") : "All sources";

  const onListKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const rows = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>(".browser-row"));
    const index = rows.findIndex((row) => row === document.activeElement);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const next = rows[Math.max(0, Math.min(rows.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)))];
      next?.focus();
    } else if ((event.key === "ArrowLeft" || event.key === "Backspace") && index >= 0) {
      event.preventDefault();
      up();
    } else if (event.key === "ArrowRight" && index >= 0) {
      event.preventDefault();
      rows[index].click();
    }
  };

  return (
    <section className="pane browser" aria-label="Source browser">
      <header className="pane__header browser__header">
        <TileGrip />
        <span className="pane__title">Taxonomy</span>
        <nav className="browser__crumbs" aria-label="Folder">
          <button type="button" className="link-button" onClick={() => go(null, "")}>
            All sources
          </button>
          {(first?.breadcrumbs ?? []).map((crumb) => (
            <span key={`${crumb.source}:${crumb.path}`}>
              <span className="viewer__sep">›</span>
              <button type="button" className="link-button" onClick={() => go(crumb.source, crumb.path)}>
                {crumb.name}
              </button>
            </span>
          ))}
        </nav>
        <div className="pane__actions">
          {browser.source && first?.source === browser.source ? (
            <ScopeToggle source={browser.source} path={first.path} kind="folder" />
          ) : null}
          {browser.source && first?.source === browser.source && first.path ? (
            <RemoveButton
              key={`${browser.source}/${first.path}`}
              folder
              source={browser.source}
              path={first.path}
              onRemoved={() => go(browser.source, first.path.split("/").slice(0, -1).join("/"))}
            />
          ) : null}
          <button
            type="button"
            className="btn btn--small btn--icon btn--ghost"
            onClick={() => dispatch({ type: "close-browser" })}
            aria-label="Close the source browser"
            title="Close"
          >
            ✕
          </button>
          <TileDock />
          <TileFold />
        </div>
      </header>
      <div
        className={`browser__body${browser.file ? " browser__body--file" : ""}`}
        ref={bodyRef}
        style={{ "--tree": `${browserTreeWidth}px` } as CSSProperties}
      >
        <div className="browser__list">
          <div className="browser__tools">
            <button
              type="button"
              className="btn btn--small btn--icon"
              onClick={up}
              disabled={!browser.source}
              aria-label="Up one folder"
              title="Up one folder (← or Backspace)"
            >
              ↑
            </button>
            <input
              className="text-input browser__filter"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              placeholder={browser.source ? "Filter this folder" : "Filter sources"}
              aria-label="Filter this folder"
            />
          </div>
          {listing.isLoading ? (
            <div className="centered muted small browser__state">
              <span className="spinner" />
            </div>
          ) : listing.isError ? (
            <div className="browser__state">
              <p className="error small">{(listing.error as Error).message}</p>
              <button type="button" className="btn btn--small" onClick={() => go(browser.source, "")}>
                {browser.source ? `Back to the root of ${browser.source}` : "Retry"}
              </button>
            </div>
          ) : (
            <div className="browser__rows" role="listbox" aria-label="Folders and files" onKeyDown={onListKeyDown}>
              {shownFolders.map((folder) => (
                <button
                  type="button"
                  role="option"
                  aria-selected={false}
                  key={`d:${folder.source ?? ""}:${folder.path}`}
                  className="browser-row browser-row--folder"
                  onClick={() => openFolder(folder)}
                  title={folder.source && !browser.source ? `Open the ${folder.name} source` : `Open ${folder.path}/`}
                >
                  <span className="browser-row__icon" aria-hidden>
                    {browser.source ? "📁" : "🗃"}
                  </span>
                  <span className="browser-row__name">{folder.name}</span>
                  {scopes(folder.source ?? browser.source, folder.path) ? (
                    <span className="browser-row__scope" title="Questions are answered only from here">
                      ⌖
                    </span>
                  ) : null}
                  {folder.source_type ? <span className="pill">{folder.source_type}</span> : null}
                  <HealthPill health={folder.health} />
                  <span className="browser-row__count" title={`${folder.documents} documents beneath`}>
                    {folder.documents.toLocaleString()}
                  </span>
                  <span className="browser-row__chevron" aria-hidden>
                    ›
                  </span>
                </button>
              ))}
              {shownFiles.map((file) => {
                const active = browser.file?.source === browser.source && browser.file?.path === file.path;
                return (
                  <button
                    type="button"
                    role="option"
                    aria-selected={active}
                    key={`f:${file.id ?? file.path}`}
                    className={`browser-row browser-row--file${active ? " browser-row--active" : ""}${
                      file.id === null ? " browser-row--unread" : ""
                    }`}
                    onClick={() => openFile(file)}
                    aria-disabled={file.id === null}
                    title={qualityTitle(file)}
                  >
                    <span className="browser-row__icon" aria-hidden>
                      {file.viewer ? fileIcon(file.viewer) : "⛔"}
                    </span>
                    <span className="browser-row__name">{file.name}</span>
                    <QualityMark quality={file.quality} />
                    {scopes(browser.source, file.path) ? (
                      <span className="browser-row__scope" title="Questions are answered only from this file">
                        ⌖
                      </span>
                    ) : null}
                    {state.excluded.includes(`${browser.source}/${file.path}`) ? (
                      <span className="browser-row__scope browser-row__omitted" title="Left out of every answer">
                        ⊘
                      </span>
                    ) : null}
                    <span className="browser-row__size">{formatBytes(file.size_bytes)}</span>
                  </button>
                );
              })}
              {!shownFolders.length && !shownFiles.length ? (
                <p className="muted small browser__state">
                  {needle ? "Nothing here matches the filter." : browser.source ? "This folder holds nothing indexed." : "No sources yet."}
                </p>
              ) : null}
              {browser.source ? <ArchivedList source={browser.source} path={browser.path} /> : null}
              {listing.hasNextPage ? (
                <button
                  type="button"
                  className="btn btn--small browser__more"
                  disabled={listing.isFetchingNextPage}
                  onClick={() => listing.fetchNextPage()}
                >
                  {listing.isFetchingNextPage
                    ? "Loading…"
                    : `Load more files (${((first?.totals.files ?? 0) - files.length).toLocaleString()} left)`}
                </button>
              ) : null}
            </div>
          )}
        </div>

        <DragHandle
          axis="x"
          variable="--tree"
          containerRef={bodyRef}
          min={180}
          max={(box) => box.width - 320}
          defaultSize={DEFAULT_TREE_WIDTH}
          label="Resize the folder list"
          onCommit={(width) => dispatch({ type: "set-pane-width", pane: "tree", width })}
        />

        <div className="browser__viewer">
          {browser.file ? (
            <FileViewer
              source={browser.file.source}
              path={browser.file.path}
              chunkId={browser.file.chunkId}
              anchor={browser.file.anchor}
              onClose={() => dispatch({ type: "close-file" })}
              onOpenPath={(path, anchor) =>
                dispatch({ type: "open-file", file: { source: browser.file!.source, path, anchor } })
              }
              onBrowse={(folder) => go(browser.file!.source, folder)}
              onAsk={onAsk}
              onShowInGraph={onShowInGraph}
            />
          ) : (
            <FolderSummary listing={first} />
          )}
        </div>
      </div>
    </section>
  );
}

/** What the right pane says before a file is opened: what this folder holds. */
function FolderSummary({ listing }: { listing?: BrowseResponse }) {
  if (!listing) return null;
  const { totals } = listing;
  const kinds = new Map<string, number>();
  for (const file of listing.files) {
    if (file.viewer) kinds.set(file.viewer, (kinds.get(file.viewer) ?? 0) + 1);
  }
  return (
    <div className="folder-summary">
      <div className="folder-summary__icon" aria-hidden>
        {listing.source ? "📂" : "🗃"}
      </div>
      <h3>{listing.source ? listing.breadcrumbs[listing.breadcrumbs.length - 1]?.name : "All sources"}</h3>
      <p className="muted">
        {listing.source
          ? `${count(totals.directories, "folder")} · ${count(totals.files, "file")} here · ${count(totals.documents, "document")} beneath`
          : `${count(totals.directories, "source")} · ${count(totals.documents, "document")}`}
      </p>
      {listing.health && listing.health.percent !== null ? (
        <p className={`small ${healthClass(listing.health)}`}>
          {listing.health.percent}% readable
          {listing.health.ok !== undefined
            ? ` · ${listing.health.ok} ok · ${listing.health.warning} warning · ${listing.health.error} error`
            : ""}
        </p>
      ) : null}
      {kinds.size ? (
        <div className="folder-summary__kinds">
          {[...kinds.entries()]
            .sort((a, b) => b[1] - a[1])
            .map(([kind, count]) => (
              <span className="pill" key={kind}>
                {fileIcon(kind)} {kind} · {count}
              </span>
            ))}
        </div>
      ) : null}
      <p className="muted small">
        {listing.source ? "Select a file to open it here." : "Open a source to walk its folders."}
      </p>
    </div>
  );
}

/** A folder's (or a source's) readable share, beside its count; nothing when all reads. */
function HealthPill({ health }: { health?: FolderHealth | null }) {
  if (!health || health.percent === null || (health.percent === 100 && !health.warning)) return null;
  const detail =
    health.ok !== undefined
      ? `${health.ok} ok, ${health.warning} warning, ${health.error} error beneath`
      : `${health.percent}% of this source reads`;
  return (
    <span className={`pill browser-row__health ${healthClass(health)}`} title={detail}>
      {health.percent}%
    </span>
  );
}

/** A file's state: nothing when ok, a mark naming its flags otherwise. */
function QualityMark({ quality }: { quality?: FileQuality }) {
  if (!quality || quality.status === "ok") return null;
  return (
    <span
      className={`browser-row__quality ${quality.status === "error" ? "error" : "source-status--partial"}`}
      aria-label={quality.flags.map((flag) => flag.code).join(", ")}
    >
      {quality.status === "error" ? "✕" : "⚠"}
    </span>
  );
}

function qualityTitle(file: BrowseFile): string {
  const reasons = (file.quality?.flags ?? []).map((flag) => `${flag.code}: ${flag.reason}`);
  return [file.url ?? file.path, ...reasons].join("\n");
}

function healthClass(health: FolderHealth): string {
  if (health.status === "degraded" || health.error) return "error";
  return health.percent === 100 && !health.warning ? "muted" : "source-status--partial";
}

function count(n: number, noun: string): string {
  return `${n.toLocaleString()} ${noun}${n === 1 ? "" : "s"}`;
}
