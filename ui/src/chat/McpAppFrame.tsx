import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useAppliedTheme } from "../hooks/useTheme";

/**
 * pheasant's MCP App view, hosted the way an MCP host hosts it.
 *
 * The same `ui://pheasant/knowledge-view.html` an agent's host renders for
 * `ask_knowledge_base`, `create_visual` and `get_image` is loaded here into a
 * sandboxed iframe, and this component speaks the *host* half of the MCP Apps
 * protocol (2026-01-26): it answers `ui/initialize`, delivers the result as
 * `ui/notifications/tool-result`, proxies `tools/call get_image` to `/media`
 * (so an image is read under the region's ACL exactly as over MCP) and
 * `tools/call create_visual` to `/assistant/visual` (the view's "Redraw as"),
 * resizes
 * on `ui/notifications/size-changed`, gives the view the whole window when it
 * asks (`ui/request-display-mode`, "fullscreen" and back to "inline"), and turns `ui/message` — "tell me more
 * about this node" — into the next question in the conversation.
 *
 * One renderer for agents and people, so a diagram looks the same in Claude
 * as it does here and a fix to it lands in both.
 *
 * `sandbox="allow-scripts"` without `allow-same-origin` gives the frame an
 * opaque origin: it cannot read this page, its storage, or the API token, and
 * it can reach the region only through the messages answered below.
 */

const PROTOCOL_VERSION = "2026-01-26";
const MIN_HEIGHT = 80;
const MAX_HEIGHT = 900;

/** What this host can give a view: its slot in the chat, or the whole window. */
type DisplayMode = "inline" | "fullscreen";
const DISPLAY_MODES: DisplayMode[] = ["inline", "fullscreen"];

interface JsonRpc {
  jsonrpc: "2.0";
  id?: number | string;
  method?: string;
  params?: Record<string, unknown>;
}

export function McpAppFrame({
  result,
  onAsk,
  title = "Visual",
}: {
  /** The tool result's structured content: an answer, or `{visual, citations}`. */
  result: Record<string, unknown>;
  /** A question the view asks the conversation to go on with. */
  onAsk?: (text: string) => void;
  title?: string;
}) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [height, setHeight] = useState(MIN_HEIGHT * 3);
  const [ready, setReady] = useState(false);
  const [mode, setMode] = useState<DisplayMode>("inline");
  const theme = useAppliedTheme();
  const view = useQuery({
    queryKey: ["mcp-app-knowledge-view"],
    queryFn: api.knowledgeView,
    staleTime: Infinity,
  });

  // Latest values for the message handler, which is registered once.
  const latest = useRef({ result, onAsk, theme, mode });
  latest.current = { result, onAsk, theme, mode };

  useEffect(() => {
    const post = (message: Record<string, unknown>) =>
      frameRef.current?.contentWindow?.postMessage({ jsonrpc: "2.0", ...message }, "*");
    const reply = (id: JsonRpc["id"], result: unknown) => post({ id, result });
    const fail = (id: JsonRpc["id"], message: string) =>
      post({ id, error: { code: -32601, message } });

    const onMessage = async (event: MessageEvent) => {
      // Only our own frame. Its origin is opaque ("null"), so the window
      // identity is the check that means something.
      if (!frameRef.current || event.source !== frameRef.current.contentWindow) return;
      const message = event.data as JsonRpc;
      if (!message || message.jsonrpc !== "2.0" || !message.method) return;
      const params = message.params ?? {};
      switch (message.method) {
        case "ui/initialize":
          reply(message.id, {
            protocolVersion: PROTOCOL_VERSION,
            hostInfo: { name: "pheasant-ui", version: "1" },
            hostCapabilities: { openLinks: {}, serverTools: {} },
            hostContext: {
              theme: latest.current.theme,
              displayMode: latest.current.mode,
              availableDisplayModes: DISPLAY_MODES,
            },
          });
          return;
        case "ui/notifications/initialized":
          setReady(true);
          return;
        case "ui/request-display-mode": {
          // The view asks; the host decides, and answers with the mode it is
          // actually in, which is not always the one asked for.
          const asked = params.mode as DisplayMode;
          if (DISPLAY_MODES.includes(asked)) setMode(asked);
          reply(message.id, {
            mode: DISPLAY_MODES.includes(asked) ? asked : latest.current.mode,
          });
          return;
        }
        case "ui/notifications/size-changed": {
          // Expanded, the frame is the window: its height is not the content's.
          if (latest.current.mode === "fullscreen") return;
          const next = Number(params.height);
          if (Number.isFinite(next)) setHeight(Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, next + 4)));
          return;
        }
        case "tools/call": {
          const name = String(params.name ?? "");
          const args = (params.arguments ?? {}) as Record<string, unknown>;
          try {
            if (name === "get_image" && typeof args.node_id === "string") {
              const image = await api.mediaBase64(args.node_id);
              reply(message.id, { content: [{ type: "image", ...image }] });
              return;
            }
            if (name === "create_visual" && typeof args.request === "string") {
              // "Redraw as": the same passages in another shape, through the
              // same operation MCP's create_visual calls.
              const drawn = await api.visualize({
                request: args.request,
                node_ids: Array.isArray(args.node_ids) ? args.node_ids.map(String) : [],
                kind: typeof args.kind === "string" ? args.kind : null,
              });
              reply(message.id, { content: [], structuredContent: drawn });
              return;
            }
          } catch (error) {
            reply(message.id, {
              isError: true,
              content: [{ type: "text", text: (error as Error).message }],
            });
            return;
          }
          fail(message.id, `tool ${name} is not available in this host`);
          return;
        }
        case "ui/message": {
          const content = params.content as { text?: string } | undefined;
          if (content?.text) latest.current.onAsk?.(content.text);
          reply(message.id, {});
          return;
        }
        case "ui/open-link": {
          const url = String(params.url ?? "");
          // Only http(s): a javascript: or data: URL from a frame must never open.
          if (/^https?:\/\//i.test(url)) window.open(url, "_blank", "noopener,noreferrer");
          reply(message.id, {});
          return;
        }
        default:
          if (message.id !== undefined) fail(message.id, `unsupported: ${message.method}`);
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  // (Re)deliver the result whenever it changes once the view is listening —
  // a deferred visual arrives after the answer that asked for it.
  useEffect(() => {
    if (!ready) return;
    frameRef.current?.contentWindow?.postMessage(
      {
        jsonrpc: "2.0",
        method: "ui/notifications/tool-result",
        params: { content: [], structuredContent: result },
      },
      "*",
    );
  }, [ready, result]);

  useEffect(() => {
    if (!ready) return;
    frameRef.current?.contentWindow?.postMessage(
      { jsonrpc: "2.0", method: "ui/notifications/host-context-changed", params: { theme } },
      "*",
    );
  }, [ready, theme]);

  // Tell the view when its mode changed, however it changed (its own button,
  // Escape here, or the host closing it).
  useEffect(() => {
    if (!ready) return;
    frameRef.current?.contentWindow?.postMessage(
      { jsonrpc: "2.0", method: "ui/notifications/host-context-changed", params: { displayMode: mode } },
      "*",
    );
  }, [ready, mode]);

  // Fullscreen is an overlay over the app, so the app must not scroll behind
  // it, and Escape must always be a way out even when the view has no focus.
  useEffect(() => {
    if (mode !== "fullscreen") return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMode("inline");
    };
    window.addEventListener("keydown", onKey);
    frameRef.current?.focus();
    return () => {
      document.body.style.overflow = previous;
      window.removeEventListener("keydown", onKey);
    };
  }, [mode]);

  if (view.isError) return <div className="muted">The visual view could not be loaded.</div>;
  if (!view.data) return <div className="muted">Loading the visual…</div>;
  const fullscreen = mode === "fullscreen";
  // The frame is the same element in both modes — moving it in the tree would
  // reload it and lose the layout being rearranged — so fullscreen is a style
  // on it, and the slot keeps the chat from collapsing while it is away.
  return (
    <div className="mcp-app-slot" style={{ height }}>
      <iframe
        ref={frameRef}
        className={fullscreen ? "mcp-app-frame mcp-app-frame--fullscreen" : "mcp-app-frame"}
        title={title}
        sandbox="allow-scripts"
        srcDoc={view.data}
        style={fullscreen ? undefined : { height }}
      />
    </div>
  );
}
