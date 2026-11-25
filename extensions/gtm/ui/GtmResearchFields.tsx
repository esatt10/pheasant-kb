import { gtmApi } from "./api";
import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { onApiAuthChanged } from "@pheasant/ui/api/client";
import type { GtmCompanyProfile, GtmResearchCitation, GtmResearchChoice } from "./types";
import { usePermissions } from "@pheasant/ui/state/permissions";
import { formatGtmValue } from "./GtmAccountBrief";
import { GtmScrollArea } from "./GtmScrollArea";

const FIELD_NAMES: Record<string, string> = {
  about: "About us",
  naics_code: "NAICS code",
  website: "Website URL",
  industry_group: "Industry group",
};

const METHODS: Record<string, string> = {
  structured: "Structured publisher data",
  metadata: "Page metadata",
  pattern: "Rule-based extraction",
  identity: "Verified company identity",
  model: "Hosted model review",
  ide: "IDE review",
};

const SOURCE_NAMES: Record<string, string> = {
  official: "Official website",
  jobs: "Job posting",
  news: "News",
  filing: "Filing",
  external: "External source",
};

function fieldName(key: string): string {
  return FIELD_NAMES[key] ?? key.charAt(0).toUpperCase() + key.slice(1).replaceAll("_", " ");
}

function valueLabel(value: unknown, withheld: boolean): string {
  return value == null ? (withheld ? "Available in analyst evidence" : "No value") : formatGtmValue(value);
}

function sourceName(source: string): string {
  return SOURCE_NAMES[source] ?? source;
}

function Citation({ cite, graphId, openGraph }: {
  cite: GtmResearchCitation;
  graphId?: string;
  openGraph: (id: string) => void;
}) {
  return <li className="gtm-field-citation">
    <div>
      {/^https?:\/\//i.test(cite.url)
        ? <a href={cite.url} target="_blank" rel="noopener noreferrer">{cite.title || cite.url}</a>
        : <strong>{cite.title || "Source"}</strong>}
      <span className="muted small"> · {sourceName(cite.source)} · {cite.published ?? "Undated"}</span>
    </div>
    {cite.quote ? <blockquote>{cite.quote}</blockquote> : null}
    {graphId ? <button className="gtm-evidence-link" onClick={() => openGraph(graphId)}>View in graph</button> : null}
  </li>;
}

export function GtmResearchFields({ source, profile, openGraph }: {
  source: string;
  profile: GtmCompanyProfile;
  openGraph: (id: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [field, setField] = useState(profile.fields.about ? "about" : "");
  const { can } = usePermissions();
  const client = useQueryClient();
  const analyst = can("sources.manage");
  const selected = profile.fields[field];
  useEffect(() => onApiAuthChanged(() => {
    void client.resetQueries({ queryKey: ["gtm-field-evidence"] });
  }), [client]);
  const lookup = useQuery({
    queryKey: ["gtm-field-evidence", source, profile.company_id, profile.revision, field],
    queryFn: ({ signal }) => gtmApi.readGtmFieldEvidence(source, profile.company_id, field, profile.revision, signal),
    enabled: expanded && analyst && Boolean(selected),
    retry: false,
    gcTime: 0,
    staleTime: Infinity,
  });
  const evidence = expanded && analyst ? lookup.data : undefined;
  const research = evidence?.research;
  const related = research?.related?.filter((cite) => !research.evidence.some(
    (existing) => existing.url === cite.url && existing.quote === cite.quote,
  ));
  const references = selected
    ? profile.evidence.filter((item) => selected.evidence_ids.includes(item.id))
    : [];
  const graphsByUrl = new Map(references.flatMap((item) => {
    const url = evidence?.source_links[item.id];
    return url ? [[url, item.graph_node_id] as const] : [];
  }));
  const keys = Object.keys(profile.fields).sort((left, right) => fieldName(left).localeCompare(fieldName(right)));
  const classification = ["industry", "industry_group", "naics_code"].includes(field) ? profile.classification : null;

  function citations(rows: GtmResearchCitation[]) {
    return rows.length ? <ul className="gtm-field-citations">{rows.map((cite, index) =>
      <Citation key={`${cite.url}-${index}`} cite={cite} graphId={graphsByUrl.get(cite.url)} openGraph={openGraph} />
    )}</ul> : null;
  }

  function choices(label: string, items?: GtmResearchChoice[]) {
    return items?.length ? <details>
      <summary>{label} ({items.length})</summary>
      <GtmScrollArea label={label}>{items.map((item, index) => <div className="gtm-field-choice" key={index}>
        <strong>{formatGtmValue(item.value)}</strong>
        {item.method ? <div className="muted small">{METHODS[item.method]}</div> : null}
        {citations(item.evidence)}
      </div>)}</GtmScrollArea>
    </details> : null;
  }

  function graphSources(ids: string[]) {
    return ids.map((id) => {
      const item = profile.evidence.find((entry) => entry.id === id);
      return item ? <button key={id} className="gtm-evidence-link" onClick={() => openGraph(item.graph_node_id)}>
        {item.publisher} · {sourceName(item.kind)}{item.published ? ` · ${item.published}` : ""}
      </button> : null;
    });
  }

  return <details className="gtm-brief-section" onToggle={(event) => setExpanded(event.currentTarget.open)}>
    <summary><strong>About us and field evidence</strong></summary>
    <label className="field"><span>Research field</span>
      <select className="text-input" value={field} onChange={(event) => setField(event.target.value)}>
        <option value="">All fields</option>
        {keys.map((key) => <option key={key} value={key}>{fieldName(key)}</option>)}
      </select>
    </label>
    {selected ? <section className="gtm-field-details" aria-label="Selected field">
      <div className="gtm-brief-heading"><h3>{fieldName(field)}</h3>
        <span className="pill">{selected.status.replaceAll("_", " ")}</span>
        {selected.detail?.method === "model" ? <span className="muted small">Source-checked model description</span> : null}
      </div>
      <p className="muted small">{selected.definition.definition}</p>
      {selected.detail?.summary ? <p className="gtm-field-value">{selected.detail.summary}</p> : null}
      {(!selected.detail?.summary || (selected.value != null && selected.value !== selected.detail.summary))
        && (selected.value != null || !selected.withheld || research?.value == null)
        ? <p className="gtm-field-value">{valueLabel(selected.value, selected.withheld)}</p> : null}
      {selected.withheld && research?.value != null ? <div className="gtm-field-value">
        <span className="muted small">Publisher wording</span>
        <p>{formatGtmValue(research.value)}</p>
      </div> : null}
      {classification ? <p className="small">NAICS {classification.edition} · {classification.source === "operator" ? "Catalog assignment" : "Reported classification"}</p> : null}
      {research?.method && !selected.detail?.statements?.length ? <p className="small">{METHODS[research.method]}
        {research.independent_origins != null
          ? ` · ${research.independent_origins} independent ${research.independent_origins === 1 ? "source" : "sources"}`
          : ""}
      </p> : null}
      {research?.derived_from ? <p className="small">Derived from <button
        className="gtm-evidence-link" onClick={() => setField(research.derived_from!)}
      >{fieldName(research.derived_from)}</button></p> : null}
      {(research?.reason || selected.reason) && !classification && selected.value == null && !selected.detail?.summary
        ? <p className="muted small">{research?.reason ?? selected.reason}</p> : null}
      {selected.detail?.statements?.length ? <details open>
        <summary>Sources for each statement</summary>
        <GtmScrollArea label="Statement sources"><ul>{selected.detail.statements.map((item) => <li key={item.text}>
          {item.text}
          <div className="gtm-evidence-list">{graphSources(item.evidence_ids)}</div>
        </li>)}</ul></GtmScrollArea>
      </details> : null}
      {selected.detail?.items?.length ? <details>
        <summary>Evidence by reported value</summary>
        <GtmScrollArea label="Reported value evidence">{selected.detail.items.map((item, index) => <div className="gtm-field-choice" key={index}>
          <strong>{formatGtmValue(item.value)}</strong>{item.category ? <span className="muted small"> · {item.category}</span> : null}
          <p>{item.text}</p>
          <div className="gtm-evidence-list">{graphSources(item.evidence_ids)}</div>
        </div>)}</GtmScrollArea>
      </details> : null}
      {!analyst && selected.related_passage_count ? <p className="muted small">{selected.related_passage_count} matching source passages</p> : null}
      {expanded && analyst && lookup.isLoading ? <p className="muted small">Loading source evidence…</p> : null}
      {expanded && analyst && lookup.error ? <p className="error small" role="status">{lookup.error.message}</p> : null}
      {evidence && !research ? <p className="muted small">Detailed evidence is available after this company is refreshed.</p> : null}
      {research?.evidence.length ? <div>
        {selected.detail?.statements?.length ? <p className="muted small">Additional publisher evidence</p> : null}
        <GtmScrollArea label="Publisher evidence">{citations(research.evidence)}</GtmScrollArea>
      </div> : references.length ? <GtmScrollArea label="Field references"><div className="gtm-evidence-list">
        {references.map((item) => <button key={item.id} className="gtm-evidence-link" onClick={() => openGraph(item.graph_node_id)}>
          {item.publisher} · {sourceName(item.kind)}{item.published ? ` · ${item.published}` : ""}
        </button>)}
      </div></GtmScrollArea> : null}
      {choices("Evidence by value", research?.items)}
      {choices("Alternative values", research?.alternatives)}
      {choices("Proposed values", research?.proposals)}
      {related?.length ? <details>
        <summary>Related source passages ({related.length})</summary>
        <GtmScrollArea label="Related source passages">{citations(related)}</GtmScrollArea>
      </details> : null}
    </section> : <GtmScrollArea label="All research fields" className="table-scroll">
      <table className="data-table"><thead><tr><th scope="col">Field</th><th scope="col">Indexed value</th><th scope="col">Status</th></tr></thead>
        <tbody>{keys.map((key) => {
          const result = profile.fields[key];
          return <tr key={key}>
            <td><button className="gtm-evidence-link" onClick={() => setField(key)}>{fieldName(key)}</button></td>
            <td>{valueLabel(result.value, result.withheld)}</td>
            <td><span className="pill">{result.status.replaceAll("_", " ")}</span></td>
          </tr>;
        })}</tbody>
      </table>
    </GtmScrollArea>}
    <details><summary>Indexed Markdown</summary><GtmScrollArea label="Indexed company Markdown">
      <pre className="content-block">{profile.markdown}</pre>
    </GtmScrollArea></details>
  </details>;
}
