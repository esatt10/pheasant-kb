import type { GraphLink, GraphNode } from "../api/types";

/**
 * The "Columns" layout: every node in a column by the role its type plays,
 * read left to right from the structure that holds things to the labels that
 * describe them.
 *
 * Force and concentric layouts place a node by its connections, so the same
 * kind of thing lands anywhere on the canvas and a reader has to decode the
 * picture before reading it. Here the column says what a node *is* before a
 * single edge is followed: roots, then groupings, then documents, then what
 * documents point at, then labels. An edge then reads as a sentence from one
 * column to another ("this concept is derived from that policy").
 *
 * Opt-in, never the default. It is deterministic (no physics, nothing random)
 * and costs O(iterations × edges), but a column of a few hundred nodes is a
 * tall column, so it is the layout for a horizon or a filtered view rather
 * than for a whole large graph at once. The concentric default stays the
 * overview.
 */

export interface ColumnSpec {
  key: string;
  title: string;
}

/** Left to right. A column with no nodes in view is not drawn at all. */
export const COLUMNS: ColumnSpec[] = [
  { key: "roots", title: "Sources & bundles" },
  { key: "groups", title: "Groups & types" },
  { key: "documents", title: "Documents" },
  { key: "targets", title: "Parts, listings & references" },
  { key: "labels", title: "Tags & entities" },
];

const COLUMN_OF_TYPE: Record<string, number> = {
  knowledge_base: 0,
  source_type: 0,
  source: 0,
  repository: 0,
  okf_bundle: 0,
  directory: 1,
  okf_type: 1,
  branch: 1,
  commit: 1,
  file: 2,
  document: 2,
  markdown_note: 2,
  memory_record: 2,
  image: 2,
  audio: 2,
  heading: 3,
  chunk: 3,
  symbol: 3,
  external_reference: 3,
  tag: 4,
  entity: 4,
  concept: 4,
  topic: 4,
};

/** Everything not named above is a target: it is pointed at, not containing. */
const DEFAULT_COLUMN = 3;

/**
 * Edges that only say where something sits. Drawn thin and quiet in this
 * layout so the relationships that carry meaning are the ones the eye follows.
 */
export const STRUCTURAL_EDGE_TYPES = new Set([
  "contains",
  "indexes",
  "has_chunk",
  "has_heading",
]);

/**
 * Colours this layout gives a relationship in place of the canvas default.
 * `derived_from` is quiet grey everywhere else because symbols and entities
 * emit it by the thousand; in Columns it is provenance between documents and
 * the one edge a reader most wants to follow, and `links_to` has to be told
 * apart from the `references` edge that usually runs beside it.
 */
export const COLUMN_EDGE_COLORS: Record<string, string> = {
  derived_from: "#c0504d",
  links_to: "#3f6fb5",
};

/** Rows per sub-column before a tall column wraps beside itself. */
const MAX_ROWS = 48;
/** Clear space between two nodes of one column, beyond their own sizes. */
const ROW_CLEARANCE = 22;
/** Room for a node plus its label, which sits to its right in this layout. */
const SUB_COLUMN_GAP = 300;
const COLUMN_GAP = 420;
const SWEEPS = 4;

export interface ColumnPlan {
  /** Node id → canvas position. */
  positions: Record<string, { x: number; y: number }>;
  /** Node id → column index, so edges can tell a same-column hop. */
  columnOf: Record<string, number>;
  /** One header per drawn column, positioned above it. */
  headers: { id: string; label: string; x: number; y: number }[];
}

export function columnForNode(node: GraphNode): number {
  // An OKF `index.md` / `log.md` is a listing of the bundle's documents, not
  // one of them; beside the targets it lists is where it reads correctly.
  const okf = node.okf as { role?: string } | null | undefined;
  if (okf && (okf.role === "index" || okf.role === "log")) return 3;
  return COLUMN_OF_TYPE[node.type ?? ""] ?? DEFAULT_COLUMN;
}

/**
 * ``sizeOf`` is the drawn diameter of a node of that type. Rows are pitched by
 * it so a column of 46px roots and one of 14px tags are both evenly spaced
 * rather than one cramped and the other sparse.
 */
export function planColumns(
  nodes: GraphNode[],
  links: GraphLink[],
  spacing = 1,
  sizeOf: (type: string | undefined) => number = () => 26,
): ColumnPlan {
  const ids = new Set<string>();
  const columnOf: Record<string, number> = {};
  const byColumn: string[][] = COLUMNS.map(() => []);
  const label: Record<string, string> = {};
  const type: Record<string, string> = {};
  for (const node of nodes) {
    if (!node.id || ids.has(node.id)) continue;
    ids.add(node.id);
    const column = columnForNode(node);
    columnOf[node.id] = column;
    byColumn[column].push(node.id);
    label[node.id] = String(node.label ?? node.relative_path ?? node.id);
    type[node.id] = node.type ?? "";
  }

  const neighbours: Record<string, string[]> = {};
  for (const link of links) {
    if (!ids.has(link.source) || !ids.has(link.target) || link.source === link.target) continue;
    (neighbours[link.source] ??= []).push(link.target);
    (neighbours[link.target] ??= []).push(link.source);
  }

  // Start from a stable, readable order: by type, then by label. Every sweep
  // below sorts with these as the tie-break, so the result is deterministic
  // for a given graph whatever order the API returned it in.
  const tieBreak = (a: string, b: string) =>
    type[a].localeCompare(type[b]) || label[a].localeCompare(label[b]) || a.localeCompare(b);
  byColumn.forEach((column) => column.sort(tieBreak));

  // Barycentre ordering: each node moves to the average height of the
  // neighbours already placed, so a type hub's members gather beside it and a
  // concept sits near what it is derived from. Alternating sweeps let both
  // sides of a column pull on it. This is the classic Sugiyama crossing
  // heuristic, cheap and good enough at the sizes this layout is meant for.
  const rank: Record<string, number> = {};
  const assignRanks = () => {
    byColumn.forEach((column) => {
      const denominator = Math.max(column.length - 1, 1);
      column.forEach((id, index) => {
        rank[id] = index / denominator;
      });
    });
  };
  assignRanks();
  for (let sweep = 0; sweep < SWEEPS; sweep++) {
    const order = sweep % 2 === 0 ? [1, 2, 3, 4, 0] : [3, 2, 1, 0, 4];
    for (const c of order) {
      const column = byColumn[c];
      if (column.length < 2) continue;
      const key: Record<string, number> = {};
      for (const id of column) {
        const others = (neighbours[id] ?? []).filter((n) => columnOf[n] !== c);
        key[id] = others.length
          ? others.reduce((sum, n) => sum + rank[n], 0) / others.length
          : rank[id];
      }
      column.sort((a, b) => key[a] - key[b] || tieBreak(a, b));
      const denominator = Math.max(column.length - 1, 1);
      column.forEach((id, index) => {
        rank[id] = index / denominator;
      });
    }
  }

  const positions: ColumnPlan["positions"] = {};
  const headers: ColumnPlan["headers"] = [];
  // Drawn sizes grow with degree (up to ~1.5x); pitch for the grown size.
  const extent = (id: string) => sizeOf(type[id]) * 1.5;
  let x = 0;
  let top = 0;
  const drawn: { c: number; x: number; width: number }[] = [];
  byColumn.forEach((column, c) => {
    if (column.length === 0) return;
    const subColumns = Math.ceil(column.length / MAX_ROWS);
    const rows = Math.ceil(column.length / subColumns);
    for (let sub = 0; sub < subColumns; sub++) {
      // Wrapped column-major, so neighbours in the order stay neighbours on
      // screen: the first `rows` nodes fill the first sub-column top to bottom.
      const slice = column.slice(sub * rows, (sub + 1) * rows);
      const ys: number[] = [];
      let y = 0;
      slice.forEach((id, index) => {
        if (index > 0) y += (extent(slice[index - 1]) + extent(id)) / 2 + ROW_CLEARANCE * spacing;
        ys.push(y);
      });
      // Each sub-column is centred on the canvas midline, so short columns sit
      // level with the middle of tall ones instead of hanging from the top.
      const middle = y / 2;
      slice.forEach((id, index) => {
        positions[id] = { x: x + sub * SUB_COLUMN_GAP * spacing, y: ys[index] - middle };
      });
      top = Math.min(top, -middle - extent(slice[0]) / 2);
    }
    const width = (subColumns - 1) * SUB_COLUMN_GAP * spacing;
    drawn.push({ c, x, width });
    x += width + COLUMN_GAP * spacing;
  });
  const headerY = top - 50 * spacing;
  for (const { c, x: left, width } of drawn) {
    headers.push({
      id: `__column__:${COLUMNS[c].key}`,
      label: COLUMNS[c].title,
      x: left + width / 2,
      y: headerY,
    });
  }
  return { positions, columnOf, headers };
}
