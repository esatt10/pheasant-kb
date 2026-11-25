import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@pheasant/ui/api/client";
import type { SourceRecord } from "@pheasant/ui/api/types";
import { sourceConfig } from "@pheasant/ui/sources/sourceConfig";
import { gtmApi } from "./api";
import { isGtmCatalog } from "./GtmCatalog";

interface Props {
  source?: SourceRecord;
  catalogName?: string;
  catalogOnly?: boolean;
  onGtmRegistered?: (name: string) => void;
  onClose: () => void;
}

export function GtmRegistration({ source, catalogName, catalogOnly, onGtmRegistered, onClose }: Props) {
  const queryClient = useQueryClient();
  const editing = Boolean(source);
  const initial = sourceConfig(source);
  const [name, setName] = useState(String(initial.name ?? catalogName ?? ""));
  const [urlsText, setUrlsText] = useState(lines(initial.urls));
  const [syncNow, setSyncNow] = useState(!editing);
  const [error, setError] = useState<string | null>(null);
  const known = useQuery({ queryKey: ["sources"], queryFn: api.sources });
  const [grouped, setGrouped] = useState(!editing);
  const [directory, setDirectory] = useState("");
  const knownSource = known.data?.find((entry) => entry.name === name.trim());
  const nameTaken = !editing && !!knownSource && !(grouped && isGtmCatalog(knownSource));
  const gtmSettings = (initial.extension_settings ?? initial.gtm ?? {}) as Record<string, unknown>;
  const gtmTarget = (gtmSettings.target ?? {}) as Record<string, unknown>;
  const [naicsCode, setNaicsCode] = useState(String(gtmTarget.naics_code ?? ""));
  const naics = useQuery({ queryKey: ["gtm-naics"], queryFn: gtmApi.gtmNaics, staleTime: Infinity });
  const gtmAcquisition = (gtmTarget.acquisition ?? {}) as Record<string, unknown>;
  const [company, setCompany] = useState(String(gtmTarget.company ?? ""));
  const folder = directory.trim() || company.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 80);
  const [website, setWebsite] = useState(String(gtmTarget.website ?? ""));
  const [fallbackUrls, setFallbackUrls] = useState(lines(gtmAcquisition.identity_fallback_urls));
  const [inputSource, setInputSource] = useState(String(gtmSettings.input_source ?? ""));
  const [discover, setDiscover] = useState(Boolean(gtmTarget.search ?? false));
  const initialDepth = gtmTarget.depth === "deep" ? "deep" : "standard";
  const [depth, setDepth] = useState<"standard" | "deep">(editing ? initialDepth : "deep");

  const mutation = useMutation({
    mutationFn: () => {
    return gtmApi.registerGtmCollection({
      name: name.trim(),
      company_id: grouped ? folder : undefined,
      target: {
        ...gtmTarget,
        ...(depth !== initialDepth ? { page_limit: undefined, job_limit: undefined } : {}),
        depth,
        company: company.trim() || null,
        website: website.trim() || null,
        naics_code: naicsCode.trim() || null,
        acquisition: { ...gtmAcquisition, identity_fallback_urls: fallbackUrls.split(/\r?\n/).map((url) => url.trim()).filter(Boolean) },
        urls: urlsText.split(/\r?\n/).map((url) => url.trim()).filter(Boolean),
        search: discover,
      },
      input_source: inputSource || null,
      sync_now: syncNow,
    });
    },
    onSuccess: () => {
      for (const queryKey of [["sources"], ["graph"], ["index-queue"], ["taxonomy", name], ["gtm-catalog", name.trim()]]) {
        void queryClient.invalidateQueries({ queryKey });
      }
      onGtmRegistered?.(name.trim());
      onClose();
    },
    onError: (err) => setError((err as Error).message),
  });

  const nameValid = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$/.test(name.trim());
  const folderValid = !grouped || /^[a-z0-9][a-z0-9_-]{0,79}$/.test(folder);
  const validNaics = !naicsCode.trim() || (naics.data
    ? naics.data.codes.some((row) => row.code === naicsCode.trim())
    : /^(?:[0-9]{2,6}|[0-9]{2}-[0-9]{2})$/.test(naicsCode.trim()));
  const hasTarget = Boolean(website.trim() || urlsText.trim()
    || (company.trim() && (discover || inputSource)));
  const hasFallbackDetails = !fallbackUrls.trim() || Boolean(company.trim() && website.trim());
  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal modal--wide" onClick={(e) => e.stopPropagation()}>
        <header className="modal__header">
          <h2>{editing ? "Edit GTM research" : "Add GTM research"}</h2>
          <button className="btn btn--ghost" onClick={onClose}>close</button>
        </header>
        <div className="wizard">
          <div className="wizard__col wizard__col--form">
            <label className="field">
              <span>Company name</span>
              <input className="text-input" value={company} onChange={(e) => setCompany(e.target.value)} />
            </label>
            <label className="field">
              <span>Official website</span>
              <input className="text-input" value={website} onChange={(e) => setWebsite(e.target.value)} placeholder="https://example.com" />
            </label>
            <label className="field">
              <span>Seed URLs, one per line</span>
              <textarea className="text-area" value={urlsText} onChange={(e) => setUrlsText(e.target.value)} />
            </label>
            <label className="field">
              <span>Official fallback URLs, one per line</span>
              <textarea className="text-area" value={fallbackUrls} onChange={(e) => setFallbackUrls(e.target.value)} />
              <span className="muted small">Used if the primary website cannot establish identity. Requires a company name and primary website.</span>
            </label>
          </div>
          <div className="wizard__col wizard__col--form">
            <label className="field">
              <span>{grouped ? "Catalog source name" : "Source name"}</span>
              <input className="text-input" disabled={editing || Boolean(catalogName)} value={name} onChange={(e) => setName(e.target.value)} placeholder={grouped ? "gtm-companies" : "example-gtm"} aria-invalid={nameTaken || !!name && !nameValid} />
              {nameTaken ? <span className="error small">This source name is already taken.</span> : null}
            </label>
            {!editing && !catalogName && !catalogOnly ? <label className="checkbox">
              <input type="checkbox" checked={grouped} onChange={(e) => setGrouped(e.target.checked)} />
              Group under a company catalog
            </label> : null}
            {grouped ? <label className="field">
              <span>Company folder ID</span>
              <input className="text-input" value={directory} placeholder={folder || "example-company"} aria-invalid={!!directory && !folderValid} onChange={(e) => setDirectory(e.target.value)} />
            </label> : null}
            <label className="field">
              <span>NAICS industry code (optional)</span>
              <input className="text-input" value={naicsCode} list="gtm-registration-naics" placeholder="513210 · Software Publishers" maxLength={6} aria-invalid={!validNaics} onChange={(e) => setNaicsCode(e.target.value)} />
              <datalist id="gtm-registration-naics">
                {naics.data?.codes.map((row) => <option key={row.code} value={row.code} label={row.sub_industry ?? row.industry} />)}
              </datalist>
            </label>
            <label className="field">
              <span>Input web collection</span>
              <select className="text-input" value={inputSource} onChange={(e) => setInputSource(e.target.value)}>
                <option value="">None</option>
                {(known.data ?? []).filter((entry) => entry.type === "web_collection" && entry.enabled).map((entry) => (
                  <option key={entry.name} value={entry.name}>{entry.name}</option>
                ))}
              </select>
            </label>
            <label className="checkbox">
              <input type="checkbox" checked={depth === "deep"} onChange={(e) => setDepth(e.target.checked ? "deep" : "standard")} />
              Deep research
            </label>
            <label className="checkbox">
              <input type="checkbox" checked={discover} onChange={(e) => setDiscover(e.target.checked)} />
              Discover additional sources with Brave Search
            </label>
            <label className="checkbox">
              <input type="checkbox" checked={syncNow} onChange={(e) => setSyncNow(e.target.checked)} />
              Sync now
            </label>
            {error ? <p className="error" role="alert">{error}</p> : null}
            <div className="button-row">
              <button className="btn btn--primary" disabled={!nameValid || !folderValid || !validNaics || nameTaken || !hasTarget || !hasFallbackDetails || mutation.isPending} onClick={() => mutation.mutate()}>
                {mutation.isPending ? "Saving..." : editing ? "Save source" : grouped ? "Register company" : "Register source"}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function lines(value: unknown): string {
  return Array.isArray(value) ? value.join("\n") : "";
}
