import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, getApiToken, onApiAuthChanged, onApiAuthRequired } from "../api/client";
import { ApiTokenDialog } from "./ApiTokenDialog";
import { HealthBadge } from "./HealthBadge";
import { McpDialog } from "./McpDialog";
import { useTheme } from "../hooks/useTheme";
import { PheasantMark } from "./PheasantMark";

export function TopBar() {
  const [theme, toggleTheme] = useTheme();
  const [showMcp, setShowMcp] = useState(false);
  const [showApiAuth, setShowApiAuth] = useState(false);
  const [apiToken, setApiTokenState] = useState(getApiToken);
  const queryClient = useQueryClient();

  useEffect(() => {
    const removeRequiredListener = onApiAuthRequired(() => setShowApiAuth(true));
    const removeChangedListener = onApiAuthChanged(() => setApiTokenState(getApiToken()));
    return () => {
      removeRequiredListener();
      removeChangedListener();
    };
  }, []);

  function onApiTokenSaved() {
    setApiTokenState(getApiToken());
    void queryClient.invalidateQueries();
  }

  const overview = useQuery({
    queryKey: ["overview"],
    queryFn: api.overview,
    // Rendered once, above every page's routes, so a source registered from
    // the Sources page (or the Notebook rail's quick-add) stays visibly
    // "syncing" even after you've closed that dialog and moved on. Polls only
    // while something actually is syncing.
    refetchInterval: (query) => (query.state.data?.sources?.some((s) => s.syncing) ? 1500 : false),
  });

  // The "Syncing N sources…" badge that used to live here is gone: the jobs
  // tray (bottom of every page) says the same thing with a phase, a counter
  // and the file currently being read, so the badge was a strictly worse
  // duplicate of it.
  return (
    <header className="topbar">
      <div className="topbar__brand">
        <span className="topbar__logo" aria-hidden>
          <PheasantMark />
        </span>
        pheasant
        {overview.data ? <span className="topbar__kb">{overview.data.name}</span> : null}
      </div>

      <nav className="topbar__nav">
        <NavLink to="/" end className={({ isActive }) => (isActive ? "navlink active" : "navlink")}>
          Notebook
        </NavLink>
        <NavLink to="/graph" className={({ isActive }) => (isActive ? "navlink active" : "navlink")}>
          Graph
        </NavLink>
        <NavLink
          to="/memory"
          className={({ isActive }) => (isActive ? "navlink active" : "navlink")}
        >
          Memory
        </NavLink>
        <NavLink
          to="/evaluation"
          className={({ isActive }) => (isActive ? "navlink active" : "navlink")}
        >
          Effectiveness
        </NavLink>
        <NavLink
          to="/tuning"
          className={({ isActive }) => (isActive ? "navlink active" : "navlink")}
        >
          Tuning
        </NavLink>
        <NavLink
          to="/sources"
          className={({ isActive }) => (isActive ? "navlink active" : "navlink")}
        >
          Sources
        </NavLink>
        <NavLink
          to="/config"
          className={({ isActive }) => (isActive ? "navlink active" : "navlink")}
        >
          Settings
        </NavLink>
      </nav>

      <div className="topbar__right">
        <HealthBadge />
        <button className="btn btn--small" onClick={() => setShowApiAuth(true)}>
          {apiToken ? "API connected" : "Connect API"}
        </button>
        <button className="btn btn--small" onClick={() => setShowMcp(true)}>
          Connect agent
        </button>
        <button
          className="btn btn--icon"
          onClick={toggleTheme}
          title={theme === "dark" ? "Switch to light" : "Switch to dark"}
          aria-label="Toggle theme"
        >
          {theme === "dark" ? "☀" : "☾"}
        </button>
      </div>

      {showMcp ? <McpDialog onClose={() => setShowMcp(false)} /> : null}
      {showApiAuth ? (
        <ApiTokenDialog
          onClose={() => setShowApiAuth(false)}
          onSaved={onApiTokenSaved}
        />
      ) : null}
    </header>
  );
}
