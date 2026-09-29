import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { RetrievalSettings } from "../api/types";

/**
 * How hard the assistant looks before it answers.
 *
 * These knobs already existed, but only as untyped keys inside
 * `assistant.workflow_options` documented in a Python module's DEFAULTS dict —
 * so they were invisible unless you read the source. They are now typed config
 * (`assistant.retrieval`), which is what makes this panel possible and what
 * lets an agent read the same values over MCP (`describe_retrieval`).
 *
 * Retrieval is query-time only, so saving takes effect on the very next
 * question: no restart, no re-index.
 */

const NUMERIC: { key: keyof RetrievalSettings; label: string; min: number; max: number }[] = [
  { key: "max_rounds", label: "Retrieval rounds", min: 1, max: 6 },
  { key: "per_query_results", label: "Passages per query", min: 1, max: 40 },
  { key: "max_context_passages", label: "Passages used to answer", min: 1, max: 60 },
  { key: "expand_depth", label: "Graph expansion depth", min: 0, max: 4 },
  { key: "expand_per_node", label: "Neighbours per expanded node", min: 1, max: 20 },
  { key: "max_facts", label: "Facts surfaced", min: 0, max: 50 },
];

const TOGGLES: { key: keyof RetrievalSettings; label: string }[] = [
  { key: "expand_graph", label: "Walk the graph out of the best hits" },
  { key: "grade_evidence", label: "Grade the evidence before answering" },
  { key: "verify_citations", label: "Drop citations that do not resolve" },
];

const ALL_MODES = ["text", "vector", "graph", "hybrid"];

export function RetrievalPanel() {
  const queryClient = useQueryClient();
  const retrieval = useQuery({ queryKey: ["retrieval"], queryFn: api.retrieval });
  const [draft, setDraft] = useState<RetrievalSettings | null>(null);

  useEffect(() => {
    if (retrieval.data) setDraft({ ...retrieval.data.retrieval });
  }, [retrieval.data]);

  const save = useMutation({
    mutationFn: () => api.updateRetrieval({ ...(draft ?? {}), persist: true }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["retrieval"] }),
  });

  if (retrieval.isLoading || !draft) return <p className="muted small">Loading…</p>;
  if (retrieval.isError)
    return <div className="banner banner--error">{(retrieval.error as Error).message}</div>;

  const help = retrieval.data?.field_help ?? {};
  const set = (key: keyof RetrievalSettings, value: unknown) =>
    setDraft((current) => (current ? { ...current, [key]: value } : current));

  const modes = draft.retrieval_modes ?? [];

  return (
    <div className="form-grid">
      <p className="muted small" style={{ margin: 0 }}>
        More rounds and more passages mean better answers on hard questions, and
        slower, costlier ones on easy questions. Changes apply to the next question
        — nothing needs restarting or re-indexing.
      </p>

      <div className="retrieval-grid">
        {NUMERIC.map(({ key, label, min, max }) => (
          <label className="field" key={key}>
            <span>{label}</span>
            <input
              className="text-input"
              type="number"
              min={min}
              max={max}
              value={String(draft[key] ?? "")}
              onChange={(event) =>
                set(key, event.target.value === "" ? null : Number(event.target.value))
              }
            />
            {help[key] ? <span className="muted small">{help[key]}</span> : null}
          </label>
        ))}
      </div>

      <div className="field">
        <span>Search modes to fan out over</span>
        <div className="sources-strip">
          {ALL_MODES.map((mode) => (
            <button
              key={mode}
              className={modes.includes(mode) ? "source-chip source-chip--on" : "source-chip"}
              onClick={() =>
                set(
                  "retrieval_modes",
                  modes.includes(mode) ? modes.filter((m) => m !== mode) : [...modes, mode],
                )
              }
            >
              <span className="source-chip__label">{mode}</span>
            </button>
          ))}
        </div>
        <span className="muted small">{help.retrieval_modes}</span>
      </div>

      {TOGGLES.map(({ key, label }) => (
        <label className="checkbox" key={key}>
          <input
            type="checkbox"
            checked={Boolean(draft[key])}
            onChange={(event) => set(key, event.target.checked)}
          />
          {label}
          {help[key] ? <span className="muted small"> — {help[key]}</span> : null}
        </label>
      ))}

      <label className="field">
        <span>Evidence grader model (optional)</span>
        <input
          className="text-input"
          type="text"
          value={draft.grader_model ?? ""}
          placeholder="Use assistant model"
          onChange={(event) => set("grader_model", event.target.value.trim() || null)}
        />
        {help.grader_model ? <span className="muted small">{help.grader_model}</span> : null}
      </label>

      {Object.keys(retrieval.data?.workflow_options ?? {}).length > 0 ? (
        <div className="banner banner--warn" style={{ marginBottom: 0 }}>
          <code>assistant.workflow_options</code> is also set in your config and takes
          precedence over these values:{" "}
          <code>{JSON.stringify(retrieval.data?.workflow_options)}</code>
        </div>
      ) : null}

      {save.isError ? (
        <div className="banner banner--error" style={{ marginBottom: 0 }}>
          {(save.error as Error).message}
        </div>
      ) : null}
      {save.isSuccess ? (
        <div className="banner" style={{ marginBottom: 0 }}>
          Saved and applied{save.data.wrote_config ? ", and written to the config file" : ""}.
        </div>
      ) : null}

      <div>
        <button className="btn btn--primary" onClick={() => save.mutate()} disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save retrieval settings"}
        </button>
      </div>
    </div>
  );
}
