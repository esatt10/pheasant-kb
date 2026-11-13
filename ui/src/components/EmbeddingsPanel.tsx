import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { EmbeddingsStatus } from "../api/types";

/**
 * Semantic search, configured without touching YAML.
 *
 * Two things make this more than a form over `search.embeddings`:
 *
 * - **Coverage.** Turning embeddings on only affects *future* syncs; content
 *   already indexed has no vector until something embeds it. The bar makes
 *   that visible, and "Build vectors" fixes it without re-reading a single
 *   source file.
 * - **Invalidation.** Changing the model or dimension puts old vectors in a
 *   different space, where similarity is meaningless. The server flags it and
 *   the rebuild can drop them rather than quietly mixing two spaces.
 */
export function EmbeddingsPanel() {
  const queryClient = useQueryClient();
  const status = useQuery({ queryKey: ["embeddings"], queryFn: api.embeddings });
  const [draft, setDraft] = useState<Partial<EmbeddingsStatus>>({});
  const [notice, setNotice] = useState<string | null>(null);

  // Re-seed the form whenever the server's view changes.
  useEffect(() => {
    if (status.data) setDraft({});
  }, [status.dataUpdatedAt, status.data]);

  // Keyed on presence in `draft`, not `??` — a field the user explicitly
  // cleared back to "unset" (e.g. dimensions, to fall back to the model's
  // own default) has a real `null` in draft, which `??` would otherwise
  // skip past to the server's last value.
  const value = <K extends keyof EmbeddingsStatus>(key: K): EmbeddingsStatus[K] | undefined =>
    (key in draft ? draft[key] : status.data?.[key]) as EmbeddingsStatus[K] | undefined;

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["embeddings"] });
    queryClient.invalidateQueries({ queryKey: ["overview"] });
  };

  const save = useMutation({
    mutationFn: (body: Parameters<typeof api.updateEmbeddings>[0]) => api.updateEmbeddings(body),
    onSuccess: (data) => {
      setDraft({});
      invalidate();
      setNotice(
        data.vectors_invalidated
          ? `Saved. The embedding space changed, so ${data.vectors_dropped ?? 0} incompatible ` +
            "vectors were cleared. Build vectors before relying on semantic search."
          : data.wrote_config
            ? "Saved to your config file."
            : "Saved to the running process.",
      );
    },
  });

  const rebuild = useMutation({
    mutationFn: (dropExisting: boolean) => api.rebuildVectors(dropExisting),
    onSuccess: (data) => {
      invalidate();
      setNotice(
        data.embedded_chunks === 0
          ? "Everything was already embedded — nothing to do."
          : `Embedded ${data.embedded_chunks} passage${data.embedded_chunks === 1 ? "" : "s"}.`,
      );
    },
  });

  if (status.isLoading) {
    return (
      <p className="muted small">
        <span className="spinner" /> Loading…
      </p>
    );
  }
  if (status.isError) {
    return <div className="banner banner--error">{(status.error as Error).message}</div>;
  }
  const data = status.data as EmbeddingsStatus;
  const provider = data.providers.find((p) => p.id === value("provider"));
  const coverage = Math.round((data.coverage ?? 0) * 100);
  const dirty = Object.keys(draft).length > 0;

  // Backends whose optional dependency is missing cannot be chosen — offering
  // them would only produce a store that builds and then fails on first use.
  const usableStores = data.store_providers.filter((entry) => entry.available);
  const unavailableStores = data.store_providers.filter((entry) => !entry.available);
  const currentStore = String(value("store_provider") ?? "");
  // Switching on has to land on a backend that works, whatever the config's
  // default happens to be in this deployment.
  const storeForEnable = usableStores.some((entry) => entry.id === currentStore)
    ? currentStore
    : (usableStores[0]?.id ?? currentStore);

  return (
    <div className="form-grid">
      <div className="banner banner--info" style={{ marginBottom: 0 }}>
        <span>
          Embeddings add semantic (<code>vector</code>) search alongside the lexical and
          graph modes, and give the agentic workflow a second way to find evidence.
          Indexing stays deterministic either way — the embedder is the only network call
          in the sync path, and the <code>stub</code> provider removes even that.
        </span>
      </div>

      <label className="checkbox">
        <input data-permission="security.manage"
          type="checkbox"
          checked={Boolean(value("enabled"))}
          onChange={(event) =>
            save.mutate({
              enabled: event.target.checked,
              provider: value("provider"),
              store_provider: storeForEnable,
            })
          }
        />
        Enable semantic search
      </label>

      {unavailableStores.length > 0 ? (
        <p className="muted small" style={{ margin: 0 }}>
          Not installed here:{" "}
          {unavailableStores.map((entry, index) => (
            <span key={entry.id}>
              {index > 0 ? ", " : ""}
              {entry.label}
              {entry.hint ? (
                <>
                  {" "}
                  (<code>{entry.hint}</code>)
                </>
              ) : null}
            </span>
          ))}
          .
        </p>
      ) : null}

      {data.enabled && !data.active ? (
        <div className="banner banner--warn" style={{ marginBottom: 0 }}>
          Embeddings are switched on but no indexer could be built
          {data.store_error ? `: ${data.store_error}` : ""}. Check the provider and, for
          the <code>lancedb</code> store, that <code>pip install 'pheasant-kb[vector]'</code>{" "}
          has run.
        </div>
      ) : null}

      {data.active ? (
        <div className="field">
          <span>Coverage</span>
          <div className="meter" title={`${data.vector_count} of ${data.chunk_count} passages`}>
            <div className="meter__fill" style={{ width: `${Math.min(100, coverage)}%` }} />
          </div>
          <span className="muted small">
            {data.vector_count} of {data.chunk_count} indexed passages embedded ({coverage}%)
            {data.dimensions_on_disk ? ` · ${data.dimensions_on_disk} dimensions on disk` : ""}
          </span>
        </div>
      ) : null}

      <div className="field">
        <span>Provider</span>
        <div className="radio-cards">
          {data.providers.map((entry) => (
            <button
              key={entry.id}
              className={`radio-card${value("provider") === entry.id ? " active" : ""}`}
              onClick={() => setDraft((prev) => ({ ...prev, provider: entry.id }))}
            >
              <span className="radio-card__title">{entry.label}</span>
              <span className="radio-card__sub">{entry.description}</span>
            </button>
          ))}
        </div>
      </div>

      <label className="field">
        <span>Model</span>
        <input
          className="text-input"
          value={String(value("model") ?? "")}
          onChange={(event) => setDraft((prev) => ({ ...prev, model: event.target.value }))}
        />
      </label>

      <div className="field-row">
        <label className="field">
          <span>Dimensions</span>
          <input
            className="text-input"
            type="number"
            min={16}
            max={4096}
            placeholder="model default"
            value={value("dimensions") ?? ""}
            onChange={(event) =>
              setDraft((prev) => ({
                ...prev,
                // Blank = unset = let the model use its own native size;
                // do not fall back to a fixed number here, it would defeat
                // the point of leaving this blank.
                dimensions: event.target.value === "" ? null : Number(event.target.value),
              }))
            }
          />
          <span className="muted small">
            Leave blank to use {provider?.label ?? "the provider"}&rsquo;s own native size.
            Only set a number to shrink vectors for storage or to pin an exact size across a
            Synapse fleet.
          </span>
        </label>
        <label className="field">
          <span>Batch size</span>
          <input
            className="text-input"
            type="number"
            min={1}
            max={512}
            value={Number(value("batch_size") ?? 64)}
            onChange={(event) =>
              setDraft((prev) => ({ ...prev, batch_size: Number(event.target.value) }))
            }
          />
        </label>
        <label className="field">
          <span>Vector store</span>
          <select
            className="text-input"
            value={currentStore || storeForEnable}
            onChange={(event) =>
              setDraft((prev) => ({ ...prev, store_provider: event.target.value }))
            }
          >
            {usableStores.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.label}
              </option>
            ))}
            {/* Keep the configured backend selectable even when its extra is
                missing, so the form shows the truth rather than silently
                reassigning it. */}
            {usableStores.some((entry) => entry.id === currentStore) ? null : (
              <option value={currentStore}>{currentStore} (not installed)</option>
            )}
          </select>
        </label>
      </div>

      {provider?.needs_key ? (
        <>
          <label className="field">
            <span>Endpoint</span>
            <input
              className="text-input"
              value={String(value("base_url") ?? "")}
              onChange={(event) => setDraft((prev) => ({ ...prev, base_url: event.target.value }))}
            />
          </label>
          <label className="field">
            <span>API key environment variable</span>
            <input
              className="text-input"
              value={String(value("api_key_env") ?? "")}
              onChange={(event) =>
                setDraft((prev) => ({ ...prev, api_key_env: event.target.value }))
              }
            />
            <span className="muted small">
              The variable <em>name</em>, read from the container environment. The key itself
              never enters your config.{" "}
              {data.api_key_present ? (
                <strong>Currently set.</strong>
              ) : (
                <strong>Not set on this server.</strong>
              )}
            </span>
          </label>
        </>
      ) : null}

      {notice ? (
        <div className="banner banner--info" style={{ marginBottom: 0 }}>
          {notice}
        </div>
      ) : null}
      {save.isError ? (
        <div className="banner banner--error" style={{ marginBottom: 0 }}>
          {(save.error as Error).message}
        </div>
      ) : null}
      {rebuild.isError ? (
        <div className="banner banner--error" style={{ marginBottom: 0 }}>
          {(rebuild.error as Error).message}
        </div>
      ) : null}

      <div className="button-row">
        <button data-permission="security.manage"
          className="btn btn--primary"
          disabled={!dirty || save.isPending}
          onClick={() =>
            save.mutate({
              provider: value("provider"),
              model: value("model"),
              base_url: value("base_url"),
              api_key_env: value("api_key_env"),
              dimensions: value("dimensions"),
              batch_size: value("batch_size"),
              store_provider: value("store_provider"),
            })
          }
        >
          {save.isPending ? "Saving…" : "Save"}
        </button>
        <button data-permission="security.manage"
          className="btn"
          disabled={!data.active || rebuild.isPending}
          onClick={() => rebuild.mutate(false)}
          title="Embed indexed passages that have no vector yet"
        >
          {rebuild.isPending ? "Building…" : "Build missing vectors"}
        </button>
        <button data-permission="security.manage"
          className="btn"
          disabled={!data.active || rebuild.isPending}
          onClick={() => rebuild.mutate(true)}
          title="Discard every vector and embed from scratch"
        >
          Rebuild from scratch
        </button>
      </div>
    </div>
  );
}
