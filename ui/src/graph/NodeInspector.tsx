import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { api } from "../api/client";
import type { GraphLink, GraphNode } from "../api/types";
import { colorForNode } from "./graphStyles";

interface NodeInspectorProps {
  selectedId: string | null;
  nodes: GraphNode[];
  links: GraphLink[];
  onExpand: (nodeId: string, depth?: number) => void;
  onShowChunks: (nodeId: string) => void;
  onPivot: (nodeId: string) => void;
  /** Open a document node's file in the viewer above the chat. */
  onOpenFile?: (source: string, path: string) => void;
}

type Tab = "overview" | "relationships" | "content";
type View = "rich" | "raw";

/** Node types whose indexed text is Markdown worth rendering. */
const MARKDOWN_TYPES = new Set(["markdown_note", "document", "memory_record"]);

/**
 * Render Markdown to sanitized HTML.
 *
 * Indexed content is whatever was in the user's files, so it is untrusted
 * input as far as the browser is concerned — a note containing a <script> tag
 * or an onerror attribute must not execute just because someone previewed it.
 * `marked` is deliberately run with no HTML passthrough of its own and the
 * result is sanitized before it reaches dangerouslySetInnerHTML.
 */
function renderMarkdown(text: string): string {
  const html = marked.parse(text, { async: false, gfm: true, breaks: false }) as string;
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
}

const CONTENT_TYPES = new Set(["file", "document", "markdown_note", "memory_record", "chunk"]);
const ARTIFACT_TYPES = new Set(["file", "document", "markdown_note", "memory_record"]);
/** Nodes that are a whole file the viewer can open. Memory records are listed by the memory page. */
const OPENABLE_TYPES = new Set(["file", "document", "markdown_note", "image", "audio"]);

// Internal bookkeeping the user never needs to see.
const HIDDEN_KEYS = new Set(["label", "type", "id", "knowledge_base_id"]);

export function NodeInspector({
  selectedId,
  nodes,
  links,
  onExpand,
  onShowChunks,
  onPivot,
  onOpenFile,
}: NodeInspectorProps) {
  const [tab, setTab] = useState<Tab>("overview");
  const [view, setView] = useState<View>("rich");
  const nodeById = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const node = selectedId ? nodeById.get(selectedId) : undefined;

  const explainQuery = useQuery({
    queryKey: ["explain", selectedId],
    queryFn: () => api.explain(selectedId as string),
    enabled: Boolean(selectedId),
  });

  const contentQuery = useQuery({
    queryKey: ["node-content", selectedId],
    queryFn: () => api.nodeContent(selectedId as string),
    enabled: Boolean(tab === "content" && selectedId && CONTENT_TYPES.has(node?.type ?? "")),
  });

  const relationships = useMemo(() => {
    if (!selectedId) return { outgoing: [], incoming: [] };
    const outgoing = links
      .filter((l) => l.source === selectedId)
      .map((l) => ({ other: l.target, type: l.type ?? "related" }));
    const incoming = links
      .filter((l) => l.target === selectedId)
      .map((l) => ({ other: l.source, type: l.type ?? "related" }));
    return { outgoing, incoming };
  }, [links, selectedId]);

  const body = contentQuery.data?.content || (node?.summary ? String(node.summary) : "");
  // Markdown by node type, or by extension for plain `file` artifacts.
  const isMarkdown =
    MARKDOWN_TYPES.has(node?.type ?? "") ||
    /\.(md|markdown)$/i.test(String(node?.relative_path ?? ""));

  if (!selectedId || !node) {
    return (
      <div className="inspector inspector--empty">
        <p style={{ margin: 0 }}>
          Select a node on the graph, or click a citation in an answer, to inspect its
          identity, relationships and indexed content.
        </p>
      </div>
    );
  }

  return (
    <div className="inspector">
      <div className="inspector__title">
        <span className="dot" style={{ background: colorForNode(node.type) }} />
        <div style={{ minWidth: 0 }}>
          <div className="inspector__label">{node.label ?? node.id}</div>
          <div className="muted small">{node.type}</div>
        </div>
      </div>

      <div className="pane__actions">
        <button className="btn btn--small" onClick={() => onExpand(selectedId)}>
          Expand neighbourhood
        </button>
        {ARTIFACT_TYPES.has(node.type ?? "") ? (
          <button className="btn btn--small" onClick={() => onShowChunks(selectedId)}>
            Show passages on graph
          </button>
        ) : null}
        {onOpenFile && OPENABLE_TYPES.has(node.type ?? "") && node.source_id && node.relative_path ? (
          <button
            className="btn btn--small"
            onClick={() => onOpenFile(String(node.source_id), String(node.relative_path))}
            title="Open this file, rendered, above the chat"
          >
            Open file
          </button>
        ) : null}
      </div>

      <div className="tabs">
        <button
          className={tab === "overview" ? "tab active" : "tab"}
          onClick={() => setTab("overview")}
        >
          Overview
        </button>
        <button
          className={tab === "relationships" ? "tab active" : "tab"}
          onClick={() => setTab("relationships")}
        >
          Links
        </button>
        <button
          className={tab === "content" ? "tab active" : "tab"}
          onClick={() => setTab("content")}
        >
          Content
        </button>
      </div>

      {tab === "overview" && (
        <div className="kv">
          {flattenAttributes(node).map(([key, value]) => (
            <div className="kv__row" key={key}>
              <span className="kv__key">{key.replace(/_/g, " ")}</span>
              <span className="kv__value">{value}</span>
            </div>
          ))}
          {explainQuery.data?.explanation && (
            <p className="explanation">{explainQuery.data.explanation}</p>
          )}
        </div>
      )}

      {tab === "relationships" && (
        <div>
          <RelGroup
            title="Outgoing"
            items={relationships.outgoing}
            nodeById={nodeById}
            onPivot={onPivot}
          />
          <RelGroup
            title="Incoming"
            items={relationships.incoming}
            nodeById={nodeById}
            onPivot={onPivot}
          />
        </div>
      )}

      {tab === "content" && (
        <div className="inspector__tabbody">
          {contentQuery.isLoading ? (
            <p className="muted small">
              <span className="spinner" /> Loading content…
            </p>
          ) : body ? (
            <>
              {isMarkdown ? (
                <div className="content-toolbar">
                  <button
                    className={view === "rich" ? "btn btn--small active" : "btn btn--small"}
                    onClick={() => setView("rich")}
                  >
                    Rendered
                  </button>
                  <button
                    className={view === "raw" ? "btn btn--small active" : "btn btn--small"}
                    onClick={() => setView("raw")}
                  >
                    Raw
                  </button>
                </div>
              ) : null}
              {isMarkdown && view === "rich" ? (
                <div
                  className="content-block content-block--rich"
                  // Sanitized in renderMarkdown; indexed file content is
                  // untrusted input.
                  dangerouslySetInnerHTML={{ __html: renderMarkdown(body) }}
                />
              ) : (
                <pre className="content-block">{body}</pre>
              )}
            </>
          ) : (
            <p className="muted small">No indexed content for this node type.</p>
          )}
        </div>
      )}
    </div>
  );
}

function RelGroup({
  title,
  items,
  nodeById,
  onPivot,
}: {
  title: string;
  items: { other: string; type: string }[];
  nodeById: Map<string, GraphNode>;
  onPivot: (id: string) => void;
}) {
  if (items.length === 0) {
    return (
      <div className="rel-group">
        <div className="rel-group__title">{title}</div>
        <span className="muted small">none</span>
      </div>
    );
  }
  return (
    <div className="rel-group">
      <div className="rel-group__title">
        {title} · {items.length}
      </div>
      {items.map((item, index) => (
        <button
          key={`${item.type}-${item.other}-${index}`}
          className="rel-item"
          onClick={() => onPivot(item.other)}
          title={item.other}
        >
          <span className="rel-item__type">{item.type}</span>
          <span className="rel-item__node">{nodeById.get(item.other)?.label ?? item.other}</span>
        </button>
      ))}
    </div>
  );
}

/**
 * Node attributes as flat, readable rows.
 *
 * Enrichment attaches nested objects (`provenance`) and arrays (`concept
 * terms`); dumping raw JSON into the panel makes the useful fields — path,
 * hash, branch — hard to find. Nested objects are flattened one level and
 * arrays are joined, with keys that duplicate an already-shown row dropped.
 */
function flattenAttributes(node: GraphNode): [string, string][] {
  const rows: [string, string][] = [];
  const emitted = new Set<string>();

  const push = (key: string, value: unknown) => {
    if (value === null || value === undefined || value === "") return;
    if (HIDDEN_KEYS.has(key) || emitted.has(key)) return;
    if (Array.isArray(value)) {
      if (value.length === 0) return;
      emitted.add(key);
      rows.push([key, value.map((item) => String(item)).join(", ")]);
      return;
    }
    if (typeof value === "object") return; // handled by the nested pass below
    emitted.add(key);
    rows.push([key, String(value)]);
  };

  for (const [key, value] of Object.entries(node)) push(key, value);
  for (const [key, value] of Object.entries(node)) {
    if (value === null || typeof value !== "object" || Array.isArray(value)) continue;
    for (const [subKey, subValue] of Object.entries(value as Record<string, unknown>)) {
      // `provenance.path` and `path` are the same string — show it once.
      if (emitted.has(subKey)) continue;
      push(subKey, subValue);
    }
    void key;
  }
  return rows;
}
