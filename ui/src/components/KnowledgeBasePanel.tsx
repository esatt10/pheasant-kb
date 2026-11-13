import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";

/**
 * Edit the live knowledge base's identity.
 *
 * The description is free to change. The name is not cosmetic — it *is* the
 * knowledge-base id: the graph's root node, the prefix of every stable
 * artifact id, and the identity a Synapse router routes to. Renaming is
 * allowed, but the panel says what it costs (a full re-index) rather than
 * accepting it silently and leaving the user with an apparently-empty
 * knowledge base and no explanation.
 */
export function KnowledgeBasePanel() {
  const queryClient = useQueryClient();
  const kb = useQuery({ queryKey: ["knowledge-base"], queryFn: api.knowledgeBase });
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  useEffect(() => {
    if (kb.data) {
      setName(kb.data.name);
      setDescription(kb.data.description);
    }
  }, [kb.data]);

  const update = useMutation({
    mutationFn: () => api.updateKnowledgeBase({ name, description }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["knowledge-base"] });
      queryClient.invalidateQueries({ queryKey: ["overview"] });
    },
  });

  if (kb.isLoading) return <p className="muted small">Loading…</p>;
  if (kb.isError) return <div className="banner banner--error">{(kb.error as Error).message}</div>;

  const renaming = kb.data !== undefined && name !== kb.data.name;
  const dirty = renaming || (kb.data !== undefined && description !== kb.data.description);

  return (
    <div className="form-grid">
      <label className="field">
        <span>Name</span>
        <input
          className="text-input"
          value={name}
          spellCheck={false}
          onChange={(event) => setName(event.target.value)}
        />
      </label>

      {renaming ? (
        <div className="banner banner--warn" style={{ marginBottom: 0 }}>
          Renaming changes this knowledge base's ID. The graph you have indexed
          belongs to <code>{kb.data?.name}</code>, so after a restart this one will
          look empty until you run a full re-index:{" "}
          <code>pheasant sync --all --mode full</code>.
        </div>
      ) : null}

      <label className="field">
        <span>Description</span>
        <input
          className="text-input"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
        <span className="muted small">
          Shown in the UI, and published in the Synapse contract if this region joins
          a fleet.
        </span>
      </label>

      <div className="muted small">
        <div>
          Config: <code>{kb.data?.config_path}</code>
        </div>
        <div>
          State: <code>{kb.data?.state_path}</code>
        </div>
        <div>Version: {kb.data?.version}</div>
      </div>

      {update.isError ? (
        <div className="banner banner--error" style={{ marginBottom: 0 }}>
          {(update.error as Error).message}
        </div>
      ) : null}
      {update.isSuccess ? (
        <div className="banner" style={{ marginBottom: 0 }}>
          Saved.
          {update.data.restart_required
            ? " Restart pheasant to apply it."
            : " Applied to the running server."}
          {update.data.rename ? ` ${update.data.rename.detail}` : ""}
        </div>
      ) : null}

      <div>
        <button data-permission="security.manage"
          className="btn btn--primary"
          disabled={!dirty || update.isPending || !name.trim()}
          onClick={() => update.mutate()}
        >
          {update.isPending ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
  );
}
