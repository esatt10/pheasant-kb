import { createContext, useContext, useEffect, useMemo, useReducer } from "react";
import type { ReactNode } from "react";
import type { AnswerDepth, ChatAnswer, GraphLink, GraphNode, HistoryTurn } from "../api/types";
import { NOISY_NODE_TYPES } from "../graph/graphStyles";

/**
 * Workspace session state.
 *
 * Every piece of "where I am and what I was looking at" lives here, in one
 * store mounted ABOVE the router — so switching between Notebook, Sources and
 * Settings, or between the Graph/Facts/Node tabs, never unmounts it and never
 * resets it. The reducer is the only way anything changes: navigation, mounts
 * and refetches are not actions and so cannot disturb the view. Only a
 * deliberate user action does.
 */

export type PanelTab = "graph" | "facts" | "node";

/** A document open in the source browser's viewer. */
export interface OpenFile {
  source: string;
  path: string;
  /** The cited passage it was opened at, to scroll to and mark. */
  chunkId?: string | null;
  /** A section to open at (a Markdown link's `#fragment`). */
  anchor?: string | null;
}

/**
 * "Search only here": one folder, or one file, of a source that every
 * question is answered from. The narrower sibling of `sourceFilter`, set from
 * the taxonomy view; `path` is spelled as the index lists it.
 */
export interface PathScope {
  source: string;
  path: string;
  kind: "folder" | "file";
}

/**
 * The source browser above the chat: which folder it shows and which file is
 * open beside it. `source: null` is the level above every source.
 */
export interface BrowserState {
  open: boolean;
  source: string | null;
  path: string;
  file: OpenFile | null;
}

export interface ChatTurn {
  id: string;
  question: string;
  answer?: ChatAnswer;
  error?: string;
}

export interface SessionState {
  /** Restrict retrieval + graph to one source, or null for everything. */
  sourceFilter: string | null;
  /** Restrict retrieval further, to one folder or file of `sourceFilter`. */
  pathScope: PathScope | null;
  /**
   * Restrict retrieval to one *kind* of source (repository, gdrive, web_collection…),
   * or null for every kind. Independent of `sourceFilter`: picking a type
   * narrows which sources are in play without committing to one of them,
   * which is the useful control once a knowledge base has more sources than
   * fit on a screen.
   */
  sourceTypeFilter: string | null;
  hiddenTypes: string[];
  panelTab: PanelTab;
  selectedId: string | null;
  /** Node the depth horizon is measured from. */
  centerId: string | null;
  /** How many layers out from the center are drawn. */
  depth: number;
  /** Escape hatch: draw the whole (filtered) graph, ignoring the horizon. */
  showAll: boolean;
  /** Layout algorithm for the canvas. */
  layout: GraphLayout;
  /** Sources rail collapsed to a spine, giving the other panes its width. */
  railCollapsed: boolean;
  /** Graph/facts/node panel collapsed to a spine, giving the chat its width. */
  panelCollapsed: boolean;
  /** Nodes the last answer surfaced; non-empty means the canvas is filtered. */
  surfacedIds: string[];
  focusIds: string[];
  answer: ChatAnswer | null;
  workflow: string | null;
  turns: ChatTurn[];
  draft: string;
  /**
   * Turns before this index are shown but no longer sent as context. "New
   * topic" moves it to the end, so the next question is answered on its own
   * without wiping the thread the reader may still want to scroll.
   */
  contextFrom: number;
  /** A pinned answer length, or null to let each question decide. */
  answerDepth: AnswerDepth | null;
  /** Pane widths in px. The graph pane in particular needs to be growable. */
  railWidth: number;
  panelWidth: number;
  /** The source browser, shown above the chat when open. */
  browser: BrowserState;
  /** Browser folded to its header strip, giving the chat the height. */
  browserCollapsed: boolean;
  /** Chat folded to its header strip, giving the browser the height. */
  chatCollapsed: boolean;
  /** Height of the browser while both it and the chat are unfolded, in px. */
  browserHeight: number;
  /** Width of the folder list beside the file viewer, in px. */
  browserTreeWidth: number;
  /**
   * Documents left out of every answer, as `source/path`. Sent as
   * `exclude_documents` for the source in scope; the omit toggle on an open
   * file adds and removes them, and the composer clears them.
   */
  excluded: string[];
}

export const DEFAULT_RAIL_WIDTH = 280;
export const DEFAULT_PANEL_WIDTH = 400;

export const DEFAULT_BROWSER_HEIGHT = 420;
export const DEFAULT_TREE_WIDTH = 260;

export const DEFAULT_DEPTH = 3;
export const MIN_DEPTH = 1;
export const MAX_DEPTH = 6;

const INITIAL: SessionState = {
  sourceFilter: null,
  pathScope: null,
  sourceTypeFilter: null,
  hiddenTypes: NOISY_NODE_TYPES,
  panelTab: "graph",
  selectedId: null,
  centerId: null,
  depth: DEFAULT_DEPTH,
  showAll: false,
  // Start with a stable radial overview. It keeps the selected/root node at
  // the centre, makes hop distance legible, and avoids the long force-layout
  // animation on the first view of a large knowledge base.
  layout: "concentric",
  railCollapsed: false,
  panelCollapsed: false,
  surfacedIds: [],
  focusIds: [],
  answer: null,
  workflow: null,
  turns: [],
  draft: "",
  contextFrom: 0,
  answerDepth: null,
  railWidth: DEFAULT_RAIL_WIDTH,
  panelWidth: DEFAULT_PANEL_WIDTH,
  browser: { open: false, source: null, path: "", file: null },
  browserCollapsed: false,
  chatCollapsed: false,
  browserHeight: DEFAULT_BROWSER_HEIGHT,
  browserTreeWidth: DEFAULT_TREE_WIDTH,
  excluded: [],
};

export type SessionAction =
  | { type: "filter-source"; source: string | null }
  | { type: "set-path-scope"; scope: PathScope | null }
  | { type: "toggle-excluded"; document: string }
  | { type: "clear-excluded"; source?: string | null }
  | { type: "filter-source-type"; sourceType: string | null }
  | { type: "toggle-type"; nodeType: string }
  | { type: "open-tab"; tab: PanelTab }
  | { type: "select-node"; nodeId: string | null }
  | { type: "center-node"; nodeId: string }
  | { type: "set-depth"; depth: number }
  | { type: "show-all"; value: boolean }
  | { type: "set-layout"; layout: GraphLayout }
  | { type: "toggle-rail" }
  | { type: "toggle-panel" }
  | { type: "clear-answer-filter" }
  | { type: "set-workflow"; workflow: string | null }
  | { type: "set-draft"; text: string }
  | { type: "ask"; id: string; question: string }
  | { type: "answered"; id: string; answer: ChatAnswer }
  | { type: "ask-failed"; id: string; error: string }
  | { type: "clear-view" }
  | { type: "new-conversation" }
  | { type: "new-topic" }
  | { type: "set-answer-depth"; depth: AnswerDepth | null }
  | { type: "set-pane-width"; pane: "rail" | "panel" | "browser" | "tree"; width: number }
  | { type: "open-browser"; source: string | null; path?: string }
  | { type: "open-file"; file: OpenFile }
  | { type: "close-file" }
  | { type: "close-browser" }
  | { type: "toggle-browser-collapsed" }
  | { type: "toggle-chat-collapsed" };

/**
 * Canvas layout algorithms.
 *
 * `kamada-kawai` (ELK's stress majorization) used to be an option here; it
 * broke the canvas in practice and was removed along with the `cytoscape-elk`
 * dependency it was the only user of.
 */
export type GraphLayout = "auto" | "cose" | "concentric" | "breadthfirst" | "columns";

export const GRAPH_LAYOUTS: { value: GraphLayout; label: string }[] = [
  { value: "auto", label: "Automatic" },
  { value: "cose", label: "Force" },
  { value: "concentric", label: "Concentric" },
  { value: "breadthfirst", label: "Hierarchy" },
  // Opt-in: nodes in columns by role (graph/columnsLayout.ts). Not the
  // default — a whole large graph makes tall columns; concentric stays the
  // overview.
  { value: "columns", label: "Columns" },
];

/** The region refuses more than this many names in one request. */
const MAX_EXCLUDED = 50;

/**
 * The omitted documents that apply to a question scoped to `source` (and,
 * under a folder scope, to that folder): one outside the scope is left out
 * already, and sending it would only add noise.
 */
export function excludedInScope(
  excluded: string[],
  source: string | null,
  scope: PathScope | null,
): string[] {
  if (scope?.kind === "file") return [];
  const prefix = scope ? `${scope.source}/${scope.path}/` : source ? `${source}/` : "";
  return excluded.filter((d) => d.startsWith(prefix));
}

function clampDepth(depth: number): number {
  if (!Number.isFinite(depth)) return DEFAULT_DEPTH;
  return Math.min(MAX_DEPTH, Math.max(MIN_DEPTH, Math.round(depth)));
}

export function sessionReducer(state: SessionState, action: SessionAction): SessionState {
  switch (action.type) {
    case "filter-source":
      // A folder scope lives inside one source: choosing another source (or
      // all of them) is choosing a scope that no longer contains it.
      return {
        ...state,
        sourceFilter: action.source,
        pathScope: state.pathScope?.source === action.source ? state.pathScope : null,
      };
    case "toggle-excluded":
      return {
        ...state,
        excluded: state.excluded.includes(action.document)
          ? state.excluded.filter((d) => d !== action.document)
          : [...state.excluded, action.document].slice(-MAX_EXCLUDED),
      };
    case "clear-excluded":
      return {
        ...state,
        excluded: action.source
          ? state.excluded.filter((d) => !d.startsWith(`${action.source}/`))
          : [],
      };
    case "set-path-scope":
      // Scoping to a folder scopes to its source too, so the rail, the graph
      // and the composer all say the same thing.
      return action.scope
        ? { ...state, pathScope: action.scope, sourceFilter: action.scope.source }
        : { ...state, pathScope: null };
    case "filter-source-type":
      // Selecting a type clears a source selected under a different one, so
      // the two controls cannot contradict each other on screen.
      return { ...state, sourceTypeFilter: action.sourceType };
    case "toggle-type":
      return {
        ...state,
        hiddenTypes: state.hiddenTypes.includes(action.nodeType)
          ? state.hiddenTypes.filter((t) => t !== action.nodeType)
          : [...state.hiddenTypes, action.nodeType],
      };
    case "open-tab":
      return { ...state, panelTab: action.tab };
    case "select-node":
      return {
        ...state,
        selectedId: action.nodeId,
        focusIds: action.nodeId ? [action.nodeId] : [],
      };
    case "toggle-rail":
      return { ...state, railCollapsed: !state.railCollapsed };
    case "toggle-panel":
      return { ...state, panelCollapsed: !state.panelCollapsed };
    case "set-layout":
      return { ...state, layout: action.layout };
    case "center-node":
      // Re-centering is a navigation action, so it clears the answer filter:
      // you asked to look somewhere else.
      return {
        ...state,
        centerId: action.nodeId,
        selectedId: action.nodeId,
        focusIds: [action.nodeId],
        surfacedIds: [],
        showAll: false,
      };
    case "set-depth":
      return { ...state, depth: clampDepth(action.depth), showAll: false };
    case "show-all":
      return { ...state, showAll: action.value };
    case "clear-answer-filter":
      return { ...state, surfacedIds: [] };
    case "set-workflow":
      return { ...state, workflow: action.workflow };
    case "set-draft":
      return { ...state, draft: action.text };
    case "ask":
      return {
        ...state,
        draft: "",
        turns: [...state.turns, { id: action.id, question: action.question }],
      };
    case "answered": {
      const surfaced = surfacedFrom(action.answer);
      // `@taxonomy` answers from the index and, here, also opens the browser
      // at the folder it listed, above the chat.
      const browsed = browsedFrom(action.answer);
      return {
        ...state,
        ...(browsed ? openBrowser(state, browsed.source, browsed.path) : {}),
        // Streaming chat first dispatches the text with visual.status=pending,
        // then dispatches the same turn again when its visual is ready. Replace
        // that turn by id so the completed MCP App result reaches the UI.
        turns: state.turns.map((turn) =>
          turn.id === action.id ? { ...turn, answer: action.answer, error: undefined } : turn,
        ),
        answer: action.answer,
        // Asking is the action that re-aims the canvas: filter to what the
        // answer actually surfaced, centered on its strongest citation.
        surfacedIds: surfaced,
        centerId: surfaced[0] ?? state.centerId,
        focusIds: surfaced,
        showAll: false,
        panelTab: action.answer.facts.length > 0 ? "facts" : "graph",
      };
    }
    case "ask-failed":
      return {
        ...state,
        turns: state.turns.map((turn) =>
          turn.id === action.id && !turn.answer ? { ...turn, error: action.error } : turn,
        ),
      };
    case "set-pane-width": {
      const width = Math.round(action.width);
      if (action.pane === "rail") return { ...state, railWidth: width };
      if (action.pane === "browser") return { ...state, browserHeight: width };
      if (action.pane === "tree") return { ...state, browserTreeWidth: width };
      return { ...state, panelWidth: width };
    }
    case "open-browser":
      return { ...state, ...openBrowser(state, action.source, action.path ?? "") };
    case "open-file": {
      const slash = action.file.path.lastIndexOf("/");
      return {
        ...state,
        ...openBrowser(state, action.file.source, slash > 0 ? action.file.path.slice(0, slash) : ""),
        browser: {
          open: true,
          source: action.file.source,
          path: slash > 0 ? action.file.path.slice(0, slash) : "",
          file: action.file,
        },
      };
    }
    case "close-file":
      return { ...state, browser: { ...state.browser, file: null } };
    case "close-browser":
      // The chat must not stay folded behind a browser that is gone.
      return { ...state, browser: { ...state.browser, open: false }, chatCollapsed: false };
    case "toggle-browser-collapsed":
      return {
        ...state,
        browserCollapsed: !state.browserCollapsed,
        // Folding one pane unfolds the other: two header strips and nothing
        // else is a layout nobody asked for.
        chatCollapsed: state.browserCollapsed ? state.chatCollapsed : false,
      };
    case "toggle-chat-collapsed":
      return {
        ...state,
        chatCollapsed: !state.chatCollapsed,
        browserCollapsed: state.chatCollapsed ? state.browserCollapsed : false,
      };
    case "clear-view":
      // Back to the plain view: nothing selected, nothing focused, no answer
      // filter, centered on the knowledge base again. Deliberately keeps the
      // conversation and the type/source filters — it clears *selections*,
      // not your reading of the answer or the lens you chose.
      return {
        ...state,
        selectedId: null,
        focusIds: [],
        surfacedIds: [],
        centerId: null,
        depth: DEFAULT_DEPTH,
        showAll: false,
      };
    case "new-topic":
      return { ...state, contextFrom: state.turns.length };
    case "set-answer-depth":
      return { ...state, answerDepth: action.depth };
    case "new-conversation":
      // Drops the thread and everything derived from it. The canvas goes back
      // to its plain state too, because the graph filter came from an answer
      // that no longer exists.
      return {
        ...state,
        turns: [],
        contextFrom: 0,
        draft: "",
        answer: null,
        surfacedIds: [],
        focusIds: [],
        selectedId: null,
        centerId: null,
        showAll: false,
        panelTab: "graph",
      };
    default:
      return state;
  }
}

/** Open the browser, unfolded, at a folder. The open file stays if it is in the same source. */
function openBrowser(
  state: SessionState,
  source: string | null,
  path: string,
): Pick<SessionState, "browser" | "browserCollapsed"> {
  const file = state.browser.file && state.browser.file.source === source ? state.browser.file : null;
  return { browser: { open: true, source, path, file }, browserCollapsed: false };
}

/** The folder a `@taxonomy` answer listed, if this answer is one. */
export function browsedFrom(answer: ChatAnswer): { source: string | null; path: string } | null {
  const inventory = answer.inventory;
  if (!inventory || inventory.action !== "taxonomy") return null;
  const result = inventory.result as { source?: string | null; path?: string };
  return { source: result.source ?? null, path: result.path ?? "" };
}

/** Turns sent with a question: answered ones since the last "new topic". */
export const MAX_HISTORY_TURNS = 6;

export function historyFor(state: Pick<SessionState, "turns" | "contextFrom">): HistoryTurn[] {
  return state.turns
    .slice(state.contextFrom)
    .filter((turn) => turn.answer && !turn.error)
    .slice(-MAX_HISTORY_TURNS)
    .map((turn) => ({ question: turn.question, answer: turn.answer?.answer ?? "" }));
}

/** Citation targets first (ranked), then any extra focus nodes the agent named. */
function surfacedFrom(answer: ChatAnswer): string[] {
  const ordered = [
    ...answer.citations.map((citation) => citation.node_id).filter(Boolean),
    ...answer.focus_node_ids,
  ] as string[];
  return [...new Set(ordered)];
}

const STORAGE_KEY = "pheasant.workspace.v1";

/**
 * What survives a reload — view preferences only.
 *
 * The conversation is deliberately NOT in this list. Questions and answers
 * live in memory for as long as the workspace is open and no longer: closing
 * the app should not mean walking back into yesterday's thread. Nothing about
 * a question is written to storage, which also keeps whatever you typed out of
 * the browser's disk cache.
 */
const PERSISTED_KEYS = [
  "sourceFilter",
  "pathScope",
  "sourceTypeFilter",
  "hiddenTypes",
  "depth",
  "panelTab",
  "workflow",
  "answerDepth",
  "railWidth",
  "panelWidth",
  "railCollapsed",
  "panelCollapsed",
  "browser",
  "browserCollapsed",
  "chatCollapsed",
  "browserHeight",
  "browserTreeWidth",
  "excluded",
] as const;

function hydrate(): SessionState {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return INITIAL;
    const saved = JSON.parse(raw) as Partial<SessionState>;
    return {
      ...INITIAL,
      sourceFilter: saved.sourceFilter ?? INITIAL.sourceFilter,
      pathScope: hydrateScope(saved.pathScope, saved.sourceFilter ?? null),
      sourceTypeFilter: saved.sourceTypeFilter ?? INITIAL.sourceTypeFilter,
      hiddenTypes: Array.isArray(saved.hiddenTypes) ? saved.hiddenTypes : INITIAL.hiddenTypes,
      depth: clampDepth(saved.depth ?? DEFAULT_DEPTH),
      panelTab: saved.panelTab ?? INITIAL.panelTab,
      railCollapsed: saved.railCollapsed === true,
      panelCollapsed: saved.panelCollapsed === true,
      workflow: saved.workflow ?? INITIAL.workflow,
      answerDepth: saved.answerDepth ?? INITIAL.answerDepth,
      railWidth: Number(saved.railWidth) || DEFAULT_RAIL_WIDTH,
      panelWidth: Number(saved.panelWidth) || DEFAULT_PANEL_WIDTH,
      browser: hydrateBrowser(saved.browser),
      browserCollapsed: saved.browserCollapsed === true,
      chatCollapsed: saved.chatCollapsed === true && saved.browser?.open === true,
      browserHeight: Number(saved.browserHeight) || DEFAULT_BROWSER_HEIGHT,
      browserTreeWidth: Number(saved.browserTreeWidth) || DEFAULT_TREE_WIDTH,
      excluded: Array.isArray(saved.excluded)
        ? saved.excluded.filter((d): d is string => typeof d === "string" && d.includes("/")).slice(-MAX_EXCLUDED)
        : [],
    };
  } catch {
    return INITIAL;
  }
}

function hydrateScope(saved: unknown, source: string | null): PathScope | null {
  if (!saved || typeof saved !== "object") return null;
  const value = saved as Partial<PathScope>;
  if (typeof value.source !== "string" || typeof value.path !== "string" || !value.path) return null;
  if (value.source !== source) return null;
  return { source: value.source, path: value.path, kind: value.kind === "file" ? "file" : "folder" };
}

function hydrateBrowser(saved: unknown): BrowserState {
  if (!saved || typeof saved !== "object") return INITIAL.browser;
  const value = saved as Partial<BrowserState>;
  const file =
    value.file && typeof value.file.source === "string" && typeof value.file.path === "string"
      ? { source: value.file.source, path: value.file.path }
      : null;
  return {
    open: value.open === true,
    source: typeof value.source === "string" ? value.source : null,
    path: typeof value.path === "string" ? value.path : "",
    file,
  };
}

interface SessionContextValue {
  state: SessionState;
  dispatch: (action: SessionAction) => void;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(sessionReducer, undefined, hydrate);

  // Persist the view preferences only (see PERSISTED_KEYS). Graph slices are
  // re-fetched from the center + depth, which is cheap and keeps us clear of
  // the storage quota on a big graph; the conversation is never written.
  useEffect(() => {
    try {
      const persisted: Record<string, unknown> = {};
      for (const key of PERSISTED_KEYS) persisted[key] = state[key];
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(persisted));
    } catch {
      /* storage unavailable or over quota — in-memory state still works */
    }
  }, [state]);

  const value = useMemo(() => ({ state, dispatch }), [state]);
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const context = useContext(SessionContext);
  if (!context) throw new Error("useSession must be used inside <SessionProvider>");
  return context;
}
