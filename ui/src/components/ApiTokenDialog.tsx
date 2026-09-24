import { FormEvent, useState } from "react";
import { getApiToken, setApiToken } from "../api/client";

export function ApiTokenDialog({
  onClose,
  onSaved,
}: {
  onClose: () => void;
  onSaved: () => void;
}) {
  const [token, setToken] = useState(getApiToken);
  const [error, setError] = useState<string | null>(null);

  function save(event: FormEvent) {
    event.preventDefault();
    if (!token.trim()) {
      setError("Paste the fleet API token to continue.");
      return;
    }
    setApiToken(token);
    onSaved();
    onClose();
  }

  function disconnect() {
    setApiToken("");
    onSaved();
    onClose();
  }

  return (
    <div className="modal-scrim" onClick={onClose}>
      <form className="modal modal--narrow" onClick={(event) => event.stopPropagation()} onSubmit={save}>
        <header className="modal__header">
          <h2>Connect to this fleet</h2>
          <button className="btn btn--ghost btn--icon" type="button" onClick={onClose} aria-label="Close">
            âœ•
          </button>
        </header>

        <p className="muted small">
          This fleet requires a bearer token for source changes and other protected API calls.
          Paste <code>PHEASANT_API_TOKEN</code> from the local <code>.env</code> file.
        </p>

        <label className="field">
          <span>Fleet API token</span>
          <input
            className="text-input"
            type="password"
            value={token}
            onChange={(event) => {
              setToken(event.target.value);
              setError(null);
            }}
            placeholder="Paste the bearer token"
            autoFocus
            autoComplete="off"
          />
        </label>
        {error ? <p className="error small">{error}</p> : null}

        <p className="muted small">The token is kept only in this tab's session storage.</p>

        <footer className="modal__footer">
          {getApiToken() ? (
            <button className="btn btn--danger" type="button" onClick={disconnect}>
              Disconnect
            </button>
          ) : null}
          <button className="btn" type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn--primary" type="submit">
            Connect
          </button>
        </footer>
      </form>
    </div>
  );
}
