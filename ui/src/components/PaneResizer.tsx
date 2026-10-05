import { useCallback, useRef } from "react";
import type { PointerEvent as ReactPointerEvent, RefObject } from "react";

interface PaneResizerProps {
  /** Which CSS variable on the container this handle drives. */
  variable: "--rail" | "--panel";
  /** Which edge the width is measured from. */
  edge: "left" | "right";
  containerRef: RefObject<HTMLElement>;
  min: number;
  max: number;
  defaultWidth: number;
  label: string;
  onCommit: (width: number) => void;
}

/**
 * Drag handle between two panes.
 *
 * While dragging it writes the CSS variable straight onto the container and
 * dispatches nothing: a re-render per pointermove would re-run the graph
 * canvas's layout on every frame, which on a large graph turns a drag into a
 * slideshow. React state is updated once, on release. Double-click restores
 * the default width.
 */
export function PaneResizer({
  variable,
  edge,
  containerRef,
  min,
  max,
  defaultWidth,
  label,
  onCommit,
}: PaneResizerProps) {
  const widthRef = useRef<number | null>(null);

  const apply = useCallback(
    (width: number) => {
      const clamped = Math.max(min, Math.min(max, width));
      widthRef.current = clamped;
      containerRef.current?.style.setProperty(variable, `${clamped}px`);
    },
    [containerRef, max, min, variable],
  );

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const handle = event.currentTarget;
    handle.setPointerCapture(event.pointerId);
    handle.classList.add("resizer--active");

    const move = (moveEvent: PointerEvent) => {
      const box = containerRef.current?.getBoundingClientRect();
      if (!box) return;
      apply(edge === "left" ? moveEvent.clientX - box.left : box.right - moveEvent.clientX);
    };
    const up = () => {
      handle.classList.remove("resizer--active");
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
      if (widthRef.current !== null) onCommit(widthRef.current);
      widthRef.current = null;
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  };

  return (
    <div
      // The variable names the grid area this handle occupies (`--rail` ->
      // `resizer--rail`), so the layout places it by name rather than by its
      // position among whichever siblings happen to be rendered.
      className={`resizer resizer${variable}`}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      tabIndex={0}
      title={`${label} — drag to resize, double-click to reset`}
      onPointerDown={onPointerDown}
      onDoubleClick={() => {
        apply(defaultWidth);
        onCommit(defaultWidth);
      }}
      onKeyDown={(event) => {
        // Keyboard resizing, because a drag handle that only takes a mouse is
        // not usable by everyone.
        const step = event.shiftKey ? 48 : 16;
        const measured = parseInt(
          getComputedStyle(containerRef.current ?? document.body).getPropertyValue(variable),
          10,
        );
        const current =
          widthRef.current ?? (Number.isFinite(measured) ? measured : defaultWidth);
        if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
          event.preventDefault();
          const direction = event.key === "ArrowRight" ? 1 : -1;
          const delta = edge === "left" ? direction * step : -direction * step;
          apply(current + delta);
          if (widthRef.current !== null) onCommit(widthRef.current);
        }
      }}
    >
      <span className="resizer__grip" aria-hidden />
    </div>
  );
}
