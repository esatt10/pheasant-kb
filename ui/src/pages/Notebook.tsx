import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  DEFAULT_DEPTH,
  DEFAULT_PANEL_WIDTH,
  DEFAULT_RAIL_WIDTH,
  useSession,
} from "../state/session";

/**
 * The workspace: sources on the left, conversation in the middle, knowledge on
 * the right. Asking a question drives the graph — the canvas filters to the
 * nodes the answer surfaced — so the answer and the structure behind it stay
 * side by side.
 *
 * No state lives in this component. It reads the session store and dispatches
 * actions, which is what lets you walk to Sources or Settings and come back to
 * exactly the view you left.
 */
export function Notebook() {
  const [sessionId, setSessionId] = useSessionId();
  const { state, dispatch } = useSession();
  const [showModelDialog, setShowModelDialog] = useState(false);
  const [showWorkflowDialog, setShowWorkflowDialog] = useState(false);
  const layoutRef = useRef<HTMLDivElement>(null);

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
    if (
      state.sourceTypeFilter &&
      !overview.data.sources.some((s) => s.type === state.sourceTypeFilter)
    ) {
      dispatch({ type: "filter-source-type", sourceType: null });
    }
  }, [overview.data, state.sourceFilter, state.sourceTypeFilter, dispatch]);
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

  const hasContent = overview.data?.has_content ?? false;

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

  return (
    <div
      className={`notebook${state.railCollapsed ? " notebook--rail-collapsed" : ""}${
        state.panelCollapsed ? " notebook--panel-collapsed" : ""
      }`}
      ref={layoutRef}
      style={
        {
          "--rail": `${state.railWidth}px`,
          "--panel": `${state.panelWidth}px`,
        } as CSSProperties
      }
    >
      <aside className="pane pane--rail">
        {state.railCollapsed ? (
          <div className="rail-collapsed__body">
            <button
              className="btn btn--small btn--icon"
              onClick={() => dispatch({ type: "toggle-rail" })}
              title="Show sources"
              aria-label="Show sources"
            >
              ›
            </button>
            <span className="rail-collapsed__label">Sources</span>
          </div>
        ) : (
        <SourceRail
          onCollapse={() => dispatch({ type: "toggle-rail" })}
          sources={overview.data?.sources ?? []}
          selected={state.sourceFilter}
          selectedType={state.sourceTypeFilter}
          onSelect={(source) => dispatch({ type: "filter-source", source })}
          onSelectType={(sourceType) => dispatch({ type: "filter-source-type", sourceType })}
          onChanged={() => overview.refetch()}
        />
        )}
      </aside>

      {state.railCollapsed ? null : (
      <PaneResizer
        variable="--rail"
        edge="left"
        containerRef={layoutRef}
        min={200}
        max={560}
        defaultWidth={DEFAULT_RAIL_WIDTH}
        label="Resize sources"
        onCommit={(width) => dispatch({ type: "set-pane-width", pane: "rail", width })}
      />
      )}

      <section className="pane pane--main">
        <header className="pane__header">
          <span className="pane__title">Chat</span>
          <div className="pane__actions">
            {state.turns.length > 0 ? (
              <button
                className="btn btn--small"
                onClick={() => dispatch({ type: "new-conversation" })}
                title="Clear this thread and start fresh"
              >
                New conversation
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
              {status.data?.ready ? "Model connected" : "Connect model"}
            </button>
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

      {state.panelCollapsed ? null : (
      <PaneResizer
        variable="--panel"
        edge="right"
        containerRef={layoutRef}
        min={320}
        max={2400}
        defaultWidth={DEFAULT_PANEL_WIDTH}
        label="Resize graph panel"
        onCommit={(width) => dispatch({ type: "set-pane-width", pane: "panel", width })}
      />
      )}

      {state.panelCollapsed ? (
        <aside className="pane pane--panel">
          <div className="rail-collapsed__body">
            <button
              className="btn btn--small btn--icon"
              onClick={() => dispatch({ type: "toggle-panel" })}
              title="Show the graph"
              aria-label="Show graph panel"
            >
              ‹
            </button>
            <span className="rail-collapsed__label">Graph · Facts · Node</span>
          </div>
        </aside>
      ) : (
      <aside className="pane pane--panel">
        <header className="pane__header">
          <div className="tabs" style={{ flex: 1 }}>
            <button
              className={state.panelTab === "graph" ? "tab active" : "tab"}
              onClick={() => dispatch({ type: "open-tab", tab: "graph" })}
            >
              Graph
            </button>
            <button
              className={state.panelTab === "facts" ? "tab active" : "tab"}
              onClick={() => dispatch({ type: "open-tab", tab: "facts" })}
            >
              Facts{state.answer?.facts.length ? ` (${state.answer.facts.length})` : ""}
            </button>
            <button
              className={state.panelTab === "node" ? "tab active" : "tab"}
              onClick={() => dispatch({ type: "open-tab", tab: "node" })}
            >
              Node
            </button>
          </div>
          <button
            className="btn btn--small btn--icon"
            onClick={() => dispatch({ type: "toggle-panel" })}
            title="Collapse the graph panel to give the chat more room"
            aria-label="Collapse graph panel"
          >
            ›
          </button>
        </header>

        {state.panelTab === "graph" ? (
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
        ) : null}

        {state.panelTab === "facts" ? (
          <div className="pane__body pane__body--pad">
            <FactList facts={state.answer?.facts ?? []} onSelect={focusNode} />
          </div>
        ) : null}

        {state.panelTab === "node" ? (
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
            />
          </div>
        ) : null}
      </aside>
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
  );
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
