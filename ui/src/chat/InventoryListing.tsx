import { useState } from "react";
import { api } from "../api/client";
import type {
  ChatAnswer,
  InventoryDocument,
  InventoryLink,
  InventoryPage,
} from "../api/types";

/** Rows a "Load all" may pull into one answer before it stops and says so. */
const LOAD_ALL_CAP = 5000;

type Inventory = NonNullable<ChatAnswer["inventory"]>;

/**
 * A paged inventory listing (documents, recent documents, document links),
 * drawn as a table that scrolls inside the answer and grows in place.
 *
 * The answer's Markdown already holds the first page — that is what an agent
 * over MCP reads, with the question that asks for the next page. Here the
 * same rows come from `inventory.result`, and "Load more" calls the endpoint
 * the answer named (`GET /documents` or `GET /documents/links`, with the same
 * filters) rather than asking a new question, so paging through a thousand
 * documents does not fill the conversation with a thousand-row history.
 */
export function InventoryListing({
  inventory,
  lead,
  onSelect,
}: {
  inventory: Inventory;
  lead: string;
  onSelect: (nodeId: string | undefined) => void;
}) {
  const page = inventory.page as InventoryPage;
  const first = inventory.result as {
    documents?: InventoryDocument[];
    links?: InventoryLink[];
    pagination?: { offset?: number };
  };
  const isLinks = page.endpoint === "/documents/links";
  const [documents, setDocuments] = useState<InventoryDocument[]>(first.documents ?? []);
  const [links, setLinks] = useState<InventoryLink[]>(first.links ?? []);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const start = first.pagination?.offset ?? 0;
  const loaded = isLinks ? links.length : documents.length;
  const remaining = Math.max(0, page.total - start - loaded);

  const fetchMore = async (all: boolean) => {
    setLoading(true);
    setError(null);
    try {
      let offset = start + loaded;
      let pulled = 0;
      do {
        const next = await api.inventoryPage(page.endpoint, page.params, offset, page.size);
        const rows = isLinks ? next.links ?? [] : next.documents ?? [];
        if (isLinks) setLinks((prev) => [...prev, ...(rows as InventoryLink[])]);
        else setDocuments((prev) => [...prev, ...(rows as InventoryDocument[])]);
        offset += rows.length;
        pulled += rows.length;
        if (!next.pagination.has_more || rows.length === 0) break;
      } while (all && pulled < LOAD_ALL_CAP);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="inv">
      <div className="inv__head">
        <span>{lead}</span>
        <span className="inv__count">
          {start + 1}–{start + loaded} of {page.total}
        </span>
      </div>
      <div className="inv__scroll">
        <table className="inv__table">
          <thead>
            {isLinks ? (
              <tr>
                <th>From</th>
                <th>Edge</th>
                <th>To</th>
              </tr>
            ) : (
              <tr>
                <th>Source</th>
                <th>Path</th>
                <th>Size</th>
                <th>Indexed</th>
              </tr>
            )}
          </thead>
          <tbody>
            {isLinks
              ? links.map((link, index) => (
                  <tr key={`${link.from.id}-${link.to.id}-${index}`}>
                    <td>
                      <PathButton doc={link.from} onSelect={onSelect} />
                    </td>
                    <td className="inv__edge">
                      {link.edge_types.join(", ")}
                      {link.cross_source ? <span className="pill">cross-source</span> : null}
                    </td>
                    <td>
                      <PathButton doc={link.to} onSelect={onSelect} />
                    </td>
                  </tr>
                ))
              : documents.map((doc) => (
                  <tr key={doc.id}>
                    <td>{doc.source}</td>
                    <td>
                      <PathButton doc={doc} onSelect={onSelect} bare />
                    </td>
                    <td>{size(doc.size_bytes)}</td>
                    <td>{(doc.last_indexed_at ?? "—").replace("T", " ").slice(0, 16)}</td>
                  </tr>
                ))}
          </tbody>
        </table>
      </div>
      <div className="inv__controls">
        {remaining > 0 ? (
          <>
            <button
              type="button"
              className="btn btn--small"
              disabled={loading}
              onClick={() => fetchMore(false)}
            >
              {loading ? "Loading…" : `Load ${Math.min(page.size, remaining)} more`}
            </button>
            <button
              type="button"
              className="btn btn--small"
              disabled={loading}
              onClick={() => fetchMore(true)}
              title={`Pull the remaining rows into this table (up to ${LOAD_ALL_CAP})`}
            >
              Load all {remaining > LOAD_ALL_CAP ? `(first ${LOAD_ALL_CAP})` : remaining}
            </button>
          </>
        ) : (
          <span className="inv__done">All {page.total} shown.</span>
        )}
        {error ? <span className="error">{error}</span> : null}
      </div>
    </div>
  );
}

function PathButton({
  doc,
  onSelect,
  bare = false,
}: {
  doc: { id: string; source: string; path: string };
  onSelect: (nodeId: string | undefined) => void;
  bare?: boolean;
}) {
  return (
    <button
      type="button"
      className="inv__path"
      onClick={() => onSelect(doc.id)}
      title="Show this document in the graph"
    >
      {bare ? doc.path : `${doc.source}/${doc.path}`}
    </button>
  );
}

function size(bytes: number | null | undefined): string {
  let value = Number(bytes ?? 0);
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (value < 1024 || unit === "GB") return unit === "B" ? `${value} B` : `${value.toFixed(1)} ${unit}`;
    value /= 1024;
  }
  return `${value.toFixed(1)} GB`;
}

/**
 * The answer's Markdown, split around the table the listing draws itself: the
 * lead sentence (without its "showing 1–50", which loading more makes stale),
 * the blocks before the rows (a links summary), and the footer notes. The
 * first page's rows and the "Page 1 of N" line are dropped.
 */
export function listingText(answer: string): { lead: string; body: string; footer: string } {
  const blocks = answer.split("\n\n");
  const lead = (blocks[0] ?? "")
    .replace(/\*\*|`/g, "")
    .replace(/ — showing [^.]*\./, ".");
  const kept = blocks
    .slice(1)
    .filter(
      (block) =>
        !/^\| (Source|From) \| (Path|Edge) \|/.test(block) &&
        !/^Showing /.test(block) &&
        !/^Page \d+ of \d+/.test(block),
    );
  return {
    lead,
    body: kept.filter((block) => !block.startsWith("_")).join("\n\n"),
    footer: kept.filter((block) => block.startsWith("_")).join("\n\n"),
  };
}
