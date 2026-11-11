import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ConnectionProvider, ConnectionRecord } from "../api/types";

/**
 * Connected accounts: a row of toggles, one per service, each expanding the
 * entry that service needs.
 *
 * The catalog (`GET /connections`) says which fields each method takes, so a
 * provider added to the region appears here without a UI change, and the form
 * an agent fills over MCP (`connect_service`) is the form a person fills here.
 * A secret is sent once and never comes back: a saved connection shows the
 * account its service named and the last four characters.
 */

export function useConnections() {
  return useQuery({ queryKey: ["connections"], queryFn: api.connections, staleTime: 10_000 });
}

/** The provider whose hosts serve `target`, when it is a repository URL. */
export function providerForTarget(target: string, providers: ConnectionProvider[]): string | null {
  const trimmed = target.trim();
  if (/^gdrive:/i.test(trimmed)) return "google_drive";
  let host = "";
  try {
    host = new URL(trimmed).hostname.toLowerCase();
  } catch {
    return null;
  }
  const match = providers.find((p) =>
    p.hosts.some((known) => host === known || host.endsWith(`.${known}`)),
  );
  return match?.id ?? null;
}

interface TogglesProps {
  providers: ConnectionProvider[];
  connections: ConnectionRecord[];
  active: string | null;
  onToggle: (id: string | null) => void;
  label: string;
}

export function ProviderToggles({ providers, connections, active, onToggle, label }: TogglesProps) {
  return (
    <div className="connect-toggles" role="group" aria-label={label}>
      {providers.map((provider) => {
        const connected = connections.some((c) => c.provider === provider.id);
        const pressed = active === provider.id;
        return (
          <button
            key={provider.id}
            type="button"
            className={`connect-toggle${pressed ? " connect-toggle--on" : ""}`}
            aria-pressed={pressed}
            aria-expanded={pressed}
            onClick={() => onToggle(pressed ? null : provider.id)}
          >
            <span className="connect-toggle__label">{provider.label}</span>
            {connected ? (
              <span className="connect-toggle__badge" aria-label="connected">
                ✓
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

interface FormProps {
  provider: ConnectionProvider;
  onConnected: (record: ConnectionRecord) => void;
  /** Where a browser sign-in returns to. */
  returnTo?: string;
  vaultReason?: string | null;
}

export function ConnectForm({ provider, onConnected, returnTo, vaultReason }: FormProps) {
  const queryClient = useQueryClient();
  const [methodId, setMethodId] = useState(provider.methods[0]?.id ?? "token");
  const method = provider.methods.find((m) => m.id === methodId) ?? provider.methods[0];
  const [values, setValues] = useState<Record<string, string>>({});
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setValues({});
    setError(null);
  }, [provider.id, methodId]);

  const save = useMutation({
    mutationFn: async () => {
      setError(null);
      if (method.browser) {
        const started = await api.startSignIn({
          provider: provider.id,
          name: name.trim() || undefined,
          client_id: values.client_id?.trim() || undefined,
          client_secret: values.client_secret?.trim() || undefined,
          return_to: returnTo ?? window.location.pathname,
        });
        window.location.assign(started.authorize_url);
        return null;
      }
      const result = await api.connect({
        provider: provider.id,
        method: method.id,
        name: name.trim() || undefined,
        fields: values,
      });
      return result.connection;
    },
    onSuccess: (record) => {
      if (!record) return;
      queryClient.invalidateQueries({ queryKey: ["connections"] });
      setValues({});
      onConnected(record);
    },
    onError: (err) => setError((err as Error).message),
  });

  // A sign-in the region holds a client for is one button; without one, the
  // form says what to register and takes the client for this sign-in.
  const signIn = method.browser ? method.oauth : null;
  const ready = Boolean(signIn?.client_configured);
  const fields = signIn && ready ? [] : method.fields;
  const missing = signIn
    ? !ready &&
      (!values.client_id?.trim() || (signIn.secret_required && !values.client_secret?.trim()))
    : method.fields.some((f) => f.required && !values[f.name]?.trim());

  return (
    <form
      className="connect-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (!missing) save.mutate();
      }}
    >
      <p className="muted small connect-form__intro">
        {provider.description}
        {provider.token_url && !signIn ? (
          <>
            {" "}
            <a href={provider.token_url} target="_blank" rel="noreferrer">
              Create one ↗
            </a>
          </>
        ) : null}
      </p>
      {provider.methods.length > 1 ? (
        <div className="connect-methods" role="radiogroup" aria-label={`${provider.label} sign-in`}>
          {provider.methods.map((entry) => (
            <button
              key={entry.id}
              type="button"
              role="radio"
              aria-checked={entry.id === method.id}
              className={`connect-method${entry.id === method.id ? " connect-method--on" : ""}`}
              onClick={() => setMethodId(entry.id)}
            >
              {entry.label}
            </button>
          ))}
        </div>
      ) : null}
      {signIn && ready ? (
        <p className="small connect-form__intro">
          You sign in on {signIn.service}&apos;s own page and come straight back here. Your
          password never reaches this region.
        </p>
      ) : null}
      {signIn && !ready ? (
        <div className="banner banner--info connect-form__setup">
          This region has no {signIn.service} OAuth client yet. Register one once at{" "}
          <a href={signIn.register_url} target="_blank" rel="noreferrer">
            {signIn.service} ↗
          </a>{" "}
          with the callback <code>{window.location.origin + (signIn.callback_path ?? "")}</code>,
          then set <code>{signIn.client_id_env}</code>
          {signIn.secret_required ? (
            <>
              {" "}
              and <code>{signIn.client_secret_env}</code>
            </>
          ) : null}{" "}
          on the api and indexer, and this becomes one button. Or enter it below for now.
        </div>
      ) : null}
      {fields.map((field) => (
        <label className="field" key={field.name}>
          <span>
            {field.label}
            {field.required ||
            (signIn &&
              (field.name === "client_id" ||
                (field.name === "client_secret" && signIn.secret_required)))
              ? ""
              : " (optional)"}
          </span>
          {field.kind === "textarea" ? (
            <textarea
              className="text-area text-area--code"
              value={values[field.name] ?? ""}
              placeholder={field.placeholder}
              spellCheck={false}
              onChange={(e) => setValues({ ...values, [field.name]: e.target.value })}
            />
          ) : (
            <input
              className="text-input"
              type={field.secret ? "password" : field.kind === "url" ? "url" : "text"}
              autoComplete="off"
              spellCheck={false}
              value={values[field.name] ?? ""}
              placeholder={field.placeholder}
              onChange={(e) => setValues({ ...values, [field.name]: e.target.value })}
            />
          )}
          {field.help ? <span className="muted small">{field.help}</span> : null}
        </label>
      ))}
      <label className="field">
        <span>Connection name (optional)</span>
        <input
          className="text-input"
          value={name}
          placeholder={provider.id}
          spellCheck={false}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      {vaultReason ? <div className="banner banner--warn">{vaultReason}</div> : null}
      {error ? (
        <div className="banner banner--error" role="alert">
          {error}
        </div>
      ) : null}
      <div className="button-row">
        <button
          className="btn btn--primary"
          type="submit"
          disabled={missing || save.isPending || Boolean(vaultReason)}
        >
          {save.isPending
            ? signIn
              ? `Opening ${signIn.service}…`
              : provider.checkable
                ? "Checking…"
                : "Saving…"
            : signIn
              ? method.label
              : `Connect ${provider.label}`}
        </button>
      </div>
    </form>
  );
}

export function ConnectionLine({ record }: { record: ConnectionRecord }) {
  const account = record.details.account ?? record.check?.account;
  return (
    <span className="connection-line">
      <strong>{record.label}</strong>
      {record.name !== record.provider ? <span className="muted"> · {record.name}</span> : null}
      {account ? <span> · {account}</span> : null}
      {record.details.hint && record.details.hint !== "…" ? (
        <span className="muted"> · {record.details.hint}</span>
      ) : null}
      {record.check ? (
        <span className={record.check.ok ? "connection-ok" : "connection-warn"}>
          {" "}
          {record.check.ok ? "✓ verified" : `⚠ ${record.check.detail}`}
        </span>
      ) : (
        <span className="muted"> · not checked</span>
      )}
    </span>
  );
}

/**
 * The QuickAdd half: "does this need a sign-in?" Toggling a service expands
 * either the account already connected (used as it is) or the form to connect
 * one. `onChange` reports the connection the new source will read with.
 */
export function SourceSignIn({
  target,
  value,
  onChange,
  onTarget,
}: {
  target: string;
  value: string | null;
  onChange: (connection: string | null) => void;
  onTarget: (target: string) => void;
}) {
  const catalog = useConnections();
  const providers = useMemo(
    () => (catalog.data?.providers ?? []).filter((p) => p.kind === "source"),
    [catalog.data],
  );
  const connections = catalog.data?.connections ?? [];
  const [active, setActive] = useState<string | null>(null);
  const [replacing, setReplacing] = useState(false);
  const detected = providerForTarget(target, providers);

  // A pasted URL on a host whose account is connected reads with it: say so
  // by opening that toggle, rather than leaving the choice invisible.
  useEffect(() => {
    if (detected && connections.some((c) => c.provider === detected)) setActive(detected);
  }, [detected, connections.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const provider = providers.find((p) => p.id === active) ?? null;
  const existing = connections.filter((c) => c.provider === active);
  const chosen = existing.find((c) => c.name === value) ?? existing[0] ?? null;

  useEffect(() => {
    onChange(provider && chosen && !replacing ? chosen.name : null);
  }, [provider?.id, chosen?.name, replacing]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!providers.length) return null;
  return (
    <div className="source-sign-in">
      <span className="muted small">Private? Sign in to read it:</span>
      <ProviderToggles
        providers={providers}
        connections={connections}
        active={active}
        label="Sign in to a service"
        onToggle={(id) => {
          setActive(id);
          setReplacing(false);
          if (id === "google_drive" && !target.trim()) onTarget("gdrive:");
        }}
      />
      {provider ? (
        <div className="connect-panel">
          {chosen && !replacing ? (
            <div className="connect-panel__using">
              <span className="small">
                Reading with <ConnectionLine record={chosen} />
              </span>
              {existing.length > 1 ? (
                <select
                  className="text-input"
                  aria-label={`${provider.label} connection`}
                  value={chosen.name}
                  onChange={(e) => onChange(e.target.value)}
                >
                  {existing.map((c) => (
                    <option key={c.name} value={c.name}>
                      {c.name}
                    </option>
                  ))}
                </select>
              ) : null}
              <button type="button" className="btn btn--small" onClick={() => setReplacing(true)}>
                Use another account
              </button>
            </div>
          ) : (
            <ConnectForm
              provider={provider}
              vaultReason={catalog.data?.vault.available ? null : catalog.data?.vault.reason}
              returnTo="/sources"
              onConnected={(record) => {
                setReplacing(false);
                onChange(record.name);
              }}
            />
          )}
        </div>
      ) : null}
    </div>
  );
}

/** How a connection arrived, as the card says it. */
const ADDED_VIA: Record<string, string> = {
  http: "in the UI or over the API",
  mcp: "by an agent over MCP",
  "sign-in": "by signing in",
};

/** The Sources page's list of connected accounts, and every service to connect. */
export function ConnectionsPanel() {
  const queryClient = useQueryClient();
  const catalog = useConnections();
  const [active, setActive] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("connected")) return { kind: "ok", text: `Connected ${params.get("connected")}.` };
    if (params.get("connection_error"))
      return { kind: "error", text: `Sign-in failed: ${params.get("connection_error")}` };
    return null;
  });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["connections"] });
  const test = useMutation({
    mutationFn: (name: string) => api.testConnection(name),
    onSuccess: (data) => {
      refresh();
      setNotice({
        kind: data.check.ok ? "ok" : "error",
        text: `${data.connection.label}: ${data.check.detail}`,
      });
    },
    onError: (err) => setNotice({ kind: "error", text: (err as Error).message }),
  });
  const remove = useMutation({
    mutationFn: (record: ConnectionRecord) =>
      api.removeConnection(record.name, record.used_by.length > 0),
    onSuccess: () => refresh(),
    onError: (err) => setNotice({ kind: "error", text: (err as Error).message }),
  });

  const data = catalog.data;
  const provider = data?.providers.find((p) => p.id === active) ?? null;
  return (
    <section className="connections" aria-labelledby="connections-heading">
      <h2 id="connections-heading">Connections</h2>
      <p className="muted small">
        Accounts this region reads private sources with, and the keys its models are called
        with. Stored encrypted; a key set in the environment still wins.
      </p>
      {notice ? (
        <div className={`banner banner--${notice.kind === "ok" ? "info" : "error"}`} role="status">
          <span>{notice.text}</span>{" "}
          <button className="btn btn--small btn--ghost" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </div>
      ) : null}
      {data && !data.vault.available ? (
        <div className="banner banner--warn">{data.vault.reason}</div>
      ) : null}
      {data?.connections.length ? (
        <ul className="connection-list">
          {data.connections.map((record) => (
            <li key={record.name} className="connection-card">
              <ConnectionLine record={record} />
              <span className="muted small">
                {record.used_by.length
                  ? `Used by ${record.used_by.join(", ")}`
                  : record.stands_in_for
                    ? record.overridden_by_environment
                      ? `${record.stands_in_for} is set in the environment, which wins`
                      : `Stands in for ${record.stands_in_for}`
                    : "Not used by a source yet"}
                {record.details.added_via ? ` · added ${ADDED_VIA[String(record.details.added_via)] ?? ""}` : ""}
              </span>
              <span className="button-row">
                <button
                  className="btn btn--small"
                  disabled={test.isPending}
                  onClick={() => test.mutate(record.name)}
                >
                  Test
                </button>
                <button
                  className="btn btn--small btn--ghost"
                  onClick={() => {
                    const also = record.used_by.length
                      ? ` ${record.used_by.join(", ")} will stop syncing until it is reconnected.`
                      : "";
                    if (window.confirm(`Remove ${record.name}?${also}`)) remove.mutate(record);
                  }}
                >
                  Remove
                </button>
              </span>
            </li>
          ))}
        </ul>
      ) : data ? (
        <p className="muted small">Nothing connected yet.</p>
      ) : null}
      {data ? (
        <>
          <h3 className="connections__sub">Connect</h3>
          <ProviderToggles
            providers={data.providers}
            connections={data.connections}
            active={active}
            onToggle={setActive}
            label="Connect a service"
          />
          {provider ? (
            <div className="connect-panel">
              <ConnectForm
                key={provider.id}
                provider={provider}
                vaultReason={data.vault.available ? null : data.vault.reason}
                onConnected={(record) => {
                  setActive(null);
                  setNotice({
                    kind: "ok",
                    text: `Connected ${record.label}${
                      record.details.account ? ` as ${record.details.account}` : ""
                    }.`,
                  });
                }}
              />
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
