import { useEffect } from "react";
import { useLocation } from "react-router-dom";
import { api } from "../api/client";
import { actions, configureBrowserTelemetry, drainUIEvents, recordUI } from "../telemetry";

export function Telemetry() {
  const { pathname } = useLocation();
  useEffect(() => {
    let active = true;
    void api.authConfig().then(config => {
      if (!active) return;
      configureBrowserTelemetry(config.telemetry?.ui_enabled ?? false, config.telemetry?.sample_ratio);
      recordUI("navigate", window.location.pathname);
    }).catch(() => undefined);
    const timer = window.setInterval(() => {
      const batch = drainUIEvents();
      if (batch.length) void api.uiTelemetry(batch).catch(() => undefined);
    }, 5000);
    const click = (event: MouseEvent) => {
      const element = event.target instanceof Element ? event.target.closest("[data-telemetry-action]") : null;
      const action = element?.getAttribute("data-telemetry-action");
      if (action && actions.has(action)) recordUI(action, window.location.pathname);
    };
    document.addEventListener("click", click, true);
    return () => { active = false; window.clearInterval(timer); document.removeEventListener("click", click, true); configureBrowserTelemetry(false); };
  }, []);
  useEffect(() => recordUI("navigate", pathname), [pathname]);
  return null;
}
