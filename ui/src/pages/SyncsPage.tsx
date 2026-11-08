import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type {
  LedgerDocument,
  PipelineDetail,
  PipelineRow,
  PipelineRun,
  PipelineSettings,
} from "../api/types";

/**
 * Syncs: every source's pipeline, made when the source was added.
 *
 * The pipelines table (what each one does and when, how it went) with an
 * inspector for one, and its document ledger as the drill-down: per document
 * when it was read, changed and is checked next, what the server said, and its
 * quality. Adding a source stays one field on Sources; this is where a pipeline
 * is adjusted in depth. All of it is `/sync/pipelines` (and the MCP
 * `*_sync_pipeline` tools), `services/sync_pipelines.py`.
 */

const KINDS: { id: string; label: string; match: (row: PipelineRow) => boolean }[] = [
  { id: "all", label: "All", match: () => true },
  { id: "web", label: "Web", match: (r) => r.type === "web_collection" || r.type === "api" },
  { id: "repo", label: "Repositories", match: (r) => r.type === "repository" },
  { id: "folder", label: "Folders", match: (r) => r.type.endsWith("_folder") },
  {
    id: "attention",
    label: "Needs attention",
    match: (r) => (r.quality?.warning ?? 0) + (r.quality?.error ?? 0) > 0 || r.last_run?.status === "failed",
  },
];

const LEDGER_STATES: { id: string; label: string }[] = [
  { id: "all", label: "All" },
  { id: "due_soon", label: "Due in the next hour" },
  { id: "changed_week", label: "Changed this week" },
  { id: "attention", label: "Needs attention" },
  { id: "unreadable", label: "Unreadable" },
  { id: "stale", label: "Stale" },
  { id: "little_text", label: "Little or no text" },
];

export function SyncsPage() {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState("all");
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [ledgerOf, setLedgerOf] = useState<string | null>(null);

  const list = useQuery({
    queryKey: ["pipelines"],
    queryFn: api.pipelines,
    refetchInterval: (query) => (query.state.data?.totals.running ? 3000 : 15000),
  });
  const rows = list.data?.pipelines ?? [];
  useEffect(() => {
    if (!selected && rows.length) setSelected(rows[0].name);
  }, [rows, selected]);

  const shown = useMemo(() => {
    const match = KINDS.find((k) => k.id === kind)?.match ?? (() => true);
    const needle = filter.trim().toLowerCase();
    return rows.filter((r) => match(r) && (!needle || r.name.toLowerCase().includes(needle)));
  }, [rows, kind, filter]);

  const runDue = useMutation({
    mutationFn: () => api.syncAll("incremental", false),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["pipelines"] }),
  });

  if (ledgerOf) {
    return <Ledger source={ledgerOf} onBack={() => setLedgerOf(null)} />;
  }

  const totals = list.data?.totals ?? {};
  return (
    <div className="page page--wide syncs">
      <div className="page__header syncs__header">
        <div>
          <h1>Syncs</h1>
          <p className="muted">
            One pipeline per source, made when the source is added. Change how and when each one runs here; adding a
            source stays one field.
          </p>
        </div>
        <div className="button-row">
          <button type="button" className="btn btn--primary" onClick={() => runDue.mutate()} disabled={runDue.isPending}>
            {runDue.isPending ? "Starting…" : "Run all now"}
          </button>
        </div>
      </div>

      <section className="syncs__summary" aria-label="Summary">
        <Stat label="Running" value={totals.running ?? 0} />
        <Stat label="Queued" value={totals.queued ?? 0} />
        <Stat label="Due in the next hour" value={totals.due_next_hour ?? 0} suffix=" pages" />
        <Stat label="Needs attention" value={totals.needs_attention ?? 0} suffix=" files" tone={totals.needs_attention ? "error" : undefined} />
        <Stat label="Paused or manual" value={(totals.paused ?? 0) + (totals.manual ?? 0)} />
      </section>

      {list.isError ? <div className="banner banner--error">{(list.error as Error).message}</div> : null}

      <div className="syncs__body">
        <section className="syncs__table content-block" aria-label="Pipelines">
          <div className="syncs__tools">
            <input
              className="text-input text-input--small"
              type="search"
              aria-label="Filter pipelines"
              placeholder="Filter pipelines"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
            {KINDS.map((k) => (
              <button
                key={k.id}
                type="button"
                className={`btn btn--small${kind === k.id ? " btn--primary" : ""}`}
                aria-pressed={kind === k.id}
                onClick={() => setKind(k.id)}
              >
                {k.label} {rows.filter(k.match).length}
              </button>
            ))}
          </div>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Pipeline</th>
                  <th>Schedule</th>
                  <th>Last run</th>
                  <th>Next run</th>
                  <th>Health</th>
                  <th>Quality</th>
                  <th>Pacing</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((row) => (
                  <tr
                    key={row.name}
                    className={row.name === selected ? "syncs__row syncs__row--selected" : "syncs__row"}
                    onClick={() => setSelected(row.name)}
                  >
                    <td>
                      <button type="button" className="link-button syncs__name" onClick={() => setSelected(row.name)}>
                        {row.name}
                      </button>
                      <div className="muted small">
                        {labelType(row.type)} · {(row.documents ?? 0).toLocaleString()} documents
                      </div>
                    </td>
                    <td>
                      <StatePill row={row} />
                      <div className="muted small">{row.schedule.label}</div>
                    </td>
                    <td>
                      <LastRun row={row} />
                    </td>
                    <td>
                      {row.next_run ? when(row.next_run.at) ?? row.next_run.label : "—"}
                      {row.due_next_hour ? <div className="muted small">{row.due_next_hour.toLocaleString()} pages due</div> : null}
                    </td>
                    <td>
                      {row.health?.percent != null ? (
                        <span className={`pill ${row.health.status === "degraded" ? "pill--danger" : "pill--ok"}`}>
                          {row.health.percent}%
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>
                      <QualityBar quality={row.quality} />
                    </td>
                    <td>
                      {row.pacing.label}
                      {row.pacing.source ? <div className="muted small">{row.pacing.source}</div> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {list.isLoading ? <p className="muted small syncs__empty">Loading pipelines…</p> : null}
            {!list.isLoading && !shown.length ? <p className="muted small syncs__empty">No pipelines match.</p> : null}
          </div>
        </section>

        {selected ? <Inspector name={selected} onOpenDocuments={() => setLedgerOf(selected)} /> : null}
      </div>
    </div>
  );
}

function Inspector({ name, onOpenDocuments }: { name: string; onOpenDocuments: () => void }) {
  const queryClient = useQueryClient();
  const detail = useQuery({
    queryKey: ["pipeline", name],
    queryFn: () => api.pipeline(name),
    refetchInterval: (query) => (query.state.data?.running ? 3000 : 20000),
  });
  const refresh = (data?: PipelineDetail) => {
    if (data) queryClient.setQueryData(["pipeline", name], data);
    queryClient.invalidateQueries({ queryKey: ["pipelines"] });
  };
  const configure = useMutation({
    mutationFn: (settings: Partial<PipelineSettings>) => api.configurePipeline(name, settings),
    onSuccess: refresh,
  });
  const run = useMutation({ mutationFn: () => api.syncSource(name, "incremental", false), onSuccess: () => refresh() });
  const deleteSchedule = useMutation({ mutationFn: () => api.deletePipelineSchedule(name), onSuccess: refresh });
  const deletePipeline = useMutation({ mutationFn: () => api.deletePipeline(name), onSuccess: refresh });
  const restore = useMutation({ mutationFn: () => api.restorePipeline(name), onSuccess: refresh });

  const d = detail.data;
  if (!d) {
    return (
      <aside className="syncs__inspector content-block" aria-label={name}>
        {detail.isError ? <p className="error small">{(detail.error as Error).message}</p> : <span className="spinner" />}
      </aside>
    );
  }
  const error = [configure, deleteSchedule, deletePipeline, restore, run].map((m) => m.error).find(Boolean) as Error | undefined;
  const isWeb = d.type === "web_collection" || d.type === "api";
  const longest = Math.max(1, ...d.stages.map((s) => s.seconds));
  return (
    <aside className="syncs__inspector content-block" aria-label={`${name} pipeline`}>
      <div className="syncs__section">
        <div className="syncs__title">
          <h2>{d.name}</h2>
          <StatePill row={d} />
        </div>
        {d.description ? <p className="muted small">{d.description}</p> : null}
        <div className="button-row">
          {d.state === "deleted" ? (
            <button type="button" className="btn btn--small btn--primary" onClick={() => restore.mutate()}>
              Restore pipeline
            </button>
          ) : (
            <>
              <button type="button" className="btn btn--small btn--primary" onClick={() => run.mutate()} disabled={!!d.running}>
                {d.running ? "Running…" : "Run now"}
              </button>
              {d.state === "scheduled" ? (
                <button type="button" className="btn btn--small" onClick={() => configure.mutate({ paused: true })}>
                  Pause
                </button>
              ) : d.state === "paused" ? (
                <button type="button" className="btn btn--small" onClick={() => configure.mutate({ paused: false })}>
                  Resume
                </button>
              ) : (
                <button type="button" className="btn btn--small" onClick={() => configure.mutate({ schedule: "auto" })}>
                  Schedule again
                </button>
              )}
            </>
          )}
          <button type="button" className="btn btn--small" onClick={onOpenDocuments}>
            Open documents
          </button>
        </div>
        {error ? <p className="error small">{error.message}</p> : null}
      </div>

      <div className="syncs__section">
        <h3>{d.running ? "This run, by stage" : "Last run, by stage"}</h3>
        {d.stages.length ? (
          <div className="syncs__stages">
            {d.stages.map((stage) => (
              <div key={stage.phase} className="syncs__stage">
                <span>{stage.phase}</span>
                <span className="syncs__stage-bar" style={{ width: `${Math.max(2, (stage.seconds / longest) * 100)}%` }} />
                <span className="muted small">{duration(stage.seconds)}</span>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted small">No stage timings recorded yet.</p>
        )}
        {d.running ? (
          <p className="muted small">
            Running since {when(d.running.since)} · heartbeat {ago(d.running.heartbeat_at)}
            {d.running.progress?.total ? ` · ${d.running.progress.current ?? 0} of ${d.running.progress.total}` : ""}
          </p>
        ) : null}
      </div>

      {d.state !== "deleted" ? <SettingsForm detail={d} isWeb={isWeb} onSave={(s) => configure.mutate(s)} saving={configure.isPending} /> : null}

      <div className="syncs__section">
        <h3>Recent runs</h3>
        {d.runs.length ? (
          <ul className="syncs__runs">
            {d.runs.map((r) => (
              <RunLine key={r.id} run={r} />
            ))}
          </ul>
        ) : (
          <p className="muted small">Never run.</p>
        )}
      </div>

      {d.failures.length ? (
        <div className="syncs__section">
          <h3>Failures in the last run, by cause</h3>
          {d.failures.map((f) => (
            <div key={f.cause} className="syncs__failure">
              <span>{f.cause}</span>
              <span className="pill pill--danger">
                {f.documents} {f.documents === 1 ? "document" : "documents"}
              </span>
            </div>
          ))}
        </div>
      ) : null}

      {d.next_due.length ? (
        <div className="syncs__section">
          <h3>Next pages due</h3>
          {d.next_due.slice(0, 5).map((page) => (
            <div key={page.url} className="syncs__due">
              <span className="mono small">{shortUrl(page.url)}</span>
              <span className="muted small">{when(page.at)}</span>
            </div>
          ))}
        </div>
      ) : null}

      {d.state !== "deleted" ? (
        <div className="syncs__section syncs__danger">
          <h3>Delete</h3>
          <p className="muted small">
            Deleting the schedule leaves the pipeline runnable by hand. Deleting the pipeline stops syncing; the indexed
            documents stay until you remove the source.
          </p>
          <div className="button-row">
            <button
              type="button"
              className="btn btn--small btn--danger"
              disabled={d.state === "manual"}
              onClick={() => window.confirm(`Delete ${name}'s schedule? It will sync only when asked.`) && deleteSchedule.mutate()}
            >
              Delete schedule
            </button>
            <button
              type="button"
              className="btn btn--small btn--danger"
              onClick={() =>
                window.confirm(`Delete ${name}'s pipeline? Nothing will sync it; its documents stay searchable.`) &&
                deletePipeline.mutate()
              }
            >
              Delete pipeline
            </button>
          </div>
        </div>
      ) : null}
    </aside>
  );
}

function SettingsForm({
  detail,
  isWeb,
  onSave,
  saving,
}: {
  detail: PipelineDetail;
  isWeb: boolean;
  onSave: (settings: Partial<PipelineSettings>) => void;
  saving: boolean;
}) {
  const s = detail.settings;
  const [schedule, setSchedule] = useState(s.schedule ?? "auto");
  const [minInterval, setMinInterval] = useState(seconds(s.interval_seconds));
  const [maxInterval, setMaxInterval] = useState(seconds(s.max_refresh_seconds));
  const [pace, setPace] = useState(s.min_request_interval_seconds == null ? "" : String(s.min_request_interval_seconds));
  const [budget, setBudget] = useState(s.max_items_per_run == null ? "" : String(s.max_items_per_run));
  const [robots, setRobots] = useState(s.respect_robots ?? true);
  useEffect(() => {
    setSchedule(s.schedule ?? "auto");
    setMinInterval(seconds(s.interval_seconds));
    setMaxInterval(seconds(s.max_refresh_seconds));
    setPace(s.min_request_interval_seconds == null ? "" : String(s.min_request_interval_seconds));
    setBudget(s.max_items_per_run == null ? "" : String(s.max_items_per_run));
    setRobots(s.respect_robots ?? true);
  }, [detail.name, s.schedule, s.interval_seconds, s.max_refresh_seconds, s.min_request_interval_seconds, s.max_items_per_run, s.respect_robots]);

  const save = () =>
    onSave({
      schedule: schedule as PipelineSettings["schedule"],
      ...(isWeb
        ? {
            interval_seconds: toSeconds(minInterval),
            max_refresh_seconds: toSeconds(maxInterval) ?? undefined,
            min_request_interval_seconds: pace === "" ? null : Number(pace),
            max_items_per_run: budget === "" ? null : Number(budget),
            respect_robots: robots,
          }
        : {}),
    });

  return (
    <form
      className="syncs__section syncs__form"
      onSubmit={(e) => {
        e.preventDefault();
        save();
      }}
    >
      <h3>Settings</h3>
      <label className="syncs__field syncs__field--wide">
        <span>Schedule</span>
        <select className="text-input text-input--small" value={schedule} onChange={(e) => setSchedule(e.target.value as PipelineSettings["schedule"])}>
          <option value="auto">{isWeb ? "Adaptive — check each page when due" : "Automatic — on change and every beat"}</option>
          <option value="manual">Manual — only when asked</option>
        </select>
      </label>
      {isWeb ? (
        <>
          <label className="syncs__field">
            <span>Shortest interval (min)</span>
            <input className="text-input text-input--small" inputMode="numeric" value={minInterval} onChange={(e) => setMinInterval(e.target.value)} />
          </label>
          <label className="syncs__field">
            <span>Longest interval (min)</span>
            <input className="text-input text-input--small" inputMode="numeric" value={maxInterval} onChange={(e) => setMaxInterval(e.target.value)} />
          </label>
          <label className="syncs__field">
            <span>Seconds between requests</span>
            <input
              className="text-input text-input--small"
              inputMode="decimal"
              placeholder={detail.pacing.delay_seconds ? `${detail.pacing.delay_seconds} (robots.txt)` : "from robots.txt"}
              value={pace}
              onChange={(e) => setPace(e.target.value)}
            />
          </label>
          <label className="syncs__field">
            <span>Pages per run at most</span>
            <input className="text-input text-input--small" inputMode="numeric" placeholder="automatic" value={budget} onChange={(e) => setBudget(e.target.value)} />
          </label>
          <label className="syncs__check syncs__field--wide">
            <input type="checkbox" checked={robots} onChange={(e) => setRobots(e.target.checked)} /> Respect robots.txt crawl delay
          </label>
        </>
      ) : null}
      <div className="button-row syncs__field--wide">
        <button type="submit" className="btn btn--small btn--primary" disabled={saving}>
          {saving ? "Saving…" : "Save settings"}
        </button>
      </div>
    </form>
  );
}

function Ledger({ source, onBack }: { source: string; onBack: () => void }) {
  const queryClient = useQueryClient();
  const [state, setState] = useState("all");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<LedgerDocument | null>(null);
  const page = useQuery({
    queryKey: ["ledger", source, state, search, offset],
    queryFn: () => api.pipelineDocuments(source, { state, pathContains: search, offset, limit: 100 }),
  });
  const recheck = useMutation({
    mutationFn: (paths: string[]) => api.recheckDocuments(source, paths),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["ledger", source] }),
  });
  const remove = useMutation({
    mutationFn: async (doc: LedgerDocument): Promise<void> => {
      if (doc.url) await api.editSourceUrls(source, { remove: [doc.url] });
      else await api.archiveDocuments([`${source}/${doc.path}`]);
    },
    onSuccess: () => {
      setSelected(null);
      queryClient.invalidateQueries({ queryKey: ["ledger", source] });
      queryClient.invalidateQueries({ queryKey: ["pipelines"] });
    },
  });
  const totals = page.data?.totals ?? {};
  const docs = page.data?.documents ?? [];
  return (
    <div className="page page--wide syncs">
      <nav aria-label="Breadcrumb" className="muted small">
        <button type="button" className="link-button" onClick={onBack}>
          Syncs
        </button>{" "}
        › {source} › Documents
      </nav>
      <div className="page__header syncs__header">
        <div>
          <h1>{source} · documents</h1>
          <p className="muted">When each document was read, when it last changed, when it is checked next, and what state it is in.</p>
        </div>
      </div>
      <div className="syncs__tools" role="group" aria-label="Show">
        {LEDGER_STATES.map((s) => (
          <button
            key={s.id}
            type="button"
            className={`btn btn--small${state === s.id ? " btn--primary" : ""}`}
            aria-pressed={state === s.id}
            onClick={() => {
              setState(s.id);
              setOffset(0);
            }}
          >
            {s.label} {(totals[s.id] ?? 0).toLocaleString()}
          </button>
        ))}
        <input
          className="text-input text-input--small"
          type="search"
          aria-label="Filter documents"
          placeholder="Filter by path"
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setOffset(0);
          }}
        />
      </div>
      <div className="syncs__body">
        <section className="syncs__table content-block" aria-label="Documents">
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Document</th>
                  <th>State</th>
                  <th>Last read</th>
                  <th>Last changed</th>
                  <th>Next check</th>
                  <th>Interval</th>
                  <th>Server said</th>
                </tr>
              </thead>
              <tbody>
                {docs.map((doc) => (
                  <tr
                    key={doc.path}
                    className={selected?.path === doc.path ? "syncs__row syncs__row--selected" : "syncs__row"}
                    onClick={() => setSelected(doc)}
                  >
                    <td>
                      <button type="button" className="link-button mono small" onClick={() => setSelected(doc)}>
                        {doc.path.split("/").pop()}
                      </button>
                      <div className="muted small">{folderOf(doc.path)}</div>
                    </td>
                    <td>
                      <QualityPill status={doc.status} flags={doc.flags} />
                    </td>
                    <td>{when(doc.last_read_at) ?? "never"}</td>
                    <td>{when(doc.last_changed_at) ?? "—"}</td>
                    <td>{doc.next_check_at ? when(doc.next_check_at) : doc.next_check}</td>
                    <td>{doc.interval_seconds ? duration(doc.interval_seconds) : "—"}</td>
                    <td className="mono small">{doc.server_said ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {page.isLoading ? <p className="muted small syncs__empty">Loading documents…</p> : null}
            {!page.isLoading && !docs.length ? <p className="muted small syncs__empty">No documents match.</p> : null}
          </div>
          <div className="button-row syncs__pager">
            <button type="button" className="btn btn--small" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>
              Previous
            </button>
            <span className="muted small">
              {docs.length ? `${offset + 1}–${offset + docs.length}` : 0} of {(totals.matching ?? 0).toLocaleString()}
            </span>
            <button
              type="button"
              className="btn btn--small"
              disabled={page.data?.pagination.next_offset == null}
              onClick={() => setOffset(page.data?.pagination.next_offset ?? offset)}
            >
              Next
            </button>
          </div>
        </section>
        {selected ? (
          <aside className="syncs__inspector content-block" aria-label={selected.path}>
            <div className="syncs__section">
              <div className="syncs__title">
                <h2 className="mono">{selected.path.split("/").pop()}</h2>
                <QualityPill status={selected.status} flags={selected.flags} />
              </div>
              {selected.flags.map((flag) => (
                <p key={flag.code} className={`syncs__reason syncs__reason--${flag.severity}`}>
                  {flag.reason}
                </p>
              ))}
              <dl className="syncs__kv">
                {selected.url ? (
                  <>
                    <dt>URL</dt>
                    <dd className="mono small">{selected.url}</dd>
                  </>
                ) : null}
                <dt>Last read</dt>
                <dd>{when(selected.last_read_at) ?? "never"}</dd>
                <dt>Last changed</dt>
                <dd>{when(selected.last_changed_at) ?? "—"}</dd>
                <dt>Next check</dt>
                <dd>
                  {selected.next_check_at ? when(selected.next_check_at) : selected.next_check}
                  {selected.interval_seconds ? ` · every ${duration(selected.interval_seconds)}` : ""}
                </dd>
                <dt>Validator</dt>
                <dd>{selected.validator ?? "none"}</dd>
                <dt>Server said</dt>
                <dd className="mono">{selected.server_said ?? "—"}</dd>
              </dl>
            </div>
            <div className="syncs__section">
              <div className="button-row">
                <button type="button" className="btn btn--small" onClick={() => recheck.mutate([selected.url ?? selected.path])}>
                  {recheck.isPending ? "Starting…" : "Recheck now"}
                </button>
                <button
                  type="button"
                  className="btn btn--small btn--danger"
                  onClick={() =>
                    window.confirm(`Remove ${selected.path.split("/").pop()} from ${source}?`) && remove.mutate(selected)
                  }
                >
                  Remove from source
                </button>
              </div>
              <p className="muted small">
                Removing a page takes it off the source's list and out of the health score at once.
              </p>
              {recheck.data ? <p className="muted small">Marked {recheck.data.marked_due} due; a sync was started.</p> : null}
              {(recheck.error || remove.error) ? <p className="error small">{((recheck.error || remove.error) as Error).message}</p> : null}
            </div>
          </aside>
        ) : null}
      </div>
    </div>
  );
}

function Stat({ label, value, suffix = "", tone }: { label: string; value: number; suffix?: string; tone?: "error" }) {
  return (
    <div className="syncs__stat">
      <span className="muted small">{label}</span>
      <span className={`syncs__stat-value${tone === "error" ? " error" : ""}`}>
        {value.toLocaleString()}
        {suffix}
      </span>
    </div>
  );
}

function StatePill({ row }: { row: PipelineRow }) {
  if (row.running) {
    const p = row.running.progress;
    return <span className="pill pill--ok">Running{p?.total ? ` ${Math.round(((p.current ?? 0) / p.total) * 100)}%` : ""}</span>;
  }
  if (row.queued.length) return <span className="pill">Queued</span>;
  const label = { scheduled: "Scheduled", paused: "Paused", manual: "Manual", deleted: "Deleted" }[row.state];
  return <span className={`pill${row.state === "deleted" ? " pill--danger" : ""}`}>{label}</span>;
}

function LastRun({ row }: { row: PipelineRow }) {
  const r = row.last_run;
  if (!r) return <span className="muted small">never ran</span>;
  const tone = r.status === "failed" ? "pill--danger" : r.unreadable ? "pill--warn" : "pill--ok";
  return (
    <>
      <span className={`pill ${tone}`}>{r.status === "failed" ? "Failed" : r.unreadable ? `${r.unreadable} unreadable` : "Healthy"}</span>
      <div className="muted small">
        {when(r.finished_at)}
        {r.duration_seconds != null ? ` · ${duration(r.duration_seconds)}` : ""}
      </div>
    </>
  );
}

function RunLine({ run }: { run: PipelineRun }) {
  const [copied, setCopied] = useState(false);
  return (
    <li className="syncs__run">
      <span className={`pill ${run.status === "failed" ? "pill--danger" : run.unreadable ? "pill--warn" : "pill--ok"}`}>{run.status}</span>
      <span className="small">
        {when(run.finished_at)} · {run.duration_seconds != null ? duration(run.duration_seconds) : "—"} · {run.indexed ?? 0} indexed
        {run.unreadable ? ` · ${run.unreadable} unreadable` : ""}
      </span>
      {run.trace_id ? (
        <button
          type="button"
          className="link-button mono small"
          title="Copy the trace id: its spans are in the region's logs (Log Analytics) under it"
          onClick={() => {
            void navigator.clipboard?.writeText(run.trace_id ?? "");
            setCopied(true);
          }}
        >
          {copied ? "copied" : `trace ${run.trace_id.slice(0, 8)}…`}
        </button>
      ) : null}
    </li>
  );
}

function QualityBar({ quality }: { quality: PipelineRow["quality"] }) {
  if (!quality) return <span>—</span>;
  const total = Math.max(1, quality.ok + quality.warning + quality.error);
  const problems = quality.warning + quality.error;
  return (
    <div>
      <div className="syncs__qbar" role="img" aria-label={`${quality.ok} ok, ${quality.warning} warnings, ${quality.error} errors`}>
        <span style={{ width: `${(quality.ok / total) * 100}%` }} className="syncs__qbar-ok" />
        <span style={{ width: `${(quality.warning / total) * 100}%` }} className="syncs__qbar-warn" />
        <span style={{ width: `${(quality.error / total) * 100}%` }} className="syncs__qbar-error" />
      </div>
      <div className="muted small">{problems ? `${problems.toLocaleString()} need attention` : `${quality.ok.toLocaleString()} ok`}</div>
    </div>
  );
}

function QualityPill({ status, flags }: { status: string; flags: LedgerDocument["flags"] }) {
  if (status === "ok") return <span className="pill pill--ok">OK</span>;
  const label = flags[0]?.code.replace("_", " ") ?? status;
  return (
    <span className={`pill ${status === "error" ? "pill--danger" : "pill--warn"}`} title={flags.map((f) => f.reason).join("\n")}>
      {label.charAt(0).toUpperCase() + label.slice(1)}
    </span>
  );
}

function labelType(type: string): string {
  return (
    { web_collection: "Web", api: "API", repository: "Repository", document_folder: "Folder", markdown_folder: "Folder", memory: "Memory" }[type] ??
    type
  );
}

function when(value: string | null | undefined): string | null {
  if (!value) return null;
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return value;
  const today = new Date();
  const sameDay = at.toDateString() === today.toDateString();
  return sameDay
    ? at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : at.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function ago(value: string | null | undefined): string {
  if (!value) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  return seconds < 90 ? `${seconds} s ago` : `${Math.round(seconds / 60)} min ago`;
}

function duration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} m ${Math.round(seconds % 60)} s`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ${Math.round((seconds % 3600) / 60)} m`;
  const days = seconds / 86400;
  return `${Number.isInteger(days) ? days : days.toFixed(1)} days`;
}

function seconds(value: number | null | undefined): string {
  return value == null ? "" : String(Math.round(value / 60));
}

function toSeconds(minutes: string): number | null {
  const n = Number(minutes);
  return minutes.trim() === "" || Number.isNaN(n) ? null : Math.round(n * 60);
}

function shortUrl(url: string): string {
  try {
    const parsed = new URL(url);
    return parsed.pathname.split("/").slice(-2).join("/");
  } catch {
    return url;
  }
}

function folderOf(path: string): string {
  const parts = path.split("/");
  return parts.length > 1 ? parts.slice(0, -1).join("/") : "";
}
