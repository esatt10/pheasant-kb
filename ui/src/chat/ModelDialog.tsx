import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api/client";
import type { AssistantProvider, AssistantStatus } from "../api/types";

interface ModelDialogProps {
  status?: AssistantStatus;
  sessionId: string | null;
  onSession: (sessionId: string | null) => void;
  onClose: () => void;
}

/**
 * Connect a chat model.
 *
 * Two routes, and the difference matters enough to spell out in the UI:
 *
 * - **Server environment** — the operator sets `ANTHROPIC_API_KEY` (or the
 *   OpenAI/Gemini equivalent) on the container. Nothing to do here; the
 *   provider card just shows as available.
 * - **This session** — the user pastes a key. It is sent once, held in the
 *   server's memory behind an opaque session id, never written to config,
 *   state or logs, and dropped on expiry, revoke, or restart.
 */
export function ModelDialog({ status, sessionId, onSession, onClose }: ModelDialogProps) {
  const providers = status?.providers ?? [];
  const [provider, setProvider] = useState<string>(
    status?.session?.provider ?? providers.find((p) => p.env_key_present)?.id ?? "anthropic",
  );
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");

  const selected: AssistantProvider | undefined = providers.find((p) => p.id === provider);

  const save = useMutation({
    mutationFn: () =>
      api.assistantKey({
        provider,
        api_key: apiKey.trim(),
        model: model.trim() || undefined,
      }),
    onSuccess: (data) => {
      onSession(data.session_id);
      setApiKey("");
      onClose();
    },
  });

  const revoke = useMutation({
    mutationFn: () => api.assistantRevoke(sessionId as string),
    onSuccess: () => {
      onSession(null);
      onClose();
    },
  });

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <header className="modal__header">
          <h2>Connect a chat model</h2>
          <button className="btn btn--ghost btn--icon" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        <p className="muted small" style={{ marginTop: 0 }}>
          pheasant answers from your own index either way. A model turns the retrieved
          passages into a written answer — retrieval, citations and graph facts work
          without one.
        </p>

        <div className="form-grid">
          <div className="field">
            <span>Provider</span>
            <div className="radio-cards">
              {providers.map((entry) => (
                <button
                  key={entry.id}
                  className={`radio-card${provider === entry.id ? " active" : ""}`}
                  onClick={() => setProvider(entry.id)}
                >
                  <span className="radio-card__title">{entry.label}</span>
                  <span className="radio-card__sub">
                    {entry.env_key_present ? "key set on server" : entry.default_model}
                  </span>
                </button>
              ))}
            </div>
          </div>

          {selected?.env_key_present ? (
            <div className="banner banner--info" style={{ marginBottom: 0 }}>
              <span>
                <strong>{selected.label}</strong> is already configured on the server via{" "}
                <code>{selected.api_key_env}</code>. You can start asking questions — or
                override it below with your own key for this session.
              </span>
            </div>
          ) : null}

          {status?.allow_session_keys === false ? (
            <div className="banner banner--warn" style={{ marginBottom: 0 }}>
              Session-supplied keys are disabled on this server. Set{" "}
              <code>{selected?.api_key_env}</code> in the container environment instead.
            </div>
          ) : (
            <>
              <label className="field">
                <span>API key (this browser session only)</span>
                <input
                  className="text-input"
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  placeholder={selected?.key_hint ?? "sk-…"}
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                />
              </label>
              <label className="field">
                <span>Model (optional)</span>
                <input
                  className="text-input"
                  placeholder={selected?.default_model ?? ""}
                  value={model}
                  onChange={(event) => setModel(event.target.value)}
                />
              </label>
              <p className="muted small" style={{ margin: 0 }}>
                Held in the server's memory only — never written to your config, the
                state database, or logs. It is dropped when you disconnect, when the
                session expires, or when the container restarts.
              </p>
            </>
          )}

          {save.isError ? <div className="error small">{(save.error as Error).message}</div> : null}
        </div>

        <div className="modal__footer">
          {sessionId ? (
            <button data-permission="security.manage"
              className="btn btn--danger"
              onClick={() => revoke.mutate()}
              disabled={revoke.isPending}
            >
              Disconnect
            </button>
          ) : null}
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button data-permission="security.manage"
            className="btn btn--primary"
            onClick={() => save.mutate()}
            disabled={!apiKey.trim() || save.isPending || status?.allow_session_keys === false}
          >
            {save.isPending ? "Connecting…" : "Connect"}
          </button>
        </div>
      </div>
    </div>
  );
}
