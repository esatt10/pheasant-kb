import { useEffect } from "react";
import type { RefObject } from "react";

/**
 * Finding a cited passage in a *rendered* document.
 *
 * The region cites a chunk of the document's indexed text; a renderer shows
 * something else — Markdown without its syntax, a spreadsheet as cells, a PDF
 * as positioned glyphs. So the passage is matched on letters and digits only,
 * lowercased: everything a renderer is free to change (whitespace, markup,
 * punctuation, cell boundaries) is outside the comparison, and what is left is
 * what a reader would call "the same words".
 *
 * The highlight is drawn with the CSS Custom Highlight API rather than by
 * wrapping text in `<mark>`: the views are React trees (and the PDF text layer
 * belongs to pdf.js), and a highlight that mutates someone else's DOM is one
 * that the next render silently undoes or breaks.
 */

const HIGHLIGHT = "pheasant-cite";

/** Characters kept for matching: letters and digits, in any script. */
const KEPT = /[\p{L}\p{N}]/u;

/** How many normalized characters make an anchor long enough not to recur by chance. */
const ANCHOR = 48;

export function normalizeForMatch(text: string): string {
  let out = "";
  for (const char of text) if (KEPT.test(char)) out += char.toLowerCase();
  return out;
}

/** Most anchors sampled from one passage; a long passage samples them further apart. */
const MAX_ANCHORS = 40;
/** Occurrences of one anchor considered; boilerplate past this many votes nowhere useful. */
const MAX_OCCURRENCES = 64;

export interface PassageMatch {
  start: number;
  end: number;
  /** Anchors that agree on this placement, of `anchors` sampled. */
  score: number;
  anchors: number;
}

/**
 * Where `passage` sits in `haystack`, both already normalized, as a
 * `[start, end)` span of `haystack` — or null.
 *
 * Exact first. Otherwise the passage is cut into anchors and each occurrence
 * of each anchor votes for where the passage would start; the placement most
 * anchors agree on wins. Voting rather than taking the first anchor found
 * matters on real documents: a running header, a repeated disclaimer or a
 * templated sentence recurs on every page, and the first occurrence of it is
 * usually the wrong one. A renderer that drops part of the passage (a link's
 * URL, a hyphenated line break) shifts the later anchors a little, which the
 * cluster's width absorbs.
 */
export function matchPassage(haystack: string, passage: string): PassageMatch | null {
  if (!passage || !haystack) return null;
  const exact = haystack.indexOf(passage);
  if (exact >= 0 && haystack.indexOf(passage, exact + 1) < 0) {
    return { start: exact, end: exact + passage.length, score: 1, anchors: 1 };
  }
  if (passage.length < ANCHOR) {
    return exact >= 0 ? { start: exact, end: exact + passage.length, score: 1, anchors: 1 } : null;
  }

  const offsets: number[] = [];
  const last = passage.length - ANCHOR;
  const step = Math.max(Math.floor(ANCHOR / 2), Math.ceil(last / (MAX_ANCHORS - 1)));
  for (let offset = 0; offset < last; offset += step) offsets.push(offset);
  offsets.push(last);

  // Each vote: where the passage would start, and which anchor said so.
  const votes: { start: number; anchor: number; at: number }[] = [];
  offsets.forEach((offset, anchor) => {
    const needle = passage.slice(offset, offset + ANCHOR);
    let count = 0;
    for (let at = haystack.indexOf(needle); at >= 0 && count < MAX_OCCURRENCES; at = haystack.indexOf(needle, at + 1)) {
      votes.push({ start: at - offset, anchor, at });
      count += 1;
    }
  });
  if (!votes.length) return null;
  votes.sort((a, b) => a.start - b.start);

  // The window of expected starts holding the most distinct anchors.
  const width = Math.max(ANCHOR, Math.floor(passage.length / 4));
  let best: { from: number; to: number; score: number } | null = null;
  const inWindow = new Map<number, number>();
  for (let from = 0, to = 0; from < votes.length; from += 1) {
    while (to < votes.length && votes[to].start - votes[from].start <= width) {
      inWindow.set(votes[to].anchor, (inWindow.get(votes[to].anchor) ?? 0) + 1);
      to += 1;
    }
    if (!best || inWindow.size > best.score) best = { from, to, score: inWindow.size };
    const leaving = votes[from].anchor;
    const left = (inWindow.get(leaving) ?? 1) - 1;
    if (left) inWindow.set(leaving, left);
    else inWindow.delete(leaving);
  }
  if (!best) return null;
  const cluster = votes.slice(best.from, best.to);
  const first = cluster.reduce((a, b) => (b.anchor < a.anchor || (b.anchor === a.anchor && b.at < a.at) ? b : a));
  const final = cluster.reduce((a, b) => (b.anchor > a.anchor || (b.anchor === a.anchor && b.at > a.at) ? b : a));
  // From the first anchor that matched to the end of the last, then outwards
  // for as long as the view and the passage still agree letter for letter.
  // Words the view does not show at either end (a sheet's name drawn as a
  // tab) are not guessed at: the text beside them here is something else.
  let start = first.at;
  for (let p = offsets[first.anchor] - 1; p >= 0 && start > 0 && haystack[start - 1] === passage[p]; p -= 1) start -= 1;
  let end = final.at + ANCHOR;
  for (
    let p = offsets[final.anchor] + ANCHOR;
    p < passage.length && end < haystack.length && haystack[end] === passage[p];
    p += 1
  )
    end += 1;
  return { start, end, score: best.score, anchors: offsets.length };
}

export function locatePassage(haystack: string, passage: string): [number, number] | null {
  const match = matchPassage(haystack, passage);
  return match ? [match.start, match.end] : null;
}

/** The text of a cited passage, from the document text and the chunk's line span. */
export function passageText(
  text: string,
  focus: { start_line?: number | null; end_line?: number | null; excerpt?: string } | null,
): string | null {
  if (!focus) return null;
  if (text && focus.start_line && focus.end_line && focus.end_line >= focus.start_line) {
    const lines = text.split("\n").slice(focus.start_line - 1, focus.end_line);
    const joined = lines.join("\n").trim();
    if (joined) return joined;
  }
  return focus.excerpt?.trim() || null;
}

interface Mapped {
  /** Normalized text of every text node under the root, in document order. */
  text: string;
  /** For each normalized character, the node and offset it came from. */
  nodes: Text[];
  offsets: number[];
}

function mapTextNodes(root: Node): Mapped {
  const nodes: Text[] = [];
  const offsets: number[] = [];
  let text = "";
  // A view's own chrome — row numbers, line-number gutters, sheet tabs — is
  // marked `data-passage-skip`: it sits between the cited words in the DOM
  // and is not part of the document.
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) =>
      node.parentElement?.closest("[data-passage-skip]") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
    const value = node.data;
    // Code points, not UTF-16 units, so an offset maps back to a real boundary.
    let index = 0;
    for (const char of value) {
      if (KEPT.test(char)) {
        const lowered = char.toLowerCase();
        for (let i = 0; i < lowered.length; i += 1) {
          text += lowered[i];
          nodes.push(node);
          offsets.push(index);
        }
      }
      index += char.length;
    }
  }
  return { text, nodes, offsets };
}

function supported(): boolean {
  return typeof CSS !== "undefined" && "highlights" in CSS && typeof Highlight !== "undefined";
}

/** Clear the cited-passage highlight wherever it is drawn. */
export function clearPassageHighlight() {
  if (supported()) CSS.highlights.delete(HIGHLIGHT);
}

/**
 * Highlight `passage` wherever it appears under `root`; true if it was found.
 * Scrolls the first match into view when `scroll` is set.
 */
export function highlightPassage(root: Element, passage: string, scroll: boolean): boolean {
  if (!supported()) return false;
  const needle = normalizeForMatch(passage);
  const mapped = mapTextNodes(root);
  const span = locatePassage(mapped.text, needle);
  if (!span) {
    CSS.highlights.delete(HIGHLIGHT);
    return false;
  }
  const [start, end] = span;
  // One range per text node, from its first matched character to its last:
  // whatever lies between nodes (a skipped row number, the space between two
  // cells) is not painted.
  const ranges: Range[] = [];
  for (let i = start; i < end; ) {
    const node = mapped.nodes[i];
    let j = i;
    while (j + 1 < end && mapped.nodes[j + 1] === node) j += 1;
    const lastOffset = mapped.offsets[j];
    // After the last kept character, however many UTF-16 units it is.
    const tail = node.data.codePointAt(lastOffset) ?? 0;
    const range = document.createRange();
    range.setStart(node, mapped.offsets[i]);
    range.setEnd(node, lastOffset + (tail > 0xffff ? 2 : 1));
    ranges.push(range);
    i = j + 1;
  }
  CSS.highlights.set(HIGHLIGHT, new Highlight(...ranges));
  if (scroll) mapped.nodes[start].parentElement?.scrollIntoView({ block: "center" });
  return true;
}

/**
 * Keep `passage` highlighted under `rootRef` while the view renders.
 *
 * Views render asynchronously — Markdown after parsing, a PDF page by page as
 * it scrolls in — so the match is retried whenever the subtree changes, and
 * scrolled to only the first time it is found: after that the reader is in
 * charge of where they are.
 */
export function usePassageHighlight(rootRef: RefObject<HTMLElement>, passage: string | null, key: string) {
  useEffect(() => {
    const root = rootRef.current;
    if (!root || !passage || !supported()) return;
    let scrolled = false;
    let frame = 0;
    const apply = () => {
      frame = 0;
      if (highlightPassage(root, passage, !scrolled)) scrolled = true;
    };
    const schedule = () => {
      if (!frame) frame = window.requestAnimationFrame(apply);
    };
    schedule();
    const observer = new MutationObserver(schedule);
    observer.observe(root, { childList: true, subtree: true, characterData: true });
    return () => {
      observer.disconnect();
      if (frame) window.cancelAnimationFrame(frame);
      clearPassageHighlight();
    };
  }, [rootRef, passage, key]);
}
