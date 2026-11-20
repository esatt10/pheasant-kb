import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError, api } from "../api/client";
import type { UsageReport, UsageTotals } from "../api/admin";
import { TraceExplorer, type TraceTarget } from "./TraceExplorer";

type Metric = "questions" | "tokens" | "cost";

const tokens = (row: UsageTotals) =>
  row.input_tokens + row.output_tokens + row.cached_input_tokens + row.embedding_tokens;

export const compact = (value: number) =>
  value >= 1e9
    ? `${(value / 1e9).toFixed(1)}B`
    : value >= 1e6
      ? `${(value / 1e6).toFixed(1)}M`
      : value >= 1e4
        ? `${Math.round(value / 1e3)}k`
        : value.toLocaleString();

export const money = (value: number | null, currency: string | null) =>
  value === null
    ? "—"
    : `${currency ?? ""} ${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`.trim();

export const bytes = (value: number | null) =>
  value === null
    ? "—"
    : value >= 1024 ** 3
      ? `${(value / 1024 ** 3).toFixed(1)} GB`
      : `${(value / 1024 ** 2).toFixed(1)} MB`;

const duration = (ms: number | null) =>
  ms === null ? "—" : ms >= 1000 ? `≤ ${(ms / 1000).toFixed(ms % 1000 ? 1 : 0)} s` : `≤ ${ms} ms`;

/**
 * What the region was used for: activity, tokens by model, who, how fast.
 *
 * Cost appears only for whoever operates the region; an Admin a customer
 * granted sees the same activity and tokens without the operator's rates.
 */
export function UsagePanel() {
  const [days, setDays] = useState(30);
  const [metric, setMetric] = useState<Metric>("questions");
  // A usage row being followed into its traces; the period above is the depth.
  const [target, setTarget] = useState<TraceTarget | null>(null);
  const exported = useMutation({
    mutationFn: (format: "csv" | "json") => api.downloadUsageExport(days, format),
  });
  const usage = useQuery({
    queryKey: ["usage", days],
    queryFn: () => api.usage(days),
    retry: false,
    refetchInterval: 60_000,
  });

  if (usage.isLoading) return <p className="muted small">Loading…</p>;
  if (usage.isError) {
    const error = usage.error as Error;
    const off = error instanceof ApiError && error.status === 404;
    return (
      <div className={off ? "banner banner--info" : "banner banner--error"}>
        {off
          ? "This region keeps no usage totals. Its operator turns them on with observability.usage."
          : error.message}
      </div>
    );
  }
  const data = usage.data!;
  const { totals, latency, size } = data;
  const shown: Metric = metric === "cost" && !data.cost_visible ? "questions" : metric;

  return (
    <>
      <div className="admin-toolbar">
        <select
          className="text-input text-input--small admin-toolbar__select"
          value={days}
          aria-label="Period"
          onChange={(event) => setDays(Number(event.target.value))}
        >
          {[7, 30, 90, 365].map((value) => (
            <option key={value} value={value}>
              Last {value} days
            </option>
          ))}
        </select>
        <span className="muted small">
          {data.window.from} to {data.window.to} (UTC)
        </span>
        <div className="admin-toolbar__actions">
          {(["csv", "json"] as const).map((format) => (
            <button
              key={format}
              type="button"
              className="btn btn--small"
              disabled={exported.isPending}
              onClick={() => exported.mutate(format)}
              title={
                format === "csv"
                  ? "Every table on this tab, and this month's spend, as CSV files in a ZIP"
                  : "Every measure on this tab, and this month's spend, as one JSON file"
              }
            >
              {exported.isPending && exported.variables === format
                ? "Exporting…"
                : `Export ${format.toUpperCase()}`}
            </button>
          ))}
        </div>
      </div>
      {exported.isError ? (
        <div className="banner banner--error">{(exported.error as Error).message}</div>
      ) : null}
      {target ? (
        <TraceExplorer days={days} target={target} onClose={() => setTarget(null)} />
      ) : (
        <>
      <p className="muted small usage-drill-hint">
        Choose a model, an organization or a person below to see the sessions and traces behind
        their numbers.
      </p>

      <div className="stat-row admin-stats">
        <Stat label="Questions" value={compact(totals.questions)} />
        <Stat label="Searches" value={compact(totals.searches)} />
        <Stat label="Active people" value={compact(totals.active_people)} />
        <Stat label="Tokens" value={compact(tokens(totals))} />
        {data.cost_visible ? (
          <Stat label="Model cost" value={money(totals.cost, data.currency)} />
        ) : null}
        <Stat
          label="Answer p50 / p95"
          value={`${duration(latency.answer.p50_ms)} / ${duration(latency.answer.p95_ms)}`}
        />
        <Stat
          label="Size"
          value={bytes(size.indexed_bytes)}
          note={
            size.limit_gb
              ? `${Math.round((size.limit_used ?? 0) * 100)}% of ${size.limit_gb} GB`
              : `${compact(size.documents)} documents`
          }
        />
      </div>
      {size.full ? (
        <div className="banner banner--warn">
          This knowledge base is at its {size.limit_gb} GB limit. Syncs that add content are
          skipped until documents or a source are removed, or the operator raises the limit.
        </div>
      ) : null}
      {data.unpriced_models.length ? (
        <div className="banner banner--warn">
          The cost total leaves out {data.unpriced_models.join(", ")}: no price is stated for{" "}
          {data.unpriced_models.length === 1 ? "it" : "them"} in the model catalog.
        </div>
      ) : null}

      <section className="section">
        <div className="admin-toolbar">
          <h2 className="section__title" style={{ margin: 0 }}>
            {shown === "questions" ? "Questions" : shown === "tokens" ? "Tokens" : "Model cost"} per
            day
          </h2>
          <div className="tabs admin-tabs--inline" role="tablist">
            {(["questions", "tokens", ...(data.cost_visible ? ["cost"] : [])] as Metric[]).map(
              (name) => (
                <button
                  key={name}
                  role="tab"
                  aria-selected={shown === name}
                  className={shown === name ? "tab active" : "tab"}
                  onClick={() => setMetric(name)}
                >
                  {name === "questions" ? "Questions" : name === "tokens" ? "Tokens" : "Cost"}
                </button>
              ),
            )}
          </div>
        </div>
        <DailyBars data={data} metric={shown} />
      </section>

      <section className="section">
        <h2 className="section__title">By model</h2>
        <Table
          head={["Model", "Kind", "Calls", "Input", "Cached input", "Output", ...(data.cost_visible ? ["Cost"] : [])]}
          onOpen={(index) => {
            const row = data.by_model[index];
            if (row.kind !== "embedding")
              setTarget({ kind: "model", label: row.model, selection: { model: row.model } });
          }}
          openable={(index) => data.by_model[index].kind !== "embedding"}
          rows={data.by_model.map((row) => [
            row.model,
            row.kind,
            compact(row.model_calls),
            compact(row.input_tokens + row.embedding_tokens),
            compact(row.cached_input_tokens),
            compact(row.output_tokens),
            ...(data.cost_visible
              ? [row.priced === false ? "no price stated" : money(row.cost, data.currency)]
              : []),
          ])}
          empty="No model has been called in this period."
        />
      </section>

      <section className="section">
        <h2 className="section__title">By organization</h2>
        <Table
          head={["Organization", "People", "Questions", "Searches", "Tokens", ...(data.cost_visible ? ["Cost"] : [])]}
          onOpen={(index) => {
            const row = data.by_organization[index];
            setTarget({
              kind: "organization",
              label: row.organization,
              selection: { organization: row.tenant_id },
            });
          }}
          rows={data.by_organization.map((row) => [
            row.organization,
            compact(row.active_people),
            compact(row.questions),
            compact(row.searches),
            compact(tokens(row)),
            ...(data.cost_visible ? [money(row.cost, data.currency)] : []),
          ])}
          empty="No activity in this period."
        />
      </section>

      <section className="section">
        <h2 className="section__title">People</h2>
        <Table
          head={["Person", "Organization", "Questions", "Searches", "Tokens", "Last active", ...(data.cost_visible ? ["Cost"] : [])]}
          onOpen={(index) => {
            const row = data.people[index];
            setTarget({ kind: "person", label: row.principal, selection: { principal: row.principal } });
          }}
          rows={data.people.map((row) => [
            row.principal,
            row.organization,
            compact(row.questions),
            compact(row.searches),
            compact(tokens(row)),
            row.last_active ?? "—",
            ...(data.cost_visible ? [money(row.cost, data.currency)] : []),
          ])}
          empty="Nobody signed in has used the region in this period."
        />
        {data.people_omitted ? (
          <p className="muted small">{data.people_omitted} more people are not listed.</p>
        ) : null}
      </section>

      <section className="section">
        <h2 className="section__title">Response time</h2>
        <p className="muted small">
          Read off a fixed histogram: each figure is the upper bound of the range the percentile
          falls in, not an exact time.
        </p>
        <Table
          head={["Operation", "Count", "Failed", "Mean", "p50", "p95"]}
          rows={(["answer", "search"] as const).map((name) => [
            name === "answer" ? "Answer" : "Search",
            compact(latency[name].count),
            compact(latency[name].errors),
            latency[name].mean_ms === null ? "—" : `${latency[name].mean_ms} ms`,
            duration(latency[name].p50_ms),
            duration(latency[name].p95_ms),
          ])}
          empty=""
        />
      </section>
        </>
      )}
    </>
  );
}

function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="stat">
      <span className="stat__value">{value}</span>
      <span className="stat__label">{label}</span>
      {note ? <span className="muted small">{note}</span> : null}
    </div>
  );
}

function Table({
  head,
  rows,
  empty,
  onOpen,
  openable,
}: {
  head: string[];
  rows: string[][];
  empty: string;
  /** Makes each row's first cell open that row's traces. */
  onOpen?: (index: number) => void;
  openable?: (index: number) => boolean;
}) {
  if (!rows.length) return <p className="muted small">{empty}</p>;
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            {head.map((name) => (
              <th key={name}>{name}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {row.map((cell, column) => (
                <td key={column}>
                  {column === 0 && onOpen && (openable?.(index) ?? true) ? (
                    <button
                      type="button"
                      className="link-button usage-open"
                      title={`See the sessions and traces behind ${cell}`}
                      onClick={() => onOpen(index)}
                    >
                      {cell} <span aria-hidden="true">›</span>
                    </button>
                  ) : (
                    cell
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * One series, one axis: a bar per day. Bars are thin, rounded at the data end
 * and anchored to the baseline, with a gap of surface between them; the hit
 * target is the whole column, and the same figures are in the table below it.
 */
function DailyBars({ data, metric }: { data: UsageReport; metric: Metric }) {
  const [hover, setHover] = useState<number | null>(null);
  const value = (row: UsageReport["by_day"][number]) =>
    metric === "questions" ? row.questions : metric === "tokens" ? tokens(row) : (row.cost ?? 0);
  const label = (amount: number) =>
    metric === "cost" ? money(amount, data.currency) : compact(amount);
  const days = data.by_day;
  const peak = Math.max(...days.map(value), 0);
  const width = 720;
  const height = 150;
  const plot = { left: 4, right: 4, top: 10, bottom: 2 };
  const step = (width - plot.left - plot.right) / Math.max(days.length, 1);
  const bar = Math.max(2, step - 2);
  const scale = (amount: number) =>
    peak > 0 ? (amount / peak) * (height - plot.top - plot.bottom) : 0;
  const baseline = height - plot.bottom;
  const active = hover === null ? null : days[hover];

  return (
    <div className="usage-chart">
      <div className="usage-chart__readout muted small" aria-live="polite">
        {active
          ? `${active.day}: ${label(value(active))}`
          : peak > 0
            ? `Peak ${label(peak)} in a day`
            : "Nothing in this period yet."}
      </div>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`${metric} per day, ${data.window.from} to ${data.window.to}`}
        onMouseLeave={() => setHover(null)}
      >
        <line className="usage-chart__axis" x1={plot.left} x2={width - plot.right} y1={baseline} y2={baseline} />
        {days.map((row, index) => {
          const tall = scale(value(row));
          const x = plot.left + index * step + (step - bar) / 2;
          const radius = Math.min(4, bar / 2, tall);
          return (
            <g key={row.day} onMouseEnter={() => setHover(index)}>
              {tall > 0 ? (
                <path
                  className={hover === index ? "usage-chart__bar usage-chart__bar--on" : "usage-chart__bar"}
                  d={`M${x},${baseline} V${baseline - tall + radius} Q${x},${baseline - tall} ${x + radius},${baseline - tall} H${x + bar - radius} Q${x + bar},${baseline - tall} ${x + bar},${baseline - tall + radius} V${baseline} Z`}
                />
              ) : null}
              <rect x={plot.left + index * step} y={0} width={step} height={height} fill="transparent" />
            </g>
          );
        })}
      </svg>
      <div className="usage-chart__ticks muted small">
        <span>{days[0]?.day}</span>
        <span>{days[days.length - 1]?.day}</span>
      </div>
      <details className="usage-chart__table">
        <summary className="muted small">Daily figures</summary>
        <Table
          head={["Day", "Questions", "Searches", "Tokens", ...(data.cost_visible ? ["Cost"] : [])]}
          rows={[...days].reverse().map((row) => [
            row.day,
            compact(row.questions),
            compact(row.searches),
            compact(tokens(row)),
            ...(data.cost_visible ? [money(row.cost, data.currency)] : []),
          ])}
          empty=""
        />
      </details>
    </div>
  );
}
