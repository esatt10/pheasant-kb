import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { McpInfo } from "../api/types";
import { CopyLine } from "./CopyLine";

/**
 * A region reached from another machine: nothing to install, so every client
 * gets the URL, and the dialog says how that client signs in.
 */
function RemoteConnect({ info, url }: { info: McpInfo; url: string }) {
  const auth = info.auth ?? { oauth: false, api_token: false, personal_keys: false };
  const configs = info.client_configs;
  return (
    <>
      <div className="section">
        <h3 className="section__title">Server URL</h3>
        <CopyLine command={url} />
        <p className="muted small" style={{ margin: "10px 0 0" }}>
          {auth.oauth
            ? "Clients sign in with Microsoft: the first call opens a browser, you sign in, then allow the client on this region's consent page. No key to paste."
            : auth.api_token
              ? "Clients send this region's API token as a bearer header."
              : "This region accepts unauthenticated clients."}
          {auth.personal_keys
            ? " A client that cannot open a browser can send a personal key (Connect API → Personal keys) as Authorization: Bearer."
            : ""}
        </p>
      </div>

      {configs["claude-code-command"] ? (
        <div className="section">
          <h3 className="section__title">Claude Code</h3>
          <CopyLine command={configs["claude-code-command"]} />
          {auth.oauth ? (
            <p className="muted small" style={{ margin: "10px 0 0" }}>
              Then run <code>/mcp</code> in Claude Code and choose <strong>Authenticate</strong>.
            </p>
          ) : null}
          {configs["claude-code"] ? (
            <>
              <p className="muted small" style={{ margin: "10px 0 6px" }}>
                Or commit it for a project, as <code>.mcp.json</code>:
              </p>
              <pre className="content-block">{configs["claude-code"]}</pre>
            </>
          ) : null}
        </div>
      ) : null}

      {configs.vscode ? (
        <div className="section">
          <h3 className="section__title">VS Code</h3>
          <p className="muted small" style={{ margin: "0 0 6px" }}>
            <code>.vscode/mcp.json</code> (or <strong>MCP: Add Server… → HTTP</strong> with the URL):
          </p>
          <pre className="content-block">{configs.vscode}</pre>
        </div>
      ) : null}

      <div className="section">
        <h3 className="section__title">Claude apps</h3>
        <p className="muted small" style={{ margin: 0 }}>
          In Claude (web, desktop or Cowork), open <strong>Settings → Connectors → Add custom connector</strong>,
          paste the server URL{auth.oauth ? ", and connect: it signs you in with Microsoft" : ""}.
        </p>
      </div>
    </>
  );
}

/**
 * The MCP surface, made visible.
 *
 * The same knowledge base the chat panel queries is exposed to coding agents
 * over MCP; this dialog shows the transports, the ready-to-paste client
 * config, and the tool list so it is obvious what an attached agent can do.
 */
export function McpDialog({ onClose }: { onClose: () => void }) {
  const info = useQuery({ queryKey: ["mcp-info"], queryFn: api.mcpInfo });

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal modal--wide" onClick={(event) => event.stopPropagation()}>
        <header className="modal__header">
          <h2>Connect a coding agent</h2>
          <button className="btn btn--ghost btn--icon" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        {info.isLoading ? (
          <p className="muted small">
            <span className="spinner" /> Loading…
          </p>
        ) : info.isError ? (
          <div className="banner banner--error">{(info.error as Error).message}</div>
        ) : info.data ? (
          <>
            {!info.data.enabled ? (
              <div className="banner banner--warn">
                MCP is disabled in this config (<code>server.mcp.enabled</code>).
              </div>
            ) : null}

            {info.data.mode === "remote" && info.data.streamable_http_url ? (
              <RemoteConnect info={info.data} url={info.data.streamable_http_url} />
            ) : (
              <>
                <div className="section">
                  <h3 className="section__title">Add to Claude Code or Cursor</h3>
                  <CopyLine command={`pheasant client-config claude-code -c ${info.data.config_path} -o .mcp.json`} />
                  {info.data.streamable_http_url ? (
                    <>
                      <p className="muted small" style={{ margin: "10px 0 6px" }}>
                        Or connect over streamable HTTP:
                      </p>
                      <CopyLine command={info.data.streamable_http_url} />
                    </>
                  ) : null}
                </div>

                {info.data.client_configs["claude-code"] ? (
                  <div className="section">
                    <h3 className="section__title">.mcp.json</h3>
                    <pre className="content-block">{info.data.client_configs["claude-code"]}</pre>
                  </div>
                ) : null}
              </>
            )}

            <div className="section">
              <h3 className="section__title">
                Tools an attached agent gets ({info.data.tools.length})
              </h3>
              <table className="data-table">
                <tbody>
                  {info.data.tools.map((tool) => (
                    <tr key={tool.name}>
                      <td style={{ whiteSpace: "nowrap" }}>
                        <code>{tool.name}</code>
                      </td>
                      <td className="muted small">{tool.description}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}
