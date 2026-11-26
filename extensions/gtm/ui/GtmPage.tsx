import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "@pheasant/ui/api/client";
import { SourceSyncProgress } from "@pheasant/ui/components/SyncProgress";
import { GtmRegistration } from "./GtmRegistration";
import { GtmCatalog, isGtmCatalog } from "./GtmCatalog";

export function GtmPage() {
  const [search, setSearch] = useSearchParams();
  const [showAdd, setShowAdd] = useState(false);
  const sources = useQuery({
    queryKey: ["sources"],
    queryFn: api.sources,
    refetchInterval: (query) => query.state.data?.some((source) => source.syncing) ? 1000 : 5000,
  });
  const catalogs = (sources.data ?? []).filter(isGtmCatalog).sort((a, b) => a.name.localeCompare(b.name));
  const requested = search.get("catalog");
  const catalog = requested ? catalogs.find((source) => source.name === requested) : catalogs[0];

  return (
    <div className="page page--wide">
      <header className="page__header">
        <div>
          <h1>GTM</h1>
          <p className="muted">Explore indexed company research, field definitions, and supporting evidence.</p>
        </div>
        <button className="btn btn--primary" data-permission="sources.manage" onClick={() => setShowAdd(true)}>
          {catalog ? "Add company" : "Create catalog"}
        </button>
      </header>
      {sources.isLoading ? <p className="muted">Loading catalogs…</p> : null}
      {sources.error ? <p className="error" role="alert">{sources.error.message}</p> : null}
      {catalogs.length ? (
        <div className="gtm-catalog-bar">
          <label className="field">
            <span>Company catalog</span>
            <select className="text-input" value={catalog?.name ?? ""} onChange={(event) => setSearch({ catalog: event.target.value })}>
              {!catalog ? <option value="" disabled>Select a catalog</option> : null}
              {catalogs.map((source) => <option key={source.name}>{source.name}</option>)}
            </select>
          </label>
          <Link to="/sources" className="btn btn--small">Manage sources</Link>
          <Link to="/syncs" className="btn btn--small">View syncs</Link>
        </div>
      ) : null}
      {catalog?.progress ? <SourceSyncProgress row={catalog.progress} /> : null}
      {catalog ? <GtmCatalog key={catalog.name} source={catalog.name} selected={search.get("company")} /> : null}
      {sources.isSuccess && !catalog ? <p className="muted">
        {requested ? "This catalog is unavailable." : "Add a company to create your first GTM catalog."}
      </p> : null}
      {showAdd ? <GtmRegistration catalogOnly catalogName={catalog?.name} onClose={() => setShowAdd(false)}
        onGtmRegistered={(name) => { if (!catalog) setSearch({ catalog: name }); }} /> : null}
    </div>
  );
}
