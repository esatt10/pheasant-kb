import DOMPurify from "dompurify";
import { useEffect, useMemo, useRef, useState } from "react";
import { highlightLines, languageFor } from "../highlight";

/** Lines drawn before "show the rest": a DOM row per line is the cost. */
const LINE_WINDOW = 5000;

export interface LineFocus {
  start: number;
  end: number;
}

/**
 * Source with line numbers, syntax colour, and the cited lines marked.
 *
 * Highlighting runs once per text; lines are kept balanced so each row is
 * valid markup on its own. `find` marks the lines that contain it and steps
 * between them; `focus` (a cited passage's line span) is scrolled into view
 * and banded when the file opens.
 */
export function CodeView({
  text,
  language,
  extension,
  focus,
  wrap,
  find,
  findIndex = 0,
  onFindCount,
  scrollToLine,
}: {
  text: string;
  language?: string | null;
  extension?: string;
  focus?: LineFocus | null;
  wrap: boolean;
  find?: string;
  findIndex?: number;
  onFindCount?: (count: number) => void;
  /** A line to bring into view (from the outline). Changes re-scroll. */
  scrollToLine?: { line: number; nonce: number } | null;
}) {
  const resolved = languageFor(language, extension);
  const lines = useMemo(() => highlightLines(text, resolved), [text, resolved]);
  const plain = useMemo(() => text.split("\n"), [text]);
  const [showAll, setShowAll] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);

  const needle = (find ?? "").trim().toLowerCase();
  const matches = useMemo(() => {
    if (!needle) return [] as number[];
    const out: number[] = [];
    plain.forEach((line, index) => {
      if (line.toLowerCase().includes(needle)) out.push(index + 1);
    });
    return out;
  }, [needle, plain]);
  const matchSet = useMemo(() => new Set(matches), [matches]);
  const current = matches.length ? matches[((findIndex % matches.length) + matches.length) % matches.length] : null;

  useEffect(() => onFindCount?.(matches.length), [matches.length, onFindCount]);

  const visibleCount =
    showAll || lines.length <= LINE_WINDOW
      ? lines.length
      : Math.max(LINE_WINDOW, (focus?.end ?? 0) + 50, (current ?? 0) + 50);
  const target = current ?? scrollToLine?.line ?? focus?.start ?? null;

  useEffect(() => {
    if (!target) return;
    const row = bodyRef.current?.querySelector<HTMLElement>(`[data-line="${target}"]`);
    row?.scrollIntoView({ block: "center" });
  }, [target, scrollToLine?.nonce, lines]);

  return (
    <div className={`code-view${wrap ? " code-view--wrap" : ""}`} ref={bodyRef}>
      <div className="code-view__lines" role="table" aria-label="File content">
        {lines.slice(0, visibleCount).map((html, index) => {
          const number = index + 1;
          const focused = focus ? number >= focus.start && number <= focus.end : false;
          const classes = [
            "src-line",
            focused ? "src-line--focus" : "",
            matchSet.has(number) ? "src-line--match" : "",
            number === current ? "src-line--current" : "",
          ]
            .filter(Boolean)
            .join(" ");
          return (
            <div className={classes} key={number} data-line={number} role="row">
              <span className="src-line__no" role="cell" aria-hidden>
                {number}
              </span>
              <span
                className="src-line__text hljs"
                role="cell"
                dangerouslySetInnerHTML={{
                  __html:
                    DOMPurify.sanitize(html, { ALLOWED_TAGS: ["span"], ALLOWED_ATTR: ["class"] }) ||
                    "\u200b",
                }}
              />
            </div>
          );
        })}
      </div>
      {visibleCount < lines.length ? (
        <button type="button" className="btn btn--small code-view__more" onClick={() => setShowAll(true)}>
          Show the remaining {(lines.length - visibleCount).toLocaleString()} lines
        </button>
      ) : null}
    </div>
  );
}
