import { useCallback, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { DocumentContent, DocumentViewer } from "../api/types";
import { CodeView } from "./views/CodeView";
import { DataTable, SpreadsheetView, StructuredTree, parseDelimited, parseStructured } from "./views/DataViews";
import { MarkdownView, splitSections } from "./views/MarkdownView";
import { AudioView, HtmlView, ImageView, SvgView, ViewerLoading } from "./views/MediaViews";
import { PdfView } from "./views/PdfView";
import { NotebookView, parseNotebook } from "./views/NotebookView";
import { ProseView, SlidesView } from "./views/ProseViews";
import { fileIcon, formatBytes } from "./fileIcons";
import { OmitToggle } from "./OmitToggle";
import { passageText, usePassageHighlight } from "./passage";
import { ScopeToggle } from "./ScopeToggle";

/** The two ways most formats can be shown: as they render, or as their text. */
type Mode = "rendered" | "source";

/** What the "rendered" mode is called for each viewer; absent means source only. */
const RENDERED_LABEL: Partial<Record<DocumentViewer, string>> = {
  markdown: "Rendered",
  json: "Tree",
  yaml: "Tree",
  table: "Table",
  notebook: "Notebook",
  html: "Preview",
  svg: "Image",
  pdf: "PDF",
  spreadsheet: "Sheets",
  slides: "Slides",
  document: "Reading",
};

/**
 * Views whose cited passage is highlighted by matching it in their rendered
 * text. The source view bands the cited lines itself, the PDF view finds the
 * page first and highlights within it, and a frame (HTML) is out of reach.
 */
const NO_PASSAGE_HIGHLIGHT = new Set<string>(["source", "pdf", "html", "svg", "image"]);

/** Views that can mark matches for the find box. */
const FINDABLE = new Set<string>(["source", "json", "yaml", "table", "spreadsheet", "slides", "document", "pdf-text"]);

interface OutlineItem {
  key: string;
  title: string;
  level: number;
  line?: number | null;
  section?: string;
}

/**
 * One file, rendered as well as its format allows.
 *
 * The region decides the viewer (`read_document`'s `viewer`), so the browser,
 * the chat and an agent agree on what a file is. Every rendered view has a
 * source view one click away, because a renderer is an interpretation and the
 * reader is entitled to the text it interpreted. Opened from a citation, the
 * cited lines are banded and scrolled to.
 */
export function FileViewer({
  source,
  path,
  chunkId,
  anchor,
  onClose,
  onOpenPath,
  onBrowse,
  onAsk,
  onShowInGraph,
}: {
  source: string;
  path: string;
  chunkId?: string | null;
  anchor?: string | null;
  onClose: () => void;
  onOpenPath: (path: string, anchor?: string) => void;
  onBrowse: (folder: string) => void;
  onAsk: (text: string) => void;
  onShowInGraph: (nodeId: string) => void;
}) {
  const content = useQuery({
    queryKey: ["document-content", source, path, chunkId ?? null],
    queryFn: () => api.documentContent({ source, path, chunkId }),
  });

  if (content.isLoading) return <ViewerLoading label={`Opening ${path}…`} />;
  if (content.isError || !content.data) {
    return (
      <div className="viewer viewer--empty">
        <p className="error small">{(content.error as Error | null)?.message ?? "This file could not be opened."}</p>
        <button type="button" className="btn btn--small" onClick={onClose}>
          Close
        </button>
      </div>
    );
  }
  return (
    <LoadedViewer
      key={`${source}/${path}/${chunkId ?? ""}`}
      data={content.data}
      anchor={anchor ?? null}
      onClose={onClose}
      onOpenPath={onOpenPath}
      onBrowse={onBrowse}
      onAsk={onAsk}
      onShowInGraph={onShowInGraph}
    />
  );
}

function LoadedViewer({
  data,
  anchor,
  onClose,
  onOpenPath,
  onBrowse,
  onAsk,
  onShowInGraph,
}: {
  data: DocumentContent;
  anchor: string | null;
  onClose: () => void;
  onOpenPath: (path: string, anchor?: string) => void;
  onBrowse: (folder: string) => void;
  onAsk: (text: string) => void;
  onShowInGraph: (nodeId: string) => void;
}) {
  const { document: doc, viewer } = data;
  const text = data.content ?? "";
  // Images and recordings have only their rendered view; everything else with
  // a renderer also has its text one click away.
  const mediaOnly = viewer === "image" || viewer === "audio";
  const renderable = Boolean(RENDERED_LABEL[viewer]);
  const renderedLabel =
    viewer === "pdf" && !data.raw.available ? "Reading" : (RENDERED_LABEL[viewer] ?? "Rendered");
  const [mode, setMode] = useState<Mode>(renderable || mediaOnly ? "rendered" : "source");
  const [wrap, setWrap] = useState(viewer === "text" || viewer === "document");
  const [find, setFind] = useState("");
  const [findIndex, setFindIndex] = useState(0);
  const [findCount, setFindCount] = useState(0);
  const [showOutline, setShowOutline] = useState(false);
  const [jump, setJump] = useState<{ item: OutlineItem; nonce: number } | null>(null);
  const [copied, setCopied] = useState(false);
  const [focusCleared, setFocusCleared] = useState(false);
  const focus = focusCleared ? null : data.focus;
  // The cited passage itself — its whole line span, not the 400-character
  // excerpt — so a rendered view can highlight what was cited.
  const passage = useMemo(() => passageText(text, focus), [text, focus]);
  const contentRef = useRef<HTMLDivElement>(null);

  const outline = useMemo<OutlineItem[]>(() => {
    if (viewer === "markdown" && text) {
      return splitSections(text)
        .sections.filter((section) => section.level > 0)
        .map((section) => ({
          key: section.id,
          title: section.title,
          level: section.level,
          line: section.line,
          section: section.id,
        }));
    }
    return data.outline.map((entry, index) => ({
      key: `${index}-${entry.path}`,
      title: entry.title,
      level: entry.level,
      line: entry.line,
    }));
  }, [viewer, text, data.outline]);

  const view = mode === "source" ? "source" : viewer === "pdf" && !data.raw.available ? "pdf-text" : viewer;
  const findable = FINDABLE.has(view) || (mode === "source" && Boolean(text));
  const lineFocus =
    focus?.start_line && focus.end_line ? { start: focus.start_line, end: focus.end_line } : null;

  const onFindCount = useCallback((count: number) => setFindCount(count), []);
  usePassageHighlight(contentRef, NO_PASSAGE_HIGHLIGHT.has(view) ? null : passage, view);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard refused: nothing to undo */
    }
  };

  const download = async () => {
    if (!data.raw.mime_type) return;
    const url = await api.documentRawObjectUrl({ source: doc.source, path: doc.path, mimeType: data.raw.mime_type });
    const anchor = window.document.createElement("a");
    anchor.href = url;
    anchor.download = doc.name;
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
  };

  const crumbs = data.breadcrumbs;

  return (
    <div className="viewer">
      <header className="viewer__header">
        <div className="viewer__title-row">
          <span className="viewer__icon" aria-hidden>
            {fileIcon(viewer)}
          </span>
          <div className="viewer__title">
            <div className="viewer__name" title={`${doc.source}/${doc.path}`}>
              {doc.name}
            </div>
            <nav className="viewer__crumbs" aria-label="Location">
              {crumbs.map((crumb, index) => (
                <span key={`${crumb.path}-${index}`}>
                  {index > 0 ? <span className="viewer__sep">/</span> : null}
                  <button type="button" className="link-button" onClick={() => onBrowse(crumb.path)}>
                    {crumb.name}
                  </button>
                </span>
              ))}
            </nav>
          </div>
          <ScopeToggle source={doc.source} path={doc.path} kind="file" />
          <OmitToggle source={doc.source} path={doc.path} />
          <button type="button" className="btn btn--small btn--icon btn--ghost" onClick={onClose} aria-label="Close file" title="Close file">
            ✕
          </button>
        </div>
        <div className="viewer__meta">
          <span className="pill">{viewer}</span>
          {data.language && data.language !== viewer ? <span className="pill">{data.language}</span> : null}
          {doc.size_bytes != null ? <span>{formatBytes(doc.size_bytes)}</span> : null}
          {doc.last_indexed_at ? <span>indexed {doc.last_indexed_at.replace("T", " ").slice(0, 16)}</span> : null}
          {data.content_origin === "chunks" ? (
            <span
              className="pill pill--warn"
              title="The original file is not readable from this replica, or it changed since it was indexed. This is the text the index holds."
            >
              reassembled from the index
            </span>
          ) : null}
          {data.content_origin === "extracted" && text ? (
            <span className="pill" title="The text extracted from this file when it was indexed">
              extracted text
            </span>
          ) : null}
          {data.truncated ? <span className="pill pill--warn">truncated</span> : null}
        </div>
        <div className="viewer__toolbar">
          {renderable && text !== "" ? (
            <div className="segmented" role="group" aria-label="View">
              <button
                type="button"
                className={mode === "rendered" ? "active" : undefined}
                onClick={() => setMode("rendered")}
              >
                {renderedLabel}
              </button>
              <button
                type="button"
                className={mode === "source" ? "active" : undefined}
                onClick={() => setMode("source")}
              >
                {viewer === "pdf" || viewer === "spreadsheet" || viewer === "slides" || viewer === "document"
                  ? "Text"
                  : "Source"}
              </button>
            </div>
          ) : null}
          {outline.length > 0 ? (
            <button
              type="button"
              className={`btn btn--small${showOutline ? " btn--active" : ""}`}
              aria-pressed={showOutline}
              onClick={() => setShowOutline((value) => !value)}
            >
              Outline · {outline.length}
            </button>
          ) : null}
          {mode === "source" && text ? (
            <button
              type="button"
              className={`btn btn--small${wrap ? " btn--active" : ""}`}
              aria-pressed={wrap}
              onClick={() => setWrap((value) => !value)}
            >
              Wrap
            </button>
          ) : null}
          {findable && text ? (
            <div className="viewer__find">
              <input
                className="text-input"
                value={find}
                placeholder="Find in file"
                aria-label="Find in file"
                onChange={(event) => {
                  setFind(event.target.value);
                  setFindIndex(0);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") setFindIndex((index) => index + (event.shiftKey ? -1 : 1));
                  if (event.key === "Escape") setFind("");
                }}
              />
              {find && view === "source" ? (
                <span className="muted small viewer__find-count">
                  {findCount ? `${(((findIndex % findCount) + findCount) % findCount) + 1}/${findCount}` : "0"}
                </span>
              ) : null}
            </div>
          ) : null}
          <span className="viewer__spacer" />
          {text ? (
            <button type="button" className="btn btn--small btn--ghost" onClick={copy} title="Copy the text">
              {copied ? "Copied" : "Copy"}
            </button>
          ) : null}
          {data.raw.available ? (
            <button type="button" className="btn btn--small btn--ghost" onClick={download} title="Download the original file">
              Download
            </button>
          ) : null}
          <button
            type="button"
            className="btn btn--small btn--ghost"
            onClick={() => onAsk(`@doc ${doc.source}/${doc.path}`)}
            title="Describe this document in the chat: outline, symbols, links in and out"
          >
            Ask
          </button>
          <button
            type="button"
            className="btn btn--small btn--ghost"
            onClick={() => onShowInGraph(doc.id)}
            title="Center the graph on this document"
          >
            Graph
          </button>
        </div>
        {focus ? (
          <div className="viewer__focus">
            <span>
              Opened at the cited passage
              {focus.start_line ? ` · lines ${focus.start_line}–${focus.end_line ?? focus.start_line}` : ""}
              {focus.heading_path ? ` · ${focus.heading_path.split(">").pop()?.trim()}` : ""}
            </span>
            <button type="button" className="link-button" onClick={() => setFocusCleared(true)}>
              clear
            </button>
          </div>
        ) : null}
      </header>

      <div className={`viewer__body${showOutline ? " viewer__body--outline" : ""}`}>
        {showOutline ? (
          <nav className="viewer__outline" aria-label="Outline">
            {outline.map((item) => (
              <button
                type="button"
                key={item.key}
                className="viewer__outline-item"
                style={{ paddingLeft: 8 + Math.max(0, item.level - 1) * 12 }}
                onClick={() => setJump({ item, nonce: Date.now() })}
                title={item.line ? `line ${item.line}` : undefined}
              >
                {item.title}
              </button>
            ))}
          </nav>
        ) : null}
        <div className="viewer__content" ref={contentRef}>
          {renderView({
            view,
            data,
            text,
            wrap,
            find,
            findIndex,
            onFindCount,
            lineFocus,
            focusExcerpt: focus?.excerpt ?? null,
            passage,
            jump,
            anchor,
            onOpenPath,
            onBrowse,
          })}
        </div>
      </div>
    </div>
  );
}

function renderView({
  view,
  data,
  text,
  wrap,
  find,
  findIndex,
  onFindCount,
  lineFocus,
  focusExcerpt,
  passage,
  jump,
  anchor,
  onOpenPath,
  onBrowse,
}: {
  view: string;
  data: DocumentContent;
  text: string;
  wrap: boolean;
  find: string;
  findIndex: number;
  onFindCount: (count: number) => void;
  lineFocus: { start: number; end: number } | null;
  focusExcerpt: string | null;
  passage: string | null;
  jump: { item: OutlineItem; nonce: number } | null;
  anchor: string | null;
  onOpenPath: (path: string, anchor?: string) => void;
  onBrowse: (folder: string) => void;
}) {
  const doc = data.document;
  const source = () => (
    <CodeView
      text={text}
      language={data.language}
      extension={doc.extension}
      focus={lineFocus}
      wrap={wrap}
      find={find}
      findIndex={findIndex}
      onFindCount={onFindCount}
      scrollToLine={jump?.item.line ? { line: jump.item.line, nonce: jump.nonce } : null}
    />
  );
  switch (view) {
    case "markdown":
      return (
        <MarkdownView
          text={text}
          documentId={doc.id}
          source={doc.source}
          path={doc.path}
          focusLine={lineFocus?.start ?? null}
          scrollTo={
            jump?.item.section
              ? { id: jump.item.section, nonce: jump.nonce }
              : anchor
                ? { id: anchor, nonce: 0 }
                : null
          }
          onOpenPath={onOpenPath}
          onBrowse={onBrowse}
        />
      );
    case "json":
    case "yaml": {
      const parsed = parseStructured(text, view);
      if (parsed.error) {
        return (
          <>
            <p className="viewer__notice">Could not parse as {view.toUpperCase()}: {parsed.error}</p>
            {source()}
          </>
        );
      }
      return <StructuredTree value={parsed.value} find={find} />;
    }
    case "table":
      return <DataTable rows={parseDelimited(text, doc.extension === ".tsv" ? "\t" : ",")} find={find} passage={passage} />;
    case "spreadsheet":
      return <SpreadsheetView text={text} find={find} passage={passage} />;
    case "slides":
      return <SlidesView text={text} find={find} />;
    case "notebook":
      return parseNotebook(text) ? <NotebookView text={text} /> : source();
    case "html":
      return <HtmlView html={text} />;
    case "svg":
      return <SvgView text={text} />;
    case "pdf":
      return <PdfView source={doc.source} path={doc.path} passage={passage} />;
    case "pdf-text":
    case "document":
      return (
        <>
          {view === "pdf-text" ? (
            <p className="viewer__notice">
              The original PDF is not readable from this replica, so this is its extracted text.
            </p>
          ) : null}
          <ProseView
            text={text}
            outline={data.outline}
            focusExcerpt={focusExcerpt}
            find={find}
            scrollTo={jump ? { title: jump.item.title, nonce: jump.nonce } : null}
          />
        </>
      );
    case "image":
      return <ImageView documentId={doc.id} name={doc.name} caption={data.content} />;
    case "audio":
      return (
        <AudioView
          source={doc.source}
          path={doc.path}
          mimeType={data.raw.mime_type}
          available={data.raw.available}
          transcript={data.content}
        />
      );
    default:
      return text ? source() : <p className="muted small viewer-pad">This file holds no indexed text.</p>;
  }
}
