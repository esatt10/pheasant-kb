import { FormEvent, useEffect, useState } from "react";
import {
  api,
  getApiToken,
  setApiToken,
  type AuthConfig,
  type AuthIdentity,
  type PersonalKey,
} from "../api/client";
import { CopyLine } from "./CopyLine";
import { usePermissions } from "../state/permissions";

const METHOD_LABELS: Record<string, string> = {
  api_token: "shared token",
  entra_sso: "Microsoft sign-in",
  entra_token: "Microsoft token",
  personal_key: "personal key",
  mcp_oauth: "MCP sign-in",
};

/**
 * How this tab proves who it is. Two ways in, side by side: sign in with
 * Microsoft (when the region has Entra on), or paste a token -- the shared
 * fleet token, or a personal key minted here. A signed-in person can mint
 * personal keys for agents and scripts that cannot open a browser.
 */
export function ApiTokenDialog({
  onClose,
  onSaved,
}: {
  onClose: () => void;
  onSaved: () => void;
}) {
  const [token, setToken] = useState(getApiToken);
  const [error, setError] = useState<string | null>(null);
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [identity, setIdentity] = useState<AuthIdentity | null>(null);

  useEffect(() => {
    api.authConfig().then(setConfig).catch(() => setConfig(null));
    if (getApiToken()) {
      api.me().then(setIdentity).catch(() => setIdentity(null));
    }
  }, []);

  function save(event: FormEvent) {
    event.preventDefault();
    if (!token.trim()) {
      setError("Paste a token to continue.");
      return;
    }
    setApiToken(token);
    onSaved();
    onClose();
  }

  async function disconnect() {
    if (identity?.auth_method === "entra_sso") {
      try {
        await api.signOut();
      } catch {
        /* the session may already have expired */
      }
    }
    setApiToken("");
    onSaved();
    onClose();
  }

  function signIn() {
    const next = window.location.pathname + window.location.search;
    window.location.assign(`/auth/login?next=${encodeURIComponent(next)}`);
  }

  const signedIn = Boolean(identity?.principal);

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal modal--narrow" onClick={(event) => event.stopPropagation()}>
        <header className="modal__header">
          <h2>Connect to this region</h2>
          <button className="btn btn--ghost btn--icon" type="button" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        {signedIn ? (
          <p className="small">
            Connected as <strong>{identity?.display_name || identity?.principal}</strong>
            <span className="muted">
              {" "}
              via {METHOD_LABELS[identity?.auth_method ?? ""] ?? identity?.auth_method}
              {identity?.role ? ` · ${identity.role}` : ""}
            </span>
          </p>
        ) : null}

        {config?.entra && !identity?.person ? (
          <section className="auth-section">
            <button className="btn btn--primary auth-signin" type="button" onClick={signIn}>
              Sign in with Microsoft
            </button>
            <p className="muted small">Use the work account this region has granted access to.</p>
          </section>
        ) : null}

        {identity?.person ? <PersonalKeys /> : null}

        <form onSubmit={save} className="auth-section">
          <label className="field">
            <span>{config?.entra ? "Or paste a token or personal key" : "Fleet API token"}</span>
            <input
              className="text-input"
              type="password"
              value={token}
              onChange={(event) => {
                setToken(event.target.value);
                setError(null);
              }}
              placeholder="Paste the bearer token"
              autoComplete="off"
            />
          </label>
          {error ? <p className="error small">{error}</p> : null}
          <p className="muted small">Kept only in this tab's session storage.</p>
          <footer className="modal__footer">
            {getApiToken() ? (
              <button className="btn btn--danger" type="button" onClick={disconnect}>
                {identity?.auth_method === "entra_sso" ? "Sign out" : "Disconnect"}
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
    </div>
  );
}

/** Keys a signed-in person mints for agents and scripts; each shown once. */
function PersonalKeys() {
  const { can } = usePermissions();
  const [role, setRole] = useState("");
  const [keys, setKeys] = useState<PersonalKey[]>([]);
  const [name, setName] = useState("");
  const [days, setDays] = useState(90);
  const [created, setCreated] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  function refresh() {
    api
      .personalKeys()
      .then((body) => setKeys(body.keys))
      .catch((err) => setError(String(err?.message ?? err)));
  }

  useEffect(refresh, []);

  async function create(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const body = await api.createPersonalKey({ name: name.trim() || "key", days, role: role || undefined });
      setCreated(body.key);
      setName("");
      refresh();
    } catch (err) {
      setError(String((err as Error)?.message ?? err));
    }
  }

  async function revoke(id: string) {
    await api.revokePersonalKey(id);
    refresh();
  }

  const live = keys.filter((key) => !key.revoked_at);
  return (
    <section className="auth-section">
      <h3 className="auth-heading">Personal keys</h3>
      <p className="muted small">
        For MCP clients and scripts that cannot sign in through a browser. Send one as{" "}
        <code>Authorization: Bearer &lt;key&gt;</code>; every call is logged under your name.
      </p>
      {created ? (
        <div className="auth-created">
          <p className="small">Copy this key now. It is not shown again.</p>
          <CopyLine command={created} />
        </div>
      ) : null}
      {live.length ? (
        <ul className="auth-keys">
          {live.map((key) => (
            <li key={key.id}>
              <span>
                <strong>{key.name}</strong>
                <span className="muted small">
                  {" "}
                  expires {key.expires_at?.slice(0, 10)}
                  {key.last_used_at ? `, last used ${key.last_used_at.slice(0, 10)}` : ", unused"}
                </span>
              </span>
              <button className="btn btn--small btn--danger" type="button" onClick={() => revoke(key.id)}>
                Revoke
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      <form className="auth-keyform" onSubmit={create}>
        <select className="text-input" aria-label="Key role" value={role} onChange={event => setRole(event.target.value)}>
          <option value="">My current permissions</option>
          <option value="Reader">Reader</option>
          {can("sources.manage") ? <option value="Contributor">Contributor</option> : null}
          {can("security.manage") ? <option value="Admin">Admin</option> : null}
        </select>
        <input
          className="text-input"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="Key name, e.g. claude-code laptop"
          aria-label="Key name"
        />
        <select
          className="text-input"
          value={days}
          onChange={(event) => setDays(Number(event.target.value))}
          aria-label="Lifetime"
        >
          <option value={30}>30 days</option>
          <option value={90}>90 days</option>
          <option value={365}>1 year</option>
        </select>
        <button className="btn" type="submit">
          Create key
        </button>
      </form>
      {error ? <p className="error small">{error}</p> : null}
    </section>
  );
}
