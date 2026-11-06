import { useEffect, useRef, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  getApiToken,
  onApiAuthChanged,
  onApiAuthRequired,
  type AuthIdentity,
} from "../api/client";
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
  const [identity, setIdentity] = useState<AuthIdentity | null>(null);
  const queryClient = useQueryClient();
  // Below 860px the nav and the actions do not fit beside the brand -- seven
  // links and three buttons laid the page out 1,075px wide on a 390px phone,
  // and the browser zoomed the whole app out to fit. They fold into a menu
  // there (CSS decides when; on a wide window the wrapper is `display:
  // contents` and this state does nothing).
  const [menuOpen, setMenuOpen] = useState(false);
  const headerRef = useRef<HTMLElement>(null);
  const location = useLocation();

  useEffect(() => setMenuOpen(false), [location.pathname]);

  useEffect(() => {
    if (!menuOpen) return;
    const onPointer = (event: PointerEvent) => {
      if (!headerRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  useEffect(() => {
    const removeRequiredListener = onApiAuthRequired(() => setShowApiAuth(true));
    const removeChangedListener = onApiAuthChanged(() => setApiTokenState(getApiToken()));
    return () => {
      removeRequiredListener();
      removeChangedListener();
    };
  }, []);

  // Who the tab's credential names, for the button's label.
  useEffect(() => {
    if (!apiToken) {
      setIdentity(null);
      return;
    }
    api.me().then(setIdentity).catch(() => setIdentity(null));
  }, [apiToken]);

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
    <header className="topbar" ref={headerRef}>
      <div className="topbar__brand">
        <span className="topbar__logo" aria-hidden>
          <PheasantMark />
        </span>
        pheasant
        {overview.data ? <span className="topbar__kb">{overview.data.name}</span> : null}
      </div>

      <button
        type="button"
        className="btn btn--icon topbar__menu-btn"
        aria-label={menuOpen ? "Close menu" : "Open menu"}
        aria-expanded={menuOpen}
        aria-controls="topbar-menu"
        onClick={() => setMenuOpen((open) => !open)}
      >
        {menuOpen ? "✕" : "☰"}
      </button>

      <div id="topbar-menu" className={`topbar__menu${menuOpen ? " topbar__menu--open" : ""}`}>
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
        <button className="btn btn--small" onClick={() => { setMenuOpen(false); setShowApiAuth(true); }}>
          {apiToken
            ? identity?.person
              ? identity.display_name || identity.principal
              : "API connected"
            : "Connect API"}
        </button>
        <button className="btn btn--small" onClick={() => { setMenuOpen(false); setShowMcp(true); }}>
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
