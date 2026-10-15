import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent, ReactNode, RefObject } from "react";
import { DragHandle } from "../browser/DragHandle";
import type { TileColumn, TileId } from "../state/session";
import { TILE_COLUMNS } from "../state/session";

/**
 * The Notebook's tiles: sections that can be dragged between and within the
 * four columns, and folded to their header wherever they are.
 *
 * Each tile's content is rendered once, through a portal, into a host element
 * that is *moved* to wherever the tile currently sits. Moving a tile therefore
 * never remounts what is in it: an answer still streaming into the chat keeps
 * streaming, and the graph keeps its layout. That is also why drag-and-drop
 * listens with native listeners — React delivers a portal's events along the
 * React tree, not along the DOM the column is part of.
 */

const DRAG_TYPE = "application/x-pheasant-tile";

export interface TileSpec {
  id: TileId;
  title: string;
  /** False while a tile has nothing to show (the taxonomy view while closed). */
  visible: boolean;
  collapsed: boolean;
  onToggle: () => void;
  /** The host its content is portalled into. */
  host: HTMLElement;
  /** A line beside the title on the folded strip. */
  subtitle?: string;
  /** Actions on the folded strip (the taxonomy view's close). */
  stripActions?: ReactNode;
}

interface TileContextValue {
  id: TileId;
  title: string;
  collapsed: boolean;
  onToggle: () => void;
}

const TileContext = createContext<TileContextValue | null>(null);

interface TileDragValue {
  dragging: TileId | null;
  setDragging: (tile: TileId | null) => void;
  move: (tile: TileId, column: TileColumn, before: TileId | null) => void;
  reset: () => void;
  /** Where each visible tile is, for keyboard moves. */
  columns: Record<TileColumn, TileId[]>;
}

const TileDragContext = createContext<TileDragValue | null>(null);

export function TileDragProvider({ value, children }: { value: TileDragValue; children: ReactNode }) {
  return <TileDragContext.Provider value={value}>{children}</TileDragContext.Provider>;
}

/** Wraps a tile's content so its header controls know which tile they belong to. */
export function TileScope({ spec, children }: { spec: TileSpec; children: ReactNode }) {
  return (
    <TileContext.Provider
      value={{ id: spec.id, title: spec.title, collapsed: spec.collapsed, onToggle: spec.onToggle }}
    >
      {children}
    </TileContext.Provider>
  );
}

/** One fresh element per tile, created once for the life of the page. */
export function useTileHosts(): Record<TileId, HTMLElement> {
  const [hosts] = useState(() => {
    const make = (id: TileId) => {
      const host = document.createElement("div");
      host.className = "tile__host";
      host.dataset.tileHost = id;
      return host;
    };
    return { sources: make("sources"), taxonomy: make("taxonomy"), chat: make("chat"), graph: make("graph") };
  });
  return hosts;
}

/** The grip a tile is dragged by; arrow keys move it, double-click resets the layout. */
export function TileGrip() {
  const tile = useContext(TileContext);
  const drag = useContext(TileDragContext);
  if (!tile || !drag) return null;
  const { id, title } = tile;

  const onKeyDown = (event: KeyboardEvent<HTMLSpanElement>) => {
    const where = TILE_COLUMNS.find((column) => drag.columns[column].includes(id));
    if (!where) return;
    const stack = drag.columns[where];
    const index = stack.indexOf(id);
    const columnIndex = TILE_COLUMNS.indexOf(where);
    let moved = true;
    if (event.key === "ArrowUp" && index > 0) drag.move(id, where, stack[index - 1]);
    else if (event.key === "ArrowDown" && index < stack.length - 1) drag.move(id, where, stack[index + 2] ?? null);
    else if (event.key === "ArrowLeft" && columnIndex > 0) drag.move(id, TILE_COLUMNS[columnIndex - 1], null);
    else if (event.key === "ArrowRight" && columnIndex < TILE_COLUMNS.length - 1)
      drag.move(id, TILE_COLUMNS[columnIndex + 1], null);
    else moved = false;
    if (moved) {
      event.preventDefault();
      // The grip moved with its tile; keep the keyboard on it.
      window.setTimeout(() => document.querySelector<HTMLElement>(`[data-tile-grip="${id}"]`)?.focus(), 0);
    }
  };

  return (
    <span
      className="tile-grip"
      role="button"
      tabIndex={0}
      draggable
      data-tile-grip={id}
      aria-label={`Move ${title}: drag, or use the arrow keys`}
      title={`Drag to move ${title} · arrow keys move it · double-click resets every tile`}
      onDragStart={(event) => {
        event.dataTransfer.setData(DRAG_TYPE, id);
        event.dataTransfer.effectAllowed = "move";
        const tileElement = event.currentTarget.closest<HTMLElement>("[data-tile]");
        if (tileElement) event.dataTransfer.setDragImage(tileElement, 16, 16);
        // Deferred: changing layout during dragstart cancels the drag in Chrome.
        window.setTimeout(() => drag.setDragging(id), 0);
      }}
      onDragEnd={() => drag.setDragging(null)}
      onDoubleClick={drag.reset}
      onKeyDown={onKeyDown}
    >
      ⠿
    </span>
  );
}

/** The fold button every tile has. */
export function TileFold() {
  const tile = useContext(TileContext);
  if (!tile) return null;
  return (
    <button
      type="button"
      className="btn btn--small btn--icon tile-fold"
      onClick={tile.onToggle}
      aria-label={tile.collapsed ? `Expand ${tile.title}` : `Collapse ${tile.title}`}
      aria-expanded={!tile.collapsed}
      title={tile.collapsed ? `Expand ${tile.title}` : `Collapse ${tile.title} to its header`}
    >
      {tile.collapsed ? "▸" : "▾"}
    </button>
  );
}

/** Moves a tile's host element into the slot it is shown in. */
function TileSlot({ host }: { host: HTMLElement }) {
  const slotRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const slot = slotRef.current;
    if (!slot) return;
    slot.appendChild(host);
    return () => {
      // Only if it is still here: a tile moved to another column has already
      // been taken by the slot that shows it now.
      if (host.parentNode === slot) slot.removeChild(host);
    };
  }, [host]);
  return <div className="tile__slot" ref={slotRef} />;
}

/**
 * One column of tiles. Unfolded tiles share its height; while two or more
 * are unfolded, a handle sizes the first of them. A side column whose tiles
 * are all folded narrows to a spine.
 */
export function TileColumnView({
  column,
  tiles,
  stackHeight,
  defaultStackHeight,
  onStackHeight,
  spine,
  columnRef: givenRef,
}: {
  column: TileColumn;
  tiles: TileSpec[];
  stackHeight: number;
  defaultStackHeight: number;
  onStackHeight: (height: number) => void;
  /** Draw folded tiles as vertical spines (a side column with nothing unfolded). */
  spine: boolean;
  /** The column element, for a caller that measures it (the inner column's resizer). */
  columnRef?: RefObject<HTMLDivElement>;
}) {
  const drag = useContext(TileDragContext);
  const ownRef = useRef<HTMLDivElement>(null);
  const columnRef = givenRef ?? ownRef;
  const firstOpenRef = useRef<HTMLDivElement>(null);
  const [drop, setDrop] = useState<{ before: TileId | null } | null>(null);
  const visible = tiles.filter((tile) => tile.visible);
  const open = visible.filter((tile) => !tile.collapsed);
  const sized = open.length >= 2 ? open[0].id : null;

  // Native listeners: a drag over a tile's content starts inside a portal.
  const visibleKey = visible.map((tile) => tile.id).join(",");
  useEffect(() => {
    const element = columnRef.current;
    if (!element || !drag) return;
    const before = (event: DragEvent): TileId | null => {
      const rows = Array.from(element.querySelectorAll<HTMLElement>(":scope > [data-tile]"));
      for (const row of rows) {
        const box = row.getBoundingClientRect();
        if (event.clientY < box.top + box.height / 2) return row.dataset.tile as TileId;
      }
      return null;
    };
    const over = (event: DragEvent) => {
      if (!event.dataTransfer?.types.includes(DRAG_TYPE)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      const target = before(event);
      setDrop((current) => (current && current.before === target ? current : { before: target }));
    };
    const leave = (event: DragEvent) => {
      if (!element.contains(event.relatedTarget as Node | null)) setDrop(null);
    };
    const dropped = (event: DragEvent) => {
      const tile = event.dataTransfer?.getData(DRAG_TYPE) as TileId | undefined;
      setDrop(null);
      if (!tile) return;
      event.preventDefault();
      drag.move(tile, column, before(event));
      drag.setDragging(null);
    };
    element.addEventListener("dragover", over);
    element.addEventListener("dragleave", leave);
    element.addEventListener("drop", dropped);
    return () => {
      element.removeEventListener("dragover", over);
      element.removeEventListener("dragleave", leave);
      element.removeEventListener("drop", dropped);
    };
  }, [drag, column, visibleKey]);

  useEffect(() => {
    if (!drag?.dragging) setDrop(null);
  }, [drag?.dragging]);

  const dragging = drag?.dragging ?? null;
  return (
    <div
      ref={columnRef}
      className={`tile-col tile-col--${column}${spine ? " tile-col--spine" : ""}${
        visible.length === 0 ? " tile-col--empty" : ""
      }${drop ? " tile-col--drop" : ""}${drop && drop.before === null ? " tile-col--drop-end" : ""}${
        dragging ? " tile-col--dragging" : ""
      }`}
      style={{ "--stack": `${stackHeight}px` } as CSSProperties}
      data-column={column}
    >
      {visible.length === 0 ? (
        <div className="tile-col__placeholder muted small">{dragging ? "Drop here" : column === "center" ? "Drag a tile here" : null}</div>
      ) : null}
      {visible.map((tile) => {
        const isDropTarget = drop?.before === tile.id && dragging !== tile.id;
        const className = `tile${tile.collapsed ? (spine ? " tile--spine" : " tile--strip") : " tile--open"}${
          tile.id === sized ? " tile--sized" : ""
        }${isDropTarget ? " tile--drop-before" : ""}${dragging === tile.id ? " tile--dragging" : ""}`;
        return (
          <TileFrame
            key={tile.id}
            tile={tile}
            className={className}
            spine={spine}
            frameRef={tile.id === sized ? firstOpenRef : undefined}
            handle={
              tile.id === sized ? (
                <DragHandle
                  axis="y"
                  variable="--stack"
                  containerRef={columnRef}
                  startRef={firstOpenRef}
                  min={120}
                  max={(box) => box.height - 160}
                  defaultSize={defaultStackHeight}
                  label={`Resize ${tile.title} and the tile below it`}
                  onCommit={onStackHeight}
                />
              ) : null
            }
          />
        );
      })}
    </div>
  );
}

function TileFrame({
  tile,
  className,
  spine,
  frameRef,
  handle,
}: {
  tile: TileSpec;
  className: string;
  spine: boolean;
  frameRef?: RefObject<HTMLDivElement>;
  handle: ReactNode;
}) {
  return (
    <>
      <div className={className} data-tile={tile.id} ref={frameRef}>
        {tile.collapsed ? (
          <TileScope spec={tile}>{spine ? <TileSpine tile={tile} /> : <TileStrip tile={tile} />}</TileScope>
        ) : (
          <TileSlot host={tile.host} />
        )}
      </div>
      {handle}
    </>
  );
}

/** A folded tile in a column that still has room across: its header, and nothing else. */
function TileStrip({ tile }: { tile: TileSpec }) {
  return (
    <section className="pane tile-strip" aria-label={`${tile.title} (collapsed)`}>
      <header className="pane__header">
        <TileGrip />
        <TileFold />
        <span className="pane__title">{tile.title}</span>
        {tile.subtitle ? <span className="tile-strip__subtitle muted small">{tile.subtitle}</span> : null}
        {tile.stripActions ? <div className="pane__actions">{tile.stripActions}</div> : null}
      </header>
    </section>
  );
}

/** A folded tile in a side column narrowed to a spine. */
function TileSpine({ tile }: { tile: TileSpec }) {
  return (
    <section className="pane tile-spine" aria-label={`${tile.title} (collapsed)`}>
      <TileFold />
      <span className="rail-collapsed__label">{tile.title}</span>
      <TileGrip />
    </section>
  );
}

/** Whether a media query matches, kept current. */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window !== "undefined" && "matchMedia" in window ? window.matchMedia(query).matches : false,
  );
  useEffect(() => {
    if (!("matchMedia" in window)) return;
    const list = window.matchMedia(query);
    const update = () => setMatches(list.matches);
    update();
    list.addEventListener("change", update);
    return () => list.removeEventListener("change", update);
  }, [query]);
  return matches;
}
