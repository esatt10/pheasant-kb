import { useQuery } from "@tanstack/react-query";
import { api } from "@pheasant/ui/api/client";
import type { GtmEnrichmentSettings } from "./types";

export const defaultEnrichment: GtmEnrichmentSettings = { enabled: true, model: "auto" };

export function GtmEnrichmentControls({ value, onChange }: {
  value: GtmEnrichmentSettings;
  onChange: (settings: GtmEnrichmentSettings) => void;
}) {
  const models = useQuery({ queryKey: ["models"], queryFn: api.models, staleTime: 60_000 });
  const choices = models.data?.chat.filter((model) => models.data?.selectable || model.default) ?? [];
  return <>
    <label className="checkbox">
      <input type="checkbox" checked={value.enabled} onChange={(event) => onChange({ ...value, enabled: event.target.checked })} />
      Enrich descriptions with a model
    </label>
    {value.enabled ? <label className="field">
      <span>Description model</span>
      <select className="text-input" value={value.model} onChange={(event) => onChange({ ...value, model: event.target.value })}>
        <option value="auto">Auto (prefer Luna)</option>
        {value.model !== "auto" && !choices.some((model) => model.id === value.model)
          ? <option value={value.model}>{value.model}</option> : null}
        {choices.map((model) => <option key={model.id} value={model.id}>
          {model.label}{model.available ? "" : " (unavailable)"}
        </option>)}
      </select>
      <span className="muted small">Uses the saved research evidence. If no model is available, GTM uses the extracted profile.</span>
    </label> : null}
  </>;
}
