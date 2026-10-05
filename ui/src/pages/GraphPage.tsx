import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { GraphHub, GraphNode } from "../api/types";
import { GraphCanvas } from "../graph/GraphCanvas";
import { GraphLegend } from "../graph/GraphLegend";
import { HorizonControls } from "../graph/HorizonControls";
import { NodeInspector } from "../graph/NodeInspector";
import { useHorizonGraph } from "../graph/useHorizonGraph";
import { colorForNode } from "../graph/graphStyles";
import { DEFAULT_DEPTH, useSession } from "../state/session";

/**
 * The graph at full width, with the diagnostics the Notebook's side panel has
 * no room for.
 *
 * The canvas in the Notebook is deliberately small — it sits beside a
 * conversation and answers "what did this answer touch". This page answers a
 * different set of questions, the ones a picture alone cannot:
 *
 *  - *What is this graph made of?* — node and edge type histograms.
 *  - *What is load-bearing?* — hubs ranked by degree; clicking one centres it.
 *  - *What is wrong with it?* — orphan count. On a healthy index this is near
 *    zero; a large number means enrichment did not run or a source indexed
 *    content nothing links to.
 *  - *How are these two things related?* — a shortest-path finder, which is
 *    the one question a canvas structurally cannot answer, because two nodes
 *    six hops apart are never on screen together.
 *
 * It shares the session store with the Notebook, so the centre, depth and
 * hidden types you set here are the ones you come back to there.
 */
export function GraphPage() {
  const { state, dispatch } = useSession();
  const [tab, setTab] = useState<"overview" | "path">("overview");
  // The side panel folds to a spine so the canvas can take the whole width;
  // remembered for the tab, like the Notebook's panes.
  const [sideCollapsed, setSideCollapsed] = useState<boolean>(() => {
    try {
      return sessionStorage.getItem("pheasant.graph.sideCollapsed") === "1";
    } catch {
      return false;
    }
  });
  const toggleSide = () =>
    setSideCollapsed((value) => {
      try {
        sessionStorage.setItem("pheasant.graph.sideCollapsed", value ? "0" : "1");
      } catch {
        /* storage unavailable */
      }
      return !value;
    });
  const [pathFrom, setPathFrom] = useState("");
  const [pathTo, setPathTo] = useState("");
  const [pathQuery, setPathQuery] = useState<{ from: string; to: string } | null>(null);

  const overview = useQuery({ queryKey: ["overview"], queryFn: api.overview });
  const diagnostics = useQuery({
    queryKey: ["graph-diagnostics"],
    queryFn: () => api.graphDiagnostics(20),
    // This walks the whole graph, so it is not something to poll. One read
    // per visit, refreshed on demand.
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

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

  const path = useQuery({
    queryKey: ["graph-path", pathQuery?.from, pathQuery?.to],
    queryFn: () => api.graphPath(pathQuery!.from, pathQuery!.to),
    enabled: pathQuery !== null,
    retry: false,
  });

  const centerLabel = centerId ? (nodeById.get(centerId)?.label ?? centerId) : null;

  return (
    <div className={`graph-page${sideCollapsed ? " graph-page--side-collapsed" : ""}`}>
      <section className="graph-page__canvas">
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
            citedIds={[]}
            layoutName={state.layout}
            spacing={1}
            shapeAlgorithm="node_type"
            onSelect={(id) => dispatch({ type: "select-node", nodeId: id })}
            onRecenter={(id) => dispatch({ type: "center-node", nodeId: id })}
          />
        )}
        <GraphLegend
          counts={overview.data?.node_counts ?? {}}
          hidden={state.hiddenTypes}
          onToggle={(nodeType) => dispatch({ type: "toggle-type", nodeType })}
        />
      </section>

      {sideCollapsed ? (
        <aside className="graph-page__side">
          <div className="rail-collapsed__body">
            <button
              className="btn btn--small btn--icon"
              onClick={toggleSide}
              title="Show diagnostics and path"
              aria-label="Show graph side panel"
            >
              ‹
            </button>
            <span className="rail-collapsed__label">Diagnostics · Path</span>
          </div>
        </aside>
      ) : (
      <aside className="graph-page__side">
        <div className="tabs graph-page__tabs">
          <button
            className={tab === "overview" ? "tab active" : "tab"}
            onClick={() => setTab("overview")}
          >
            Diagnostics
          </button>
          <button className={tab === "path" ? "tab active" : "tab"} onClick={() => setTab("path")}>
            Path
          </button>
          <button
            className="btn btn--small btn--icon"
            onClick={toggleSide}
            title="Collapse the side panel to give the graph the whole width"
            aria-label="Collapse graph side panel"
          >
            ›
          </button>
        </div>

        {tab === "overview" ? (
          <div className="pane__body pane__body--pad">
            {diagnostics.isLoading ? (
              <p className="muted small">
                <span className="spinner" /> Walking the graph…
              </p>
            ) : diagnostics.isError ? (
              <div className="banner banner--error">{(diagnostics.error as Error).message}</div>
            ) : diagnostics.data ? (
              <>
                <div className="stat-row">
                  <Stat label="Nodes" value={diagnostics.data.total_nodes.toLocaleString()} />
                  <Stat label="Edges" value={diagnostics.data.total_links.toLocaleString()} />
                  <Stat label="Edges/node" value={String(diagnostics.data.density)} />
                </div>

                {diagnostics.data.orphan_count > 0 ? (
                  <div className="banner banner--warn">
                    {diagnostics.data.orphan_count.toLocaleString()} node
                    {diagnostics.data.orphan_count === 1 ? " has" : "s have"} no edges at
                    all. A few is normal; a large share usually means enrichment has
                    not run over a source yet.
                  </div>
                ) : null}

                <h3 className="side__heading">Hubs</h3>
                <p className="muted small">
                  Most-connected nodes. These are what a traversal will pass through;
                  click one to centre the canvas on it.
                </p>
                <div className="hub-list">
                  {diagnostics.data.hubs.map((hub) => (
                    <HubRow
                      key={hub.node_id}
                      hub={hub}
                      onSelect={() => dispatch({ type: "center-node", nodeId: hub.node_id })}
                    />
                  ))}
                </div>

                <h3 className="side__heading">Relationships</h3>
                <Histogram counts={diagnostics.data.edge_types} />

                <h3 className="side__heading">Node types</h3>
                <Histogram counts={diagnostics.data.node_types} colorize />

                <button
                  className="btn btn--small"
                  onClick={() => diagnostics.refetch()}
                  disabled={diagnostics.isFetching}
                >
                  {diagnostics.isFetching ? "Refreshing…" : "Refresh"}
                </button>
              </>
            ) : null}
          </div>
        ) : null}

        {tab === "path" ? (
          <div className="pane__body pane__body--pad">
            <p className="muted small">
              How are two things connected? Edges are followed in both directions —
              relatedness is not a question about which way an import happens to
              point.
            </p>
            <label className="field">
              <span>From node id</span>
              <input
                className="text-input"
                spellCheck={false}
                value={pathFrom}
                onChange={(event) => setPathFrom(event.target.value)}
                placeholder={state.selectedId ?? "file:docs:README.md:branch=main"}
              />
            </label>
            <button
              className="btn btn--small"
              disabled={!state.selectedId}
              onClick={() => setPathFrom(state.selectedId ?? "")}
            >
              Use selected node
            </button>
            <label className="field">
              <span>To node id</span>
              <input
                className="text-input"
                spellCheck={false}
                value={pathTo}
                onChange={(event) => setPathTo(event.target.value)}
              />
            </label>
            <button
              className="btn btn--primary btn--small"
              disabled={!pathFrom.trim() || !pathTo.trim()}
              onClick={() => setPathQuery({ from: pathFrom.trim(), to: pathTo.trim() })}
            >
              Find path
            </button>

            {path.isFetching ? <p className="muted small">Searching…</p> : null}
            {path.isError ? (
              <div className="banner banner--error">{(path.error as Error).message}</div>
            ) : null}
            {path.data && !path.data.found ? (
              <div className="banner banner--warn">
                No path within 8 hops. They may be in unconnected parts of the graph.
              </div>
            ) : null}
            {path.data?.found ? (
              <>
                <p className="small">
                  {path.data.hops} hop{path.data.hops === 1 ? "" : "s"}:
                </p>
                <ol className="path-list">
                  {path.data.path.map((node) => (
                    <li key={node.node_id}>
                      <button
                        className="fact__object"
                        onClick={() =>
                          dispatch({ type: "center-node", nodeId: node.node_id })
                        }
                        title={node.node_id}
                      >
                        <span
                          className="source-dot"
                          style={{
                            background: colorForNode(node.type),
                            borderRadius: "50%",
                          }}
                        />
                        {node.label ?? node.node_id}
                      </button>
                    </li>
                  ))}
                </ol>
              </>
            ) : null}

            <h3 className="side__heading">Selected node</h3>
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
              }}
              onPivot={(nodeId) => nodeId && dispatch({ type: "select-node", nodeId })}
            />
          </div>
        ) : null}
      </aside>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <span className="stat__value">{value}</span>
      <span className="stat__label">{label}</span>
    </div>
  );
}

function HubRow({ hub, onSelect }: { hub: GraphHub; onSelect: () => void }) {
  return (
    <button className="hub" onClick={onSelect} title={hub.node_id}>
      <span
        className="source-dot"
        style={{ background: colorForNode(hub.type ?? undefined), borderRadius: "50%" }}
      />
      <span className="hub__label">{hub.label ?? hub.node_id}</span>
      <span className="hub__degree">{hub.degree}</span>
    </button>
  );
}

/** A count table that shows proportion, so one dominant type is obvious. */
function Histogram({
  counts,
  colorize = false,
}: {
  counts: Record<string, number>;
  colorize?: boolean;
}) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const max = entries[0]?.[1] ?? 1;
  if (entries.length === 0) return <p className="muted small">Nothing yet.</p>;
  return (
    <div className="histogram">
      {entries.map(([name, count]) => (
        <div className="histogram__row" key={name}>
          <span className="histogram__name">{name}</span>
          <span className="histogram__bar">
            <span
              className="histogram__fill"
              style={{
                width: `${Math.max(2, Math.round((count / max) * 100))}%`,
                background: colorize ? colorForNode(name) : undefined,
              }}
            />
          </span>
          <span className="histogram__count">{count.toLocaleString()}</span>
        </div>
      ))}
    </div>
  );
}

export type { GraphNode };
