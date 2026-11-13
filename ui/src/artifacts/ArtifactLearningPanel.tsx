import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";

type ArtifactLearningSettings = {
  consolidation_enabled?: boolean;
  session_ttl_days?: number | null;
  user_ttl_days?: number | null;
  org_ttl_days?: number | null;
  default_policy?: string;
  steering_enabled?: boolean;
  usage_tracking?: boolean;
  max_records?: number | null;
};

/**
 * The `artifacts.learning` config block, edited in place.
 *
 * `LIVE_APPLICABLE_SECTIONS["artifacts"]` is true, so every knob here takes effect
 * without a restart — and the panel reports what the server said rather than
 * assuming, because a UI that says "saved" for a value the process is still
 * ignoring has lied.
 */
export function ArtifactLearningPanel() {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<ArtifactLearningSettings>({});
  const [note, setNote] = useState<string | null>(null);

  const sections = useQuery({
    queryKey: ["config-sections"],
    queryFn: () => api.configSections(),
  });

  const current = (sections.data?.sections ?? []).find((section) => section.id === "artifacts");

  useEffect(() => {
    if (current) setDraft((current.values.learning ?? {}) as ArtifactLearningSettings);
  }, [current]);

  const save = useMutation({
    mutationFn: () => api.patchConfigSection("artifacts", { learning: draft }),
    onSuccess: (result) => {
      setNote(
        result.applied
          ? "Applied to the running server."
          : "Saved — restart required for this to take effect.",
      );
      queryClient.invalidateQueries({ queryKey: ["config-sections"] });
      queryClient.invalidateQueries({ queryKey: ["config"] });
    },
    onError: (error: Error) => setNote(error.message),
  });

  if (sections.isLoading) return <p>Loading…</p>;
  if (!current) return <p>No artifact learning settings available.</p>;

  const set = (patch: Partial<ArtifactLearningSettings>) => setDraft((prev) => ({ ...prev, ...patch }));
  const numeric = (value: string) => (value.trim() === "" ? null : Number(value));

  return (
    <div className="memory-panel">
      <label className="checkbox">
        <input
          type="checkbox"
          checked={Boolean(draft.consolidation_enabled)}
          onChange={(event) => set({ consolidation_enabled: event.target.checked })}
        />
        Consolidate superseded and expired records
      </label>

      <label className="field">
        <span>How typed artifacts take part in a search that does not say</span>
        <select
          className="text-input"
          value={draft.default_policy ?? "auto"}
          onChange={(event) => set({ default_policy: event.target.value })}
        >
          <option value="auto">auto — like any other source</option>
          <option value="off">off — never in results</option>
          <option value="only">only — learned artifacts</option>
          <option value="prefer">prefer — keeps a share of the slots</option>
        </select>
      </label>

      <label className="checkbox">
        <input
          type="checkbox"
          checked={Boolean(draft.steering_enabled)}
          onChange={(event) => set({ steering_enabled: event.target.checked })}
        />
        Let alias / preference / exclusion records re-rank results
      </label>

      <label className="checkbox">
        <input
          type="checkbox"
          checked={Boolean(draft.usage_tracking)}
          onChange={(event) => set({ usage_tracking: event.target.checked })}
        />
        Count which artifacts retrieval returns (feeds salience)
      </label>

      <div className="memory-panel__grid">
        {(
          [
            ["session_ttl_days", "Session TTL (days)"],
            ["user_ttl_days", "User TTL (days)"],
            ["org_ttl_days", "Org TTL (days)"],
            ["max_records", "Max records"],
          ] as [keyof ArtifactLearningSettings, string][]
        ).map(([key, label]) => (
          <label className="field" key={key}>
            <span>{label}</span>
            <input
              className="text-input"
              type="number"
              min={0}
              placeholder="unbounded"
              value={draft[key] === null || draft[key] === undefined ? "" : String(draft[key])}
              onChange={(event) => set({ [key]: numeric(event.target.value) } as ArtifactLearningSettings)}
            />
          </label>
        ))}
      </div>

      <div className="memory-panel__actions">
        <button data-permission="artifacts.manage" className="button button--primary" onClick={() => save.mutate()} disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save"}
        </button>
        {note && <span className="section__hint">{note}</span>}
      </div>
    </div>
  );
}
