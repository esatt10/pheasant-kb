/** Only static route/action identifiers leave the browser. Never inspect DOM text or inputs. */
export const routes = new Set(["/", "/graph", "/artifacts", "/evaluation", "/tuning", "/sources", "/syncs", "/config"]);
export const actions = new Set(["ask", "artifact.select", "source.select", "pane.select"]);
type UIEvent = { trace_id: string; span_id: string; action: string; route: string; started_at: string; duration_ms: number };
let enabled = false;
let ratio = 1;
let current: { value: string; expires: number } | null = null;
let queue: UIEvent[] = [];
const hex = (length: number) => Array.from(crypto.getRandomValues(new Uint8Array(length)), n => n.toString(16).padStart(2, "0")).join("");
export function configureBrowserTelemetry(value: boolean, sampleRatio = 1) {
  enabled = value;
  ratio = Math.min(1, Math.max(0, sampleRatio));
  if (!enabled) { current = null; queue = []; }
}
export function browserTraceparent(): string | null {
  return enabled && current && Date.now() < current.expires ? current.value : null;
}
export function recordUI(action: string, route: string) {
  if (!enabled || !routes.has(route) || !(action === "navigate" || actions.has(action))) return;
  // OTel's trace-id ratio sampler uses the low 63 bits. Match it for UI roots.
  const trace = hex(16), id = hex(8);
  const sampled = (BigInt("0x" + trace.slice(16)) & ((1n << 63n) - 1n)) < BigInt(Math.floor(ratio * 2 ** 63));
  current = { value: `00-${trace}-${id}-${sampled ? "01" : "00"}`, expires: Date.now() + 30_000 };
  if (sampled && queue.length < 50) queue.push({ trace_id: trace, span_id: id, action, route, started_at: new Date().toISOString(), duration_ms: 0 });
}
export function drainUIEvents(): UIEvent[] { const batch = queue; queue = []; return batch; }
