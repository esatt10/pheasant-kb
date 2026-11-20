import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ModelPrice, ModelPrices } from "../api/admin";
import { operatorOnly } from "./ModelsPanel";

const FIELDS: { key: keyof ModelPrice; label: string }[] = [
  { key: "input", label: "Input" },
  { key: "output", label: "Output" },
  { key: "cached_input", label: "Cached input" },
];

const shown = (value: number | null | undefined) => (value == null ? "—" : String(value));

/**
 * What each model costs per million tokens, and the operator's runtime changes
 * to it. A change applies to calls made after it: what was already spent keeps
 * the price it was charged at. Only an operator admin sees or changes prices
 * (`OPERATOR_REQUIRED` for anyone else), so the section is absent for them.
 */
export function PricesSection() {
  const queryClient = useQueryClient();
  const prices = useQuery({ queryKey: ["model-prices"], queryFn: api.modelPrices, retry: false });
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const save = useMutation({
    mutationFn: (change: Record<string, Partial<ModelPrice> | null>) => api.setModelPrices(change),
    onSuccess: (data) => {
      queryClient.setQueryData(["model-prices"], data);
      setEditing(null);
    },
  });

  if (prices.isLoading || (prices.isError && operatorOnly(prices.error))) return null;
  if (prices.isError)
    return <div className="banner banner--error">{(prices.error as Error).message}</div>;
  const data: ModelPrices = prices.data!;
  if (!data.models.length) return null;

  const begin = (id: string, effective: ModelPrice | null) => {
    setEditing(id);
    setDraft(
      Object.fromEntries(FIELDS.map(({ key }) => [key, effective?.[key] == null ? "" : String(effective[key])])),
    );
  };
  const changed = (effective: ModelPrice | null): Partial<ModelPrice> | null => {
    const out: Partial<ModelPrice> = {};
    for (const { key } of FIELDS) {
      const text = (draft[key] ?? "").trim();
      if (text === "") continue;
      const value = Number(text);
      if (!Number.isFinite(value) || value < 0) return null;
      if (value !== effective?.[key]) out[key] = value;
    }
    return out;
  };

  return (
    <section className="section prices-section">
      <h2 className="section__title">Prices</h2>
      <p className="muted small">
        What each model costs per million tokens, in {data.currency}. {data.note} Change one when
        your provider changes its price.
      </p>
      {save.isError ? (
        <div className="banner banner--error">{(save.error as Error).message}</div>
      ) : null}
      <div className="table-scroll">
        <table className="data-table prices-table">
          <thead>
            <tr>
              <th>Model</th>
              {FIELDS.map(({ key, label }) => (
                <th key={key}>{label}</th>
              ))}
              <th />
            </tr>
          </thead>
          <tbody>
            {data.models.map((model) => {
              const open = editing === model.id;
              const update = open ? changed(model.effective) : null;
              return (
                <tr key={model.id}>
                  <td>
                    {model.label}
                    <div className="muted small">
                      {model.kind}
                      {model.override ? " · set here" : ""}
                      {model.effective ? "" : " · no price, not counted"}
                    </div>
                  </td>
                  {FIELDS.map(({ key, label }) => (
                    <td key={key}>
                      {open ? (
                        <input
                          className="text-input text-input--small prices-input"
                          type="number"
                          inputMode="decimal"
                          min={0}
                          step="any"
                          aria-label={`${label} price for ${model.label}`}
                          value={draft[key] ?? ""}
                          onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}
                        />
                      ) : (
                        <span title={`Catalog: ${shown(model.catalog[key])}`}>
                          {key === "cached_input" &&
                          model.effective?.cached_input == null &&
                          model.effective?.input != null
                            ? "= input"
                            : shown(model.effective?.[key])}
                        </span>
                      )}
                    </td>
                  ))}
                  <td className="admin-actions">
                    {open ? (
                      <>
                        <button
                          type="button"
                          className="btn btn--small btn--primary"
                          disabled={save.isPending || !update || !Object.keys(update).length}
                          onClick={() => update && save.mutate({ [model.id]: update })}
                        >
                          Save
                        </button>
                        <button type="button" className="btn btn--small" onClick={() => setEditing(null)}>
                          Cancel
                        </button>
                      </>
                    ) : (
                      <>
                        <button
                          type="button"
                          className="btn btn--small"
                          onClick={() => begin(model.id, model.effective)}
                        >
                          Change
                        </button>
                        {model.override ? (
                          <button
                            type="button"
                            className="btn btn--small"
                            disabled={save.isPending}
                            onClick={() => save.mutate({ [model.id]: null })}
                            title="Go back to the deployment's price, from now on"
                          >
                            Use catalog price
                          </button>
                        ) : null}
                      </>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {data.history.length ? (
        <details className="prices-history">
          <summary>Price changes ({data.history.length})</summary>
          <ul>
            {data.history.slice(0, 20).map((change, index) => (
              <li key={`${change.at}-${change.model}-${index}`} className="small">
                {new Date(change.at).toLocaleString()} · {change.by} · <strong>{change.model}</strong>:{" "}
                {FIELDS.map(({ key, label }) => `${label} ${shown(change.from?.[key])} → ${shown(change.to?.[key])}`).join(", ")}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}
