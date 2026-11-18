import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import type { TaxonomyIssue, TaxonomyNode } from "../api/types";

/**
 * The extracted outline of a source's documents, and any numbering defects
 * found in them.
 *
 * Read from `GET /taxonomy`, which reads the emitted `heading` nodes rather
 * than re-parsing the files — so what shows here is exactly what the index and
 * the chunks' `heading_path` agree on. If they ever disagree with the document,
 * this panel is where you see it.
 */
export function TaxonomyOutline({ sourceName }: { sourceName: string }) {
  const outline = useQuery({
    queryKey: ["taxonomy", sourceName],
    queryFn: () => api.taxonomy({ source: sourceName }),
  });

  if (outline.isLoading) return <p className="muted small">Reading the outline…</p>;
  if (outline.isError) return <p className="error small">Could not read the outline.</p>;

  const documents = outline.data?.documents ?? [];
  if (!documents.length) {
    return (
      <p className="muted small">
        No sections indexed. Taxonomy extraction has to be on for this source <em>and</em> the
        source synced after that was set.
      </p>
    );
  }

  return (
    <div className="taxonomy-outline">
      {outline.data?.truncated ? (
        <p className="muted small">Showing the first 2000 sections.</p>
      ) : null}
      {documents.map((document) => (
        <section key={document.relative_path} className="taxonomy-outline__doc">
          <h4>
            {document.relative_path}{" "}
            <span className="muted small">{document.heading_count} sections</span>
          </h4>
          {document.issues.length ? <IssueList issues={document.issues} /> : null}
          <NodeList nodes={document.tree} />
        </section>
      ))}
    </div>
  );
}

function NodeList({ nodes }: { nodes: TaxonomyNode[] }) {
  if (!nodes.length) return null;
  return (
    <ul className="taxonomy-tree">
      {nodes.map((node) => (
        <li key={`${node.line}:${node.path}`}>
          {node.number ? <span className="taxonomy-tree__number">{node.number}</span> : null}
          <span>{node.title}</span>
          <NodeList nodes={node.children} />
        </li>
      ))}
    </ul>
  );
}

/**
 * Gaps, duplicates and backwards numbering. For a contract or a procedure "is
 * anything missing?" is the question people actually ask of the document, so it
 * is surfaced next to the outline rather than buried in the API response.
 */
function IssueList({ issues }: { issues: TaxonomyIssue[] }) {
  return (
    <ul className="taxonomy-issues">
      {issues.map((issue, index) => (
        <li key={`${issue.kind}:${issue.line}:${index}`}>
          <span className={`taxonomy-issues__kind taxonomy-issues__kind--${issue.kind}`}>
            {issue.kind.replace("_", " ")}
          </span>{" "}
          {describeIssue(issue)}
        </li>
      ))}
    </ul>
  );
}

export function describeIssue(issue: TaxonomyIssue): string {
  const after = issue.after ?? "?";
  const at = issue.at ?? "?";
  if (issue.kind === "gap") {
    const listed = issue.missing ?? [];
    const unlisted = Math.max(0, (issue.missing_count ?? listed.length) - listed.length);
    const missing = listed.join(", ") + (unlisted ? `, … ${unlisted.toLocaleString()} more` : "");
    return `${after} → ${at}${listed.length ? ` (missing ${missing})` : ""}`;
  }
  if (issue.kind === "duplicate") return `${at} appears twice, after ${after}`;
  return `${at} comes after ${after}`;
}
