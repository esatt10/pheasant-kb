import { useState } from "react";
import { AccessPanel } from "../admin/AccessPanel";
import { ControlsPanel } from "../admin/ControlsPanel";
import { FleetPanel } from "../admin/FleetPanel";
import { ModelsPanel } from "../admin/ModelsPanel";
import { UsagePanel } from "../admin/UsagePanel";
import { useUiExtensions } from "../extensions";
import { usePermissions } from "../state/permissions";

type Tab = string;

const TABS: { id: Tab; label: string; permission: string }[] = [
  // Granting is a Contributor's too; the rest is an Admin's.
  { id: "access", label: "Access", permission: "settings.manage" },
  { id: "models", label: "Models", permission: "security.manage" },
  { id: "usage", label: "Usage", permission: "security.manage" },
  // Read by an Admin; changed by an operator admin only (the server says which).
  { id: "controls", label: "Controls", permission: "security.manage" },
  { id: "fleet", label: "Knowledge bases", permission: "security.manage" },
];

/**
 * Administration: who may use the region, the models it offers, what it was
 * used for, and the knowledge bases beside it. Each tab is an adapter over one
 * service the HTTP API and MCP expose as well; the server decides what a
 * caller may do, and a tab the caller has no permission for is not offered.
 */
export function AdminPage() {
  const { can } = usePermissions();
  // An installed extension's tabs follow the region's own (the Harness tab's
  // telemetry, say), each gated on the permission its server routes ask for.
  const extensionTabs = useUiExtensions().flatMap((extension) =>
    (extension.adminTabs ?? []).map((tab) => ({ ...tab, id: `${extension.id}:${tab.id}` })),
  );
  const offered = [...TABS, ...extensionTabs].filter((tab) => can(tab.permission));
  const [tab, setTab] = useState<Tab>("access");
  const current = offered.find((entry) => entry.id === tab) ?? offered[0];

  if (!current) {
    return (
      <div className="page">
        <p>
          Administration is for Contributors (granting access) and Admins. Search, chat, browsing
          and downloads remain available.
        </p>
      </div>
    );
  }
  return (
    <div className="page page--wide admin-page">
      <header className="page__header">
        <h1>Administration</h1>
        <div className="tabs admin-tabs" role="tablist">
          {offered.map((entry) => (
            <button
              key={entry.id}
              role="tab"
              aria-selected={current.id === entry.id}
              className={current.id === entry.id ? "tab active" : "tab"}
              onClick={() => setTab(entry.id)}
            >
              {entry.label}
            </button>
          ))}
        </div>
      </header>
      {current.id === "access" ? <AccessPanel /> : null}
      {current.id === "models" ? <ModelsPanel /> : null}
      {current.id === "usage" ? <UsagePanel /> : null}
      {current.id === "controls" ? <ControlsPanel /> : null}
      {current.id === "fleet" ? <FleetPanel /> : null}
      {extensionTabs.map(({ id, Panel }) => (current.id === id ? <Panel key={id} /> : null))}
    </div>
  );
}
