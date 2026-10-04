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

export interface ChatTurn {
  id: string;
  question: string;
  answer?: ChatAnswer;
  error?: string;
}

export interface SessionState {
  /** Restrict retrieval + graph to one source, or null for everything. */
  sourceFilter: string | null;
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
}

export const DEFAULT_RAIL_WIDTH = 280;
export const DEFAULT_PANEL_WIDTH = 400;

export const DEFAULT_DEPTH = 3;
export const MIN_DEPTH = 1;
export const MAX_DEPTH = 6;

const INITIAL: SessionState = {
  sourceFilter: null,
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
};

export type SessionAction =
  | { type: "filter-source"; source: string | null }
  | { type: "filter-source-type"; sourceType: string | null }
  | { type: "toggle-type"; nodeType: string }
  | { type: "open-tab"; tab: PanelTab }
  | { type: "select-node"; nodeId: string | null }
  | { type: "center-node"; nodeId: string }
  | { type: "set-depth"; depth: number }
  | { type: "show-all"; value: boolean }
  | { type: "set-layout"; layout: GraphLayout }
  | { type: "toggle-rail" }
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
  | { type: "set-pane-width"; pane: "rail" | "panel"; width: number };

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

function clampDepth(depth: number): number {
  if (!Number.isFinite(depth)) return DEFAULT_DEPTH;
  return Math.min(MAX_DEPTH, Math.max(MIN_DEPTH, Math.round(depth)));
}

export function sessionReducer(state: SessionState, action: SessionAction): SessionState {
  switch (action.type) {
    case "filter-source":
      return { ...state, sourceFilter: action.source };
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
      return {
        ...state,
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
    case "set-pane-width":
      return action.pane === "rail"
        ? { ...state, railWidth: Math.round(action.width) }
        : { ...state, panelWidth: Math.round(action.width) };
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
  "sourceTypeFilter",
  "hiddenTypes",
  "depth",
  "panelTab",
  "workflow",
  "answerDepth",
  "railWidth",
  "panelWidth",
] as const;

function hydrate(): SessionState {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return INITIAL;
    const saved = JSON.parse(raw) as Partial<SessionState>;
    return {
      ...INITIAL,
      sourceFilter: saved.sourceFilter ?? INITIAL.sourceFilter,
      sourceTypeFilter: saved.sourceTypeFilter ?? INITIAL.sourceTypeFilter,
      hiddenTypes: Array.isArray(saved.hiddenTypes) ? saved.hiddenTypes : INITIAL.hiddenTypes,
      depth: clampDepth(saved.depth ?? DEFAULT_DEPTH),
      panelTab: saved.panelTab ?? INITIAL.panelTab,
      workflow: saved.workflow ?? INITIAL.workflow,
      answerDepth: saved.answerDepth ?? INITIAL.answerDepth,
      railWidth: Number(saved.railWidth) || DEFAULT_RAIL_WIDTH,
      panelWidth: Number(saved.panelWidth) || DEFAULT_PANEL_WIDTH,
    };
  } catch {
    return INITIAL;
  }
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
