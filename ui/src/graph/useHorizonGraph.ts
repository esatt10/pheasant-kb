import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { GraphLink, GraphNode } from "../api/types";

/**
 * The canvas never draws the whole graph by default.
 *
 * A real index is tens of thousands of nodes; drawn at once it is a hairball
 * that answers no question. So the view is a *horizon*: everything within N
 * hops of one center node, N defaulting to 2 and adjustable by the user. After
 * a question the horizon is drawn around the nodes the answer actually
 * surfaced, so the canvas shows the evidence rather than the universe.
 */

export type HorizonMode = "center" | "answer" | "all";

export interface HorizonGraph {
  nodes: GraphNode[];
  links: GraphLink[];
  /** Hop distance from the nearest center; missing for the unbounded view. */
  depths: Record<string, number>;
  mode: HorizonMode;
  /** Nodes matching the filters before the horizon/limit was applied. */
  matchedNodes?: number;
  /** At least one backing request omitted nodes because of its safety limit. */
  truncated?: boolean;
}

export interface HorizonOptions {
  centerId: string | null;
  depth: number;
  showAll: boolean;
  surfacedIds: string[];
  hiddenTypes: string[];
  sourceFilter: string | null;
  /**
   * The selected node always gets its own one-hop slice unioned in.
   *
   * The horizon is a *drawing* budget, but a selection is a question — "what
   * is this connected to" — and answering it with "nothing, your depth filter
   * is 2" is the filter lying about the graph. Its neighbours may sit outside
   * the horizon, and `merge` drops any edge whose endpoint is missing, so
   * selecting a node near the horizon's rim showed it with no links at all.
   */
  selectedId?: string | null;
  enabled?: boolean;
}

/**
 * Edges left out of the walk.
 *
 * `indexes` runs from a source straight to every artifact it holds, bypassing
 * the directory tree. Walking it inside a bounded horizon means the budget is
 * spent leaping to files, and the directory→file structure — which is present
 * in the graph — never appears. Excluding it makes the canvas show the tree;
 * the files are still one hop from their directory.
 */
const SHORTCUT_EDGE_TYPES = ["indexes"];

/** Neighbours per slice. Generous for one center, tighter when unioning many. */
const SINGLE_CENTER_LIMIT = 500;
const PER_ANSWER_NODE_LIMIT = 150;
/** How many surfaced nodes get their own neighbourhood after a question. */
const MAX_ANSWER_CENTERS = 5;
/** Neighbours pulled in for the selected node, outside the horizon. */
const SELECTION_NEIGHBOUR_LIMIT = SINGLE_CENTER_LIMIT;

export function useHorizonGraph(options: HorizonOptions) {
  const { centerId, depth, showAll, surfacedIds, hiddenTypes, sourceFilter, selectedId } = options;
  const centers = surfacedIds.slice(0, MAX_ANSWER_CENTERS);
  const mode: HorizonMode = showAll || !centerId ? "all" : centers.length > 0 ? "answer" : "center";

  const query = useQuery({
    queryKey: [
      "horizon-graph",
      mode,
      mode === "answer" ? centers.join("|") : centerId,
      depth,
      hiddenTypes.join("|"),
      sourceFilter,
      // The selection widens the fetch, so it belongs in the key — otherwise
      // selecting a rim node would serve the cached, link-less slice.
      selectedId ?? "",
    ],
    enabled: options.enabled ?? true,
    queryFn: async (): Promise<HorizonGraph> => {
      if (mode === "all") {
        const graph = await api.graph({
          excludeTypes: hiddenTypes,
          source: sourceFilter ?? undefined,
        });
        return {
          nodes: graph.nodes,
          links: graph.links,
          depths: {},
          mode,
          matchedNodes: graph.matched_nodes,
          truncated: graph.truncated,
        };
      }

      const targets = mode === "answer" ? centers : [centerId as string];
      const limit = mode === "answer" ? PER_ANSWER_NODE_LIMIT : SINGLE_CENTER_LIMIT;
      const requests = targets.map((nodeId) => ({ nodeId, hops: depth, limit }));
      // The selected node's own neighbourhood, at one hop, regardless of the
      // horizon. Small and bounded, so it costs one extra slice call and makes
      // "what is this connected to" answerable at any depth setting.
      if (selectedId && !targets.includes(selectedId)) {
        requests.push({ nodeId: selectedId, hops: 1, limit: SELECTION_NEIGHBOUR_LIMIT });
      }
      const slices = await Promise.all(
        requests.map(({ nodeId, hops, limit: sliceLimit }) =>
          api
            .graphSlice(nodeId, hops, {
              limit: sliceLimit,
              excludeEdgeTypes: SHORTCUT_EDGE_TYPES,
              // Hidden types are pruned during the walk, not after it — the
              // budget has to reach the structure, not be spent on nodes this
              // view is about to drop.
              excludeTypes: hiddenTypes,
            })
            // One unreachable center must not blank the canvas.
            .catch(() => ({
              node_id: nodeId,
              depth: hops,
              nodes: [],
              links: [],
              depths: {},
              truncated: false,
            })),
        ),
      );
      return merge(slices, hiddenTypes, sourceFilter, mode);
    },
  });

  return {
    graph: query.data ?? { nodes: [], links: [], depths: {}, mode },
    isLoading: query.isLoading,
    isFetching: query.isFetching,
    refetch: query.refetch,
    mode,
  };
}

/**
 * Union the slices, then apply the filters the slice endpoint does not know
 * about (hidden node types, source scope) and drop links whose endpoints went
 * with them — a dangling edge renders as a line to nowhere.
 */
function merge(
  slices: Array<{
    nodes: GraphNode[];
    links: GraphLink[];
    depths?: Record<string, number>;
    truncated?: boolean;
  }>,
  hiddenTypes: string[],
  sourceFilter: string | null,
  mode: HorizonMode,
): HorizonGraph {
  const hidden = new Set(hiddenTypes);
  const nodes = new Map<string, GraphNode>();
  const depths: Record<string, number> = {};
  let matched = 0;

  for (const slice of slices) {
    for (const node of slice.nodes) {
      if (!node.id) continue;
      matched += 1;
      if (node.type && hidden.has(node.type)) continue;
      if (sourceFilter && node.source_id && node.source_id !== sourceFilter) continue;
      nodes.set(node.id, node);
    }
    for (const [nodeId, hop] of Object.entries(slice.depths ?? {})) {
      const current = depths[nodeId];
      if (current === undefined || hop < current) depths[nodeId] = hop;
    }
  }

  const links = new Map<string, GraphLink>();
  for (const slice of slices) {
    for (const link of slice.links) {
      if (!nodes.has(link.source) || !nodes.has(link.target)) continue;
      links.set(`${link.source}|${link.type}|${link.key ?? 0}|${link.target}`, link);
    }
  }

  return {
    nodes: [...nodes.values()],
    links: [...links.values()],
    depths,
    mode,
    matchedNodes: matched,
    truncated: slices.some((slice) => slice.truncated),
  };
}
