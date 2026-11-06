import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, RefObject } from "react";
import { api } from "../../api/client";
import { matchPassage, normalizeForMatch, usePassageHighlight } from "../passage";
import { ViewerLoading, useObjectUrl } from "./MediaViews";
import type { PDFDocumentProxy } from "./pdfjs";

/** Pages rendered ahead of the viewport, so scrolling does not meet blank pages. */
const RENDER_MARGIN = "800px 0px";
const MIN_ZOOM = 0.25;
const MAX_ZOOM = 4;

/**
 * A PDF drawn page by page with pdf.js, each page carrying a text layer.
 *
 * The text layer is the point: it is what lets a cited passage be found and
 * highlighted on the page it came from, where the browser's own viewer is a
 * frame nothing can mark. That viewer is still one click away — it is the
 * better tool for printing and searching — and it is where this falls back to
 * if pdf.js cannot read the file.
 */
export function PdfView({
  source,
  path,
  passage,
  passageAt = null,
}: {
  source: string;
  path: string;
  passage: string | null;
  /** Where the passage sits in the extracted text, 0 to 1: the page to search first. */
  passageAt?: number | null;
}) {
  const [native, setNative] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [citedPage, setCitedPage] = useState<number | null>(null);

  if (native || failure) {
    return (
      <NativePdf
        source={source}
        path={path}
        failure={failure}
        citedPage={citedPage}
        onHighlights={() => setNative(false)}
      />
    );
  }
  return (
    <PdfPages
      source={source}
      path={path}
      passage={passage}
      passageAt={passageAt}
      onCitedPage={setCitedPage}
      onNative={() => setNative(true)}
      onFail={setFailure}
    />
  );
}

/**
 * The browser's own viewer, in a frame: it needs the whole file as an object
 * URL (a frame cannot send the token), so that download happens only here.
 */
function NativePdf({
  source,
  path,
  failure,
  citedPage,
  onHighlights,
}: {
  source: string;
  path: string;
  failure: string | null;
  citedPage: number | null;
  onHighlights: () => void;
}) {
  const { url, error } = useObjectUrl(
    () => api.documentRawObjectUrl({ source, path, mimeType: "application/pdf" }),
    `${source}/${path}`,
  );
  if (error) return <p className="error small viewer-pad">The PDF could not be loaded: {error}</p>;
  if (!url) return <ViewerLoading label="Loading the PDF…" />;
  return (
    <div className="pdf-shell">
      <div className="pdf-bar">
        {failure ? (
          <span className="muted small">This PDF could not be drawn with highlights ({failure}).</span>
        ) : (
          <button type="button" className="btn btn--small" onClick={onHighlights}>
            Show highlights
          </button>
        )}
        {citedPage ? <span className="muted small">cited passage on page {citedPage}</span> : null}
      </div>
      {/* `#page=` opens the browser's viewer at the cited page, where it
          honours it; the passage itself cannot be marked in there. */}
      <iframe className="pdf-view" src={citedPage ? `${url}#page=${citedPage}` : url} title={path} />
    </div>
  );
}

/**
 * Pages in the order the passage search reads them: the estimated page, then
 * outward one page either side, so a passage is usually found on the first
 * few pages read rather than after every page before it.
 */
export function searchOrder(pages: number, at: number | null): number[] {
  if (at === null || !Number.isFinite(at)) return Array.from({ length: pages }, (_, i) => i + 1);
  const start = Math.min(pages, Math.max(1, Math.round(at * (pages - 1)) + 1));
  const order = [start];
  for (let step = 1; order.length < pages; step += 1) {
    if (start + step <= pages) order.push(start + step);
    if (start - step >= 1) order.push(start - step);
  }
  return order;
}

function PdfPages({
  source,
  path,
  passage,
  passageAt,
  onCitedPage,
  onNative,
  onFail,
}: {
  source: string;
  path: string;
  passage: string | null;
  passageAt: number | null;
  onCitedPage: (page: number | null) => void;
  onNative: () => void;
  onFail: (reason: string) => void;
}) {
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null);
  /** Page 1's size at scale 1: every placeholder is drawn at it until its page renders. */
  const [base, setBase] = useState<{ width: number; height: number } | null>(null);
  const [fitWidth, setFitWidth] = useState(0);
  const [zoom, setZoom] = useState<number | "fit">("fit");
  const [target, setTarget] = useState<number | null>(null);
  const [locating, setLocating] = useState(Boolean(passage));
  const stageRef = useRef<HTMLDivElement>(null);
  const key = `${source}/${path}`;

  useEffect(() => {
    let cancelled = false;
    let loaded: PDFDocumentProxy | null = null;
    (async () => {
      try {
        const { loadPdf } = await import("./pdfjs");
        loaded = await loadPdf(api.documentRawSource({ source, path }));
        if (cancelled) {
          void loaded.destroy();
          return;
        }
        const first = await loaded.getPage(1);
        const viewport = first.getViewport({ scale: 1 });
        if (cancelled) return;
        setBase({ width: viewport.width, height: viewport.height });
        setDoc(loaded);
      } catch (reason) {
        if (!cancelled) onFail(reason instanceof Error ? reason.message : String(reason));
      }
    })();
    return () => {
      cancelled = true;
      if (loaded) void loaded.destroy();
    };
    // `onFail` is a state setter; the document is a function of its path alone.
  }, [key]);

  // Which page the cited passage starts on: read page texts in order (no
  // drawing) until one holds it. The page is then drawn and scrolled to, and
  // the highlight finds the passage in its text layer.
  useEffect(() => {
    if (!doc || !passage) return;
    let cancelled = false;
    const needle = normalizeForMatch(passage);
    setLocating(true);
    (async () => {
      // Pages are read outward from where the passage sits in the extracted
      // text, and the one most of the passage's anchors agree on wins: a
      // sentence repeated on every page must not pull the passage elsewhere,
      // and a perfect match ends the search. Reading from page 1 cost every
      // page before the passage -- seconds for a book, and with range loading
      // every one of those pages' bytes too.
      const texts = new Map<number, string>();
      const pageText = async (number: number) => {
        let text = texts.get(number);
        if (text === undefined) {
          const content = await (await doc.getPage(number)).getTextContent();
          text = normalizeForMatch(content.items.map((item) => ("str" in item ? item.str : "")).join(" "));
          texts.set(number, text);
        }
        return text;
      };
      let best: { page: number; score: number } | null = null;
      for (const number of searchOrder(doc.numPages, passageAt)) {
        if (cancelled) break;
        // The previous page's tail rides along, so a passage that starts at
        // the foot of one page and runs onto the next is still found.
        const carried = number > 1 ? (await pageText(number - 1)).slice(-needle.length) : "";
        const match = matchPassage(carried + (await pageText(number)), needle);
        if (match && (!best || match.score > best.score)) {
          best = { page: match.start < carried.length ? number - 1 : number, score: match.score };
          if (match.score === match.anchors) break;
        }
      }
      if (!cancelled) {
        setTarget(best?.page ?? null);
        onCitedPage(best?.page ?? null);
        setLocating(false);
      }
    })().catch(() => {
      if (!cancelled) setLocating(false);
    });
    return () => {
      cancelled = true;
    };
  }, [doc, passage, passageAt]);

  // Fit-to-width follows the pane: the viewer is resized with the tiles.
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const measure = () => setFitWidth(stage.clientWidth);
    measure();
    const observer = new ResizeObserver(() => measure());
    observer.observe(stage);
    return () => observer.disconnect();
  }, [doc]);

  // Keyed on the document too: the stage exists only once it has loaded.
  // And held until the page is known, so a lookalike on page 1 is never
  // highlighted (and scrolled to) on the way.
  usePassageHighlight(stageRef, locating ? null : passage, `${key}:${doc ? doc.numPages : "loading"}`);

  const fit = base && fitWidth > 0 ? Math.max(MIN_ZOOM, (fitWidth - 32) / base.width) : 1;
  // Quantized, so a pane resized by a pixel does not redraw every page.
  const scale = zoom === "fit" ? Math.round(fit * 20) / 20 : zoom;

  // A new scale (a zoom, or the tile resized or moved) keeps the reader at the
  // same place in the document: every page scales together, so the scroll
  // position as a fraction of the whole is the place.
  const scrollRatio = useRef(0);
  useLayoutEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const remember = () => {
      scrollRatio.current = stage.scrollHeight ? stage.scrollTop / stage.scrollHeight : 0;
    };
    stage.addEventListener("scroll", remember, { passive: true });
    return () => stage.removeEventListener("scroll", remember);
  }, [doc]);
  useLayoutEffect(() => {
    const stage = stageRef.current;
    if (stage && scrollRatio.current) stage.scrollTop = scrollRatio.current * stage.scrollHeight;
  }, [scale]);

  if (!doc || !base) return <ViewerLoading label="Drawing the PDF…" />;
  const step = (factor: number) =>
    setZoom((current) => {
      const from = current === "fit" ? scale : current;
      return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Math.round(from * factor * 100) / 100));
    });

  return (
    <div className="pdf-shell">
      <div className="pdf-bar">
        <button
          type="button"
          className={`btn btn--small${zoom === "fit" ? " btn--active" : ""}`}
          onClick={() => setZoom("fit")}
        >
          Fit width
        </button>
        <button type="button" className="btn btn--small btn--icon" aria-label="Zoom out" onClick={() => step(1 / 1.2)}>
          −
        </button>
        <button type="button" className="btn btn--small btn--icon" aria-label="Zoom in" onClick={() => step(1.2)}>
          +
        </button>
        <span className="muted small">
          {Math.round(scale * 100)}% · {doc.numPages} page{doc.numPages === 1 ? "" : "s"}
        </span>
        {passage ? (
          <span className="muted small">
            {locating ? "finding the cited passage…" : target ? `· cited passage on page ${target}` : "· cited passage not located on a page"}
          </span>
        ) : null}
        <span className="viewer__spacer" />
        <button
          type="button"
          className="btn btn--small btn--ghost"
          onClick={onNative}
          title="Open in the browser's own PDF viewer, for printing and searching"
        >
          Browser viewer
        </button>
      </div>
      <div className="pdf-stage" ref={stageRef}>
        {Array.from({ length: doc.numPages }, (_, index) => (
          <PdfPage
            key={index + 1}
            doc={doc}
            number={index + 1}
            scale={scale}
            placeholder={base}
            target={target === index + 1}
            stageRef={stageRef}
          />
        ))}
      </div>
    </div>
  );
}

interface DrawSignal {
  cancelled: boolean;
  onCancel?: () => void;
}

function PdfPage({
  doc,
  number,
  scale,
  placeholder,
  target,
  stageRef,
}: {
  doc: PDFDocumentProxy;
  number: number;
  scale: number;
  placeholder: { width: number; height: number };
  target: boolean;
  stageRef: RefObject<HTMLDivElement>;
}) {
  const pageRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textRef = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const [size, setSize] = useState<{ scale: number; width: number; height: number } | null>(null);

  useEffect(() => {
    const page = pageRef.current;
    if (!page) return;
    const observer = new IntersectionObserver(
      (entries) => setVisible(entries.some((entry) => entry.isIntersecting)),
      { root: stageRef.current, rootMargin: RENDER_MARGIN },
    );
    observer.observe(page);
    return () => observer.disconnect();
  }, [stageRef]);

  // The cited page is brought into view before it is drawn; drawing it is
  // what the intersection then triggers.
  useEffect(() => {
    if (target) pageRef.current?.scrollIntoView({ block: "start" });
  }, [target]);

  const draw = useCallback(async (signal: DrawSignal) => {
    const canvas = canvasRef.current;
    const textDiv = textRef.current;
    if (!canvas || !textDiv) return;
    const page = await doc.getPage(number);
    if (signal.cancelled) return;
    const viewport = page.getViewport({ scale });
    setSize({ scale, width: viewport.width, height: viewport.height });
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.floor(viewport.width * ratio);
    canvas.height = Math.floor(viewport.height * ratio);
    const context = canvas.getContext("2d");
    if (!context) return;
    const task = page.render({
      canvasContext: context,
      viewport,
      transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : undefined,
    });
    const { TextLayer } = await import("./pdfjs");
    const layer = new TextLayer({ textContentSource: page.streamTextContent(), container: textDiv, viewport });
    textDiv.replaceChildren();
    const stop = () => {
      task.cancel();
      layer.cancel();
    };
    signal.onCancel = stop;
    try {
      await Promise.all([task.promise, layer.render()]);
    } catch {
      /* cancelled by a newer draw or by unmounting */
    }
  }, [doc, number, scale]);

  useEffect(() => {
    if (!visible && !target) {
      // A page far out of view gives its pixels back: a long PDF drawn whole
      // at screen resolution is gigabytes of canvas.
      const canvas = canvasRef.current;
      if (canvas) {
        canvas.width = 0;
        canvas.height = 0;
      }
      textRef.current?.replaceChildren();
      return;
    }
    const signal: DrawSignal = { cancelled: false };
    void draw(signal);
    return () => {
      signal.cancelled = true;
      signal.onCancel?.();
    };
  }, [visible, target, draw]);

  const shown = size && size.scale === scale ? size : { width: placeholder.width * scale, height: placeholder.height * scale };
  return (
    <div
      ref={pageRef}
      className="pdf-page"
      data-page={number}
      style={{ width: shown.width, height: shown.height, "--scale-factor": scale } as CSSProperties}
    >
      <canvas ref={canvasRef} style={{ width: shown.width, height: shown.height }} />
      <div ref={textRef} className="textLayer" />
    </div>
  );
}
