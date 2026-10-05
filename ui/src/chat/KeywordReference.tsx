import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, RefObject } from "react";
import { createPortal } from "react-dom";
import type { AnswerKeyword } from "../api/types";

/**
 * What `@pheasant` reads, written as you would type it. `<…>` is a slot to
 * fill. `tests/test_answer_keywords.py` fills the slots and asserts every one
 * of these is still answered from the index, so the card cannot advertise a
 * phrasing the reader has stopped understanding.
 */
export const PHEASANT_PHRASES: { phrase: string; does: string }[] = [
  { phrase: "@pheasant list sources", does: "every source, with counts and status" },
  { phrase: "@pheasant source <name>", does: "one source: folders, types, links in and out" },
  { phrase: "@pheasant list documents", does: "every document, a page at a time" },
  { phrase: "@pheasant pdfs in <source>", does: "filtered by type and source" },
  { phrase: "@pheasant documents matching <text>", does: "filtered by path" },
  { phrase: "@pheasant document <path>", does: "outline, symbols, links in and out" },
  { phrase: "@pheasant what links to <path>", does: "a document's backlinks" },
  { phrase: "@pheasant links to <path>", does: "the same, paged" },
  { phrase: "@pheasant links between <source> and <source>", does: "how two sources relate" },
  { phrase: "@pheasant cross-source links", does: "every link that crosses sources" },
  { phrase: "@pheasant how many documents", does: "counts" },
  { phrase: "@pheasant recent documents", does: "newest first" },
  { phrase: "@pheasant sync status", does: "health and the index queue" },
  { phrase: "@pheasant list documents page 2", does: "any listing, any page" },
  { phrase: "@pheasant more", does: "the next page of the last listing" },
];

const STORE_KEY = "pheasant.keywordReference";
const WIDTH = 360;
/** The narrowest the card gets to stay beside the chat rather than over it. */
const MIN_WIDTH = 240;
const GAP = 12;

interface Placement {
  left: number;
  top: number;
}

interface Stored {
  open?: boolean;
  /** Set only once the reader has dragged it; until then it docks itself. */
  placement?: Placement | null;
}

function readStored(): Stored {
  try {
    return JSON.parse(window.localStorage.getItem(STORE_KEY) ?? "{}") as Stored;
  } catch {
    return {};
  }
}

function writeStored(value: Stored) {
  try {
    window.localStorage.setItem(STORE_KEY, JSON.stringify(value));
  } catch {
    /* a private window: the card still works, it just forgets */
  }
}

/** Whether the card is open, remembered per browser. */
export function useKeywordReference() {
  const [open, setOpen] = useState<boolean>(() => readStored().open === true);
  const set = useCallback((next: boolean) => {
    setOpen(next);
    writeStored({ ...readStored(), open: next });
  }, []);
  return { open, setOpen: set, toggle: () => set(!open) };
}

/**
 * The quick reference for first-word keywords, as a floating card.
 *
 * Non-modal: no scrim, nothing behind it is disabled, and focus stays in the
 * composer. It docks *beside* the chat column, over the graph pane, so the
 * conversation and the sources rail stay in view while you type; where there
 * is no room beside the chat it takes the viewport's right edge. Drag it by
 * its header to put it anywhere; "Dock" puts it back.
 *
 * While the message's first word is an `@…` being typed, the card narrows to
 * the keywords it could become (Tab completes the first). Opened that way it
 * closes again once the first word is done; opened with the button it stays.
 * Clicking a keyword puts it at the front of the message, and clicking an
 * example replaces the message with it.
 */
export function KeywordReference({
  keywords,
  draft,
  pinned,
  anchorRef,
  onClose,
  onInsert,
}: {
  keywords: AnswerKeyword[];
  draft: string;
  /** Opened with the button, so it stays until closed. */
  pinned: boolean;
  /** The chat column, which the card docks beside. */
  anchorRef: RefObject<HTMLElement>;
  onClose: () => void;
  onInsert: (text: string) => void;
}) {
  const [placement, setPlacement] = useState<Placement | null>(
    () => readStored().placement ?? null,
  );
  const [docked, setDocked] = useState<Placement & { height: number; width: number }>({
    left: 0,
    top: 0,
    height: 480,
    width: WIDTH,
  });
  const [filter, setFilter] = useState("");
  const cardRef = useRef<HTMLDivElement>(null);

  const typing = /^\s*@([\w-]*)$/.exec(draft);
  const prefix = (typing?.[1] ?? "").toLowerCase();
  const query = typing ? prefix : filter.trim().toLowerCase().replace(/^@/, "");
  const visible = pinned || Boolean(typing);

  // Dock beside the chat column, and follow it when the window or the panes
  // are resized. A dragged card keeps the place it was put.
  useLayoutEffect(() => {
    if (!visible) return;
    const place = () => {
      const chat = anchorRef.current?.getBoundingClientRect();
      const viewport = { width: window.innerWidth, height: window.innerHeight };
      const top = Math.max(GAP, (chat?.top ?? 80) + GAP);
      const height = Math.max(240, (chat?.bottom ?? viewport.height) - top - GAP);
      // Beside the chat, as wide as the room there allows; only when there is
      // too little room for a readable card does it take the viewport's edge.
      const room = chat ? viewport.width - chat.right - 2 * GAP : 0;
      const width = room >= MIN_WIDTH ? Math.min(WIDTH, room) : Math.min(WIDTH, viewport.width - 2 * GAP);
      const left = room >= MIN_WIDTH ? (chat?.right ?? 0) + GAP : viewport.width - width - GAP;
      setDocked({ left, top, width, height: Math.min(height, viewport.height - top - GAP) });
    };
    place();
    window.addEventListener("resize", place);
    const observer =
      typeof ResizeObserver === "undefined" || !anchorRef.current
        ? null
        : new ResizeObserver(place);
    if (observer && anchorRef.current) observer.observe(anchorRef.current);
    return () => {
      window.removeEventListener("resize", place);
      observer?.disconnect();
    };
  }, [visible, anchorRef]);

  useEffect(() => {
    if (!visible) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && cardRef.current?.contains(document.activeElement)) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [visible, onClose]);

  if (!visible || keywords.length === 0) return null;

  const startDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if ((event.target as HTMLElement).closest("button, input")) return;
    const card = cardRef.current?.getBoundingClientRect();
    if (!card) return;
    event.preventDefault();
    const dx = event.clientX - card.left;
    const dy = event.clientY - card.top;
    let last: Placement = { left: card.left, top: card.top };
    const move = (moved: PointerEvent) => {
      last = {
        left: clamp(moved.clientX - dx, 0, window.innerWidth - card.width),
        top: clamp(moved.clientY - dy, 0, window.innerHeight - 48),
      };
      setPlacement(last);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      writeStored({ ...readStored(), placement: last });
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const dock = () => {
    setPlacement(null);
    writeStored({ ...readStored(), placement: null });
  };

  const matches = (keyword: AnswerKeyword) =>
    !query ||
    [keyword.keyword, ...keyword.aliases].some((name) => name.slice(1).startsWith(query)) ||
    (!typing && keyword.summary.toLowerCase().includes(query));
  const shown = keywords.filter(matches);
  const groups = new Map<string, AnswerKeyword[]>();
  for (const keyword of shown) {
    groups.set(keyword.group, [...(groups.get(keyword.group) ?? []), keyword]);
  }
  const phrases = PHEASANT_PHRASES.filter(
    (row) =>
      !query ||
      (typing ? "pheasant".startsWith(query) : row.phrase.toLowerCase().includes(query)),
  );
  const at = placement ?? docked;

  return createPortal(
    <div
      ref={cardRef}
      className="keyref"
      role="dialog"
      aria-modal="false"
      aria-label="Keyword quick reference"
      style={{
        left: at.left,
        top: at.top,
        width: placement ? Math.min(WIDTH, window.innerWidth - 2 * GAP) : docked.width,
        maxHeight: placement ? window.innerHeight - placement.top - GAP : docked.height,
      }}
    >
      <header className="keyref__head" onPointerDown={startDrag} title="Drag to move">
        <span className="keyref__title">@ keywords</span>
        {placement ? (
          <button type="button" className="btn btn--small" onClick={dock}>
            Dock
          </button>
        ) : null}
        <button
          type="button"
          className="btn btn--ghost btn--icon btn--small"
          onClick={onClose}
          aria-label="Close the keyword reference"
          title="Close (Esc)"
        >
          ×
        </button>
      </header>
      <p className="keyref__rule">
        Start a message with a keyword; several can lead (<code>@detailed @table …</code>).
        Only <code>@pheasant</code> works anywhere.
        {typing ? " Tab completes the first match." : ""}
      </p>
      {typing ? null : (
        <input
          className="keyref__filter"
          type="search"
          placeholder="Filter…"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          aria-label="Filter keywords"
        />
      )}
      <div className="keyref__body">
        {[...groups.entries()].map(([group, members]) => (
          <section key={group} className="keyref__group">
            <h4>{group}</h4>
            {members.map((keyword) => (
              <div key={keyword.keyword} className="keyref__row">
                <button
                  type="button"
                  className="keyref__keyword"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => onInsert(keyword.keyword)}
                  title={
                    keyword.aliases.length
                      ? `Start the message with ${keyword.keyword} (also ${keyword.aliases.join(", ")})`
                      : `Start the message with ${keyword.keyword}`
                  }
                >
                  {keyword.keyword}
                </button>
                <div className="keyref__about">
                  {/* Summaries are Markdown for the help answer; plain text here. */}
                  <span>{keyword.summary.replace(/`/g, "")}</span>
                  <button
                    type="button"
                    className="keyref__example"
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => onInsert(`=${keyword.example}`)}
                    title="Use this example"
                  >
                    {keyword.example}
                  </button>
                </div>
              </div>
            ))}
          </section>
        ))}
        {phrases.length && keywords.some((k) => k.keyword === "@pheasant") ? (
          <section className="keyref__group">
            <h4>@pheasant asks the index</h4>
            {phrases.map((row) => (
              <button
                type="button"
                key={row.phrase}
                className="keyref__phrase"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => onInsert(`=${row.phrase}`)}
                title="Use this"
              >
                <code>{row.phrase}</code>
                <span>{row.does}</span>
              </button>
            ))}
          </section>
        ) : null}
        {shown.length === 0 && phrases.length === 0 ? (
          <p className="keyref__none">No keyword starts with “@{query}”.</p>
        ) : null}
      </div>
    </div>,
    document.body,
  );
}

/** The keywords the draft's first word could become, while it is being typed. */
export function keywordMatches(draft: string, keywords: AnswerKeyword[]): AnswerKeyword[] {
  const typing = /^\s*@([\w-]*)$/.exec(draft);
  if (!typing) return [];
  const prefix = typing[1].toLowerCase();
  return keywords.filter((keyword) =>
    [keyword.keyword, ...keyword.aliases].some((name) => name.slice(1).startsWith(prefix)),
  );
}

/**
 * The draft after a click in the card. `=text` replaces the draft (an
 * example); a bare keyword completes an `@…` the reader is typing as the first
 * word, or else goes in front of what they already wrote.
 */
export function insertKeyword(draft: string, inserted: string): string {
  if (inserted.startsWith("=")) {
    const example = inserted.slice(1);
    // Slots are for the reader to fill: stop before the first one.
    const slot = example.indexOf("<");
    return slot >= 0 ? example.slice(0, slot) : `${example} `;
  }
  if (/^\s*@[\w-]*$/.test(draft)) return `${inserted} `;
  return `${inserted} ${draft.trimStart()}`;
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), Math.max(low, high));
}
