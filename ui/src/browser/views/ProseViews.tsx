import { useEffect, useMemo, useRef } from "react";
import type { DocumentOutlineEntry } from "../../api/types";

/**
 * Extracted document text (DOCX, DOC, RTF, EPUB, a PDF read as text) as a
 * page to read: paragraphs on a measure, the section headings the index found
 * set as headings, and the cited passage marked and scrolled to.
 */
export function ProseView({
  text,
  outline,
  focusExcerpt,
  find,
  scrollTo,
}: {
  text: string;
  outline: DocumentOutlineEntry[];
  focusExcerpt?: string | null;
  find?: string;
  scrollTo?: { title: string; nonce: number } | null;
}) {
  const blocks = useMemo(() => paragraphs(text), [text]);
  const headings = useMemo(() => new Set(outline.map((entry) => entry.title.trim())), [outline]);
  const focusIndex = useMemo(() => locate(blocks, focusExcerpt), [blocks, focusExcerpt]);
  const needle = (find ?? "").trim().toLowerCase();
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (focusIndex < 0) return;
    rootRef.current?.querySelector(`[data-block="${focusIndex}"]`)?.scrollIntoView({ block: "center" });
  }, [focusIndex, blocks]);

  useEffect(() => {
    if (!scrollTo) return;
    const index = blocks.findIndex((block) => block.trim() === scrollTo.title.trim());
    if (index >= 0) rootRef.current?.querySelector(`[data-block="${index}"]`)?.scrollIntoView({ block: "start" });
  }, [scrollTo, blocks]);

  useEffect(() => {
    if (!needle) return;
    const index = blocks.findIndex((block) => block.toLowerCase().includes(needle));
    if (index >= 0) rootRef.current?.querySelector(`[data-block="${index}"]`)?.scrollIntoView({ block: "center" });
  }, [needle, blocks]);

  if (!blocks.length) return <p className="muted small viewer-pad">No text was extracted from this document.</p>;
  return (
    <div className="prose-view" ref={rootRef}>
      {blocks.map((block, index) => {
        const heading =
          headings.has(block.trim()) ||
          /^#{1,6}\s/.test(block) ||
          looksLikeHeading(block, blocks[index + 1]);
        const classes = [
          heading ? "prose-view__heading" : "prose-view__para",
          index === focusIndex ? "prose-view--focus" : "",
          needle && block.toLowerCase().includes(needle) ? "prose-view--match" : "",
        ]
          .filter(Boolean)
          .join(" ");
        return heading ? (
          <h3 key={index} data-block={index} className={classes}>
            {block.replace(/^#{1,6}\s+/, "")}
          </h3>
        ) : (
          <p key={index} data-block={index} className={classes}>
            {block}
          </p>
        );
      })}
    </div>
  );
}

/**
 * A deck from its extracted text: one block per slide, notes included
 * (`ingestion.office`). The first line of a slide is usually its title.
 */
export function SlidesView({ text, find }: { text: string; find?: string }) {
  const slides = useMemo(() => text.split(/\n\s*\n/).map((block) => block.trim()).filter(Boolean), [text]);
  const needle = (find ?? "").trim().toLowerCase();
  if (!slides.length) return <p className="muted small viewer-pad">No text was extracted from this deck.</p>;
  return (
    <div className="slides-view">
      {slides.map((slide, index) => {
        const [title, ...rest] = slide.split("\n");
        const hit = needle && slide.toLowerCase().includes(needle);
        return (
          <article key={index} className={`slide-card${hit ? " slide-card--match" : ""}`}>
            <span className="slide-card__no">{index + 1}</span>
            <h4 className="slide-card__title">{title}</h4>
            {rest.length ? (
              <ul className="slide-card__body">
                {rest.map((line, lineIndex) => (
                  <li key={lineIndex}>{line}</li>
                ))}
              </ul>
            ) : null}
          </article>
        );
      })}
    </div>
  );
}

/**
 * Paragraphs from extracted text. A DOCX gives one line per paragraph and a
 * PDF gives its printed lines, so a line is joined to the next only when it
 * looks hard-wrapped: long, and not ending a sentence.
 */
function paragraphs(text: string): string[] {
  const out: string[] = [];
  for (const block of text.split(/\n\s*\n/)) {
    let current = "";
    for (const raw of block.split("\n")) {
      const line = raw.trim();
      if (!line) continue;
      current = current ? `${current} ${line}` : line;
      if (line.length < 55 || /[.!?:;)"”]$/.test(line)) {
        out.push(current);
        current = "";
      }
    }
    if (current) out.push(current);
  }
  return out;
}

/** A short line that reads as a title: no closing punctuation, followed by text. */
function looksLikeHeading(block: string, next: string | undefined): boolean {
  return (
    block.length <= 60 &&
    !/[.,;:!?)]$/.test(block) &&
    /^[\p{Lu}\d]/u.test(block) &&
    next !== undefined &&
    next.length > block.length
  );
}

/** The paragraph a cited excerpt starts in, by its first words. */
function locate(blocks: string[], excerpt?: string | null): number {
  const probe = (excerpt ?? "").replace(/\s+/g, " ").trim().slice(0, 60).toLowerCase();
  if (probe.length < 8) return -1;
  const flat = blocks.map((block) => block.replace(/\s+/g, " ").toLowerCase());
  const exact = flat.findIndex((block) => block.includes(probe));
  if (exact >= 0) return exact;
  const short = probe.slice(0, 24);
  return flat.findIndex((block) => block.includes(short));
}
