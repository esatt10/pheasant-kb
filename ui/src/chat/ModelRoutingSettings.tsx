import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ModelCatalog, ModelRoutingPolicy } from "../api/types";
import "./ModelRoutingSettings.css";

const LABELS = {
  off: "Region model",
  openai: "Auto · OpenAI",
  anthropic: "Auto · Anthropic",
  custom: "Auto · custom selection",
};

/** Changes apply to this chat immediately. Saving is a separate authorized action. */
export function ModelRoutingSettings({ catalog, value, onChange }: {
  catalog: ModelCatalog;
  value: ModelRoutingPolicy | null;
  onChange: (policy: ModelRoutingPolicy | null) => void;
}) {
  const client = useQueryClient();
  const policy = value ?? catalog.routing.settings;
  const update = (changes: Partial<ModelRoutingPolicy>) => {
    save.reset();
    onChange({ ...policy, ...changes });
  };
  const save = useMutation({
    mutationFn: () => api.saveModelRouting(policy),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["models"] });
    },
  });
  const candidates = catalog.chat.filter((m) => policy.mode === "custom" || (
    (m.family?.trim().toLowerCase() || (m.provider === "azure" && m.model.startsWith("gpt-")
      ? "openai" : m.provider)) === policy.mode
  ));
  const invalid = policy.mode === "custom" && !policy.models.length;
  return <details className="model-routing">
    <summary className="btn btn--small">Chat settings · {LABELS[policy.mode]}</summary>
    <div className="model-routing__panel">
      <strong>Answering model</strong>
      <label>
        Route automatically
        <select aria-label="Model routing mode" value={policy.mode}
          onChange={(event) => update({ mode: event.target.value as ModelRoutingPolicy["mode"] })}>
          <option value="openai">Available OpenAI models</option>
          <option value="anthropic">Available Anthropic models</option>
          <option value="custom">Custom model selection</option>
          <option value="off">Use the region's fixed model</option>
        </select>
      </label>
      {policy.mode !== "off" ? <>
        <p className="muted small">The decision model selects your next answering model from the
          available candidates. The default criteria cover low, medium and high reasoning.</p>
        <fieldset>
          <legend>{policy.mode === "custom" ? "Select models" : "Candidates"}</legend>
          {candidates.map((model) => <div className="model-routing__candidate" key={model.id}>
            {policy.mode === "custom" ? <label>
              <input type="checkbox" checked={policy.models.includes(model.id)}
                disabled={!model.available && !policy.models.includes(model.id)}
                onChange={(event) => update({ models: event.target.checked
                  ? [...policy.models, model.id] : policy.models.filter((id) => id !== model.id) })} />
              {model.label}
            </label> : <span>{model.label}</span>}
            {!model.available ? <span className="muted small">unavailable</span> : null}
            <select aria-label={`Reasoning level for ${model.label}`}
              value={policy.reasoning_levels[model.id] ?? model.reasoning_level ?? ""}
              onChange={(event) => {
                const levels = { ...policy.reasoning_levels };
                if (event.target.value) levels[model.id] = event.target.value as "low" | "medium" | "high";
                else delete levels[model.id];
                update({ reasoning_levels: levels });
              }}>
              <option value="">Infer capability</option>
              <option value="low">Low reasoning</option>
              <option value="medium">Medium reasoning</option>
              <option value="high">High reasoning</option>
            </select>
          </div>)}
          {!candidates.length ? <p className="muted small">No models in this series are listed.</p> : null}
        </fieldset>
        {invalid ? <p className="error small">Select at least one model.</p> : null}
        <label>
          Selection criteria
          <textarea aria-label="Model selection criteria" rows={6} maxLength={12000}
            value={policy.criteria} onChange={(event) => update({ criteria: event.target.value })} />
        </label>
        <button type="button" className="btn btn--small"
          onClick={() => update({ criteria: catalog.routing.defaults.criteria, reasoning_levels: {} })}>
          Reset to low / medium / high reasoning
        </button>
      </> : null}
      <p className="muted small">Changes apply to this chat. Saved defaults apply to everyone.</p>
      <div className="model-routing__actions">
        <button type="button" className="btn btn--small" onClick={() => { save.reset(); onChange(null); }}>
          Use saved defaults
        </button>
        {catalog.routing.can_save ? <button type="button" className="btn btn--primary btn--small"
          disabled={invalid || !policy.criteria.trim() || save.isPending}
          onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save as region default"}
        </button> : null}
      </div>
      {save.isSuccess ? <p className="small" role="status">Routing defaults saved.</p> : null}
      {save.isError ? <p className="error small" role="alert">{save.error.message}</p> : null}
    </div>
  </details>;
}
