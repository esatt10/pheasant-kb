import type { GraphLink, GraphNode } from "../api/types";
import type { Theme } from "../hooks/useTheme";

// Cytoscape's exported stylesheet type name varies across @types versions, so we
// keep the stylesheet array loosely typed and let Cytoscape validate at runtime.
type CyStylesheet = Record<string, unknown>;

/**
 * Node colours, tuned for a light canvas.
 *
 * The old palette was picked for a near-black background and washed out on
 * paper; these are the same hue families at a darker, higher-contrast value so
 * type is still readable as colour at a glance on either theme.
 */
export const NODE_COLORS: Record<string, string> = {
  knowledge_base: "#8a6a2a",
  // A hub grouping every source of one kind. Same family as `source` but a
  // step cooler, so the two read as related without the hub competing with
  // the sources hanging off it.
  source_type: "#3f7f86",
  source: "#4a6a70",
  repository: "#4a6a70",
  directory: "#6f8b8e",
  file: "#5f6b45",
  document: "#5f6b45",
  markdown_note: "#7d8d5c",
  memory_record: "#8a6f9e",
  chunk: "#6e6480",
  symbol: "#a06b3f",
  entity: "#9a5a58",
  concept: "#7c5d84",
  external_reference: "#7c776b",
  // Open Knowledge Format: a detected bundle, its concept-type hubs and tags.
  // The bundle shares the hub family with `source_type` (it groups); types
  // and tags are warm so a bundle's classification reads apart from content.
  okf_bundle: "#2f6f7a",
  okf_type: "#b5832e",
  tag: "#9c7a3c",
};

export const EDGE_COLORS: Record<string, string> = {
  contains: "#9e998c",
  indexes: "#7f8b9e",
  has_chunk: "#8b81a6",
  mentions: "#a8808f",
  derived_from: "#9e998c",
  imports: "#b98f5c",
  calls: "#b07a4a",
  references: "#5f8b93",
  similar_to: "#9b86ad",
  links_to: "#6b8a9e",
  about: "#8a6f9e",
  supersedes: "#b0654f",
  tagged_with: "#c2a36b",
  executed_by: "#4f8a5b",
  attested_by: "#3e7a4a",
  computed_by: "#6a9a74",
};

export const ALL_EDGE_TYPES = Object.keys(EDGE_COLORS);
export const ALL_NODE_TYPES = Object.keys(NODE_COLORS);

/**
 * Types hidden by default.
 *
 * A real index produces far more `concept` nodes than anything else — on the
 * sample workspace they are ~80% of the graph — and drawn all at once they bury
 * the structure the user actually came to navigate. They stay one click away in
 * the legend rather than being the first thing you see.
 */
export const NOISY_NODE_TYPES = ["concept", "chunk", "entity", "external_reference"];

export type ShapeAlgorithm = "node_type" | "degree" | "uniform";

/**
 * Four shapes, not twelve.
 *
 * Cytoscape offers stars, vees and tags, and using all of them made the canvas
 * read as decoration rather than structure. The vocabulary here is deliberately
 * small: containers are rounded rectangles, documents are rectangles, ideas are
 * circles, and code symbols are diamonds. Colour carries the finer distinction.
 */
export const NODE_TYPE_SHAPES: Record<string, string> = {
  knowledge_base: "round-rectangle",
  // Hexagon: the one hub shape in the graph, so a grouping node is
  // distinguishable from the sources it groups at a glance.
  source_type: "hexagon",
  source: "round-rectangle",
  repository: "round-rectangle",
  directory: "round-rectangle",
  file: "rectangle",
  document: "rectangle",
  markdown_note: "rectangle",
  memory_record: "round-rectangle",
  chunk: "ellipse",
  symbol: "diamond",
  entity: "ellipse",
  concept: "ellipse",
  external_reference: "rectangle",
  okf_bundle: "hexagon",
  okf_type: "hexagon",
  tag: "ellipse",
};

/** Base radius per type, so the graph has a visual hierarchy at rest. */
const NODE_SIZES: Record<string, number> = {
  knowledge_base: 46,
  // Between the root and a source: it sits under one and groups the other.
  source_type: 40,
  source: 36,
  repository: 36,
  directory: 24,
  file: 26,
  document: 26,
  markdown_note: 26,
  memory_record: 26,
  chunk: 14,
  symbol: 18,
  entity: 18,
  concept: 14,
  external_reference: 16,
  okf_bundle: 40,
  okf_type: 30,
  tag: 14,
};

export function colorForNode(type?: string): string {
  return (type && NODE_COLORS[type]) || "#7c776b";
}

/**
 * Canvas colours resolved per theme.
 *
 * Cytoscape draws to a `<canvas>` and never resolves CSS custom properties, so
 * anything handed to it has to be a concrete value — `var(--graph-label, …)`
 * silently used its light-theme fallback in dark mode.
 */
const CANVAS_COLORS = {
  light: {
    node: "#7c776b",
    edge: "#a9a49a",
    label: "#4b463d",
    labelBackground: "#ffffff",
    outline: "#1f1c17",
    accent: "#5f6b45",
  },
  dark: {
    node: "#7c776b",
    edge: "#4a453c",
    label: "#bcb4a6",
    labelBackground: "#191713",
    outline: "#ece7dd",
    accent: "#a9b485",
  },
} as const;

export function shapeForNodeType(type?: string): string {
  return (type && NODE_TYPE_SHAPES[type]) || "ellipse";
}

export function sizeForNodeType(type?: string): number {
  return (type && NODE_SIZES[type]) || 20;
}

export interface CyElement {
  data: Record<string, unknown>;
  classes?: string;
}

/** Rings beyond this share the outermost styling. */
export const MAX_STYLED_RING = 4;

// Convert a node-link graph into Cytoscape elements, deduplicating ids.
export function toElements(
  nodes: GraphNode[],
  links: GraphLink[],
  shapeAlgorithm: ShapeAlgorithm = "node_type",
  depths?: Record<string, number>,
): CyElement[] {
  const seen = new Set<string>();
  const elements: CyElement[] = [];
  const degree = new Map<string, number>();
  links.forEach((link) => {
    degree.set(link.source, (degree.get(link.source) ?? 0) + 1);
    degree.set(link.target, (degree.get(link.target) ?? 0) + 1);
  });
  for (const node of nodes) {
    if (!node.id || seen.has(node.id)) continue;
    seen.add(node.id);
    const connections = degree.get(node.id) ?? 0;
    const hop = depths?.[node.id];
    elements.push({
      data: {
        id: node.id,
        label: shorten(node.label ?? node.id),
        ntype: node.type ?? "unknown",
        shape: shapeForNode(node.type, connections, shapeAlgorithm),
        // Well-connected nodes grow a little so hubs stand out without a
        // separate "importance" legend.
        size: sizeForNode(node.type, connections, shapeAlgorithm),
      },
      // Distance from the center reads as depth on the canvas: the center is
      // solid, each ring out a little quieter, so "how far is this from what I
      // asked about" is answerable at a glance.
      classes: hop === undefined ? undefined : `ring-${Math.min(hop, MAX_STYLED_RING)}`,
    });
  }
  for (const link of links) {
    if (!seen.has(link.source) || !seen.has(link.target)) continue;
    const etype = link.type ?? "related";
    const id = `${link.source}__${etype}__${link.key ?? 0}__${link.target}`;
    elements.push({
      data: { id, source: link.source, target: link.target, etype, label: etype },
    });
  }
  return elements;
}

function shapeForNode(type: string | undefined, degree: number, algorithm: ShapeAlgorithm): string {
  if (algorithm === "uniform") return "ellipse";
  if (algorithm === "degree") {
    if (degree >= 8) return "round-rectangle";
    if (degree >= 3) return "diamond";
    return "ellipse";
  }
  return shapeForNodeType(type);
}

function sizeForNode(type: string | undefined, degree: number, algorithm: ShapeAlgorithm): number {
  if (algorithm === "degree") return Math.min(52, 14 + degree * 3);
  const base = sizeForNodeType(type);
  return Math.min(base + Math.min(degree, 12), base * 1.8);
}

function shorten(label: string): string {
  if (label.length <= 26) return label;
  return `…${label.slice(label.length - 25)}`;
}

export function buildStylesheet(theme: Theme = "light"): CyStylesheet[] {
  const canvas = CANVAS_COLORS[theme];
  const nodeColorRules: CyStylesheet[] = Object.entries(NODE_COLORS).map(([type, color]) => ({
    selector: `node[ntype = "${type}"]`,
    style: { "background-color": color, "border-color": color },
  }));
  const edgeColorRules: CyStylesheet[] = Object.entries(EDGE_COLORS).map(([type, color]) => ({
    selector: `edge[etype = "${type}"]`,
    style: { "line-color": color, "target-arrow-color": color },
  }));
  return [
    {
      selector: "node",
      style: {
        "background-color": canvas.node,
        "background-opacity": 0.92,
        shape: "data(shape)",
        "border-width": 0,
        label: "data(label)",
        // Read against the canvas, not against the node — labels sit below.
        color: canvas.label,
        "font-size": 9.5,
        "font-weight": 500,
        "text-valign": "bottom",
        "text-margin-y": 5,
        "text-wrap": "ellipsis",
        "text-max-width": "110px",
        "text-background-color": canvas.labelBackground,
        "text-background-opacity": 0,
        width: "data(size)",
        height: "data(size)",
        "transition-property": "opacity, border-width, background-opacity",
        "transition-duration": 120,
      },
    },
    // Only the structural spine is labelled at rest; labelling every concept
    // turns the canvas into a word cloud.
    {
      selector: 'node[ntype = "concept"], node[ntype = "chunk"]',
      style: { "font-size": 8.5, opacity: 0.8 },
    },
    {
      selector: 'node[ntype = "knowledge_base"], node[ntype = "source"]',
      style: { "font-size": 11.5, "font-weight": 600 },
    },
    {
      selector: "edge",
      style: {
        width: 1.1,
        "line-color": canvas.edge,
        "target-arrow-color": canvas.edge,
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.6,
        "curve-style": "bezier",
        opacity: 0.75,
      },
    },
    ...nodeColorRules,
    ...edgeColorRules,
    {
      selector: "node.selected",
      style: {
        "border-width": 3,
        "border-color": canvas.outline,
        "border-opacity": 0.85,
        "background-opacity": 1,
        "z-index": 20,
      },
    },
    {
      // Nodes the current answer cited — the link from prose back to graph.
      selector: "node.cited",
      style: {
        "border-width": 3,
        "border-color": canvas.accent,
        "background-opacity": 1,
        "z-index": 15,
      },
    },
    // Rings: the center is emphatic, each layer out recedes. This is what
    // makes a depth-3 horizon read as "near / middle / far" instead of as a
    // flat blob of equally-loud nodes.
    {
      selector: "node.ring-0",
      style: {
        "border-width": 3,
        "border-color": canvas.outline,
        "border-opacity": 0.55,
        "font-size": 12,
        "font-weight": 700,
        "z-index": 12,
      },
    },
    { selector: "node.ring-1", style: { opacity: 1 } },
    { selector: "node.ring-2", style: { opacity: 0.85, "font-size": 9 } },
    { selector: "node.ring-3", style: { opacity: 0.65, "font-size": 8.5 } },
    { selector: "node.ring-4", style: { opacity: 0.5, label: "" } },
    // Faded, not erased: the surrounding structure is still the context that
    // makes a focused neighbourhood legible.
    { selector: ".faded", style: { opacity: 0.18 } },
    {
      selector: "node.focus",
      style: { "border-width": 2, "border-color": canvas.accent, "z-index": 10 },
    },
    { selector: "edge.focus", style: { opacity: 0.9, width: 1.6 } },
  ];
}
