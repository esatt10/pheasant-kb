import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { harnessApi } from "./api";
import { Pill } from "./RunView";
import type { TelemetryDestination, TraceSpan } from "./types";

/**
 * The Admin page's Harness tab: everything the harness records, from the
 * durable tables (so every replica shows the same thing), and every place its
 * telemetry goes. Each destination can be tested from here, which is how an
 * export path is proven before a cloud collector depends on it; with none
 * configured the row says so rather than looking healthy.
 */

const WINDOWS = [
  [24, "Last 24 hours"],
  [168, "Last 7 days"],
  [720, "Last 30 days"],
] as const;

const seconds = (raw: number | string | null | undefined) => {
  const value = raw === null || raw === undefined ? NaN : Number(raw);
  if (Number.isNaN(value)) return "—";
  return value < 90 ? `${value.toFixed(1)} s` : `${(value / 60).toFixed(1)} min`;
};
const count = (raw: number | string | null | undefined) => {
  const value = raw === null || raw === undefined ? NaN : Number(raw);
  return Number.isNaN(value) ? "—" : value.toLocaleString();
};
const when = (stamp: number) => new Date(stamp * 1000).toLocaleString();
const bytes = (raw: number | string | null | undefined) => {
  const value = Number(raw ?? 0);
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let n = value;
  let unit = 0;
  while (n >= 1024 && unit < units.length - 1) { n /= 1024; unit += 1; }
  return `${n.toFixed(unit ? 1 : 0)} ${units[unit]}`;
};

function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="stat">
      <span className="stat__value">{value}</span>
      <span className="stat__label">{label}</span>
      {note ? <span className="muted small">{note}</span> : null}
    </div>
  );
}

function Table({ head, rows, empty }: { head: string[]; rows: (string | JSX.Element)[][]; empty: string }) {
  if (!rows.length) return <p className="muted small">{empty}</p>;
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead><tr>{head.map((name) => <th key={name}>{name}</th>)}</tr></thead>
        <tbody>{rows.map((row, index) => <tr key={index}>{row.map((cell, column) => <td key={column}>{cell}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

function Destination({ row }: { row: TelemetryDestination }) {
  const client = useQueryClient();
  const test = useMutation({
    mutationFn: () => harnessApi.testTelemetry(row.id),
    onSettled: () => client.invalidateQueries({ queryKey: ["harness-telemetry"] }),
  });
  const last = test.data ?? row.last_test;
  return (
    <li className={`hx-dest${row.configured ? "" : " is-off"}`}>
      <div className="hx-dest__head">
        <strong>{row.label}</strong>
        <Pill value={row.configured ? "connected" : "not configured"} />
        {row.kind ? <span className="muted small">{row.kind}</span> : null}
        <button type="button" className="btn btn--small hx-dest__test" onClick={() => test.mutate()} disabled={test.isPending}>
          {test.isPending ? "Testing…" : "Send a test"}
        </button>
      </div>
      <div className="mono small hx-dest__where">{row.where}{row.protocol ? ` · ${row.protocol}` : ""}{row.headers?.length ? ` · headers: ${row.headers.join(", ")}` : ""}</div>
      <p className="muted small">{row.detail}</p>
      {test.error ? <p className="error small" role="alert">{(test.error as Error).message}</p> : null}
      {last ? (
        <p className={`small hx-dest__result ${last.ok ? "is-ok" : "is-bad"}`} role="status">
          {last.ok ? "✓" : "✗"} {last.message}
          {last.via ? ` (${last.via})` : ""}
          {last.elapsed_ms !== undefined ? ` · ${last.elapsed_ms} ms` : ""}
          {last.at ? ` · ${when(last.at)}` : ""}
          {last.trace_id ? <span className="mono"> · trace {last.trace_id.slice(0, 12)}…</span> : null}
        </p>
      ) : <p className="muted small">Not tested yet.</p>}
    </li>
  );
}

function TraceView({ runId, onClose }: { runId: string; onClose: () => void }) {
  const trace = useQuery({ queryKey: ["harness-trace", runId], queryFn: () => harnessApi.trace(runId) });
  const spans: TraceSpan[] = trace.data?.spans ?? [];
  const start = spans.length ? Math.min(...spans.map((s) => Date.parse(s.start_time))) : 0;
  const end = spans.length ? Math.max(...spans.map((s) => Date.parse(s.end_time))) : 1;
  const span = Math.max(1, end - start);
  return (
    <section className="section hx-trace">
      <div className="admin-toolbar">
        <h3 className="section__title">Trace of {runId}</h3>
        <span className="muted small mono">{trace.data?.trace_id}</span>
        <div className="admin-toolbar__actions"><button type="button" className="btn btn--small" onClick={onClose}>Close</button></div>
      </div>
      {trace.isLoading ? <p className="muted">Loading…</p> : null}
      <ol className="hx-trace__spans">
        {spans.map((s) => {
          const left = ((Date.parse(s.start_time) - start) / span) * 100;
          const width = Math.max(0.6, ((Date.parse(s.end_time) - Date.parse(s.start_time)) / span) * 100);
          return (
            <li key={s.span_id} className={s.parent_span_id ? "is-child" : "is-root"}>
              <span className="hx-trace__name mono" title={JSON.stringify(s.attributes, null, 2)}>{s.name.replace("pheasant.harness.", "")}</span>
              <span className="hx-trace__bar"><span className={`hx-trace__fill${s.status.code === "ERROR" ? " is-error" : ""}`} style={{ left: `${left}%`, width: `${width}%` }} /></span>
              <span className="muted small">{s.duration_ms} ms</span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

export function TelemetryPanel() {
  const [hours, setHours] = useState<number>(168);
  const [traceOf, setTraceOf] = useState<string | null>(null);
  const data = useQuery({ queryKey: ["harness-telemetry", hours], queryFn: () => harnessApi.telemetry(hours), refetchInterval: 10_000 });
  if (data.isLoading) return <p className="muted">Loading harness telemetry…</p>;
  if (data.error) return <div className="banner banner--error">{(data.error as Error).message}</div>;
  const t = data.data!;
  const metricLines = Object.values(t.metrics).flat();
  return (
    <div className="hx-telemetry">
      <div className="admin-toolbar">
        <select className="text-input text-input--small admin-toolbar__select" value={hours} aria-label="Period"
          onChange={(event) => setHours(Number(event.target.value))}>
          {WINDOWS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <span className="muted small">Updated {when(t.generated_at)} · refreshes every 10 s</span>
      </div>

      <section className="section">
        <h3 className="section__title">Runs</h3>
        <div className="stat-row admin-stats">
          <Stat label="Runs" value={count(t.runs.total)} note={`${count(t.runs.finished)} finished`} />
          <Stat label="Succeeded" value={t.runs.success_rate === null ? "—" : `${Math.round(t.runs.success_rate * 100)}%`} note={`${count(t.runs.by_state.succeeded ?? 0)} of ${count(t.runs.finished)}`} />
          <Stat label="Typical time" value={seconds(t.runs.p50_seconds)} note={`slowest 5%: ${seconds(t.runs.p95_seconds)}`} />
          <Stat label="Waiting for approval" value={count(t.approvals_pending)} />
          <Stat label="Unconfirmed writes" value={count(t.effects_uncertain)} note="may have reached their target" />
          <Stat label="Stopped by budget" value={count(t.runs.budget_stops)} />
        </div>
        <div className="hx-chips">{Object.entries(t.runs.by_state).map(([state, n]) => <span key={state} className="hx-chip"><Pill value={state} /> {n}</span>)}</div>
        <Table head={["Kind of run", "Runs", "Succeeded", "Partial", "Failed", "Typical", "Slowest 5%"]} empty="No runs in this period."
          rows={Object.entries(t.workloads).map(([name, w]) => [name.replace(/_/g, " "), count(w.runs), count(w.succeeded), count(w.partial), count(w.failed), seconds(w.p50_seconds), seconds(w.p95_seconds)])} />
      </section>

      <section className="section">
        <h3 className="section__title">What runs spent</h3>
        <div className="stat-row admin-stats">
          <Stat label="Model calls" value={count(t.usage.model_calls)} />
          <Stat label="Model tokens" value={count(t.usage.model_tokens)} />
          <Stat label="Tool calls" value={count(t.usage.tool_calls)} />
          <Stat label="Bytes read" value={bytes(t.usage.bytes_scanned)} />
        </div>
        <Table head={["Step", "Outcome", "Attempts", "Mean time"]} empty="No steps ran in this period."
          rows={t.steps.map((s) => [s.action, <Pill key="o" value={s.status} />, count(s.n), seconds(s.mean_seconds)])} />
      </section>

      <section className="section hx-grid2">
        <div>
          <h3 className="section__title">Checks</h3>
          <Table head={["Outcome", "Checks"]} empty="No checks ran in this period."
            rows={Object.entries(t.verifications).map(([outcome, n]) => [<Pill key="o" value={outcome} />, count(n)])} />
        </div>
        <div>
          <h3 className="section__title">Deliveries</h3>
          <Table head={["Where", "State", "Count"]} empty="Nothing was delivered in this period."
            rows={t.deliveries.map((d) => [d.adapter, <Pill key="s" value={d.state} />, count(d.n)])} />
        </div>
      </section>

      <section className="section">
        <h3 className="section__title">Requests through connected accounts</h3>
        <Table head={["Provider", "Action", "Outcome", "Requests", "Bytes"]} empty="No connection was used in this period."
          rows={t.connection_calls.map((c) => [c.provider, c.action, <Pill key="o" value={c.outcome} />, count(c.n), bytes(c.bytes)])} />
        {t.recent_denials.length ? (
          <>
            <h4 className="hx-subhead">Latest refusals</h4>
            <Table head={["When", "Connection", "Action", "Resource", "Why"]} empty=""
              rows={t.recent_denials.map((d) => [when(d.at), d.connection, d.action, <span key="r" className="mono small">{d.resource}</span>, d.reason ?? ""])} />
          </>
        ) : null}
      </section>

      <section className="section">
        <h3 className="section__title">Where telemetry goes</h3>
        <p className="muted small">
          Every finished run becomes one trace: {count(t.traces.emitted)} emitted in this period, {count(t.traces.exported)} also
          exported to a collector{t.traces.errors ? `, ${t.traces.errors} with errors` : ""}. Test a destination to prove the path works.
        </p>
        <ul className="hx-dests">{t.destinations.map((row) => <Destination key={row.id} row={row} />)}</ul>
      </section>

      <section className="section">
        <h3 className="section__title">Recent runs and their traces</h3>
        <Table head={["Started", "Kind", "State", "Time", "Model calls", "Trace", ""]} empty="No runs in this period."
          rows={t.recent_runs.map((r) => [
            when(r.created_at), r.workload.replace(/_/g, " "), <Pill key="s" value={r.state} />, seconds(r.seconds),
            count(r.usage.model_calls ?? 0),
            <span key="t" className="small">{r.trace}{r.spans ? ` · ${r.spans} spans` : ""}{r.exported ? " · exported" : ""}</span>,
            <span key="a" className="hx-row-actions">
              <button type="button" className="btn btn--small btn--ghost" onClick={() => setTraceOf(r.run_id)}>Trace</button>
              <Link className="btn btn--small btn--ghost" to={`/harness?run=${encodeURIComponent(r.run_id)}`}>Open</Link>
            </span>,
          ])} />
        {traceOf ? <TraceView runId={traceOf} onClose={() => setTraceOf(null)} /> : null}
      </section>

      <details className="section">
        <summary className="section__title">Prometheus series ({metricLines.length})</summary>
        <pre className="hx-metrics mono small">{metricLines.map((m) => `${m.series} ${m.value}`).join("\n") || "No series yet."}</pre>
      </details>
    </div>
  );
}
