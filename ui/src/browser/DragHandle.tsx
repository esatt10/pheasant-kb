import { useRef } from "react";
import type { PointerEvent as ReactPointerEvent, RefObject } from "react";

/**
 * A drag handle that sizes the pane before it, along either axis.
 *
 * The same mechanics as `PaneResizer`: while dragging it writes a CSS variable
 * straight onto the container and dispatches nothing, and commits once on
 * release, so the viewer and the chat do not re-render on every pointermove.
 * Double-click restores the default; arrow keys nudge it.
 */
export function DragHandle({
  axis,
  variable,
  containerRef,
  min,
  max,
  defaultSize,
  label,
  onCommit,
}: {
  /** `y` sizes a height (a horizontal bar); `x` a width (a vertical bar). */
  axis: "x" | "y";
  variable: string;
  containerRef: RefObject<HTMLElement>;
  min: number;
  max: (container: DOMRect) => number;
  defaultSize: number;
  label: string;
  onCommit: (size: number) => void;
}) {
  const sizeRef = useRef<number | null>(null);

  const apply = (size: number) => {
    const box = containerRef.current?.getBoundingClientRect();
    const ceiling = box ? max(box) : size;
    const clamped = Math.max(min, Math.min(Math.max(min, ceiling), size));
    sizeRef.current = clamped;
    containerRef.current?.style.setProperty(variable, `${clamped}px`);
  };

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const handle = event.currentTarget;
    handle.setPointerCapture(event.pointerId);
    handle.classList.add("drag-handle--active");
    const move = (moveEvent: PointerEvent) => {
      const box = containerRef.current?.getBoundingClientRect();
      if (!box) return;
      apply(axis === "y" ? moveEvent.clientY - box.top : moveEvent.clientX - box.left);
    };
    const up = () => {
      handle.classList.remove("drag-handle--active");
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
      if (sizeRef.current !== null) onCommit(sizeRef.current);
      sizeRef.current = null;
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  };

  return (
    <div
      className={`drag-handle drag-handle--${axis}`}
      role="separator"
      aria-orientation={axis === "y" ? "horizontal" : "vertical"}
      aria-label={label}
      tabIndex={0}
      title={`${label} — drag to resize, double-click to reset`}
      onPointerDown={onPointerDown}
      onDoubleClick={() => {
        apply(defaultSize);
        onCommit(sizeRef.current ?? defaultSize);
      }}
      onKeyDown={(event) => {
        const keys = axis === "y" ? ["ArrowUp", "ArrowDown"] : ["ArrowLeft", "ArrowRight"];
        if (!keys.includes(event.key)) return;
        event.preventDefault();
        const step = event.shiftKey ? 48 : 16;
        const measured = parseInt(
          getComputedStyle(containerRef.current ?? document.body).getPropertyValue(variable),
          10,
        );
        const current = Number.isFinite(measured) ? measured : defaultSize;
        apply(current + (event.key === keys[1] ? step : -step));
        if (sizeRef.current !== null) onCommit(sizeRef.current);
        sizeRef.current = null;
      }}
    >
      <span className="drag-handle__grip" aria-hidden />
    </div>
  );
}
