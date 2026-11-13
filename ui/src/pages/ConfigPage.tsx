import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import yaml from "js-yaml";
import { api } from "../api/client";
import { ObjectEditor } from "../config/ObjectEditor";
import { EmbeddingsPanel } from "../components/EmbeddingsPanel";
import { ArtifactLearningPanel } from "../artifacts/ArtifactLearningPanel";
import { WorkflowPanel } from "../components/WorkflowPanel";
import { KnowledgeBasePanel } from "../components/KnowledgeBasePanel";
import { RetrievalPanel } from "../components/RetrievalPanel";
import { diffContext, lineDiff } from "../util/diff";

type Mode = "form" | "yaml";

export function ConfigPage() {
  const queryClient = useQueryClient();
  const configQuery = useQuery({ queryKey: ["config"], queryFn: api.getConfig });
  const [mode, setMode] = useState<Mode>("form");
  const [working, setWorking] = useState<Record<string, unknown>>({});
  const [yamlText, setYamlText] = useState("");
  const [showDiff, setShowDiff] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exported, setExported] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const save = useMutation({
    mutationFn: (body: { config?: Record<string, unknown>; yaml_text?: string }) =>
      api.putConfig(body),
    onSuccess: () => {
      setSaved(true);
      queryClient.invalidateQueries({ queryKey: ["config"] });
    },
    onError: (err) => setSaveError((err as Error).message),
  });

  const saveToServer = () => {
    setSaveError(null);
    setSaved(false);
    // Raw YAML mode saves the user's exact text (preserving comments); form
    // mode saves the edited config object and lets the server render it.
    if (mode === "yaml") {
      save.mutate({ yaml_text: yamlText });
    } else {
      save.mutate({ config: working });
    }
  };

  useEffect(() => {
    if (configQuery.data) {
      setWorking(structuredClone(configQuery.data.effective));
      setYamlText(configQuery.data.raw_yaml ?? yaml.dump(configQuery.data.effective));
    }
  }, [configQuery.data]);

  const proposedYaml = useMemo(() => {
    if (mode === "yaml") return yamlText;
    try {
      return yaml.dump(working, { sortKeys: false });
    } catch {
      return yamlText;
    }
  }, [mode, yamlText, working]);

  // Diff baseline must be rendered the same way as `proposedYaml`, else an
  // unedited page shows spurious changes. In Raw YAML mode the editor is seeded
  // from the user's sparse `raw_yaml`, so diff against that. In Form mode the
  // editor holds the fully-resolved `effective` config, so diff against the
  // effective config dumped the same way (not the sparse file).
  const baseline = useMemo(() => {
    const data = configQuery.data;
    if (!data) return "";
    if (mode === "yaml") return data.raw_yaml ?? yaml.dump(data.effective, { sortKeys: false });
    try {
      return yaml.dump(data.effective, { sortKeys: false });
    } catch {
      return data.raw_yaml ?? "";
    }
  }, [mode, configQuery.data]);
  const diff = useMemo(() => lineDiff(baseline, proposedYaml), [baseline, proposedYaml]);
  const visibleDiff = useMemo(() => diffContext(diff), [diff]);
  const changedCount = diff.filter((d) => d.kind !== "same").length;

  const exportYaml = () => {
    setExportError(null);
    setExported(false);
    try {
      const parsed = yaml.load(proposedYaml);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("Config YAML must be a mapping at the root.");
      }
      const blob = new Blob([proposedYaml], { type: "application/yaml" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "pheasant.adjusted.yaml";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setExported(true);
    } catch (err) {
      setExportError((err as Error).message);
    }
  };

  if (configQuery.isLoading) {
    return (
      <div className="page">
        <p className="muted">Loading config...</p>
      </div>
    );
  }
  if (configQuery.isError) {
    return (
      <div className="page">
        <p className="error">{(configQuery.error as Error).message}</p>
      </div>
    );
  }

  return (
    <div className="page page--wide">
      <div className="page__header">
        <h1>Settings</h1>
        <div className="muted small">{configQuery.data?.path}</div>
      </div>

      {/* The things people actually want to change live above the raw config
          editor, as purpose-built panels: what this knowledge base is, what
          answers questions and how hard it looks, and whether semantic search
          is on. Everything else is still reachable through the form/YAML
          editor below — the UI is a superset of the file, not a subset. */}
      <section className="section">
        <h2 className="section__title">Knowledge base</h2>
        <div className="card" style={{ padding: 16 }}>
          <KnowledgeBasePanel />
        </div>
      </section>

      <section className="section">
        <h2 className="section__title">Question answering</h2>
        <div className="card" style={{ padding: 16 }}>
          <WorkflowPanel selected={null} onSelect={() => undefined} />
        </div>
      </section>

      <section className="section">
        <h2 className="section__title">Retrieval tuning</h2>
        <div className="card" style={{ padding: 16 }}>
          <RetrievalPanel />
        </div>
      </section>

      <section className="section">
        <h2 className="section__title">Artifact learning and retention</h2>
        <div className="card" style={{ padding: 16 }}>
          <ArtifactLearningPanel />
        </div>
      </section>

      <section className="section">
        <h2 className="section__title">Semantic search (embeddings)</h2>
        <div className="card" style={{ padding: 16 }}>
          <EmbeddingsPanel />
        </div>
      </section>

      <h2 className="section__title">Full configuration</h2>
      <div className="config-toolbar">
        <div className="tabs">
          <button className={mode === "form" ? "tab active" : "tab"} onClick={() => setMode("form")}>
            Form
          </button>
          <span>
            <button className={mode === "yaml" ? "tab active" : "tab"} onClick={() => setMode("yaml")}>
              Raw YAML
            </button>
          </span>
        </div>
        <div className="spacer" />
        <span>
          <button className="btn" onClick={() => setShowDiff((v) => !v)}>
            {showDiff ? "Hide diff" : `Preview diff (${changedCount})`}
          </button>
        </span>
        <button className="btn" onClick={exportYaml}>
          Export adjusted YAML
        </button>
        <span>
          <button className="btn btn--primary" onClick={saveToServer} disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save to pheasant"}
          </button>
        </span>
      </div>

      {exportError && <p className="error">{exportError}</p>}
      {saveError && <p className="error">{saveError}</p>}
      {exported && (
        <p className="notice">
          Adjusted YAML exported. Replace your mounted config and restart pheasant to apply it.
        </p>
      )}
      {saved && (
        <p className="notice">
          Config written to the server. Restart pheasant to apply the new configuration.
        </p>
      )}

      {showDiff && (
        <pre className="diff">
          {visibleDiff.map((line, i) => (
            <div key={i} className={`diff-line diff-line--${line.kind}`}>
              <span className="diff-gutter">{line.kind === "add" ? "+" : line.kind === "del" ? "-" : " "}</span>
              {line.text}
            </div>
          ))}
        </pre>
      )}

      {mode === "form" ? (
        <ObjectEditor value={working} onChange={setWorking} />
      ) : (
        <textarea
          className="text-input code yaml-editor"
          value={yamlText}
          onChange={(e) => setYamlText(e.target.value)}
          spellCheck={false}
        />
      )}
    </div>
  );
}
