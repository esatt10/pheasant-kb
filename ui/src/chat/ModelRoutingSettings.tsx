import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { CatalogModel, ModelCatalog, ModelRoutingPolicy } from "../api/types";
import "./ModelRoutingSettings.css";

type Level = "low" | "medium" | "high";
type Choice = "auto" | "one";

const STRENGTHS: { value: Level | ""; label: string; title: string }[] = [
  { value: "", label: "Auto", title: "Let pheasant infer this model's strength from its description" },
  { value: "low", label: "Light", title: "Quick lookups and short answers" },
  { value: "medium", label: "Medium", title: "Comparisons, walkthroughs, a few sections" },
  { value: "high", label: "Deep", title: "Hard reasoning, code, long detailed answers" },
];

/** The maker a catalog entry belongs to, as the routing service reads it. */
export function seriesOf(model: CatalogModel): string {
  const family = model.family?.trim().toLowerCase();
  if (family) return family;
  if (model.provider === "azure" && model.model.startsWith("gpt-")) return "openai";
  return model.provider ?? "other";
}

const FAMILY_LABEL: Record<string, string> = { openai: "OpenAI", anthropic: "Anthropic" };

/** What the conversation answers with, in the reader's words. */
export function routingSummary(policy: ModelRoutingPolicy, catalog: ModelCatalog): string {
  const label = (id: string | null | undefined) =>
    catalog.chat.find((model) => model.id === id)?.label ?? id ?? "the region's model";
  if (policy.mode === "off") return label(catalog.default_chat);
  if (policy.mode === "custom" && policy.models.length === 1) return label(policy.models[0]);
  if (policy.mode === "custom") return `Automatic · ${policy.models.length} models`;
  return `Automatic · ${FAMILY_LABEL[policy.mode] ?? policy.mode}`;
}

function choiceOf(policy: ModelRoutingPolicy): Choice {
  return policy.mode === "off" || (policy.mode === "custom" && policy.models.length === 1)
    ? "one"
    : "auto";
}

/**
 * The composer's model control: a button naming what answers this
 * conversation, and the dialog that changes it.
 *
 * Two plain choices, because "which model answers" has only two answers a
 * reader means: one model every time, or let pheasant pick per question. The
 * routing policy underneath (series, custom set, rubric, strengths) is shaped
 * from those choices; nobody has to know its field names.
 */
export function ModelRoutingSettings({
  catalog,
  value,
  onChange,
  decisionModel,
  onDecisionModel,
}: {
  catalog: ModelCatalog;
  value: ModelRoutingPolicy | null;
  onChange: (policy: ModelRoutingPolicy | null) => void;
  decisionModel?: string | null;
  onDecisionModel?: (id: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const policy = value ?? catalog.routing.settings;
  return (
    <>
      <button
        ref={button}
        type="button"
        className="btn btn--small model-routing__trigger"
        aria-haspopup="dialog"
        aria-expanded={open}
        title="Choose which model answers in this conversation"
        onClick={() => setOpen(true)}
      >
        <span className="model-routing__trigger-label">Model</span>
        {routingSummary(policy, catalog)}
        {value ? <span className="model-routing__dot" title="Changed for this conversation" /> : null}
      </button>
      {/* Portalled: rendered in the composer's hint row it inherited that row's
          muted colour and small type. */}
      {open ? createPortal(
        <ModelRoutingDialog
          catalog={catalog}
          value={value}
          onChange={onChange}
          decisionModel={decisionModel ?? null}
          onDecisionModel={onDecisionModel}
          onClose={() => {
            setOpen(false);
            button.current?.focus();
          }}
        />,
        document.body,
      ) : null}
    </>
  );
}

function ModelRoutingDialog({
  catalog,
  value,
  onChange,
  decisionModel,
  onDecisionModel,
  onClose,
}: {
  catalog: ModelCatalog;
  value: ModelRoutingPolicy | null;
  onChange: (policy: ModelRoutingPolicy | null) => void;
  decisionModel: string | null;
  onDecisionModel?: (id: string | null) => void;
  onClose: () => void;
}) {
  const client = useQueryClient();
  const dialog = useRef<HTMLDivElement>(null);
  const policy = value ?? catalog.routing.settings;
  const choice = choiceOf(policy);
  const models = catalog.chat.filter((model) => model.id !== "configured");
  const families: string[] = [...new Set(models.map(seriesOf))].filter(
    (f) => f === "openai" || f === "anthropic",
  );
  const defaultModel = models.find((model) => model.id === catalog.default_chat);
  const selected =
    policy.mode === "off" ? catalog.default_chat : policy.mode === "custom" ? policy.models[0] : null;

  const save = useMutation({
    mutationFn: () => api.saveModelRouting(policy),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["models"] }),
  });
  const update = (next: ModelRoutingPolicy) => {
    save.reset();
    onChange(next);
  };

  // Esc closes; focus starts inside and stays there while the dialog is open.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.querySelector<HTMLElement>("[data-autofocus], button")?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      if (previous && document.contains(previous) && !dialog.current) previous.focus();
    };
  }, [onClose]);

  const pickOne = (id: string) =>
    update(
      id === catalog.default_chat
        ? { ...policy, mode: "off", models: [] }
        : { ...policy, mode: "custom", models: [id] },
    );
  // Automatic starts from the default model's maker, else every available model.
  const pickAuto = () => {
    const family = seriesOf(defaultModel ?? models[0]);
    update(
      families.includes(family)
        ? { ...policy, mode: family as ModelRoutingPolicy["mode"], models: [] }
        : { ...policy, mode: "custom", models: models.filter((m) => m.available).map((m) => m.id) },
    );
  };
  const candidates =
    policy.mode === "custom"
      ? models
      : models.filter((model) => seriesOf(model) === policy.mode);
  const strength = (model: CatalogModel): Level | "" =>
    policy.reasoning_levels[model.id] ?? (model.reasoning_level as Level | null) ?? "";
  const setStrength = (model: CatalogModel, level: Level | "") => {
    const levels = { ...policy.reasoning_levels };
    if (level) levels[model.id] = level;
    else delete levels[model.id];
    update({ ...policy, reasoning_levels: levels });
  };
  const autoInvalid = choice === "auto" && policy.mode === "custom" && policy.models.length < 2;

  return (
    <div className="modal-scrim" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div
        className="modal model-routing"
        role="dialog"
        aria-modal="true"
        aria-labelledby="model-routing-title"
        ref={dialog}
      >
        <header className="modal__header">
          <h2 id="model-routing-title">Answering model</h2>
          <button type="button" className="btn btn--ghost btn--icon" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>
        <p className="model-routing__lead">
          Choose which model writes the answers in this conversation.
        </p>

        <div className="radio-cards model-routing__choice" role="radiogroup" aria-label="How to choose">
          <button
            type="button"
            role="radio"
            aria-checked={choice === "auto"}
            className={`radio-card${choice === "auto" ? " active" : ""}`}
            onClick={() => choice !== "auto" && pickAuto()}
            data-autofocus={choice === "auto" ? "" : undefined}
          >
            <span className="radio-card__title">Automatic</span>
            <span className="radio-card__sub">
              Picks the right model for each question: a light one for quick lookups, a stronger
              one for hard or detailed answers.
            </span>
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={choice === "one"}
            className={`radio-card${choice === "one" ? " active" : ""}`}
            onClick={() => choice !== "one" && pickOne(catalog.default_chat ?? models[0].id)}
            data-autofocus={choice === "one" ? "" : undefined}
          >
            <span className="radio-card__title">One model</span>
            <span className="radio-card__sub">Every answer comes from the model you choose.</span>
          </button>
        </div>

        {choice === "one" ? (
          <section className="model-routing__section" aria-label="Model">
            <h3 className="model-routing__heading">Model</h3>
            <div className="model-routing__list" role="radiogroup">
              {models.map((model) => (
                <button
                  key={model.id}
                  type="button"
                  role="radio"
                  aria-checked={selected === model.id}
                  disabled={!model.available}
                  className={`model-routing__model${selected === model.id ? " active" : ""}`}
                  onClick={() => pickOne(model.id)}
                >
                  <span className="model-routing__model-name">
                    {model.label}
                    {model.id === catalog.default_chat ? (
                      <span className="pill pill--accent">region default</span>
                    ) : null}
                    {!model.available ? <span className="pill pill--warn">unavailable</span> : null}
                  </span>
                  {model.description ? (
                    <span className="model-routing__model-note">{model.description}</span>
                  ) : null}
                </button>
              ))}
            </div>
          </section>
        ) : (
          <section className="model-routing__section" aria-label="Choose from">
            <h3 className="model-routing__heading">Choose from</h3>
            <div className="segmented model-routing__pool" role="group" aria-label="Choose from">
              {families.map((family) => (
                <button
                  key={family}
                  type="button"
                  className={policy.mode === family ? "active" : ""}
                  aria-pressed={policy.mode === family}
                  onClick={() => update({ ...policy, mode: family as ModelRoutingPolicy["mode"], models: [] })}
                >
                  All {FAMILY_LABEL[family]} models
                </button>
              ))}
              <button
                type="button"
                className={policy.mode === "custom" ? "active" : ""}
                aria-pressed={policy.mode === "custom"}
                onClick={() =>
                  update({
                    ...policy,
                    mode: "custom",
                    models:
                      policy.mode === "custom"
                        ? policy.models
                        : candidates.filter((m) => m.available).map((m) => m.id),
                  })
                }
              >
                Models I pick
              </button>
            </div>
            <p className="model-routing__note">
              For each question pheasant weighs how hard it is and how detailed an answer it asks
              for, then uses the lightest model that can do it well. Set a model's strength to steer
              that balance.
            </p>
            <div className="model-routing__list">
              {candidates.map((model) => {
                const included = policy.mode !== "custom" || policy.models.includes(model.id);
                return (
                  <div
                    key={model.id}
                    className={`model-routing__model model-routing__model--row${included ? " active" : ""}`}
                  >
                    {policy.mode === "custom" ? (
                      <label className="model-routing__include">
                        <input
                          type="checkbox"
                          checked={included}
                          disabled={!model.available && !included}
                          onChange={(event) =>
                            update({
                              ...policy,
                              models: event.target.checked
                                ? [...policy.models, model.id]
                                : policy.models.filter((id) => id !== model.id),
                            })
                          }
                        />
                        <span className="model-routing__model-name">{model.label}</span>
                      </label>
                    ) : (
                      <span className="model-routing__model-name">{model.label}</span>
                    )}
                    {model.description ? (
                      <span className="model-routing__model-note">{model.description}</span>
                    ) : null}
                    {!model.available ? <span className="pill pill--warn">unavailable</span> : null}
                    <div className="segmented model-routing__strength" role="group" aria-label={`Strength of ${model.label}`}>
                      {STRENGTHS.map((option) => (
                        <button
                          key={option.label}
                          type="button"
                          title={option.title}
                          className={strength(model) === option.value ? "active" : ""}
                          aria-pressed={strength(model) === option.value}
                          disabled={!included}
                          onClick={() => setStrength(model, option.value)}
                        >
                          {option.label}
                        </button>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
            {autoInvalid ? (
              <p className="error small" role="alert">
                Pick at least two models to choose between, or switch to One model.
              </p>
            ) : null}
            <details className="model-routing__advanced">
              <summary>Selection guidance</summary>
              <p className="model-routing__note">
                The instructions the selector follows. Write your own to favour, say, clear prose or
                low cost.
              </p>
              <textarea
                className="text-input model-routing__criteria"
                aria-label="Selection guidance"
                rows={6}
                maxLength={12000}
                value={policy.criteria}
                onChange={(event) => update({ ...policy, criteria: event.target.value })}
              />
              <button
                type="button"
                className="btn btn--small"
                onClick={() =>
                  update({ ...policy, criteria: catalog.routing.defaults.criteria, reasoning_levels: {} })
                }
              >
                Reset guidance and strengths
              </button>
            </details>
          </section>
        )}

        {catalog.decisions.length > 1 && onDecisionModel ? (
          <details className="model-routing__advanced">
            <summary>Judgement model</summary>
            <p className="model-routing__note">
              Grades whether the evidence is enough and whether an answer is supported, and picks
              the model in Automatic.
            </p>
            <div className="segmented" role="group" aria-label="Judgement model">
              {catalog.decisions.map((model) => {
                const active = (decisionModel ?? catalog.default_decision) === model.id;
                return (
                  <button
                    key={model.id}
                    type="button"
                    className={active ? "active" : ""}
                    aria-pressed={active}
                    disabled={!model.available}
                    title={model.description ?? undefined}
                    onClick={() => onDecisionModel(model.id === catalog.default_decision ? null : model.id)}
                  >
                    {model.label}
                    {model.id === catalog.default_decision ? " (default)" : ""}
                  </button>
                );
              })}
            </div>
          </details>
        ) : null}

        <footer className="model-routing__footer">
          <span className="model-routing__note">
            {value ? "Applies to this conversation." : "Using the region's default."}
          </span>
          <div className="model-routing__actions">
            {value ? (
              <button type="button" className="btn btn--small" onClick={() => { save.reset(); onChange(null); }}>
                Use region default
              </button>
            ) : null}
            {catalog.routing.can_save && value ? (
              <button
                type="button"
                className="btn btn--small"
                disabled={autoInvalid || !policy.criteria.trim() || save.isPending}
                onClick={() => save.mutate()}
                title="Make this everyone's default in this knowledge base"
              >
                {save.isPending ? "Saving…" : "Save as region default"}
              </button>
            ) : null}
            <button type="button" className="btn btn--primary btn--small" onClick={onClose}>
              Done
            </button>
          </div>
        </footer>
        {save.isSuccess ? (
          <p className="model-routing__status" role="status">
            Saved: this is now the default for everyone.
          </p>
        ) : null}
        {save.isError ? (
          <p className="error small" role="alert">
            {save.error.message}
          </p>
        ) : null}
      </div>
    </div>
  );
}
