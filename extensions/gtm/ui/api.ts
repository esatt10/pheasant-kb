import { request, qs } from "@pheasant/ui/api/client";
import type {
  GtmCatalogQuery,
  GtmIndustry,
  GtmCompanyProfile,
  GtmCompanyRow,
  GtmFieldEvidence,
  GtmEnrichmentSettings,
  GtmTechnologyTaxonomy,
} from "./types";

export const gtmApi = {
  registerGtmCollection: (body: {
    name: string;
    company_id?: string;
    segment?: string;
    target: Record<string, unknown>;
    input_source: string | null;
    sync_now: boolean;
  }) =>
    request<{ status: string; source: Record<string, unknown> }>("/gtm/collections", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  queryGtmCatalog: (source: string, criteria: GtmCatalogQuery) =>
    request<{ companies: GtmCompanyRow[]; has_more: boolean }>(`/gtm/catalogs/${encodeURIComponent(source)}/query`, {
      method: "POST", body: JSON.stringify(criteria),
    }),
  gtmNaics: () => request<{ edition: number; codes: GtmIndustry[] }>("/gtm/naics"),
  gtmTechnologyTaxonomy: () => request<GtmTechnologyTaxonomy>("/gtm/technology-taxonomy"),
  readGtmCompany: (source: string, company: string) =>
    request<GtmCompanyProfile>(`/gtm/catalogs/${encodeURIComponent(source)}/companies/${encodeURIComponent(company)}`),
  readGtmFieldEvidence: (source: string, company: string, field: string, revision: string, signal?: AbortSignal) =>
    request<GtmFieldEvidence>(
      `/gtm/catalogs/${encodeURIComponent(source)}/companies/${encodeURIComponent(company)}/evidence${qs({ field, revision })}`,
      { signal, cache: "no-store" },
    ),
  syncGtmCompany: (source: string, company: string, {
    deep, productPageLimit, enrichment, pageLimit, excerptLimit,
  }: {
    deep?: boolean; productPageLimit?: number; enrichment?: GtmEnrichmentSettings; pageLimit?: number; excerptLimit?: number;
  }) => {
    const query = qs({ deep: deep ? "true" : undefined, product_page_limit: productPageLimit,
      page_limit: pageLimit, excerpt_limit: excerptLimit,
      enrichment_enabled: enrichment ? String(enrichment.enabled) : undefined, enrichment_model: enrichment?.model });
    return request(`/gtm/catalogs/${encodeURIComponent(source)}/companies/${encodeURIComponent(company)}/sync${query}`, { method: "POST" });
  },
};
