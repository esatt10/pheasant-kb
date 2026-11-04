import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { DragHandle } from "../browser/DragHandle";
import type { CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { GraphFact, GraphNode } from "../api/types";
import { ChatPanel } from "../chat/ChatPanel";
import { ModelDialog } from "../chat/ModelDialog";
import { GraphCanvas } from "../graph/GraphCanvas";
import { HorizonControls } from "../graph/HorizonControls";
import { NodeInspector } from "../graph/NodeInspector";
import { GraphLegend } from "../graph/GraphLegend";
import { useHorizonGraph } from "../graph/useHorizonGraph";
import { SourceRail } from "../components/SourceRail";
import { EmptyState } from "../components/EmptyState";
import { PaneResizer } from "../components/PaneResizer";
import { WorkflowPanel } from "../components/WorkflowPanel";
import { colorForNode } from "../graph/graphStyles";
import { useSessionId } from "../hooks/useSessionId";
import {
  TileColumnView,
  TileDock,
  TileDragProvider,
  TileFold,
  TileGrip,
  TileScope,
  TileSlot,
  isTileDrag,
  tileDragProps,
  useMediaQuery,
  useTileHosts,
} from "../components/Tiles";
import type { TileSpec } from "../components/Tiles";
import {
  DEFAULT_BROWSER_HEIGHT,
  DEFAULT_DEPTH,
  DEFAULT_INNER_WIDTH,
  DEFAULT_PANEL_WIDTH,
  DEFAULT_RAIL_WIDTH,
  PANEL_TAB_LABELS,
  isPanelTab,
  useSession,
} from "../state/session";
import type { BrowserState, PanelTab, TileColumn, TileId, TileLayout } from "../state/session";

/** A phone's Notebook sections; "panel" is the tab group, shown as Graph. */
type MobileTab = "chat" | "sources" | "taxonomy" | "panel";
/** The panel's tabs on a phone, where the taxonomy view has a section of its own. */
const PHONE_PANEL_TABS: PanelTab[] = ["graph", "facts", "node"];

/** Height of the first of two unfolded tiles stacked in a side column, before it is dragged. */
const DEFAULT_SIDE_STACK = 360;

// The browser and its renderers (syntax highlighting included) load the first
// time it opens, so a workspace that never browses never pays for them.
const SourceBrowser = lazy(() =>
  import("../browser/SourceBrowser").then((module) => ({ default: module.SourceBrowser })),
);

/**
 * The workspace: sources on the left, the taxonomy view beside the chat when
 * it is open, conversation in the middle, knowledge on the right — by default. Each section is a tile (`components/Tiles.tsx`)
 * that can be dragged to another column or another place in its own, and
 * folds to its header wherever it is; a reload puts every tile back. Asking a
 * question drives the graph — the canvas filters to the nodes the answer
 * surfaced — so the answer and the structure behind it stay side by side.
 *
 * No view state lives in this component (a drag in progress aside). It reads
 * the session store and dispatches actions, which is what lets you walk to
 * Sources or Settings and come back to exactly the view you left.
 */
export function Notebook() {
  const [sessionId, setSessionId] = useSessionId();
  const { state, dispatch } = useSession();
  const [showModelDialog, setShowModelDialog] = useState(false);
  const [showWorkflowDialog, setShowWorkflowDialog] = useState(false);
  const layoutRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);

  const overview = useQuery({
    queryKey: ["overview"],
    queryFn: api.overview,
    // Mirrors SourcesPage: a background sync (wait: false) reports its
    // progress here, so the Sources rail needs to keep polling while one is
    // running rather than only refreshing on the events this page already
    // triggers (asking a question, switching sources).
    refetchInterval: (query) => (query.state.data?.sources?.some((s) => s.syncing) ? 1500 : 5000),
  });
  useEffect(() => {
    if (!overview.data) return;
    if (state.sourceFilter && !overview.data.sources.some((s) => s.name === state.sourceFilter)) {
      dispatch({ type: "filter-source", source: null });
    }
    // A source removed since it was added to the selection leaves it.
    for (const name of state.alsoSources) {
      if (!overview.data.sources.some((s) => s.name === name)) {
        dispatch({ type: "toggle-source", source: name });
      }
    }
    if (
      state.sourceTypeFilter &&
      !overview.data.sources.some((s) => s.type === state.sourceTypeFilter)
    ) {
      dispatch({ type: "filter-source-type", sourceType: null });
    }
  }, [overview.data, state.sourceFilter, state.alsoSources, state.sourceTypeFilter, dispatch]);
  const status = useQuery({
    queryKey: ["assistant-status", sessionId],
    queryFn: () => api.assistantStatus(sessionId),
  });

  // The knowledge-base root is the natural center before you pick one: it is
  // the one node every source hangs off.
  const centerId = state.centerId ?? overview.data?.knowledge_base ?? null;

  const { graph, isLoading, isFetching, mode } = useHorizonGraph({
    centerId,
    depth: state.depth,
    showAll: state.showAll,
    surfacedIds: state.surfacedIds,
    hiddenTypes: state.hiddenTypes,
    sourceFilter: state.sourceFilter,
    selectedId: state.selectedId,
    enabled: overview.isSuccess,
  });

  const nodeById = useMemo(
    () => new Map(graph.nodes.map((node) => [node.id, node])),
    [graph.nodes],
  );

  const citedIds = useMemo(
    () => (state.answer?.citations ?? []).filter((c) => c.node_id).map((c) => c.node_id as string),
    [state.answer],
  );

  const centerLabel = useMemo(() => {
    if (!centerId) return null;
    return labelOf(nodeById.get(centerId), centerId);
  }, [centerId, nodeById]);

  /**
   * Focus a node that may not be inside the current horizon — a citation can
   * point at a chunk while chunks are hidden, or at a node several layers out.
   * Re-centering there is the action that brings it into view.
   */
  const focusNode = useCallback(
    (nodeId: string | undefined) => {
      if (!nodeId) return;
      dispatch({ type: "open-tab", tab: "node" });
      if (nodeById.has(nodeId)) dispatch({ type: "select-node", nodeId });
      else dispatch({ type: "center-node", nodeId });
    },
    [dispatch, nodeById],
  );

  // Narrow windows fold every tile into the middle column (below); a phone
  // shows one of them at a time, chosen by a tab strip. Stacked on a phone,
  // two open tiles left the chat its header and nothing else.
  const narrow = useMediaQuery("(max-width: 1180px)");
  const compact = useMediaQuery("(max-width: 860px)");
  const [mobileTab, setMobileTab] = useState<MobileTab>("chat");

  /** Put text in the composer (unfolding the chat if it was folded) and focus it. */
  const askInChat = useCallback(
    (text: string) => {
      setMobileTab("chat");
      if (state.chatCollapsed) dispatch({ type: "toggle-chat-collapsed" });
      dispatch({ type: "set-draft", text: `${text} ` });
      window.setTimeout(() => document.querySelector<HTMLTextAreaElement>(".composer textarea")?.focus(), 0);
    },
    [dispatch, state.chatCollapsed],
  );

  const showInGraph = useCallback(
    (nodeId: string) => {
      setMobileTab("panel");
      dispatch({ type: "center-node", nodeId });
      // Unfolds the graph wherever it is: in the panel, or a tile of its own.
      dispatch({ type: "open-tab", tab: "graph" });
    },
    [dispatch],
  );

  const hasContent = overview.data?.has_content ?? false;
  const browserOpen = state.browser.open;

  const hosts = useTileHosts();
  const [dragging, setDragging] = useState<TileId | null>(null);
  // A panel tab dragged over the panel's tab bar, and the tab it would land before.
  const [dockDrop, setDockDrop] = useState<{ before: PanelTab | null } | null>(null);
  // A tablet stacks every tile in the middle column rather than hiding it
  // wherever it was dragged, and keeps the graph on the right. The sources
  // rail heads the stack, folded until asked for: it used to be dropped
  // outright below 1180px, so nothing narrower could scope a question to a
  // source or add one. Its fold state is its own, so opening it there does
  // not change the desktop rail's remembered state.
  const [narrowRailOpen, setNarrowRailOpen] = useState(false);
  const layout = useMemo<TileLayout>(() => {
    if (compact) return { left: [], inner: [], center: [mobileTab], right: [] };
    if (!narrow) return state.tiles;
    const left = state.tiles.left.filter((tile) => tile !== "sources");
    return {
      left: [],
      inner: [],
      center: ["sources", ...left, ...state.tiles.inner, ...state.tiles.center],
      right: state.tiles.right,
    };
  }, [state.tiles, narrow, compact, mobileTab]);
  // On a phone the one tile shown is never folded: its tab is the fold.
  const railCollapsed = compact ? false : narrow ? !narrowRailOpen : state.railCollapsed;

  // Opening the taxonomy view anywhere (a citation, "browse" on a source, the
  // chat's Taxonomy button) shows it on a phone; closing it goes back to chat.
  const browserKey = `${browserOpen}|${state.browser.source}|${state.browser.path}|${state.browser.file?.path ?? ""}`;
  const lastBrowserKey = useRef(browserKey);
  useEffect(() => {
    if (lastBrowserKey.current === browserKey) return;
    lastBrowserKey.current = browserKey;
    setMobileTab((tab) => (browserOpen ? "taxonomy" : tab === "taxonomy" ? "chat" : tab));
  }, [browserKey, browserOpen]);

  if (overview.isSuccess && !hasContent) {
    return (
      <EmptyState
        overview={overview.data}
        onAdded={() => {
          overview.refetch();
        }}
      />
    );
  }

  // The panel is a tab group. On a desktop it holds whichever tabs are
  // docked; a phone gives the taxonomy view a section of its own and keeps
  // the rest together, wherever they were on a wider window.
  const groupTabs: PanelTab[] = compact ? PHONE_PANEL_TABS : state.docked;
  const isDocked = (tab: PanelTab) => groupTabs.includes(tab);
  const activeTab: PanelTab | undefined =
    groupTabs.includes(state.panelTab) && (state.panelTab !== "taxonomy" || browserOpen)
      ? state.panelTab
      : (groupTabs.find((tab) => tab !== "taxonomy" || browserOpen) ?? groupTabs[0]);
  const dock = (tab: PanelTab) => (compact || isDocked(tab) ? undefined : () => dispatch({ type: "dock-tab", tab }));
  /** A panel tab as a tile of its own: graph, facts or node. */
  const tabSpec = (tab: "graph" | "facts" | "node"): TileSpec => ({
    id: tab,
    title: tab === "facts" && state.answer?.facts.length ? `Facts (${state.answer.facts.length})` : PANEL_TAB_LABELS[tab],
    visible: true,
    collapsed: compact ? false : Boolean(state.tabCollapsed[tab]),
    onToggle: () => dispatch({ type: "toggle-tab-collapsed", tab }),
    host: hosts[tab],
    onDock: dock(tab),
  });

  const specs: Record<TileId, TileSpec> = {
    sources: {
      id: "sources",
      title: "Sources",
      visible: true,
      collapsed: railCollapsed,
      onToggle: () =>
        narrow ? setNarrowRailOpen((open) => !open) : dispatch({ type: "toggle-rail" }),
      host: hosts.sources,
      subtitle: state.sourceFilter ?? undefined,
    },
    taxonomy: {
      id: "taxonomy",
      title: "Taxonomy",
      visible: browserOpen,
      collapsed: compact ? false : state.browserCollapsed,
      onToggle: () => dispatch({ type: "toggle-browser-collapsed" }),
      host: hosts.taxonomy,
      onDock: dock("taxonomy"),
      subtitle: browserLocation(state.browser),
      stripActions: (
        <button
          type="button"
          className="btn btn--small btn--icon btn--ghost"
          onClick={() => dispatch({ type: "close-browser" })}
          aria-label="Close the source browser"
          title="Close"
        >
          ✕
        </button>
      ),
    },
    chat: {
      id: "chat",
      title: "Chat",
      visible: true,
      collapsed: compact ? false : state.chatCollapsed,
      onToggle: () => dispatch({ type: "toggle-chat-collapsed" }),
      host: hosts.chat,
    },
    panel: {
      id: "panel",
      title: groupTabs.map((tab) => PANEL_TAB_LABELS[tab]).join(" · "),
      // With every tab pulled out there is nothing left to hold.
      visible: groupTabs.length > 0,
      collapsed: compact ? false : state.panelCollapsed,
      onToggle: () => dispatch({ type: "toggle-panel" }),
      host: hosts.panel,
    },
    graph: tabSpec("graph"),
    facts: tabSpec("facts"),
    node: tabSpec("node"),
  };
  const visibleIds = (column: TileColumn) => layout[column].filter((id) => specs[id].visible);
  const columns = {
    left: visibleIds("left"),
    inner: visibleIds("inner"),
    center: visibleIds("center"),
    right: visibleIds("right"),
  };
  const sideOpen = (column: TileColumn) => columns[column].some((id) => !specs[id].collapsed);
  // A tile no column holds (a phone shows one at a time) has no slot to hold
  // its host, so its content must not be rendered: portalled into a detached
  // host, the graph's Cytoscape measured NaN, threw, and blanked the whole page.
  const placed = new Set<TileId>([...layout.left, ...layout.inner, ...layout.center, ...layout.right]);
  /** Whether a panel tab's content is on screen: the panel's open tab, or an unfolded tile. */
  const tabShown = (tab: PanelTab) =>
    isDocked(tab)
      ? placed.has("panel") && !specs.panel.collapsed && activeTab === tab
      : placed.has(tab) && !specs[tab].collapsed && specs[tab].visible;
  const selectTab = (tab: PanelTab) =>
    tab === "taxonomy" && !browserOpen
      ? dispatch({ type: "open-browser", source: state.sourceFilter ?? null, path: "" })
      : dispatch({ type: "open-tab", tab });
  const dragValue = {
    dragging,
    setDragging,
    move: (tile: TileId, column: TileColumn, before: TileId | null) =>
      dispatch({ type: "move-tile", tile, column, before }),
    reset: () => dispatch({ type: "reset-tiles" }),
    columns,
  };

  /** What the graph, facts and node tabs show, in the panel or as tiles. */
  const tabBody = (tab: "graph" | "facts" | "node") => {
    if (tab === "graph")
      return (
        <>
            <div className="graph-pane__body">
              <div className="graph-overlay">
                <HorizonControls
                  mode={mode}
                  depth={state.depth}
                  centerLabel={centerLabel}
                  surfacedCount={state.surfacedIds.length}
                  nodeCount={graph.nodes.length}
                  truncated={Boolean(graph.truncated)}
                  busy={isFetching}
                  dirty={
                    state.selectedId !== null ||
                    state.centerId !== null ||
                    state.surfacedIds.length > 0 ||
                    state.showAll ||
                    state.depth !== DEFAULT_DEPTH
                  }
                  layout={state.layout}
                  onLayout={(layout) => dispatch({ type: "set-layout", layout })}
                  onDepth={(depth) => dispatch({ type: "set-depth", depth })}
                  onShowAll={(value) => dispatch({ type: "show-all", value })}
                  onClearAnswerFilter={() => dispatch({ type: "clear-answer-filter" })}
                  onClear={() => dispatch({ type: "clear-view" })}
                />
              </div>
              {isLoading ? (
                <div className="centered muted">
                  <span className="spinner" />
                </div>
              ) : graph.nodes.length === 0 ? (
                <div className="centered muted small">
                  Nothing within {state.depth} layers of here. Widen the depth, or show all.
                </div>
              ) : (
                <GraphCanvas
                  nodes={graph.nodes}
                  links={graph.links}
                  depths={graph.depths}
                  selectedId={state.selectedId}
                  focusIds={state.focusIds}
                  citedIds={citedIds}
                  layoutName={state.layout}
                  spacing={1}
                  shapeAlgorithm="node_type"
                  onSelect={(id) => {
                    dispatch({ type: "select-node", nodeId: id });
                    // Only jump to the Node tab when there is a node to show;
                    // a background tap is a deselect and should leave the
                    // graph exactly as it is.
                    if (id) dispatch({ type: "open-tab", tab: "node" });
                  }}
                  onRecenter={(id) => dispatch({ type: "center-node", nodeId: id })}
                />
              )}
            </div>
            <GraphLegend
              counts={overview.data?.node_counts ?? {}}
              hidden={state.hiddenTypes}
              onToggle={(nodeType) => dispatch({ type: "toggle-type", nodeType })}
            />
        </>
      );
    if (tab === "facts")
      return (
          <div className="pane__body pane__body--pad">
            <FactList facts={state.answer?.facts ?? []} onSelect={focusNode} />
          </div>
      );
    return (
          <div className="pane__body">
            <NodeInspector
              selectedId={state.selectedId}
              nodes={graph.nodes}
              links={graph.links}
              onExpand={(nodeId) => dispatch({ type: "center-node", nodeId })}
              onShowChunks={(nodeId) => {
                if (state.hiddenTypes.includes("chunk")) {
                  dispatch({ type: "toggle-type", nodeType: "chunk" });
                }
                dispatch({ type: "center-node", nodeId });
                dispatch({ type: "open-tab", tab: "graph" });
              }}
              onPivot={focusNode}
              onOpenFile={(source, path) => dispatch({ type: "open-file", file: { source, path } })}
            />
          </div>
    );
  };
  /** A side column's track: its width, a spine, a drop strip while dragging, or nothing. */
  const sideTrack = (column: TileColumn, width: string) =>
    columns[column].length === 0
      ? dragging && !narrow
        ? "44px"
        : "0px"
      : sideOpen(column)
        ? width
        : "44px";
  const stackHeight = (column: TileColumn) =>
    column === "center" ? state.browserHeight : (state.stackHeights[column] ?? DEFAULT_SIDE_STACK);
  const columnView = (column: TileColumn) => (
    <TileColumnView
      column={column}
      tiles={layout[column].map((id) => specs[id])}
      stackHeight={stackHeight(column)}
      defaultStackHeight={column === "center" ? DEFAULT_BROWSER_HEIGHT : DEFAULT_SIDE_STACK}
      onStackHeight={(height) =>
        column === "center"
          ? dispatch({ type: "set-pane-width", pane: "browser", width: height })
          : dispatch({ type: "set-stack-height", column, height })
      }
      spine={column !== "center" && columns[column].length > 0 && !sideOpen(column)}
      columnRef={column === "inner" ? innerRef : undefined}
    />
  );

  return (
    <TileDragProvider value={dragValue}>
    <div
      className={`notebook${dragging ? " notebook--dragging" : ""}${compact ? " notebook--tabs" : ""}`}
      ref={layoutRef}
      style={
        {
          "--rail": `${state.railWidth}px`,
          "--panel": `${state.panelWidth}px`,
          "--inner": `${state.innerWidth}px`,
          "--nb-left": sideTrack("left", "var(--rail)"),
          "--nb-lgrip": sideOpen("left") ? "8px" : "0px",
          "--nb-inner": sideTrack("inner", "var(--inner)"),
          "--nb-igrip": sideOpen("inner") ? "8px" : "0px",
          "--nb-rgrip": sideOpen("right") ? "8px" : "0px",
          "--nb-right": sideTrack("right", "var(--panel)"),
        } as CSSProperties
      }
    >
      {compact ? (
        <nav className="nb-tabs" role="tablist" aria-label="Notebook sections">
          {(
            [
              ["chat", "Chat"],
              ["sources", "Sources"],
              ["taxonomy", "Taxonomy"],
              ["panel", "Graph"],
            ] as const
          ).map(([id, label]) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={mobileTab === id}
              className={`nb-tabs__tab${mobileTab === id ? " nb-tabs__tab--active" : ""}`}
              onClick={() => {
                // The taxonomy tab is the view itself; opening it is the tap.
                if (id === "taxonomy" && !browserOpen) {
                  dispatch({ type: "open-browser", source: state.sourceFilter ?? null, path: "" });
                }
                setMobileTab(id);
              }}
            >
              {label}
              {id === "sources" && (state.sourceFilter || state.alsoSources.length) ? (
                <span className="nb-tabs__dot" aria-label="(filtered)" />
              ) : null}
            </button>
          ))}
        </nav>
      ) : null}
      {columnView("left")}
      {sideOpen("left") ? (
        <PaneResizer
          variable="--rail"
          edge="left"
          containerRef={layoutRef}
          min={200}
          max={1200}
          defaultWidth={DEFAULT_RAIL_WIDTH}
          label="Resize the left column"
          onCommit={(width) => dispatch({ type: "set-pane-width", pane: "rail", width })}
        />
      ) : null}
      {columnView("inner")}
      {sideOpen("inner") ? (
        <DragHandle
          axis="x"
          variable="--inner"
          containerRef={layoutRef}
          startRef={innerRef}
          min={280}
          max={(box) => box.width - 360}
          defaultSize={DEFAULT_INNER_WIDTH}
          label="Resize the column beside the chat"
          onCommit={(width) => dispatch({ type: "set-pane-width", pane: "inner", width })}
        />
      ) : null}
      {columnView("center")}
      {sideOpen("right") ? (
        <PaneResizer
          variable="--panel"
          edge="right"
          containerRef={layoutRef}
          min={320}
          max={2400}
          defaultWidth={DEFAULT_PANEL_WIDTH}
          label="Resize the right column"
          onCommit={(width) => dispatch({ type: "set-pane-width", pane: "panel", width })}
        />
      ) : null}
      {columnView("right")}

      {/* Each tile's content, rendered once and portalled into the host its
          tile shows (`components/Tiles.tsx`), so moving a tile never remounts
          it. A folded tile's content is dropped, except the chat's: an answer
          being written must still land in the thread when it is unfolded. So
          is the content of a tile the layout does not place. */}
      {!railCollapsed && placed.has("sources")
        ? createPortal(
            <TileScope spec={specs.sources}>
              <aside className="pane pane--rail">
                <SourceRail
                  sources={overview.data?.sources ?? []}
                  selected={state.sourceFilter}
                  selectedType={state.sourceTypeFilter}
                  onSelect={(source) => {
                    dispatch({ type: "filter-source", source });
                    // Stacked above the chat, an open rail is what the chat
                    // gave its height to: picking a source hands it back (the
                    // folded header still names the selection).
                    if (compact) setMobileTab("chat");
                    else if (narrow) setNarrowRailOpen(false);
                  }}
                  alsoSelected={state.alsoSources}
                  onToggle={(source) => dispatch({ type: "toggle-source", source })}
                  onSelectType={(sourceType) => dispatch({ type: "filter-source-type", sourceType })}
                  onChanged={() => overview.refetch()}
                  onBrowse={(source) => dispatch({ type: "open-browser", source, path: "" })}
                />
              </aside>
            </TileScope>,
            hosts.sources,
          )
        : null}

      {browserOpen && (compact ? placed.has("taxonomy") && !specs.taxonomy.collapsed : tabShown("taxonomy"))
        ? createPortal(
            <TileScope spec={specs.taxonomy}>
              <Suspense
                fallback={
                  <section className="pane browser centered muted small">
                    <span className="spinner" />
                  </section>
                }
              >
                <SourceBrowser onAsk={askInChat} onShowInGraph={showInGraph} />
              </Suspense>
            </TileScope>,
            hosts.taxonomy,
          )
        : null}

      {createPortal(
        <TileScope spec={specs.chat}>
          <section className="pane pane--main">
            <header className="pane__header">
              <TileGrip />
              <span className="pane__title">Chat</span>
              <div className="pane__actions">
                {/* On a phone the Taxonomy tab is this button. */}
                {browserOpen || compact ? null : (
                  <button
                    className="btn btn--small"
                    onClick={() => dispatch({ type: "open-browser", source: state.sourceFilter, path: "" })}
                    title="Browse a source's folders and files (or type @taxonomy)"
                  >
                    Taxonomy
                  </button>
                )}
                {state.turns.length > 0 ? (
                  <button
                    className="btn btn--small"
                    onClick={() => dispatch({ type: "new-conversation" })}
                    title="Clear this thread and start fresh"
                  >
                    {compact ? "New chat" : "New conversation"}
                  </button>
                ) : null}
                <button
                  className="btn btn--small"
                  onClick={() => setShowWorkflowDialog(true)}
                  title="Choose how questions are answered"
                >
                  {state.workflow ?? "Workflow"}
                </button>
                <button
                  className="btn btn--small"
                  onClick={() => setShowModelDialog(true)}
                  title="Connect an LLM to synthesize answers"
                >
                  {/* Short on a phone, where three of these share 375px. */}
                  {status.data?.ready ? (compact ? "Model ✓" : "Model connected") : compact ? "Model" : "Connect model"}
                </button>
                <TileFold />
              </div>
            </header>
            <div className="pane__body" style={{ overflow: "hidden" }}>
              <ChatPanel
                status={status.data}
                sessionId={sessionId}
                hasContent={hasContent}
                onCitationClick={focusNode}
                onConnectModel={() => setShowModelDialog(true)}
              />
            </div>
          </section>
        </TileScope>,
        hosts.chat,
      )}

      {/* The panel: a tab group. Each docked tab's content is portalled into
          its own host (below), and the panel shows the open tab's host in its
          body -- so a tab pulled out into a tile of its own is the same
          content moved, not a second copy. Dragging a tab off the bar makes
          it a tile where it lands; dragging a tile onto the bar docks it. */}
      {placed.has("panel") && !specs.panel.collapsed && activeTab
        ? createPortal(
            <TileScope spec={specs.panel}>
              <aside className="pane pane--panel">
                <header
                  className={`pane__header panel__bar${dockDrop ? " panel__bar--drop" : ""}`}
                  data-dock-target=""
                  onDragOver={(event) => {
                    if (compact || !isTileDrag(event) || !isPanelTab(dragging)) return;
                    event.preventDefault();
                    event.dataTransfer.dropEffect = "move";
                    const over = (event.target as HTMLElement).closest<HTMLElement>("[data-panel-tab]");
                    const before = (over?.dataset.panelTab as PanelTab | undefined) ?? null;
                    setDockDrop((current) => (current && current.before === before ? current : { before }));
                  }}
                  onDragLeave={(event) => {
                    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDockDrop(null);
                  }}
                  onDrop={(event) => {
                    setDockDrop(null);
                    if (!isPanelTab(dragging)) return;
                    event.preventDefault();
                    const before = dockDrop?.before;
                    dispatch({ type: "dock-tab", tab: dragging, before: before === dragging ? null : before });
                    setDragging(null);
                  }}
                >
                  <TileGrip />
                  <div className="tabs panel__tabs" role="tablist" aria-label="Panel">
                    {groupTabs.map((tab) => (
                      <div
                        key={tab}
                        role="tab"
                        tabIndex={0}
                        aria-selected={activeTab === tab}
                        data-panel-tab={tab}
                        className={`tab${activeTab === tab ? " active" : ""}${
                          dockDrop?.before === tab && dragging !== tab ? " tab--drop-before" : ""
                        }`}
                        title={compact ? undefined : `${PANEL_TAB_LABELS[tab]} · drag to any column to open it as its own tile`}
                        {...(compact ? {} : tileDragProps(tab, dragValue))}
                        onClick={() => selectTab(tab)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            selectTab(tab);
                          }
                        }}
                      >
                        {PANEL_TAB_LABELS[tab]}
                        {tab === "facts" && state.answer?.facts.length ? ` (${state.answer.facts.length})` : ""}
                      </div>
                    ))}
                  </div>
                  {compact ? null : (
                    <button
                      type="button"
                      className="btn btn--small btn--icon btn--ghost panel__popout"
                      onClick={() => dispatch({ type: "undock-tab", tab: activeTab })}
                      aria-label={`Open ${PANEL_TAB_LABELS[activeTab]} as its own tile`}
                      title={`Open ${PANEL_TAB_LABELS[activeTab]} as its own tile (or drag its tab to any column)`}
                    >
                      ⧉
                    </button>
                  )}
                  <TileFold />
                </header>
                <TileSlot host={hosts[activeTab]} className="panel__slot" />
              </aside>
            </TileScope>,
            hosts.panel,
          )
        : null}

      {(["graph", "facts", "node"] as const).map((tab) =>
        tabShown(tab)
          ? createPortal(
              <TileScope key={tab} spec={specs[tab]}>
                {isDocked(tab) ? (
                  <div className="panel-tab">{tabBody(tab)}</div>
                ) : (
                  <section className="pane pane--panel">
                    <header className="pane__header">
                      <TileGrip />
                      <span className="pane__title">{specs[tab].title}</span>
                      <div className="pane__actions">
                        <TileDock />
                        <TileFold />
                      </div>
                    </header>
                    {tabBody(tab)}
                  </section>
                )}
              </TileScope>,
              hosts[tab],
              tab,
            )
          : null,
      )}

      {showWorkflowDialog ? (
        <div className="modal-scrim" onClick={() => setShowWorkflowDialog(false)}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <header className="modal__header">
              <h2>How questions are answered</h2>
              <button
                className="btn btn--ghost btn--icon"
                onClick={() => setShowWorkflowDialog(false)}
                aria-label="Close"
              >
                ✕
              </button>
            </header>
            <WorkflowPanel
              selected={state.workflow}
              onSelect={(workflow) => dispatch({ type: "set-workflow", workflow })}
            />
            <div className="modal__footer">
              <button className="btn btn--primary" onClick={() => setShowWorkflowDialog(false)}>
                Done
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {showModelDialog ? (
        <ModelDialog
          status={status.data}
          sessionId={sessionId}
          onSession={(id) => {
            setSessionId(id);
            status.refetch();
          }}
          onClose={() => setShowModelDialog(false)}
        />
      ) : null}
    </div>
    </TileDragProvider>
  );
}

/** Where the taxonomy view is, for its folded strip. */
function browserLocation(browser: BrowserState): string {
  const where = browser.source ? [browser.source, ...browser.path.split("/").filter(Boolean)].join(" / ") : "All sources";
  return browser.file ? `${where} · ${browser.file.path.split("/").pop()}` : where;
}

function labelOf(node: GraphNode | undefined, fallback: string): string {
  const label = node?.label ?? fallback;
  return label.length > 40 ? `…${label.slice(label.length - 39)}` : label;
}

/**
 * Surfaced facts: subject–predicate–object triples read straight off the graph
 * around the passages an answer used. Both endpoints are clickable, so a fact
 * is a navigation handle rather than a static readout.
 */
function FactList({
  facts,
  onSelect,
}: {
  facts: GraphFact[];
  onSelect: (nodeId: string) => void;
}) {
  if (facts.length === 0) {
    return (
      <p className="muted small">
        Ask a question and the relationships pheasant found around the cited passages
        show up here.
      </p>
    );
  }
  return (
    <div className="fact-list">
      {facts.map((fact, index) => (
        <div
          className="fact"
          key={`${fact.subject_id}-${fact.edge_type}-${fact.object_id}-${index}`}
        >
          <span
            className="source-dot"
            style={{ background: colorForNode(fact.object_type), borderRadius: "50%" }}
          />
          <button
            className="fact__object"
            onClick={() => onSelect(fact.object_id)}
            title={`Show ${fact.object} on the graph`}
          >
            {fact.object}
          </button>
          <span className="fact__from">
            <span className="fact__predicate">{fact.predicate_passive ?? fact.predicate}</span>
            <button
              className="fact__subject"
              onClick={() => onSelect(fact.subject_id)}
              title={fact.subject}
            >
              {fact.subject}
            </button>
          </span>
        </div>
      ))}
    </div>
  );
}
