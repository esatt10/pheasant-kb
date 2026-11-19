import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError, api } from "../api/client";
import type { TraceCalls, UsageCall, UsageSelection, UsageSessionSummary } from "../api/admin";

/** What a drill-down follows: a usage row, named the way the reader saw it. */
export interface TraceTarget {
  kind: "person" | "organization" | "model";
  label: string;
  selection: UsageSelection;
}

const when = (iso: string) =>
  new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

const seconds = (ms: number | null | undefined) =>
  ms == null ? "—" : ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;

const sessionName = (key: string) =>
  key.startsWith("trace:") ? `Single call ${key.slice(6, 14)}…` : key;

/**
 * The traces behind a usage row: its sessions over the period, then one
 * session's calls with the steps each answer took.
 *
 * Nothing is fetched until it is opened: the sessions when a row is chosen,
 * a session's calls when it is. The period selector above is the depth.
 */
export function TraceExplorer({
  days,
  target,
  onClose,
}: {
  days: number;
  target: TraceTarget;
  onClose: () => void;
}) {
  const [session, setSession] = useState<string | null>(null);
  // Questions by default: on a live region nearly every ledger row is the UI
  // polling for jobs and status, which would bury the conversations.
  const [calls, setCalls] = useState<TraceCalls>("questions");
  // What the box says, and the search it settles into a moment after typing
  // stops (or at once on Enter), so each keystroke is not a ledger query.
  const [typed, setTyped] = useState("");
  const [match, setMatch] = useState("");
  useEffect(() => {
    const settled = window.setTimeout(() => setMatch(typed.trim()), 350);
    return () => window.clearTimeout(settled);
  }, [typed]);
  const clearSearch = () => {
    setTyped("");
    setMatch("");
  };
  return (
    <section className="section trace-explorer" aria-label="Traces">
      <nav className="trace-explorer__crumbs" aria-label="Trace navigation">
        <button type="button" className="btn btn--ghost btn--small" onClick={onClose}>
          ‹ Usage
        </button>
        <span aria-hidden="true">/</span>
        {session ? (
          <button type="button" className="btn btn--ghost btn--small" onClick={() => setSession(null)}>
            {target.label}
          </button>
        ) : (
          <strong>{target.label}</strong>
        )}
        {session ? (
          <>
            <span aria-hidden="true">/</span>
            <strong className="trace-explorer__crumb-session">{sessionName(session)}</strong>
          </>
        ) : null}
        <div className="segmented trace-explorer__calls" role="group" aria-label="Which calls">
          {(
            [
              ["questions", "Questions", "The questions and searches the totals count"],
              ["all", "Every call", "Every request, the interface's own polling included"],
            ] as const
          ).map(([value, label, title]) => (
            <button
              key={value}
              type="button"
              className={calls === value ? "active" : ""}
              aria-pressed={calls === value}
              title={title}
              onClick={() => setCalls(value)}
            >
              {label}
            </button>
          ))}
        </div>
      </nav>
      <form
        className="trace-explorer__search"
        role="search"
        onSubmit={(event) => {
          event.preventDefault();
          setMatch(typed.trim());
        }}
      >
        <input
          className="text-input"
          type="search"
          aria-label={session ? "Search this session's calls" : "Search these sessions' calls"}
          placeholder="Search questions and answers, e.g. deploy*worker"
          value={typed}
          maxLength={200}
          onChange={(event) => setTyped(event.target.value)}
        />
        <span className="muted small trace-explorer__search-hint">
          <code>*</code> any characters · <code>?</code> one character
        </span>
      </form>
      {session ? (
        <SessionView
          days={days}
          session={session}
          calls={calls}
          match={match}
          onShowAll={clearSearch}
        />
      ) : (
        <SessionList days={days} target={target} calls={calls} match={match} onOpen={setSession} />
      )}
    </section>
  );
}

function SessionList({
  days,
  target,
  calls,
  match,
  onOpen,
}: {
  days: number;
  target: TraceTarget;
  calls: TraceCalls;
  match: string;
  onOpen: (session: string) => void;
}) {
  const sessions = useQuery({
    queryKey: ["usage-sessions", days, target.selection, calls, match],
    queryFn: () => api.usageSessions(days, target.selection, calls, match),
    retry: false,
  });
  const exported = useExport();
  if (sessions.isLoading) return <p className="muted small">Loading sessions…</p>;
  if (sessions.isError) return <TraceError error={sessions.error as Error} />;
  const data = sessions.data!;
  return (
    <>
      <div className="trace-explorer__head">
        <div>
          <h2 className="section__title">Sessions</h2>
          <p className="muted small">
            {data.sessions.length
              ? match
                ? `${data.sessions.length}${data.truncated ? "+" : ""} with a call matching "${match}" in the last ${days} days, newest first. Calls counts the matching ones.`
                : `${data.sessions.length}${data.truncated ? "+" : ""} in the last ${days} days, newest first. A session is one conversation; a call made outside one stands alone.`
              : match
                ? `No calls matching "${match}" by ${target.label} in the last ${days} days.`
                : calls === "questions"
                ? `No questions or searches by ${target.label} in the last ${days} days. "Every call" shows the rest of the traffic.`
                : `No traced calls for ${target.label} in the last ${days} days.`}
          </p>
        </div>
        <button
          type="button"
          className="btn btn--small"
          disabled={exported.isPending || !data.sessions.length}
          onClick={() => exported.mutate({ days, calls, match, ...target.selection })}
          title={
            match
              ? "Save the calls matching this search as JSON lines"
              : "Save every call in this selection as JSON lines"
          }
        >
          {exported.isPending ? "Exporting…" : "Export JSONL"}
        </button>
      </div>
      <Retention note={data.retention.note} />
      {exported.isError ? <TraceError error={exported.error as Error} /> : null}
      {data.sessions.length ? (
        <div className="table-scroll">
          <table className="data-table trace-explorer__table">
            <thead>
              <tr>
                <th>Session</th>
                <th>Last call</th>
                <th>Calls</th>
                <th>Time</th>
                <th>Person</th>
                <th>Surface</th>
              </tr>
            </thead>
            <tbody>
              {data.sessions.map((row: UsageSessionSummary) => (
                <tr key={row.session}>
                  <td>
                    <button type="button" className="link-button" onClick={() => onOpen(row.session)}>
                      {sessionName(row.session)}
                    </button>
                    {row.errors ? (
                      <span className="pill pill--warn">{row.errors} failed</span>
                    ) : null}
                  </td>
                  <td>{when(row.last_at)}</td>
                  <td>{row.calls}</td>
                  <td>{seconds(row.duration_ms)}</td>
                  <td>{row.principal ?? "—"}</td>
                  <td>{row.modality ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {data.kql ? (
        <KqlBlock
          kql={data.kql}
          note={
            data.kql_note ??
            `The spans behind these calls over the last ${days} days. Change the period above to widen or narrow it.`
          }
        />
      ) : null}
    </>
  );
}

function SessionView({
  days,
  session,
  calls,
  match,
  onShowAll,
}: {
  days: number;
  session: string;
  calls: TraceCalls;
  match: string;
  onShowAll: () => void;
}) {
  const found = useQuery({
    queryKey: ["usage-session", days, session, calls, match],
    queryFn: () => api.usageSession(session, days, calls, match),
    retry: false,
  });
  const exported = useExport();
  if (found.isLoading) return <p className="muted small">Loading calls…</p>;
  if (found.isError)
    return (
      <>
        <TraceError error={found.error as Error} />
        {match ? (
          <button type="button" className="btn btn--small" onClick={onShowAll}>
            Show all calls
          </button>
        ) : null}
      </>
    );
  const data = found.data!;
  return (
    <>
      <div className="trace-explorer__head">
        <div>
          <h2 className="section__title">
            {data.calls.length} call{data.calls.length === 1 ? "" : "s"}
            {match ? ` matching "${match}"` : ""}
          </h2>
          <p className="muted small">
            {when(data.first_at)} to {when(data.last_at)}
            {match ? (
              <>
                {" · "}
                <button type="button" className="link-button" onClick={onShowAll}>
                  Show all calls
                </button>
              </>
            ) : null}
          </p>
        </div>
        <button
          type="button"
          className="btn btn--small"
          disabled={exported.isPending}
          onClick={() => exported.mutate({ days, session, calls, match })}
          title={
            match
              ? "Save this session's calls matching the search as JSON lines"
              : "Save this session's calls as JSON lines"
          }
        >
          {exported.isPending ? "Exporting…" : "Export JSONL"}
        </button>
      </div>
      <Retention note={data.retention.note} />
      {exported.isError ? <TraceError error={exported.error as Error} /> : null}
      <ol className="trace-calls">
        {data.calls.map((call) => (
          <CallCard key={call.span_id} call={call} />
        ))}
      </ol>
      <KqlBlock
        kql={data.kql}
        note="This session's spans and logs, over its own time span plus a few minutes."
      />
    </>
  );
}

/** Phones show a call's question and fold its steps; a wider pane shows them. */
const stepsOpenByDefault = () =>
  !(typeof window !== "undefined" && window.matchMedia?.("(max-width: 860px)").matches);

function CallCard({ call }: { call: UsageCall }) {
  const [copied, setCopied] = useState(false);
  // Read once: a phone turned sideways keeps whatever the reader opened.
  const [stepsOpen] = useState(stepsOpenByDefault);
  const trace = call.answer_trace;
  const tokens = trace?.tokens;
  return (
    <li className={`trace-call${call.status !== "ok" ? " trace-call--failed" : ""}`}>
      <div className="trace-call__head">
        <span className="trace-call__time">{when(call.started_at)}</span>
        <span className="pill">{call.modality}</span>
        <span className="pill">{call.operation}</span>
        {trace?.models?.chat ? (
          <span className="pill pill--accent" title="Answering model">
            {trace.models.chat}
          </span>
        ) : null}
        {trace?.route?.depth ? (
          <span className="pill" title="How long an answer was asked for">
            {trace.route.depth}
            {trace.route.intent ? ` · ${trace.route.intent}` : ""}
          </span>
        ) : null}
        {trace?.routing?.outcome ? (
          <span className="pill" title="How the answering model was chosen">
            {trace.routing.outcome.replace(/_/g, " ")}
          </span>
        ) : null}
        {call.status !== "ok" ? <span className="pill pill--warn">{call.status}</span> : null}
        <span className="trace-call__metrics">
          {tokens ? `${tokens.input_tokens.toLocaleString()} in · ${tokens.output_tokens.toLocaleString()} out · ` : ""}
          {seconds(call.duration_ms)}
        </span>
      </div>
      {call.question ? <p className="trace-call__question">{call.question}</p> : null}
      {call.answer ? (
        <details className="trace-call__answer">
          <summary>Answer</summary>
          <p>{call.answer}</p>
        </details>
      ) : null}
      {trace?.steps?.length ? (
        <details className="trace" open={stepsOpen}>
          <summary className="trace__toggle">{trace.steps.length} steps</summary>
          <ol className="trace__list">
            {trace.steps.map((step, index) => (
              <li key={`${step.name}-${index}`}>
                <span className="trace__name">{step.name}</span>
                <div className="trace__detail">
                  <div>
                    {step.detail}
                    {step.model ? (
                      <span className="trace__model" title="Model this step used">
                        {step.model}
                      </span>
                    ) : null}
                  </div>
                </div>
                <span className="trace__metrics">
                  {step.passages ? <span>{step.passages} passages</span> : null}
                  <span>in {step.input_tokens ?? "—"} tokens</span>
                  <span>out {step.output_tokens ?? "—"} tokens</span>
                  <span>{step.duration_seconds == null ? "—" : `${step.duration_seconds.toFixed(3)}s`}</span>
                </span>
              </li>
            ))}
          </ol>
        </details>
      ) : null}
      <button
        type="button"
        className="link-button trace-call__id"
        title="Copy the trace id: its spans are in Log Analytics under it (OperationId)"
        onClick={() => {
          void navigator.clipboard?.writeText(call.trace_id);
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1500);
        }}
      >
        {copied ? "copied" : `trace ${call.trace_id.slice(0, 12)}…`}
      </button>
    </li>
  );
}

function KqlBlock({ kql, note }: { kql: string; note: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="kql-block">
      <div className="kql-block__head">
        <div>
          <h3 className="kql-block__title">Log Analytics query</h3>
          <p className="muted small">
            {note} Paste it into the Logs of the workspace this region reports to.
          </p>
        </div>
        <button
          type="button"
          className="btn btn--small"
          onClick={() => {
            void navigator.clipboard?.writeText(kql);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          }}
        >
          {copied ? "Copied" : "Copy KQL"}
        </button>
      </div>
      <pre className="kql-block__query">
        <code>{kql}</code>
      </pre>
    </div>
  );
}

function Retention({ note }: { note: string | null }) {
  return note ? <div className="banner banner--info">{note}</div> : null;
}

function TraceError({ error }: { error: Error }) {
  const off = error instanceof ApiError && error.status === 404 && /ledger/i.test(error.message);
  return <div className={off ? "banner banner--info" : "banner banner--error"}>{error.message}</div>;
}

function useExport() {
  return useMutation({
    mutationFn: ({
      days,
      ...selection
    }: UsageSelection & { days: number; session?: string; calls?: TraceCalls; match?: string }) =>
      api.downloadUsageTraces(days, selection),
  });
}
