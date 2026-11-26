import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { onApiAuthChanged, request } from "@pheasant/ui/api/client";
import type { SourceRecord } from "@pheasant/ui/api/types";
import { usePermissions } from "@pheasant/ui/state/permissions";
import { isGtmCatalog } from "./GtmCatalog";
import { GtmScrollArea } from "./GtmScrollArea";

const GROUPS = {
  official: "Official website",
  jobs: "Job postings",
  news: "News and articles",
  filing: "Filings",
  external: "External sources",
};

type ResearchDocument = { url: string; title: string | null; kind: keyof typeof GROUPS; published: string | null };

export function GtmResearchDocuments({ source, path }: { source: SourceRecord; path: string }) {
  const { can } = usePermissions();
  return can("sources.manage") && isGtmCatalog(source) && /^[a-z0-9][a-z0-9_-]{0,79}$/.test(path)
    ? <Documents source={source.name} company={path} /> : null;
}

function Documents({ source, company }: { source: string; company: string }) {
  const client = useQueryClient();
  const inventory = useQuery({
    queryKey: ["gtm-research-documents", source, company],
    queryFn: ({ signal }) => request<{ documents: ResearchDocument[]; passage_count: number | null }>(
      `/gtm/catalogs/${encodeURIComponent(source)}/companies/${encodeURIComponent(company)}/research-documents`,
      { signal, cache: "no-store" },
    ),
    gcTime: 0,
    retry: false,
  });
  useEffect(() => onApiAuthChanged(() => {
    void client.resetQueries({ queryKey: ["gtm-research-documents"] });
  }), [client]);
  const documents = inventory.data?.documents ?? [];

  return <section className="gtm-taxonomy-documents" aria-label="GTM research documents">
    <h3>Research documents{inventory.data ? ` (${documents.length})` : ""}</h3>
    <p className="muted small">{inventory.data?.passage_count != null
      ? `${inventory.data.passage_count.toLocaleString()} passages in the company research index`
      : "Sources admitted to company research"}</p>
    {inventory.isPending ? <p className="muted small" role="status">Loading research documents…</p> : null}
    {inventory.isError ? <p className="error small" role="status">{inventory.error.message}</p> : null}
    {inventory.isSuccess && !documents.length ? <p className="muted small">No documents are saved for this company.</p> : null}
    <GtmScrollArea label="Research document list">{Object.entries(GROUPS).map(([kind, label]) => {
      const rows = documents.filter((document) => document.kind === kind);
      return rows.length ? <div key={kind} className="gtm-taxonomy-document-group">
        <h4>{label} ({rows.length})</h4>
        <ul>{rows.map((document) => <li key={document.url}>
          {/^https?:\/\//i.test(document.url) ? (
            <a href={document.url} target="_blank" rel="noopener noreferrer" title={document.url}>
              {document.title || document.url}
            </a>
          ) : <span>{document.title || document.url}</span>}
          {document.published ? <time dateTime={document.published}>{document.published}</time> : null}
        </li>)}</ul>
      </div> : null;
    })}</GtmScrollArea>
  </section>;
}
