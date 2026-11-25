export interface GtmCatalogQuery {
  company_id?: string;
  segment?: string;
  industry_code?: string;
  naics_code?: string;
  technology?: string;
  field?: string;
  status?: string;
  value?: string | number | boolean | null;
  offset?: number;
  limit?: number;
}

export interface GtmIndustry {
  edition: number;
  code: string;
  sector_code: string;
  industry: string;
  sub_industry: string | null;
  source: "operator" | "reported" | "reference";
}

export interface GtmCompanyRow {
  company_id: string;
  company: string;
  domain: string;
  segment?: string | null;
  classification?: GtmIndustry | null;
  relative_path: string;
  revision: string;
}

export interface GtmTechnologyTaxonomy {
  id: string;
  version: number;
  concepts: { id: string; label: string; broader: string[]; aliases: string[]; product_count: number }[];
}

type OfferingValue = { value: string; evidence_ids: string[] };

export interface GtmEnrichmentSettings {
  enabled: boolean;
  model: string;
}

export interface GtmOffering {
  id: string;
  name: string;
  kind: "product" | "service" | "support";
  role: "provider" | "implementation_partner";
  evidence_ids: string[];
  capabilities?: OfferingValue[];
  buyers?: OfferingValue[];
  delivery?: OfferingValue[];
  support?: OfferingValue[];
}

export interface GtmCompanyProfile extends Pick<GtmCompanyRow, "company_id" | "company" | "domain" | "segment" | "classification" | "revision"> {
  publication_pending: boolean;
  artifact_id: string;
  identity_method?: "official_fallback";
  path: string;
  markdown: string | null;
  indexed_at?: string;
  technology?: { name: string; category: string; category_id?: string; signals: { relationship: string; evidence_ids: string[] }[] }[];
  signal_coverage?: { jobs: number; news: number };
  offerings?: GtmOffering[];
  offering_research?: { before: number; after?: number; budget: number; attempts?: number; outcome?: string };
  enrichment?: { status: string; model?: string; fields: string[]; settings: GtmEnrichmentSettings };
  research?: {
    documents: number;
    passages: number;
    fields_searched: number;
    fields_with_passages: number;
    documents_by_kind: Record<string, number>;
    supplement_tracks: string[];
  };
  hubspot?: { property: string; value: unknown; status: "candidate" | "mapping" | "review"; field: string }[];
  fields: Record<string, {
    definition: { definition: string; type: string; hubspot: string | null };
    status: string;
    value: unknown;
    withheld: boolean;
    evidence_ids: string[];
    reason?: string;
    detail?: {
      method?: "model";
      summary?: string;
      statements?: { text: string; evidence_ids: string[] }[];
      items?: { value: unknown; text: string; category?: string; evidence_ids: string[] }[];
    };
    related_passage_count?: number;
  }>;
  evidence: { id: string; graph_node_id: string; kind: string; publisher: string; published: string | null; field_keys: string[] }[];
}

export interface GtmResearchCitation {
  url: string;
  title: string | null;
  source: string;
  published: string | null;
  quote: string | null;
}

export interface GtmResearchChoice {
  value: unknown;
  method?: string;
  independent_origins?: number;
  evidence: GtmResearchCitation[];
}

export interface GtmFieldEvidence {
  revision: string;
  source_links: Record<string, string>;
  research: (GtmResearchChoice & {
    status: string;
    reason?: string;
    derived_from?: string;
    items?: GtmResearchChoice[];
    alternatives?: GtmResearchChoice[];
    proposals?: GtmResearchChoice[];
    related?: GtmResearchCitation[];
  }) | null;
}
