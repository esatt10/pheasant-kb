import { Route, Routes } from "react-router-dom";
import { TopBar } from "./components/TopBar";
import { JobsTray } from "./components/JobsTray";
import { RegionBanner } from "./components/RegionBanner";
import { Notebook } from "./pages/Notebook";
import { GraphPage } from "./pages/GraphPage";
import { MemoryPage } from "./pages/MemoryPage";
import { EvaluationPage } from "./pages/EvaluationPage";
import { TuningPage } from "./pages/TuningPage";
import { SourcesPage } from "./pages/SourcesPage";
import { ConfigPage } from "./pages/ConfigPage";
import { SessionProvider } from "./state/session";

/**
 * The session store sits ABOVE the router on purpose: routes unmount as you
 * navigate, so any workspace state held inside a page would be destroyed by
 * the act of visiting Sources or Settings. Up here, only an action changes it.
 *
 * The jobs tray is outside <Routes> for the same reason — background work
 * outlives the page you started it from, and a progress bar that disappears
 * when you navigate is worse than no progress bar.
 */
export function App() {
  return (
    <SessionProvider>
      <div className="app">
        <TopBar />
        <RegionBanner />
        <main className="app__main">
          <Routes>
            <Route path="/" element={<Notebook />} />
            <Route path="/graph" element={<GraphPage />} />
            <Route path="/memory" element={<MemoryPage />} />
            <Route path="/evaluation" element={<EvaluationPage />} />
            <Route path="/tuning" element={<TuningPage />} />
            <Route path="/sources" element={<SourcesPage />} />
            <Route path="/config" element={<ConfigPage />} />
          </Routes>
        </main>
        <JobsTray />
      </div>
    </SessionProvider>
  );
}
