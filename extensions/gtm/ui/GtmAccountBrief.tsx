import { useState } from "react";
import { GtmScrollArea } from "./GtmScrollArea";
import type { GtmCompanyProfile, GtmOffering } from "./types";

const RELATIONSHIPS: Record<string, string> = {
  internal_use: "Internal use",
  change_in_progress: "Change in progress",
  hiring: "Hiring evidence",
  product_or_integration: "Product or integration",
  public_website: "Public website",
  mentioned: "Mention",
};

export function formatGtmValue(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return new Intl.NumberFormat("en-US").format(value);
  return String(value ?? "");
}

function values(profile: GtmCompanyProfile, field: string): string[] {
  const value = profile.fields[field]?.value;
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string") : [];
}

export function GtmAccountBrief({ profile, openGraph }: {
  profile: GtmCompanyProfile;
  openGraph: (id: string) => void;
}) {
  const [relationship, setRelationship] = useState("");
  const evidence = new Map(profile.evidence.map((item) => [item.id, item]));
  const technologies = profile.technology ?? [];
  const usedRelations = new Set(technologies.flatMap((item) => item.signals.map((signal) => signal.relationship)));
  const groups = new Map<string, typeof technologies>();
  for (const item of technologies) {
    if (relationship && !item.signals.some((signal) => signal.relationship === relationship)) continue;
    const group = groups.get(item.category) ?? [];
    group.push(item);
    groups.set(item.category, group);
  }
  const industry = profile.classification;
  const activities = values(profile, "business_activities");
  const activity = profile.fields.business_activities;
  const about = profile.fields.about;
  const overviewField = [activity, about].find((field) => field?.detail?.method === "model")
    ?? (activity?.detail?.summary ? activity : about);
  const overview = overviewField?.detail?.summary;
  const initiatives = values(profile, "capabilities_in_development");
  const hypotheses = values(profile, "service_need_hypotheses");
  const snapshot = [
    ["Industry", industry?.industry],
    ["Sub-industry", industry?.sub_industry],
    ["Company size", profile.fields.employees?.value ?? profile.fields.employee_range?.value],
    ["Founded", profile.fields.founded_year?.value],
    ["Country / region", profile.fields.country_code?.value],
    ["Publicly traded", profile.fields.is_public?.value],
  ];

  function sourceButtons(ids: string[]) {
    return ids.map((id) => {
      const item = evidence.get(id);
      if (!item) return null;
      return <button key={id} className="gtm-evidence-link" onClick={() => openGraph(item.graph_node_id)}>
        {item.publisher} · {item.kind}{item.published ? ` · ${item.published}` : " · undated"}
      </button>;
    });
  }

  function descriptions(field: string) {
    const detail = profile.fields[field]?.detail;
    if (detail?.method !== "model" || !detail.statements?.length) return null;
    return <ul>{detail.statements.map((item) => <li key={item.text}>
      {item.text}<div className="gtm-evidence-list">{sourceButtons(item.evidence_ids)}</div>
    </li>)}</ul>;
  }

  function offering(item: GtmOffering) {
    const facets = [
      ["Capabilities", item.capabilities], ["Buyers", item.buyers],
      ["Delivery", item.delivery], ["Support", item.support],
    ] as const;
    return <article className="gtm-field-choice" key={item.id}>
      <h4>{item.name}</h4>
      <p className="muted small">{item.role === "implementation_partner" ? "Implementation partner" :
        item.kind === "support" ? "Company-provided support" : `Company ${item.kind}`}</p>
      <div className="gtm-evidence-list">{sourceButtons(item.evidence_ids)}</div>
      {facets.filter(([, values]) => values?.length).map(([label, values]) => <div key={label}>
        <strong className="small">{label}</strong>
        <ul>{values?.map((value) => <li key={value.value}>
          {value.value} <span className="gtm-evidence-list">{sourceButtons(value.evidence_ids)}</span>
        </li>)}</ul>
      </div>)}
    </article>;
  }

  const offers = profile.offerings ?? [];
  const products = descriptions("products_services");
  const buyers = descriptions("customer_segments");
  const support = descriptions("support_services");
  const initiativeDetails = descriptions("capabilities_in_development");
  const hypothesisDetails = descriptions("service_need_hypotheses");
  const customerSegments = profile.fields.customer_segments;
  const audiences = customerSegments?.detail?.items ?? [];
  const salesBrief = profile.sales_brief;
  return <>
    <section className="gtm-brief-section" aria-label="Account snapshot">
      <div className="gtm-brief-heading">
        <h3>Account snapshot</h3>
        {industry ? <span className="pill" title={industry.source === "operator" ? "Operator-assigned catalog classification" : "Reported by an attributable source"}>
          NAICS {industry.code} · {industry.source === "operator" ? "Catalog assignment" : "Reported"}
        </span> : null}
      </div>
      <dl className="gtm-firmographics">
        {snapshot.filter(([key, value]) => value != null || key === "Industry" || key === "Company size").map(([key, value]) => <div key={String(key)}>
          <dt>{String(key)}</dt><dd>{value == null ? "Unverified" : key === "Founded" ? String(value) : formatGtmValue(value)}</dd>
        </div>)}
      </dl>
      {overview || activities.length ? <div className="gtm-activity">
        <h4>What they do</h4>
        <p>{overview ?? activities.join(" · ")}</p>
        <div className="gtm-evidence-list">{sourceButtons(
          [...new Set(overviewField?.detail?.statements?.flatMap((item) => item.evidence_ids)
            ?? activity?.evidence_ids.slice(0, 3) ?? [])],
        )}</div>
      </div> : null}
    </section>

    <section className="gtm-brief-section" aria-label="Sales briefing">
      <h3>Sales briefing</h3>
      <h4>Who they serve</h4>
      {buyers ?? (audiences.length ? <ul>{audiences.map((item, i) => <li key={i}>
        {formatGtmValue(item.value)}<div className="gtm-evidence-list">{sourceButtons(item.evidence_ids)}</div>
      </li>)}</ul> : <p className="muted small">Buyer segments are unverified for this account.</p>)}
      {salesBrief?.signals.length ? <><h4>What changed</h4><ul>{salesBrief.signals.map((item) => <li key={`${item.date}-${item.text}`}>
        <time dateTime={item.date}>{item.date}</time> · {item.text}
        <div className="gtm-evidence-list">{sourceButtons(item.evidence_ids)}</div>
      </li>)}</ul></> : null}
      {salesBrief?.questions.length ? <><h4>First-call questions</h4><ul>{salesBrief.questions.map((item) => <li key={item.text}>
        {item.text}<div className="gtm-evidence-list">{sourceButtons(item.evidence_ids)}</div>
      </li>)}</ul></> : null}
    </section>

    <section className="gtm-brief-section" aria-label="Products and services">
      <div className="gtm-brief-heading">
        <h3>Products and services</h3>
        {profile.offering_research ? <span className="muted small">
          {profile.offering_research.attempts ?? 0} targeted page attempts
        </span> : null}
      </div>
      {products}
      {offers.length === 0 ? products ? null : <p className="muted small">No offering met the evidence requirements.</p>
        : <GtmScrollArea label="Featured offerings">{offers.slice(0, 6).map(offering)}</GtmScrollArea>}
      {offers.length > 6 ? <details>
        <summary>More offerings ({offers.length - 6})</summary>
        <GtmScrollArea label="Additional offerings">{offers.slice(6).map(offering)}</GtmScrollArea>
      </details> : null}
      {support ? <><h4>Customer support</h4>{support}</> : null}
    </section>

    <section className="gtm-brief-section" aria-label="Sales signals">
      <div className="gtm-brief-heading">
        <h3>Sales signals</h3>
        {profile.signal_coverage ? <span className="muted small">{profile.signal_coverage.jobs} hiring sources · {profile.signal_coverage.news} news sources</span> : null}
      </div>
      {initiatives.length || hypotheses.length || initiativeDetails || hypothesisDetails ? <div className="gtm-sales-signals">
        {initiatives.length || initiativeDetails ? <div>
          <h4>Capabilities in development</h4>
          {initiativeDetails ?? <><ul>{initiatives.map((item) => <li key={item}>{item}</li>)}</ul>
            <div className="gtm-evidence-list">{sourceButtons(profile.fields.capabilities_in_development.evidence_ids.slice(0, 4))}</div></>}
        </div> : null}
        {hypotheses.length || hypothesisDetails ? <div>
          <h4>Outreach hypotheses</h4>
          {hypothesisDetails ?? <><ul>{hypotheses.map((item) => <li key={item}>{item}</li>)}</ul>
            <div className="gtm-evidence-list">{sourceButtons(profile.fields.service_need_hypotheses.evidence_ids.slice(0, 4))}</div></>}
        </div> : null}
      </div> : <p className="muted small">No initiatives or outreach hypotheses met the evidence requirements.</p>}
    </section>

    <section className="gtm-brief-section" aria-label="Technology landscape">
      <div className="gtm-brief-heading">
        <h3>Technology landscape</h3>
        {technologies.length ? <label className="gtm-technology-filter"><span className="sr-only">Technology evidence type</span>
          <select className="text-input" value={relationship} onChange={(event) => setRelationship(event.target.value)}>
            <option value="">All evidence types</option>
            {Object.entries(RELATIONSHIPS).filter(([key]) => usedRelations.has(key)).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
          </select>
        </label> : null}
      </div>
      {descriptions("technology_stack")}
      {technologies.length ? <p className="muted small">{new Set(technologies.map((item) => item.name)).size} named technologies across {new Set(technologies.map((item) => item.category)).size} categories</p> :
        <p className="muted small">No categorized technology evidence is available in this revision.</p>}
      <GtmScrollArea label="Technology groups"><div className="gtm-tech-groups">
        {[...groups].map(([category, items]) => <div key={category} className="gtm-tech-group">
          <h4>{category}</h4>
          {items.map((item) => <details key={item.name} className="gtm-tech-row">
            <summary>
              <strong>{item.name}</strong>
              <span className="gtm-tech-relation">{item.signals.filter((signal) => !relationship || signal.relationship === relationship).map((signal) => RELATIONSHIPS[signal.relationship]).join(" · ")}</span>
            </summary>
            {item.signals.filter((signal) => !relationship || signal.relationship === relationship).map((signal) => <div key={signal.relationship} className="gtm-tech-sources">
              <span className="muted small">{RELATIONSHIPS[signal.relationship]}</span>
              <div className="gtm-evidence-list">{sourceButtons(signal.evidence_ids)}</div>
            </div>)}
          </details>)}
        </div>)}
      </div></GtmScrollArea>
    </section>

    {profile.hubspot?.length ? <details className="gtm-brief-section">
      <summary><strong>HubSpot property review</strong></summary>
      <GtmScrollArea label="HubSpot company properties" className="table-scroll"><table className="data-table">
        <thead><tr><th scope="col">Company property</th><th scope="col">Suggested value</th><th scope="col">Review status</th></tr></thead>
        <tbody>{profile.hubspot.map((item) => <tr key={item.field}>
          <td>{item.property}</td><td>{formatGtmValue(item.value)}</td>
          <td>{item.status === "mapping" ? "Map to HubSpot options or units" : item.status === "review" ? "Verify before import" : "Candidate value"}</td>
        </tr>)}</tbody>
      </table></GtmScrollArea>
      {industry ? <p className="muted small">Store NAICS {industry.code} in a custom company property.</p> : null}
    </details> : null}
  </>;
}
