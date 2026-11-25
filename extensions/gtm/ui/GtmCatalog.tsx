import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { gtmApi } from "./api";
import type { SourceRecord } from "@pheasant/ui/api/types";
import type { GtmCatalogQuery, GtmCompanyProfile, GtmEnrichmentSettings } from "./types";
import { useSession } from "@pheasant/ui/state/session";
import { GtmAccountBrief } from "./GtmAccountBrief";
import { GtmResearchFields } from "./GtmResearchFields";
import { GtmScrollArea } from "./GtmScrollArea";
import { defaultEnrichment, GtmEnrichmentControls } from "./GtmEnrichmentControls";

const PAGE_SIZE = 25;
const STATUSES = ["observed", "derived", "inferred", "proposed", "conflict", "missing", "blocked", "not_applicable"];

function DescriptionSettings({ profile, pending, update }: {
  profile: GtmCompanyProfile;
  pending: boolean;
  update: (settings: GtmEnrichmentSettings) => void;
}) {
  const [settings, setSettings] = useState(profile.enrichment?.settings ?? defaultEnrichment);
  const result = profile.enrichment;
  return <details className="gtm-brief-section" data-permission="sources.manage">
    <summary><strong>Description settings</strong>{result?.fields.length
      ? <span className="muted small"> · {result.fields.length} fields enriched with {result.model}</span>
      : result?.status === "model_unavailable" ? <span className="muted small"> · Extracted profile in use</span> : null}</summary>
    <GtmEnrichmentControls value={settings} onChange={setSettings} />
    <button className="btn btn--small" disabled={pending} onClick={() => update(settings)}>Update descriptions</button>
  </details>;
}

export function isGtmCatalog(source: SourceRecord): boolean {
  if (source.type !== "gtm_research" || !source.config_json) return false;
  try { const config = JSON.parse(source.config_json); return (config.extension_settings ?? config.gtm)?.catalog === true; }
  catch { return false; }
}

export function gtmLocation(catalog: string, company?: string) {
  const params = new URLSearchParams({ catalog });
  if (company) params.set("company", company);
  return { pathname: "/gtm", search: `?${params}` };
}

export function GtmCatalog({ source, selected }: { source: string; selected: string | null }) {
  const [criteria, setCriteria] = useState<GtmCatalogQuery>({ limit: PAGE_SIZE, offset: 0 });
  const [industryCode, setIndustryCode] = useState("");
  const [naicsCode, setNaicsCode] = useState("");
  const [technology, setTechnology] = useState("");
  const [key, setKey] = useState("");
  const [status, setStatus] = useState("");
  const [value, setValue] = useState("");
  const [filterError, setFilterError] = useState("");
  const navigate = useNavigate();
  const { dispatch } = useSession();
  const queryClient = useQueryClient();
  const naics = useQuery({ queryKey: ["gtm-naics"], queryFn: gtmApi.gtmNaics, staleTime: Infinity });
  const taxonomy = useQuery({
    queryKey: ["gtm-technology-taxonomy"], queryFn: gtmApi.gtmTechnologyTaxonomy, staleTime: Infinity,
  });
  const companies = useQuery({
    queryKey: ["gtm-catalog", source, "query", criteria],
    queryFn: () => gtmApi.queryGtmCatalog(source, criteria),
    refetchInterval: 8000,
  });
  const profile = useQuery({
    queryKey: ["gtm-catalog", source, "company", selected],
    enabled: selected !== null,
    queryFn: () => gtmApi.readGtmCompany(source, selected!),
    refetchInterval: 8000,
  });
  const refresh = useMutation({
    mutationFn: ({ company, deep, productPageLimit, enrichment }: { company: string; deep?: boolean; productPageLimit?: number; enrichment?: GtmEnrichmentSettings }) =>
      gtmApi.syncGtmCompany(source, company, deep, productPageLimit, enrichment),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sources"] });
      queryClient.invalidateQueries({ queryKey: ["index-queue"] });
    },
  });
  const openGraph = (nodeId: string) => {
    dispatch({ type: "center-node", nodeId });
    navigate("/graph");
  };

  function applyFilters() {
    const requested = value.trim();
    const field = key.trim();
    setFilterError("");
    const code = naicsCode.trim();
    if (code && naics.data && !naics.data.codes.some((row) => row.code === code && (!industryCode || row.sector_code === industryCode))) {
      setFilterError("Choose a NAICS sub-industry code within the selected industry.");
      return;
    }
    let parsed: GtmCatalogQuery["value"];
    if (requested && field) {
      try {
        const candidate: unknown = JSON.parse(requested);
        if (candidate !== null && typeof candidate === "object") {
          setFilterError("Enter one value, such as Kubernetes, 100, or true.");
          return;
        }
        parsed = candidate as GtmCatalogQuery["value"];
      } catch { parsed = requested; }
    }
    setCriteria({ industry_code: industryCode || undefined, naics_code: code || undefined,
      technology: technology.trim() || undefined, field: field || undefined, status: field && status || undefined,
      value: parsed, limit: PAGE_SIZE, offset: 0 });
    if (selected) navigate(gtmLocation(source));
  }

  const data = profile.data;
  const offset = criteria.offset ?? 0;
  const rows = companies.data?.companies ?? [];
  const categories = taxonomy.data?.concepts ?? [];
  const categoryLabels = new Map(categories.map((category) => [category.id, category.label]));
  const typedTechnology = technology.trim().toLowerCase();
  const selectedCategory = categories.find((category) =>
    [category.id, category.label, ...category.aliases].some((value) => value.toLowerCase() === typedTechnology),
  );

  return (
    <section aria-label="Company research">
        <form className="gtm-filters" onSubmit={(event) => { event.preventDefault(); applyFilters(); }}>
          <label className="field"><span>Industry</span><select className="text-input" value={industryCode} onChange={(event) => { setIndustryCode(event.target.value); setNaicsCode(""); }}>
            <option value="">All industries</option>
            {naics.data?.codes.filter((row) => row.code === row.sector_code).map((row) => <option key={row.code} value={row.code}>{row.industry}</option>)}
          </select></label>
          <label className="field"><span>Sub-industry</span><input className="text-input" list="gtm-filter-naics" maxLength={6} value={naicsCode} onChange={(event) => setNaicsCode(event.target.value)} placeholder="NAICS code" />
            <datalist id="gtm-filter-naics">{naics.data?.codes.filter((row) => row.sub_industry && (!industryCode || row.sector_code === industryCode)).map((row) => <option key={row.code} value={row.code} label={row.sub_industry ?? ""} />)}</datalist>
          </label>
          <label className="field"><span>Technology</span><input className="text-input" maxLength={120} value={technology} onChange={(event) => setTechnology(event.target.value)} placeholder="e.g. SAP EWM or WMS" /></label>
          <button type="submit" className="btn">Apply filters</button>
          <details className="gtm-advanced-filters">
            <summary>Browse technology categories</summary>
            <label className="field"><span>Technology category</span>
              <select className="text-input" value={selectedCategory?.id ?? ""} onChange={(event) => {
                setTechnology(categoryLabels.get(event.target.value) ?? "");
              }}>
                <option value="">Choose a category</option>
                {categories.map((category) => <option key={category.id} value={category.id}>
                  {category.broader.length ? `${category.broader.map((parent) => categoryLabels.get(parent)).join(" / ")} › ` : ""}
                  {category.label}
                </option>)}
              </select>
            </label>
            {taxonomy.error ? <p className="error small" role="status">{taxonomy.error.message}</p> : null}
          </details>
          <details className="gtm-advanced-filters">
            <summary>Advanced field filters</summary>
            <div className="button-row">
              <label className="field"><span>Field key</span><input className="text-input" value={key} onChange={(event) => setKey(event.target.value)} placeholder="technology_stack" /></label>
              <label className="field"><span>Status</span><select className="text-input" value={status} disabled={!key.trim()} onChange={(event) => setStatus(event.target.value)}>
                <option value="">Any status</option>{STATUSES.map((entry) => <option key={entry}>{entry}</option>)}
              </select></label>
              <label className="field"><span>Exact value</span><input className="text-input" value={value} disabled={!key.trim()} onChange={(event) => setValue(event.target.value)} /></label>
            </div>
          </details>
        </form>
        {filterError || companies.error ? <p className="error" role="alert">{filterError || companies.error?.message}</p> : null}
        <div className={`gtm-workspace${selected ? " gtm-workspace--split" : ""}`}>
          <section aria-label="Indexed companies">
            {companies.isLoading ? <p className="muted">Loading companies…</p> : null}
            {companies.data ? <h2 className="section__title" aria-live="polite">{companies.data.has_more || offset ? `Companies ${offset + 1}–${offset + rows.length}` : `${rows.length} indexed ${rows.length === 1 ? "company" : "companies"}`}</h2> : null}
            {rows.length ? <GtmScrollArea key={offset} label="Company results" className="table-scroll">
              <table className="data-table"><thead><tr><th scope="col">Company</th><th scope="col">Industry</th>{selected ? null : <><th scope="col">Sub-industry</th><th scope="col">Domain</th></>}</tr></thead><tbody>
                {rows.map((row) => <tr key={row.company_id} className={selected === row.company_id ? "gtm-selected" : undefined}>
                  <td><Link to={gtmLocation(source, row.company_id)} aria-current={selected === row.company_id ? "true" : undefined} onClick={() => refresh.reset()}>{row.company}</Link></td>
                  <td>{row.classification?.industry ?? "Unclassified"}{selected && row.classification?.sub_industry ? <div className="muted small">{row.classification.sub_industry}</div> : null}</td>
                  {selected ? null : <><td>{row.classification?.sub_industry ?? "—"}{row.classification ? <div className="muted small">{row.classification.code}</div> : null}</td><td>{row.domain}</td></>}
                </tr>)}
              </tbody></table>
            </GtmScrollArea> : null}
            {companies.data && rows.length === 0 ? <p className="muted">No indexed companies match.</p> : null}
            {offset || companies.data?.has_more ? <div className="button-row gtm-paging">
              <button className="btn btn--small" disabled={!offset} onClick={() => setCriteria({ ...criteria, offset: Math.max(0, offset - PAGE_SIZE) })}>Previous</button>
              <button className="btn btn--small" disabled={!companies.data?.has_more} onClick={() => setCriteria({ ...criteria, offset: offset + PAGE_SIZE })}>Next</button>
            </div> : null}
          </section>
          {selected ? <section className="gtm-profile" aria-label="Company profile">
            {profile.isLoading ? <p className="muted">Loading indexed profile…</p> : null}
            {profile.error || refresh.error ? <p className="error" role="alert">{(profile.error ?? refresh.error)?.message}</p> : null}
            {data ? <>
          <header className="gtm-brief-header">
            <h2>{data.company}</h2>
            <p>{data.domain}{data.identity_method === "official_fallback" ? " · Verification publisher" : ""}</p>
          </header>
          <div className="button-row gtm-profile__actions">
            <button className="btn btn--small" onClick={() => openGraph(data.artifact_id)}>Show in graph</button>
            <button className="btn btn--small" data-permission="sources.manage" disabled={refresh.isPending} onClick={() => refresh.mutate({ company: data.company_id })}>Refresh company</button>
            <button className="btn btn--small" data-permission="sources.manage" disabled={refresh.isPending}
              onClick={() => refresh.mutate({ company: data.company_id, productPageLimit: 48 })}>Research offerings</button>
            {!data.research ? <button className="btn btn--small" data-permission="sources.manage" disabled={refresh.isPending} onClick={() => refresh.mutate({ company: data.company_id, deep: true })}>Enable deep research and refresh</button> : null}
            {refresh.isSuccess ? <span role="status">Refresh requested</span> : null}
          </div>
          {data.research ? <p className="muted small">Deep research · {data.research.documents} documents · {data.research.passages} indexed passages · {data.research.fields_with_passages} fields with matching passages</p> : null}
          <DescriptionSettings key={`${data.company_id}-${data.revision}`} profile={data} pending={refresh.isPending}
            update={(enrichment) => refresh.mutate({ company: data.company_id, enrichment })} />
          <GtmResearchFields key={`fields-${data.company_id}`} source={source} profile={data} openGraph={openGraph} />
          <GtmAccountBrief key={data.company_id} profile={data} openGraph={openGraph} />
            </> : null}
          </section> : null}
        </div>
    </section>
  );
}
