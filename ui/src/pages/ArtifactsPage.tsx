import { useState } from "react";
import { ArtifactsPanel } from "../artifacts/ArtifactsPanel";
import { ArtifactRecords } from "../artifacts/ArtifactRecords";

export function ArtifactsPage() {
  const [selected, setSelected] = useState<string | null>(null);
  return <div className="page">
    <h1>Artifacts</h1>
    <p className="muted">Published knowledge, composed files, reports, and the evidence behind them.</p>
    <ArtifactRecords />
    <ArtifactsPanel selectedId={selected} onSelect={setSelected} />
  </div>;
}
